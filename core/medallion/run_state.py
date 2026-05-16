"""
core/medallion/run_state.py — Per-run state file and lockfile management.
"""
from __future__ import annotations

import hashlib
import json
import os
import time
from contextlib import contextmanager
from dataclasses import dataclass, field, asdict
from pathlib import Path
from typing import Any, Optional


class WorkspaceBusy(RuntimeError):
    pass


@contextmanager
def acquire_lock(state_dir: Path, *, stale_after_seconds: int = 3600):
    """Acquire the medallion build lockfile atomically (open with 'x' flag)."""
    state_dir.mkdir(parents=True, exist_ok=True)
    lock = state_dir / ".lock"
    payload = json.dumps({"pid": os.getpid(), "ts": time.time()}, indent=2)

    try:
        with open(lock, "x", encoding="utf-8") as fh:
            fh.write(payload)
    except FileExistsError:
        # Lock file exists — check if stale
        try:
            age = time.time() - lock.stat().st_mtime
        except OSError:
            age = 0
        if age < stale_after_seconds:
            try:
                holder = json.loads(lock.read_text(encoding="utf-8"))
            except Exception:
                holder = {}
            raise WorkspaceBusy(
                f"Lock held by run_id=? pid={holder.get('pid', '?')} for {age:.0f}s"
            )
        # Stale lock — remove and re-acquire
        try:
            lock.unlink()
        except OSError:
            pass
        with open(lock, "x", encoding="utf-8") as fh:
            fh.write(payload)

    try:
        yield lock
    finally:
        try:
            lock.unlink()
        except FileNotFoundError:
            pass


def new_run_id(manifest_hash: str) -> str:
    suffix = hashlib.sha1(manifest_hash.encode()).hexdigest()[:8]
    return f"{time.strftime('%Y%m%dT%H%M%SZ', time.gmtime())}-{suffix}"


@dataclass
class TableRunStatus:
    status: str = "pending"          # pending | ok | failed
    row_count_before: int = 0
    row_count_after: int = 0
    elapsed_s: float = 0.0
    assertions: dict[str, str] = field(default_factory=dict)
    error: Optional[str] = None

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        if d["error"] is None:
            del d["error"]
        if not d["assertions"]:
            del d["assertions"]
        return d


@dataclass
class RunState:
    run_id: str
    manifest_hash: str
    target_declared: str
    target_actual: str
    started_at: str
    finished_at: str = ""
    elapsed_seconds: float = 0.0
    degraded_run: bool = False
    per_table_status: dict[str, TableRunStatus] = field(default_factory=dict)
    kpi_diff: dict[str, Any] = field(default_factory=dict)
    retry_history: list[dict] = field(default_factory=list)
    blocker_entries_added: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "run_id": self.run_id,
            "manifest_hash": self.manifest_hash,
            "target_declared": self.target_declared,
            "target_actual": self.target_actual,
            "started_at": self.started_at,
            "finished_at": self.finished_at,
            "elapsed_seconds": round(self.elapsed_seconds, 3),
            "degraded_run": self.degraded_run,
            "per_table_status": {k: v.to_dict() for k, v in self.per_table_status.items()},
            "kpi_diff": self.kpi_diff,
            "retry_history": self.retry_history,
            "blocker_entries_added": self.blocker_entries_added,
        }

    def write(self, run_dir: Path) -> Path:
        run_dir.mkdir(parents=True, exist_ok=True)
        path = run_dir / "run.json"
        path.write_text(json.dumps(self.to_dict(), indent=2), encoding="utf-8")
        return path
