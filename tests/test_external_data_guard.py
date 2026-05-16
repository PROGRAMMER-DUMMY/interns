from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from core.onboarding.workspace_onboarding import WorkspaceOnboarder
from core.storage.workspace_layout import WorkspaceLayout
from tools.list_workspace_files import list_workspace_files


class ExternalDataGuardTests(unittest.TestCase):
    def test_external_path_listing_is_bounded_and_does_not_create_interns(self):
        with tempfile.TemporaryDirectory() as repo_td, tempfile.TemporaryDirectory() as external_td:
            repo = Path(repo_td)
            external = Path(external_td)
            for idx in range(5):
                (external / f"cms_{idx}.csv").write_text("id\n1\n", encoding="utf-8")

            listing = list_workspace_files(repo, str(external), max_files=2)

            self.assertTrue(listing.exists)
            self.assertEqual(listing.external_state, "external_danger_root_bounded_listing_only")
            self.assertEqual(listing.file_count, 2)
            self.assertTrue(listing.truncated)
            self.assertFalse((external / "interns").exists())

    def test_onboarding_rejects_external_workspace(self):
        with tempfile.TemporaryDirectory() as repo_td, tempfile.TemporaryDirectory() as external_td:
            repo = Path(repo_td)
            external = Path(external_td)
            (external / "data.csv").write_text("id\n1\n", encoding="utf-8")

            with self.assertRaisesRegex(ValueError, "external data root"):
                WorkspaceOnboarder(repo, external).run()

            self.assertFalse((external / "interns").exists())

    def test_external_dataset_requires_allowlist(self):
        with tempfile.TemporaryDirectory() as repo_td, tempfile.TemporaryDirectory() as external_td:
            repo = Path(repo_td)
            workspace = repo / "workspaces" / "demo"
            external = Path(external_td)
            workspace.mkdir(parents=True)
            external_file = external / "approved.csv"
            external_file.write_text("id\n1\n", encoding="utf-8")
            layout = WorkspaceLayout(workspace)

            self.assertFalse(layout.is_dataset_allowed(external_file))

            layout.state_dir.mkdir(parents=True)
            layout.workspace_settings.write_text(
                json.dumps(
                    {
                        "dataset_allowlist": [
                            {
                                "type": "external_absolute",
                                "path": str(external_file),
                                "reason": "unit test",
                            }
                        ]
                    }
                ),
                encoding="utf-8",
            )

            self.assertTrue(layout.is_dataset_allowed(external_file))

    def test_onboarding_includes_allowlisted_external_file(self):
        with tempfile.TemporaryDirectory() as repo_td, tempfile.TemporaryDirectory() as external_td:
            repo = Path(repo_td)
            workspace = repo / "workspaces" / "demo"
            workspace.mkdir(parents=True)
            external_file = Path(external_td) / "approved.csv"
            external_file.write_text("id,value\n1,10\n", encoding="utf-8")
            layout = WorkspaceLayout(workspace)
            layout.state_dir.mkdir(parents=True)
            layout.workspace_settings.write_text(
                json.dumps(
                    {
                        "dataset_allowlist": [
                            {
                                "type": "external_absolute",
                                "path": str(external_file),
                                "reason": "unit test",
                            }
                        ]
                    }
                ),
                encoding="utf-8",
            )

            result = WorkspaceOnboarder(repo, "workspaces/demo", sample_rows=10).run()

            self.assertIn(str(external_file), result.inputs.data_files)
            self.assertEqual(result.profile_count, 1)
            self.assertFalse((Path(external_td) / "interns").exists())


if __name__ == "__main__":
    unittest.main()
