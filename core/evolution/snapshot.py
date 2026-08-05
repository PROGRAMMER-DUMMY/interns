"""Keep the last N ``discovery.json`` payloads so drift has something to diff.

One rule: a snapshot is only written when the discovery payload actually
CHANGED. Re-running discovery on an unchanged source must not push the previous
schema out of the history window -- otherwise the rotation itself would erase
the evidence drift detection depends on.
"""
from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from core.storage.workspace_layout import WorkspaceLayout

HISTORY_DIRNAME = "discovery_history"
MAX_SNAPSHOTS = 20

REASON_NO_DISCOVERY = "no_discovery"
REASON_IDENTICAL = "identical_to_latest"
REASON_CREATED = "created"


@dataclass(frozen=True)
class SnapshotResult:
    snapshot_path: str
    created: bool
    reason: str
    snapshot_count: int
    pruned: list[str] = field(default_factory=list)

    def summary(self) -> dict[str, Any]:
        return asdict(self)


def discovery_path(layout: WorkspaceLayout) -> Path:
    return layout.generated_dir / "intake" / "discovery.json"


def history_dir(layout: WorkspaceLayout) -> Path:
    return layout.generated_dir / "intake" / HISTORY_DIRNAME


def list_snapshots(layout: WorkspaceLayout) -> list[Path]:
    """Snapshot files oldest-first. The UTC stamp in the name sorts lexically."""
    directory = history_dir(layout)
    if not directory.is_dir():
        return []
    return sorted(directory.glob("*.json"))


def load_snapshot(path: str | Path) -> dict[str, Any]:
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return {}
    return data if isinstance(data, dict) else {}


def snapshot_discovery(
    layout: WorkspaceLayout,
    *,
    repo_root: str | Path | None = None,
    now: datetime | None = None,
    keep: int = MAX_SNAPSHOTS,
) -> SnapshotResult:
    """Copy the current ``discovery.json`` into the history dir, then rotate."""
    source = discovery_path(layout)
    if not source.exists():
        return SnapshotResult("", False, REASON_NO_DISCOVERY, len(list_snapshots(layout)))

    raw = source.read_bytes()
    existing = list_snapshots(layout)
    if existing and existing[-1].read_bytes() == raw:
        return SnapshotResult(
            _rel(existing[-1], repo_root), False, REASON_IDENTICAL, len(existing)
        )

    directory = history_dir(layout)
    directory.mkdir(parents=True, exist_ok=True)
    stamp = (now or datetime.now(timezone.utc)).strftime("%Y%m%dT%H%M%S.%fZ")
    target = directory / f"{stamp}.json"
    suffix = 0
    while target.exists():  # same-microsecond re-run must not overwrite history
        suffix += 1
        target = directory / f"{stamp}-{suffix}.json"
    target.write_bytes(raw)

    snapshots = list_snapshots(layout)
    pruned: list[str] = []
    for stale in snapshots[: max(0, len(snapshots) - max(1, keep))]:
        pruned.append(stale.name)
        stale.unlink()

    return SnapshotResult(
        _rel(target, repo_root), True, REASON_CREATED, len(list_snapshots(layout)), pruned
    )


def _rel(path: Path, root: str | Path | None) -> str:
    if root is None:
        return path.as_posix()
    try:
        return path.resolve().relative_to(Path(root).resolve()).as_posix()
    except ValueError:
        return path.as_posix()


__all__ = [
    "HISTORY_DIRNAME",
    "MAX_SNAPSHOTS",
    "REASON_CREATED",
    "REASON_IDENTICAL",
    "REASON_NO_DISCOVERY",
    "SnapshotResult",
    "discovery_path",
    "history_dir",
    "list_snapshots",
    "load_snapshot",
    "snapshot_discovery",
]
