from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path

from core.config import Config, DatabricksConfig
from core.execution.backend import DuckDBBackend, StrictWarehouseBackend, build_execution_backend
from core.failures import FailureKind, WorkflowBlockedError, validation_blocker
from core.medallion.design import _preflight
from core.onboarding.kpi_blocker_workflow import prepare_kpi_blocker_panel
from core.onboarding.relationship_contracts import load_relationship_contracts
from core.onboarding.source_to_target_planner import SourceToTargetPlanner
from core.onboarding.workspace_artifact_validator import WorkspaceArtifactValidator
from core.storage.workspace_layout import WorkspaceLayout


class FailureContractTests(unittest.TestCase):
    def test_validation_blocker_summary_is_structured(self):
        failure = validation_blocker(
            "unit.stage",
            "bad artifact",
            next_command="uv run validate-workspace-artifacts --workspace workspaces/demo",
        )

        self.assertEqual(failure.summary()["kind"], "validation_blocker")
        self.assertEqual(failure.summary()["stage"], "unit.stage")
        self.assertIn("next_command", failure.summary())

    def test_remote_denied_backend_keeps_structured_fallback_reason(self):
        os.environ.pop("AUTORESEARCH_ALLOW_REMOTE_EXECUTION", None)
        cfg = Config(
            databricks=DatabricksConfig(
                enabled=True,
                execution="jobs",
                host="https://example.cloud.databricks.com",
                token="token",
            )
        )

        backend = build_execution_backend(cfg)

        self.assertIsInstance(backend, DuckDBBackend)
        self.assertEqual(backend.fallback_failure.kind, FailureKind.REMOTE_EXECUTION_DENIED)

    def test_strict_warehouse_failure_is_typed(self):
        class FailingClient:
            def _extract_warehouse_id(self):
                return "warehouse_1"

            def get_client(self):
                raise RuntimeError("remote unavailable")

        with tempfile.TemporaryDirectory() as tmp:
            sql_path = Path(tmp) / "query.sql"
            log_path = Path(tmp) / "run.log"
            sql_path.write_text("SELECT 1", encoding="utf-8")
            backend = StrictWarehouseBackend(
                FailingClient(),
                DatabricksConfig(enabled=True, execution="warehouse", fallback="fail"),
            )

            result = backend.execute({"sql_file": str(sql_path)}, 10, 10, log_path)

            self.assertEqual(result.exit_code, 1)
            self.assertIsNotNone(result.failure)
            self.assertEqual(result.failure.kind, FailureKind.REMOTE_EXECUTION_UNAVAILABLE)

    def test_kpi_panel_validation_failure_is_typed(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            workspace = root / "workspaces" / "demo"
            contracts = workspace / "interns" / "generated" / "contracts"
            profiles = workspace / "interns" / "generated" / "profiles"
            contracts.mkdir(parents=True)
            profiles.mkdir(parents=True)
            (contracts / "kpi_registry.json").write_text(json.dumps({"kpis": []}), encoding="utf-8")
            (profiles / "profile_index.json").write_text(json.dumps({"profiles": []}), encoding="utf-8")

            with self.assertRaises(WorkflowBlockedError) as caught:
                prepare_kpi_blocker_panel(root, "workspaces/demo", onboard_if_missing=False)

            self.assertEqual(caught.exception.failure.kind, FailureKind.VALIDATION_BLOCKER)
            self.assertEqual(caught.exception.failure.stage, "prepare_kpi_blocker_panel.validation")

    def test_medallion_preflight_invalid_registry_is_typed(self):
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp) / "demo"
            layout = WorkspaceLayout(project_root=workspace)
            layout.contracts_dir.mkdir(parents=True)
            layout.profiles_dir.mkdir(parents=True)
            (layout.contracts_dir / "domain_model.json").write_text("{}", encoding="utf-8")
            (layout.profiles_dir / "profile_index.json").write_text("{}", encoding="utf-8")
            (layout.contracts_dir / "kpi_registry.json").write_text("{bad json", encoding="utf-8")

            with self.assertRaises(WorkflowBlockedError) as caught:
                _preflight(layout)

            self.assertEqual(caught.exception.failure.kind, FailureKind.VALIDATION_BLOCKER)
            self.assertEqual(caught.exception.failure.stage, "medallion_design.preflight")

    def test_source_to_target_rejects_mapping_without_contract_metadata(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            workspace = root / "workspaces" / "demo"
            contracts = workspace / "interns" / "generated" / "contracts"
            contracts.mkdir(parents=True)
            (contracts / "kpi_feature_mapping.json").write_text(
                json.dumps({"version": 2, "workspace": "workspaces/demo", "kpis": []}),
                encoding="utf-8",
            )

            with self.assertRaises(ValueError) as caught:
                SourceToTargetPlanner(root, "workspaces/demo").build()

            self.assertIn("artifact_type", str(caught.exception))

    def test_relationship_loader_rejects_unsupported_contract_version(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            contracts = root / "workspaces" / "demo" / "interns" / "generated" / "contracts"
            contracts.mkdir(parents=True)
            (contracts / "relationship_contracts.json").write_text(
                json.dumps(
                    {
                        "artifact_type": "relationship_contracts.json",
                        "version": 999,
                        "generated_by": "build-relationship-contracts",
                        "relationships": [],
                    }
                ),
                encoding="utf-8",
            )

            with self.assertRaises(ValueError) as caught:
                load_relationship_contracts(root, "workspaces/demo")

            self.assertIn("version=999 is not supported", str(caught.exception))

    def test_validator_rejects_source_to_target_plan_missing_metadata(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            workspace = root / "workspaces" / "demo"
            contracts = workspace / "interns" / "generated" / "contracts"
            contracts.mkdir(parents=True)
            (contracts / "source_to_target_plan.json").write_text(
                json.dumps({"version": 1, "target_engine": "sql", "kpis": []}),
                encoding="utf-8",
            )

            result = WorkspaceArtifactValidator(root, "workspaces/demo").run()

            self.assertFalse(result.ok)
            self.assertTrue(any("source_to_target_plan.json" in error for error in result.errors))


if __name__ == "__main__":
    unittest.main()
