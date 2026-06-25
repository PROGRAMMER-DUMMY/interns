from __future__ import annotations

import io
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest import mock

from core.medallion.budget import BudgetExceeded, BudgetTracker
from core.execution.backend import ExecutionResult
from core.medallion.build import build_medallion
from core.medallion.databricks_target import CompromiseHistory
from core.medallion.delta_emitter import emit_silver_spark
from core.medallion.contracts_md import render_lineage_md
from core.medallion.lineage import Lineage, LineageEdge, LineageNode
from core.medallion.design import (
    EXIT_MODEL_SEARCH_FAILED,
    MedallionExit,
    _emit_silver_sql_duckdb,
    design_medallion,
)
from core.medallion.manifest import BronzeTable, Manifest, SilverTable, manifest_to_yaml
from core.medallion.model_classifier import ModelCapability
from core.medallion.run_state import RunState
from core.medallion.salt_store import _init_salt_cli
from core.medallion.silver_contract import SilverContract, TableContract
from core.medallion.tier_router import assign_tiers, pick_model, rank_models, should_deescalate


class MedallionP2P3Tests(unittest.TestCase):
    def test_silver_spark_hashes_pii_with_workspace_salt_udf(self):
        with tempfile.TemporaryDirectory() as tmp:
            out_dir = Path(tmp)
            table = SilverTable(
                name="patients",
                derived_from=["bronze.patients__hospital_a"],
                primary_key=["PatientID"],
            )
            contract = SilverContract(
                workspace="demo",
                tables={
                    "patients": TableContract(pii_hash_columns=["PatientName"]),
                },
            )

            path = emit_silver_spark(table, out_dir, "demo-workspace", contract)
            generated = path.read_text(encoding="utf-8")

            self.assertIn('key="medallion_salt__demo-workspace"', generated)
            self.assertIn('spark.udf.register("get_workspace_salt"', generated)
            self.assertIn("get_workspace_salt()", generated)
            self.assertIn("sha2(concat(coalesce(cast(PatientName AS string), ''), get_workspace_salt()), 256)", generated)
            self.assertNotIn("replace with salt UDF", generated)
            self.assertNotIn('F.sha2(F.coalesce(F.col("PatientName").cast("string"), F.lit("")), 256)', generated)

    def test_silver_duckdb_hashes_pii_with_bound_salt_parameter(self):
        with tempfile.TemporaryDirectory() as tmp:
            out_dir = Path(tmp)
            manifest = Manifest(
                workspace="demo",
                inputs_hash="sha256:test",
                silver=[
                    SilverTable(
                        name="patients",
                        derived_from=["bronze.patients__hospital_a"],
                        primary_key=["PatientID"],
                    )
                ],
            )
            contract = SilverContract(
                workspace="demo",
                tables={"patients": TableContract(pii_hash_columns=["PatientName"])},
            )

            [path] = _emit_silver_sql_duckdb(manifest, contract, out_dir)
            generated = path.read_text(encoding="utf-8")

            # The star may carry a canonical-PK rename / EXCLUDE before REPLACE,
            # so `*` and `REPLACE` are no longer adjacent -- assert the REPLACE
            # clause exists rather than the exact `* REPLACE` adjacency.
            self.assertIn("REPLACE (", generated)
            self.assertIn("sha256(coalesce(cast(PatientName AS VARCHAR), '') || $salt) AS PatientName", generated)
            self.assertNotIn("{salt}", generated)

    def test_init_salt_cli_does_not_print_salt_material(self):
        secret = "abc12345" * 8
        stdout = io.StringIO()
        with mock.patch("core.medallion.salt_store.materialize_salt_if_missing", return_value=secret):
            with redirect_stdout(stdout):
                code = _init_salt_cli(["--workspace", "demo"])

        self.assertEqual(code, 0)
        output = stdout.getvalue()
        self.assertIn("Salt ready for workspace `demo`", output)
        self.assertNotIn(secret, output)
        self.assertNotIn(secret[:8], output)

    def test_run_state_records_target_override_and_compromise_fields(self):
        state = RunState(
            run_id="run-1",
            manifest_hash="sha256:test",
            target_declared="delta",
            target_actual="duckdb",
            started_at="2026-05-27T00:00:00+00:00",
            degraded_run=True,
            fallback_reason="Databricks unavailable",
            compromise_history=[{"target": "delta", "exit_code": 1}],
        )

        payload = state.to_dict()

        self.assertEqual(payload["target_declared"], "delta")
        self.assertEqual(payload["target_actual"], "duckdb")
        self.assertTrue(payload["degraded_run"])
        self.assertEqual(payload["fallback_reason"], "Databricks unavailable")
        self.assertEqual(payload["compromise_history"][0]["target"], "delta")

    def test_lineage_markdown_includes_gold_column_traces(self):
        lineage = Lineage(
            workspace="demo",
            nodes=[
                LineageNode(layer="bronze", table="patients", columns=["PatientID"]),
                LineageNode(layer="silver", table="patient", columns=["PatientID"]),
                LineageNode(layer="gold", table="fact_patient", columns=["PatientID"]),
            ],
            edges=[
                LineageEdge(
                    from_node="bronze.patients",
                    from_columns=["PatientID"],
                    to_node="silver.patient",
                    to_columns=["PatientID"],
                ),
                LineageEdge(
                    from_node="silver.patient",
                    from_columns=["PatientID"],
                    to_node="gold.fact_patient",
                    to_columns=["PatientID"],
                ),
            ],
        )

        markdown = render_lineage_md(lineage)

        self.assertIn("## Gold Column Traces", markdown)
        self.assertIn("`bronze.patients.PatientID`", markdown)

    def test_budget_tracker_loads_persisted_spend_for_resume(self):
        with tempfile.TemporaryDirectory() as tmp:
            state_path = Path(tmp) / "budget.json"
            first = BudgetTracker(0.01, state_path=state_path)
            first.charge(engine="api", model_id="claude-sonnet-4-6", in_tokens=1_000, out_tokens=1_000)

            resumed = BudgetTracker(0.001, state_path=state_path)

            self.assertGreater(resumed.spent, 0)
            self.assertEqual(len(resumed.history), 1)
            with self.assertRaises(BudgetExceeded):
                resumed.check(tables_done=0, tables_total=2)

    def test_tier_router_picks_cheapest_tier_that_meets_task_minimum(self):
        capabilities = [
            ModelCapability(engine="api", model_id="light", benchmark_composite=0.1),
            ModelCapability(engine="api", model_id="medium", benchmark_composite=0.5),
            ModelCapability(engine="api", model_id="heavy", benchmark_composite=0.9),
        ]

        assignment = assign_tiers(rank_models(capabilities))

        self.assertEqual(pick_model("bronze_append_sql", assignment), ("light", "light"))
        self.assertEqual(pick_model("star_schema_design", assignment), ("medium", "medium"))
        self.assertTrue(should_deescalate(0.7, 0.2, cache_near_hit=False, cheap_mode=False))

    def test_build_delta_without_databricks_degrades_to_duckdb(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            workspace = root / "workspaces" / "demo"
            medallion = workspace / "interns" / "generated" / "medallion"
            medallion.mkdir(parents=True)
            (medallion / "manifest.yaml").write_text(
                manifest_to_yaml(Manifest(workspace="demo", inputs_hash="sha256:test")),
                encoding="utf-8",
            )
            (medallion / "silver_contract.json").write_text('{"workspace": "demo"}', encoding="utf-8")

            with mock.patch("core.medallion.mlflow_emit.start_run", return_value=None):
                with mock.patch("core.medallion.mlflow_emit.finalize_run", return_value=None):
                    state = build_medallion(
                        workspace,
                        root,
                        cfg=None,
                        target_override="delta",
                        force_with_blockers=True,
                    )

            self.assertEqual(state.target_declared, "delta")
            self.assertEqual(state.target_actual, "duckdb")
            self.assertTrue(state.degraded_run)
            self.assertIn("Databricks is not configured", state.fallback_reason)

    def test_build_delta_executes_spark_files_through_databricks_wrapper(self):
        class FakeDatabricks:
            execution = "connect"
            fallback = "duckdb"

            def is_active(self):
                return True

        class FakeConfig:
            databricks = FakeDatabricks()
            max_run_seconds = 5
            hard_timeout_seconds = 5

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            workspace = root / "workspaces" / "demo"
            medallion = workspace / "interns" / "generated" / "medallion"
            bronze_dir = medallion / "bronze"
            bronze_dir.mkdir(parents=True)
            (medallion / "manifest.yaml").write_text(
                manifest_to_yaml(
                    Manifest(
                        workspace="demo",
                        inputs_hash="sha256:test",
                        bronze=[
                            BronzeTable(
                                name="patients__hospital_a",
                                source_file="workspaces/demo/datasets/patients.csv",
                                source_system="hospital_a",
                            )
                        ],
                    )
                ),
                encoding="utf-8",
            )
            (medallion / "silver_contract.json").write_text('{"workspace": "demo"}', encoding="utf-8")
            (bronze_dir / "patients__hospital_a.spark.py").write_text("print('ok')\n", encoding="utf-8")

            def fake_execute(task, time_budget, hard_timeout, log_path, cfg):
                self.assertTrue(str(task["editable_file"]).endswith("patients__hospital_a.spark.py"))
                return ExecutionResult(0, "ok", 0.01), CompromiseHistory(
                    attempts=[{"target": "connect", "exit_code": 0}]
                )

            with mock.patch("core.medallion.mlflow_emit.start_run", return_value=None):
                with mock.patch("core.medallion.mlflow_emit.finalize_run", return_value=None):
                    with mock.patch(
                        "core.medallion.databricks_target.execute_with_downsize_then_fallback",
                        side_effect=fake_execute,
                    ):
                        state = build_medallion(
                            workspace,
                            root,
                            cfg=FakeConfig(),
                            target_override="delta",
                            force_with_blockers=True,
                        )

            self.assertEqual(state.target_actual, "delta")
            self.assertFalse(state.degraded_run)
            self.assertEqual(state.per_table_status["bronze.patients__hospital_a"].status, "ok")
            self.assertEqual(state.compromise_history[0]["target"], "connect")

    def test_design_medallion_writes_p4_model_routing_metadata_for_pinned_model(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            workspace = self._minimal_onboarded_workspace(root)

            design_medallion(
                workspace,
                root,
                cheap=True,
                force=True,
                engine="gemini-api",
                model="gemma-4",
            )

            metadata_path = workspace / "interns" / "state" / "medallion" / "discovered_models_latest.json"
            metadata = metadata_path.read_text(encoding="utf-8")
            self.assertIn('"model_id": "gemma-4"', metadata)
            self.assertIn('"task_class": "star_schema_design"', metadata)

    def test_design_medallion_no_search_fails_on_uncached_model(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            workspace = self._minimal_onboarded_workspace(root)

            with self.assertRaises(MedallionExit) as ctx:
                design_medallion(
                    workspace,
                    root,
                    cheap=True,
                    force=True,
                    engine="gemini-api",
                    model="uncached-model",
                    no_search=True,
                )

            self.assertEqual(ctx.exception.code, EXIT_MODEL_SEARCH_FAILED)

    def _minimal_onboarded_workspace(self, root: Path) -> Path:
        workspace = root / "workspaces" / "demo"
        contracts = workspace / "interns" / "generated" / "contracts"
        profiles = workspace / "interns" / "generated" / "profiles"
        contracts.mkdir(parents=True)
        profiles.mkdir(parents=True)
        (contracts / "domain_model.json").write_text('{"datasets": []}', encoding="utf-8")
        (contracts / "kpi_registry.json").write_text('{"kpis": [{"id": "kpi_001"}]}', encoding="utf-8")
        (contracts / "kpi_feature_mapping.json").write_text("{}", encoding="utf-8")
        (contracts / "semantic_contract.json").write_text("{}", encoding="utf-8")
        (contracts / "workspace_feature_definitions.json").write_text("{}", encoding="utf-8")
        (profiles / "profile_index.json").write_text("{}", encoding="utf-8")
        return workspace


if __name__ == "__main__":
    unittest.main()
