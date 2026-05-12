"""
core/loop.py — autonomous experiment loop.
"""
# ruff: noqa: E402
import json
import argparse
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from core.optimization.change_classifier import classify_diff, expected_reason
from core.config import Config, load as load_config
from core.governance.contracts import OptimizationPolicy
from core.execution.backend import ExecutionBackend, build_execution_backend
from core.governance.evaluator import GovernanceEvaluator
from core.agents.intern_bus import InternBus
from core.governance.mode_policy import ModePlanner
from core.optimization.memory import (
    OptimizationMemory,
    OptimizationMemoryRecord,
    describe_actual_result,
)
from core.optimization.planner import OptimizationPlanner
from core.observability.parser import RegexLogParser
from core.governance.semantic_contract import SemanticContract
from core.optimization.strategy import SingleMetricDecisionStrategy
from core.observability.telemetry_backend import build_telemetry_backend
from core.storage.workspace import Workspace

HEADER = "commit\tprimary_metric\tstatus\tdescription"

class ExperimentLoop:
    def __init__(self, cfg: Optional[Config] = None, task_id: str = "06_sql_optimization"):
        self.cfg = cfg or load_config()
        self.workspace = Workspace(self.cfg.workspace_db)

        # Build backends from config (Databricks if configured, else local)
        self.backend: ExecutionBackend = build_execution_backend(self.cfg)
        self.local_telemetry, self.db_telemetry = build_telemetry_backend(self.cfg, self.workspace)

        # InternBus gets the Databricks telemetry for LLM tracing (or None)
        self.bus = InternBus(self.cfg, telemetry=self.db_telemetry)
        
        self.tasks_path = ROOT / "config" / "tasks.json"
        self._load_task(task_id)

        self.memory = OptimizationMemory(self.workspace)
        self.planner = OptimizationPlanner(self.memory, ROOT)
        self.semantic_contract = SemanticContract.from_task(self.task, ROOT)
        self.optimization_policy = OptimizationPolicy.from_task(self.task)
        self.mode_planner = ModePlanner(self.optimization_policy)
        self.governance = GovernanceEvaluator(self.optimization_policy)
        self.metric_parser = RegexLogParser()
                 
        self.decision_strategy = SingleMetricDecisionStrategy()
        
        self._running = False
        self._state = {
            "experiment_count":    0,
            "consecutive_discards": 0,
            "best_metric":         None,
            "best_commit":         None,
            "session_start":       datetime.now(timezone.utc).isoformat(),
        }

    def _load_task(self, task_id: str):
        if not self.tasks_path.exists():
            raise FileNotFoundError("config/tasks.json missing")
        data = json.loads(self.tasks_path.read_text(encoding="utf-8"))
        for t in data.get("tasks", []):
            if t["id"] == task_id:
                self.task = t
                return
        raise ValueError(f"Task '{task_id}' not found in tasks.json")

    def start(self, mode: str = "auto") -> None:
        self._running = True
        self.cfg.state_dir.mkdir(parents=True, exist_ok=True)
        self._write_status("running")

        print(f"\n[loop] === {self.task['name']} ===", flush=True)
        while self._running and self._state["experiment_count"] < self.cfg.max_experiments_session:
            self._run_one(mode)
        self._finish()

    def stop(self) -> None:
        self._running = False

    def _run_one(self, mode: str) -> None:
        n = self._state["experiment_count"] + 1
        run_name = f"exp_{n}"
        print(f"\n[loop] Experiment {n}  |  best: {self._state['best_metric']}", flush=True)

        # Begin telemetry run (starts MLflow run when Databricks is active)
        self.local_telemetry.begin_run(run_name)
        if self.db_telemetry:
            self.db_telemetry.begin_run(run_name)

        mode_plan = self.mode_planner.build_plan(None if mode in {"auto", "semi"} else mode)
        plan = self.planner.build_plan(self.task, self.semantic_contract)
        active_interns = self.bus.list_active(self.task["domain"])
        intern_reports = []

        for intern in active_interns:
            report = self.bus.invoke(intern, "Analyze and suggest improvement", {
                "experiment_count": n,
                "best_metric": self._state["best_metric"],
                "editable_file": self.task["editable_file"],
                "results_tsv": self._read_results(),
                "run_log": self.cfg.run_log.read_text(encoding="utf-8") if self.cfg.run_log.exists() else "",
                "optimization_plan": plan.as_prompt_context(),
                "optimization_memory": self.memory.as_prompt_context(),
                "semantic_contract": json.dumps(self.semantic_contract.summary(), indent=2),
                "optimization_policy": json.dumps(self.optimization_policy.summary(), indent=2),
                "mode_plan": json.dumps(mode_plan.as_dict(), indent=2),
            })
            intern_reports.append(report)

        # Execute via configured backend (DuckDB | Jobs | Warehouse | Connect)
        from core.execution.backend import ExecutionResult
        result: ExecutionResult = self.backend.execute(
            self.task, self.cfg.max_run_seconds, self.cfg.hard_timeout_seconds, self.cfg.run_log
        )

        baseline_metric = self._state["best_metric"]
        artifact_diff = self.workspace.diff_file(self.task.get("editable_file", ""))
        classification = classify_diff(artifact_diff, self.task.get("editable_file", ""))
        status = self.decision_strategy.decide(result.metric, self._state, self.task)
        all_metrics = self.metric_parser.parse_all_metrics(result.log_content)
        matching_score = all_metrics.get("matching_score")
        execution_time = all_metrics.get("execution_time_seconds", result.elapsed_seconds)
        metric_delta, _actual_result = describe_actual_result(
            baseline_metric,
            result.metric,
            self.task.get("direction", "higher"),
            status,
        )
        governance_decision = self.governance.evaluate(
            run_id=run_name,
            task=self.task,
            status=status,
            baseline_metric=baseline_metric,
            candidate_metric=result.metric,
            metric_delta=metric_delta,
            execution_time_seconds=execution_time,
            matching_score=matching_score,
            classification=classification,
            semantic_contract=self.semantic_contract,
            mode_plan=mode_plan,
            artifact_diff=artifact_diff,
            planner_evidence=plan.evidence,
            profiler_evidence={"parsed_metrics": all_metrics},
        )
        self.workspace.log_governance_decision(governance_decision.summary())
        status = self._apply_decision(status, result.metric, governance_decision)
        desc = intern_reports[0].splitlines()[0][:80] if intern_reports else "no reports"
        self._log_result(n, result.metric, status, desc)
        self._record_optimization_memory(
            run_name,
            baseline_metric,
            result,
            status,
            classification,
            plan,
            governance_decision,
            all_metrics,
        )

        # Flush telemetry (logs metrics, ends MLflow run)
        tel_params = {
            "execution_time_seconds": result.elapsed_seconds,
            "token_count": result.token_count,
            "description": desc,
            "backend": type(self.backend).__name__,
        }
        self.local_telemetry.end_run(result.metric, tel_params, status)
        if self.db_telemetry:
            try:
                self.db_telemetry.end_run(result.metric, tel_params, status)
                if result.telemetry_partial:
                    print("[loop] Warning: Databricks telemetry was partial for this run", flush=True)
            except Exception as exc:
                print(f"[loop] Databricks telemetry end_run failed (non-fatal): {exc}", flush=True)

        self._state["experiment_count"] = n
        self._write_status("running")

    def _apply_decision(self, status: str, metric: Optional[float], governance_decision) -> str:
        if status == "crash":
            self._state["consecutive_discards"] += 1
            self._git_reset()
        elif status == "keep":
            if governance_decision.decision == "approved" and governance_decision.promotion_allowed:
                self._state["best_metric"] = metric
                self._state["best_commit"] = self.workspace.current_commit()
                self._state["consecutive_discards"] = 0
                self._git_commit(f"exp{self._state['experiment_count']+1}: {metric:.4f}")
                print(f"[loop] KEEP  metric={metric}", flush=True)
            else:
                self._git_reset()
                print(
                    "[loop] REVIEW  "
                    f"metric={metric} governance={governance_decision.decision}",
                    flush=True,
                )
                return "review"
        elif status == "discard":
            self._state["consecutive_discards"] += 1
            self._git_reset()
            print(f"[loop] DISCARD  metric={metric}  best={self._state.get('best_metric')}", flush=True)
            
        return status

    def _finish(self) -> None:
        self._write_status("idle")
        print(f"\n[loop] Session complete. Best: {self._state['best_metric']}", flush=True)

    def _git_commit(self, message: str) -> None:
        self.workspace.commit(message, self.task.get("editable_file", ""))

    def _git_reset(self) -> None:
        self.workspace.revert_file(self.task.get("editable_file", ""))

    def _log_result(self, n: int, metric, status: str, desc: str) -> None:
        commit = self.workspace.current_commit()
        self.workspace.log_experiment(commit, desc, status, str(metric))

    def _record_optimization_memory(
        self,
        run_name: str,
        baseline_metric: Optional[float],
        result,
        status: str,
        classification,
        plan,
        governance_decision,
        all_metrics: dict[str, float],
    ) -> None:
        matching_score = all_metrics.get("matching_score")
        execution_time = all_metrics.get("execution_time_seconds", result.elapsed_seconds)
        correctness_passed = status != "crash"
        guardrails_failed = []
        if matching_score is not None and matching_score < 100.0:
            correctness_passed = False
            guardrails_failed.append("matching_score_below_100")

        metric_delta, actual_result = describe_actual_result(
            baseline_metric,
            result.metric,
            self.task.get("direction", "higher"),
            status,
        )
        record = OptimizationMemoryRecord(
            run_id=run_name,
            task_id=self.task.get("id", ""),
            artifact=self.task.get("editable_file", ""),
            change_type=classification.primary_type,
            change_types=classification.change_types,
            expected_reason=expected_reason(classification.primary_type),
            actual_result=actual_result,
            decision=status,
            baseline_metric=baseline_metric,
            candidate_metric=result.metric,
            metric_delta=metric_delta,
            direction=self.task.get("direction", "higher"),
            correctness_passed=correctness_passed,
            guardrails_failed=guardrails_failed,
            execution_time_seconds=execution_time,
            matching_score=matching_score,
            confidence=classification.confidence,
            evidence={
                "classification": classification.evidence,
                "recommended_strategy": plan.recommended_strategy,
                "planner_rationale": plan.rationale,
                "governance_decision": governance_decision.decision,
                "governance_rationale": governance_decision.rationale,
                "backend": type(self.backend).__name__,
            },
        )
        self.memory.record(record)

    def _write_status(self, state: str) -> None:
        status = {
            "running": state == "running",
            "state": state,
            "experiment_count": self._state["experiment_count"],
            "best_metric": self._state["best_metric"],
            "last_updated": datetime.now(timezone.utc).isoformat(),
        }
        self.workspace.save_loop_status(status)

    def _read_results(self) -> str:
        return self.workspace.get_results_tsv_string()

def main() -> None:
    cfg = load_config()
    tasks_path = ROOT / "config" / "tasks.json"
    if not tasks_path.exists():
        print("Error: config/tasks.json not found")
        return
    data = json.loads(tasks_path.read_text(encoding="utf-8"))
    task_id = data.get("active_task", "06_sql_optimization")
    
    p = argparse.ArgumentParser()
    p.add_argument(
        "--mode",
        choices=["auto", "semi", "sql", "polars", "sql_polars_hybrid", "global_exploration"],
        default="auto",
    )
    p.add_argument("--task", default=task_id)
    args = p.parse_args()

    loop = ExperimentLoop(cfg, task_id=args.task)
    try:
        loop.start(mode=args.mode)
    except KeyboardInterrupt:
        loop.stop()

if __name__ == "__main__":
    main()
