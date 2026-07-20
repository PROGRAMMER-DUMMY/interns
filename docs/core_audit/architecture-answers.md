# Architecture answers — the two machines, Databricks-primary, token economics

Evidence-only, per direct question. `file:line` or a stated grep for every claim. Dated
2026-07-20. No fixes, no plan, no recommendations — answers only.

## 1. The two halves — which one is live?

**Direct answer: the deterministic pipeline (`AGENTS.md`) is live. The optimization
loop (`CONTEXT.md`'s "Enterprise Data Flow") is real, working code with zero non-test
callers outside a single unexercised entry point. It has never run end to end, not in
production, not in a test.**

### Per-component

Every one of the six components below has **exactly one non-test caller**, and it is
the same caller in every case: `core/orchestration/loop.py` (`ExperimentLoop.__init__`,
lines 102–109). Nothing in the deterministic pipeline (`core/onboarding/**`,
`core/dev/harness_cli.py`, `core/orchestration/dagster_defs.py`) imports any of them.
Verified by grep for each import/instantiation site, repo-wide.

| Component | Defined | Instantiated/called outside tests | Verdict |
|---|---|---|---|
| `SemanticContract` | `core/governance/semantic_contract.py:26` | `core/governance/evaluator.py:16`, `core/optimization/planner.py:12`, `core/orchestration/loop.py:50` (internal wiring inside the optimization/governance modules, plus the one external driver) | implemented, wired, single unexercised entry point |
| `ChangeClassifier` (`classify_diff`) | `core/optimization/change_classifier.py:55` (note: no class of this name — it's a module-level function; `ChangeClassification` is the dataclass) | `core/orchestration/loop.py:435` only | implemented, wired, single unexercised entry point |
| `OptimizationMemory` | `core/optimization/memory.py:38` | `core/orchestration/loop.py:102` only | implemented, wired, single unexercised entry point |
| `OptimizationPlanner` | `core/optimization/planner.py:36` | `core/orchestration/loop.py:103` only | implemented, wired, single unexercised entry point |
| `GovernanceEvaluator` | `core/governance/evaluator.py:98` | `core/orchestration/loop.py:107` only | implemented, wired, single unexercised entry point |
| `DecisionStrategy` (`BaseDecisionStrategy`/`SingleMetricDecisionStrategy`) | `core/optimization/strategy.py:3,7` | `core/orchestration/loop.py:109` only | implemented, wired, single unexercised entry point |

None of these are "scaffolding only" — the code is real, does what it claims, and is
unit-tested individually (`tests/test_enterprise_optimization.py:178-337`: one test per
component, isolated/mocked inputs). None is "exercised in a real run" either. The
honest middle bucket the three offered options don't quite name: **wired to a single
real call site that has itself never executed.**

### Has the full loop ever run end to end — production or test?

**No, on both counts.**

- `ExperimentLoop`'s per-iteration method is `_run_one` (`core/orchestration/loop.py:314-492`)
  — this is the only place all six components chain together (planner → intern
  suggestion → `ExecutionBackend` → classifier → governance → memory). Grepped
  `_run_one\(` and `loop.start(` across `tests/`: **zero matches.**
- `tests/test_loop_integration.py` (359 lines) — the loop-specific test file — exercises
  isolated helper methods via a hand-built `_make_loop` stub (retry semantics, review-pause
  gate, crash recovery, live-mutation gate, intern chaining). It never calls `_run_one` or
  `start`.
- `config/tasks.json` currently holds `{"active_task": "", "tasks": []}` — zero configured
  tasks. `ExperimentLoop.__init__` calls `self._load_task(task_id)` against this file
  (`loop.py:75-76`); there is nothing for it to load right now.
- No `state/dry_run/` directory exists anywhere in the repo (checked via `find`) — the
  `--dry-run` flag's own diff-output location has never been written to.
- No `state/REVIEW_PENDING.json` exists anywhere — the review-pause gate has never fired.
- This project's own prior audit work already reached the same conclusion independently:
  `core/observability/cost_ledger.py`'s `EXEMPTIONS` dict (Phase 1a.2a) exempts `loop` with
  the reason `"out-of-launch-scope loop/interns subsystem (Phase 1a retarget)"` — filed
  before this question was asked, for an unrelated reason (cost-anchor coverage), and it
  independently corroborates: `loop` is not the launched platform.
- P0.1 hardening (`208590e`) made this structural: `ExperimentLoop` defaults `dry_run=True`;
  live writes require `--live --confirm-live-mutation` **and** a human-set
  `AUTORESEARCH_ALLOW_LOCAL_MUTATION=1`; the shipped `Dockerfile:32` CMD is the
  non-mutating `green-gate --json`, specifically so a container's default entrypoint is
  never the mutation loop.

### Is there `OptimizationMemory` data on disk anywhere, from a genuine run?

**No.** `OptimizationMemory.record` writes to a SQLite table
(`CREATE TABLE IF NOT EXISTS optimization_memory`, `core/storage/workspace.py:82`) inside
each workspace's `interns/state/workspace.db`
(`core/storage/workspace_layout.py:48`). A repo-wide `find` for `workspace.db` (excluding
worktrees/venv) returns **zero files** — not one, real or fixture, anywhere on this
machine right now. There is nothing to show.

### On the specific claim — "the query-optimization capability may already exist"

Partially right, with the gap being exactly where it matters. `classify_diff`
(`core/optimization/change_classifier.py:21-51`) is a real, correct, regex-pattern-based
labeler — it does produce `predicate_pushdown` / `join_rewrite` / `cte_rewrite` /
`aggregation_rewrite` / `case_simplification` / `column_pruning` labels from a diff, and
is unit-tested (`tests/test_enterprise_optimization.py:178`). But it only ever receives a
diff from inside `loop.py`'s automated code-mutation cycle (`CodeMutator` rewrites
`editable_file`, the before/after diff is classified) — a cycle that, per above, has never
run. It is not wired into the deterministic KPI pipeline actually in use
(`generate-kpi-sql`, `run-kpi-execution-harness`, etc.). So: the capability is not
"missing" in the sense of absent code, and it is not "already available" in the sense of
something a user could invoke today to get an optimization recommendation on real KPI
SQL. It is real, correct, and has never labeled a real change.

## 2. Databricks-primary — proven or aspirational?

**Direct answer: aspirational, and currently disabled by explicit policy, not merely
unexercised. No KPI pipeline has ever completed end-to-end against a real remote
Databricks workspace, as far as any evidence on this machine shows.**

- `config/lock.toml:9` — `enabled = false` — with the comment on the same line:
  `# PHI safety: disabled (non-BAA trial); re-enable only after BAA + de-identification`.
  This is not "not yet configured"; it is deliberately turned off for a stated compliance
  reason.
- `state/databricks/deployments/` — the directory `CONTEXT.md` names as where
  cross-workspace deployment reports and the deployment index live — **does not exist
  anywhere in the repo.** No deployment has ever produced a report to put there.
- Zero `deploy_approval.json` files anywhere in the repo (checked by filename, repo-wide).
  `core/onboarding/databricks/deploy_gates.py:29-33` defines the five gates
  (`G1_local_green` … `G5_remote_approval`, bodies at lines 63, 107, 130, 146, 171); no
  workspace has ever produced the approval artifact those gates check for.
- **First-hand, this-session evidence.** Running `medallion apply-deploy` in this session
  printed: `[x] medallion deploy refused: no deployment approval artifact at
  ws/interns/state/medallion/deploy_approval.json; run \`medallion apply-deploy --workspace
  <ws> --confirmed-by <name>\` first (the refusal IS the feature)`, and all five gates
  failed, including `G5_remote_approval: AUTORESEARCH_ALLOW_REMOTE_EXECUTION is not 1; a
  human must set it in the executing shell (agents must never set it)`. Separately, the
  execution backend printed: `[execution_backend] Databricks configured; remote execution
  requires explicit approval. Set AUTORESEARCH_ALLOW_REMOTE_EXECUTION=1 to use it. Falling
  back to DuckDB.` (`core/execution/backend.py:543`).
- `AUTORESEARCH_ALLOW_REMOTE_EXECUTION` is set to `"1"` **only** inside test
  `setUp`/`tearDown` blocks that unit-test the gate itself
  (`tests/regressions/test_core_p2_gates.py:38,71`, `tests/test_enterprise_optimization.py:2850`).
  Every non-test occurrence in the repo is a check, a doc reference, or a skill
  description — never a log line showing it was actually set during a real run.
- `mlflow.db` (repo root, 880 KB) does contain 15 real, finished MLflow runs across two
  experiments (`/medallion/Healthcare-RCM-Data-Platform`,
  `/medallion/Hospital_Patient_Records`) — this is genuine evidence of `DatabricksTelemetry`
  actually writing. But every run's tags read `mlflow.runName = 'duckdb'` and
  `mlflow.source.type = 'LOCAL'`, tracked against the local SQLite backend store with
  `file:///…/mlruns/…` artifact URIs. This is local-execution telemetry mirrored to a
  local MLflow store — not a Databricks-hosted experiment, and not evidence of remote
  execution.
- No Delta-to-Unity-Catalog write evidence, no Databricks job-run logs, found anywhere.

Every KPI pipeline run this project has evidence of — including every run driven in this
session and the 15 MLflow-tracked medallion builds — executed against local DuckDB.

## 3. Token economics — still outstanding

**Direct answer: the "~44 pp of model quota" claim (`CLAUDE.md:180`,
`AGENTS.md:485`) has no traceable methodology, citation, or date anywhere in the repo. It
cannot be confirmed or refuted in the terms it's stated, because nothing in this repo
converts a token count into a percentage of account quota. What the ledger can and does
now measure — real, per-run token counts — is reported below.**

- Repo-wide grep for `44 pp|44%|quota`: the figure appears verbatim in `CLAUDE.md:180`
  and `AGENTS.md:485`, in both cases as a bare assertion ("Per-run token cost is ~44 pp of
  quota") with no formula, no source run, no date attached.
  `tools/token_report.py` (the repo's only token-measurement tool prior to Phase 1a)
  measures **context-window size** — file/section token counts for CLI startup context —
  and contains zero references to "quota" anywhere in the file. No other module computes
  or stores a quota percentage. The claim is unverifiable as written; it is not derived
  from anything currently in the repo.

**What the ledger measures instead — two real runs, this project's actual pipeline,
this session:**

Both runs: `onboard-workspace` → `resolve-kpi-features` → `medallion design` →
`medallion build` (gated/blocked, still anchors) → `workspace-dashboard --screen
--no-refresh`, on Healthcare-RCM, temporal-attribution methodology from the completed
1a.2c-precondition slice (`core/observability/cost_ingest.py`; per-run window intersected
against transcript timestamps, systematic undercount documented and labeled).

| Run | Date | Driving pattern | Window | Turns caught | Total tokens | in / out / cached |
|---|---|---|---|---|---|---|
| A | 2026-07-19 | 4 separate turns | 96.5s | 4 | **785,216** | 8 / 896 / 784,312 |
| B | 2026-07-20 | 3 separate turns | 114.4s | 2 | **470,729** | 4 / 420 / 470,305 |

Both real, both from `temporal_approximate` (never the whole-session total — the
`session_total` figure for run B's session was 127,825,317 tokens across 585 turns
spanning 2026-07-18 to 2026-07-20, which is the multi-day agent session, not one
pipeline run, and is excluded here for exactly the reason recorded in
`docs/core_audit/cost-ledger.md`). Both driving-pattern-sensitive per the same doc's
standing rule — this is a range (470K–785K tokens for one onboard→resolve→design→build→
dashboard cycle), not a single number, and is not claimed to be exhaustive across all
possible driving patterns.

**On converting this to "pp of quota":** not done, and not derivable from anything in
this repo. A percentage of quota requires knowing the account's absolute token quota,
which is plan-specific and not a value any file in this repo defines, stores, or
computes. One qualified observation, not a fact: in both real measurements the token
total is **>99% cache-read** (784,312/785,216 and 470,305/470,729), a token class
providers typically price and rate-limit far more cheaply than fresh input. If "~44 pp"
was derived from a raw token count without accounting for that discount, it would
overstate real quota consumption — but there is no evidence in the repo of how "~44 pp"
was originally computed, so this is a plausible explanation, not a confirmed one.
