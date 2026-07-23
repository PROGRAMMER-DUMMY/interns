"""WorkspaceLayout.databricks_source_mode() -- the single source of truth
every other module reads to decide whether local dataset discovery runs,
runs additively alongside Unity Catalog profiling, or is skipped entirely.
"""
from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from core.storage.workspace_layout import WorkspaceLayout


class DatabricksSourceModeTests(unittest.TestCase):
    def _layout(self, root: Path, settings: dict | None) -> WorkspaceLayout:
        workspace = root / "workspaces" / "demo"
        workspace.mkdir(parents=True, exist_ok=True)
        if settings is not None:
            (workspace / "workspace_settings.json").write_text(
                json.dumps(settings), encoding="utf-8"
            )
        return WorkspaceLayout(project_root=workspace)

    def test_no_databricks_source_key_is_local_files(self):
        with tempfile.TemporaryDirectory() as tmp:
            layout = self._layout(Path(tmp), None)
            self.assertEqual(layout.databricks_source_mode(), "local_files")

    def test_settings_present_without_databricks_source_key_is_local_files(self):
        with tempfile.TemporaryDirectory() as tmp:
            layout = self._layout(Path(tmp), {"dataset_allowlist": ["datasets/foo"]})
            self.assertEqual(layout.databricks_source_mode(), "local_files")

    def test_declared_without_mode_defaults_to_additive(self):
        """Today's original silent-merge behavior, preserved until a human
        answers the data_source_panel to make the choice explicit."""
        with tempfile.TemporaryDirectory() as tmp:
            layout = self._layout(
                Path(tmp),
                {"databricks_source": {"catalog": "main", "schema": "bronze"}},
            )
            self.assertEqual(layout.databricks_source_mode(), "additive")

    def test_explicit_exclusive_mode_is_honored(self):
        with tempfile.TemporaryDirectory() as tmp:
            layout = self._layout(
                Path(tmp),
                {
                    "databricks_source": {
                        "catalog": "main",
                        "schema": "bronze",
                        "mode": "exclusive",
                    }
                },
            )
            self.assertEqual(layout.databricks_source_mode(), "exclusive")

    def test_explicit_local_files_mode_is_honored(self):
        with tempfile.TemporaryDirectory() as tmp:
            layout = self._layout(
                Path(tmp),
                {
                    "databricks_source": {
                        "catalog": "main",
                        "schema": "bronze",
                        "mode": "local_files",
                    }
                },
            )
            self.assertEqual(layout.databricks_source_mode(), "local_files")

    def test_unrecognized_mode_falls_back_to_additive(self):
        with tempfile.TemporaryDirectory() as tmp:
            layout = self._layout(
                Path(tmp),
                {"databricks_source": {"catalog": "main", "schema": "bronze", "mode": "bogus"}},
            )
            self.assertEqual(layout.databricks_source_mode(), "additive")


if __name__ == "__main__":
    unittest.main()
