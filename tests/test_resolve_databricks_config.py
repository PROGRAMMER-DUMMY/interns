"""core.config.resolve_databricks_config: the generic per-enterprise config
resolution seam every Databricks call site should use instead of load()
directly, so a second enterprise's credentials/catalog is a real
implementation (config/enterprises/<id>/lock.toml), not a retrofit of every
call site reading the single global config/lock.toml.
"""
from __future__ import annotations

import unittest
from pathlib import Path
from unittest import mock

from core.config import resolve_databricks_config
from core.storage.workspace_layout import WorkspaceLayout


class ResolveDatabricksConfigTests(unittest.TestCase):
    def test_empty_enterprise_id_falls_back_to_global_config(self):
        with mock.patch("core.config.load") as fake_load:
            fake_load.return_value.databricks = "GLOBAL_SENTINEL"
            result = resolve_databricks_config("")
        self.assertEqual(result, "GLOBAL_SENTINEL")
        fake_load.assert_called_once()

    def test_unknown_enterprise_id_falls_back_to_global_config(self):
        with mock.patch("core.config.load") as fake_load:
            fake_load.return_value.databricks = "GLOBAL_SENTINEL"
            result = resolve_databricks_config("no_such_enterprise_xyz")
        self.assertEqual(result, "GLOBAL_SENTINEL")

    def test_real_override_file_is_used_when_present(self):
        import core.config as config_module

        override_dir = config_module.ROOT / "config" / "enterprises" / "_test_acme_corp"
        override_path = override_dir / "lock.toml"
        override_dir.mkdir(parents=True, exist_ok=True)
        try:
            override_path.write_text(
                "[databricks]\n"
                "enabled = true\n"
                "execution = \"warehouse\"\n"
                "catalog = \"acme_catalog\"\n"
                "schema = \"acme_schema\"\n",
                encoding="utf-8",
            )
            result = resolve_databricks_config("_test_acme_corp")
            self.assertTrue(result.enabled)
            self.assertEqual(result.catalog, "acme_catalog")
            self.assertEqual(result.schema, "acme_schema")
        finally:
            override_path.unlink(missing_ok=True)
            try:
                override_dir.rmdir()
            except OSError:
                pass


class WorkspaceLayoutEnterpriseIdTests(unittest.TestCase):
    def _layout(self, root: Path, settings: dict | None) -> WorkspaceLayout:
        import json
        import tempfile

        workspace = root / "workspaces" / "demo"
        workspace.mkdir(parents=True, exist_ok=True)
        if settings is not None:
            (workspace / "workspace_settings.json").write_text(
                json.dumps(settings), encoding="utf-8"
            )
        return WorkspaceLayout(project_root=workspace)

    def test_no_databricks_source_is_empty(self):
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            layout = self._layout(Path(tmp), None)
            self.assertEqual(layout.enterprise_id(), "")

    def test_explicit_enterprise_id_wins(self):
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            layout = self._layout(
                Path(tmp),
                {
                    "databricks_source": {
                        "catalog": "healthcare_rcm",
                        "schema": "bronze",
                        "enterprise_id": "acme_corp",
                    }
                },
            )
            self.assertEqual(layout.enterprise_id(), "acme_corp")

    def test_falls_back_to_catalog_when_no_explicit_enterprise_id(self):
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            layout = self._layout(
                Path(tmp),
                {"databricks_source": {"catalog": "healthcare_rcm", "schema": "bronze"}},
            )
            self.assertEqual(layout.enterprise_id(), "healthcare_rcm")


if __name__ == "__main__":
    unittest.main()
