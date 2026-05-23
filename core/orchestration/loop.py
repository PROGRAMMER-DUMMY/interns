"""
core/orchestration/loop.py — autonomous experiment loop.

Each iteration runs three phases:
  1. Intern chain (sequential, with retry, output chaining, scheduling gate)
  2. Main-agent code mutation (CodeMutator → editable_file is rewritten)
  3. Experiment execution + governance + memory recording

Safety hardening:
  * --dry-run flag: proposed edits are written to state/dry_run/exp_<n>.diff
    but the editable_file is NOT modified and the experiment is NOT executed
  * Review pause: governance 'review' decision writes state/REVIEW_PENDING.json,
    next iteration blocks until that file is cleared
  * Stale-PID recovery: on __init__, detect crashed prior session and auto-
    revert the editable_file before resuming
"""
# ruff: noqa: E402
import argparse
import json
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from core.optimization.change_classifier import classify_diff, expected_reason
from core.config import Config, load as load_config
from core.governance.contracts import OptimizationPolicy
from core.onboarding.workspace.bootstrap import AutoBootstrap
from core.execution.backend import ExecutionBackend, build_execution_backend
from core.failures import StructuredFailure, internal_bug, validation_blocker
from core.governance.evaluator import GovernanceEvaluator
from core.agents.intern_bus import InternBus, truncate_for_chain
from core.agents.code_mutator import CodeMutator, MutationResult
from core.agents.cli_inspector import render_startup_banner, resolve_active_model
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
from core.storage.workspace_layout import WorkspaceLayout

HEADER = "commit\tprimary_metric\tstatus\tdescription"

# Names of interns gated by deep_research_every_n. These use the expensive
# deep_research model — we don't want to fire them every iteration.
_EXPENSIVE_INTERNS = {"insights"}


class ExperimentLoop:
    def __init__(
        self,
        cfg: Optional[Config] = None,
        task_id: str | None = None,
        dry_run: bool = False,
    ):
        self.cfg = cfg or load_config()
        self.dry_run = dry_run
        self.tasks_path = ROOT / "config" / "tasks.json"
        self._load_task(task_id)
        self.workspace_layout = WorkspaceLayout.from_task(self.task, ROOT)
        self.workspace_layout.ensure_runtime_dirs()
        self.bootstrap_result = AutoBootstrap(ROOT, self.task, self.cfg).ensure_ready()
        self._refresh_task_after_bootstrap()
        self.workspace = Workspace(self.workspace_layout.workspace_db)

        # State paths used by safety hardening
        self.loop_pid_file = self.cfg.state_dir / "loop.pid"
        self.review_pending_file = self.cfg.state_dir / "REVIEW_PENDING.json"
        self.dry_run_dir = self.cfg.state_dir / "dry_run"
        if self.dry_run:
            self.dry_run_dir.mkdir(parents=True, exist_ok=True)

        # Run crash-recovery BEFORE we touch any other state
        self._recover_from_stale_pid()
        self._write_pid_file()

        # Build backends from config (Databricks if configured, else local)
        self.backend: ExecutionBackend = build_execution_backend(self.cfg)
        self.local_telemetry, self.db_telemetry = build_telemetry_backend(self.cfg, self.workspace)

        # InternBus gets the Databricks telemetry for LLM tracing (or None)
        self.bus = InternBus(self.cfg, telemetry=self.db_telemetry)
        self.mutator = CodeMutator(self.cfg, telemetry=self.db_telemetry, root=ROOT)

        self.memory = OptimizationMemory(self.workspace)
        self.planner = OptimizationPlanner(self.memory, ROOT)
        self.semantic_contract = SemanticContract.from_task(self.task, ROOT)
        self.optimization_policy = OptimizationPolicy.from_task(self.task)
        self.mode_planner = ModePlanner(self.optimization_policy)
        self.governance = GovernanceEvaluator(self.optimization_policy)
        self.metric_parser = RegexLogParser()
        self.decision_strategy = SingleMetricDecisionStrategy()

        # Cache of last expensive-intern reports so skipped iterations can
        # still inject the most recent signal into the chain.
        self._cached_intern_reports: dict[str, str] = {}

        self._running = False
        self._state = {
            "experiment_count":    0,
            "consecutive_discards": 0,
            "best_metric":         None,
            "best_commit":         None,
            "session_start":       datetime.now(timezone.utc).isoformat(),
        }

    # ── Task loading ──────────────────────────────────────────────────────────

    def _load_task(self, task_id: str | None):
        if not self.tasks_path.exists():
            raise FileNotFoundError("config/tasks.json missing")
        data = json.loads(self.tasks_path.read_text(encoding="utf-8"))
        task_id = task_id or data.get("active_task")
        if not task_id:
            raise ValueError("No active task configured in config/tasks.json")
        for t in data.get("tasks", []):
            if t["id"] == task_id:
                self.task = t
                return
        raise ValueError(f"Task '{task_id}' not found in tasks.json")

    def _refresh_task_after_bootstrap(self) -> None:
        generated = self.bootstrap_result.artifacts
        if "baseline_sql" in generated:
            self.task["editable_file"] = generated["baseline_sql"]
            self.task.setdefault("sql_file", generated["baseline_sql"])
        if "experiment" in generated:
            self.task["experiment_cmd"] = ["uv", "run", "python", generated["experiment"]]
        if "evaluator" in generated:
            self.task["evaluator_cmd"] = ["uv", "run", "python", generated["evaluator"]]
        if "semantic_contract" in generated:
            contract_cfg = self.task.setdefault("semantic_contract", {})
            contract_cfg["methodology_json"] = generated["semantic_contract"]

    # ── Safety hardening (P3) ─────────────────────────────────────────────────

    def _write_pid_file(self) -> None:
        try:
            self.loop_pid_file.parent.mkdir(parents=True, exist_ok=True)
            self.loop_pid_file.write_text(
                json.dumps({"pid": os.getpid(),
                            "started_at": datetime.now(timezone.utc).isoformat()}),
                encoding="utf-8",
            )
        except Exception as exc:
            print(f"[loop] could not write PID file: {exc}", flush=True)

    def _clear_pid_file(self) -> None:
        try:
            self.loop_pid_file.unlink(missing_ok=True)
        except OSError as exc:
            print(f"[loop] could not clear PID file: {exc}", flush=True)

    def _recover_from_stale_pid(self) -> None:
        """If a prior loop process died mid-iteration, revert any uncommitted
        edits to the editable_file and clear stale state files. Logs a
        recovery event so it is visible in telemetry."""
        if not self.loop_pid_file.exists():
            return
        try:
            data = json.loads(self.loop_pid_file.read_text(encoding="utf-8") or "{}")
            stale_pid = int(data.get("pid", 0) or 0)
        except (OSError, json.JSONDecodeError, TypeError, ValueError):
            stale_pid = 0

        alive = False
        if stale_pid > 0:
            try:
                os.kill(stale_pid, 0)
                alive = True
            except (OSError, PermissionError):
                alive = False

        if alive:
            # Another loop is genuinely running — refuse to start a second one.
            raise RuntimeError(
                f"Another autoresearch loop is already running (PID {stale_pid}). "
                "Stop it before starting a new session, or delete state/loop.pid."
            )

        # Stale: revert and clear.
        edit_file = self.task.get("editable_file", "")
        recovery_msg = f"stale loop PID {stale_pid} detected; reverting {edit_file}"
        print(f"[loop] RECOVERY: {recovery_msg}", flush=True)
        if edit_file:
            try:
                self.workspace.revert_file(edit_file)
            except Exception as exc:
                print(f"[loop] RECOVERY: revert failed (non-fatal): {exc}", flush=True)
        try:
            self.workspace.append_run_log(f"[recovery] {recovery_msg}")
        except Exception as exc:
            failure = internal_bug("loop.recovery_log", str(exc))
            print(f"[loop] RECOVERY log failed ({failure.kind.value}): {exc}", flush=True)
        self._clear_pid_file()
        # If a review-pause file was left behind by the crash, leave it: the
        # operator must explicitly clear it before resuming.

    def _check_review_gate(self) -> bool:
        """Return True if a REVIEW_PENDING file is blocking the loop. Caller
        should refuse to start the next iteration."""
        if self.review_pending_file.exists():
            try:
                info = json.loads(self.review_pending_file.read_text(encoding="utf-8") or "{}")
            except (OSError, json.JSONDecodeError, TypeError):
                info = {}
            run_id = info.get("run_id", "?")
            print(
                f"[loop] REVIEW PAUSE: governance flagged run {run_id} for human review. "
                f"Clear {self.review_pending_file} to resume.",
                flush=True,
            )
            return True
        return False

    def _write_review_pending(self, run_id: str, rationale: str,
                              metric: Optional[float]) -> None:
        payload = {
            "run_id":   run_id,
            "rationale": rationale,
            "metric":   metric,
            "raised_at": datetime.now(timezone.utc).isoformat(),
            "experiment_count": self._state["experiment_count"],
        }
        self.review_pending_file.parent.mkdir(parents=True, exist_ok=True)
        self.review_pending_file.write_text(json.dumps(payload, indent=2), encoding="utf-8")
        print(
            f"[loop] Wrote {self.review_pending_file} — loop will pause on next iteration",
            flush=True,
        )

    def _record_setup_metadata(self) -> None:
        """Persist the resolved CLI/model state so each session's costs can be
        attributed to a specific binary + model. Written to intern_activity
        with name='setup' for easy filtering in the dashboard."""
        resolved_model = resolve_active_model(self.cfg.main_agent)
        details = {
            "ts":            datetime.now(timezone.utc).isoformat(),
            "main_agent":    self.cfg.main_agent,
            "resolved_model": resolved_model,
            "has_api_key":   self.cfg.has_api_key(),
            "dry_run":       self.dry_run,
            "deep_research_every_n": self.cfg.deep_research_every_n,
            "max_experiments_session": self.cfg.max_experiments_session,
        }
        try:
            self.workspace.log_intern_activity("setup", "session_start", details)
        except Exception as exc:
            print(f"[loop] could not record setup metadata: {exc}", flush=True)

    # ── Lifecycle ─────────────────────────────────────────────────────────────

    def start(self, mode: str = "auto") -> None:
        self._running = True
        self.workspace_layout.ensure_runtime_dirs()
        self._write_status("running")

        print(f"\n[loop] === {self.task['name']} ===", flush=True)
        if self.dry_run:
            print("[loop] DRY-RUN MODE — edits will be written to "
                  f"{self.dry_run_dir} but NOT applied", flush=True)

        # Show the operator exactly which CLI / model is about to spend tokens.
        print(render_startup_banner(self.cfg.main_agent, self.cfg.has_api_key()),
              flush=True)
        self._record_setup_metadata()

        print(
            "[loop] Bootstrap "
            f"{self.bootstrap_result.action}: {self.bootstrap_result.reason}",
            flush=True,
        )
        if self.bootstrap_result.databricks and self.bootstrap_result.databricks.configured:
            print(
                "[loop] Databricks readiness: "
                f"{self.bootstrap_result.databricks.message} "
                "(remote execution requires explicit approval)",
                flush=True,
            )
        try:
            while self._running and self._state["experiment_count"] < self.cfg.max_experiments_session:
                if self._check_review_gate():
                    # Wait one iteration cycle then re-check; the operator
                    # clears REVIEW_PENDING.json by deleting it.
                    time.sleep(5)
                    continue
                self._run_one(mode)
        finally:
            self._finish()
            self._clear_pid_file()

    def stop(self) -> None:
        self._running = False

    # ── Iteration ─────────────────────────────────────────────────────────────

    def _run_one(self, mode: str) -> None:
        n = self._state["experiment_count"] + 1
        run_name = f"exp_{n}"
        print(f"\n[loop] Experiment {n}  |  best: {self._state['best_metric']}", flush=True)

        # Begin telemetry run
        self.local_telemetry.begin_run(run_name)
        if self.db_telemetry:
            self.db_telemetry.begin_run(run_name)

        mode_plan = self.mode_planner.build_plan(None if mode in {"auto", "semi"} else mode)
        plan = self.planner.build_plan(self.task, self.semantic_contract)
        active_interns = self.bus.list_active(self.task["domain"])

        # ── Phase 1: Intern chain ─────────────────────────────────────────────
        intern_reports: list[str] = []          # all reports used in this iteration
        chain_outputs: list[tuple[str, str]] = []  # (intern_name, full_report) for chaining

        base_context = self._build_intern_context(n, plan, mode_plan)
        for intern in active_interns:
            # Scheduling gate: skip expensive interns unless on every-N tick.
            if self._is_expensive(intern) and not self._should_run_expensive(n):
                cached = self._cached_intern_reports.get(intern)
                if cached:
                    print(f"[loop] {intern}: skipped (cached from prior iteration)",
                          flush=True)
                    intern_reports.append(f"### {intern} (cached)\n{cached}")
                    chain_outputs.append((intern, cached))
                else:
                    print(f"[loop] {intern}: skipped (every_n gate, no cache yet)",
                          flush=True)
                continue

            # Build per-intern context with chained prior outputs
            context = dict(base_context)
            context["prior_intern_reports"] = self._format_prior_outputs(chain_outputs)

            report = self.bus.invoke(intern, "Analyze and suggest improvement", context)
            intern_reports.append(report)
            chain_outputs.append((intern, report))

            # Cache successful expensive runs for skipped iterations
            if self._is_expensive(intern) and not report.startswith("INTERN_FAILED:"):
                self._cached_intern_reports[intern] = report

        # ── Phase 2: Main-agent code mutation ─────────────────────────────────
        editable_file = self.task.get("editable_file", "")
        edit_path = ROOT / editable_file if editable_file else None
        if not edit_path or not edit_path.exists():
            self._abort_iteration(n, run_name,
                                  reason=f"editable_file '{editable_file}' missing")
            return

        current_content = edit_path.read_text(encoding="utf-8", errors="replace")
        mutation = self.mutator.mutate(
            editable_file=editable_file,
            file_content=current_content,
            intern_reports=intern_reports,
            semantic_contract_text=json.dumps(
                self.semantic_contract.summary(), indent=2),
            best_metric=self._state["best_metric"],
            optimization_plan=plan.as_prompt_context(),
            experiment_count=n,
        )
        print(f"[loop] main agent: {mutation.diff_summary} ({mutation.elapsed_s}s)",
              flush=True)
        self._log_mutation(run_name, mutation)

        if not mutation.success:
            self._abort_iteration(n, run_name,
                                  reason=f"main_agent_failed: {mutation.error}")
            return

        # In dry-run mode: save the proposed edit and skip experiment execution
        if self.dry_run:
            diff_path = self.dry_run_dir / f"{run_name}.diff"
            diff_path.write_text(
                self._format_dry_run_record(
                    run_name, editable_file, current_content,
                    mutation.new_content or "", mutation,
                ),
                encoding="utf-8",
            )
            print(f"[loop] DRY-RUN: proposed edit saved to {diff_path}; "
                  "skipping experiment execution",
                  flush=True)
            self._state["experiment_count"] = n
            self.local_telemetry.end_run(None,
                                          {"description": "dry-run",
                                           "backend": "dry-run"},
                                          "dry_run")
            if self.db_telemetry:
                try:
                    self.db_telemetry.end_run(None,
                                              {"description": "dry-run"},
                                              "dry_run")
                except Exception as exc:
                    failure = internal_bug("loop.db_telemetry_dry_run", str(exc))
                    print(f"[loop] telemetry failed ({failure.kind.value}): {exc}", flush=True)
            return

        # Live mode: write the new file content
        edit_path.write_text(mutation.new_content or "", encoding="utf-8")

        # ── Phase 3: Experiment execution + governance ────────────────────────
        from core.execution.backend import ExecutionResult
        result: ExecutionResult = self.backend.execute(
            self.task,
            self.cfg.max_run_seconds,
            self.cfg.hard_timeout_seconds,
            self.workspace_layout.run_log,
        )
        if result.failure:
            print(
                f"[loop] structured failure: {result.failure.kind.value} "
                f"stage={result.failure.stage}",
                flush=True,
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
        status = self._apply_decision(status, result.metric, governance_decision, run_name)
        desc = self._build_desc(mutation, intern_reports)
        self._log_result(n, result.metric, status, desc)
        self._record_optimization_memory(
            run_name, baseline_metric, result, status, classification,
            plan, governance_decision, all_metrics,
        )

        # Flush telemetry
        tel_params = {
            "execution_time_seconds": result.elapsed_seconds,
            "token_count": result.token_count,
            "description": desc,
            "backend": type(self.backend).__name__,
            "main_agent_elapsed_s": mutation.elapsed_s,
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

    # ── Intern phase helpers ──────────────────────────────────────────────────

    def _build_intern_context(self, n: int, plan, mode_plan) -> dict:
        return {
            "experiment_count": n,
            "best_metric":      self._state["best_metric"],
            "editable_file":    self.task["editable_file"],
            "results_tsv":      self._read_results(),
            "run_log": (
                self.workspace_layout.run_log.read_text(encoding="utf-8")
                if self.workspace_layout.run_log.exists()
                else ""
            ),
            "optimization_plan":   plan.as_prompt_context(),
            "optimization_memory": self.memory.as_prompt_context(),
            "semantic_contract":   json.dumps(self.semantic_contract.summary(), indent=2),
            "optimization_policy": json.dumps(self.optimization_policy.summary(), indent=2),
            "mode_plan":           json.dumps(mode_plan.as_dict(), indent=2),
            "workspace_layout":    json.dumps(self.workspace_layout.summary(), indent=2),
        }

    def _format_prior_outputs(self, chain_outputs: list[tuple[str, str]]) -> str:
        if not chain_outputs:
            return ""
        cap = int(getattr(self.cfg, "prior_output_max_chars", 1800))
        lines = []
        for name, report in chain_outputs:
            lines.append(f"### Prior output from `{name}`")
            lines.append(truncate_for_chain(report, cap))
            lines.append("")
        return "\n".join(lines)

    def _is_expensive(self, intern_name: str) -> bool:
        return intern_name in _EXPENSIVE_INTERNS

    def _should_run_expensive(self, n: int) -> bool:
        """Honour deep_research_every_n. Expensive interns fire on iteration 1
        and every N iterations after (1, 1+N, 1+2N, ...). They also fire
        whenever the loop is 'stuck' (consecutive_discards >= threshold)."""
        every_n = max(1, int(self.cfg.deep_research_every_n))
        if (n - 1) % every_n == 0:
            return True
        if self._state["consecutive_discards"] >= int(self.cfg.stuck_threshold):
            return True
        return False

    # ── Mutation telemetry ────────────────────────────────────────────────────

    def _log_mutation(self, run_id: str, mutation: MutationResult) -> None:
        details = {
            "ts":                  datetime.now(timezone.utc).isoformat(),
            "run_id":              run_id,
            "success":             mutation.success,
            "error":               mutation.error,
            "lang":                mutation.lang,
            "elapsed_s":           mutation.elapsed_s,
            "prompt_chars":        mutation.prompt_chars,
            "intern_reports_used": mutation.intern_reports_used,
            "new_content_chars":   len(mutation.new_content or ""),
        }
        try:
            self.workspace.log_intern_activity(
                "main_agent",
                "code_mutation",
                details,
            )
        except Exception as exc:
            print(f"[loop] could not log mutation: {exc}", flush=True)

    def _format_dry_run_record(
        self, run_name: str, editable_file: str,
        old_content: str, new_content: str, mutation: MutationResult,
    ) -> str:
        import difflib
        diff = "".join(difflib.unified_diff(
            old_content.splitlines(keepends=True),
            new_content.splitlines(keepends=True),
            fromfile=f"a/{editable_file}",
            tofile=f"b/{editable_file}",
        ))
        return (
            f"# Dry-run record: {run_name}\n"
            f"# editable_file: {editable_file}\n"
            f"# main_agent: {self.cfg.main_agent}\n"
            f"# elapsed_s: {mutation.elapsed_s}\n"
            f"# prompt_chars: {mutation.prompt_chars}\n"
            f"# intern_reports_used: {mutation.intern_reports_used}\n"
            f"# lang: {mutation.lang}\n"
            "# ----------------------------------------------------------------\n"
            f"{diff}\n"
        )

    # ── Iteration helpers ─────────────────────────────────────────────────────

    def _abort_iteration(self, n: int, run_name: str, *, reason: str | StructuredFailure) -> None:
        """Record an aborted iteration without running the experiment."""
        failure = reason if isinstance(reason, StructuredFailure) else validation_blocker("loop.iteration", reason)
        print(f"[loop] ABORT iteration {n}: {failure.message}", flush=True)
        self._state["consecutive_discards"] += 1
        try:
            self.workspace.append_run_log(f"[abort] {run_name}: {json.dumps(failure.summary(), sort_keys=True)}")
        except Exception as exc:
            log_failure = internal_bug("loop.abort_log", str(exc))
            print(f"[loop] abort log failed ({log_failure.kind.value}): {exc}", flush=True)
        self.local_telemetry.end_run(
            None,
            {
                "description": failure.message[:120],
                "backend": "aborted",
                "failure_kind": failure.kind.value,
                "failure_stage": failure.stage,
            },
            "aborted",
        )
        if self.db_telemetry:
            try:
                self.db_telemetry.end_run(
                    None,
                    {
                        "description": failure.message[:120],
                        "failure_kind": failure.kind.value,
                        "failure_stage": failure.stage,
                    },
                    "aborted",
                )
            except Exception as exc:
                telemetry_failure = internal_bug("loop.db_telemetry_abort", str(exc))
                print(f"[loop] telemetry failed ({telemetry_failure.kind.value}): {exc}", flush=True)
        self._state["experiment_count"] = n
        self._write_status("running")

    def _apply_decision(
        self,
        status: str,
        metric: Optional[float],
        governance_decision,
        run_id: str,
    ) -> str:
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
                self._write_review_pending(
                    run_id,
                    rationale=getattr(governance_decision, "rationale", ""),
                    metric=metric,
                )
                return "review"
        elif status == "discard":
            self._state["consecutive_discards"] += 1
            self._git_reset()
            print(f"[loop] DISCARD  metric={metric}  best={self._state.get('best_metric')}", flush=True)

        return status

    def _build_desc(self, mutation: MutationResult, intern_reports: list[str]) -> str:
        if mutation.success:
            head = (mutation.raw_output or "").strip().splitlines()
            first = head[0] if head else "mutation_ok"
            return first[:80]
        if intern_reports:
            return intern_reports[0].splitlines()[0][:80] if intern_reports[0] else "no reports"
        return "no reports"

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
            "workspace": self.task.get("workspace", ""),
            "interns_dir": str(self.workspace_layout.interns_dir),
            "dry_run": self.dry_run,
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
    task_id = data.get("active_task")
    if not task_id:
        print("Error: active_task not configured in config/tasks.json")
        return

    p = argparse.ArgumentParser()
    p.add_argument(
        "--mode",
        choices=["auto", "semi", "sql", "polars", "sql_polars_hybrid", "pyspark", "global_exploration"],
        default="auto",
    )
    p.add_argument("--task", default=task_id)
    p.add_argument(
        "--dry-run",
        action="store_true",
        help="Run interns + main agent, save proposed edits to state/dry_run/, "
             "but do NOT modify the editable_file or run the experiment.",
    )
    args = p.parse_args()

    loop = ExperimentLoop(cfg, task_id=args.task, dry_run=args.dry_run)
    try:
        loop.start(mode=args.mode)
    except KeyboardInterrupt:
        loop.stop()


if __name__ == "__main__":
    main()
