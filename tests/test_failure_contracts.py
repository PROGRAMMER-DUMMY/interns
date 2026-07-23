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
from core.onboarding.kpi.blocker_workflow import (
    _is_stale_harness_freshness_error,
    prepare_kpi_blocker_panel,
)
from core.onboarding.relationships.contracts import load_relationship_contracts
from core.onboarding.relationships.source_to_target_planner import SourceToTargetPlanner
from core.onboarding.workspace.validation import WorkspaceArtifactValidator
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

    def test_stale_harness_freshness_error_does_not_block_panel_prep(self):
        # prepare_kpi_blocker_panel's own job (resolve features, regenerate
        # SQL) immediately invalidates any previously-recorded harness's
        # sql_sha256, by construction, every time it succeeds. Treating that
        # staleness as a hard blocker created a deadlock: the exact call
        # meant to fix a workspace failed as a side effect of doing its job
        # correctly. Found live: repeatedly had to bypass
        # prepare_kpi_blocker_panel entirely and call KPIFeatureResolver
        # directly to work around this. Only the two staleness-specific
        # messages are exempt; a genuine execution failure still blocks.
        self.assertTrue(_is_stale_harness_freshness_error(
            "workspaces/demo/interns/generated/evidence/kpi_execution_harness.json: "
            "harness record #1 for `kpi_001` is stale; SQL hash changed since execution"
        ))
        self.assertTrue(_is_stale_harness_freshness_error(
            "workspaces/demo/interns/generated/evidence/kpi_execution_harness.json: "
            "harness record #2 for `kpi_002` missing sql_sha256; rerun "
            "run-kpi-execution-harness after SQL generation"
        ))
        self.assertFalse(_is_stale_harness_freshness_error(
            "workspaces/demo/interns/generated/evidence/kpi_execution_harness.json: "
            "KPI execution harness did not pass"
        ))
        self.assertFalse(_is_stale_harness_freshness_error(
            "workspaces/demo/interns/generated/evidence/kpi_execution_harness.json: "
            "harness record #3 for `kpi_003` claims passed but fresh execution "
            "returned `failed` (possible tampered/stale manifest)"
        ))

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

    def test_validator_does_not_require_new_route_artifacts_for_old_workspaces(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            workspace = root / "workspaces" / "demo"
            (workspace / "docs").mkdir(parents=True)
            (workspace / "datasets").mkdir(parents=True)

            result = WorkspaceArtifactValidator(root, "workspaces/demo").run()

            self.assertTrue(result.ok)
            self.assertFalse(any("catalog_contract.json" in error for error in result.errors))
            self.assertFalse(any("data_engineering_route.json" in error for error in result.errors))
            self.assertFalse(any("pipeline_plan.json" in error for error in result.errors))
            self.assertFalse(any("pipeline_decisions.json" in error for error in result.errors))
            self.assertFalse(any("pipeline_layers.sql" in error for error in result.errors))

    def test_validator_rejects_malformed_catalog_and_pipeline_route_artifacts(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            workspace = root / "workspaces" / "demo"
            contracts = workspace / "interns" / "generated" / "contracts"
            contracts.mkdir(parents=True)
            (workspace / "docs").mkdir(parents=True)
            (workspace / "datasets").mkdir(parents=True)
            (contracts / "catalog_contract.json").write_text(
                json.dumps(
                    {
                        "artifact_type": "catalog_contract.json",
                        "version": 1,
                        "generated_by": "build-catalog-contract",
                        "catalog": "local_workspace",
                        "default_schema": "raw",
                        "objects": {},
                    }
                ),
                encoding="utf-8",
            )
            (contracts / "data_engineering_route.json").write_text(
                json.dumps(
                    {
                        "artifact_type": "data_engineering_route.json",
                        "version": 1,
                        "generated_by": "prepare-data-engineering-route",
                        "workspace": "workspaces/demo",
                        "selected_track": "kpi_only",
                        "target_engine": "bad_engine",
                        "start_layer": "raw",
                        "catalog_contract": "workspaces/demo/interns/generated/contracts/catalog_contract.json",
                        "catalog_object_count": "1",
                        "existing_layers": {},
                        "status": "ready_for_pipeline_plan",
                    }
                ),
                encoding="utf-8",
            )
            (contracts / "pipeline_plan.json").write_text(
                json.dumps(
                    {
                        "artifact_type": "pipeline_plan.json",
                        "version": 1,
                        "generated_by": "prepare-pipeline-plan",
                        "workspace": "workspaces/demo",
                        "selected_track": "kpi_only",
                        "target_engine": "sql",
                        "table_format": "duckdb_view",
                        "catalog_contract": "workspaces/demo/interns/generated/contracts/catalog_contract.json",
                        "route_contract": "workspaces/demo/interns/generated/contracts/data_engineering_route.json",
                        "policy": [],
                        "layers": {},
                        "quality_gates": {},
                        "blockers": {},
                        "status": "ready_for_generation",
                    }
                ),
                encoding="utf-8",
            )

            result = WorkspaceArtifactValidator(root, "workspaces/demo").run()

            self.assertFalse(result.ok)
            self.assertTrue(any("catalog_contract.json missing `workspace`" in error for error in result.errors))
            self.assertTrue(any("catalog_contract.json `objects` must be a list" in error for error in result.errors))
            self.assertTrue(any("data_engineering_route.json missing `requested_track`" in error for error in result.errors))
            self.assertTrue(any("data_engineering_route.json unsupported target_engine" in error for error in result.errors))
            self.assertTrue(any("pipeline_plan.json `policy` must be an object" in error for error in result.errors))
            self.assertTrue(any("pipeline_plan.json `layers` must be a list" in error for error in result.errors))

    def test_validator_rejects_malformed_pipeline_decisions(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            workspace = root / "workspaces" / "demo"
            contracts = workspace / "interns" / "generated" / "contracts"
            contracts.mkdir(parents=True)
            (workspace / "docs").mkdir(parents=True)
            (workspace / "datasets").mkdir(parents=True)
            (contracts / "pipeline_decisions.json").write_text(
                json.dumps(
                    {
                        "artifact_type": "wrong.json",
                        "generated_by": "apply-pipeline-decision",
                        "workspace": "workspaces/other",
                        "percentage_denominator_scopes": [],
                    }
                ),
                encoding="utf-8",
            )

            result = WorkspaceArtifactValidator(root, "workspaces/demo").run()

            self.assertFalse(result.ok)
            self.assertTrue(any("missing `artifact_type: pipeline_decisions.json`" in error for error in result.errors))

            (contracts / "pipeline_decisions.json").write_text(
                json.dumps(
                    {
                        "artifact_type": "pipeline_decisions.json",
                        "generated_by": "apply-pipeline-decision",
                        "workspace": "workspaces/other",
                        "percentage_denominator_scopes": [],
                    }
                ),
                encoding="utf-8",
            )

            result = WorkspaceArtifactValidator(root, "workspaces/demo").run()

            self.assertFalse(result.ok)
            self.assertTrue(any("pipeline_decisions.json missing `version`" in error for error in result.errors))
            self.assertTrue(any("pipeline_decisions.json workspace" in error for error in result.errors))
            self.assertTrue(
                any(
                    "pipeline_decisions.json `percentage_denominator_scopes` must be an object" in error
                    for error in result.errors
                )
            )

    def test_validator_rejects_empty_pipeline_sql(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            workspace = root / "workspaces" / "demo"
            pipeline = workspace / "interns" / "generated" / "pipeline"
            pipeline.mkdir(parents=True)
            (workspace / "docs").mkdir(parents=True)
            (workspace / "datasets").mkdir(parents=True)
            (pipeline / "pipeline_layers.sql").write_text(" \n", encoding="utf-8")

            result = WorkspaceArtifactValidator(root, "workspaces/demo").run()

            self.assertFalse(result.ok)
            self.assertTrue(any("generated pipeline SQL must be non-empty" in error for error in result.errors))

    def test_validator_rejects_raw_pipeline_sql_paths_outside_bootstrap(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            workspace = root / "workspaces" / "demo"
            pipeline = workspace / "interns" / "generated" / "pipeline"
            pipeline.mkdir(parents=True)
            (workspace / "docs").mkdir(parents=True)
            (workspace / "datasets").mkdir(parents=True)
            (pipeline / "pipeline_layers.sql").write_text(
                "\n".join(
                    [
                        "-- BEGIN CATALOG BOOTSTRAP",
                        "CREATE VIEW raw_claims AS SELECT * FROM read_csv_auto('workspaces/demo/datasets/claims.csv');",
                        "-- END CATALOG BOOTSTRAP",
                        "CREATE VIEW bronze_claims AS SELECT * FROM read_csv_auto('workspaces/demo/datasets/claims.csv');",
                        "",
                    ]
                ),
                encoding="utf-8",
            )

            result = WorkspaceArtifactValidator(root, "workspaces/demo").run()

            self.assertFalse(result.ok)
            self.assertTrue(
                any(
                    "raw dataset path references are only allowed inside CATALOG BOOTSTRAP" in error
                    for error in result.errors
                )
            )

    def test_validator_rejects_pipeline_sql_raw_paths_without_bootstrap(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            workspace = root / "workspaces" / "demo"
            pipeline = workspace / "interns" / "generated" / "pipeline"
            pipeline.mkdir(parents=True)
            (workspace / "docs").mkdir(parents=True)
            (workspace / "datasets").mkdir(parents=True)
            (pipeline / "pipeline_layers.sql").write_text(
                "CREATE VIEW raw_claims AS SELECT * FROM read_csv_auto('workspaces/demo/datasets/claims.csv');\n",
                encoding="utf-8",
            )

            result = WorkspaceArtifactValidator(root, "workspaces/demo").run()

            self.assertFalse(result.ok)
            self.assertTrue(any("missing BEGIN/END CATALOG BOOTSTRAP" in error for error in result.errors))


if __name__ == "__main__":
    unittest.main()
