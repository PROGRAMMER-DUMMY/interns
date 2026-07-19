from core.observability.cost_ledger import anchored
import argparse
import hashlib
import json
import re
import sys
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

_BOOTSTRAP_ROOT = Path(__file__).resolve().parents[1]
if str(_BOOTSTRAP_ROOT) not in sys.path:
    sys.path.insert(0, str(_BOOTSTRAP_ROOT))

from core.observability.log_redaction import redact as _shared_redact  # noqa: E402
from core.paths import PROJECT_ROOT  # noqa: E402

SESSION_ROOT = PROJECT_ROOT / ".agents" / "sessions"
DEFAULT_SESSION_DIR = SESSION_ROOT / "current"
ALIAS_DIR = SESSION_ROOT / "_aliases"
CURRENT_SESSION_POINTER = SESSION_ROOT / "current_session.txt"
LOCAL_ABSOLUTE_PATH_PATTERN = re.compile(
    r"(?i)(?:[A-Z]:[\\/](?:Users|Documents and Settings)[\\/][^\s`'\"<>]+|"
    r"/(?:Users|home)/[^\s`'\"<>]+)"
)


@dataclass(frozen=True)
class SnapshotPaths:
    root: str
    events: str
    transcript: str
    compact: str
    intent_verification: str
    intent_verification_json: str
    commands: str
    file_changes: str
    decisions: str
    snapshot: str


@dataclass
class SessionSnapshot:
    session_id: str
    workspace: str = ""
    tool: str = ""
    status: str = "active"
    started_at: str = ""
    updated_at: str = ""
    finished_at: str = ""
    event_count: int = 0
    paths: SnapshotPaths | None = None
    warnings: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data["paths"] = asdict(self.paths) if self.paths else {}
        return data


def start_session(
    repo_root: Path,
    session_dir: Path,
    *,
    workspace: str = "",
    tool: str = "",
    session_id: str = "",
) -> SessionSnapshot:
    now = _now()
    session_dir.mkdir(parents=True, exist_ok=True)
    sid = session_id or _default_session_id(workspace=workspace, tool=tool, now=now)
    snapshot = SessionSnapshot(
        session_id=sid,
        workspace=_clean_path_text(workspace),
        tool=tool,
        started_at=now,
        updated_at=now,
        paths=_paths(repo_root, session_dir),
    )
    _write_if_missing(session_dir / "events.jsonl", "")
    _write_text(
        session_dir / "transcript.md",
        "\n".join(
            [
                "# Session Transcript",
                "",
                f"- Session ID: `{sid}`",
                f"- Workspace: `{snapshot.workspace or 'unspecified'}`",
                f"- Tool: `{tool or 'unspecified'}`",
                f"- Started at: `{now}`",
                "",
            ]
        ),
    )
    _write_text(
        session_dir / "commands.md",
        "\n".join(["# Session Commands", "", "Commands are recorded as audit events.", ""]),
    )
    _write_text(
        session_dir / "file_changes.md",
        "\n".join(["# Session File Changes", "", "File changes are recorded as audit events.", ""]),
    )
    _write_text(
        session_dir / "decisions.md",
        "\n".join(["# Session Decisions", "", "User confirmations and accepted decisions.", ""]),
    )
    _append_event(
        repo_root,
        session_dir,
        {
            "type": "session_start",
            "session_id": sid,
            "workspace": snapshot.workspace,
            "tool": tool,
            "created_at": now,
        },
    )
    _write_snapshot(repo_root, session_dir, snapshot)
    return snapshot


def start_named_session(
    repo_root: Path,
    name: str,
    *,
    workspace: str = "",
    tool: str = "",
    session_id: str = "",
) -> SessionSnapshot:
    safe_name = _slug(name)
    if not safe_name:
        raise SystemExit("--name must contain at least one letter or number")
    session_dir = _session_root_for(repo_root) / _timestamped_dir_name(safe_name)
    _write_named_session_pointer(repo_root, safe_name, session_dir)
    return start_session(
        repo_root,
        session_dir,
        workspace=workspace,
        tool=tool,
        session_id=session_id,
    )


def append_turn(
    repo_root: Path,
    session_dir: Path,
    *,
    role: str,
    content: str,
) -> SessionSnapshot:
    snapshot = _load_or_create(repo_root, session_dir)
    safe_content, redacted = _redact(content)
    event = {
        "type": "turn",
        "role": role,
        "content": safe_content,
        "redacted": redacted,
        "created_at": _now(),
    }
    _append_event(repo_root, session_dir, event)
    event_no = _event_count(session_dir)
    _append_text(
        session_dir / "transcript.md",
        "\n".join(
            [
                f"## Turn {event_no:03d}",
                "",
                f"Role: `{role}`",
                "",
                "Content:",
                "",
                "```text",
                safe_content.rstrip(),
                "```",
                "",
            ]
        ),
    )
    if redacted:
        snapshot.warnings.append("turn_content_redacted_for_secret_pattern")
    return _refresh_snapshot(repo_root, session_dir, snapshot)


def append_command(
    repo_root: Path,
    session_dir: Path,
    *,
    command: str,
    status: str,
    summary: str,
    exit_code: str = "",
) -> SessionSnapshot:
    snapshot = _load_or_create(repo_root, session_dir)
    safe_command, command_redacted = _redact(_clean_path_text(command))
    safe_summary, summary_redacted = _redact(summary)
    event = {
        "type": "command",
        "command": safe_command,
        "status": status,
        "exit_code": exit_code,
        "summary": safe_summary,
        "redacted": command_redacted or summary_redacted,
        "created_at": _now(),
    }
    _append_event(repo_root, session_dir, event)
    _append_text(
        session_dir / "commands.md",
        "\n".join(
            [
                f"## Command {_event_count(session_dir):03d}",
                "",
                f"- Status: `{status}`",
                f"- Exit code: `{exit_code or 'not_recorded'}`",
                "",
                "```powershell",
                safe_command,
                "```",
                "",
                safe_summary.rstrip(),
                "",
            ]
        ),
    )
    if command_redacted or summary_redacted:
        snapshot.warnings.append("command_event_redacted_for_secret_pattern")
    return _refresh_snapshot(repo_root, session_dir, snapshot)


def append_file_change(
    repo_root: Path,
    session_dir: Path,
    *,
    path: str,
    action: str,
    summary: str,
) -> SessionSnapshot:
    snapshot = _load_or_create(repo_root, session_dir)
    safe_path = _clean_path_text(path)
    safe_summary, redacted = _redact(summary)
    event = {
        "type": "file_change",
        "path": safe_path,
        "action": action,
        "summary": safe_summary,
        "redacted": redacted,
        "created_at": _now(),
    }
    _append_event(repo_root, session_dir, event)
    _append_text(
        session_dir / "file_changes.md",
        "\n".join(
            [
                f"## File Change {_event_count(session_dir):03d}",
                "",
                f"- Path: `{safe_path}`",
                f"- Action: `{action}`",
                "",
                safe_summary.rstrip(),
                "",
            ]
        ),
    )
    if redacted:
        snapshot.warnings.append("file_change_event_redacted_for_secret_pattern")
    return _refresh_snapshot(repo_root, session_dir, snapshot)


def append_decision(
    repo_root: Path,
    session_dir: Path,
    *,
    decision: str,
    status: str,
) -> SessionSnapshot:
    snapshot = _load_or_create(repo_root, session_dir)
    safe_decision, redacted = _redact(decision)
    event = {
        "type": "decision",
        "decision": safe_decision,
        "status": status,
        "redacted": redacted,
        "created_at": _now(),
    }
    _append_event(repo_root, session_dir, event)
    _append_text(
        session_dir / "decisions.md",
        "\n".join(
            [
                f"## Decision {_event_count(session_dir):03d}",
                "",
                f"- Status: `{status}`",
                "",
                safe_decision.rstrip(),
                "",
            ]
        ),
    )
    if redacted:
        snapshot.warnings.append("decision_event_redacted_for_secret_pattern")
    return _refresh_snapshot(repo_root, session_dir, snapshot)


def finish_session(repo_root: Path, session_dir: Path, *, status: str = "finished") -> SessionSnapshot:
    snapshot = _load_or_create(repo_root, session_dir)
    now = _now()
    _append_event(repo_root, session_dir, {"type": "session_finish", "status": status, "created_at": now})
    snapshot.status = status
    snapshot.finished_at = now
    return _refresh_snapshot(repo_root, session_dir, snapshot)


def verify_session_intent(repo_root: Path, session_dir: Path, *, accepted: bool = False) -> dict[str, Any]:
    snapshot = _load_or_create(repo_root, session_dir)
    events = _read_events(session_dir)
    verification = _build_intent_verification(snapshot, events, accepted=accepted)
    _write_text(
        session_dir / "intent_verification.json",
        json.dumps(verification, indent=2, ensure_ascii=False) + "\n",
    )
    _write_text(
        session_dir / "intent_verification.md",
        _render_intent_verification_markdown(verification),
    )
    _write_snapshot(repo_root, session_dir, snapshot)
    return verification


def _paths(repo_root: Path, session_dir: Path) -> SnapshotPaths:
    return SnapshotPaths(
        root=_rel(session_dir, repo_root),
        events=_rel(session_dir / "events.jsonl", repo_root),
        transcript=_rel(session_dir / "transcript.md", repo_root),
        compact=_rel(session_dir / "compact.md", repo_root),
        intent_verification=_rel(session_dir / "intent_verification.md", repo_root),
        intent_verification_json=_rel(session_dir / "intent_verification.json", repo_root),
        commands=_rel(session_dir / "commands.md", repo_root),
        file_changes=_rel(session_dir / "file_changes.md", repo_root),
        decisions=_rel(session_dir / "decisions.md", repo_root),
        snapshot=_rel(session_dir / "snapshot.json", repo_root),
    )


def _load_or_create(repo_root: Path, session_dir: Path) -> SessionSnapshot:
    path = session_dir / "snapshot.json"
    if not path.exists():
        return start_session(repo_root, session_dir)
    data = json.loads(path.read_text(encoding="utf-8"))
    paths = data.get("paths") or {}
    return SessionSnapshot(
        session_id=str(data.get("session_id") or ""),
        workspace=str(data.get("workspace") or ""),
        tool=str(data.get("tool") or ""),
        status=str(data.get("status") or "active"),
        started_at=str(data.get("started_at") or ""),
        updated_at=str(data.get("updated_at") or ""),
        finished_at=str(data.get("finished_at") or ""),
        event_count=int(data.get("event_count") or 0),
        paths=SnapshotPaths(
            root=str(paths.get("root") or _rel(session_dir, repo_root)),
            events=str(paths.get("events") or _rel(session_dir / "events.jsonl", repo_root)),
            transcript=str(paths.get("transcript") or _rel(session_dir / "transcript.md", repo_root)),
            compact=str(paths.get("compact") or _rel(session_dir / "compact.md", repo_root)),
            intent_verification=str(
                paths.get("intent_verification")
                or _rel(session_dir / "intent_verification.md", repo_root)
            ),
            intent_verification_json=str(
                paths.get("intent_verification_json")
                or _rel(session_dir / "intent_verification.json", repo_root)
            ),
            commands=str(paths.get("commands") or _rel(session_dir / "commands.md", repo_root)),
            file_changes=str(paths.get("file_changes") or _rel(session_dir / "file_changes.md", repo_root)),
            decisions=str(paths.get("decisions") or _rel(session_dir / "decisions.md", repo_root)),
            snapshot=str(paths.get("snapshot") or _rel(path, repo_root)),
        ),
        warnings=list(data.get("warnings") or []),
    )


def _refresh_snapshot(repo_root: Path, session_dir: Path, snapshot: SessionSnapshot) -> SessionSnapshot:
    snapshot.updated_at = _now()
    snapshot.event_count = _event_count(session_dir)
    snapshot.paths = _paths(repo_root, session_dir)
    _write_snapshot(repo_root, session_dir, snapshot)
    return snapshot


def _write_snapshot(repo_root: Path, session_dir: Path, snapshot: SessionSnapshot) -> None:
    snapshot.paths = _paths(repo_root, session_dir)
    snapshot.event_count = _event_count(session_dir)
    _write_text(session_dir / "snapshot.json", json.dumps(snapshot.to_dict(), indent=2) + "\n")
    _write_compact(repo_root, session_dir, snapshot)


def _append_event(repo_root: Path, session_dir: Path, event: dict[str, Any]) -> None:
    session_dir.mkdir(parents=True, exist_ok=True)
    event = {
        "event_id": _event_count(session_dir) + 1,
        **event,
    }
    event["path_root"] = _rel(session_dir, repo_root)
    _append_text(session_dir / "events.jsonl", json.dumps(event, ensure_ascii=False) + "\n")


def _event_count(session_dir: Path) -> int:
    path = session_dir / "events.jsonl"
    if not path.exists():
        return 0
    text = path.read_text(encoding="utf-8")
    return sum(1 for line in text.splitlines() if line.strip())


def _write_compact(repo_root: Path, session_dir: Path, snapshot: SessionSnapshot) -> None:
    events = _read_events(session_dir)
    turns = [event for event in events if event.get("type") == "turn"]
    commands = [event for event in events if event.get("type") == "command"]
    file_changes = [event for event in events if event.get("type") == "file_change"]
    decisions = [event for event in events if event.get("type") == "decision"]
    verification = _load_json_if_exists(session_dir / "intent_verification.json")
    last_user = next((event for event in reversed(turns) if event.get("role") == "user"), {})
    latest_intent = _first_line(str(last_user.get("content") or "")) or "not recorded"
    lines = [
        "# Compact Session Context",
        "",
        f"- Session ID: `{snapshot.session_id}`",
        f"- Workspace: `{snapshot.workspace or 'unspecified'}`",
        f"- Tool: `{snapshot.tool or 'unspecified'}`",
        f"- Status: `{snapshot.status}`",
        f"- Events: `{len(events)}`",
        f"- Updated at: `{snapshot.updated_at or snapshot.started_at}`",
        "",
        "## Current Intent",
        "",
        f"- {latest_intent}",
        "",
        "## Recent Commands",
        "",
    ]
    if commands:
        for event in commands[-5:]:
            command = _first_line(str(event.get("command") or ""))
            status = str(event.get("status") or "unknown")
            summary = _first_line(str(event.get("summary") or ""))
            lines.append(f"- `{status}` `{command}` - {summary}")
    else:
        lines.append("- none recorded")
    lines.extend(["", "## File Changes", ""])
    if file_changes:
        for event in file_changes[-10:]:
            lines.append(
                f"- `{event.get('action') or 'change'}` `{event.get('path') or ''}` - "
                f"{_first_line(str(event.get('summary') or ''))}"
            )
    else:
        lines.append("- none recorded")
    lines.extend(["", "## Decisions", ""])
    if decisions:
        for event in decisions[-10:]:
            lines.append(
                f"- `{event.get('status') or 'recorded'}` "
                f"{_first_line(str(event.get('decision') or ''))}"
            )
    else:
        lines.append("- none recorded")
    lines.extend(
        [
            "",
            "## Verification",
            "",
        ]
    )
    if verification:
        lines.extend(
            [
                f"- Overall status: `{verification.get('overall_status') or 'unknown'}`",
                f"- Report: `{_rel(session_dir / 'intent_verification.md', repo_root)}`",
                "",
            ]
        )
    else:
        lines.extend(["- not run", ""])
    lines.extend(
        [
            "## Exact Audit Files",
            "",
            f"- `{_rel(session_dir / 'transcript.md', repo_root)}`",
            f"- `{_rel(session_dir / 'events.jsonl', repo_root)}`",
            f"- `{_rel(session_dir / 'commands.md', repo_root)}`",
            "",
            "Read this compact file by default. Open the exact audit files only when needed.",
            "",
        ]
    )
    _write_text(session_dir / "compact.md", "\n".join(lines))


def _build_intent_verification(
    snapshot: SessionSnapshot,
    events: list[dict[str, Any]],
    *,
    accepted: bool,
) -> dict[str, Any]:
    user_turns = [event for event in events if event.get("type") == "turn" and event.get("role") == "user"]
    assistant_turns = [
        event for event in events if event.get("type") == "turn" and event.get("role") == "assistant"
    ]
    commands = [event for event in events if event.get("type") == "command"]
    file_changes = [event for event in events if event.get("type") == "file_change"]
    decisions = [event for event in events if event.get("type") == "decision"]
    failed_commands = [
        event for event in commands if str(event.get("status") or "").lower() in {"failed", "error"}
    ]
    constraints = _infer_constraints(user_turns + decisions)
    tasks = _build_task_records(user_turns, assistant_turns, commands, file_changes, decisions)
    guardrail_checks = _guardrail_checks(events, tasks)
    evidence = {
        "user_turn_count": len(user_turns),
        "assistant_turn_count": len(assistant_turns),
        "command_count": len(commands),
        "failed_command_count": len(failed_commands),
        "file_change_count": len(file_changes),
        "decision_count": len(decisions),
        "has_compact_context": True,
        "has_exact_transcript": True,
    }
    overall = _overall_status(tasks, guardrail_checks, accepted=accepted)
    return {
        "artifact_type": "intent_verification",
        "version": 1,
        "generated_by": "session-snapshot verify",
        "generated_at": _now(),
        "session_id": snapshot.session_id,
        "workspace": snapshot.workspace,
        "tool": snapshot.tool,
        "status_taxonomy": [
            "model_pass",
            "accepted_pass",
            "partial",
            "failed",
            "blocked",
            "not_checked",
        ],
        "overall_status": overall,
        "guardrail_checks": guardrail_checks,
        "intent_contract": {
            "session_goal": _session_goal(user_turns),
            "constraints": constraints,
            "success_criteria": _success_criteria(constraints),
            "open_ambiguities": _open_ambiguities(user_turns),
            "tasks": tasks,
        },
        "evidence_checklist": evidence,
        "failure_policy": {
            "missing_evidence": "partial_or_not_checked",
            "deterministic_failure": "failed",
            "hard_guardrail_failure": "failed",
            "semantic_mismatch": "partial",
        },
    }


def _build_task_records(
    user_turns: list[dict[str, Any]],
    assistant_turns: list[dict[str, Any]],
    commands: list[dict[str, Any]],
    file_changes: list[dict[str, Any]],
    decisions: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    tasks = []
    for index, turn in enumerate(user_turns, start=1):
        request = _first_line(str(turn.get("content") or ""), max_chars=240)
        if not request:
            continue
        status = _task_status(request, assistant_turns, commands, file_changes, decisions)
        tasks.append(
            {
                "task_id": f"task_{index:03d}",
                "user_request": request,
                "expected_output": _expected_output(request),
                "guardrails": _infer_constraints([turn]),
                "evidence_required": _evidence_required(request),
                "evidence_found": _evidence_found(request, commands, file_changes, decisions),
                "status": status,
            }
        )
    if not tasks:
        return [
            {
                "task_id": "task_001",
                "user_request": "No user request recorded.",
                "expected_output": "No verification target.",
                "guardrails": [],
                "evidence_required": ["user_turn"],
                "evidence_found": [],
                "status": "not_checked",
            }
        ]
    return tasks


def _task_status(
    request: str,
    assistant_turns: list[dict[str, Any]],
    commands: list[dict[str, Any]],
    file_changes: list[dict[str, Any]],
    decisions: list[dict[str, Any]],
) -> str:
    lower = request.lower()
    failed_commands = [
        event for event in commands if str(event.get("status") or "").lower() in {"failed", "error"}
    ]
    if failed_commands:
        return "failed"
    if any(word in lower for word in ("delete", "remove", "cleanup")):
        has_decision = bool(decisions)
        has_delete_evidence = any(
            str(event.get("action") or "").lower() in {"delete", "remove"}
            or "cleanup" in str(event.get("summary") or "").lower()
            for event in file_changes + commands
        )
        return "model_pass" if has_decision and has_delete_evidence else "partial"
    if any(word in lower for word in ("implement", "add", "write", "create", "change", "fix")):
        return "model_pass" if file_changes or commands else "partial"
    if any(word in lower for word in ("run", "verify", "test", "check", "dry run")):
        return "model_pass" if commands else "partial"
    if any(word in lower for word in ("workspace", "working directory", "set ")):
        return "model_pass" if commands or decisions else "partial"
    if assistant_turns:
        return "partial"
    return "not_checked"


def _overall_status(
    tasks: list[dict[str, Any]],
    guardrail_checks: list[dict[str, Any]],
    *,
    accepted: bool,
) -> str:
    guardrail_statuses = {str(check.get("status") or "") for check in guardrail_checks}
    if "failed" in guardrail_statuses:
        return "failed"
    if "blocked" in guardrail_statuses:
        return "blocked"
    statuses = {str(task.get("status") or "") for task in tasks}
    if "failed" in statuses:
        return "failed"
    if "blocked" in statuses:
        return "blocked"
    if statuses and statuses.issubset({"model_pass"}) and accepted:
        return "accepted_pass"
    if statuses and statuses.issubset({"model_pass"}):
        return "model_pass"
    if statuses and statuses.issubset({"not_checked"}):
        return "not_checked"
    return "partial"


def _guardrail_checks(events: list[dict[str, Any]], tasks: list[dict[str, Any]]) -> list[dict[str, Any]]:
    checks = [
        _local_absolute_path_check(events),
        _secret_redaction_check(events),
        _failed_command_check(events),
        _missing_evidence_check(tasks),
        _delete_confirmation_check(events, tasks),
    ]
    return checks


def _local_absolute_path_check(events: list[dict[str, Any]]) -> dict[str, Any]:
    offenders = []
    for event in events:
        if event.get("type") == "turn" and event.get("role") == "user":
            continue
        text = _event_text(event)
        if LOCAL_ABSOLUTE_PATH_PATTERN.search(text):
            offenders.append(str(event.get("event_id") or "?"))
    return {
        "id": "no_local_absolute_paths",
        "status": "failed" if offenders else "model_pass",
        "evidence": f"offending_event_ids={','.join(offenders)}" if offenders else "no local absolute paths found in non-user events",
        "hard_guardrail": True,
    }


def _secret_redaction_check(events: list[dict[str, Any]]) -> dict[str, Any]:
    redacted = [str(event.get("event_id") or "?") for event in events if event.get("redacted")]
    return {
        "id": "no_secret_like_content",
        "status": "failed" if redacted else "model_pass",
        "evidence": f"redacted_event_ids={','.join(redacted)}" if redacted else "no redacted secret-like content recorded",
        "hard_guardrail": True,
    }


def _failed_command_check(events: list[dict[str, Any]]) -> dict[str, Any]:
    failed = [
        str(event.get("event_id") or "?")
        for event in events
        if event.get("type") == "command" and str(event.get("status") or "").lower() in {"failed", "error"}
    ]
    return {
        "id": "no_failed_commands_unresolved",
        "status": "failed" if failed else "model_pass",
        "evidence": f"failed_command_event_ids={','.join(failed)}" if failed else "no failed command events recorded",
        "hard_guardrail": False,
    }


def _missing_evidence_check(tasks: list[dict[str, Any]]) -> dict[str, Any]:
    missing = [
        str(task.get("task_id") or "?")
        for task in tasks
        if str(task.get("status") or "") in {"partial", "not_checked"}
    ]
    return {
        "id": "evidence_required_for_pass",
        "status": "partial" if missing else "model_pass",
        "evidence": f"tasks_missing_pass_evidence={','.join(missing)}" if missing else "all tasks have pass evidence",
        "hard_guardrail": False,
    }


def _delete_confirmation_check(events: list[dict[str, Any]], tasks: list[dict[str, Any]]) -> dict[str, Any]:
    delete_tasks = [
        task
        for task in tasks
        if any(word in str(task.get("user_request") or "").lower() for word in ("delete", "remove", "cleanup"))
    ]
    if not delete_tasks:
        return {
            "id": "delete_requires_confirmation",
            "status": "model_pass",
            "evidence": "no delete/cleanup task recorded",
            "hard_guardrail": True,
        }
    has_decision = any(event.get("type") == "decision" for event in events)
    has_delete_evidence = any(
        event.get("type") in {"command", "file_change"}
        and (
            str(event.get("action") or "").lower() in {"delete", "remove"}
            or any(word in _event_text(event).lower() for word in ("delete", "remove", "cleanup"))
        )
        for event in events
    )
    status = "model_pass" if has_decision and has_delete_evidence else "failed"
    evidence = (
        "delete/cleanup had decision and execution evidence"
        if status == "model_pass"
        else "delete/cleanup task missing explicit decision or execution evidence"
    )
    return {
        "id": "delete_requires_confirmation",
        "status": status,
        "evidence": evidence,
        "hard_guardrail": True,
    }


def _event_text(event: dict[str, Any]) -> str:
    parts = []
    for value in event.values():
        if isinstance(value, str):
            parts.append(value)
    return " ".join(parts)


def _session_goal(user_turns: list[dict[str, Any]]) -> str:
    if not user_turns:
        return "No user goal recorded."
    return _first_line(str(user_turns[-1].get("content") or ""), max_chars=240)


def _infer_constraints(events: list[dict[str, Any]]) -> list[str]:
    text = " ".join(str(event.get("content") or event.get("decision") or "") for event in events).lower()
    constraints = []
    if "absolute path" in text or "c:/users" in text or "onedrive" in text:
        constraints.append("Use project-root-relative paths; do not expose local absolute paths.")
    if "table" in text and ("result" in text or "output" in text):
        constraints.append("Show query/result outputs in table format.")
    if "hide" in text or "background" in text or "internal files" in text:
        constraints.append("Keep internal/generated files in the background unless requested.")
    if "guardrail" in text:
        constraints.append("Respect global hard guardrails plus workspace-level user-editable guardrails.")
    if "secret" in text or ".env" in text:
        constraints.append("Do not expose secrets, credentials, tokens, or environment dumps.")
    return constraints


def _success_criteria(constraints: list[str]) -> list[str]:
    criteria = ["Relevant user requests have evidence-backed status."]
    criteria.extend(constraints)
    return criteria


def _open_ambiguities(user_turns: list[dict[str, Any]]) -> list[str]:
    text = " ".join(str(event.get("content") or "") for event in user_turns).lower()
    ambiguities = []
    if "intent" in text:
        ambiguities.append("Semantic intent may require reviewer acceptance before accepted_pass.")
    return ambiguities


def _expected_output(request: str) -> str:
    lower = request.lower()
    if any(word in lower for word in ("delete", "remove", "cleanup")):
        return "Confirmed cleanup applied only within the approved boundary."
    if any(word in lower for word in ("implement", "add", "write", "create", "change", "fix")):
        return "Code or documentation change plus verification evidence."
    if any(word in lower for word in ("run", "verify", "test", "check", "dry run")):
        return "Command/test/check result with status and key output."
    return "Assistant response addresses the user request."


def _evidence_required(request: str) -> list[str]:
    lower = request.lower()
    if any(word in lower for word in ("delete", "remove", "cleanup")):
        return ["user_decision", "command_or_file_change", "post_check"]
    if any(word in lower for word in ("implement", "add", "write", "create", "change", "fix")):
        return ["file_change", "test_or_compile_when_applicable"]
    if any(word in lower for word in ("run", "verify", "test", "check", "dry run")):
        return ["command_result"]
    return ["assistant_response"]


def _evidence_found(
    request: str,
    commands: list[dict[str, Any]],
    file_changes: list[dict[str, Any]],
    decisions: list[dict[str, Any]],
) -> list[str]:
    found = []
    if commands:
        found.append("command_result")
    if file_changes:
        found.append("file_change")
    if decisions:
        found.append("user_decision")
    if not found and request:
        found.append("assistant_response_only")
    return found


def _render_intent_verification_markdown(verification: dict[str, Any]) -> str:
    contract = verification["intent_contract"]
    evidence = verification["evidence_checklist"]
    lines = [
        "# Intent Verification",
        "",
        f"- Session ID: `{verification['session_id']}`",
        f"- Workspace: `{verification.get('workspace') or 'unspecified'}`",
        f"- Tool: `{verification.get('tool') or 'unspecified'}`",
        f"- Overall status: `{verification['overall_status']}`",
        "",
        "## Intent Contract",
        "",
        f"- Session goal: {contract['session_goal']}",
        "",
        "### Constraints",
        "",
    ]
    if contract["constraints"]:
        lines.extend(f"- {constraint}" for constraint in contract["constraints"])
    else:
        lines.append("- none inferred")
    lines.extend(
        [
            "",
            "## Evidence Checklist",
            "",
            "| Evidence | Count/Status |",
            "|---|---:|",
        ]
    )
    for key, value in evidence.items():
        lines.append(f"| `{key}` | `{value}` |")
    lines.extend(
        [
            "",
            "## Guardrail Checks",
            "",
            "| Check | Status | Hard Guardrail | Evidence |",
            "|---|---|---:|---|",
        ]
    )
    for check in verification.get("guardrail_checks") or []:
        evidence_text = str(check.get("evidence") or "").replace("|", "\\|")
        lines.append(
            f"| `{check.get('id')}` | `{check.get('status')}` | "
            f"`{bool(check.get('hard_guardrail'))}` | {evidence_text} |"
        )
    lines.extend(
        [
            "",
            "## Task Verification",
            "",
            "| Task | Status | Evidence Found | User Request |",
            "|---|---|---|---|",
        ]
    )
    for task in contract["tasks"]:
        evidence_found = ", ".join(task.get("evidence_found") or [])
        request = str(task.get("user_request") or "").replace("|", "\\|")
        lines.append(
            f"| `{task['task_id']}` | `{task['status']}` | {evidence_found or 'none'} | {request} |"
        )
    lines.extend(
        [
            "",
            "## Open Ambiguities",
            "",
        ]
    )
    if contract["open_ambiguities"]:
        lines.extend(f"- {item}" for item in contract["open_ambiguities"])
    else:
        lines.append("- none inferred")
    lines.extend(
        [
            "",
            "Statuses: `model_pass`, `accepted_pass`, `partial`, `failed`, `blocked`, `not_checked`.",
            "A model pass is not the same as user acceptance.",
            "",
        ]
    )
    return "\n".join(lines)


def _load_json_if_exists(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return {}


def _read_events(session_dir: Path) -> list[dict[str, Any]]:
    path = session_dir / "events.jsonl"
    if not path.exists():
        return []
    events = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        try:
            events.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return events


def _read_content(args: argparse.Namespace) -> str:
    values = []
    content = getattr(args, "content", None)
    if content:
        values.append(content)
    content_file = getattr(args, "content_file", None)
    if content_file:
        values.append(Path(content_file).read_text(encoding="utf-8"))
    if getattr(args, "stdin", False):
        values.append(sys.stdin.read())
    if not values:
        raise SystemExit("content is required: pass --content, --content-file, or --stdin")
    return "\n".join(values)


def _redact(value: str) -> tuple[str, bool]:
    safe = _shared_redact(value)
    return safe, safe != value


def _clean_path_text(value: str) -> str:
    return value.replace("\\", "/")


def _first_line(value: str, *, max_chars: int = 180) -> str:
    line = " ".join(value.strip().splitlines()).strip()
    if len(line) > max_chars:
        return line[: max_chars - 3].rstrip() + "..."
    return line


def _slug(value: str) -> str:
    slug = re.sub(r"[^A-Za-z0-9_.-]+", "-", value.strip().lower()).strip("-._")
    return slug[:80]


def _timestamped_dir_name(name: str) -> str:
    stamp = datetime.now(timezone.utc).strftime("%Y-%m-%d-%H%M%S")
    return f"{stamp}-{name}"


# Named-session storage is resolved RELATIVE TO repo_root (the CLI passes the
# invocation cwd) rather than the import-time PROJECT_ROOT, so a session started
# from another working tree lands there. The module-level SESSION_ROOT/ALIAS_DIR
# constants remain the defaults for the in-process API.
def _session_root_for(repo_root: Path) -> Path:
    return repo_root / ".agents" / "sessions"


def _alias_dir_for(repo_root: Path) -> Path:
    return _session_root_for(repo_root) / "_aliases"


def _write_named_session_pointer(repo_root: Path, name: str, session_dir: Path) -> None:
    rel = _rel(session_dir, repo_root)
    _write_text(_alias_dir_for(repo_root) / f"{name}.txt", rel + "\n")
    _write_text(_session_root_for(repo_root) / "current_session.txt", rel + "\n")


def _resolve_named_session_dir(repo_root: Path, name: str) -> Path:
    safe_name = _slug(name)
    if not safe_name:
        raise SystemExit("--name must contain at least one letter or number")
    pointer = _alias_dir_for(repo_root) / f"{safe_name}.txt"
    if not pointer.exists():
        raise SystemExit(f"session name not found: {safe_name}. Run `session-snapshot start --name {safe_name}` first.")
    value = pointer.read_text(encoding="utf-8").strip()
    if not value:
        raise SystemExit(f"session name has an empty pointer: {safe_name}")
    path = Path(value)
    return path if path.is_absolute() else repo_root / path


def _default_session_id(*, workspace: str, tool: str, now: str) -> str:
    seed = f"{workspace}|{tool}|{now}".encode("utf-8")
    digest = hashlib.sha256(seed).hexdigest()[:10]
    return f"session-{digest}"


def _write_if_missing(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if not path.exists():
        path.write_text(content, encoding="utf-8")


def _write_text(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


def _append_text(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(content)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _rel(path: Path, repo_root: Path) -> str:
    resolved = path.resolve()
    try:
        return resolved.relative_to(repo_root.resolve()).as_posix()
    except ValueError:
        return resolved.as_posix()


def _session_dir_arg(repo_root: Path, args: argparse.Namespace) -> Path:
    if getattr(args, "name", ""):
        return _resolve_named_session_dir(repo_root, args.name)
    value = getattr(args, "session_dir", "")
    if not value:
        return DEFAULT_SESSION_DIR
    path = Path(value).expanduser()
    return path.resolve() if path.is_absolute() else (repo_root / path).resolve()


def _add_session_name(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--name", default="", help="Named session alias under .agents/sessions/.")



@anchored("session-snapshot")
def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description="Record exact agent/user session snapshots.")
    parser.add_argument("--session-dir", default="")
    subparsers = parser.add_subparsers(dest="action", required=True)

    start = subparsers.add_parser("start", help="Start or reset a session snapshot.")
    _add_session_name(start)
    start.add_argument("--workspace", default="")
    start.add_argument("--tool", default="")
    start.add_argument("--session-id", default="")

    append = subparsers.add_parser("append", help="Append a conversation turn.")
    _add_session_name(append)
    append.add_argument("--role", required=True, choices=["user", "assistant", "system", "tool"])
    append.add_argument("--content", default="")
    append.add_argument("--content-file", default="")
    append.add_argument("--stdin", action="store_true")

    command = subparsers.add_parser("command", help="Append a command event.")
    _add_session_name(command)
    command.add_argument("--command", required=True, dest="command_text")
    command.add_argument("--status", required=True)
    command.add_argument("--summary", default="")
    command.add_argument("--exit-code", default="")

    file_change = subparsers.add_parser("file-change", help="Append a file-change event.")
    _add_session_name(file_change)
    file_change.add_argument("--path", required=True)
    file_change.add_argument("--action", required=True, dest="change_action")
    file_change.add_argument("--summary", default="")

    decision = subparsers.add_parser("decision", help="Append a user decision or confirmation.")
    _add_session_name(decision)
    decision.add_argument("--decision", required=True)
    decision.add_argument("--status", default="accepted")

    finish = subparsers.add_parser("finish", help="Finish the session snapshot.")
    _add_session_name(finish)
    finish.add_argument("--status", default="finished")

    verify = subparsers.add_parser("verify", help="Verify recorded work against inferred user intent.")
    _add_session_name(verify)
    verify.add_argument(
        "--accepted",
        action="store_true",
        help="Promote all-model-pass verification to accepted_pass after user/reviewer approval.",
    )

    args = parser.parse_args(argv)
    # Resolve session storage relative to the invocation cwd so the CLI records
    # sessions in the working tree it is run from (named-session paths derive
    # from this). The in-process API still defaults to PROJECT_ROOT.
    repo_root = Path.cwd()

    if args.action == "start":
        if args.name and not args.session_dir:
            result = start_named_session(
                repo_root,
                args.name,
                workspace=args.workspace,
                tool=args.tool,
                session_id=args.session_id,
            )
        else:
            result = start_session(
                repo_root,
                _session_dir_arg(repo_root, args),
                workspace=args.workspace,
                tool=args.tool,
                session_id=args.session_id,
            )
    elif args.action == "append":
        session_dir = _session_dir_arg(repo_root, args)
        result = append_turn(repo_root, session_dir, role=args.role, content=_read_content(args))
    elif args.action == "command":
        session_dir = _session_dir_arg(repo_root, args)
        result = append_command(
            repo_root,
            session_dir,
            command=args.command_text,
            status=args.status,
            summary=args.summary,
            exit_code=args.exit_code,
        )
    elif args.action == "file-change":
        session_dir = _session_dir_arg(repo_root, args)
        result = append_file_change(
            repo_root,
            session_dir,
            path=args.path,
            action=args.change_action,
            summary=args.summary,
        )
    elif args.action == "decision":
        session_dir = _session_dir_arg(repo_root, args)
        result = append_decision(repo_root, session_dir, decision=args.decision, status=args.status)
    elif args.action == "finish":
        session_dir = _session_dir_arg(repo_root, args)
        result = finish_session(repo_root, session_dir, status=args.status)
    elif args.action == "verify":
        session_dir = _session_dir_arg(repo_root, args)
        result = verify_session_intent(repo_root, session_dir, accepted=args.accepted)
    else:  # pragma: no cover - argparse enforces choices
        raise SystemExit(f"unknown command: {args.action}")

    payload = result.to_dict() if hasattr(result, "to_dict") else result
    print(json.dumps(payload, indent=2))


if __name__ == "__main__":
    main()
