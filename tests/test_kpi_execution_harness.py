import json
import tempfile
import unittest
from pathlib import Path

from core.onboarding.kpi.execution_harness import KPIExecutionHarness
from core.onboarding.kpi.sql_generator import DuckDBKPISQLGenerator
from core.onboarding.benchmark.agent_benchmark import AgentBenchmarkScorecardBuilder
from core.onboarding.workspace.validation import WorkspaceArtifactValidator


class KPIExecutionHarnessTests(unittest.TestCase):
    def _write_harness_artifact(self, root: Path, payload: dict) -> Path:
        path = root / "workspaces" / "demo" / "interns" / "generated" / "evidence" / "kpi_execution_harness.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(payload), encoding="utf-8")
        return path

    def test_harness_executes_exact_result_view_and_writes_table_sample(self):
        try:
            import duckdb  # noqa: F401
        except ImportError:
            self.skipTest("duckdb is not installed")

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            solutions = root / "workspaces" / "demo" / "interns" / "generated" / "solutions"
            solutions.mkdir(parents=True)
            (solutions / "kpi_001.sql").write_text(
                'CREATE OR REPLACE VIEW "kpi_001_results" AS SELECT 42 AS answer;',
                encoding="utf-8",
            )

            result = KPIExecutionHarness(root, "workspaces/demo", sample_limit=5).run()

            self.assertTrue(result.ok)
            self.assertEqual(result.records[0].result_view, "kpi_001_results")
            self.assertEqual(result.records[0].row_count, 1)
            self.assertIn("| answer |", result.records[0].sample_output_table)
            manifest = json.loads(
                (
                    root
                    / "workspaces"
                    / "demo"
                    / "interns"
                    / "generated"
                    / "evidence"
                    / "kpi_execution_harness.json"
                ).read_text(encoding="utf-8")
            )
            self.assertTrue(manifest["ok"])

    def test_ecommerce_sql_pattern_is_schema_based_not_workspace_name_based(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            workspace = root / "workspaces" / "shop"
            workspace.mkdir(parents=True)
            generator = DuckDBKPISQLGenerator(root, "workspaces/shop")
            profile_map = {
                f"workspaces/shop/{name}.csv": {"format": "csv", "schema": {}}
                for name in (
                    "orders",
                    "order_items",
                    "order_item_refunds",
                    "products",
                    "website_pageviews",
                    "website_sessions",
                )
            }

            sql = generator._workspace_specific_result_sql(
                {"name": "What is the session-to-order conversion rate by traffic source and campaign?"},
                "kpi_001",
                {},
                profile_map,
            )

            self.assertIn('"kpi_001_results"', sql)
            self.assertIn("conversion_rate_pct", sql)
            self.assertIn("workspaces/shop/orders.csv", sql)

    def test_harness_rejects_feature_view_without_final_result_view(self):
        try:
            import duckdb  # noqa: F401
        except ImportError:
            self.skipTest("duckdb is not installed")

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            solutions = root / "workspaces" / "demo" / "interns" / "generated" / "solutions"
            solutions.mkdir(parents=True)
            (solutions / "kpi_001.sql").write_text(
                'CREATE OR REPLACE VIEW "kpi_001_features" AS SELECT 42 AS answer;',
                encoding="utf-8",
            )

            result = KPIExecutionHarness(root, "workspaces/demo").run()

            self.assertFalse(result.ok)
            self.assertIn("kpi_001_results", result.records[0].errors[0])

    def test_harness_rejects_placeholder_ready_marker_result(self):
        try:
            import duckdb  # noqa: F401
        except ImportError:
            self.skipTest("duckdb is not installed")

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            solutions = root / "workspaces" / "demo" / "interns" / "generated" / "solutions"
            solutions.mkdir(parents=True)
            (solutions / "kpi_001.sql").write_text(
                'CREATE OR REPLACE VIEW "kpi_001_results" AS SELECT 1 AS ready_marker;',
                encoding="utf-8",
            )

            result = KPIExecutionHarness(root, "workspaces/demo").run()

            self.assertFalse(result.ok)
            self.assertIn("placeholder", result.records[0].errors[0])

    def test_validator_rejects_generated_sql_without_result_view(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            workspace = root / "workspaces" / "demo"
            contracts = workspace / "interns" / "generated" / "contracts"
            solutions = workspace / "interns" / "generated" / "solutions"
            contracts.mkdir(parents=True)
            solutions.mkdir(parents=True)
            (contracts / "kpi_feature_mapping.json").write_text(
                json.dumps(
                    {
                        "artifact_type": "kpi_feature_mapping.json",
                        "version": 2,
                        "generated_by": "resolve-kpi-features",
                        "workspace": "workspaces/demo",
                        "kpis": [
                            {
                                "kpi_id": "kpi_001",
                                "name": "Demo KPI",
                                "status": "ready_for_sql",
                                "features": [],
                                "open_questions": [],
                            }
                        ],
                        "summary": {
                            "kpi_count": 1,
                            "ready_kpi_count": 1,
                            "blocked_kpi_count": 0,
                            "unresolved_feature_count": 0,
                        },
                        "blocker_clusters": [],
                    }
                ),
                encoding="utf-8",
            )
            (solutions / "kpi_001.sql").write_text(
                'CREATE OR REPLACE VIEW "kpi_001_features" AS SELECT 42 AS answer;',
                encoding="utf-8",
            )

            result = WorkspaceArtifactValidator(root, "workspaces/demo").run()

            self.assertFalse(result.ok)
            self.assertTrue(any("kpi_001_results" in error for error in result.errors))

    def test_benchmark_treats_onboarded_domain_model_as_data_model_ready(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            contracts = root / "workspaces" / "demo" / "interns" / "generated" / "contracts"
            contracts.mkdir(parents=True)
            (contracts / "domain_model.json").write_text(
                json.dumps(
                    {
                        "artifact_type": "domain_model.json",
                        "version": 1,
                        "generated_by": "onboard-workspace",
                        "workspace": "workspaces/demo",
                        "datasets": [{"path": "workspaces/demo/datasets/facts.csv"}],
                        "data_models": [],
                    }
                ),
                encoding="utf-8",
            )

            component = AgentBenchmarkScorecardBuilder(
                root,
                "workspaces/demo",
                domain="healthcare",
            )._data_model_readiness_component()

            self.assertEqual(component["status"], "ready")
            self.assertEqual(component["details"]["state"], "onboarded_domain_model")

    def test_benchmark_recognizes_passing_kpi_execution_harness_artifact(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            artifact_path = self._write_harness_artifact(
                root,
                {
                    "artifact_type": "kpi_execution_harness.json",
                    "version": 1,
                    "generated_by": "run-kpi-execution-harness",
                    "workspace": "workspaces/demo",
                    "ok": True,
                    "kpi_count": 1,
                    "passed_count": 1,
                    "failed_count": 0,
                    "records": [
                        {
                            "kpi_id": "kpi_001",
                            "sql_path": "workspaces/demo/interns/generated/solutions/kpi_001.sql",
                            "status": "passed",
                            "result_view": "kpi_001_results",
                            "row_count": 1,
                            "columns": ["answer"],
                            "sample_output_table": "| answer |\n| --- |\n| 42 |",
                            "errors": [],
                            "warnings": [],
                        }
                    ],
                    "manifest_path": "workspaces/demo/interns/generated/evidence/kpi_execution_harness.json",
                    "report_path": "workspaces/demo/interns/reports/kpi_execution_harness.md",
                },
            )

            component = AgentBenchmarkScorecardBuilder(root, "workspaces/demo")._kpi_execution_harness_component()

            self.assertEqual(component["status"], "ready")
            self.assertEqual(component["score"], 100.0)
            self.assertEqual(component["details"]["state"], "passed")
            self.assertIn(str(artifact_path.relative_to(root)).replace("\\", "/"), component["evidence_paths"])

    def test_benchmark_blocks_missing_or_failed_kpi_execution_harness_artifact(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            builder = AgentBenchmarkScorecardBuilder(root, "workspaces/demo")

            missing_component = builder._kpi_execution_harness_component()
            self.assertEqual(missing_component["status"], "blocked")
            self.assertEqual(missing_component["details"]["state"], "missing")
            self.assertTrue(missing_component["blockers"])

            self._write_harness_artifact(
                root,
                {
                    "artifact_type": "kpi_execution_harness.json",
                    "version": 1,
                    "generated_by": "run-kpi-execution-harness",
                    "workspace": "workspaces/demo",
                    "ok": False,
                    "kpi_count": 1,
                    "passed_count": 0,
                    "failed_count": 1,
                    "records": [
                        {
                            "kpi_id": "kpi_001",
                            "sql_path": "workspaces/demo/interns/generated/solutions/kpi_001.sql",
                            "status": "failed",
                            "result_view": "kpi_001_features",
                            "row_count": None,
                            "columns": [],
                            "sample_output_table": "",
                            "errors": ["final result view `kpi_001_results` was not created"],
                            "warnings": [],
                        }
                    ],
                    "manifest_path": "workspaces/demo/interns/generated/evidence/kpi_execution_harness.json",
                    "report_path": "workspaces/demo/interns/reports/kpi_execution_harness.md",
                },
            )

            failed_component = builder._kpi_execution_harness_component()
            self.assertEqual(failed_component["status"], "blocked")
            self.assertEqual(failed_component["details"]["state"], "failed")
            self.assertIn("KPI execution harness did not pass", failed_component["blockers"])
            self.assertIn("does not target exact result view `kpi_001_results`", " ".join(failed_component["blockers"]))

    def test_validator_rejects_placeholder_harness_columns(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self._write_harness_artifact(
                root,
                {
                    "artifact_type": "kpi_execution_harness.json",
                    "version": 1,
                    "generated_by": "run-kpi-execution-harness",
                    "workspace": "workspaces/demo",
                    "ok": True,
                    "kpi_count": 1,
                    "passed_count": 1,
                    "failed_count": 0,
                    "records": [
                        {
                            "kpi_id": "kpi_001",
                            "sql_path": "workspaces/demo/interns/generated/solutions/kpi_001.sql",
                            "status": "passed",
                            "result_view": "kpi_001_results",
                            "row_count": 1,
                            "columns": ["ready_marker"],
                            "sample_output_table": "| ready_marker |\n| --- |\n| 1 |",
                            "errors": [],
                            "warnings": [],
                        }
                    ],
                },
            )

            result = WorkspaceArtifactValidator(root, "workspaces/demo").run()

            self.assertFalse(result.ok)
            self.assertTrue(any("placeholder result columns" in error for error in result.errors))

    def test_benchmark_release_gates_require_kpi_execution_harness(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            builder = AgentBenchmarkScorecardBuilder(root, "workspaces/demo")
            components = {
                "kpi_readiness": {"status": "ready"},
                "data_model_readiness": {"status": "ready", "score": 100.0},
                "relationship_proof": {"status": "ready"},
                "source_to_target_readiness": {"status": "ready"},
                "validation_status": {"status": "ready"},
                "kpi_execution_harness": {"status": "blocked"},
                "presentation_readiness": {"status": "ready"},
                "wiki_reuse": {"status": "ready"},
                "workflow_checkpoint": {"status": "ready"},
                "autopilot_safety": {"status": "ready"},
            }

            gates = builder._release_gates(components, core_readiness=100.0, product_maturity=100.0)
            executable_gate = next(gate for gate in gates if gate["gate"] == "executable_sql_generation")
            production_gate = next(gate for gate in gates if gate["gate"] == "production_promotion")

            self.assertEqual(executable_gate["status"], "blocked")
            self.assertEqual(production_gate["status"], "blocked")


if __name__ == "__main__":
    unittest.main()
