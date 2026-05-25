from __future__ import annotations

import re
import tempfile
import unittest
from pathlib import Path

from core.onboarding.kpi.feature_resolver import KPIFeatureResolver
from core.onboarding.pipeline_plan import (
    DataEngineeringRoutePlanner,
    PipelineDecisionRecorder,
    PipelinePlanner,
)
from core.onboarding.pipeline_sql_generator import PipelineSQLGenerator
from core.onboarding.relationships.source_to_target_planner import SourceToTargetPlanner
from core.onboarding.workspace.onboarding import WorkspaceOnboarder


class PipelineSQLGeneratorTests(unittest.TestCase):
    def _create_workspace(self, root: Path) -> Path:
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
        return workspace

    def test_generates_layer_sql_with_raw_paths_only_in_bootstrap(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self._create_workspace(root)
            WorkspaceOnboarder(root, "workspaces/demo", sample_rows=10).run()
            KPIFeatureResolver(root, "workspaces/demo", domain="healthcare").run()
            SourceToTargetPlanner(root, "workspaces/demo", target_engine="sql").build()
            DataEngineeringRoutePlanner(root, "workspaces/demo", track="medallion").build()
            PipelineDecisionRecorder(root, "workspaces/demo").record_table_format("local_parquet")
            PipelinePlanner(root, "workspaces/demo", track="medallion").build()

            result = PipelineSQLGenerator(root, "workspaces/demo").generate()

            self.assertEqual(result.status, "generated")
            sql = (root / result.path).read_text(encoding="utf-8")
            self.assertIn("-- BEGIN CATALOG BOOTSTRAP", sql)
            self.assertIn("read_csv_auto('workspaces/demo/datasets/transactions.csv'", sql)
            self.assertIn('"bronze_transactions"', sql)
            self.assertIn('"silver_transactions"', sql)
            self.assertIn('"gold_transactions"', sql)
            business_sql = re.sub(
                r"--\s*BEGIN CATALOG BOOTSTRAP\b.*?--\s*END CATALOG BOOTSTRAP\b",
                "",
                sql,
                flags=re.IGNORECASE | re.DOTALL,
            )
            self.assertNotIn("read_csv_auto", business_sql)
            self.assertNotIn("workspaces/demo/datasets/transactions.csv", business_sql)

    def test_generates_format_specific_bootstrap_reader(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            workspace = root / "workspaces" / "demo"
            contracts = workspace / "interns" / "generated" / "contracts"
            contracts.mkdir(parents=True)
            (contracts / "catalog_contract.json").write_text(
                """
{
  "artifact_type": "catalog_contract.json",
  "objects": [
    {
      "logical_name": "raw.claims",
      "dataset": "workspaces/demo/datasets/claims.parquet",
      "format": "parquet",
      "physical_bindings": [{"binding_type": "duckdb_view", "object_name": "catalog_raw_claims"}]
    }
  ]
}
""".strip(),
                encoding="utf-8",
            )
            (contracts / "pipeline_plan.json").write_text(
                """
{
  "artifact_type": "pipeline_plan.json",
  "status": "ready_for_generation",
  "selected_track": "medallion",
  "layers": [
    {
      "layer": "bronze",
      "objects": [{"source_object": "raw.claims", "target_object": "bronze.claims"}]
    }
  ]
}
""".strip(),
                encoding="utf-8",
            )

            result = PipelineSQLGenerator(root, "workspaces/demo").generate()

            sql = (root / result.path).read_text(encoding="utf-8")
            self.assertIn("read_parquet('workspaces/demo/datasets/claims.parquet'", sql)
            self.assertNotIn("read_csv_auto('workspaces/demo/datasets/claims.parquet'", sql)

    def test_blocks_when_pipeline_plan_has_blockers(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self._create_workspace(root)
            WorkspaceOnboarder(root, "workspaces/demo", sample_rows=10).run()
            DataEngineeringRoutePlanner(root, "workspaces/demo", track="existing_gold_validation").build()
            PipelinePlanner(root, "workspaces/demo", track="existing_gold_validation").build()

            with self.assertRaises(ValueError):
                PipelineSQLGenerator(root, "workspaces/demo").generate()

    def test_denominator_decision_allows_percentage_pipeline_sql_generation(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self._create_workspace(root)
            WorkspaceOnboarder(root, "workspaces/demo", sample_rows=10).run()
            contracts = root / "workspaces" / "demo" / "interns" / "generated" / "contracts"
            (contracts / "source_to_target_plan.json").write_text(
                '{"summary":{"blocked_kpi_count":0},"kpis":[{"kpi_id":"kpi_001","business_question":"percentage share","metric":"percentage share"}]}',
                encoding="utf-8",
            )
            DataEngineeringRoutePlanner(root, "workspaces/demo", track="medallion").build()
            PipelineDecisionRecorder(root, "workspaces/demo").record_table_format("local_parquet")
            PipelineDecisionRecorder(root, "workspaces/demo").record_denominator_scope("kpi_001", "global_total")
            PipelinePlanner(root, "workspaces/demo", track="medallion").build()

            result = PipelineSQLGenerator(root, "workspaces/demo").generate()

            self.assertEqual(result.status, "generated")


if __name__ == "__main__":
    unittest.main()
