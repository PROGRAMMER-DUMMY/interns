from __future__ import annotations

import contextlib
import io
import json
import tempfile
import unittest
from pathlib import Path

from core.onboarding.workspace.flow import (
    WorkspaceFlow,
    _build_kpi_resolution_review,
    _compact_panel,
    main as workspace_flow_main,
    _render_panel_markdown,
    _result_view,
)


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

    def test_kpi_blocker_panel_keeps_full_resolution_review_visible(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            workspace = root / "workspaces" / "demo"
            contracts = workspace / "interns" / "generated" / "contracts"
            contracts.mkdir(parents=True)
            (contracts / "kpi_feature_mapping.json").write_text(
                json.dumps(
                    {
                        "kpis": [
                            {
                                "kpi_id": "kpi_001",
                                "name": (
                                    "What is trend for amount paid for medicare LOB across gender "
                                    "and payer for patients above 50 years of age"
                                ),
                                "source": "workspaces/demo/docs/Sample KPI.xlsx",
                                "metric": "sum(PaidAmount)",
                                "cuts": (
                                    "Month (ServiceDate), LineOfBusiness, PayorID, Gender, "
                                    "Age(DOB), LOB = Medicare, Age > 50"
                                ),
                                "status": "ready_for_sql",
                                "features": [
                                    {
                                        "feature": "PaidAmount",
                                        "state": "proven_direct",
                                        "source_columns": [
                                            {
                                                "dataset": "workspaces/demo/datasets/transactions.csv",
                                                "column": "PaidAmount",
                                            }
                                        ],
                                    },
                                    {
                                        "feature": "Age",
                                        "state": "proven_formula",
                                        "source_columns": [
                                            {
                                                "dataset": "workspaces/demo/datasets/patients.csv",
                                                "column": "DOB",
                                            }
                                        ],
                                    },
                                ],
                            }
                        ]
                    }
                ),
                encoding="utf-8",
            )

            review = _build_kpi_resolution_review(root, "workspaces/demo")
            panel = _compact_panel(
                stage="kpi_blocker",
                status="needs_user_answer",
                source_panel={
                    "question": "Approve KPI 1 resolution?",
                    "options": [{"option_id": "option_a", "label": "Approve mapping"}],
                    "recommended_option_id": "option_a",
                    "output_dialect": {
                        "label": "SQL (default)",
                        "alternatives": ["polars", "pyspark"],
                        "rule": "Render SQL by default.",
                    },
                    "immutable_kpi_policy": {
                        "rule": "The KPI from the source workbook or registry is hard truth and must not be rewritten.",
                    },
                    "kpi_understanding": [
                        {
                            "kpi_id": "kpi_001",
                            "original_kpi": {
                                "business_question": "What is trend for amount paid for medicare LOB?",
                                "metric": "sum(PaidAmount)",
                                "cuts": ["Month (ServiceDate)", "LOB = Medicare"],
                            },
                            "my_understanding": "Answer the KPI exactly as written.",
                            "strict_proven_sql": "SELECT \"PaidAmount\" FROM \"transactions\" LIMIT 20;",
                            "intent_sql_sketch": "-- NON-EXECUTABLE INTENT SKETCH\nSELECT sum(PaidAmount) FROM <SOURCE_TABLE>;",
                            "demo_result_table": "| metric_value |\n| --- |\n| <computed> |",
                        }
                    ],
                },
                instruction="Review the full KPI resolution before answering.",
                artifact_paths=["workspaces/demo/interns/reports/blocker_question_panel/current.json"],
                resolution_review=review,
            )
            markdown = _render_panel_markdown(panel)

            self.assertIn("KPI Resolution Review", markdown)
            self.assertIn("What is trend for amount paid for medicare LOB", markdown)
            self.assertIn("sum(PaidAmount)", markdown)
            self.assertIn("Month (ServiceDate)", markdown)
            self.assertIn("LOB = Medicare", markdown)
            self.assertIn("Age > 50", markdown)
            self.assertIn("Resolved Source Mapping", markdown)
            self.assertIn("## Output Dialect", markdown)
            self.assertIn("SQL (default)", markdown)
            self.assertIn("## Immutable KPI Policy", markdown)
            self.assertIn("## KPI Understanding Review", markdown)
            self.assertIn("#### My Understanding", markdown)
            self.assertIn("#### Strict Proven SQL", markdown)
            self.assertIn("#### Placeholder Intent SQL", markdown)
            self.assertIn("Demo Result Table", markdown)
            self.assertTrue(panel["hidden_panel_harness"]["hidden"])
            self.assertTrue(panel["hidden_panel_harness"]["passed"])

    def test_workspace_flow_cli_prints_panel_markdown_not_only_summary_json(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            workspace = root / "workspaces" / "demo"
            workspace.mkdir(parents=True)
            (workspace / "encounters.csv").write_text("Id,START\nE1,2024-01-01\n", encoding="utf-8")
            (workspace / "hospital_analytics_questions.sql").write_text(
                "-- How many encounters occurred each year?\n",
                encoding="utf-8",
            )
            stdout = io.StringIO()

            with contextlib.redirect_stdout(stdout):
                exit_code = workspace_flow_main(
                    [
                        "--repo-root",
                        str(root),
                        "start",
                        "--workspace",
                        "workspaces/demo",
                        "--intent",
                        "kpi_generation",
                    ]
                )

            output = stdout.getvalue()
            self.assertEqual(exit_code, 0)
            self.assertIn("# Workspace Flow: kpi_generation_route", output)
            self.assertIn("## Question", output)
            self.assertIn("## Options", output)
            self.assertIn("## Next Step", output)
            self.assertNotEqual(output.lstrip()[0], "{")

    def test_plan_mode_returns_three_orchestration_choices(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            workspace = root / "workspaces" / "demo"
            workspace.mkdir(parents=True)
            (workspace / "encounters.csv").write_text("Id,START\nE1,2024-01-01\n", encoding="utf-8")

            result = WorkspaceFlow(root, "workspaces/demo").start(intent="full_kpi_sql", mode="plan")

            self.assertEqual(result.stage, "workflow_checkpoint")
            self.assertEqual(result.status, "needs_user_choice")
            panel = json.loads((root / result.current_panel_path).read_text(encoding="utf-8"))
            self.assertEqual(panel["source"], "workflow_checkpoint")
            self.assertEqual([option["option_id"] for option in panel["options"]], ["option_a", "option_b", "option_c"])

    def test_data_quality_gate_runs_before_kpi_blocker_and_records_duplicate_decision(self):
        try:
            import duckdb  # noqa: F401
            import polars  # noqa: F401
        except ImportError:
            self.skipTest("duckdb or polars is not installed")

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            workspace = root / "workspaces" / "demo"
            (workspace / "datasets").mkdir(parents=True)
            (workspace / "docs").mkdir()
            (workspace / "datasets" / "transactions.csv").write_text(
                "TransactionID,PatientID,ServiceDate,PayorID,PaidAmount,ModifiedDate\n"
                "T1,P1,2024-01-01,PAY1,10.00,2024-01-03\n"
                "T1,P1,2024-01-01,PAY1,10.00,2024-01-04\n"
                "T2,P2,2024-01-02,PAY2,20.00,2024-01-05\n",
                encoding="utf-8",
            )
            (workspace / "docs" / "kpi_registry.csv").write_text(
                "Key business question,Description,Cuts / grain hints,Metric,Data model refinement required\n"
                "What is paid amount?,Baseline KPI,PayorID,sum(PaidAmount),Confirm source\n",
                encoding="utf-8",
            )
            (workspace / "docs" / "data_model.md").write_text(
                "# Data Model\n\ntransactions has TransactionID, PatientID, ServiceDate, PayorID, PaidAmount.\n",
                encoding="utf-8",
            )

            result = WorkspaceFlow(root, "workspaces/demo").start(intent="full_kpi_sql")

            self.assertEqual(result.stage, "data_quality_duplicate_review")
            panel = json.loads((root / result.current_panel_path).read_text(encoding="utf-8"))
            self.assertEqual(panel["source"], "duplicate_review")
            self.assertEqual(panel["recommended_option_id"], "option_a")
            self.assertIn("orchestration_context", panel)
            self.assertEqual(panel["orchestration_context"]["layer_route"]["selected_track"], "medallion")

            WorkspaceFlow.from_session(root, result.session_id).answer(answer="option_a")

            decisions = json.loads(
                (workspace / "interns" / "generated" / "contracts" / "duplicate_decisions.json").read_text(
                    encoding="utf-8"
                )
            )
            self.assertEqual(decisions["decisions"][0]["action"], "preserve")

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
