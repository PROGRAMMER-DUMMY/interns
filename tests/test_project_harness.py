import json
import tempfile
import unittest
from pathlib import Path

from core.onboarding.harness.project_harness import ProjectHarness
from core.onboarding.harness.trajectory_recorder import WorkspaceTrajectoryRecorder
from core.onboarding.workspace.validation import WorkspaceArtifactValidator


class ProjectHarnessTests(unittest.TestCase):
    def test_project_harness_scores_valid_kpi_workspace_above_threshold(self):
        try:
            import duckdb  # noqa: F401
        except ImportError:
            self.skipTest("duckdb is not installed")

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            workspace = root / "workspaces" / "demo"
            _write_minimal_ready_workspace(workspace)

            result = ProjectHarness(root, "workspaces/demo", domain="healthcare").run()

            self.assertTrue(result.ok)
            self.assertGreaterEqual(result.score, 95)
            self.assertTrue(result.checks["workflow_guardrails"]["ok"])
            self.assertEqual(result.checks["layered_pipeline"]["status"], "skipped")
            self.assertTrue(result.checks["evidence_graph"]["ok"])
            self.assertTrue((workspace / "interns" / "generated" / "evidence" / "project_harness.json").exists())
            self.assertTrue((workspace / "interns" / "reports" / "project_harness.md").exists())

    def test_project_harness_blocks_on_bad_trajectory_health(self):
        try:
            import duckdb  # noqa: F401
        except ImportError:
            self.skipTest("duckdb is not installed")

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            workspace = root / "workspaces" / "demo"
            _write_minimal_ready_workspace(workspace)
            dataset = workspace / "datasets" / "raw.csv"
            dataset.parent.mkdir(parents=True)
            dataset.write_text("Id\n1\n", encoding="utf-8")
            WorkspaceTrajectoryRecorder(root, "workspaces/demo").record(
                event_type="command",
                status="failed",
                exit_code=1,
                summary="Unsupported shell pipeline failed.",
                command="cat workspaces/demo/datasets/raw.csv | head -n 1",
            )

            result = ProjectHarness(root, "workspaces/demo", domain="healthcare").run()

            self.assertFalse(result.ok)
            self.assertFalse(result.checks["workflow_guardrails"]["ok"])
            self.assertTrue(any("workflow guardrail" in blocker for blocker in result.blockers))
            self.assertIn(
                "uv run validate-workflow-guardrails --workspace workspaces/demo",
                result.next_commands,
            )

    def test_project_harness_blocks_on_failed_pipeline_execution_harness_artifact(self):
        try:
            import duckdb  # noqa: F401
        except ImportError:
            self.skipTest("duckdb is not installed")

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            workspace = root / "workspaces" / "demo"
            _write_minimal_ready_workspace(workspace)
            harness_dir = workspace / "interns" / "generated" / "evidence" / "pipeline_execution_harness"
            harness_dir.mkdir(parents=True)
            _write_json(
                harness_dir / "current.json",
                {
                    "artifact_type": "pipeline_execution_harness/current.json",
                    "ok": False,
                    "status": "failed",
                    "summary": {"passed_count": 3, "failed_count": 1},
                },
            )

            result = ProjectHarness(root, "workspaces/demo", domain="healthcare").run()

            self.assertFalse(result.ok)
            check = result.checks["pipeline_execution_harness"]
            self.assertEqual(check["status"], "failed")
            self.assertFalse(check["ok"])
            self.assertEqual(check["passed"], 3)
            self.assertEqual(check["failed"], 1)
            self.assertTrue(any("pipeline execution harness" in blocker for blocker in result.blockers))

    def test_project_harness_blocks_when_required_pipeline_execution_harness_missing(self):
        try:
            import duckdb  # noqa: F401
        except ImportError:
            self.skipTest("duckdb is not installed")

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            workspace = root / "workspaces" / "demo"
            _write_minimal_ready_workspace(workspace)
            contracts = workspace / "interns" / "generated" / "contracts"
            _write_json(
                contracts / "pipeline_plan.json",
                {
                    "artifact_type": "pipeline_plan.json",
                    "status": "ready_for_generation",
                    "selected_track": "medallion",
                    "layers": [{"layer": "bronze", "objects": [{"target_object": "bronze.facts"}]}],
                },
            )

            result = ProjectHarness(root, "workspaces/demo", domain="healthcare").run()

            self.assertFalse(result.ok)
            check = result.checks["pipeline_execution_harness"]
            self.assertEqual(check["status"], "failed")
            self.assertFalse(check["ok"])

    def test_project_harness_blocks_on_failed_data_quality_harness_artifact(self):
        try:
            import duckdb  # noqa: F401
        except ImportError:
            self.skipTest("duckdb is not installed")

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            workspace = root / "workspaces" / "demo"
            _write_minimal_ready_workspace(workspace)
            harness_dir = workspace / "interns" / "generated" / "evidence" / "data_quality_harness"
            harness_dir.mkdir(parents=True)
            _write_json(
                harness_dir / "current.json",
                {
                    "artifact_type": "data_quality_harness/current.json",
                    "ok": False,
                    "status": "failed",
                    "summary": {"passed_count": 5, "failed_count": 2},
                },
            )

            result = ProjectHarness(root, "workspaces/demo", domain="healthcare").run()

            self.assertFalse(result.ok)
            check = result.checks["data_quality_harness"]
            self.assertEqual(check["status"], "failed")
            self.assertFalse(check["ok"])
            self.assertEqual(check["passed"], 5)
            self.assertEqual(check["failed"], 2)
            self.assertTrue(any("data quality harness" in blocker for blocker in result.blockers))

    def test_validator_accepts_root_layout_when_inventory_proves_inputs(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            workspace = root / "workspaces" / "demo"
            _write_minimal_ready_workspace(workspace)

            result = WorkspaceArtifactValidator(root, "workspaces/demo").run()

            self.assertTrue(result.ok)
            self.assertFalse(any("docs/ was not found" in warning for warning in result.warnings))
            self.assertFalse(any("datasets/ was not found" in warning for warning in result.warnings))

    def test_validator_requires_pipeline_execution_and_data_quality_for_generated_pipeline_sql(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            workspace = root / "workspaces" / "demo"
            _write_minimal_ready_workspace(workspace)
            contracts = workspace / "interns" / "generated" / "contracts"
            pipeline = workspace / "interns" / "generated" / "pipeline"
            pipeline.mkdir(parents=True)
            _write_json(
                contracts / "catalog_contract.json",
                {
                    "artifact_type": "catalog_contract.json",
                    "version": 1,
                    "generated_by": "build-catalog-contract",
                    "workspace": "workspaces/demo",
                    "catalog": "local_workspace",
                    "default_schema": "raw",
                    "policy": {},
                    "objects": [
                        {
                            "object_id": "raw__facts",
                            "logical_name": "raw.facts",
                            "layer": "raw",
                            "dataset": "workspaces/demo/facts.csv",
                            "columns": [{"name": "Id"}],
                            "physical_bindings": [],
                            "trust_state": "profile_backed",
                        }
                    ],
                },
            )
            _write_json(
                contracts / "pipeline_plan.json",
                {
                    "artifact_type": "pipeline_plan.json",
                    "version": 1,
                    "generated_by": "prepare-pipeline-plan",
                    "workspace": "workspaces/demo",
                    "selected_track": "medallion",
                    "target_engine": "sql",
                    "table_format": "local_parquet",
                    "catalog_contract": "workspaces/demo/interns/generated/contracts/catalog_contract.json",
                    "route_contract": "workspaces/demo/interns/generated/contracts/data_engineering_route.json",
                    "policy": {},
                    "layers": [{"layer": "bronze", "objects": [{"target_object": "bronze.facts"}]}],
                    "quality_gates": [],
                    "blockers": [],
                    "status": "ready_for_generation",
                },
            )
            (pipeline / "pipeline_layers.sql").write_text(
                'CREATE OR REPLACE VIEW "bronze_facts" AS SELECT 1 AS Id;\n',
                encoding="utf-8",
            )

            result = WorkspaceArtifactValidator(root, "workspaces/demo").run()

            self.assertFalse(result.ok)
            self.assertTrue(any("pipeline_execution_harness" in error for error in result.errors))
            self.assertTrue(any("data_quality_harness" in error for error in result.errors))


def _write_minimal_ready_workspace(workspace: Path) -> None:
    contracts = workspace / "interns" / "generated" / "contracts"
    requirements = workspace / "interns" / "generated" / "requirements"
    profiles = workspace / "interns" / "generated" / "profiles"
    solutions = workspace / "interns" / "generated" / "solutions"
    presentation = workspace / "interns" / "reports" / "presentation"
    contracts.mkdir(parents=True)
    requirements.mkdir(parents=True)
    profiles.mkdir(parents=True)
    solutions.mkdir(parents=True)
    presentation.mkdir(parents=True)
    (workspace / "facts.csv").write_text("Id\n1\n", encoding="utf-8")
    (workspace / "questions.sql").write_text("-- demo KPI\n", encoding="utf-8")
    (workspace / "schema.sql").write_text("CREATE TABLE facts(Id INT);\n", encoding="utf-8")
    _write_json(
        requirements / "input_inventory.json",
        {
            "workspace": "workspaces/demo",
            "data_files": ["workspaces/demo/facts.csv"],
            "kpi_registries": ["workspaces/demo/questions.sql"],
            "data_models": ["workspaces/demo/schema.sql"],
        },
    )
    _write_json(
        profiles / "profile_index.json",
        {
            "artifact_type": "profile_index.json",
            "version": 1,
            "generated_by": "onboard-workspace",
            "profiles": [
                {
                    "path": "workspaces/demo/facts.csv",
                    "schema": {"Id": "Int64"},
                    "row_count": 1,
                    "profile_path": "workspaces/demo/interns/generated/profiles/facts.csv.profile.json",
                }
            ],
        },
    )
    _write_json(
        contracts / "domain_model.json",
        {
            "artifact_type": "domain_model.json",
            "version": 1,
            "generated_by": "onboard-workspace",
            "datasets": [{"path": "workspaces/demo/facts.csv"}],
            "data_models": ["workspaces/demo/schema.sql"],
        },
    )
    _write_json(
        contracts / "kpi_registry.json",
        {
            "artifact_type": "kpi_registry.json",
            "version": 1,
            "generated_by": "onboard-workspace",
            "kpis": [
                {
                    "kpi_id": "kpi_001",
                    "name": "Demo KPI",
                    "description": "Count facts",
                    "cuts": "",
                    "metric": "count(Id)",
                    "refinement_required": "",
                    "source": "workspaces/demo/questions.sql",
                    "status": "ready",
                }
            ],
        },
    )
    _write_json(
        contracts / "kpi_feature_mapping.json",
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
        },
    )
    _write_json(
        contracts / "relationship_contracts.json",
        {
            "artifact_type": "relationship_contracts.json",
            "version": 1,
            "generated_by": "build-relationship-contracts",
            "relationships": [],
            "summary": {
                "relationship_count": 0,
                "executable_relationship_count": 0,
                "candidate_relationship_count": 0,
            },
        },
    )
    _write_json(
        contracts / "source_to_target_plan.json",
        {
            "artifact_type": "source_to_target_plan.json",
            "version": 1,
            "generated_by": "plan-source-to-target",
            "target_engine": "sql",
            "kpis": [],
            "summary": {"kpi_count": 1, "ready_kpi_count": 1, "blocked_kpi_count": 0},
        },
    )
    (solutions / "kpi_001.sql").write_text(
        'CREATE OR REPLACE VIEW "kpi_001_results" AS SELECT 1 AS fact_count;',
        encoding="utf-8",
    )
    for name in ("data-model.svg", "data-model.mermaid.md", "kpi_registry.xlsx"):
        (presentation / name).write_text("placeholder", encoding="utf-8")
    _write_json(
        presentation / "presentation_manifest.json",
        {
            "generated_paths": [
                "workspaces/demo/interns/reports/presentation/data-model.svg",
                "workspaces/demo/interns/reports/presentation/data-model.mermaid.md",
                "workspaces/demo/interns/reports/presentation/kpi_registry.xlsx",
            ]
        },
    )


def _write_json(path: Path, payload: dict) -> None:
    path.write_text(json.dumps(payload, indent=2), encoding="utf-8")


if __name__ == "__main__":
    unittest.main()
