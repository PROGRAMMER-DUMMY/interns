import json
import tempfile
import unittest
from pathlib import Path

from core.execution.backend import normalize_command
from core.governance.contracts import OptimizationPolicy
from core.governance.evaluator import GovernanceEvaluator
from core.governance.mode_policy import ModePlanner
from core.governance.semantic_contract import SemanticContract
from core.optimization.change_classifier import classify_diff
from core.optimization.memory import OptimizationMemory, OptimizationMemoryRecord
from core.optimization.planner import OptimizationPlanner
from core.profiling.data_model_profiler import DataModelProfiler
from core.storage.workspace import Workspace


class EnterpriseOptimizationTests(unittest.TestCase):
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


if __name__ == "__main__":
    unittest.main()
