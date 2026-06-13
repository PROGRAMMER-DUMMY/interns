# optimization — audit

## Purpose
`core/optimization/` owns the adaptive-optimization layer of the control plane. It (1) classifies
git diffs of generated artifacts into stable optimization-pattern labels (`predicate_pushdown`,
`join_rewrite`, `cte_rewrite`, `aggregation_rewrite`, `case_simplification`, `column_pruning`),
(2) ranks the next optimization strategy from hotspot evidence + semantic-contract risk + learned
memory (`OptimizationPlanner`), (3) records each experiment's hypothesis/outcome/guardrails to
`workspace.db` (`OptimizationMemory`), (4) decides keep/discard per run (`SingleMetricDecisionStrategy`),
and (5) maintains a separate SQL/Polars/PySpark engine-routing memory (`EngineEvolutionMemory`,
file-backed JSON + metadata store) consumed by the source-to-target planner.

All five concerns are genuinely wired end-to-end: `core/orchestration/loop.py` imports and invokes
the classifier, planner, memory, decision strategy, and `describe_actual_result`/`expected_reason`
on every iteration; `core/onboarding/relationships/source_to_target_planner.py` invokes
`EngineEvolutionMemory.recommendation_for`. No dead modules in this package.

## Files
| File | Lines | Purpose | Key classes/functions |
| --- | --- | --- | --- |
| `__init__.py` | 18 | Package re-exports | `ChangeClassification`, `OptimizationMemory`, `OptimizationPlanner`, `SingleMetricDecisionStrategy`, `classify_diff`, `expected_reason`, `describe_actual_result` |
| `change_classifier.py` | 100 | Rule-based diff→pattern labeler | `ChangeClassification` (frozen dc), `_PATTERNS`, `classify_diff()`, `expected_reason()` |
| `engine_evolution.py` | 390 | Engine-routing memory (JSON + md + metadata store) + CLI | `EngineEvolutionRecord`, `EngineEvolutionMemory` (`record`, `lessons`, `recommendation_for`, `_derive_lessons`), `main()` |
| `memory.py` | 70 | Optimization experiment memory over `workspace.db` | `OptimizationMemoryRecord`, `OptimizationMemory`, `describe_actual_result()` |
| `planner.py` | 130 | Strategy ranker from hotspots/memory/contract | `OptimizationPlan`, `OptimizationPlanner` (`build_plan`, `_strategies_from_hotspots`, `_strategies_from_memory`, `_rationale`) |
| `strategy.py` | 22 | Per-run keep/discard decision | `BaseDecisionStrategy`, `SingleMetricDecisionStrategy` |

## Findings
| Tag | Location (file:line) | Description | Suggested fix |
| --- | --- | --- | --- |
| [BUG] | `strategy.py:13-22` | `decide()` reads `task.get("direction")` with **no default**. If a task omits `direction` (hand-authored `tasks.json` has no `direction` key; only `kickstart.py:215` injects one), then for any `best is not None` neither the `higher` nor `lower` branch matches, so `improved=False` and **every candidate is discarded** — the loop can never improve. Meanwhile loop/memory default the same field to `"higher"` (loop.py:440/701/716), so memory records `direction="higher"` while the strategy silently treats it as unknown. | Default to `task.get("direction", "higher")` for parity with loop/memory, or validate `direction in {"higher","lower"}` at task load and fail closed. |
| [BUG] | `memory.py:68` | `describe_actual_result` treats **any** non-`"higher"` value as `"lower"` (`delta>0 if direction=="higher" else delta<0`), whereas `strategy.py` treats unknown as neither. A typo/blank direction yields inconsistent "improved" labeling between the recorded memory and the keep/discard decision. | Branch explicitly on `higher`/`lower` and raise or mark `unknown` for anything else, matching one canonical contract. |
| [NOT-PROD] | `change_classifier.py:28-51` | `join_rewrite` matches bare `\bon\b` and `column_pruning` matches `\bselect\b` on any `+`/`-` line. Almost every non-trivial SQL diff contains `select`/`on`, so most diffs get tagged with multiple labels, `primary_type` is just the first pattern in `_PATTERNS` order (predicate_pushdown wins by position), and `confidence` collapses to `medium`. Labels are stable but low-precision; the `.sql`→`high` override (line 77) then over-states confidence on these noisy multi-label hits. | Require stronger anchors (e.g. `join` keyword, `select`-list deltas vs `select` anywhere), score patterns rather than first-match, and gate the `.sql` confidence bump on single-label results only. |
| [NOT-PROD] | `change_classifier.py:29-34` | Patterns key on added **or** removed lines (`^[+-]`) without distinguishing direction, so removing a `where`/`join` is classified identically to adding one. A removed predicate (a regression) is labeled `predicate_pushdown` with the "reduce rows earlier" positive hypothesis. | Separate add vs remove evidence; an optimization label should reflect net intent, not mere keyword presence. |
| [NOT-PROD] | `planner.py:104-108` | `_strategies_from_memory` sorts on `(success_rate, avg_metric_delta)` but `avg_metric_delta` is direction-naive — for `direction="lower"` metrics a more-negative delta is better, yet `reverse=True` ranks larger (worse) deltas first. Ranking can prefer the worse pattern on minimize objectives. | Normalize delta by direction before ranking, or rank on a direction-aware "improvement" field computed at write time. |
| [NOT-PROD] | `engine_evolution.py:147,152-153` | `float(row.get("elapsed_seconds"))` / `int(row.get(...))` run inside comprehensions with only `is not None` guards; a malformed stored record (string/garbage from the public CLI `main()` or external metadata store) raises `ValueError`/`TypeError` and aborts `_derive_lessons`, which runs inside `record()` and would fail the whole write. | Wrap coercions in try/except or validate record shape on load; `_load_payload` already tolerates bad JSON but not bad field types. |
| [NOT-PROD] | `strategy.py:1-22` | No module docstring, returns bare string sentinels (`"crash"`/`"keep"`/`"discard"`) instead of an enum; trailing-whitespace lines. Minor but it is the one component on the per-run keep/discard hot path. | Use an enum or typed literal; add docstring; align with `governance` decision vocabulary. |
| [INTEGRATION] | `engine_evolution.py` (whole) vs `memory.py`/`planner.py` | Two parallel, non-interoperating memories: `OptimizationMemory` (SQLite, pattern stats) and `EngineEvolutionMemory` (JSON + metadata store, engine lessons). The planner consumes only the former; the source-to-target planner consumes only the latter. Engine-routing evidence never informs `OptimizationPlanner` and pattern evidence never informs engine choice. Not dead, but a coupling gap given CONTEXT.md presents one "evolution memory" flow. | Document the split deliberately, or expose a thin façade so the planner can read engine lessons (and vice versa). |
| [MISSING] | `change_classifier.py:55`, `memory.py`, `planner.py` | No unit-level negative tests visible in-package; coverage lives in `tests/test_enterprise_optimization.py` / `tests/test_engine_evolution.py`. The direction default bug and remove-vs-add ambiguity above are exactly the cases not exercised. | Add table-driven classifier tests (add vs remove, multi-keyword), and a strategy test for missing `direction`. |
| [DUP] | `memory.py:51` & `planner.py:23` | Two `as_prompt_context` implementations plus `describe_actual_result` called twice per iteration in the loop (loop.py:437 and again 698) recomputing the identical delta. Harmless but redundant compute. | Compute delta once in the loop and pass it down. |

## Cross-package coupling
- `memory.py` -> `core.storage.workspace.Workspace` (`log_optimization_memory`,
  `get_optimization_pattern_stats`, `get_recent_optimization_memory`). Persistence is **safe**:
  parameterized SQL, `with self.conn` transactional inserts, and `redact_keys()` applied to the
  `evidence` JSON before write (workspace.py:246). Booleans coerced to 0/1.
- `planner.py` -> `core.governance.semantic_contract.SemanticContract` (`.rules`, `.summary()`),
  reads `interns/generated/evidence/hotspots.json` (tolerant of missing/invalid JSON).
- `engine_evolution.py` -> `core.storage.metadata_store` (`build_metadata_store`/`upsert`) and
  `core.storage.workspace_layout.WorkspaceLayout`; writes JSON + markdown with marker-bounded
  section replace (idempotent).
- Consumers: `core/orchestration/loop.py` (classifier, planner, memory, decision strategy,
  `expected_reason`, `describe_actual_result`, `plan.as_prompt_context()` into intern context at
  loop.py:372/501-502); `core/onboarding/relationships/source_to_target_planner.py`
  (`EngineEvolutionMemory.recommendation_for`); `core/governance/evaluator.py` consumes the
  `ChangeClassification` passed from the loop.

## Verdict
**Architecturally sound and fully integrated — not dead code.** The classifier, planner, optimization
memory, decision strategy, and engine-evolution memory are each invoked on real execution paths, and
persistence (SQLite parameterized + redacted; JSON marker-idempotent) is production-grade. The
**blocking** issue is the `direction` default mismatch (`strategy.py` BUG): tasks without an explicit
`direction` will discard every improvement and never converge, and only onboarding-generated tasks set
that field. Secondary risks are low classifier precision (keyword-presence heuristics fire on almost
any SQL diff, add/remove undistinguished) and a direction-naive memory ranker that can prefer the worse
pattern on minimize objectives. Recommend: fix the strategy default + add task-load validation, tighten
classifier anchors and make confidence single-label-gated, and add the missing negative tests before
trusting the adaptive ranking in production.
