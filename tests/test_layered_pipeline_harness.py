from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from core.onboarding.bronze_silver_standards import BronzeSilverStandardsBuilder
from core.onboarding.harness.layered_pipeline_harness import LayeredPipelineHarness
from core.onboarding.kpi.feature_resolver import KPIFeatureResolver
from core.onboarding.pipeline_plan import DataEngineeringRoutePlanner, PipelineDecisionRecorder, PipelinePlanner
from core.onboarding.relationships.source_to_target_planner import SourceToTargetPlanner
from core.onboarding.workspace.onboarding import WorkspaceOnboarder


class LayeredPipelineHarnessTests(unittest.TestCase):
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

    def test_passes_ready_medallion_pipeline_plan(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self._create_workspace(root)
            WorkspaceOnboarder(root, "workspaces/demo", sample_rows=10).run()
            KPIFeatureResolver(root, "workspaces/demo", domain="healthcare").run()
            SourceToTargetPlanner(root, "workspaces/demo", target_engine="sql").build()
            DataEngineeringRoutePlanner(root, "workspaces/demo", track="medallion").build()
            PipelineDecisionRecorder(root, "workspaces/demo").record_table_format("local_parquet")
            PipelinePlanner(root, "workspaces/demo", track="medallion").build()

            result = LayeredPipelineHarness(root, "workspaces/demo").run()

            self.assertTrue(result.ok)
            current = json.loads((root / result.current_json_path).read_text(encoding="utf-8"))
            self.assertEqual(current["summary"]["track"], "medallion")
            self.assertEqual(current["summary"]["layers"], 3)

    def test_fails_missing_contracts(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            workspace = root / "workspaces" / "demo"
            workspace.mkdir(parents=True)

            result = LayeredPipelineHarness(root, "workspaces/demo").run()

            self.assertFalse(result.ok)
            current = json.loads((root / result.current_json_path).read_text(encoding="utf-8"))
            codes = {finding["code"] for finding in current["findings"]}
            self.assertIn("missing_catalog_contract", codes)
            self.assertIn("missing_data_engineering_route", codes)
            self.assertIn("missing_pipeline_plan", codes)

    def test_fails_dedup_not_approval_gated(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            workspace = root / "workspaces" / "demo"
            contracts = workspace / "interns" / "generated" / "contracts"
            contracts.mkdir(parents=True)
            BronzeSilverStandardsBuilder(root, "workspaces/demo").build()
            (contracts / "catalog_contract.json").write_text(
                json.dumps(
                    {
                        "artifact_type": "catalog_contract.json",
                        "objects": [
                            {
                                "logical_name": "raw.transactions",
                                "profile_path": "profiles/transactions.profile.json",
                                "physical_bindings": [
                                    {"binding_type": "local_file", "usage": "ingestion_bootstrap"}
                                ],
                            }
                        ],
                    }
                ),
                encoding="utf-8",
            )
            (contracts / "data_engineering_route.json").write_text(
                json.dumps(
                    {
                        "artifact_type": "data_engineering_route.json",
                        "selected_track": "medallion",
                        "remote_policy": {"remote_mutation_requires_explicit_approval": True},
                    }
                ),
                encoding="utf-8",
            )
            (contracts / "pipeline_plan.json").write_text(
                json.dumps(
                    {
                        "artifact_type": "pipeline_plan.json",
                        "selected_track": "medallion",
                        "bronze_silver_standards": "workspaces/demo/interns/generated/contracts/bronze_silver_standards.json",
                        "transformation_manifest": "workspaces/demo/interns/generated/contracts/transformation_manifest.json",
                        "workflow_reroute_policy": "workspaces/demo/interns/generated/contracts/workflow_reroute_policy.json",
                        "quality_gates": [
                            "catalog_contract_present",
                            "raw_paths_limited_to_ingestion_bootstrap",
                            "grain_declared",
                            "duplicate_behavior_declared",
                            "layered_harness_required",
                        ],
                        "layers": [
                            {
                                "layer": "silver",
                                "objects": [
                                    {
                                        "grain": "source_row",
                                        "duplicate_behavior": "detect",
                                        "deduplication": {"application": "automatic"},
                                    }
                                ],
                            }
                        ],
                    }
                ),
                encoding="utf-8",
            )

            result = LayeredPipelineHarness(root, "workspaces/demo").run()

            self.assertFalse(result.ok)
            current = json.loads((root / result.current_json_path).read_text(encoding="utf-8"))
            codes = {finding["code"] for finding in current["findings"]}
            self.assertIn("dedup_not_approval_gated", codes)

    def test_fails_pipeline_plan_blockers(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            workspace = root / "workspaces" / "demo"
            contracts = workspace / "interns" / "generated" / "contracts"
            contracts.mkdir(parents=True)
            BronzeSilverStandardsBuilder(root, "workspaces/demo").build()
            (contracts / "catalog_contract.json").write_text(
                json.dumps(
                    {
                        "artifact_type": "catalog_contract.json",
                        "objects": [
                            {
                                "logical_name": "raw.transactions",
                                "profile_path": "profiles/transactions.profile.json",
                                "physical_bindings": [
                                    {"binding_type": "local_file", "usage": "ingestion_bootstrap"}
                                ],
                            }
                        ],
                    }
                ),
                encoding="utf-8",
            )
            (contracts / "data_engineering_route.json").write_text(
                json.dumps(
                    {
                        "artifact_type": "data_engineering_route.json",
                        "selected_track": "medallion",
                        "remote_policy": {"remote_mutation_requires_explicit_approval": True},
                    }
                ),
                encoding="utf-8",
            )
            (contracts / "pipeline_plan.json").write_text(
                json.dumps(
                    {
                        "artifact_type": "pipeline_plan.json",
                        "status": "blocked",
                        "bronze_silver_standards": "workspaces/demo/interns/generated/contracts/bronze_silver_standards.json",
                        "transformation_manifest": "workspaces/demo/interns/generated/contracts/transformation_manifest.json",
                        "workflow_reroute_policy": "workspaces/demo/interns/generated/contracts/workflow_reroute_policy.json",
                        "quality_gates": [
                            "catalog_contract_present",
                            "raw_paths_limited_to_ingestion_bootstrap",
                            "grain_declared",
                            "duplicate_behavior_declared",
                            "layered_harness_required",
                        ],
                        "layers": [],
                        "blockers": [
                            {
                                "type": "percentage_denominator_scope_unresolved",
                                "message": "Denominator scope is unresolved.",
                            }
                        ],
                    }
                ),
                encoding="utf-8",
            )

            result = LayeredPipelineHarness(root, "workspaces/demo").run()

            self.assertFalse(result.ok)
            current = json.loads((root / result.current_json_path).read_text(encoding="utf-8"))
            codes = {finding["code"] for finding in current["findings"]}
            self.assertIn("pipeline_plan_blocked", codes)
            self.assertIn("pipeline_blocker_percentage_denominator_scope_unresolved", codes)


if __name__ == "__main__":
    unittest.main()
