"""Workflow guardrail harness for agent/tool failures.

This harness catches failures that happen around the governed tools rather than
inside generated KPI SQL: unsupported shell commands, raw-data reads that bypass
profiles, and blocker panels that ask about invented or non-source-backed
features.
"""
from __future__ import annotations

import argparse
import json
import re
import shlex
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from core.onboarding.harness.trajectory_recorder import load_trajectory
from core.presentation.console_tables import render_markdown_table
from core.storage.workspace_layout import WorkspaceLayout


HARNESS_VERSION = 1
WINDOWS_UNIX_COMMANDS = {"cat", "head", "tail", "grep", "sed", "awk"}
RAW_DATA_READ_COMMANDS = {"cat", "type", "get-content", "import-csv"}
DATA_SUFFIXES = {".csv", ".parquet", ".json", ".jsonl", ".xlsx", ".xls"}
GENERIC_TEMPORAL_PLACEHOLDERS = {"created_at", "updated_at", "timestamp", "event_time"}


@dataclass(frozen=True)
class WorkflowGuardResult:
    workspace: str
    ok: bool
    status: str
    current_json_path: str
    current_markdown_path: str
    evidence_path: str
    error_count: int
    warning_count: int

    def summary(self) -> dict[str, Any]:
        return {
            "artifact_type": "workflow_guard_harness_result",
            "version": HARNESS_VERSION,
            "generated_by": "validate-workflow-guardrails",
            **asdict(self),
        }


class WorkflowGuardHarness:
    def __init__(
        self,
        repo_root: str | Path,
        workspace: str | Path,
        *,
        command_log: str | Path | None = None,
    ) -> None:
        self.repo_root = Path(repo_root).resolve()
        self.workspace = (self.repo_root / workspace).resolve()
        self.workspace_rel = _rel(self.workspace, self.repo_root)
        self.layout = WorkspaceLayout(project_root=self.workspace)
        self.command_log = self._resolve_optional(command_log)

    def run(self) -> WorkflowGuardResult:
        self._validate_workspace()
        self.layout.ensure_runtime_dirs()
        findings = []
        findings.extend(self._check_artifacts())
        findings.extend(self._check_trajectory())
        findings.extend(self._check_command_log())
        error_count = sum(1 for item in findings if item["severity"] == "error")
        warning_count = sum(1 for item in findings if item["severity"] == "warning")
        report = {
            "artifact_type": "workflow_guard_harness/current.json",
            "version": HARNESS_VERSION,
            "generated_by": "validate-workflow-guardrails",
            "workspace": self.workspace_rel,
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "ok": error_count == 0,
            "status": "passed" if error_count == 0 else "failed",
            "summary": {
                "errors": error_count,
                "warnings": warning_count,
                "findings": len(findings),
            },
            "findings": findings,
            "next_commands": self._next_commands(findings),
        }

        report_dir = self.layout.reports_dir / "workflow_guard_harness"
        evidence_dir = self.layout.evidence_dir / "workflow_guard_harness"
        report_dir.mkdir(parents=True, exist_ok=True)
        evidence_dir.mkdir(parents=True, exist_ok=True)
        current_json = report_dir / "current.json"
        current_md = report_dir / "current.md"
        evidence_path = evidence_dir / "current.json"
        current_json.write_text(json.dumps(report, indent=2, default=str) + "\n", encoding="utf-8")
        evidence_path.write_text(json.dumps(report, indent=2, default=str) + "\n", encoding="utf-8")
        current_md.write_text(_render_markdown(report), encoding="utf-8")
        return WorkflowGuardResult(
            workspace=self.workspace_rel,
            ok=bool(report["ok"]),
            status=str(report["status"]),
            current_json_path=_rel(current_json, self.repo_root),
            current_markdown_path=_rel(current_md, self.repo_root),
            evidence_path=_rel(evidence_path, self.repo_root),
            error_count=error_count,
            warning_count=warning_count,
        )

    def _validate_workspace(self) -> None:
        if not self.workspace.exists():
            raise FileNotFoundError(f"workspace not found: {self.workspace_rel}")
        if self.workspace == self.repo_root or not self.workspace.is_relative_to(self.repo_root):
            raise ValueError(f"workspace must be inside repo root: {self.workspace}")

    def _resolve_optional(self, value: str | Path | None) -> Path | None:
        if value is None:
            return None
        path = Path(value)
        return (self.repo_root / path).resolve() if not path.is_absolute() else path.resolve()

    def _check_artifacts(self) -> list[dict[str, Any]]:
        findings = []
        registry = _load_json(self.layout.contracts_dir / "kpi_registry.json")
        mapping = _load_json(self.layout.contracts_dir / "kpi_feature_mapping.json")
        panel = _load_json(self.layout.reports_dir / "blocker_question_panel" / "current.json")
        profile_index = _load_json(self.layout.profiles_dir / "profile_index.json")
        profile_columns = _profile_columns(profile_index)

        for idx, kpi in enumerate(registry.get("kpis") or [], start=1):
            for feature in _split_features(str(kpi.get("cuts") or "")):
                if _is_generic_temporal_placeholder(feature) and _key(feature) not in profile_columns:
                    findings.append(
                        _finding(
                            "error",
                            "invented_temporal_feature",
                            f"Registry KPI #{idx} contains generic temporal feature `{feature}` with no matching allowed profile column.",
                            artifact=_rel(self.layout.contracts_dir / "kpi_registry.json", self.repo_root),
                            recommendation="Regenerate or repair KPI parsing so source terms such as Month(ServiceDate) stay source-backed.",
                        )
                    )

        for kpi in mapping.get("kpis") or []:
            source_text = " ".join(
                str(kpi.get(key) or "") for key in ("name", "description", "metric", "cuts")
            )
            for feature in kpi.get("features") or []:
                if not isinstance(feature, dict):
                    continue
                name = str(feature.get("feature") or "")
                if _is_generic_temporal_placeholder(name) and _key(name) not in profile_columns:
                    findings.append(
                        _finding(
                            "error",
                            "unproven_mapping_feature",
                            f"KPI `{kpi.get('kpi_id')}` has unresolved generic feature `{name}` that is not present in profile evidence.",
                            artifact=_rel(self.layout.contracts_dir / "kpi_feature_mapping.json", self.repo_root),
                            recommendation="Resolve the original source date term before asking a blocker question.",
                            details={"source_text": source_text},
                        )
                    )

        panel_feature = str(panel.get("feature") or "")
        if panel and _is_generic_temporal_placeholder(panel_feature) and _key(panel_feature) not in profile_columns:
            findings.append(
                _finding(
                    "error",
                    "blocker_panel_invented_feature",
                    f"Current blocker panel asks about `{panel_feature}`, but no matching profile column exists.",
                    artifact=_rel(
                        self.layout.reports_dir / "blocker_question_panel" / "current.json",
                        self.repo_root,
                    ),
                    recommendation="Regenerate blocker panel after fixing KPI parser/source-row normalization.",
                )
            )
        if panel and panel.get("status") == "needs_user_answer" and not panel.get("evidence_files"):
            findings.append(
                _finding(
                    "warning",
                    "blocker_panel_missing_evidence_files",
                    "Current blocker panel has no evidence files listed.",
                    artifact=_rel(
                        self.layout.reports_dir / "blocker_question_panel" / "current.json",
                        self.repo_root,
                    ),
                    recommendation="Include source KPI row and profile artifacts before asking the user.",
                )
            )
        return findings

    def _check_command_log(self) -> list[dict[str, Any]]:
        if not self.command_log:
            return []
        if not self.command_log.exists():
            return [
                _finding(
                    "error",
                    "missing_command_log",
                    f"Command log not found: {_rel(self.command_log, self.repo_root)}",
                    recommendation="Pass a JSONL command log produced by session-snapshot or compatible tooling.",
                )
            ]
        records = _load_jsonl(self.command_log)
        findings = []
        commands = [str(item.get("command") or "") for item in records if isinstance(item, dict)]
        for index, record in enumerate(records):
            if not isinstance(record, dict):
                continue
            command = str(record.get("command") or "")
            status = str(record.get("status") or "")
            exit_code = record.get("exit_code")
            if not command:
                continue
            tokens = _command_tokens(command)
            first = tokens[0].lower() if tokens else ""
            if first in WINDOWS_UNIX_COMMANDS:
                findings.append(
                    _finding(
                        "error",
                        "unsupported_shell_command",
                        f"Command uses non-portable shell utility `{tokens[0]}`.",
                        command=command,
                        recommendation="Use project tools, `rg`, or PowerShell-native bounded commands.",
                    )
                )
            if _reads_raw_dataset(command) and not _mentions_profile(command):
                findings.append(
                    _finding(
                        "error",
                        "raw_data_read_before_profile",
                        "Command reads a raw dataset directly without referencing profile evidence.",
                        command=command,
                        recommendation="Read profile_index.json and relevant profile JSON first; use bounded samples only with a reason.",
                    )
                )
            if _failed(status, exit_code) and not _has_retry_or_recovery(commands[index + 1 : index + 4]):
                findings.append(
                    _finding(
                        "warning",
                        "failed_command_without_recovery",
                        "Failed command was not followed by an obvious retry, safer alternative, or project tool.",
                        command=command,
                        recommendation="Retry with the proper project tool or a platform-appropriate bounded command.",
                    )
                )
        return findings

    def _check_trajectory(self) -> list[dict[str, Any]]:
        trajectory_path = self.layout.state_dir / "trajectory.jsonl"
        if not trajectory_path.exists():
            return []
        records = load_trajectory(trajectory_path)
        findings = []
        commands = [str(item.get("command") or "") for item in records if isinstance(item, dict)]
        for index, record in enumerate(records):
            if not isinstance(record, dict):
                continue
            command = str(record.get("command") or "")
            status = str(record.get("status") or "")
            exit_code = record.get("exit_code")
            if command:
                tokens = _command_tokens(command)
                first = tokens[0].lower() if tokens else ""
                if first in WINDOWS_UNIX_COMMANDS:
                    findings.append(
                        _finding(
                            "error",
                            "trajectory_unsupported_shell_command",
                            f"Trajectory command uses non-portable shell utility `{tokens[0]}`.",
                            artifact=_rel(trajectory_path, self.repo_root),
                            command=command,
                            recommendation="Use project tools, `rg`, or PowerShell-native bounded commands.",
                        )
                    )
                if _reads_raw_dataset(command) and not _mentions_profile(command):
                    findings.append(
                        _finding(
                            "error",
                            "trajectory_raw_data_read_before_profile",
                            "Trajectory command reads a raw dataset directly without referencing profile evidence.",
                            artifact=_rel(trajectory_path, self.repo_root),
                            command=command,
                            recommendation="Read profile_index.json and relevant profile JSON first; use bounded samples only with a reason.",
                        )
                    )
            if _failed(status, exit_code) and not _trajectory_has_recovery(
                records[index + 1 : index + 5],
                commands[index + 1 : index + 5],
            ):
                findings.append(
                    _finding(
                        "warning",
                        "trajectory_failed_step_without_recovery",
                        "Trajectory contains a failed step without a nearby retry or recovery event.",
                        artifact=_rel(trajectory_path, self.repo_root),
                        command=command,
                        recommendation="Record a retry/recovery event or rerun with a platform-appropriate project tool.",
                        details={
                            "event_type": record.get("event_type"),
                            "summary": record.get("summary"),
                        },
                    )
                )
        return findings

    def _next_commands(self, findings: list[dict[str, Any]]) -> list[str]:
        commands = []
        if any(item["code"] in {"invented_temporal_feature", "blocker_panel_invented_feature"} for item in findings):
            commands.append(f"uv run prepare-kpi-blocker-panel --workspace {self.workspace_rel} --domain healthcare")
        commands.append(f"uv run validate-workflow-guardrails --workspace {self.workspace_rel}")
        return commands


def _finding(
    severity: str,
    code: str,
    message: str,
    *,
    artifact: str = "",
    command: str = "",
    recommendation: str = "",
    details: dict[str, Any] | None = None,
) -> dict[str, Any]:
    return {
        "severity": severity,
        "code": code,
        "message": message,
        "artifact": artifact,
        "command": command,
        "recommendation": recommendation,
        "details": details or {},
    }


def _load_json(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return {}
    return data if isinstance(data, dict) else {}


def _load_jsonl(path: Path) -> list[dict[str, Any]]:
    rows = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        try:
            data = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(data, dict):
            rows.append(data)
    return rows


def _profile_columns(profile_index: dict[str, Any]) -> set[str]:
    columns = set()
    for profile in profile_index.get("profiles") or []:
        if not isinstance(profile, dict):
            continue
        for column in profile.get("columns") or []:
            if isinstance(column, dict):
                columns.add(_key(column.get("name")))
    return columns


def _split_features(value: str) -> list[str]:
    features = []
    for part in re.split(r"[,;]", value):
        cleaned = part.strip()
        if not cleaned:
            continue
        if "=" in cleaned:
            cleaned = cleaned.split("=", 1)[0].strip()
        if ">" in cleaned:
            cleaned = cleaned.split(">", 1)[0].strip()
        features.append(cleaned)
    return features


def _is_generic_temporal_placeholder(value: str) -> bool:
    return _key(value) in GENERIC_TEMPORAL_PLACEHOLDERS


def _key(value: Any) -> str:
    return re.sub(r"[^a-z0-9]+", "_", str(value or "").strip().lower()).strip("_")


def _command_tokens(command: str) -> list[str]:
    try:
        return shlex.split(command, posix=False)
    except ValueError:
        return command.split()


def _reads_raw_dataset(command: str) -> bool:
    lowered = command.lower()
    tokens = [token.lower() for token in _command_tokens(command)]
    if not tokens or tokens[0] not in RAW_DATA_READ_COMMANDS:
        return False
    return any(suffix in lowered for suffix in DATA_SUFFIXES) and "/datasets/" in lowered.replace("\\", "/")


def _mentions_profile(command: str) -> bool:
    lowered = command.lower().replace("\\", "/")
    return "/generated/profiles/" in lowered or "profile_index.json" in lowered


def _failed(status: str, exit_code: Any) -> bool:
    if status.lower() in {"failed", "error"}:
        return True
    try:
        return int(exit_code) != 0
    except (TypeError, ValueError):
        return False


def _has_retry_or_recovery(commands: list[str]) -> bool:
    recovery_terms = (
        "uv run ",
        "get-content",
        "select-object",
        "profile_index.json",
        "validate-workflow-guardrails",
    )
    return any(any(term in command.lower() for term in recovery_terms) for command in commands)


def _trajectory_has_recovery(next_records: list[dict[str, Any]], next_commands: list[str]) -> bool:
    for record in next_records:
        event_type = str(record.get("event_type") or "").lower()
        if event_type in {"retry", "recovery"}:
            return True
        if record.get("recovery_for"):
            return True
    return _has_retry_or_recovery(next_commands)


def _render_markdown(report: dict[str, Any]) -> str:
    lines = [
        "# Workflow Guard Harness",
        "",
        f"- Workspace: `{report['workspace']}`",
        f"- Status: `{report['status']}`",
        f"- Errors: `{report['summary']['errors']}`",
        f"- Warnings: `{report['summary']['warnings']}`",
        "",
        "## Findings",
        "",
    ]
    findings = report.get("findings") or []
    if findings:
        lines.append(
            render_markdown_table(
                ["Severity", "Code", "Message", "Recommendation"],
                [
                    [
                        item["severity"],
                        item["code"],
                        item["message"],
                        item["recommendation"],
                    ]
                    for item in findings
                ],
            )
        )
    else:
        lines.append("_No workflow guardrail findings._")
    lines.extend(["", "## Next Commands", ""])
    lines.extend(f"- `{command}`" for command in report.get("next_commands") or [])
    lines.append("")
    return "\n".join(lines)


def _rel(path: Path, root: Path) -> str:
    try:
        return str(path.resolve().relative_to(root.resolve())).replace("\\", "/")
    except ValueError:
        return str(path).replace("\\", "/")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Validate workflow guardrails around governed tools.")
    parser.add_argument("--workspace", required=True)
    parser.add_argument("--repo-root", default=".")
    parser.add_argument("--command-log")
    args = parser.parse_args(argv)
    result = WorkflowGuardHarness(
        args.repo_root,
        args.workspace,
        command_log=args.command_log,
    ).run()
    print(json.dumps(result.summary(), indent=2))
    return 0 if result.ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
