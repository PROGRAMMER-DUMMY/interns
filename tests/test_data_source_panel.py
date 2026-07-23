"""core.onboarding.data_source_panel: the explicit, human-confirmed,
workspace-level choice of where a workspace's data lives -- local files,
Databricks additively merged with local files, or Databricks exclusively.
"""
from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from core.onboarding.data_source_panel import (
    DataSourceAnswerRecorder,
    DataSourcePanelBuilder,
)
from core.onboarding.workspace.validation import WorkspaceArtifactValidator
from core.storage.workspace_layout import WorkspaceLayout


class DataSourcePanelGenerationTests(unittest.TestCase):
    def _workspace(self, root: Path, settings: dict | None = None) -> Path:
        workspace = root / "workspaces" / "demo"
        workspace.mkdir(parents=True, exist_ok=True)
        if settings is not None:
            (workspace / "workspace_settings.json").write_text(
                json.dumps(settings), encoding="utf-8"
            )
        return workspace

    def test_no_databricks_source_recommends_local_files(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self._workspace(root)
            result = DataSourcePanelBuilder(root, "workspaces/demo").prepare()
            panel = json.loads((root / result.current_json_path).read_text(encoding="utf-8"))
            self.assertEqual(panel["current_mode"], "local_files")
            self.assertEqual(panel["recommended_option_id"], "local_files")
            self.assertFalse(panel["mode_declared_explicitly"])
            self.assertEqual(panel["status"], "needs_user_answer")

    def test_declared_without_mode_needs_answer_and_recommends_additive(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self._workspace(
                root, {"databricks_source": {"catalog": "main", "schema": "bronze"}}
            )
            result = DataSourcePanelBuilder(root, "workspaces/demo").prepare()
            panel = json.loads((root / result.current_json_path).read_text(encoding="utf-8"))
            self.assertEqual(panel["current_mode"], "additive")
            self.assertEqual(panel["recommended_option_id"], "databricks_additive")
            self.assertFalse(panel["mode_declared_explicitly"])
            self.assertEqual(panel["status"], "needs_user_answer")
            self.assertEqual(panel["current_databricks_source"]["catalog"], "main")

    def test_explicit_mode_is_marked_answered(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self._workspace(
                root,
                {"databricks_source": {"catalog": "main", "schema": "bronze", "mode": "exclusive"}},
            )
            result = DataSourcePanelBuilder(root, "workspaces/demo").prepare()
            panel = json.loads((root / result.current_json_path).read_text(encoding="utf-8"))
            self.assertTrue(panel["mode_declared_explicitly"])
            self.assertEqual(panel["status"], "answered")


class DataSourceAnswerRecorderTests(unittest.TestCase):
    def _workspace(self, root: Path) -> Path:
        workspace = root / "workspaces" / "demo"
        workspace.mkdir(parents=True, exist_ok=True)
        return workspace

    def test_local_files_answer_needs_no_catalog_schema(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self._workspace(root)
            result = DataSourceAnswerRecorder(root, "workspaces/demo").apply("local_files")
            self.assertEqual(result["mode"], "local_files")
            layout = WorkspaceLayout(project_root=root / "workspaces" / "demo")
            self.assertEqual(layout.databricks_source_mode(), "local_files")

    def test_exclusive_answer_requires_catalog_and_schema(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self._workspace(root)
            with self.assertRaises(ValueError):
                DataSourceAnswerRecorder(root, "workspaces/demo").apply("databricks_exclusive")

    def test_exclusive_answer_with_catalog_schema_persists_and_records_human_provenance(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self._workspace(root)
            result = DataSourceAnswerRecorder(root, "workspaces/demo").apply(
                "databricks_exclusive",
                catalog="healthcare_rcm",
                schema="bronze",
                confirmed_by="shubham",
            )
            self.assertEqual(result["mode"], "exclusive")
            self.assertEqual(result["source"], "human")
            settings = json.loads((root / result["settings_path"]).read_text(encoding="utf-8"))
            self.assertEqual(settings["databricks_source"]["mode"], "exclusive")
            self.assertEqual(settings["databricks_source"]["catalog"], "healthcare_rcm")
            layout = WorkspaceLayout(project_root=root / "workspaces" / "demo")
            self.assertEqual(layout.databricks_source_mode(), "exclusive")

    def test_empty_confirmed_by_records_agent_source(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self._workspace(root)
            result = DataSourceAnswerRecorder(root, "workspaces/demo").apply(
                "databricks_additive", catalog="main", schema="bronze"
            )
            self.assertEqual(result["source"], "agent")
            self.assertEqual(result["confirmed_by"], "")

    def test_catalog_schema_can_be_declared_once_and_reused_on_a_later_mode_change(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self._workspace(root)
            recorder = DataSourceAnswerRecorder(root, "workspaces/demo")
            recorder.apply("databricks_additive", catalog="main", schema="bronze", confirmed_by="shubham")
            # A later mode change doesn't need catalog/schema repeated.
            result = recorder.apply("databricks_exclusive", confirmed_by="shubham")
            self.assertEqual(result["mode"], "exclusive")
            settings = json.loads((root / result["settings_path"]).read_text(encoding="utf-8"))
            self.assertEqual(settings["databricks_source"]["catalog"], "main")
            self.assertEqual(settings["databricks_source"]["schema"], "bronze")

    def test_invalid_answer_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self._workspace(root)
            with self.assertRaises(ValueError):
                DataSourceAnswerRecorder(root, "workspaces/demo").apply("bogus_mode")


class DataSourcePanelValidationTests(unittest.TestCase):
    """validate-workspace-artifacts knows the data_source_panel/current.json
    contract -- but its ABSENCE (the overwhelming majority of workspaces,
    which never declare a databricks_source) must never be an error."""

    def _workspace(self, root: Path) -> Path:
        workspace = root / "workspaces" / "demo"
        workspace.mkdir(parents=True, exist_ok=True)
        return workspace

    def test_missing_panel_is_not_an_error(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self._workspace(root)
            validator = WorkspaceArtifactValidator(root, "workspaces/demo")
            validator._validate_data_source_panel()
            self.assertEqual(validator.result.errors, [])

    def test_well_formed_panel_passes(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self._workspace(root)
            DataSourcePanelBuilder(root, "workspaces/demo").prepare()
            validator = WorkspaceArtifactValidator(root, "workspaces/demo")
            validator._validate_data_source_panel()
            self.assertEqual(validator.result.errors, [])

    def test_malformed_panel_missing_required_key_is_an_error(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            workspace = self._workspace(root)
            layout = WorkspaceLayout(project_root=workspace)
            panel_dir = layout.reports_dir / "data_source_panel"
            panel_dir.mkdir(parents=True, exist_ok=True)
            (panel_dir / "current.json").write_text(
                json.dumps(
                    {
                        "artifact_type": "data_source_panel/current.json",
                        "version": 1,
                        "generated_by": "prepare-data-source-panel",
                        "status": "needs_user_answer",
                        # current_mode/options/recommended_option_id missing
                    }
                ),
                encoding="utf-8",
            )
            validator = WorkspaceArtifactValidator(root, "workspaces/demo")
            validator._validate_data_source_panel()
            self.assertTrue(validator.result.errors)

    def test_recommended_option_not_in_options_is_an_error(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            workspace = self._workspace(root)
            layout = WorkspaceLayout(project_root=workspace)
            panel_dir = layout.reports_dir / "data_source_panel"
            panel_dir.mkdir(parents=True, exist_ok=True)
            (panel_dir / "current.json").write_text(
                json.dumps(
                    {
                        "artifact_type": "data_source_panel/current.json",
                        "version": 1,
                        "generated_by": "prepare-data-source-panel",
                        "status": "needs_user_answer",
                        "current_mode": "local_files",
                        "recommended_option_id": "not_a_real_option",
                        "options": [{"option_id": "local_files", "label": "x", "description": "y"}],
                    }
                ),
                encoding="utf-8",
            )
            validator = WorkspaceArtifactValidator(root, "workspaces/demo")
            validator._validate_data_source_panel()
            self.assertTrue(validator.result.errors)


if __name__ == "__main__":
    unittest.main()
