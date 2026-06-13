"""Atomic JSON writes + fail-loud reads + a thread-safe cwd guard.

These are the durability/concurrency primitives the P4 remediation depends on:

- ``write_json_atomic`` / ``write_text_atomic`` — temp-file + ``os.replace`` so a
  crash mid-write can never leave a half-written (corrupt) JSON store. The bare
  ``write_text`` pattern is exactly what produced the corrupt files that the old
  silent-reset paths then erased.
- ``read_json_or_quarantine`` — on a decode error, rename the bad file to
  ``*.corrupt-<ts>`` and RAISE, instead of silently returning ``{}``/``[]`` and
  letting the next write overwrite (and permanently destroy) human decisions.
- ``pushd`` — a process-global, thread-safe ``os.chdir`` guard. ``os.chdir`` is
  process-wide, so concurrent threads (parallel KPI fan-out, Dash callbacks)
  racing it corrupt each other's relative-path resolution. A single module-level
  lock serializes the critical section. (cwd is per-process, so cross-process is
  unaffected.) Ban bare ``os.chdir`` in library code; use ``pushd``.

Ref: core-audit storage.md, ob-memory.md (themes T1 + T6).
"""
from __future__ import annotations

import json
import os
import threading
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator


class CorruptStoreError(RuntimeError):
    """A JSON store could not be parsed; the bad file was quarantined."""


def write_text_atomic(path: Path, text: str, *, encoding: str = "utf-8") -> None:
    """Write ``text`` to ``path`` atomically (temp file in the same dir + replace)."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f"{path.name}.{os.getpid()}.{threading.get_ident()}.tmp")
    try:
        tmp.write_text(text, encoding=encoding)
        os.replace(tmp, path)  # atomic on the same filesystem
    finally:
        try:
            tmp.unlink()
        except OSError:
            pass


def write_json_atomic(path: Path, payload: Any, *, indent: int = 2) -> None:
    """Serialize ``payload`` to JSON and write it atomically."""
    write_text_atomic(Path(path), json.dumps(payload, indent=indent) + "\n")


def _quarantine(path: Path) -> Path:
    ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
    corrupt = path.with_name(f"{path.name}.corrupt-{ts}")
    try:
        os.replace(path, corrupt)
    except OSError:
        return path
    return corrupt


def read_json_or_quarantine(path: Path, *, default: Any = None) -> Any:
    """Read+parse JSON at ``path``.

    - Missing file -> ``default`` (a fresh store is normal).
    - Corrupt file -> rename to ``*.corrupt-<ts>`` and raise ``CorruptStoreError``
      (never return empty, which would let the caller overwrite and destroy data).
    """
    path = Path(path)
    if not path.exists():
        return default
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        corrupt = _quarantine(path)
        raise CorruptStoreError(
            f"corrupt JSON store {path} quarantined to {corrupt.name}; refusing to "
            f"overwrite with an empty store ({exc})"
        ) from exc


_CHDIR_LOCK = threading.RLock()


@contextmanager
def pushd(path: Path | str) -> Iterator[Path]:
    """Thread-safe ``os.chdir(path)`` for the duration of the block.

    Serializes the process-global cwd change behind a re-entrant lock so
    concurrent threads cannot interleave chdir/restore. Restores the previous
    cwd on exit. Prefer passing absolute paths over this where feasible.
    """
    target = Path(path)
    with _CHDIR_LOCK:
        previous = os.getcwd()
        os.chdir(target)
        try:
            yield target
        finally:
            os.chdir(previous)


__all__ = [
    "CorruptStoreError",
    "pushd",
    "read_json_or_quarantine",
    "write_json_atomic",
    "write_text_atomic",
]
