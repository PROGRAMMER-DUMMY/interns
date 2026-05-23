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

    def test_validator_accepts_root_layout_when_inventory_proves_inputs(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            workspace = root / "workspaces" / "demo"
            _write_minimal_ready_workspace(workspace)

            result = WorkspaceArtifactValidator(root, "workspaces/demo").run()

            self.assertTrue(result.ok)
            self.assertFalse(any("docs/ was not found" in warning for warning in result.warnings))
            self.assertFalse(any("datasets/ was not found" in warning for warning in result.warnings))


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
