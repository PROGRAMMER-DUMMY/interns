"""Regression: a workspace root must live under `workspaces/`.

Origin (2026-07-26 audit): a direct WorkspaceLayout(...) call with a relative
path materialised `<repo>/rcm_dashboard/interns/reports/cost_ledger/` -- a
governed artifact written outside the workspace tree entirely.
"""
from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from core.paths import PROJECT_ROOT
from core.storage.workspace_layout import WorkspaceLayout


class WorkspaceRootContainmentTests(unittest.TestCase):
    def test_a_repo_root_sibling_is_rejected(self):
        with self.assertRaises(ValueError):
            WorkspaceLayout(project_root=PROJECT_ROOT / "rcm_dashboard")

    def test_the_repo_root_itself_is_rejected(self):
        with self.assertRaises(ValueError):
            WorkspaceLayout(project_root=PROJECT_ROOT)

    def test_a_real_workspace_is_accepted(self):
        layout = WorkspaceLayout(project_root=PROJECT_ROOT / "workspaces" / "demo_ws")
        self.assertTrue(str(layout.interns_dir).endswith("interns"))

    def test_a_temp_workspaces_subdir_is_accepted(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "workspaces" / "demo"
            layout = WorkspaceLayout(project_root=root)
            self.assertEqual(layout.project_root, root)

    def test_a_bare_temp_dir_is_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaises(ValueError):
                WorkspaceLayout(project_root=Path(tmp) / "demo")


if __name__ == "__main__":
    unittest.main()
