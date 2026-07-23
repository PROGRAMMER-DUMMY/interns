"""workspace_settings.json's "databricks_source" declaration -- generic,
additive table discovery + profiling via the SQL warehouse. Must never
hardcode any workspace/catalog/table name in core/ -- every value comes
from the declaration or from Unity Catalog itself (SHOW TABLES).
"""
from __future__ import annotations

import json
import re
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from core.onboarding.workspace.onboarding import WorkspaceOnboarder


def _write_json(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload), encoding="utf-8")


def _fixture_workspace(tmp: str, settings: dict | None = None) -> tuple[Path, Path]:
    repo = Path(tmp)
    ws = repo / "workspaces" / "demo"
    ws.mkdir(parents=True, exist_ok=True)
    if settings is not None:
        _write_json(ws / "workspace_settings.json", settings)
    return repo, ws


class DatabricksSourceTablesTests(unittest.TestCase):
    def test_no_declaration_returns_empty_list(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo, ws = _fixture_workspace(tmp, settings={})
            onboarder = WorkspaceOnboarder(repo, ws)
            self.assertEqual(onboarder._databricks_source_tables(), [])

    def test_declaration_present_but_databricks_not_configured_returns_empty(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo, ws = _fixture_workspace(
                tmp, settings={"databricks_source": {"catalog": "any_catalog", "schema": "any_schema"}}
            )
            onboarder = WorkspaceOnboarder(repo, ws)
            with mock.patch("core.execution.databricks_client.DatabricksClient.is_configured", return_value=False):
                self.assertEqual(onboarder._databricks_source_tables(), [])

    def test_generic_discovery_via_show_tables_no_hardcoded_names(self):
        # Deliberately unrelated catalog/schema/table names -- proves nothing
        # in core/ assumes any specific workspace's naming.
        with tempfile.TemporaryDirectory() as tmp:
            repo, ws = _fixture_workspace(
                tmp, settings={"databricks_source": {"catalog": "zzz_widgets", "schema": "raw_layer"}}
            )
            onboarder = WorkspaceOnboarder(repo, ws)
            with mock.patch(
                "core.execution.databricks_client.DatabricksClient.is_configured", return_value=True
            ), mock.patch(
                "core.execution.databricks_client.DatabricksClient.execute_query"
            ) as fake_query:
                fake_query.return_value = (
                    ["database", "tableName", "isTemporary"],
                    [["raw_layer", "widget_orders", False], ["raw_layer", "widget_returns", False]],
                )
                tables = onboarder._databricks_source_tables()
            self.assertEqual(
                tables, ["zzz_widgets.raw_layer.widget_orders", "zzz_widgets.raw_layer.widget_returns"]
            )
            fake_query.assert_called_once_with("SHOW TABLES IN `zzz_widgets`.`raw_layer`")

    def test_table_discovery_failure_does_not_raise(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo, ws = _fixture_workspace(
                tmp, settings={"databricks_source": {"catalog": "c", "schema": "s"}}
            )
            onboarder = WorkspaceOnboarder(repo, ws)
            with mock.patch(
                "core.execution.databricks_client.DatabricksClient.is_configured", return_value=True
            ), mock.patch(
                "core.execution.databricks_client.DatabricksClient.execute_query",
                side_effect=RuntimeError("warehouse unreachable"),
            ):
                self.assertEqual(onboarder._databricks_source_tables(), [])


class ProfileDatabricksTablesTests(unittest.TestCase):
    def test_writes_profile_and_returns_summary_matching_local_shape(self):
        from core.profiling.data_model_profiler import ColumnProfile, DatasetProfile

        with tempfile.TemporaryDirectory() as tmp:
            repo, ws = _fixture_workspace(tmp, settings={})
            onboarder = WorkspaceOnboarder(repo, ws)
            fake_profile = DatasetProfile(
                path="`c`.`s`.`t`", format="delta", row_count=42, file_count=1, size_bytes=0,
                schema={"A": "string"}, columns=[ColumnProfile(name="A", dtype="string")],
                downcast_recommendations=[], sources_used=["sql_warehouse_sample"], warnings=[],
            )
            with mock.patch(
                "core.profiling.databricks_table_profiler.profile_uc_table", return_value=fake_profile
            ):
                profiles, warnings = onboarder.profile_databricks_tables(["c.s.t"])

            self.assertEqual(warnings, [])
            self.assertEqual(len(profiles), 1)
            summary = profiles[0]
            self.assertEqual(summary["row_count"], 42)
            self.assertEqual(summary["resource_mode"], "databricks_sql_warehouse")
            profile_file = repo / summary["profile_path"]
            self.assertTrue(profile_file.exists())

    def test_malformed_fqn_warns_without_raising(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo, ws = _fixture_workspace(tmp, settings={})
            onboarder = WorkspaceOnboarder(repo, ws)
            profiles, warnings = onboarder.profile_databricks_tables(["not_a_valid_fqn"])
            self.assertEqual(profiles, [])
            self.assertTrue(any("malformed_databricks_table_fqn" in w for w in warnings))

    def test_empty_table_list_is_a_no_op(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo, ws = _fixture_workspace(tmp, settings={})
            onboarder = WorkspaceOnboarder(repo, ws)
            profiles, warnings = onboarder.profile_databricks_tables([])
            self.assertEqual((profiles, warnings), ([], []))


class ExclusiveModeOnboardingTests(unittest.TestCase):
    """Phase B: databricks_source.mode="exclusive" skips local dataset
    discovery entirely and hard-warns instead of silently proceeding on an
    empty profile index when zero UC tables are actually discovered.
    """

    def _fixture_with_local_csv(self, tmp: str, mode: str) -> tuple[Path, Path]:
        repo, ws = _fixture_workspace(
            tmp,
            settings={
                "databricks_source": {"catalog": "main", "schema": "bronze", "mode": mode}
            },
        )
        (ws / "datasets").mkdir(parents=True, exist_ok=True)
        (ws / "datasets" / "orders.csv").write_text(
            "OrderID,Amount\n1,10.0\n2,20.0\n", encoding="utf-8"
        )
        (ws / "docs").mkdir(parents=True, exist_ok=True)
        (ws / "docs" / "kpi_registry.csv").write_text(
            "Key business question,Description,Cuts / grain hints,Metric,Data model refinement required\n"
            "What is total amount?,Baseline KPI,,sum(Amount),\n",
            encoding="utf-8",
        )
        return repo, ws

    def test_exclusive_mode_skips_local_data_files(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo, ws = self._fixture_with_local_csv(tmp, "exclusive")
            onboarder = WorkspaceOnboarder(repo, ws, sample_rows=10)
            with mock.patch.object(
                WorkspaceOnboarder, "_databricks_source_tables", return_value=["main.bronze.orders"]
            ), mock.patch.object(
                WorkspaceOnboarder, "profile_databricks_tables",
                return_value=([{"format": "delta", "path": "`main`.`bronze`.`orders`",
                                 "schema": {"OrderID": "int", "Amount": "double"},
                                 "profile_path": "x", "row_count": 2}], []),
            ):
                result = onboarder.run()
            self.assertEqual(result.inputs.data_files, [])
            self.assertEqual(result.inputs.databricks_source_mode, "exclusive")
            self.assertEqual(result.inputs.databricks_tables, ["main.bronze.orders"])
            self.assertEqual(result.profile_count, 1)
            # Stale local file present but not scanned -- non-blocking warning.
            self.assertTrue(
                any("exclusive_databricks_mode_stale_local_files_present" in w for w in result.warnings)
            )
            # Not the hard zero-tables warning -- tables WERE discovered.
            self.assertFalse(
                any("exclusive_databricks_mode_zero_tables_discovered" in w for w in result.warnings)
            )

    def test_exclusive_mode_zero_tables_hard_warns_and_redirects_next_step(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo, ws = _fixture_workspace(
                tmp,
                settings={
                    "databricks_source": {"catalog": "main", "schema": "bronze", "mode": "exclusive"}
                },
            )
            (ws / "docs").mkdir(parents=True, exist_ok=True)
            (ws / "docs" / "kpi_registry.csv").write_text(
                "Key business question,Description,Cuts / grain hints,Metric,Data model refinement required\n"
                "What is total amount?,Baseline KPI,,sum(Amount),\n",
                encoding="utf-8",
            )
            onboarder = WorkspaceOnboarder(repo, ws, sample_rows=10)
            with mock.patch.object(WorkspaceOnboarder, "_databricks_source_tables", return_value=[]):
                result = onboarder.run()
            self.assertEqual(result.inputs.data_files, [])
            self.assertEqual(result.profile_count, 0)
            self.assertTrue(
                any("exclusive_databricks_mode_zero_tables_discovered" in w for w in result.warnings)
            )
            self.assertIn("apply-data-source-answer", result.next_command)
            self.assertIn("databricks_exclusive", result.next_command)

    def test_additive_mode_still_scans_local_files(self):
        """Sanity check: only "exclusive" changes local discovery -- "additive"
        (today's original silent-merge behavior) is untouched."""
        with tempfile.TemporaryDirectory() as tmp:
            repo, ws = self._fixture_with_local_csv(tmp, "additive")
            onboarder = WorkspaceOnboarder(repo, ws, sample_rows=10)
            with mock.patch.object(WorkspaceOnboarder, "_databricks_source_tables", return_value=[]):
                result = onboarder.run()
            # additive mode must still discover local files -- unlike exclusive
            # mode's test above, which asserts data_files == [] outright.
            self.assertIn("workspaces/demo/datasets/orders.csv", result.inputs.data_files)
            self.assertEqual(result.inputs.databricks_source_mode, "additive")
            self.assertFalse(
                any("exclusive_databricks_mode" in w for w in result.warnings)
            )


class GenericityGuardTest(unittest.TestCase):
    def test_new_databricks_source_code_has_no_hardcoded_workspace_vocabulary(self):
        """Regression guard: the databricks_source discovery/profiling code in
        onboarding.py must never hardcode a specific workspace's catalog,
        schema, or domain vocabulary (e.g. "healthcare_rcm", "bronze" as a
        literal default, "rcm", "hospital"). Every real value must come from
        workspace_settings.json or from Unity Catalog itself.
        """
        text = Path("core/onboarding/workspace/onboarding.py").read_text(encoding="utf-8")
        start = text.index("_databricks_source_tables")
        end = text.index("def profile_databricks_tables") + len(text[text.index("def profile_databricks_tables"):].split("\n\n\n")[0])
        region = text[start:end]
        banned = re.compile(r"healthcare|\brcm\b|hospital", re.IGNORECASE)
        match = banned.search(region)
        self.assertIsNone(match, f"found workspace-specific vocabulary in generic code: {match}")


if __name__ == "__main__":
    unittest.main()
