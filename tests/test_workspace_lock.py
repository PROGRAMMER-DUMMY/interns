"""Tests for :mod:`core.storage.workspace_lock`."""

from __future__ import annotations

import json
import os
import socket
import tempfile
import unittest
from pathlib import Path

from core.storage.workspace_lock import WorkspaceLockTimeout, workspace_lock


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

    def test_second_acquire_times_out(self) -> None:
        with workspace_lock(self.workspace):
            with self.assertRaises(WorkspaceLockTimeout) as ctx:
                with workspace_lock(
                    self.workspace,
                    timeout_seconds=0.5,
                    poll_interval=0.05,
                ):
                    self.fail("Second acquisition should not have succeeded")

            message = str(ctx.exception)
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


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
