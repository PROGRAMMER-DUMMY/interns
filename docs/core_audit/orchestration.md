# orchestration — audit

## Purpose
`core/orchestration/` is the experiment/KPI loop spine. `loop.py` runs the autonomous
iteration loop that wires together onboarding/bootstrap, the intern chain, main-agent
code mutation, an `ExecutionBackend`, the change classifier, the governance evaluator,
optimization memory/planner, and dual (local + Databricks) telemetry. `runner.py` is a
standalone experiment runner (subprocess + evaluator + metric parse). `governor.py`
provides deterministic error→specialist routing with a retry cap (circuit breaker),
used by the medallion pipeline rather than by the loop.

## Files
| File | Lines | Purpose | Key classes/functions |
| --- | --- | --- | --- |
| `__init__.py` | 11 | Package exports | re-exports `ExperimentLoop`, `main`, `ExperimentRunner`, `RunResult` |
| `governor.py` | 96 | Error routing + retry circuit breaker | `Governor` (`decide_routing`, `decide_medallion_routing`, `run_specialist`), `RoutingDecision`, `MEDALLION_ROUTING` |
| `loop.py` | 785 | Autonomous 3-phase experiment loop + safety hardening | `ExperimentLoop` (`start`, `_run_one`, `_apply_decision`, `_recover_from_stale_pid`, `_check_review_gate`, `_record_optimization_memory`), `main` |
| `runner.py` | 150 | Standalone experiment+evaluator runner | `ExperimentRunner` (`run`, `_run_experiment`, `_run_evaluator`, `_kill`), `RunResult` |

## Findings
| Tag | Location (file:line) | Description | Suggested fix |
| --- | --- | --- | --- |
| [DEAD] | `runner.py:30-150`, `__init__.py:3` | `ExperimentRunner`/`RunResult` are exported but never instantiated anywhere in the repo. The loop uses `ExecutionBackend` (`DuckDBBackend`) instead. The whole module is dead code. | Delete `runner.py` and the exports, or have `DuckDBBackend` delegate to it (see DUP). |
| [DUP] | `runner.py:60-150` vs `execution/backend.py:144-202` | `_run_experiment`/`_run_evaluator`/`_kill` subprocess+timeout+SIGTERM logic in the runner is duplicated almost verbatim in `DuckDBBackend._run_subprocess`/`_run_evaluator`/`_kill`. Two copies of the kill/timeout logic to maintain. | Have one own the subprocess mechanics; the other delegates. |
| [DEAD] | `runner.py:103-131` | `_parse_metric` and `_parse_all_metrics` are private methods never called — `run()` uses `self.parser` instead. Reimplements parser logic that already lives in `RegexLogParser`. | Remove. |
| [INTEGRATION] | `governor.py` (whole) | `Governor`/`decide_routing`/`run_specialist` exist but the loop never imports or invokes `Governor`. Only `decide_medallion_routing` is used, and only by `medallion/build.py`. The CONTEXT data-flow "intern suggestions → ExecutionBackend → ... GovernanceEvaluator" loop has no error-routing-to-specialist step; on backend failure the loop just records and discards. `decide_routing` (the KPI/SQL variant) has zero callers. | Either wire `Governor` into `_run_one`'s failure path (route `result.failure` to a specialist + retry) or move it out of `orchestration/` since the loop spine does not use it. |
| [NOT-PROD] | `loop.py:423-466` | When `result.failure` is set (backend/PHI/remote failure), the loop only prints, then proceeds to classify the diff, decide status, run governance, and record memory on a *failed* execution as if it produced a valid metric. There is no retry and no early abort on structured failure. | On `result.failure`, branch to `_abort_iteration` (or a retry via Governor) instead of continuing the keep/discard pipeline. |
| [NOT-PROD] | `loop.py:295-301` | "Retry/stall" handling is thin: the only stall response is `_should_run_expensive` re-firing deep-research when `consecutive_discards >= stuck_threshold`. There is no max-consecutive-discard stop, no backoff, and no escalation; a persistently failing task burns the full `max_experiments_session`. | Add a hard stop / escalation when `consecutive_discards` exceeds a cap. |
| [NOT-PROD] | `loop.py:296-300` | Review-gate wait is a blocking `time.sleep(5)` busy-loop inside the main thread; `stop()` flips `_running` but is only reachable via `KeyboardInterrupt`. A pending review will spin indefinitely with no timeout or operator-notification beyond a printed line each ~5s. | Add a max-wait/timeout and event-based wakeup; log once, not every cycle. |
| [BUG] | `loop.py:631-634` | On keep, `_git_commit(f"exp{self._state['experiment_count']+1}: ...")` uses `experiment_count+1`, but `experiment_count` is only set to `n` at line 485 (after `_apply_decision`). At commit time `experiment_count` is still the *previous* value, so the commit message labels the run `exp{n}` only by coincidence of the `+1`; if ordering ever changes this silently mislabels. Also `metric:.4f` will raise if `metric` is `None` on a keep. | Pass `n`/`run_id` explicitly; guard `metric` formatting against `None`. |
| [NOT-PROD] | `loop.py:159-160,202-203,262-263,407-409,482-483,554-555,589-591` | Many broad `except Exception` blocks that only `print(...)` and continue (PID write, recovery log, setup metadata, telemetry, mutation log, abort log). Failures here are swallowed with no structured failure surfaced to governance/telemetry, masking partial-state corruption. | Narrow excepts; route to `StructuredFailure`/telemetry instead of bare prints. |
| [BUG] | `loop.py:413` | Live mode writes `mutation.new_content or ""` to the editable file *before* execution. If `new_content` is empty/None on a "successful" mutation, the file is clobbered to empty, the experiment runs against an empty SQL, and recovery only happens on the *next* process crash, not this iteration. | Guard: if `success` but `new_content` is falsy, abort the iteration. |
| [INTEGRATION] | `loop.py:434-436,690-691` | `matching_score` / `execution_time_seconds` are pulled from `metric_parser.parse_all_metrics(result.log_content)`. If the backend log does not emit those keys (e.g. warehouse backend emits its own format), `matching_score` is `None` and the correctness guardrail at line 694 silently passes. The "profiler guardrails" stage in the CONTEXT diagram is only realized as `profiler_evidence={"parsed_metrics": all_metrics}` — there is no actual `DataModelProfiler` guardrail invoked in the loop. | Confirm every backend emits the contract metric keys, or fail closed when correctness signals are absent; wire the profiler explicitly. |
| [MISSING] | `loop.py:99-106` | `OptimizationPlanner` and `OptimizationMemory` are wired, but the DataModelProfiler / `WorkspaceKickstarter` stages from the Enterprise Data Flow are not invoked in the loop (only `AutoBootstrap`). The diagram's profiler-evidence node is not produced by a profiler here. | Document that profiling is upstream (bootstrap) or wire it; otherwise the diagram overstates loop coverage. |
| [NOT-PROD] | `runner.py:36-58` | `ExperimentRunner.run` indexes `task["experiment_cmd"]`/`task["evaluator_cmd"]` with hard `KeyError` if absent (the loop's backend uses `.get(..., [])`). Combined with being dead code, it is an inconsistent, unguarded duplicate. | If kept, mirror the backend's `.get` + normalize_command guards. |

## Cross-package coupling
- `core.execution.backend` (`build_execution_backend`, `ExecutionBackend`, `ExecutionResult`, `DuckDBBackend`) — primary execution dependency; remote-approval gate (`AUTORESEARCH_ALLOW_REMOTE_EXECUTION=1`) lives here, not in the loop. **Confirmed: local-safe-by-default + remote-needs-approval IS enforced** (`backend.py:533-547`), and PHI/strict gates fail closed in the warehouse/jobs backends.
- `core.onboarding.workspace.bootstrap.AutoBootstrap` — auto-onboarding/fingerprint stage.
- `core.governance.{contracts.OptimizationPolicy, evaluator.GovernanceEvaluator, mode_policy.ModePlanner, semantic_contract.SemanticContract}` — guardrail/promotion stages (all wired).
- `core.optimization.{change_classifier, memory, planner, strategy}` — classification, memory, ranking, decision strategy (all wired).
- `core.observability.{parser.RegexLogParser, telemetry_backend.build_telemetry_backend}` — metric parse + dual telemetry (wired; Databricks telemetry additive, failures non-fatal).
- `core.agents.{intern_bus.InternBus, code_mutator.CodeMutator, cli_inspector, registry.InternRegistry}` — intern chain + main-agent mutation; `registry` used by `Governor` only.
- `core.storage.{workspace.Workspace, workspace_layout.WorkspaceLayout}` — all loop-called methods (`revert_file`, `diff_file`, `commit`, `current_commit`, `log_*`, `save_loop_status`, `get_results_tsv_string`) **exist and verified**.
- `governor.py` is consumed only by `core.medallion.build` — it is in this package but not part of the loop spine.

## Verdict
The loop wires the **majority** of the CONTEXT Enterprise Data Flow (bootstrap → contracts → policy/mode → planner → interns → backend → classifier → governance → memory → telemetry) and correctly delegates the local-safe/remote-approval rule to the execution layer, which is properly fail-closed. Crash recovery (stale-PID revert), single-instance locking, dry-run, and a review-pause gate are real and reasonable.

However it is **not production-ready as a resilient loop**: the failure path is the weak spot. Structured backend failures are printed but then run through the full keep/discard/governance/memory pipeline as if valid (no early abort, no retry, no Governor routing); stall handling is limited to re-firing deep research with no hard stop or backoff; and pervasive bare `except Exception: print(...)` blocks swallow state-mutation errors. The `Governor` circuit-breaker exists but is disconnected from the loop, and `runner.py` is entirely dead/duplicated code. Recommend: branch on `result.failure`, add a consecutive-discard hard stop, wire (or relocate) `Governor`, delete `runner.py`, and guard the empty-`new_content` write and `None`-metric commit-message format.
