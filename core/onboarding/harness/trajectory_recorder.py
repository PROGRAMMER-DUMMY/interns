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
from core.storage.workspace_lock import workspace_lock


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

    @property
    def _summary_state_path(self) -> Path:
        return self.layout.state_dir / "trajectory_summary_state.json"

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

        # Locked: this fires on every tool_start/tool_result for every CLI
        # command, so two racing commands appending to the SAME
        # trajectory.jsonl (this repo's own workflow-guard/delegation paths
        # both write it too) is a real, not theoretical, race.
        with workspace_lock(self.workspace):
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

            # Incrementally maintain the summary counter from the ONE new
            # record instead of a full trajectory.jsonl re-scan (this fires
            # on every tool_start/tool_result -- a full re-parse per append
            # is O(n) per write / O(n^2) total over a workspace's lifetime).
            # Bootstraps once via a real full scan if the counter is
            # missing/corrupt (a pre-existing trajectory.jsonl from before
            # this fix, or an out-of-band writer) -- that scan already
            # includes the record just appended above, so no double-count.
            state = _load_summary_state(self._summary_state_path)
            if state is None:
                state = _bootstrap_summary_state(self.trajectory_path)
            else:
                state = _apply_record_to_summary_state(state, record)
            _save_summary_state(self._summary_state_path, state)

        return self.render()

    def render(self) -> TrajectoryRecordResult:
        self._validate_workspace()
        self.layout.ensure_runtime_dirs()
        # Locked (reentrant -- a no-op nested acquire when called from
        # record(), a real one when called standalone): reads trajectory.jsonl
        # then writes 3 files from it; an unlocked concurrent call could
        # interleave/truncate mid-write on any of them.
        with workspace_lock(self.workspace):
            # event_count/summary come from the persisted counter (O(1),
            # bootstrapped via one full scan if absent) rather than a full
            # re-parse of trajectory.jsonl; `events` only ever needs the last
            # 100 records (same as before), read via a bounded tail-read
            # instead of the whole file. current.json's OUTPUT SHAPE is
            # unchanged -- this is a computation-cost fix, not a behavior
            # change (see tests/regressions/test_trajectory_recorder_incremental_summary.py).
            state = _load_summary_state(self._summary_state_path)
            if state is None:
                state = _bootstrap_summary_state(self.trajectory_path)
                _save_summary_state(self._summary_state_path, state)
            event_count = state["event_count"]
            summary = {
                "failures": state["failures"],
                "recoveries": state["recoveries"],
                "validations": state["validations"],
                "commands": state["commands"],
                "last_status": state["last_status"],
            }
            report = {
                "artifact_type": "trajectory/current.json",
                "version": TRAJECTORY_VERSION,
                "generated_by": "record-workspace-trajectory",
                "workspace": self.workspace_rel,
                "generated_at": datetime.now(timezone.utc).isoformat(),
                "event_count": event_count,
                "summary": summary,
                "events": _tail_records(self.trajectory_path, 100),
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
            event_count=event_count,
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


def _parse_trajectory_line(line: str) -> dict[str, Any] | None:
    """Parse one raw JSONL line into a record dict, or None to skip it
    (blank line / non-dict JSON value). An unparseable line becomes a
    synthetic ``parse_error`` record rather than being silently dropped --
    shared by both the full-file ``load_trajectory`` and the tail-read path
    so both see the exact same rows for the same input."""
    if not line.strip():
        return None
    try:
        value = json.loads(line)
    except json.JSONDecodeError:
        return {"event_type": "parse_error", "status": "error", "summary": "Invalid JSONL trajectory row."}
    if isinstance(value, dict):
        return value
    return None


def load_trajectory(path: str | Path) -> list[dict[str, Any]]:
    """Full trajectory history. Real callers beyond this module need the
    WHOLE history (workflow_guard_harness.py's reliability checks scan
    across every past event, not just the tail) -- this function's contract
    is intentionally unchanged; only WorkspaceTrajectoryRecorder's internal
    render()/record() hot path was changed to avoid calling this on every
    append (see _tail_records / the summary-state counter below)."""
    source = Path(path)
    if not source.exists():
        return []
    records: list[dict[str, Any]] = []
    for line in source.read_text(encoding="utf-8").splitlines():
        parsed = _parse_trajectory_line(line)
        if parsed is not None:
            records.append(parsed)
    return records


def _tail_lines(path: Path, n: int, chunk_size: int = 8192) -> list[str]:
    """Return up to the last *n* raw lines of a text file, reading from the
    end in bounded chunks instead of the whole file. Standard seek-backward
    tail-read; used so trajectory_recorder's render() doesn't pay an O(file
    size) cost on every append just to get the last ~100 events."""
    if not path.exists():
        return []
    with path.open("rb") as f:
        f.seek(0, 2)  # seek to end
        pos = f.tell()
        data = b""
        while pos > 0 and data.count(b"\n") <= n:
            read_size = min(chunk_size, pos)
            pos -= read_size
            f.seek(pos)
            data = f.read(read_size) + data
    text = data.decode("utf-8", errors="replace")
    return text.splitlines()[-n:]


def _tail_records(path: Path, n: int) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    for line in _tail_lines(path, n):
        parsed = _parse_trajectory_line(line)
        if parsed is not None:
            records.append(parsed)
    return records[-n:]


_EMPTY_SUMMARY_STATE: dict[str, Any] = {
    "event_count": 0,
    "failures": 0,
    "recoveries": 0,
    "validations": 0,
    "commands": 0,
    "last_status": "empty",
}


def _load_summary_state(path: Path) -> dict[str, Any] | None:
    """Return the persisted incremental summary counter, or None if it's
    missing/unreadable/corrupt -- the caller bootstraps via a real full scan
    in that case (a pre-existing trajectory.jsonl from before this fix, or an
    out-of-band writer that never touched the counter file)."""
    if not path.exists():
        return None
    try:
        state = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(state, dict) or "event_count" not in state:
        return None
    return state


def _save_summary_state(path: Path, state: dict[str, Any]) -> None:
    path.write_text(json.dumps(state, sort_keys=True), encoding="utf-8")


def _apply_record_to_summary_state(state: dict[str, Any], record: dict[str, Any]) -> dict[str, Any]:
    """Fold ONE new record into the previous counter state -- O(1), mirrors
    _summarize()'s per-record logic exactly so the incremental and full-scan
    paths always agree."""
    updated = dict(state)
    updated["event_count"] = state.get("event_count", 0) + 1
    if _is_failure(record):
        updated["failures"] = state.get("failures", 0) + 1
    event_type = str(record.get("event_type") or "").lower()
    if event_type in {"retry", "recovery"}:
        updated["recoveries"] = state.get("recoveries", 0) + 1
    if event_type == "validation":
        updated["validations"] = state.get("validations", 0) + 1
    if record.get("command"):
        updated["commands"] = state.get("commands", 0) + 1
    updated["last_status"] = record.get("status")
    return updated


def _bootstrap_summary_state(trajectory_path: Path) -> dict[str, Any]:
    """One-time full scan (only when the persisted counter is absent/corrupt)
    to compute the correct starting state -- reuses the existing
    load_trajectory + _summarize logic verbatim, so the bootstrap value is
    byte-for-byte what the old always-full-scan code would have computed."""
    records = load_trajectory(trajectory_path)
    summary = _summarize(records)
    return {"event_count": len(records), **summary}


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
