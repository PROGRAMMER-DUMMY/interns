"""Tests for :mod:`core.storage.workspace_lock`."""

from __future__ import annotations

import json
import os
import socket
import subprocess
import sys
import tempfile
import textwrap
import threading
import unittest
from pathlib import Path

from core.storage.workspace_lock import WorkspaceLockTimeout, workspace_lock

# Cross-process worker: acquire the workspace lock, append an enter/exit event
# pair with a brief hold, then release. Several of these launched together let
# the test prove the lock excludes across *processes* (the real CLI scenario),
# not just across threads in one process. The hold window forces waiters to be
# mid-poll while the holder runs its release path (which unlinks the lock
# file) -- the exact window where a faulty unlink-on-release would let two
# processes hold simultaneously.
_CROSS_PROCESS_WORKER = textwrap.dedent(
    """
    import json, os, sys, time
    from pathlib import Path
    from core.storage.workspace_lock import workspace_lock

    ws = Path(sys.argv[1])
    hold = float(sys.argv[2])
    record = Path(sys.argv[3])
    who = sys.argv[4]
    with workspace_lock(ws, timeout_seconds=60.0):
        with record.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps({"who": who, "ev": "enter", "t": time.time()}) + "\\n")
        time.sleep(hold)
        with record.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps({"who": who, "ev": "exit", "t": time.time()}) + "\\n")
    """
)


class WorkspaceLockTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.workspace = Path(self._tmp.name)

    def test_acquire_and_release_creates_and_removes_lock_file(self) -> None:
        lock_path_seen: Path | None = None
        with workspace_lock(self.workspace) as lock_path:
            lock_path_seen = lock_path
            self.assertTrue(lock_path.exists())
            payload = json.loads(lock_path.read_text(encoding="utf-8"))
            self.assertEqual(payload["pid"], os.getpid())
            self.assertIn("acquired_at", payload)
            self.assertIn("hostname", payload)

        assert lock_path_seen is not None
        self.assertFalse(
            lock_path_seen.exists(),
            f"Lock file {lock_path_seen} should be removed on release",
        )

    def test_second_acquire_from_other_thread_times_out(self) -> None:
        """A different owner (here: another thread) must still time out.

        Same-thread nested acquisition is re-entrant by design (see
        tests/test_workspace_lock_reentrancy.py), so the contending acquire
        runs on a separate thread to exercise the cross-owner path.
        """
        outcome: dict[str, object] = {}

        def contend() -> None:
            try:
                with workspace_lock(
                    self.workspace,
                    timeout_seconds=0.5,
                    poll_interval=0.05,
                ):
                    outcome["acquired"] = True
            except WorkspaceLockTimeout as exc:
                outcome["error"] = str(exc)

        with workspace_lock(self.workspace):
            thread = threading.Thread(target=contend)
            thread.start()
            thread.join(timeout=30)
            self.assertFalse(thread.is_alive(), "contending thread must finish")

        self.assertNotIn(
            "acquired",
            outcome,
            "Second acquisition from another thread should not have succeeded",
        )
        message = str(outcome.get("error", ""))
        self.assertIn("workspace.lock", message)
        self.assertIn(str(os.getpid()), message)

    def test_lock_file_records_pid_and_hostname(self) -> None:
        with workspace_lock(self.workspace) as lock_path:
            raw = lock_path.read_text(encoding="utf-8").strip()
            self.assertTrue(raw, "Lock file should contain metadata while held")
            payload = json.loads(raw)
            self.assertEqual(payload["pid"], os.getpid())
            self.assertEqual(payload["hostname"], socket.gethostname())

    def test_lock_creates_state_dir_if_missing(self) -> None:
        state_dir = self.workspace / "interns" / "state"
        self.assertFalse(state_dir.exists())

        with workspace_lock(self.workspace) as lock_path:
            self.assertTrue(state_dir.is_dir())
            self.assertEqual(lock_path.parent, state_dir)

    def test_cross_process_mutual_exclusion(self) -> None:
        """Separate processes must never hold the lock simultaneously.

        Mirrors the governed CLI envelope: each apply-* runs in its own
        process, wrapping its read-modify-write in ``workspace_lock``. If the
        lock fails to serialise across processes, overlapping applies clobber
        each other (the reported non-monotonic count). Several overlapping
        workers with a short hold window stress the release/unlink path.
        """
        worker_file = self.workspace / "_lock_worker.py"
        worker_file.write_text(_CROSS_PROCESS_WORKER, encoding="utf-8")
        record = self.workspace / "events.jsonl"
        record.write_text("", encoding="utf-8")

        repo_root = Path(__file__).resolve().parents[1]
        env = dict(os.environ)
        env["PYTHONPATH"] = str(repo_root) + os.pathsep + env.get("PYTHONPATH", "")

        worker_count = 8
        procs = [
            subprocess.Popen(
                [sys.executable, str(worker_file), str(self.workspace), "0.15", str(record), f"p{i}"],
                cwd=str(repo_root),
                env=env,
            )
            for i in range(worker_count)
        ]
        for proc in procs:
            self.assertEqual(proc.wait(timeout=120), 0, "worker process must exit cleanly")

        events = [
            json.loads(line)
            for line in record.read_text(encoding="utf-8").splitlines()
            if line.strip()
        ]
        enters = sum(1 for e in events if e["ev"] == "enter")
        self.assertEqual(enters, worker_count, "every worker must have acquired the lock")

        events.sort(key=lambda e: e["t"])
        depth = 0
        max_depth = 0
        for event in events:
            depth += 1 if event["ev"] == "enter" else -1
            max_depth = max(max_depth, depth)
        self.assertLessEqual(
            max_depth,
            1,
            f"workspace_lock must give cross-process mutual exclusion; "
            f"observed {max_depth} simultaneous holders",
        )


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
