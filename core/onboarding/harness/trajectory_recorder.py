"""Workspace-scoped trajectory recording for governed agent workflows."""
from __future__ import annotations
from core.observability.cost_ledger import anchored

import argparse
import json
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from core.governance.audit_chain import append_audit_record
from core.observability.log_redaction import redact
from core.paths import PROJECT_ROOT
from core.presentation.console_tables import render_markdown_table
from core.storage.workspace_layout import WorkspaceLayout


TRAJECTORY_VERSION = 1


@dataclass(frozen=True)
class TrajectoryRecordResult:
    workspace: str
    ok: bool
    event_count: int
    trajectory_path: str
    current_json_path: str
    current_markdown_path: str
    evidence_path: str

    def summary(self) -> dict[str, Any]:
        return {
            "artifact_type": "trajectory_record_result",
            "version": TRAJECTORY_VERSION,
            "generated_by": "record-workspace-trajectory",
            **asdict(self),
        }


class WorkspaceTrajectoryRecorder:
    def __init__(self, repo_root: str | Path, workspace: str | Path) -> None:
        self.repo_root = Path(repo_root).resolve()
        self.workspace = (self.repo_root / workspace).resolve()
        self.workspace_rel = _rel(self.workspace, self.repo_root)
        self.layout = WorkspaceLayout(project_root=self.workspace)

    @property
    def trajectory_path(self) -> Path:
        return self.layout.state_dir / "trajectory.jsonl"

    def record(
        self,
        *,
        event_type: str,
        status: str = "ok",
        summary: str = "",
        command: str | None = None,
        exit_code: int | None = None,
        artifact: str | None = None,
        decision: str | None = None,
        validation: str | None = None,
        recovery_for: str | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> TrajectoryRecordResult:
        self._validate_workspace()
        self.layout.ensure_runtime_dirs()
        record = {
            "schema_version": TRAJECTORY_VERSION,
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "workspace": self.workspace_rel,
            "event_type": _redact(event_type.strip() or "event"),
            "status": _redact(status.strip() or "ok"),
            "summary": _redact(summary),
        }
        optional = {
            "command": command,
            "exit_code": exit_code,
            "artifact": artifact,
            "decision": decision,
            "validation": validation,
            "recovery_for": recovery_for,
            "metadata": metadata,
        }
        for key, value in optional.items():
            if value is None or value == "":
                continue
            record[key] = _redact(value)

        self.trajectory_path.parent.mkdir(parents=True, exist_ok=True)
        with self.trajectory_path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(record, sort_keys=True, default=str) + "\n")

        # Additive: also append to the tamper-evident audit chain.
        # Failures here MUST NOT break trajectory recording — degrade silently.
        try:
            _audit_chain_path = self.trajectory_path.parent / "audit_chain.jsonl"
            append_audit_record(_audit_chain_path, record)
        except Exception:  # noqa: BLE001
            pass

        return self.render()

    def render(self) -> TrajectoryRecordResult:
        self._validate_workspace()
        self.layout.ensure_runtime_dirs()
        records = load_trajectory(self.trajectory_path)
        report = {
            "artifact_type": "trajectory/current.json",
            "version": TRAJECTORY_VERSION,
            "generated_by": "record-workspace-trajectory",
            "workspace": self.workspace_rel,
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "event_count": len(records),
            "summary": _summarize(records),
            "events": records[-100:],
        }
        report_dir = self.layout.reports_dir / "trajectory"
        evidence_dir = self.layout.evidence_dir / "trajectory"
        report_dir.mkdir(parents=True, exist_ok=True)
        evidence_dir.mkdir(parents=True, exist_ok=True)
        current_json = report_dir / "current.json"
        current_md = report_dir / "current.md"
        evidence_path = evidence_dir / "current.json"
        payload = json.dumps(report, indent=2, default=str) + "\n"
        current_json.write_text(payload, encoding="utf-8")
        evidence_path.write_text(payload, encoding="utf-8")
        current_md.write_text(_render_markdown(report), encoding="utf-8")
        return TrajectoryRecordResult(
            workspace=self.workspace_rel,
            ok=True,
            event_count=len(records),
            trajectory_path=_rel(self.trajectory_path, self.repo_root),
            current_json_path=_rel(current_json, self.repo_root),
            current_markdown_path=_rel(current_md, self.repo_root),
            evidence_path=_rel(evidence_path, self.repo_root),
        )

    def _validate_workspace(self) -> None:
        if not self.workspace.exists():
            raise FileNotFoundError(f"workspace not found: {self.workspace_rel}")
        if self.workspace == self.repo_root or not self.workspace.is_relative_to(self.repo_root):
            raise ValueError(f"workspace must be inside repo root: {self.workspace}")


def record_trajectory_event_safe(
    repo_root: str | Path,
    workspace: str | Path,
    *,
    event_type: str,
    status: str = "ok",
    summary: str = "",
    command: str | None = None,
    exit_code: int | None = None,
    artifact: str | None = None,
    decision: str | None = None,
    validation: str | None = None,
    recovery_for: str | None = None,
    metadata: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Best-effort event recording for controlled workflow tools.

    Recording must not break the primary workflow. If the workspace is missing or
    the report cannot be written, return a small skipped payload instead.
    """
    try:
        result = WorkspaceTrajectoryRecorder(repo_root, workspace).record(
            event_type=event_type,
            status=status,
            summary=summary,
            command=command,
            exit_code=exit_code,
            artifact=artifact,
            decision=decision,
            validation=validation,
            recovery_for=recovery_for,
            metadata=metadata,
        )
        return result.summary()
    except Exception as exc:
        return {
            "artifact_type": "trajectory_record_result",
            "version": TRAJECTORY_VERSION,
            "generated_by": "record-workspace-trajectory",
            "workspace": str(workspace),
            "ok": False,
            "status": "skipped",
            "reason": f"{type(exc).__name__}: {exc}",
        }


def load_trajectory(path: str | Path) -> list[dict[str, Any]]:
    source = Path(path)
    if not source.exists():
        return []
    records: list[dict[str, Any]] = []
    for line in source.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        try:
            value = json.loads(line)
        except json.JSONDecodeError:
            records.append({"event_type": "parse_error", "status": "error", "summary": "Invalid JSONL trajectory row."})
            continue
        if isinstance(value, dict):
            records.append(value)
    return records


def _summarize(records: list[dict[str, Any]]) -> dict[str, Any]:
    failures = [item for item in records if _is_failure(item)]
    recoveries = [item for item in records if str(item.get("event_type") or "").lower() in {"retry", "recovery"}]
    validations = [item for item in records if str(item.get("event_type") or "").lower() == "validation"]
    commands = [item for item in records if item.get("command")]
    return {
        "failures": len(failures),
        "recoveries": len(recoveries),
        "validations": len(validations),
        "commands": len(commands),
        "last_status": records[-1].get("status") if records else "empty",
    }


def _render_markdown(report: dict[str, Any]) -> str:
    summary = report.get("summary") or {}
    events = report.get("events") or []
    failures = summary.get("failures", 0)
    status_icon = "FAILED" if failures else "OK"

    lines = [
        "# Workspace Trajectory",
        "",
        f"- **Workspace:** `{report.get('workspace')}`",
        f"- **Generated at:** `{report.get('generated_at')}`",
        f"- **Status:** {status_icon} | Events: {report.get('event_count', 0)} | Failures: {failures}",
        "",
    ]

    # Decisions section — feature mappings and relationship approvals
    decision_events = [e for e in events if e.get("decision")]
    if decision_events:
        lines += ["## Decisions Made", ""]
        for e in decision_events:
            ts = str(e.get("timestamp") or "")[:10]
            lines.append(f"- **{ts}** {e['decision']}")
        lines.append("")

    # KPI / artifact events
    artifact_events = [e for e in events if e.get("artifact")]
    if artifact_events:
        lines += ["## Artifacts Generated", ""]
        for e in artifact_events:
            ts = str(e.get("timestamp") or "")[:10]
            lines.append(f"- **{ts}** `{e['artifact']}` — {e.get('summary', '')}")
        lines.append("")

    # Failures
    failure_events = [e for e in events if _is_failure(e)]
    if failure_events:
        lines += ["## Failures", ""]
        for e in failure_events:
            ts = str(e.get("timestamp") or "")[:19]
            lines.append(f"- **{ts}** {e.get('summary', '')} (exit={e.get('exit_code', '?')})")
        lines.append("")

    # Recent event log (last 20, only if no dedicated sections covered them)
    recent = [e for e in events[-20:] if not e.get("decision") and not e.get("artifact")]
    if recent:
        lines += ["## Recent Events", ""]
        lines.append(render_markdown_table(
            ["Time", "Type", "Status", "Summary"],
            [[str(e.get("timestamp") or "")[:19], e.get("event_type", ""),
              e.get("status", ""), e.get("summary", "")] for e in recent],
        ))
        lines.append("")

    return "\n".join(lines)


def _is_failure(item: dict[str, Any]) -> bool:
    status = str(item.get("status") or "").lower()
    exit_code = item.get("exit_code")
    return status in {"failed", "failure", "error"} or (isinstance(exit_code, int) and exit_code != 0)


def _redact(value: Any) -> Any:
    if isinstance(value, str):
        return redact(value)
    if isinstance(value, list):
        return [_redact(item) for item in value]
    if isinstance(value, dict):
        return {str(key): _redact(item) for key, item in value.items()}
    return value


def _rel(path: Path, root: Path) -> str:
    try:
        return path.resolve().relative_to(root.resolve()).as_posix()
    except ValueError:
        return str(path)


def _metadata(value: str | None) -> dict[str, Any] | None:
    if not value:
        return None
    try:
        parsed = json.loads(value)
    except json.JSONDecodeError as exc:
        raise SystemExit(f"--metadata-json must be valid JSON: {exc}") from exc
    if not isinstance(parsed, dict):
        raise SystemExit("--metadata-json must decode to a JSON object")
    return parsed



@anchored("record-workspace-trajectory")
def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description="Record or render a workspace trajectory event.")
    parser.add_argument("--workspace", required=True)
    parser.add_argument("--event-type", default="event")
    parser.add_argument("--status", default="ok")
    parser.add_argument("--summary", default="")
    parser.add_argument("--command")
    parser.add_argument("--exit-code", type=int)
    parser.add_argument("--artifact")
    parser.add_argument("--decision")
    parser.add_argument("--validation")
    parser.add_argument("--recovery-for")
    parser.add_argument("--metadata-json")
    parser.add_argument("--render-only", action="store_true")
    args = parser.parse_args(argv)

    recorder = WorkspaceTrajectoryRecorder(PROJECT_ROOT, args.workspace)
    if args.render_only:
        result = recorder.render()
    else:
        result = recorder.record(
            event_type=args.event_type,
            status=args.status,
            summary=args.summary,
            command=args.command,
            exit_code=args.exit_code,
            artifact=args.artifact,
            decision=args.decision,
            validation=args.validation,
            recovery_for=args.recovery_for,
            metadata=_metadata(args.metadata_json),
        )
    print(json.dumps(result.summary(), indent=2))


if __name__ == "__main__":
    main()
