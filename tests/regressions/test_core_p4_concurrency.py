"""Regression tests for core remediation P4 — concurrency & durability.

Themes T1 (process-global os.chdir races) + T6 (non-atomic / unlocked writes,
silent corrupt-store reset). Workspace-agnostic.
"""
from __future__ import annotations

import json
import os
import threading
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory


# ── P4a: atomic IO + fail-loud corrupt store ─────────────────────────────────
class AtomicIoTests(unittest.TestCase):
    def test_write_json_atomic_roundtrip(self) -> None:
        from core.storage.atomic_io import write_json_atomic

        with TemporaryDirectory() as tmp:
            p = Path(tmp) / "sub" / "store.json"
            write_json_atomic(p, {"a": 1})
            self.assertEqual(json.loads(p.read_text()), {"a": 1})
            # No leftover temp files in the directory.
            self.assertEqual([f.name for f in p.parent.iterdir()], ["store.json"])

    def test_read_missing_returns_default(self) -> None:
        from core.storage.atomic_io import read_json_or_quarantine

        with TemporaryDirectory() as tmp:
            self.assertEqual(read_json_or_quarantine(Path(tmp) / "x.json", default={}), {})

    def test_corrupt_store_quarantined_and_raises(self) -> None:
        from core.storage.atomic_io import CorruptStoreError, read_json_or_quarantine

        with TemporaryDirectory() as tmp:
            p = Path(tmp) / "store.json"
            p.write_text("{ not json", encoding="utf-8")
            with self.assertRaises(CorruptStoreError):
                read_json_or_quarantine(p, default={})
            # Original is renamed aside, not left to be overwritten.
            self.assertFalse(p.exists())
            self.assertTrue(any(f.name.startswith("store.json.corrupt-") for f in Path(tmp).iterdir()))


class MemoryStoreDurabilityTests(unittest.TestCase):
    def test_corrupt_definitions_not_silently_reset(self) -> None:
        from core.onboarding.memory.workspace_definitions import (
            load_workspace_definitions,
            workspace_definitions_path,
        )
        from core.storage.atomic_io import CorruptStoreError
        from core.storage.workspace_layout import WorkspaceLayout

        with TemporaryDirectory() as tmp:
            layout = WorkspaceLayout(project_root=Path(tmp))
            path = workspace_definitions_path(layout)
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text('{"definitions": [ truncated', encoding="utf-8")
            # Must NOT silently return an empty skeleton (which the next write
            # would persist, erasing human definitions) — it must fail loud.
            with self.assertRaises(CorruptStoreError):
                load_workspace_definitions(layout)


# ── P4b: no os.chdir races (thread-safe pushd) ───────────────────────────────
class PushdTests(unittest.TestCase):
    def test_pushd_restores_cwd_and_serializes(self) -> None:
        from core.storage.atomic_io import pushd

        original = os.getcwd()
        with TemporaryDirectory() as tmp:
            with pushd(tmp):
                self.assertEqual(Path(os.getcwd()).resolve(), Path(tmp).resolve())
            self.assertEqual(os.getcwd(), original)

    def test_concurrent_pushd_never_interleaves(self) -> None:
        from core.storage.atomic_io import pushd

        original = os.getcwd()
        errors: list[str] = []

        with TemporaryDirectory() as a, TemporaryDirectory() as b:
            def worker(target: str) -> None:
                for _ in range(50):
                    with pushd(target):
                        if Path(os.getcwd()).resolve() != Path(target).resolve():
                            errors.append(target)

            threads = [threading.Thread(target=worker, args=(t,)) for t in (a, b, a, b)]
            for t in threads:
                t.start()
            for t in threads:
                t.join()

        self.assertEqual(errors, [])
        self.assertEqual(os.getcwd(), original)

    def test_no_bare_oschdir_in_targeted_modules(self) -> None:
        # The chdir sites the audit flagged must route through pushd, not bare
        # os.chdir. Ref: SUMMARY.md T1.
        import core.dashboard.profile as m1
        import core.dashboard.renderer as m2
        import core.onboarding.kpi.execution_harness as m3
        import core.onboarding.kpi.panel_preview_executor as m4
        import core.onboarding.kpi.verify_kpi_output as m5
        import core.onboarding.workspace.flow as m6

        for mod in (m1, m2, m3, m4, m5, m6):
            src = Path(mod.__file__).read_text(encoding="utf-8")
            self.assertNotIn("os.chdir(", src, f"{mod.__name__} still uses bare os.chdir")


# ── P4c: SQLite is WAL + cross-thread safe ───────────────────────────────────
class SqliteConcurrencyTests(unittest.TestCase):
    def test_cross_thread_writes_do_not_raise(self) -> None:
        from core.storage.workspace import Workspace

        with TemporaryDirectory() as tmp:
            db = Path(tmp) / "state" / "workspace.db"
            ws = Workspace(db_path=db, work_dir=Path(tmp))
            errors: list[BaseException] = []

            def writer(n: int) -> None:
                try:
                    for i in range(20):
                        ws.log_experiment(
                            run_id=f"r{n}-{i}", results="{}", status="ok", metrics="{}"
                        )
                except BaseException as exc:  # noqa: BLE001
                    errors.append(exc)

            threads = [threading.Thread(target=writer, args=(n,)) for n in range(4)]
            for t in threads:
                t.start()
            for t in threads:
                t.join()
            ws.conn.close()  # release the file so TemporaryDirectory can clean up (Windows)
            self.assertEqual(errors, [], f"cross-thread writes raised: {errors}")

    def test_wal_mode_enabled(self) -> None:
        from core.storage.workspace import Workspace

        with TemporaryDirectory() as tmp:
            db = Path(tmp) / "state" / "workspace.db"
            ws = Workspace(db_path=db, work_dir=Path(tmp))
            mode = ws.conn.execute("PRAGMA journal_mode;").fetchone()[0]
            ws.conn.close()  # release the file (Windows temp cleanup)
            self.assertEqual(str(mode).lower(), "wal")


if __name__ == "__main__":
    unittest.main()
