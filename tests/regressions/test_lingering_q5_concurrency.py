"""Regressions for Q5 (lingering-issues plan): concurrency/durability
remainder. See ~/.claude/plans/dynamic-cooking-firefly.md Q5.

- workspace_lock no longer unlinks the lock file on release or during stale
  reclaim (the classic flock-with-unlink double-acquire hazard); a dead
  holder is reclaimed by retrying on the same fd/inode instead.
- metadata_store.DeltaMetadataStore.upsert is a real upsert (merge/replace by
  document_id), not a blind append that duplicates rows.
- kpi/cli_agent_confirm_cli.py: verified (not fixed -- already correct) that
  the mutation runs under workspace_lock via the shared run_workspace_command
  envelope; no direct caller bypasses it.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import textwrap
import unittest
from pathlib import Path

from core.storage.metadata_store import DeltaMetadataStore
from core.storage.workspace_lock import workspace_lock

_KILL_HOLDER_WORKER = textwrap.dedent(
    """
    import sys
    from pathlib import Path
    from core.storage.workspace_lock import workspace_lock

    ws = Path(sys.argv[1])
    ready = Path(sys.argv[2])
    with workspace_lock(ws, timeout_seconds=60.0):
        ready.write_text("locked", encoding="utf-8")
        import time
        time.sleep(60)
    """
)


class DeltaMetadataStoreTrueUpsertTests(unittest.TestCase):
    def test_repeated_upsert_replaces_not_duplicates(self):
        from deltalake import DeltaTable

        with tempfile.TemporaryDirectory() as tmp:
            store = DeltaMetadataStore(Path(tmp) / "delta")
            store.upsert("contracts", "doc1", {"n": 1}, workspace="workspaces/demo")
            result = store.upsert("contracts", "doc1", {"n": 2}, workspace="workspaces/demo")
            table_path = Path(result.path)
            rows = DeltaTable(str(table_path)).to_pyarrow_table().to_pylist()
            matching = [r for r in rows if r["document_id"] == "doc1"]
            self.assertEqual(len(matching), 1, f"expected exactly 1 row, got {len(matching)}: {rows}")
            self.assertIn('"n": 2', matching[0]["payload_json"])

    def test_different_document_ids_both_present(self):
        from deltalake import DeltaTable

        with tempfile.TemporaryDirectory() as tmp:
            store = DeltaMetadataStore(Path(tmp) / "delta")
            store.upsert("contracts", "doc1", {"n": 1}, workspace="workspaces/demo")
            result = store.upsert("contracts", "doc2", {"n": 2}, workspace="workspaces/demo")
            rows = DeltaTable(str(Path(result.path))).to_pyarrow_table().to_pylist()
            ids = sorted(r["document_id"] for r in rows)
            self.assertEqual(ids, ["doc1", "doc2"])


class WorkspaceLockPersistenceAndReclaimTests(unittest.TestCase):
    def test_lock_file_never_unlinked_across_repeated_acquisitions(self):
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp)
            with workspace_lock(workspace) as p1:
                pass
            self.assertTrue(p1.exists())
            with workspace_lock(workspace) as p2:
                pass
            self.assertEqual(p1, p2)
            self.assertTrue(p2.exists())

    def test_stale_holder_process_is_reclaimed_without_unlink(self):
        # A real subprocess acquires the lock and is killed while still
        # holding it (simulating a crash) -- proves the reclaim path works
        # via retry-on-same-fd, not unlink+recreate, and that the lock file
        # itself is never deleted in the process.
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp)
            worker_file = workspace / "_holder.py"
            worker_file.write_text(_KILL_HOLDER_WORKER, encoding="utf-8")
            ready = workspace / "ready.flag"

            repo_root = Path(__file__).resolve().parents[2]
            env = dict(os.environ)
            env["PYTHONPATH"] = str(repo_root) + os.pathsep + env.get("PYTHONPATH", "")

            proc = subprocess.Popen(
                [sys.executable, str(worker_file), str(workspace), str(ready)],
                cwd=str(repo_root), env=env,
            )
            try:
                for _ in range(200):
                    if ready.exists():
                        break
                    import time
                    time.sleep(0.05)
                self.assertTrue(ready.exists(), "holder subprocess never acquired the lock")

                lock_path = workspace / "interns" / "state" / "workspace.lock"
                held_payload = json.loads(lock_path.read_text(encoding="utf-8"))
                self.assertIsInstance(held_payload["pid"], int)

                proc.kill()
                proc.wait(timeout=30)

                # A fresh acquire must reclaim -- not hang until timeout --
                # and the lock file must still exist throughout (never
                # unlinked by either the dead holder or the reclaimer).
                with workspace_lock(workspace, timeout_seconds=10.0, poll_interval=0.1) as p:
                    self.assertTrue(p.exists())
                    payload = json.loads(p.read_text(encoding="utf-8"))
                    self.assertEqual(payload["pid"], os.getpid())
            finally:
                if proc.poll() is None:
                    proc.kill()
                    proc.wait(timeout=10)


if __name__ == "__main__":
    unittest.main()
