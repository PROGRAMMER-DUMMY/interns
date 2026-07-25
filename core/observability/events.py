"""Structured JSONL event emitter for CLI command observability.

Best-effort, stdlib-only event logging. This module intentionally avoids
imports from the ``core.`` namespace so it can be wired into any module
without creating dependency cycles.
"""
from __future__ import annotations

import json
import logging
import os
import socket
import time
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator

_LOGGER = logging.getLogger(__name__)

_EVENTS_RELATIVE_PATH = ("interns", "state", "events.jsonl")


def _events_path(workspace_path: Path) -> Path:
    return Path(workspace_path).joinpath(*_EVENTS_RELATIVE_PATH)


def _safe_details(details: dict | None) -> Any:
    if details is None:
        return None
    try:
        # Strict probe first: if ``details`` is not natively JSON-serializable
        # we fall back to a repr rather than silently coercing via ``str``.
        json.dumps(details)
        return details
    except Exception:
        # Second chance: ``default=str`` covers most stringifiable objects but
        # the spec mandates a ``_repr`` fallback when that still raises. We
        # always emit ``_repr`` here so non-trivial objects (e.g. ``object()``)
        # surface a stable shape instead of an opaque coerced string.
        try:
            return {"_repr": str(details)}
        except Exception:
            return {"_repr": "<unrepresentable>"}


def emit_event(
    workspace_path: Path,
    *,
    event_type: str,
    command: str,
    status: str = "ok",
    duration_ms: int | None = None,
    summary: str = "",
    details: dict | None = None,
) -> None:
    """Append a single JSONL event line. Never raises."""
    try:
        # Local import (not module-level): this module deliberately avoids
        # core.* imports at parse time so it stays importable from anywhere
        # without creating a cycle (see module docstring); workspace_lock.py
        # itself has no core.* deps, but the invariant is worth preserving
        # here regardless. Fires on every command via time_command(), same
        # as cost_ledger.append() -- unlocked, this loses entries under
        # concurrent commands exactly like that bug did.
        from core.storage.workspace_lock import workspace_lock

        path = _events_path(workspace_path)
        path.parent.mkdir(parents=True, exist_ok=True)
        safe_details = _safe_details(details)
        record = {
            "ts": datetime.now(timezone.utc).isoformat(),
            "event_type": event_type,
            "command": command,
            "status": status,
            "duration_ms": duration_ms,
            "summary": summary,
            "details": safe_details,
            "pid": os.getpid(),
            "hostname": socket.gethostname(),
        }
        try:
            line = json.dumps(record, default=str)
        except Exception:
            # Final defensive fallback: replace details with a repr string.
            record["details"] = {"_repr": str(details)}
            line = json.dumps(record, default=str)
        with workspace_lock(Path(workspace_path)):
            with path.open("a", encoding="utf-8") as handle:
                handle.write(line + "\n")
    except Exception as exc:  # pragma: no cover - best-effort path
        _LOGGER.warning("emit_event failed: %s", exc)


@contextmanager
def time_command(
    workspace_path: Path,
    command: str,
    *,
    event_type: str = "command",
    summary: str = "",
) -> Iterator[dict]:
    """Context manager that times a block and emits an event on exit.

    Yields a mutable dict the caller may mutate to attach ``details``.
    Re-raises any exception raised inside the block after emitting.
    """
    details: dict = {}
    start = time.perf_counter()
    status = "ok"
    error: BaseException | None = None
    try:
        yield details
    except BaseException as exc:
        error = exc
        status = "error"
        details["error_type"] = type(exc).__name__
        details["error"] = str(exc)
        raise
    finally:
        duration_ms = int((time.perf_counter() - start) * 1000)
        emit_event(
            workspace_path,
            event_type=event_type,
            command=command,
            status=status,
            duration_ms=duration_ms,
            summary=summary,
            details=details if details else None,
        )
        # ``raise`` in the except clause handles re-raising; nothing more to do.
        del error
