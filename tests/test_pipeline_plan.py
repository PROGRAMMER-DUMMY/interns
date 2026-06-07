from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from core.onboarding.kpi.feature_resolver import KPIFeatureResolver
from core.onboarding.pipeline_plan import (
    DataEngineeringRoutePlanner,
    PipelineFormatPanel,
    PipelineDecisionRecorder,
    PipelinePlanner,
)
from core.onboarding.relationships.source_to_target_planner import SourceToTargetPlanner
from core.onboarding.workspace.onboarding import WorkspaceOnboarder


class PipelinePlanTests(unittest.TestCase):
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

    def test_route_auto_selects_kpi_only_from_profile_backed_raw_catalog(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self._create_workspace(root)
            WorkspaceOnboarder(root, "workspaces/demo", sample_rows=10).run()

            result = DataEngineeringRoutePlanner(root, "workspaces/demo").build()

            self.assertEqual(result.selected_track, "kpi_only")
            self.assertEqual(result.start_layer, "raw")
            route = json.loads((root / result.json_path).read_text(encoding="utf-8"))
            self.assertEqual(route["catalog_object_count"], 1)
            self.assertEqual(route["remote_policy"]["mode"], "local_first")
            self.assertTrue(route["remote_policy"]["remote_mutation_requires_explicit_approval"])
            self.assertEqual(route["quality_assessment"]["status"], "passed")
            self.assertIn("Single-source KPI proof", route["route_reason"])

    def test_route_auto_selects_medallion_for_multi_source_kpi_workspace(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            workspace = self._create_workspace(root)
            (workspace / "datasets" / "patients.csv").write_text(
                "PatientID,Gender\nP1,Female\nP2,Male\n",
                encoding="utf-8",
            )
            WorkspaceOnboarder(root, "workspaces/demo", sample_rows=10).run()

            result = DataEngineeringRoutePlanner(root, "workspaces/demo").build()

            self.assertEqual(result.selected_track, "medallion")
            route = json.loads((root / result.json_path).read_text(encoding="utf-8"))
            self.assertEqual(route["decision_inputs"]["catalog_object_count"], 2)
            self.assertIn("Multiple raw source objects", route["route_reason"])
            self.assertEqual(route["quality_assessment"]["status"], "passed")

    def test_route_auto_selects_medallion_for_external_profile_family_without_kpis(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            workspace = root / "workspaces" / "cms"
            profiles = workspace / "interns" / "generated" / "profiles"
            contracts = workspace / "interns" / "generated" / "contracts"
            docs = workspace / "docs"
            profiles.mkdir(parents=True)
            contracts.mkdir(parents=True)
            docs.mkdir(parents=True)
            first = r"D:\Cold_Storage\raw\CMS\PBJ_dailyNurseStaffing_CY2025Q1.csv"
            second = r"D:\Cold_Storage\raw\CMS\PBJ_dailyNurseStaffing_CY2025Q2.csv"
            (profiles / "profile_index.json").write_text(
                json.dumps(
                    {
                        "profiles": [
                            {"path": first, "format": "csv", "schema": {"PROVNUM": "String"}},
                            {"path": second, "format": "csv", "schema": {"PROVNUM": "String"}},
                        ]
                    }
                ),
                encoding="utf-8",
            )
            (contracts / "kpi_registry.json").write_text(
                json.dumps({"artifact_type": "kpi_registry.json", "kpis": []}),
                encoding="utf-8",
            )
            (docs / "source_selection.json").write_text(
                json.dumps(
                    {
                        "sources": [
                            {
                                "path": first,
                                "target_kind": "dataset",
                                "discovery_group": "PBJ Nurse Staffing",
                            },
                            {
                                "path": second,
                                "target_kind": "dataset",
                                "discovery_group": "PBJ Nurse Staffing",
                            },
                        ]
                    }
                ),
                encoding="utf-8",
            )

            result = DataEngineeringRoutePlanner(root, "workspaces/cms", track="auto").build()

            self.assertEqual(result.selected_track, "medallion")
            route = json.loads((root / result.json_path).read_text(encoding="utf-8"))
            self.assertEqual(route["source_family_summary"]["family_count"], 1)
            self.assertTrue((contracts / "source_family_contracts.json").exists())

    def test_medallion_pipeline_plan_declares_layers_cleaning_and_dedup_gates(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self._create_workspace(root)
            WorkspaceOnboarder(root, "workspaces/demo", sample_rows=10).run()
            KPIFeatureResolver(root, "workspaces/demo", domain="healthcare").run()
            SourceToTargetPlanner(root, "workspaces/demo", target_engine="sql").build()
            DataEngineeringRoutePlanner(root, "workspaces/demo", track="medallion").build()

            result = PipelinePlanner(
                root,
                "workspaces/demo",
                track="medallion",
                target_engine="sql",
                table_format="local_parquet",
            ).build()

            self.assertEqual(result.selected_track, "medallion")
            self.assertEqual(result.table_format, "local_parquet")
            self.assertEqual(result.status, "ready_for_generation")
            plan = json.loads((root / result.json_path).read_text(encoding="utf-8"))
            layers = {layer["layer"]: layer for layer in plan["layers"]}
            self.assertEqual(set(layers), {"bronze", "silver", "gold"})
            self.assertTrue(layers["bronze"]["objects"][0]["audit_columns_required"])
            self.assertEqual(
                layers["silver"]["objects"][0]["deduplication"]["application"],
                "approval_gated",
            )
            self.assertIn("dedup_application_approval_gated", plan["quality_gates"])
            self.assertIn("raw_paths_limited_to_ingestion_bootstrap", plan["quality_gates"])

    def test_medallion_pipeline_plan_blocks_until_table_format_is_chosen(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self._create_workspace(root)
            WorkspaceOnboarder(root, "workspaces/demo", sample_rows=10).run()
            KPIFeatureResolver(root, "workspaces/demo", domain="healthcare").run()
            SourceToTargetPlanner(root, "workspaces/demo", target_engine="sql").build()
            DataEngineeringRoutePlanner(root, "workspaces/demo", track="medallion").build()

            result = PipelinePlanner(root, "workspaces/demo", track="medallion").build()

            self.assertEqual(result.status, "blocked")
            self.assertEqual(result.table_format, "unresolved")
            plan = json.loads((root / result.json_path).read_text(encoding="utf-8"))
            self.assertEqual(plan["blockers"][0]["type"], "pipeline_table_format_unresolved")
            self.assertTrue(
                (root / "workspaces" / "demo" / "interns" / "reports" / "pipeline_format" / "current.json").exists()
            )

    def test_pipeline_format_panel_answer_unblocks_table_format(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self._create_workspace(root)
            WorkspaceOnboarder(root, "workspaces/demo", sample_rows=10).run()
            KPIFeatureResolver(root, "workspaces/demo", domain="healthcare").run()
            SourceToTargetPlanner(root, "workspaces/demo", target_engine="sql").build()
            DataEngineeringRoutePlanner(root, "workspaces/demo", track="medallion").build()
            PipelineFormatPanel(root, "workspaces/demo").prepare()

            answer = PipelineFormatPanel(root, "workspaces/demo").apply("option_a")
            result = PipelinePlanner(root, "workspaces/demo", track="medallion").build()

            self.assertEqual(answer["table_format"], "delta")
            self.assertEqual(result.table_format, "delta")
            self.assertEqual(result.status, "ready_for_generation")

    def test_existing_gold_validation_blocks_without_lineage_plan(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self._create_workspace(root)
            WorkspaceOnboarder(root, "workspaces/demo", sample_rows=10).run()
            DataEngineeringRoutePlanner(
                root,
                "workspaces/demo",
                track="existing_gold_validation",
            ).build()

            result = PipelinePlanner(
                root,
                "workspaces/demo",
                track="existing_gold_validation",
            ).build()

            self.assertEqual(result.status, "blocked")
            plan = json.loads((root / result.json_path).read_text(encoding="utf-8"))
            blocker_types = {blocker["type"] for blocker in plan["blockers"]}
            self.assertIn("existing_gold_validation_missing_source_plan", blocker_types)

    def test_percentage_kpi_blocks_without_denominator_scope(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self._create_workspace(root)
            WorkspaceOnboarder(root, "workspaces/demo", sample_rows=10).run()
            contracts = root / "workspaces" / "demo" / "interns" / "generated" / "contracts"
            (contracts / "source_to_target_plan.json").write_text(
                json.dumps(
                    {
                        "artifact_type": "source_to_target_plan.json",
                        "summary": {"kpi_count": 1, "ready_kpi_count": 1, "blocked_kpi_count": 0},
                        "kpis": [
                            {
                                "kpi_id": "kpi_001",
                                "business_question": "What is percentage share of lives by department?",
                                "metric": "percentage share of distinct PatientID",
                            }
                        ],
                    }
                ),
                encoding="utf-8",
            )
            DataEngineeringRoutePlanner(root, "workspaces/demo", track="medallion").build()
            PipelineDecisionRecorder(root, "workspaces/demo").record_table_format(
                "local_parquet",
                reason="Test focuses on denominator blocker.",
            )

            result = PipelinePlanner(root, "workspaces/demo", track="medallion").build()

            self.assertEqual(result.status, "blocked")
            plan = json.loads((root / result.json_path).read_text(encoding="utf-8"))
            blocker_types = {blocker["type"] for blocker in plan["blockers"]}
            self.assertIn("percentage_denominator_scope_unresolved", blocker_types)

    def test_percentage_denominator_decision_unblocks_pipeline_plan(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self._create_workspace(root)
            WorkspaceOnboarder(root, "workspaces/demo", sample_rows=10).run()
            contracts = root / "workspaces" / "demo" / "interns" / "generated" / "contracts"
            (contracts / "source_to_target_plan.json").write_text(
                json.dumps(
                    {
                        "artifact_type": "source_to_target_plan.json",
                        "summary": {"kpi_count": 1, "ready_kpi_count": 1, "blocked_kpi_count": 0},
                        "kpis": [
                            {
                                "kpi_id": "kpi_001",
                                "business_question": "What is percentage share of lives by department?",
                                "metric": "percentage share of distinct PatientID",
                            }
                        ],
                    }
                ),
                encoding="utf-8",
            )
            DataEngineeringRoutePlanner(root, "workspaces/demo", track="medallion").build()
            PipelineDecisionRecorder(root, "workspaces/demo").record_table_format(
                "local_parquet",
                reason="Test focuses on denominator blocker.",
            )
            PipelineDecisionRecorder(root, "workspaces/demo").record_denominator_scope(
                "kpi_001",
                "global_total",
                reason="Approved by test.",
            )

            result = PipelinePlanner(root, "workspaces/demo", track="medallion").build()

            self.assertEqual(result.status, "ready_for_generation")
            plan = json.loads((root / result.json_path).read_text(encoding="utf-8"))
            self.assertEqual(
                plan["decisions"]["percentage_denominator_scopes"]["kpi_001"],
                "global_total",
            )
            self.assertEqual(plan["blockers"], [])

    def test_apply_pipeline_decision_grain_bucketing_keys_under_kpi_id(self):
        # A direct, non-deadlocking path to record a grain decision. It must key
        # under the given kpi_id (the intent-contract panel path mis-keyed it to
        # an empty string, so the generator never applied banding).
        from core.onboarding.pipeline_plan import decision_main
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self._create_workspace(root)
            WorkspaceOnboarder(root, "workspaces/demo", sample_rows=10).run()
            rc = decision_main([
                "--workspace", "workspaces/demo", "--repo-root", str(root),
                "--kpi-id", "kpi_002", "--grain-bucketing", "band_continuous_cuts",
            ])
            self.assertEqual(rc, 0)
            decisions = json.loads(
                (root / "workspaces" / "demo" / "interns" / "generated"
                 / "contracts" / "pipeline_decisions.json").read_text(encoding="utf-8")
            )
            self.assertEqual(
                decisions["grain_bucketing_decisions"]["kpi_002"],
                "band_continuous_cuts",
            )
            self.assertNotIn("", decisions["grain_bucketing_decisions"])

    def test_apply_pipeline_decision_requires_exactly_one_facet(self):
        from core.onboarding.pipeline_plan import decision_main
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            with self.assertRaises(SystemExit):
                decision_main([
                    "--workspace", "workspaces/demo", "--repo-root", str(root),
                    "--kpi-id", "kpi_002",
                ])  # neither facet -> error


if __name__ == "__main__":
    unittest.main()
