"""Idempotency stamp helper for apply-* commands.

Provides a deterministic op_id hash, a frozen AppliedOp record, and a
JSONL-backed log under ``<workspace>/interns/state/applied_ops.jsonl``
so repeated invocations of an apply-* command can be detected and skipped.

Dependency-free by design: stdlib only, no imports from ``core.*``.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


_STATE_DIRNAME = "state"
_INTERNS_DIRNAME = "interns"
_LOG_FILENAME = "applied_ops.jsonl"


def _log_path(workspace_path: Path) -> Path:
    return Path(workspace_path) / _INTERNS_DIRNAME / _STATE_DIRNAME / _LOG_FILENAME


def compute_op_id(*args: Any, **kwargs: Any) -> str:
    """Return a deterministic 16-char hex hash for the given arguments.

    The hash is stable across calls with semantically equal inputs.
    """

    payload = json.dumps(
        [list(args), sorted(kwargs.items())],
        default=str,
        sort_keys=True,
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:16]


def fingerprint_paths(*paths: Path | str) -> str:
    """Return a short content-fingerprint over the given file paths.

    Use this to add file-state into `compute_op_id` so a re-apply against a
    rebuilt artifact is NOT treated as a duplicate of a prior apply against a
    different artifact content. Missing files contribute an empty hash slot;
    order-independent across the inputs.
    """

    parts: list[str] = []
    for raw in paths:
        path = Path(raw)
        if not path.exists() or not path.is_file():
            parts.append(f"{path.as_posix()}::missing")
            continue
        try:
            digest = hashlib.sha256(path.read_bytes()).hexdigest()[:16]
        except OSError:
            digest = "unreadable"
        parts.append(f"{path.as_posix()}::{digest}")
    parts.sort()
    return hashlib.sha256("\n".join(parts).encode("utf-8")).hexdigest()[:16]


@dataclass(frozen=True)
class AppliedOp:
    """A single applied-op log entry."""

    op_id: str
    command: str
    applied_at: str
    payload: dict


def _iter_records(workspace_path: Path) -> list[dict]:
    path = _log_path(workspace_path)
    if not path.exists():
        return []
    records: list[dict] = []
    with path.open("r", encoding="utf-8") as handle:
        for line in handle:
            line = line.strip()
            if not line:
                continue
            try:
                records.append(json.loads(line))
            except json.JSONDecodeError:
                # Skip corrupt lines rather than fail the helper.
                continue
    return records


def is_duplicate_op(workspace_path: Path, op_id: str) -> bool:
    """Return True if ``op_id`` already appears in the applied-ops log."""

    for record in _iter_records(workspace_path):
        if record.get("op_id") == op_id:
            return True
    return False


def record_op(
    workspace_path: Path,
    *,
    op_id: str,
    command: str,
    payload: dict | None = None,
) -> bool:
    """Append a new applied-op record. Returns False if op_id is a duplicate."""

    if is_duplicate_op(workspace_path, op_id):
        return False

    path = _log_path(workspace_path)
    path.parent.mkdir(parents=True, exist_ok=True)

    record = AppliedOp(
        op_id=op_id,
        command=command,
        applied_at=datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ"),
        payload=dict(payload) if payload else {},
    )

    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(asdict(record), sort_keys=True))
        handle.write("\n")
    return True


def get_applied_op(workspace_path: Path, op_id: str) -> AppliedOp | None:
    """Return the most recent AppliedOp for ``op_id`` or None."""

    match: dict | None = None
    for record in _iter_records(workspace_path):
        if record.get("op_id") == op_id:
            match = record  # keep iterating so the last (most recent) wins
    if match is None:
        return None
    return AppliedOp(
        op_id=str(match.get("op_id", "")),
        command=str(match.get("command", "")),
        applied_at=str(match.get("applied_at", "")),
        payload=dict(match.get("payload") or {}),
    )


def list_applied_ops(
    workspace_path: Path,
    *,
    command: str | None = None,
    limit: int | None = None,
) -> list[AppliedOp]:
    """Return applied ops newest-first, optionally filtered by command."""

    records = _iter_records(workspace_path)
    if command is not None:
        records = [r for r in records if r.get("command") == command]
    # File is append-only chronologically; newest-first means reverse.
    records.reverse()
    if limit is not None:
        records = records[:limit]
    return [
        AppliedOp(
            op_id=str(r.get("op_id", "")),
            command=str(r.get("command", "")),
            applied_at=str(r.get("applied_at", "")),
            payload=dict(r.get("payload") or {}),
        )
        for r in records
    ]


__all__ = [
    "AppliedOp",
    "compute_op_id",
    "get_applied_op",
    "is_duplicate_op",
    "list_applied_ops",
    "record_op",
]
