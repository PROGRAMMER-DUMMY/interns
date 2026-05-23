from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from core.onboarding.workspace.flow import WorkspaceFlow, _result_view


class WorkspaceFlowTests(unittest.TestCase):
    def test_start_kpi_generation_creates_compact_session_panel(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            workspace = root / "workspaces" / "demo"
            workspace.mkdir(parents=True)
            (workspace / "encounters.csv").write_text("Id,START\nE1,2024-01-01\n", encoding="utf-8")
            (workspace / "hospital_analytics_questions.sql").write_text(
                "-- How many encounters occurred each year?\n",
                encoding="utf-8",
            )

            result = WorkspaceFlow(root, "workspaces/demo").start(intent="kpi_generation")

            self.assertEqual(result.status, "needs_user_answer")
            self.assertEqual(result.stage, "kpi_generation_route")
            self.assertTrue((root / result.state_path).exists())
            panel = json.loads((root / result.current_panel_path).read_text(encoding="utf-8"))
            self.assertEqual(panel["source"], "kpi_generation")
            self.assertIn("instruction", panel)
            self.assertTrue(panel["options"])

            reloaded = WorkspaceFlow.from_session(root, result.session_id).status()
            self.assertEqual(reloaded.session_id, result.session_id)
            self.assertEqual(reloaded.current_panel_path, result.current_panel_path)

    def test_full_kpi_sql_flow_runs_quiet_backend_and_writes_results(self):
        try:
            import duckdb  # noqa: F401
            import polars  # noqa: F401
        except ImportError:
            self.skipTest("duckdb or polars is not installed")

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            workspace = root / "workspaces" / "demo"
            (workspace / "docs").mkdir(parents=True)
            (workspace / "datasets").mkdir()
            (workspace / "datasets" / "encounters.csv").write_text(
                "Id,START,ENCOUNTERCLASS\nE1,2024-01-01,ambulatory\nE2,2024-01-02,inpatient\n",
                encoding="utf-8",
            )
            (workspace / "docs" / "kpi_registry.csv").write_text(
                "Key business question,Description,Cuts / grain hints,Metric,Data model refinement required\n"
                "How many encounters are recorded?,Count encounters,,count(Id),\n",
                encoding="utf-8",
            )

            result = WorkspaceFlow(root, "workspaces/demo").start(intent="full_kpi_sql")

            self.assertEqual(result.status, "complete")
            panel = json.loads((root / result.current_panel_path).read_text(encoding="utf-8"))
            self.assertEqual(panel["source"], "complete")
            self.assertEqual(panel["summary"]["generated_kpi_count"], 1)
            result_md = workspace / "interns" / "reports" / "kpi_results" / "current.md"
            self.assertTrue(result_md.exists())
            self.assertIn("kpi_001", result_md.read_text(encoding="utf-8"))
            harness = workspace / "interns" / "generated" / "evidence" / "kpi_execution_harness.json"
            self.assertTrue(harness.exists())
            self.assertTrue(json.loads(harness.read_text(encoding="utf-8"))["ok"])

    def test_result_preview_requires_exact_result_view(self):
        try:
            import duckdb
        except ImportError:
            self.skipTest("duckdb is not installed")

        conn = duckdb.connect(":memory:")
        try:
            conn.execute('CREATE OR REPLACE VIEW "kpi_001_features" AS SELECT 1 AS value')
            self.assertEqual(_result_view(conn, "kpi_001"), "")
            conn.execute('CREATE OR REPLACE VIEW "kpi_001_results" AS SELECT 1 AS value')
            self.assertEqual(_result_view(conn, "kpi_001"), "kpi_001_results")
        finally:
            conn.close()


if __name__ == "__main__":
    unittest.main()
