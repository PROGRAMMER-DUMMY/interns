import json
import os
import tempfile
import unittest
from pathlib import Path

from core.execution.backend import normalize_command
from core.execution.backend import DuckDBBackend, build_execution_backend
from core.config import Config, DatabricksConfig
from core.governance.contracts import OptimizationPolicy
from core.governance.evaluator import GovernanceEvaluator
from core.governance.mode_policy import ModePlanner
from core.governance.semantic_contract import SemanticContract
from core.onboarding.auto_bootstrap import AutoBootstrap
from core.onboarding.workspace_onboarding import (
    WorkspaceOnboarder,
    find_root_artifact_violations,
)
from core.optimization.change_classifier import classify_diff
from core.optimization.memory import OptimizationMemory, OptimizationMemoryRecord
from core.optimization.planner import OptimizationPlanner
from core.profiling.data_model_profiler import DataModelProfiler
from core.storage.metadata_store import (
    DeltaMetadataStore,
    LocalMetadataStore,
    MongoMetadataStore,
    build_metadata_store,
)
from core.storage.workspace import Workspace
from core.storage.workspace_layout import WorkspaceLayout


class EnterpriseOptimizationTests(unittest.TestCase):
    def _create_demo_workspace(self, root: Path) -> Path:
        workspace = root / "workspaces" / "demo"
        (workspace / "datasets").mkdir(parents=True)
        (workspace / "docs").mkdir(parents=True)
        (workspace / "datasets" / "transactions.csv").write_text(
            "ClaimID,PaidAmount,LineOfBusiness\n"
            "C1,10.50,Commercial\n"
            "C2,20.25,Medicare\n",
            encoding="utf-8",
        )
        (workspace / "docs" / "kpi_registry.csv").write_text(
            "Key business question,Description,Cuts / grain hints,Metric,Data model refinement required\n"
            "What is paid amount by line of business?,Baseline KPI,LineOfBusiness,sum(PaidAmount),Confirm paid amount source\n",
            encoding="utf-8",
        )
        (workspace / "docs" / "data_model.md").write_text(
            "# Data Model\n\ntransactions has ClaimID, PaidAmount, LineOfBusiness.\n",
            encoding="utf-8",
        )
        return workspace

    def test_command_normalization_accepts_legacy_strings(self):
        self.assertEqual(
            normalize_command("uv run python tests/06_sql_optimization/experiment.py"),
            ["uv", "run", "python", "tests/06_sql_optimization/experiment.py"],
        )
        self.assertEqual(normalize_command(["uv", "run"]), ["uv", "run"])

    def test_change_classifier_labels_sql_diff(self):
        diff = """
diff --git a/model.sql b/model.sql
+WHERE Tot_Clms > 0
-JOIN old_table o ON x.id = o.id
+JOIN new_table n ON x.id = n.id
"""
        result = classify_diff(diff, "model.sql")
        self.assertEqual(result.primary_type, "predicate_pushdown")
        self.assertIn("join_rewrite", result.change_types)

    def test_semantic_contract_reads_registry_and_methodology(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            registry = root / "registry.md"
            registry.write_text(
                """
| KPI Name | Definition |
| :--- | :--- |
| **Cost Per Claim** | `Tot_Drug_Cst / Tot_Clms` |

## Transformation Guardrails
1. **NPI Uniqueness**: one row per NPI.
""",
                encoding="utf-8",
            )
            methodology = root / "methodology.json"
            methodology.write_text(
                json.dumps({
                    "columns": {
                        "Prscrbr_NPI": {
                            "expected_type": "string",
                            "is_primary_key": True,
                        }
                    },
                    "relationships": [],
                }),
                encoding="utf-8",
            )
            contract = SemanticContract.from_task(
                {
                    "id": "task",
                    "semantic_contract": {
                        "kpi_registry": "registry.md",
                        "methodology_json": "methodology.json",
                    },
                },
                root,
            )
            summary = contract.summary()
            self.assertEqual(summary["kpi_count"], 1)
            self.assertGreaterEqual(summary["rule_count"], 2)

    def test_memory_records_pattern_stats(self):
        workspace = Workspace(":memory:")
        memory = OptimizationMemory(workspace)
        memory.record(
            OptimizationMemoryRecord(
                run_id="exp_1",
                task_id="task",
                artifact="model.sql",
                change_type="predicate_pushdown",
                change_types=["predicate_pushdown"],
                expected_reason="reduce rows",
                actual_result="improved",
                decision="keep",
                baseline_metric=1.0,
                candidate_metric=2.0,
                metric_delta=1.0,
                direction="higher",
                correctness_passed=True,
            )
        )
        stats = memory.pattern_stats()
        self.assertEqual(stats[0]["change_type"], "predicate_pushdown")
        self.assertEqual(stats[0]["success_rate"], 1.0)

    def test_planner_uses_hotspot_suggestions(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            hotspot_path = root / "hotspots.json"
            hotspot_path.write_text(
                json.dumps({
                    "suggestions": [
                        {"type": "repeated_scan", "message": "table scanned repeatedly"}
                    ]
                }),
                encoding="utf-8",
            )
            workspace = Workspace(":memory:")
            planner = OptimizationPlanner(OptimizationMemory(workspace), root)
            plan = planner.build_plan(
                {"id": "task", "hotspots_file": "hotspots.json"},
                SemanticContract(name="task"),
            )
            self.assertEqual(plan.recommended_strategy, "cte_rewrite")

    def test_mode_policy_blocks_global_promotion_by_default(self):
        policy = OptimizationPolicy.from_task({})
        plan = ModePlanner(policy).build_plan("global_exploration")
        self.assertEqual(plan.active_mode, "global_exploration")
        self.assertFalse(plan.promotion_allowed)
        self.assertIn("global_exploration_no_auto_promotion", plan.warnings)

    def test_governance_records_review_decision_and_alert(self):
        policy = OptimizationPolicy.from_task({})
        mode_plan = ModePlanner(policy).build_plan("sql")
        classification = classify_diff("+WHERE amount > 0", "model.sql")
        contract = SemanticContract(name="task")
        decision = GovernanceEvaluator(policy).evaluate(
            run_id="exp_1",
            task={"id": "task", "editable_file": "model.sql", "direction": "higher"},
            status="keep",
            baseline_metric=1.0,
            candidate_metric=2.0,
            metric_delta=1.0,
            execution_time_seconds=1.5,
            matching_score=100.0,
            classification=classification,
            semantic_contract=contract,
            mode_plan=mode_plan,
            artifact_diff="+WHERE amount > 0",
        )
        self.assertEqual(decision.decision, "needs_review")
        self.assertEqual(decision.approval_state, "pending_human")
        self.assertTrue(decision.alerts)

        workspace = Workspace(":memory:")
        workspace.log_governance_decision(decision.summary())
        stored = workspace.get_recent_governance_decisions()
        alerts = workspace.get_recent_alerts()
        self.assertEqual(stored[0]["decision"], "needs_review")
        self.assertEqual(alerts[0]["severity"], "review")

    def test_data_model_profiler_records_exact_bounds_and_downcast(self):
        try:
            import polars as pl
        except ImportError:
            self.skipTest("polars is not installed")

        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "data.parquet"
            pl.DataFrame({
                "small_id": [1, 2, 3],
                "amount": [1.1, 2.2, 3.3],
            }).write_parquet(path)
            profile = DataModelProfiler().profile_path(path, exact=True)
            by_name = {col.name: col for col in profile.columns}
            self.assertEqual(by_name["small_id"].exact_min, 1)
            self.assertEqual(by_name["small_id"].exact_max, 3)
            recs = {rec.column: rec for rec in profile.downcast_recommendations}
            self.assertEqual(recs["small_id"].decision, "recommend")
            self.assertEqual(recs["amount"].decision, "approval_required")

    def test_workspace_layout_groups_outputs_under_interns(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            layout = WorkspaceLayout.from_task(
                {
                    "editable_file": "workspaces/demo_project/src/model.sql",
                },
                root,
            )
            self.assertEqual(layout.project_root, root / "workspaces" / "demo_project")
            self.assertEqual(layout.interns_dir, layout.project_root / "interns")
            self.assertEqual(layout.workspace_db, layout.interns_dir / "state" / "workspace.db")
            self.assertEqual(layout.run_log, layout.interns_dir / "state" / "run.log")

            explicit = WorkspaceLayout.from_task(
                {
                    "workspace": "workspaces/explicit_project",
                    "editable_file": "somewhere/else/model.sql",
                },
                root,
            )
            self.assertEqual(explicit.project_root, root / "workspaces" / "explicit_project")
            explicit.ensure_runtime_dirs()
            self.assertTrue(explicit.solutions_dir.exists())
            self.assertTrue(explicit.requirements_dir.exists())
            self.assertTrue(explicit.memory_dir.exists())
            self.assertTrue(explicit.evaluation_dir.exists())

    def test_workspace_onboarding_generates_fresh_workspace_artifacts(self):
        try:
            import polars  # noqa: F401
        except ImportError:
            self.skipTest("polars is not installed")

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            workspace = self._create_demo_workspace(root)

            result = WorkspaceOnboarder(root, "workspaces/demo", sample_rows=10).run()

            self.assertEqual(result.kpi_count, 1)
            self.assertEqual(result.profile_count, 1)
            self.assertTrue((workspace / "interns" / "generated" / "solutions" / "kpi_metrics.sql").exists())
            self.assertTrue((workspace / "interns" / "evaluation" / "experiment.py").exists())
            self.assertTrue((workspace / "interns" / "evaluation" / "evaluator.py").exists())
            self.assertTrue((workspace / "interns" / "generated" / "contracts" / "semantic_contract.json").exists())
            self.assertTrue((workspace / "interns" / "generated" / "profiles" / "profile_index.json").exists())
            self.assertTrue((workspace / "interns" / "state" / "delta_metadata" / "contracts" / "_delta_log").exists())
            self.assertTrue((workspace / "interns" / "state" / "delta_metadata" / "profiles" / "_delta_log").exists())
            self.assertTrue((workspace / "interns" / "reports" / "open_questions.md").exists())
            self.assertEqual(find_root_artifact_violations(workspace), [])

            baseline_sql = (
                workspace / "interns" / "generated" / "solutions" / "kpi_metrics.sql"
            ).read_text(encoding="utf-8")
            self.assertIn("kpi_baseline_manifest", baseline_sql)
            self.assertIn("What is paid amount by line of business?", baseline_sql)

    def test_workspace_root_artifact_policy_flags_generated_files(self):
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp) / "workspaces" / "demo"
            workspace.mkdir(parents=True)
            (workspace / "kpi_metrics.sql").write_text("select 1;", encoding="utf-8")
            (workspace / "analytics.duckdb").write_text("", encoding="utf-8")
            violations = find_root_artifact_violations(workspace)
            self.assertEqual(len(violations), 2)
            self.assertTrue(any(path.endswith("kpi_metrics.sql") for path in violations))
            self.assertTrue(any(path.endswith("analytics.duckdb") for path in violations))

    def test_auto_bootstrap_generates_then_reuses_current_artifacts(self):
        try:
            import polars  # noqa: F401
        except ImportError:
            self.skipTest("polars is not installed")

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            workspace = self._create_demo_workspace(root)
            task = {"id": "demo", "workspace": "workspaces/demo"}

            first = AutoBootstrap(root, task).ensure_ready()
            second = AutoBootstrap(root, task).ensure_ready()

            self.assertEqual(first.action, "generated")
            self.assertEqual(second.action, "reuse")
            self.assertEqual(second.reason, "artifacts_current")
            self.assertTrue((workspace / "interns" / "state" / "bootstrap_manifest.json").exists())

    def test_auto_bootstrap_regenerates_when_input_fingerprint_changes(self):
        try:
            import polars  # noqa: F401
        except ImportError:
            self.skipTest("polars is not installed")

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            workspace = self._create_demo_workspace(root)
            task = {"id": "demo", "workspace": "workspaces/demo"}

            first = AutoBootstrap(root, task).ensure_ready()
            (workspace / "docs" / "kpi_registry.csv").write_text(
                "Key business question,Description,Cuts / grain hints,Metric,Data model refinement required\n"
                "What is paid amount by line of business?,Baseline KPI,LineOfBusiness,sum(PaidAmount),Confirm paid amount source\n"
                "What is claim count?,Count KPI,LineOfBusiness,count(ClaimID),Confirm claim grain\n",
                encoding="utf-8",
            )
            second = AutoBootstrap(root, task).ensure_ready()

            self.assertEqual(first.action, "generated")
            self.assertEqual(second.action, "generated")
            self.assertEqual(second.reason, "input_fingerprint_changed")
            contract = json.loads(
                (workspace / "interns" / "generated" / "contracts" / "semantic_contract.json")
                .read_text(encoding="utf-8")
            )
            self.assertEqual(contract["kpi_count"], 2)

    def test_databricks_backend_requires_explicit_remote_approval(self):
        os.environ.pop("AUTORESEARCH_ALLOW_REMOTE_EXECUTION", None)
        cfg = Config(
            databricks=DatabricksConfig(
                enabled=True,
                execution="jobs",
                host="https://example.cloud.databricks.com",
                token="token",
            )
        )
        self.assertIsInstance(build_execution_backend(cfg), DuckDBBackend)

    def test_local_metadata_store_writes_structured_document(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = LocalMetadataStore(Path(tmp) / "metadata")
            result = store.upsert(
                "contracts",
                "semantic_contract",
                {"kpi_count": 2},
                workspace="workspaces/demo",
            )
            self.assertEqual(result.backend, "local")
            path = Path(result.path)
            self.assertTrue(path.exists())
            payload = json.loads(path.read_text(encoding="utf-8"))
            self.assertEqual(payload["workspace"], "workspaces/demo")
            self.assertEqual(payload["payload"]["kpi_count"], 2)

    def test_delta_metadata_store_writes_structured_table(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = DeltaMetadataStore(Path(tmp) / "delta")
            result = store.upsert(
                "contracts",
                "semantic_contract",
                {"kpi_count": 2},
                workspace="workspaces/demo",
            )
            self.assertEqual(result.backend, "delta")
            table_path = Path(result.path)
            self.assertTrue((table_path / "_delta_log").exists())

            from deltalake import DeltaTable

            rows = DeltaTable(str(table_path)).to_pyarrow_table().to_pylist()
            self.assertEqual(rows[0]["workspace"], "workspaces/demo")
            self.assertEqual(rows[0]["document_id"], "semantic_contract")
            self.assertIn('"kpi_count": 2', rows[0]["payload_json"])

    def test_mongo_metadata_store_falls_back_to_local_when_unavailable(self):
        with tempfile.TemporaryDirectory() as tmp:
            fallback = LocalMetadataStore(Path(tmp) / "metadata")
            store = MongoMetadataStore("mongodb://127.0.0.1:1", local_fallback=fallback)
            result = store.upsert(
                "contracts",
                "semantic_contract",
                {"kpi_count": 2},
                workspace="workspaces/demo",
            )
            self.assertIn("mongo->local", result.backend)
            self.assertIsNotNone(result.warning)
            self.assertTrue(Path(result.path).exists())

    def test_build_metadata_store_uses_local_without_mongo_uri(self):
        with tempfile.TemporaryDirectory() as tmp:
            old_backend = os.environ.get("AUTORESEARCH_METADATA_BACKEND")
            old_uri = os.environ.get("AUTORESEARCH_MONGO_URI")
            try:
                os.environ["AUTORESEARCH_METADATA_BACKEND"] = "mongo"
                os.environ.pop("AUTORESEARCH_MONGO_URI", None)
                layout = WorkspaceLayout(project_root=Path(tmp) / "workspaces" / "demo")
                layout.ensure_runtime_dirs()
                self.assertIsInstance(build_metadata_store(layout), DeltaMetadataStore)
            finally:
                if old_backend is None:
                    os.environ.pop("AUTORESEARCH_METADATA_BACKEND", None)
                else:
                    os.environ["AUTORESEARCH_METADATA_BACKEND"] = old_backend
                if old_uri is None:
                    os.environ.pop("AUTORESEARCH_MONGO_URI", None)
                else:
                    os.environ["AUTORESEARCH_MONGO_URI"] = old_uri

    def test_build_metadata_store_defaults_to_delta(self):
        with tempfile.TemporaryDirectory() as tmp:
            old_backend = os.environ.get("AUTORESEARCH_METADATA_BACKEND")
            try:
                os.environ.pop("AUTORESEARCH_METADATA_BACKEND", None)
                layout = WorkspaceLayout(project_root=Path(tmp) / "workspaces" / "demo")
                layout.ensure_runtime_dirs()
                self.assertIsInstance(build_metadata_store(layout), DeltaMetadataStore)
            finally:
                if old_backend is None:
                    os.environ.pop("AUTORESEARCH_METADATA_BACKEND", None)
                else:
                    os.environ["AUTORESEARCH_METADATA_BACKEND"] = old_backend


if __name__ == "__main__":
    unittest.main()
