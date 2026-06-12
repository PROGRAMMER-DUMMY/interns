"""Cross-platform exclusive file lock for per-workspace state mutations.

Many CLIs in this repo write to ``workspaces/<project>/interns/state/`` and
``interns/generated/contracts/`` concurrently. This module provides a
process-level mutex that any such writer can wrap around its critical
section to serialise mutations on a single workspace.

The implementation is intentionally dependency-free (stdlib only) so it can
be imported from anywhere in ``core`` without creating import cycles.
"""

from __future__ import annotations

import json
import os
import socket
import sys
import threading
import time
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterator

_LOCK_RELATIVE_PATH = ("interns", "state", "workspace.lock")

# Re-entrancy registry: lock-file key -> {"owner": (pid, thread_ident),
# "depth": int}. Lets the *same* pid+thread nest ``with workspace_lock(...)``
# (e.g. a CLI wrapper that holds the lock and then calls a helper which also
# locks) without self-deadlocking, while any other thread or process still
# contends on the OS-level lock as before. The pid in the owner tuple guards
# against a forked child inheriting the registry and wrongly treating itself
# as the holder.
_REENTRANT_GUARD = threading.Lock()
_ACTIVE_LOCKS: dict[str, dict] = {}


def _lock_key(lock_path: Path) -> str:
    """Normalised registry key for a lock file (case-folded, resolved)."""

    try:
        resolved = lock_path.resolve()
    except OSError:
        resolved = lock_path
    return os.path.normcase(str(resolved))


class WorkspaceLockTimeout(RuntimeError):
    """Raised when the workspace lock cannot be acquired within the timeout."""


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def _read_existing_holder_pid(lock_path: Path) -> str:
    """Best-effort read of the pid recorded in an existing lock file."""

    try:
        raw = lock_path.read_text(encoding="utf-8").strip()
        if not raw:
            return "unknown"
        payload = json.loads(raw)
        pid = payload.get("pid")
        return str(pid) if pid is not None else "unknown"
    except (OSError, ValueError):
        return "unknown"


def _pid_alive(pid_value: str) -> bool:
    """Return True if a process with ``pid_value`` is still running.

    Used so the lock can be reclaimed when the recorded holder is dead — the
    common case after a crashed / SIGKILL'd run leaves stale metadata behind.
    Conservative: anything we can't determine returns True (don't steal a lock
    we're unsure about).
    """

    if not pid_value or pid_value == "unknown":
        return False
    try:
        pid = int(pid_value)
    except (TypeError, ValueError):
        return True
    if pid <= 0:
        return False
    if sys.platform == "win32":  # pragma: no cover - platform branch
        try:
            import ctypes
            import ctypes.wintypes as wt

            PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
            STILL_ACTIVE = 259
            kernel32 = ctypes.windll.kernel32
            handle = kernel32.OpenProcess(
                PROCESS_QUERY_LIMITED_INFORMATION, False, pid
            )
            if not handle:
                # OpenProcess returns NULL for non-existent or already-exited
                # processes (typical errors: ERROR_INVALID_PARAMETER=87).
                return False
            try:
                exit_code = wt.DWORD()
                ok = kernel32.GetExitCodeProcess(handle, ctypes.byref(exit_code))
                if not ok:
                    return True  # conservative: don't steal
                return exit_code.value == STILL_ACTIVE
            finally:
                kernel32.CloseHandle(handle)
        except Exception:
            return True
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        # Process exists but we don't have permission to signal it; it's alive.
        return True
    except OSError:
        return True


def _write_metadata(fd: int, lock_path: Path) -> None:
    """Truncate the open lock file and write a single JSON metadata line."""

    payload = {
        "pid": os.getpid(),
        "acquired_at": _utc_now_iso(),
        "hostname": socket.gethostname(),
    }
    encoded = (json.dumps(payload) + "\n").encode("utf-8")
    os.lseek(fd, 0, os.SEEK_SET)
    os.ftruncate(fd, 0)
    os.write(fd, encoded)
    try:
        os.fsync(fd)
    except OSError:
        # fsync may not be supported on every filesystem; metadata is advisory.
        pass


# Windows ``msvcrt.locking`` is *mandatory*: every byte in the locked range
# is unreadable to any other handle. We therefore lock a single sentinel byte
# at a high offset and keep the leading bytes — where the JSON metadata
# lives — readable for other processes (including the timeout-error
# diagnostics and any external "who holds this?" tooling).
_WIN_LOCK_OFFSET = 1 << 30  # 1 GiB; far past any plausible metadata size.


if sys.platform == "win32":  # pragma: no cover - platform branch
    import msvcrt

    def _try_lock(fd: int) -> bool:
        try:
            os.lseek(fd, _WIN_LOCK_OFFSET, os.SEEK_SET)
            msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
            return True
        except OSError:
            return False

    def _unlock(fd: int) -> None:
        try:
            os.lseek(fd, _WIN_LOCK_OFFSET, os.SEEK_SET)
            msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)
        except OSError:
            pass

else:  # pragma: no cover - platform branch
    import fcntl

    def _try_lock(fd: int) -> bool:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            return True
        except OSError:
            return False

    def _unlock(fd: int) -> None:
        try:
            fcntl.flock(fd, fcntl.LOCK_UN)
        except OSError:
            pass


@contextmanager
def workspace_lock(
    workspace_path: Path,
    *,
    timeout_seconds: float = 30.0,
    poll_interval: float = 0.25,
) -> Iterator[Path]:
    """Acquire an exclusive cross-platform lock for a workspace.

    The lock file lives at
    ``workspace_path / "interns" / "state" / "workspace.lock"`` and contains a
    single JSON line recording the holder's pid, the acquisition timestamp
    (UTC, ISO 8601), and the hostname.

    Args:
        workspace_path: Root of the workspace whose state is being mutated.
        timeout_seconds: Maximum time to wait for the lock before raising
            :class:`WorkspaceLockTimeout`.
        poll_interval: Seconds to sleep between acquisition attempts while
            contended.

    Re-entrancy: the lock is re-entrant within a single process *and* thread.
    If the calling thread already holds the lock for this workspace, nested
    acquisition succeeds immediately (a depth counter is incremented) and the
    OS-level lock is released only when the outermost ``with`` block exits.
    Acquisition from a different thread or process still blocks until the
    holder releases (or :class:`WorkspaceLockTimeout` is raised).

    Yields:
        The :class:`pathlib.Path` to the lock file.

    Raises:
        WorkspaceLockTimeout: If acquisition does not succeed within
            ``timeout_seconds``.
    """

    workspace_path = Path(workspace_path)
    lock_path = workspace_path.joinpath(*_LOCK_RELATIVE_PATH)
    lock_path.parent.mkdir(parents=True, exist_ok=True)

    key = _lock_key(lock_path)
    me = (os.getpid(), threading.get_ident())

    # Re-entrant fast path: this exact pid+thread already holds the lock.
    with _REENTRANT_GUARD:
        entry = _ACTIVE_LOCKS.get(key)
        if entry is not None and entry["owner"] == me:
            entry["depth"] += 1
            held_lock_path: Path = entry["lock_path"]
            nested = True
        else:
            nested = False
    if nested:
        try:
            yield held_lock_path
        finally:
            with _REENTRANT_GUARD:
                entry = _ACTIVE_LOCKS.get(key)
                if entry is not None and entry["owner"] == me:
                    entry["depth"] -= 1
        return

    # Open (or create) the lock file. O_RDWR is required on Windows because
    # msvcrt.locking insists on a writable handle.
    fd = os.open(lock_path, os.O_RDWR | os.O_CREAT, 0o644)

    deadline = time.monotonic() + max(0.0, float(timeout_seconds))
    acquired = False
    stale_reclaim_attempted = False
    try:
        while True:
            if _try_lock(fd):
                acquired = True
                break
            # Stale-holder reclamation: if the recorded PID is no longer alive
            # (process crashed, SIGKILL'd, or exited without cleanup), reset
            # the file once and retry. Only attempted once per acquire to
            # avoid spinning if the holder churns between checks.
            if not stale_reclaim_attempted:
                stale_reclaim_attempted = True
                holder_pid = _read_existing_holder_pid(lock_path)
                if holder_pid != "unknown" and not _pid_alive(holder_pid):
                    # Close + reopen to drop any stale kernel state, then try
                    # to lock again immediately. Windows requires this dance
                    # because msvcrt.locking releases on close, so a fresh
                    # handle gets a clean slate.
                    try:
                        os.close(fd)
                    except OSError:
                        pass
                    try:
                        lock_path.unlink()
                    except OSError:
                        pass
                    fd = os.open(lock_path, os.O_RDWR | os.O_CREAT, 0o644)
                    if _try_lock(fd):
                        acquired = True
                        break
            if time.monotonic() >= deadline:
                holder_pid = _read_existing_holder_pid(lock_path)
                raise WorkspaceLockTimeout(
                    f"Timed out waiting for workspace lock at {lock_path} "
                    f"(held by pid {holder_pid})"
                )
            time.sleep(max(0.0, float(poll_interval)))

        _write_metadata(fd, lock_path)
        with _REENTRANT_GUARD:
            _ACTIVE_LOCKS[key] = {"owner": me, "depth": 1, "lock_path": lock_path}
        yield lock_path
    finally:
        if acquired:
            with _REENTRANT_GUARD:
                entry = _ACTIVE_LOCKS.get(key)
                if entry is not None and entry["owner"] == me:
                    _ACTIVE_LOCKS.pop(key, None)
            _unlock(fd)
        try:
            os.close(fd)
        except OSError:
            pass
        # Best-effort cleanup of the lock file. On Windows a concurrent
        # waiter may already have re-opened it, in which case removal will
        # fail and that's fine.
        try:
            lock_path.unlink()
        except OSError:
            pass


__all__ = ["WorkspaceLockTimeout", "workspace_lock"]
