from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from tools.workspace_selection_harness import prepare_workspace_selection, render_text


class WorkspaceSelectionHarnessTests(unittest.TestCase):
    def test_empty_workspace_blocks_and_lists_available_workspaces(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "workspaces" / "CMS_Medical").mkdir(parents=True)
            other = root / "workspaces" / "Healthcare-RCM-Data-Platform"
            (other / "docs").mkdir(parents=True)
            (other / "datasets" / "EMR").mkdir(parents=True)
            (other / "docs" / "Sample KPI.xlsx").write_text("placeholder", encoding="utf-8")
            (other / "datasets" / "EMR" / "patients.csv").write_text("id\n1\n", encoding="utf-8")

            result = prepare_workspace_selection(root, "CMS medical")

            self.assertEqual(result.status, "blocked_empty_workspace")
            self.assertEqual(result.resolved_workspace, "workspaces/CMS_Medical")
            self.assertIn("contains no files", result.message)
            self.assertIn("workspace_settings.json", result.recommended_next_step)
            self.assertIn(
                "Healthcare-RCM-Data-Platform",
                {item["name"] for item in result.available_workspaces},
            )
            self.assertTrue(result.external_data_setup["do_not_copy_raw_data_into_workspace"])

            text = render_text(result)
            self.assertIn("Workspace Selection Harness", text)
            self.assertIn("blocked_empty_workspace", text)
            self.assertIn("External Data Workspace Option", text)
            self.assertIn("dataset_allowlist", text)

    def test_missing_workspace_blocks_with_available_choices(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "workspaces" / "Demo").mkdir(parents=True)

            result = prepare_workspace_selection(root, "Unknown Workspace")

            self.assertEqual(result.status, "blocked_missing_workspace")
            self.assertEqual(result.resolved_workspace, "workspaces/Unknown Workspace")
            self.assertIn("could not find", result.message)
            self.assertEqual(result.available_workspaces[0]["path"], "workspaces/Demo")

    def test_ready_workspace_requires_confirmation_not_onboarding(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            workspace = root / "workspaces" / "Demo"
            (workspace / "docs").mkdir(parents=True)
            (workspace / "datasets").mkdir()
            (workspace / "docs" / "Sample KPI.xlsx").write_text("placeholder", encoding="utf-8")
            (workspace / "datasets" / "patients.csv").write_text("id\n1\n", encoding="utf-8")

            result = prepare_workspace_selection(root, "Demo")

            self.assertEqual(result.status, "ready_for_confirmation")
            self.assertIn("confirm", result.recommended_next_step.lower())
            self.assertEqual(result.listing["file_count"], 2)


if __name__ == "__main__":
    unittest.main()
