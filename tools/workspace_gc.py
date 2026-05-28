"""Workspace garbage collection.

Generic GC for `interns/state/` artifacts whose lifecycle is `cache` or
`log`. Source-of-truth artifacts (contracts, applied_ops, wiki) are NEVER
touched. Designed to be safe-by-default: pass `apply=False` for a dry run,
which is the CLI default.

Targets:

- `state/workflow_sessions/<wf_*>/` directories whose session.json is
  `complete` OR older than `max_session_age_hours`.
- `state/panel_previews/*.json` older than `max_preview_age_hours`.
- `state/handoffs/*.md` older than `max_handoff_age_hours`.
- `state/events.jsonl` rotated/truncated when over `max_log_mb`.
- `state/trajectory.jsonl` rotated/truncated when over `max_log_mb`.
- `state/delta_metadata/` removed entirely when `workspace_settings.delta_enabled` is false.

Workspace-agnostic: paths via WorkspaceLayout, no hardcoded names.
"""
from __future__ import annotations

import json
import shutil
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from core.storage.workspace_layout import WorkspaceLayout


DEFAULT_MAX_SESSION_AGE_HOURS = 24
DEFAULT_MAX_PREVIEW_AGE_HOURS = 72
DEFAULT_MAX_HANDOFF_AGE_HOURS = 24 * 14
DEFAULT_MAX_LOG_MB = 5


@dataclass
class GcAction:
    kind: str
    path: str
    reason: str
    bytes_freed: int = 0


@dataclass
class GcReport:
    workspace: str
    apply: bool
    actions: list[GcAction] = field(default_factory=list)

    @property
    def bytes_freed(self) -> int:
        return sum(action.bytes_freed for action in self.actions)

    def summary(self) -> dict[str, Any]:
        return {
            "workspace": self.workspace,
            "apply": self.apply,
            "action_count": len(self.actions),
            "bytes_freed": self.bytes_freed,
            "actions": [
                {
                    "kind": action.kind,
                    "path": action.path,
                    "reason": action.reason,
                    "bytes_freed": action.bytes_freed,
                }
                for action in self.actions
            ],
        }


def _dir_size(path: Path) -> int:
    if not path.exists():
        return 0
    total = 0
    for child in path.rglob("*"):
        try:
            if child.is_file():
                total += child.stat().st_size
        except OSError:
            continue
    return total


def _age_hours(path: Path) -> float:
    try:
        mtime = path.stat().st_mtime
    except OSError:
        return 0.0
    return (time.time() - mtime) / 3600


def _is_complete_session(session_dir: Path) -> bool:
    state_file = session_dir / "session.json"
    if not state_file.exists():
        return False
    try:
        data = json.loads(state_file.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return False
    status = str(data.get("status") or (data.get("current_panel") or {}).get("status") or "")
    return status == "complete"


def collect_sessions(
    layout: WorkspaceLayout,
    *,
    max_age_hours: int = DEFAULT_MAX_SESSION_AGE_HOURS,
) -> list[tuple[Path, str, int]]:
    """Return (dir, reason, bytes) for workflow_sessions targeted by GC."""
    sessions_dir = layout.workflow_sessions_dir
    if not sessions_dir.exists():
        return []
    out: list[tuple[Path, str, int]] = []
    for entry in sorted(sessions_dir.iterdir()):
        if not entry.is_dir():
            continue
        if _is_complete_session(entry):
            out.append((entry, "session status=complete", _dir_size(entry)))
            continue
        if _age_hours(entry) > max_age_hours:
            out.append(
                (
                    entry,
                    f"session inactive > {max_age_hours}h (last updated {_age_hours(entry):.1f}h ago)",
                    _dir_size(entry),
                )
            )
    return out


def collect_panel_previews(
    layout: WorkspaceLayout,
    *,
    max_age_hours: int = DEFAULT_MAX_PREVIEW_AGE_HOURS,
) -> list[tuple[Path, str, int]]:
    previews_dir = layout.state_dir / "panel_previews"
    if not previews_dir.exists():
        return []
    out: list[tuple[Path, str, int]] = []
    for entry in sorted(previews_dir.iterdir()):
        if not entry.is_file():
            continue
        if _age_hours(entry) > max_age_hours:
            try:
                size = entry.stat().st_size
            except OSError:
                size = 0
            out.append((entry, f"preview cache > {max_age_hours}h", size))
    return out


def collect_handoffs(
    layout: WorkspaceLayout,
    *,
    max_age_hours: int = DEFAULT_MAX_HANDOFF_AGE_HOURS,
) -> list[tuple[Path, str, int]]:
    handoffs_dir = layout.handoffs_dir
    if not handoffs_dir.exists():
        return []
    out: list[tuple[Path, str, int]] = []
    for entry in sorted(handoffs_dir.iterdir()):
        if not entry.is_file():
            continue
        if _age_hours(entry) > max_age_hours:
            try:
                size = entry.stat().st_size
            except OSError:
                size = 0
            out.append((entry, f"handoff > {max_age_hours}h", size))
    return out


def collect_log_rotations(
    layout: WorkspaceLayout,
    *,
    max_mb: int = DEFAULT_MAX_LOG_MB,
) -> list[tuple[Path, str, int]]:
    out: list[tuple[Path, str, int]] = []
    for name in ("trajectory.jsonl", "events.jsonl"):
        path = layout.state_dir / name
        if not path.exists():
            continue
        try:
            size = path.stat().st_size
        except OSError:
            continue
        if size > max_mb * 1024 * 1024:
            out.append(
                (
                    path,
                    f"log over {max_mb} MB ({size / (1024 * 1024):.2f} MB) — rotate to {name}.1",
                    size // 2,
                )
            )
    return out


def collect_delta_metadata(layout: WorkspaceLayout) -> list[tuple[Path, str, int]]:
    settings: dict[str, Any] = {}
    try:
        settings = layout.load_settings() or {}
    except Exception:
        settings = {}
    delta_enabled = bool(settings.get("delta_enabled", False))
    delta_dir = layout.state_dir / "delta_metadata"
    if not delta_dir.exists() or delta_enabled:
        return []
    return [
        (
            delta_dir,
            "delta_metadata present but workspace_settings.delta_enabled is false — safe to remove",
            _dir_size(delta_dir),
        )
    ]


def garbage_collect(
    layout: WorkspaceLayout,
    *,
    apply: bool = False,
    max_session_age_hours: int = DEFAULT_MAX_SESSION_AGE_HOURS,
    max_preview_age_hours: int = DEFAULT_MAX_PREVIEW_AGE_HOURS,
    max_handoff_age_hours: int = DEFAULT_MAX_HANDOFF_AGE_HOURS,
    max_log_mb: int = DEFAULT_MAX_LOG_MB,
) -> GcReport:
    """Plan (and optionally execute) GC for this workspace's cache+log artifacts."""
    report = GcReport(workspace=layout.project_root.name, apply=apply)

    for path, reason, size in collect_sessions(layout, max_age_hours=max_session_age_hours):
        report.actions.append(GcAction("delete_session_dir", str(path), reason, size))
        if apply:
            shutil.rmtree(path, ignore_errors=True)

    for path, reason, size in collect_panel_previews(layout, max_age_hours=max_preview_age_hours):
        report.actions.append(GcAction("delete_panel_preview", str(path), reason, size))
        if apply:
            try:
                path.unlink()
            except OSError:
                pass

    for path, reason, size in collect_handoffs(layout, max_age_hours=max_handoff_age_hours):
        report.actions.append(GcAction("delete_handoff", str(path), reason, size))
        if apply:
            try:
                path.unlink()
            except OSError:
                pass

    for path, reason, size in collect_log_rotations(layout, max_mb=max_log_mb):
        report.actions.append(GcAction("rotate_log", str(path), reason, size))
        if apply:
            rotated = path.with_suffix(path.suffix + ".1")
            try:
                if rotated.exists():
                    rotated.unlink()
                path.rename(rotated)
                path.touch()
            except OSError:
                pass

    for path, reason, size in collect_delta_metadata(layout):
        report.actions.append(GcAction("delete_delta_metadata", str(path), reason, size))
        if apply:
            shutil.rmtree(path, ignore_errors=True)

    return report


__all__ = [
    "GcAction",
    "GcReport",
    "DEFAULT_MAX_HANDOFF_AGE_HOURS",
    "DEFAULT_MAX_LOG_MB",
    "DEFAULT_MAX_PREVIEW_AGE_HOURS",
    "DEFAULT_MAX_SESSION_AGE_HOURS",
    "collect_delta_metadata",
    "collect_handoffs",
    "collect_log_rotations",
    "collect_panel_previews",
    "collect_sessions",
    "garbage_collect",
]
