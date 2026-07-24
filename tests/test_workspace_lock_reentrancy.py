"""Re-entrancy regression tests for :mod:`core.storage.workspace_lock`.

Regression for the `prepare-kpi-blocker-panel --force-onboard` self-deadlock:
`prepare_main` wraps the workflow in ``with workspace_lock(...)`` and the
nested onboarding step (`WorkspaceOnboarder.run`) acquires the same lock in
the same process. Before the fix the inner acquisition contended on the
OS-level lock against its own process and timed out with
``WorkspaceLockTimeout``.

Contract proven here:
- Nested ``with workspace_lock(...)`` in the same process+thread succeeds
  immediately (no deadlock, no timeout) to arbitrary depth.
- The OS-level lock is released only when the outermost block exits.
- A different thread in the same process still blocks and times out.
- A different process still blocks and times out (the original cross-process
  mutual-exclusion guarantee is unchanged).
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import textwrap
import threading
import time
import unittest
from pathlib import Path

from core.storage.workspace_lock import WorkspaceLockTimeout, workspace_lock

# Cross-process contender: try to acquire an already-held lock with a short
# timeout. Exit 0 if (correctly) timed out, exit 3 if it wrongly acquired.
_CROSS_PROCESS_CONTENDER = textwrap.dedent(
    """
    import sys
    from pathlib import Path
    from core.storage.workspace_lock import WorkspaceLockTimeout, workspace_lock

    ws = Path(sys.argv[1])
    try:
        with workspace_lock(ws, timeout_seconds=1.0, poll_interval=0.05):
            sys.exit(3)
    except WorkspaceLockTimeout:
        sys.exit(0)
    """
)


class WorkspaceLockReentrancyTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.workspace = Path(self._tmp.name)

    def test_nested_acquire_same_thread_does_not_deadlock(self) -> None:
        """The --force-onboard shape: outer CLI lock + inner onboarding lock."""
        started = time.monotonic()
        with workspace_lock(self.workspace, timeout_seconds=5.0) as outer_path:
            with workspace_lock(self.workspace, timeout_seconds=5.0) as inner_path:
                self.assertEqual(outer_path, inner_path)
                self.assertTrue(outer_path.exists())
        elapsed = time.monotonic() - started
        self.assertLess(
            elapsed,
            4.0,
            "Nested same-thread acquisition must be immediate, not wait out "
            "a timeout",
        )

    def test_nested_acquire_three_levels_deep(self) -> None:
        with workspace_lock(self.workspace, timeout_seconds=5.0) as p1:
            with workspace_lock(self.workspace, timeout_seconds=5.0) as p2:
                with workspace_lock(self.workspace, timeout_seconds=5.0) as p3:
                    self.assertEqual(p1, p2)
                    self.assertEqual(p2, p3)
                    self.assertTrue(p1.exists())
                self.assertTrue(p1.exists(), "inner exit must not release")
            self.assertTrue(p1.exists(), "middle exit must not release")
        # The lock file is deliberately never unlinked (flock-with-unlink
        # hazard); it persists as a small sentinel after the outermost exit.
        self.assertTrue(p1.exists(), "lock file persists after outermost exit")

    def test_inner_exit_keeps_lock_held_until_outer_exit(self) -> None:
        """While nested, another owner must still be excluded; after the
        outermost exit it must be able to acquire."""
        with workspace_lock(self.workspace, timeout_seconds=5.0):
            with workspace_lock(self.workspace, timeout_seconds=5.0):
                pass
            # Inner block exited, but the outer block still holds the lock:
            # a contending thread must time out.
            self.assertFalse(self._other_thread_can_acquire(timeout_seconds=0.5))
        # Fully released: a contending thread acquires immediately.
        self.assertTrue(self._other_thread_can_acquire(timeout_seconds=5.0))

    def test_nested_acquire_via_unresolved_path_alias(self) -> None:
        """Re-entrancy must key on the resolved lock file, not the exact
        Path object spelling (callers pass resolved and unresolved forms)."""
        (self.workspace / "sub").mkdir()
        alias = self.workspace / "sub" / ".."
        with workspace_lock(self.workspace, timeout_seconds=5.0):
            started = time.monotonic()
            with workspace_lock(alias, timeout_seconds=5.0):
                pass
            self.assertLess(time.monotonic() - started, 4.0)

    def test_other_thread_still_blocks_while_held(self) -> None:
        with workspace_lock(self.workspace, timeout_seconds=5.0):
            self.assertFalse(self._other_thread_can_acquire(timeout_seconds=0.5))

    def test_other_process_still_blocks_while_held(self) -> None:
        contender = self.workspace / "_contender.py"
        contender.write_text(_CROSS_PROCESS_CONTENDER, encoding="utf-8")

        repo_root = Path(__file__).resolve().parents[1]
        env = dict(os.environ)
        env["PYTHONPATH"] = str(repo_root) + os.pathsep + env.get("PYTHONPATH", "")

        with workspace_lock(self.workspace, timeout_seconds=5.0):
            proc = subprocess.run(
                [sys.executable, str(contender), str(self.workspace)],
                cwd=str(repo_root),
                env=env,
                timeout=120,
            )
        self.assertEqual(
            proc.returncode,
            0,
            "Cross-process contender must time out (exit 0) while the lock "
            "is held; exit 3 means it wrongly acquired",
        )

    def test_reentrancy_state_clears_after_release(self) -> None:
        """A fresh acquire after release must take the real OS lock again
        (no stale registry entry pretending the lock is still owned)."""
        with workspace_lock(self.workspace, timeout_seconds=5.0):
            pass
        with workspace_lock(self.workspace, timeout_seconds=5.0) as lock_path:
            payload = json.loads(lock_path.read_text(encoding="utf-8"))
            self.assertEqual(payload["pid"], os.getpid())
            # While held again, another thread must still be excluded.
            self.assertFalse(self._other_thread_can_acquire(timeout_seconds=0.5))

    def _other_thread_can_acquire(self, *, timeout_seconds: float) -> bool:
        """Run a contending acquire on a separate thread; report success."""
        outcome: dict[str, bool] = {}

        def contend() -> None:
            try:
                with workspace_lock(
                    self.workspace,
                    timeout_seconds=timeout_seconds,
                    poll_interval=0.05,
                ):
                    outcome["acquired"] = True
            except WorkspaceLockTimeout:
                outcome["acquired"] = False

        thread = threading.Thread(target=contend)
        thread.start()
        thread.join(timeout=60)
        self.assertFalse(thread.is_alive(), "contending thread must finish")
        return outcome.get("acquired", False)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
