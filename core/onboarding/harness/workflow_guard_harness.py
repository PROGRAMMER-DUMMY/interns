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
SLOW_STEP_THRESHOLD_MS = 120000
# Generic shell, git, and runner executables that are always allowed regardless of
# the project tool roster. Kept domain-agnostic on purpose.
GENERIC_COMMANDS = {
    "git", "uv", "python", "python3", "py", "pip", "ruff", "pytest",
    "rg", "ls", "dir", "cd", "echo", "type", "get-content", "get-childitem",
    "select-object", "select-string", "measure-object", "where-object",
    "foreach-object", "test-path", "new-item", "remove-item", "set-content",
    "out-file", "mkdir", "node", "npm", "npx", "pwsh", "powershell", "bash",
    "sh", "cmd", "set-location",
}
# Workflow stages that mean the session is still awaiting work / not terminal.
TERMINAL_STAGE_KEYWORDS = ("complete", "done", "finished", "closed", "passed", "terminal")
AWAITING_STAGE_KEYWORDS = ("await", "pending", "blocked", "needs_", "in_progress", "waiting")


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
        roster_severity: str = "warning",
    ) -> None:
        self.repo_root = Path(repo_root).resolve()
        self.workspace = (self.repo_root / workspace).resolve()
        self.workspace_rel = _rel(self.workspace, self.repo_root)
        self.layout = WorkspaceLayout(project_root=self.workspace)
        self.command_log = self._resolve_optional(command_log)
        self.roster_severity = roster_severity if roster_severity in {"error", "warning"} else "warning"

    def run(self) -> WorkflowGuardResult:
        self._validate_workspace()
        self.layout.ensure_runtime_dirs()
        findings = []
        findings.extend(self._check_artifacts())
        findings.extend(self._check_trajectory())
        findings.extend(self._check_command_log())
        findings.extend(self._check_roster_utilization())
        findings.extend(self._check_stalled_steps())
        findings.extend(self._check_unsupported_commands())
        findings.extend(self._check_failed_without_retry())
        findings.extend(self._check_incomplete_workflow())
        findings.extend(self._check_session_monitored())
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
            lowered = command.lower()
            if "move-item" in lowered and "workspaces/" in lowered.replace("\\", "/"):
                findings.append(
                    _finding(
                        "error",
                        "workspace_source_mutation_without_delete_plan",
                        "Workspace source file was moved or renamed without a delete/mutation plan.",
                        command=command,
                        recommendation="Show a deletion/mutation plan and require explicit confirmation.",
                    )
                )
            if "prepare-kpi-blocker-panel" in lowered:
                findings.append(
                    _finding(
                        "error",
                        "skip_data_quality_before_kpi_blocker",
                        "KPI blocker workflow ran before data-quality review.",
                        command=command,
                        recommendation="Run data quality before KPI blocker questions.",
                        details={
                            "violated_rule_id": "skip_data_quality_before_kpi_or_generation",
                            "replacement_command": f"uv run run-data-quality-harness --workspace {self.workspace_rel}",
                        },
                    )
                )
            if "generate-pipeline-sql" in lowered:
                findings.append(
                    _finding(
                        "error",
                        "generate_code_before_contracts_and_harnesses",
                        "Pipeline SQL generation ran before contract/harness gates.",
                        command=command,
                        recommendation="Run source-to-target, pipeline plan, and layered harness first.",
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

    def _check_roster_utilization(self) -> list[dict[str, Any]]:
        """Warn when a generation panel ships without routing any agent/skill — the
        runtime complement to the roster-coverage unit test. Keeps the available
        agents/skills from sitting idle in the actual workflow output."""
        findings: list[dict[str, Any]] = []
        for label, path in _roster_panels(self.layout):
            if not path.exists():
                continue
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                continue
            if not (data.get("suggested_skills") or data.get("required_specialists")):
                findings.append(
                    _finding(
                        self.roster_severity,
                        "roster_not_routed",
                        f"{label} panel routes no agent or skill; the available roster is idle here.",
                        artifact=_rel(path, self.repo_root),
                        recommendation=(
                            "Attach routing_for(<stage>) so the panel carries required_specialists "
                            "and suggested_skills for the orchestrator to activate."
                        ),
                    )
                )
        return findings

    def _check_stalled_steps(self) -> list[dict[str, Any]]:
        """Catch CLI-agent tool calls that never produced a result (stalled) and
        steps whose recorded duration crossed the slow-step threshold. Both point
        at a hung or runaway tool invocation around the governed workflow."""
        trajectory_path = self.layout.state_dir / "trajectory.jsonl"
        if not trajectory_path.exists():
            return []
        records = load_trajectory(trajectory_path)
        findings: list[dict[str, Any]] = []

        open_starts: dict[str, dict[str, Any]] = {}
        for record in records:
            if not isinstance(record, dict):
                continue
            event_type = str(record.get("event_type") or "").lower()
            tool_id = _tool_id(record)
            if event_type == "tool_start" and tool_id:
                open_starts[tool_id] = record
            elif event_type == "tool_result" and tool_id:
                open_starts.pop(tool_id, None)

            duration = _duration_ms(record)
            if duration is not None and duration > SLOW_STEP_THRESHOLD_MS:
                findings.append(
                    _finding(
                        "warning",
                        "slow_step",
                        f"Trajectory step ran for {int(duration)} ms, above the {SLOW_STEP_THRESHOLD_MS} ms slow-step threshold.",
                        artifact=_rel(trajectory_path, self.repo_root),
                        command=str(record.get("command") or ""),
                        recommendation="Investigate the long-running tool call; split or bound the work, or record why it is expected.",
                        details={
                            "event_type": record.get("event_type"),
                            "duration_ms": duration,
                            "summary": record.get("summary"),
                        },
                    )
                )

        for tool_id, record in open_starts.items():
            findings.append(
                _finding(
                    "warning",
                    "stalled_step",
                    f"Tool start `{tool_id}` has no matching tool_result; the step appears stalled.",
                    artifact=_rel(trajectory_path, self.repo_root),
                    command=str(record.get("command") or ""),
                    recommendation="Record a tool_result/recovery event for the call or rerun it through the proper project tool.",
                    details={
                        "tool_id": tool_id,
                        "summary": record.get("summary"),
                    },
                )
            )
        return findings

    def _check_session_monitored(self) -> list[dict[str, Any]]:
        """Flag a session that clearly DID work but recorded no trajectory, so the
        reliability checks (stalls, retries, tool calls) can't see it. Makes session
        monitoring effectively mandatory — silent, unmonitored runs are surfaced."""
        trajectory_path = self.layout.state_dir / "trajectory.jsonl"
        has_trajectory = bool(load_trajectory(trajectory_path)) if trajectory_path.exists() else False
        if has_trajectory:
            return []
        # Evidence that a governed workflow actually ran in this workspace.
        activity: list[str] = []
        for label, path in (
            ("applied_ops", self.layout.state_dir / "applied_ops.jsonl"),
            ("events", self.layout.state_dir / "events.jsonl"),
        ):
            try:
                if path.exists() and path.stat().st_size > 0:
                    activity.append(label)
            except OSError:
                continue
        if self.layout.solutions_dir.exists() and any(self.layout.solutions_dir.glob("kpi_*")):
            activity.append("generated_solutions")
        if not activity:
            return []  # nothing ran -> nothing to monitor
        return [
            _finding(
                "warning",
                "session_not_monitored",
                "Workflow activity occurred (" + ", ".join(activity) + ") but no session trajectory "
                "was recorded; stalls, retries, and tool calls in this session are not monitored.",
                artifact=_rel(trajectory_path, self.repo_root),
                command=f"uv run record-workspace-trajectory --workspace {self.workspace_rel} --render-only",
                recommendation=(
                    "Record session steps with record-workspace-trajectory (workspace-flow / apply-* "
                    "do this automatically) so reliability checks can see this session."
                ),
            )
        ]

    def _check_unsupported_commands(self) -> list[dict[str, Any]]:
        """Flag recorded commands that invoke an executable which is neither a known
        project tool nor a generic shell/git command. Catches the CLI agent reaching
        for an unsupported or hallucinated tool."""
        trajectory_path = self.layout.state_dir / "trajectory.jsonl"
        if not trajectory_path.exists():
            return []
        records = load_trajectory(trajectory_path)
        if not records:
            return []
        allowed_tools = _allowed_tool_names(self.repo_root)
        if not allowed_tools:
            return []  # no tool roster discoverable -> cannot judge; degrade safely
        findings: list[dict[str, Any]] = []
        for record in records:
            if not isinstance(record, dict):
                continue
            command = str(record.get("command") or "")
            if not command:
                continue
            name = _invoked_tool_name(command)
            if not name:
                continue
            if name in allowed_tools or name in GENERIC_COMMANDS:
                continue
            findings.append(
                _finding(
                    "warning",
                    "unsupported_command",
                    f"Command invokes `{name}`, which is not a known project tool or generic shell/git command.",
                    artifact=_rel(trajectory_path, self.repo_root),
                    command=command,
                    recommendation="Use a documented project tool from .agents/tools.json or a generic shell/git command.",
                )
            )
        return findings

    def _check_failed_without_retry(self) -> list[dict[str, Any]]:
        """Promote a clearly unrecovered failure to an error. ``_check_trajectory``
        already warns on failed-without-recovery; this raises the severity to a
        blocker when the same command/step is not retried and no recovery or
        validation event follows within a short window."""
        trajectory_path = self.layout.state_dir / "trajectory.jsonl"
        if not trajectory_path.exists():
            return []
        records = load_trajectory(trajectory_path)
        if not records:
            return []
        commands = [str(item.get("command") or "") for item in records if isinstance(item, dict)]
        findings: list[dict[str, Any]] = []
        for index, record in enumerate(records):
            if not isinstance(record, dict):
                continue
            status = str(record.get("status") or "")
            exit_code = record.get("exit_code")
            if not _failed(status, exit_code):
                continue
            window = records[index + 1 : index + 5]
            window_commands = commands[index + 1 : index + 5]
            command = str(record.get("command") or "")
            retried = bool(command) and any(
                command.strip() and command.strip() == other.strip() for other in window_commands
            )
            if retried or _trajectory_has_recovery(window, window_commands):
                continue
            findings.append(
                _finding(
                    "error",
                    "failed_without_recovery",
                    "Trajectory step failed and was not retried, recovered, or validated within the following steps.",
                    artifact=_rel(trajectory_path, self.repo_root),
                    command=command,
                    recommendation="Retry the step, route to a recovery/validation tool, or record why the failure is terminal.",
                    details={
                        "event_type": record.get("event_type"),
                        "status": status,
                        "summary": record.get("summary"),
                    },
                )
            )
        return findings

    def _check_incomplete_workflow(self) -> list[dict[str, Any]]:
        """Conservatively flag a workflow session whose latest stage is a non-terminal
        awaiting stage while the trajectory shows no later progress. Only clear cases
        are reported to avoid false positives on in-flight work."""
        findings: list[dict[str, Any]] = []
        panels = _workflow_stage_panels(self.layout)
        if not panels:
            return []

        trajectory_path = self.layout.state_dir / "trajectory.jsonl"
        records = load_trajectory(trajectory_path) if trajectory_path.exists() else []
        progressed = _has_progress_after_panels(records, panels)

        for path, data in panels:
            stage = str(data.get("stage") or "").strip()
            if not stage or not _is_awaiting_stage(stage, data):
                continue
            if progressed:
                continue
            findings.append(
                _finding(
                    "warning",
                    "workflow_incomplete",
                    f"Workflow session is parked at non-terminal stage `{stage}` with no further trajectory progress.",
                    artifact=_rel(path, self.repo_root),
                    recommendation="Advance the workflow with the stage's next tool, or close the session if it is intentionally paused.",
                    details={"stage": stage, "status": data.get("status")},
                )
            )
        return findings


def _roster_panels(layout) -> list[tuple[str, Any]]:
    reports = layout.reports_dir
    return [
        ("KPI generation", reports / "kpi_generation" / "current.json"),
        ("data model generation", reports / "data_model_generation" / "current.json"),
        ("engine generation", reports / "engine_generation" / "current.json"),
    ]


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


def _tool_id(record: dict[str, Any]) -> str:
    """Stable id used to pair a tool_start with its tool_result. Prefers an explicit
    id in metadata, then a tool name, so the pairing tolerates either convention."""
    metadata = record.get("metadata")
    if isinstance(metadata, dict):
        for field in ("tool_id", "id", "call_id", "tool_call_id", "tool_use_id", "tool", "tool_name", "name"):
            value = metadata.get(field)
            if value not in (None, ""):
                return str(value)
    for field in ("tool_id", "tool", "tool_name"):
        value = record.get(field)
        if value not in (None, ""):
            return str(value)
    return ""


def _duration_ms(record: dict[str, Any]) -> float | None:
    metadata = record.get("metadata")
    source = metadata if isinstance(metadata, dict) else record
    value = source.get("duration_ms")
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _allowed_tool_names(repo_root: Path) -> set[str]:
    """Known project tool names, sourced from .agents/tools.json when present, else
    from the pyproject [project.scripts] table. Both are optional; an empty set is a
    safe degrade (the generic-command allowlist still suppresses false positives)."""
    names = _tool_names_from_agents_json(repo_root / ".agents" / "tools.json")
    if names:
        return names
    names = _tool_names_from_pyproject(repo_root / "pyproject.toml")
    if names:
        return names
    # Fallback: the tool roster is a property of the installed package, not the
    # workspace — locate the real repo pyproject relative to this module so the
    # check still works under a workspace-relative or sandboxed repo_root.
    module_repo = Path(__file__).resolve().parents[3]
    return _tool_names_from_pyproject(module_repo / "pyproject.toml")


def _tool_names_from_agents_json(path: Path) -> set[str]:
    if not path.exists():
        return set()
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return set()
    names: set[str] = set()
    if isinstance(data, dict):
        tools = data.get("tools")
        if isinstance(tools, dict):
            names.update(str(key) for key in tools)
        elif isinstance(tools, list):
            for item in tools:
                if isinstance(item, dict):
                    name = item.get("name") or item.get("id")
                    if name:
                        names.add(str(name))
                elif isinstance(item, str):
                    names.add(item)
    return {name.lower() for name in names if name}


def _tool_names_from_pyproject(path: Path) -> set[str]:
    if not path.exists():
        return set()
    names: set[str] = set()
    in_scripts = False
    for line in path.read_text(encoding="utf-8").splitlines():
        stripped = line.strip()
        if stripped.startswith("["):
            in_scripts = stripped == "[project.scripts]"
            continue
        if in_scripts and "=" in stripped and not stripped.startswith("#"):
            name = stripped.split("=", 1)[0].strip().strip('"')
            if name:
                names.add(name.lower())
    return names


def _invoked_tool_name(command: str) -> str:
    """Return the effective executable/tool name for a command. Handles `uv run
    <name>` by returning <name>; otherwise returns the first token. Returns "" when
    no meaningful name can be extracted."""
    tokens = [token for token in _command_tokens(command) if token]
    if not tokens:
        return ""
    first = tokens[0].lower()
    if first == "uv" and len(tokens) >= 3 and tokens[1].lower() == "run":
        candidate = tokens[2]
        if candidate.startswith("-"):
            return "uv"
        return candidate.lower()
    return first


def _workflow_stage_panels(layout) -> list[tuple[Path, dict[str, Any]]]:
    """Workflow session panels that carry a `stage` field, from workflow_sessions and
    the reports tree. Only panels with a stage are returned."""
    panels: list[tuple[Path, dict[str, Any]]] = []
    seen: set[Path] = set()
    candidates: list[Path] = []
    sessions_dir = layout.workflow_sessions_dir
    if sessions_dir.exists():
        candidates.extend(sorted(sessions_dir.glob("*/current.json")))
    reports_dir = layout.reports_dir
    if reports_dir.exists():
        candidates.extend(sorted(reports_dir.glob("**/current.json")))
    for path in candidates:
        resolved = path.resolve()
        if resolved in seen:
            continue
        seen.add(resolved)
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if isinstance(data, dict) and str(data.get("stage") or "").strip():
            panels.append((path, data))
    return panels


def _is_awaiting_stage(stage: str, data: dict[str, Any]) -> bool:
    key = _key(stage)
    status = _key(data.get("status"))
    if any(term in key for term in TERMINAL_STAGE_KEYWORDS):
        return False
    if any(term in status for term in TERMINAL_STAGE_KEYWORDS):
        return False
    return any(term.strip("_") in key for term in AWAITING_STAGE_KEYWORDS) or any(
        term.strip("_") in status for term in AWAITING_STAGE_KEYWORDS
    )


def _has_progress_after_panels(records: list[dict[str, Any]], panels: list[tuple[Path, dict[str, Any]]]) -> bool:
    """Conservative progress signal: treat the workflow as still moving if the
    trajectory's newest event is later than the newest panel timestamp. Without
    comparable timestamps we assume progress (no flag) to avoid false positives."""
    panel_times = [
        _parse_ts(data.get("generated_at") or data.get("updated_at") or data.get("timestamp"))
        for _, data in panels
    ]
    panel_times = [value for value in panel_times if value is not None]
    if not panel_times:
        return True
    latest_panel = max(panel_times)
    event_times = [
        _parse_ts(record.get("timestamp"))
        for record in records
        if isinstance(record, dict)
    ]
    event_times = [value for value in event_times if value is not None]
    if not event_times:
        return False
    return max(event_times) > latest_panel


def _parse_ts(value: Any) -> datetime | None:
    if not value:
        return None
    text = str(value).strip().replace("Z", "+00:00")
    try:
        return datetime.fromisoformat(text)
    except ValueError:
        return None


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
    parser.add_argument(
        "--roster-severity",
        choices=("warning", "error"),
        default="warning",
        help="Severity for the roster_not_routed finding (default: warning).",
    )
    args = parser.parse_args(argv)
    result = WorkflowGuardHarness(
        args.repo_root,
        args.workspace,
        command_log=args.command_log,
        roster_severity=args.roster_severity,
    ).run()
    print(json.dumps(result.summary(), indent=2))
    return 0 if result.ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
