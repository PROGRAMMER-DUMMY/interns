from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from core.onboarding.harness.pipeline_execution_harness import PipelineExecutionHarness
from core.onboarding.kpi.feature_resolver import KPIFeatureResolver
from core.onboarding.pipeline_plan import DataEngineeringRoutePlanner, PipelineDecisionRecorder, PipelinePlanner
from core.onboarding.pipeline_sql_generator import PipelineSQLGenerator
from core.onboarding.relationships.source_to_target_planner import SourceToTargetPlanner
from core.onboarding.workspace.onboarding import WorkspaceOnboarder


class PipelineExecutionHarnessTests(unittest.TestCase):
    def setUp(self) -> None:
        try:
            import duckdb  # noqa: F401
        except ImportError:
            self.skipTest("duckdb is not installed")

    def _create_workspace(self, root: Path) -> None:
        workspace = root / "workspaces" / "demo"
        (workspace / "datasets").mkdir(parents=True)
        (workspace / "docs").mkdir(parents=True)
        (workspace / "datasets" / "transactions.csv").write_text(
            "ClaimID,PaidAmount,LineOfBusiness,ServiceDate\n"
            "C1,10.50,Commercial,2024-01-01\n"
            "C2,20.25,Medicare,2024-01-02\n",
            encoding="utf-8",
        )
        (workspace / "docs" / "kpi_registry.csv").write_text(
            "Key business question,Description,Cuts / grain hints,Metric,Data model refinement required\n"
            "What is paid amount by line of business?,Baseline KPI,LineOfBusiness,sum(PaidAmount),Confirm paid amount source\n",
            encoding="utf-8",
        )
        (workspace / "docs" / "data_model.md").write_text(
            "# Data Model\n\ntransactions has ClaimID, PaidAmount, LineOfBusiness, ServiceDate.\n",
            encoding="utf-8",
        )

    def test_executes_generated_pipeline_sql_and_writes_evidence(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self._create_workspace(root)
            WorkspaceOnboarder(root, "workspaces/demo", sample_rows=10).run()
            KPIFeatureResolver(root, "workspaces/demo", domain="healthcare").run()
            SourceToTargetPlanner(root, "workspaces/demo", target_engine="sql").build()
            DataEngineeringRoutePlanner(root, "workspaces/demo", track="medallion").build()
            PipelineDecisionRecorder(root, "workspaces/demo").record_table_format("local_parquet")
            PipelinePlanner(root, "workspaces/demo", track="medallion").build()
            PipelineSQLGenerator(root, "workspaces/demo").generate()

            result = PipelineExecutionHarness(root, "workspaces/demo").run()

            self.assertTrue(result.ok)
            self.assertEqual(len(result.records), 3)
            self.assertTrue(all(record.row_count == 2 for record in result.records))
            bronze = next(record for record in result.records if record.layer == "bronze")
            self.assertIn("_source_object", bronze.columns)
            report = json.loads((root / result.evidence_path).read_text(encoding="utf-8"))
            self.assertEqual(report["artifact_type"], "pipeline_execution_harness/current.json")
            self.assertEqual(report["status"], "passed")

    def test_fails_when_generated_pipeline_sql_is_missing(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            workspace = root / "workspaces" / "demo"
            contracts = workspace / "interns" / "generated" / "contracts"
            contracts.mkdir(parents=True)
            (contracts / "pipeline_plan.json").write_text(
                json.dumps(
                    {
                        "artifact_type": "pipeline_plan.json",
                        "status": "ready_for_generation",
                        "layers": [
                            {
                                "layer": "bronze",
                                "objects": [
                                    {
                                        "source_object": "raw.transactions",
                                        "target_object": "bronze.transactions",
                                    }
                                ],
                            }
                        ],
                    }
                ),
                encoding="utf-8",
            )

            result = PipelineExecutionHarness(root, "workspaces/demo").run()

            self.assertFalse(result.ok)
            self.assertIn("generated pipeline_layers.sql was not found", result.errors)

    def test_redacts_sensitive_values_from_sample_output(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            workspace = root / "workspaces" / "demo"
            pipeline = workspace / "interns" / "generated" / "pipeline"
            contracts = workspace / "interns" / "generated" / "contracts"
            data = workspace / "datasets"
            pipeline.mkdir(parents=True)
            contracts.mkdir(parents=True)
            data.mkdir(parents=True)
            (data / "patients.csv").write_text(
                "PatientID,FirstName,LastName,SSN,PhoneNumber,Address\n"
                "P1,Ada,Lovelace,111-22-3333,555-0100,1 Main St\n",
                encoding="utf-8",
            )
            (contracts / "pipeline_plan.json").write_text(
                json.dumps(
                    {
                        "artifact_type": "pipeline_plan.json",
                        "status": "ready_for_generation",
                        "layers": [
                            {
                                "layer": "bronze",
                                "objects": [
                                    {
                                        "source_object": "raw.patients",
                                        "target_object": "bronze.patients",
                                    }
                                ],
                            }
                        ],
                    }
                ),
                encoding="utf-8",
            )
            (pipeline / "pipeline_layers.sql").write_text(
                "-- BEGIN CATALOG BOOTSTRAP\n"
                "CREATE OR REPLACE VIEW \"catalog_raw_patients\" AS "
                "SELECT * FROM read_csv_auto('workspaces/demo/datasets/patients.csv', union_by_name=true);\n"
                "-- END CATALOG BOOTSTRAP\n"
                "CREATE OR REPLACE VIEW \"bronze_patients\" AS "
                "SELECT *, 'raw.patients' AS _source_object, current_timestamp AS _ingested_at, "
                "row_number() OVER () AS _source_row_number FROM \"catalog_raw_patients\";\n",
                encoding="utf-8",
            )

            result = PipelineExecutionHarness(root, "workspaces/demo").run()

            self.assertTrue(result.ok)
            sample = result.records[0].sample_output_table
            self.assertIn("<redacted>", sample)
            self.assertNotIn("111-22-3333", sample)
            self.assertNotIn("555-0100", sample)
            self.assertNotIn("1 Main St", sample)
            self.assertNotIn("Ada", sample)
            self.assertNotIn("P1", sample)


if __name__ == "__main__":
    unittest.main()
