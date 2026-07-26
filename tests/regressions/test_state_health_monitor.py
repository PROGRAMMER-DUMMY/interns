"""New feature: workspace state-file health monitor.

None of the per-workspace state/audit files (trajectory.jsonl,
audit_chain.jsonl, session.json under workflow_sessions/*, run.log) rotate --
confirmed during the earlier profiler/audit-chain work this session, and
rotation was explicitly deferred (audit_chain.jsonl can't be naively
truncated without a chain-continuity design). This tool gives visibility into
that growth before it becomes a problem: a read-only report over
WorkspaceLayout.state_dir, sizes/ages/kind classification, glob-discovered
rather than hardcoded to the several independent modules that each write
their own session.json.

See core/onboarding/harness/state_health.py.
"""
from __future__ import annotations

import json
import tempfile
import time
import unittest
from pathlib import Path

from core.onboarding.harness.state_health import (
    record_state_health,
    scan_all_workspaces,
    scan_workspace_state,
)
from core.storage.workspace_layout import WorkspaceLayout


def _make_workspace(repo_root: Path, name: str) -> Path:
    ws = repo_root / "workspaces" / name
    ws.mkdir(parents=True)
    return ws


class ScanWorkspaceStateTests(unittest.TestCase):
    def test_empty_state_dir_reports_zero(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo_root = Path(tmp)
            ws = _make_workspace(repo_root, "demo")
            layout = WorkspaceLayout(project_root=ws)
            layout.ensure_runtime_dirs()
            scan = scan_workspace_state(layout, repo_root=repo_root)
            self.assertEqual(scan["file_count"], 0)
            self.assertEqual(scan["total_bytes"], 0)

    def test_known_files_are_classified_by_kind(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo_root = Path(tmp)
            ws = _make_workspace(repo_root, "demo")
            layout = WorkspaceLayout(project_root=ws)
            layout.ensure_runtime_dirs()
            (layout.state_dir / "trajectory.jsonl").write_text('{"a": 1}\n', encoding="utf-8")
            (layout.state_dir / "audit_chain.jsonl").write_text('{"b": 2}\n', encoding="utf-8")
            session_dir = layout.workflow_sessions_dir / "sess1"
            session_dir.mkdir(parents=True)
            (session_dir / "session.json").write_text("{}", encoding="utf-8")

            scan = scan_workspace_state(layout, repo_root=repo_root)
            kinds = {f["kind"] for f in scan["top_files"]}
            self.assertIn("trajectory_log", kinds)
            self.assertIn("audit_chain", kinds)
            self.assertIn("workflow_session", kinds)
            self.assertEqual(scan["session_file_count"], 1)

    def test_unknown_file_is_bucketed_as_other_not_dropped(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo_root = Path(tmp)
            ws = _make_workspace(repo_root, "demo")
            layout = WorkspaceLayout(project_root=ws)
            layout.ensure_runtime_dirs()
            (layout.state_dir / "some_future_module_state.json").write_text("{}", encoding="utf-8")

            scan = scan_workspace_state(layout, repo_root=repo_root)
            self.assertEqual(scan["file_count"], 1)
            self.assertEqual(scan["top_files"][0]["kind"], "other")

    def test_large_file_is_flagged(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo_root = Path(tmp)
            ws = _make_workspace(repo_root, "demo")
            layout = WorkspaceLayout(project_root=ws)
            layout.ensure_runtime_dirs()
            big = layout.state_dir / "trajectory.jsonl"
            big.write_bytes(b"x" * (11 * 1024 * 1024))

            scan = scan_workspace_state(layout, repo_root=repo_root)
            self.assertEqual(len(scan["large_files"]), 1)
            self.assertTrue(scan["large_files"][0]["large"])

    def test_stale_session_is_flagged(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo_root = Path(tmp)
            ws = _make_workspace(repo_root, "demo")
            layout = WorkspaceLayout(project_root=ws)
            layout.ensure_runtime_dirs()
            session_dir = layout.workflow_sessions_dir / "old_sess"
            session_dir.mkdir(parents=True)
            stale_file = session_dir / "session.json"
            stale_file.write_text("{}", encoding="utf-8")
            old_time = time.time() - (200 * 86400)
            import os
            os.utime(stale_file, (old_time, old_time))

            scan = scan_workspace_state(layout, repo_root=repo_root)
            self.assertEqual(scan["stale_session_count"], 1)

    def test_scan_never_writes_anything(self):
        """Read-only: scanning must not create/modify any file the caller
        didn't already have (report artifacts are only written by
        record_state_health, never by the scan function itself)."""
        with tempfile.TemporaryDirectory() as tmp:
            repo_root = Path(tmp)
            ws = _make_workspace(repo_root, "demo")
            layout = WorkspaceLayout(project_root=ws)
            layout.ensure_runtime_dirs()
            (layout.state_dir / "trajectory.jsonl").write_text("{}\n", encoding="utf-8")
            before = sorted(p.as_posix() for p in ws.rglob("*") if p.is_file())
            scan_workspace_state(layout, repo_root=repo_root)
            after = sorted(p.as_posix() for p in ws.rglob("*") if p.is_file())
            self.assertEqual(before, after)


class RecordStateHealthTests(unittest.TestCase):
    def test_writes_json_and_markdown_report(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo_root = Path(tmp)
            ws = _make_workspace(repo_root, "demo")
            layout = WorkspaceLayout(project_root=ws)
            layout.ensure_runtime_dirs()
            (layout.state_dir / "trajectory.jsonl").write_text('{"x": 1}\n', encoding="utf-8")

            result = record_state_health(repo_root, "workspaces/demo")
            json_path = repo_root / result.report_json_path
            md_path = repo_root / result.report_markdown_path
            self.assertTrue(json_path.exists())
            self.assertTrue(md_path.exists())
            payload = json.loads(json_path.read_text(encoding="utf-8"))
            self.assertEqual(payload["file_count"], 1)
            self.assertIn("# Workspace State-File Health", md_path.read_text(encoding="utf-8"))

    def test_missing_workspace_raises(self):
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaises(FileNotFoundError):
                record_state_health(tmp, "workspaces/does_not_exist")


class ScanAllWorkspacesTests(unittest.TestCase):
    def test_rollup_covers_every_workspace_sorted_largest_first(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo_root = Path(tmp)
            small_ws = _make_workspace(repo_root, "small")
            big_ws = _make_workspace(repo_root, "big")
            WorkspaceLayout(project_root=small_ws).ensure_runtime_dirs()
            big_layout = WorkspaceLayout(project_root=big_ws)
            big_layout.ensure_runtime_dirs()
            (big_layout.state_dir / "trajectory.jsonl").write_bytes(b"x" * 5000)

            summaries = scan_all_workspaces(repo_root)
            names = [s["workspace"] for s in summaries]
            self.assertIn("workspaces/small", names)
            self.assertIn("workspaces/big", names)
            self.assertEqual(summaries[0]["workspace"], "workspaces/big")

    def test_no_workspaces_dir_returns_empty(self):
        with tempfile.TemporaryDirectory() as tmp:
            self.assertEqual(scan_all_workspaces(tmp), [])


if __name__ == "__main__":
    unittest.main()
