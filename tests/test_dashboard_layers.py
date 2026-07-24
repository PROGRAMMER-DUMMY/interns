"""core.dashboard.model.layers: gold read dispatches on databricks_source_mode()
(local Delta vs. dbt-built Databricks mart), gated by the same
AUTORESEARCH_ALLOW_REMOTE_EXECUTION approval every other Databricks execution
path requires, plus the gold_source_status() staleness signal.

DatabricksClient calls are mocked -- this suite covers gating/dispatch/parsing
logic, not a live Databricks read (that's a separate real-infrastructure
verification step, same split established for the dbt project generator and
Cosmos wiring this session).
"""
from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from core.dashboard.model import layers
from core.storage.workspace_layout import WorkspaceLayout


def _workspace(root: Path, *, catalog: str = "", gold_schema: str = "gold",
               mode: str = "exclusive", with_dbt_project: bool = True) -> WorkspaceLayout:
    ws = root / "workspaces" / "demo"
    ws.mkdir(parents=True)
    settings: dict = {}
    if catalog:
        settings["databricks_source"] = {"catalog": catalog, "schema": "bronze", "mode": mode}
    (ws / "workspace_settings.json").write_text(json.dumps(settings), encoding="utf-8")
    if with_dbt_project and catalog:
        dbt_dir = ws / "dbt"
        dbt_dir.mkdir()
        (dbt_dir / "dbt_project.yml").write_text(
            "name: 'dbx_demo'\nprofile: 'dbx_demo'\nversion: '1.0.0'\n"
            "models:\n  dbx_demo:\n    staging:\n      +schema: silver\n"
            f"    marts:\n      +schema: {gold_schema}\n",
            encoding="utf-8",
        )
    return WorkspaceLayout(project_root=ws)


class GateTests(unittest.TestCase):
    def test_no_env_var_refuses_regardless_of_everything_else(self):
        with tempfile.TemporaryDirectory() as tmp:
            layout = _workspace(Path(tmp), catalog="main")
            with patch.dict(os.environ, {}, clear=False):
                os.environ.pop("AUTORESEARCH_ALLOW_REMOTE_EXECUTION", None)
                self.assertIsNone(layers._databricks_read_context(layout))

    def test_no_catalog_declared_refuses(self):
        with tempfile.TemporaryDirectory() as tmp:
            layout = _workspace(Path(tmp), catalog="")
            with patch.dict(os.environ, {"AUTORESEARCH_ALLOW_REMOTE_EXECUTION": "1"}):
                self.assertIsNone(layers._databricks_read_context(layout))

    def test_no_dbt_project_yet_refuses(self):
        with tempfile.TemporaryDirectory() as tmp:
            layout = _workspace(Path(tmp), catalog="main", with_dbt_project=False)
            with patch.dict(os.environ, {"AUTORESEARCH_ALLOW_REMOTE_EXECUTION": "1"}):
                self.assertIsNone(layers._databricks_read_context(layout))

    def test_gate_open_catalog_and_project_present_but_config_inactive_refuses(self):
        with tempfile.TemporaryDirectory() as tmp:
            layout = _workspace(Path(tmp), catalog="main")
            with patch.dict(os.environ, {"AUTORESEARCH_ALLOW_REMOTE_EXECUTION": "1"}):
                with patch("core.config.resolve_databricks_config") as mock_resolve:
                    mock_resolve.return_value = MagicMock(is_active=lambda: False)
                    self.assertIsNone(layers._databricks_read_context(layout))


class GoldSchemaParsingTests(unittest.TestCase):
    def test_reads_custom_gold_schema(self):
        with tempfile.TemporaryDirectory() as tmp:
            layout = _workspace(Path(tmp), catalog="main", gold_schema="gold_custom")
            self.assertEqual(layers._read_dbt_gold_schema(layout), "gold_custom")

    def test_missing_project_yml_returns_none(self):
        with tempfile.TemporaryDirectory() as tmp:
            layout = _workspace(Path(tmp), catalog="main", with_dbt_project=False)
            self.assertIsNone(layers._read_dbt_gold_schema(layout))


class DispatchByModeTests(unittest.TestCase):
    def test_local_mode_never_touches_databricks_helpers(self):
        with tempfile.TemporaryDirectory() as tmp:
            layout = _workspace(Path(tmp), catalog="")  # mode defaults local_files
            self.assertEqual(layout.databricks_source_mode(), "local_files")
            with patch("core.dashboard.model.layers._list_databricks_gold_kpis") as mock_list:
                self.assertEqual(layers.list_gold_kpis(layout), [])
                mock_list.assert_not_called()
            with patch("core.dashboard.model.layers._read_databricks_gold") as mock_read:
                self.assertIsNone(layers.read_gold(layout, "kpi_001"))
                mock_read.assert_not_called()

    def test_exclusive_mode_dispatches_to_databricks_helpers(self):
        with tempfile.TemporaryDirectory() as tmp:
            layout = _workspace(Path(tmp), catalog="main", mode="exclusive")
            with patch("core.dashboard.model.layers._list_databricks_gold_kpis", return_value=["kpi_001"]) as mock_list:
                self.assertEqual(layers.list_gold_kpis(layout), ["kpi_001"])
                mock_list.assert_called_once()
            with patch("core.dashboard.model.layers._read_databricks_gold", return_value=None) as mock_read:
                layers.read_gold(layout, "kpi_001")
                mock_read.assert_called_once()


class DatabricksQueryTests(unittest.TestCase):
    def _patched_context(self, layout, mock_client):
        return patch(
            "core.dashboard.model.layers._databricks_read_context",
            return_value=("main", "gold", mock_client),
        )

    def test_list_filters_to_fct_prefixed_tables(self):
        with tempfile.TemporaryDirectory() as tmp:
            layout = _workspace(Path(tmp), catalog="main")
            mock_client = MagicMock()
            mock_client.execute_query.return_value = (
                ["database", "tableName", "isTemporary"],
                [["gold", "fct_kpi_001", False], ["gold", "fct_kpi_002", False], ["gold", "_data_quality", False]],
            )
            with self._patched_context(layout, mock_client):
                self.assertEqual(layers.list_gold_kpis(layout), ["kpi_001", "kpi_002"])

    def test_read_gold_builds_a_dataframe_from_the_query_result(self):
        with tempfile.TemporaryDirectory() as tmp:
            layout = _workspace(Path(tmp), catalog="main")
            mock_client = MagicMock()
            mock_client.execute_query.return_value = (
                ["lineofbusiness", "sum_paidamount"],
                [["Commercial", 10.5], ["Medicare", 20.25]],
            )
            with self._patched_context(layout, mock_client):
                df = layers.read_gold(layout, "kpi_001")
            self.assertIsNotNone(df)
            self.assertEqual(df.columns, ["lineofbusiness", "sum_paidamount"])
            self.assertEqual(df.height, 2)
            called_sql = mock_client.execute_query.call_args[0][0]
            self.assertIn("`main`.`gold`.`fct_kpi_001`", called_sql)

    def test_query_failure_returns_none_not_raise(self):
        with tempfile.TemporaryDirectory() as tmp:
            layout = _workspace(Path(tmp), catalog="main")
            mock_client = MagicMock()
            mock_client.execute_query.side_effect = RuntimeError("boom")
            with self._patched_context(layout, mock_client):
                self.assertIsNone(layers.read_gold(layout, "kpi_001"))


class GoldSourceStatusTests(unittest.TestCase):
    def test_local_mode_with_no_gold_dir_is_unavailable(self):
        with tempfile.TemporaryDirectory() as tmp:
            layout = _workspace(Path(tmp), catalog="")
            status = layers.gold_source_status(layout)
            self.assertEqual(status.mode, "local_files")
            self.assertEqual(status.source, "unavailable")
            self.assertIsNone(status.as_of)
            self.assertTrue(status.remote_read_approved)  # local reads need no approval

    def test_local_mode_reports_as_of_from_delta_log_mtime(self):
        with tempfile.TemporaryDirectory() as tmp:
            layout = _workspace(Path(tmp), catalog="")
            log_dir = layout.gold_dir / "kpi_001_results" / "_delta_log"
            log_dir.mkdir(parents=True)
            (log_dir / "00000000000000000000.json").write_text("{}", encoding="utf-8")
            status = layers.gold_source_status(layout)
            self.assertEqual(status.source, "local_delta")
            self.assertIsNotNone(status.as_of)

    def test_databricks_mode_gate_closed_is_unavailable_not_silently_local(self):
        with tempfile.TemporaryDirectory() as tmp:
            layout = _workspace(Path(tmp), catalog="main")
            with patch.dict(os.environ, {}, clear=False):
                os.environ.pop("AUTORESEARCH_ALLOW_REMOTE_EXECUTION", None)
                status = layers.gold_source_status(layout)
            self.assertEqual(status.mode, "exclusive")
            self.assertEqual(status.source, "unavailable")
            self.assertFalse(status.remote_read_approved)
            self.assertIsNone(status.as_of)

    def test_databricks_mode_reports_as_of_from_describe_history(self):
        with tempfile.TemporaryDirectory() as tmp:
            layout = _workspace(Path(tmp), catalog="main")
            mock_client = MagicMock()
            mock_client.execute_query.side_effect = [
                (["database", "tableName", "isTemporary"], [["gold", "fct_kpi_001", False]]),
                # `version` before `timestamp` -- DESCRIBE HISTORY's real
                # column order (found live), not the earlier assumption that
                # position 0 was the timestamp (that silently returned the
                # version number "1" as a fake as_of value).
                (["version", "timestamp", "operation"],
                 [["1", "2026-07-24T02:09:59Z", "CREATE OR REPLACE TABLE AS SELECT"]]),
            ]
            with patch(
                "core.dashboard.model.layers._databricks_read_context",
                return_value=("main", "gold", mock_client),
            ):
                with patch.dict(os.environ, {"AUTORESEARCH_ALLOW_REMOTE_EXECUTION": "1"}):
                    status = layers.gold_source_status(layout)
            self.assertEqual(status.source, "databricks_dbt_mart")
            self.assertTrue(status.remote_read_approved)
            self.assertEqual(status.as_of, "2026-07-24T02:09:59Z")


if __name__ == "__main__":
    unittest.main()
