# Platform Reliability + SQL Coverage Plan
_Authored 2026-05-29 by the orchestrating CLI agent after a deep workspace-agnosticism + advanced-SQL session._

This plan covers (1) the SQL coverage that remains after Tier A landed,
(2) reliability improvements informed by the five Anthropic AI-safety
research areas applied to *this* platform, and (3) a prioritized roadmap.

It is intentionally action-oriented. Each item has a concrete file path,
LOC estimate, and acceptance criteria. The goal is that any future
session can pick up an item without re-deriving the architecture.

---

## 0. Current state snapshot (so the plan is grounded)

**Tests:** 88 pass + 1 skip across 11 suites. Genericity audit enforces
8 named violation patterns. 5-tier artifact lifecycle, 14 specialist
subagents registered, vocabulary research module, dashboard MVP, wiki
preservation, delegation pipeline, all in place.

**SQL coverage today (`core/onboarding/kpi/result_view_builder.py`):**
- Aggregations: `sum / avg / count / count distinct / min / max`
- Conditional count via predicates: `count(col = 'literal')`
- Ratios: `A / B` with `NULLIF` guard
- Time bucketing: `year / quarter / month / week / day` via `date_trunc`
- Filters: comparison operators + quoted literals from `cuts`
- `ORDER BY ... DESC LIMIT N` from "top N" in KPI name
- **Tier A**: window functions (percent-of-total, share-of-group,
  running total, moving average, rank within), HAVING clauses, date
  arithmetic (`age (X)`, `days since X`)
- Joins: one-hop LEFT JOIN via executable `relationship_contracts.json`
- Catalog bootstrap: `CREATE OR REPLACE VIEW catalog_raw_*` per dataset
- Multi-source `UNION ALL BY NAME` for the bootstrap-row view

**Tests/fixtures available:**
- `tests/test_result_view_builder.py` (27)
- `tests/test_workspace_flow_generic.py` (3 — retail end-to-end)
- `tests/test_workspace_vocabulary_research.py` (7)
- `tests/test_genericity_audit.py` (9)
- `tests/test_relationship_state_preservation.py` (2)
- `tests/test_dashboard_spec_preservation.py` (3)
- `tests/test_dashboard_callback_live.py` (5)
- `tests/test_delegation_pipeline.py` (8)

---

## Part 1 — Remaining SQL coverage

### Tier B — Compositional logic (~320 LOC)

Needed when a KPI's semantics are multi-step: "first compute X per Y,
then aggregate Z over X" or "find the next event within N days".
Without Tier B, these KPIs fall through to `SELECT *` even when their
shape is well-defined.

#### B.1 Inline CTEs in the result view (~120 LOC)

**Trigger patterns in KPI text:**
- `"first ... then ..."`
- `"per X, count Y"` (compute Y at per-X grain, then aggregate)
- Metrics that nest aggregations: `avg(count_per_customer)`

**Implementation:**
1. Extend `ParsedKPI` with `cte_stages: list[CTEStage]` where
   `CTEStage(name, select_expr, group_by, source_view)`.
2. Detect nested aggregations in `parse_kpi` via a stack-aware regex
   on parenthesized aggregations.
3. SQL composer emits `WITH stage_1 AS (...), stage_2 AS (...) SELECT ... FROM stage_2`.

**Acceptance:**
- `tests/test_result_view_builder.py::test_cte_nested_aggregation_emits_with_chain`
- Generic across domains (test fixture: retail "avg order count per customer").

#### B.2 Self-joins for sequence/cohort analysis (~200 LOC)

**Trigger patterns:**
- `"within N days"` — sequence analysis
- `"next X after Y"` — event correlation
- `"first / last X per Y"` — anchored cohort
- `"readmission"` / `"retention"` / `"churn within N"` — domain-agnostic
  cohort phrases that all reduce to time-window self-join

**Implementation:**
1. New module `core/onboarding/kpi/sequence_builder.py` with a
   `SequenceSpec` dataclass capturing self-join shape.
2. Detect patterns in `parse_kpi`; build a `SequenceSpec` per matched
   pattern.
3. SQL composer emits `FROM <view> t1 JOIN <view> t2 ON t1.key =
   t2.key AND t2.date BETWEEN t1.date + 1 AND t1.date + N`.
4. Generic across domains: retail "repeat purchase within 30 days",
   healthcare "readmission within 30 days", finance "trade within 1 day".

**Acceptance:**
- `tests/test_sequence_builder.py` (8 tests across 3 synthetic domains)
- No domain words hardcoded; trigger phrases are generic English.

### Tier C — Specialized SQL (~480 LOC)

Each item below is small and useful for specific KPI shapes. Ship
on-demand when an actual KPI needs them.

| Feature | Trigger patterns | LOC | KPI shape unlocked |
|---|---|---|---|
| NULL handling (`COALESCE`, `IFNULL`) | per-feature `null_policy` in `kpi_feature_mapping.json` | 30 | "Treat NULL paid as 0" |
| String cleaning (`LOWER`, `TRIM`, `REPLACE`, `REGEXP_REPLACE`) | per-feature `cleaning_rules` in mapping | 80 | "Canonicalize Gender", "Strip whitespace from PayorID" |
| INNER / OUTER join semantics | per-relationship `join_type` in `relationship_contracts.json` | 40 | "Inner join customers to orders (drop orderless)" |
| UNION / EXCEPT / INTERSECT | `"customers who bought A AND B"`, `"X but not Y"` | 80 | Set-membership KPIs |
| PIVOT / UNPIVOT | `"revenue by month as columns"` | 150 | Crosstab reports |
| CASE buckets in SELECT | `"by age group: 0-18, 19-64, 65+"`, `"by tier"` | 60 | Bucketed dimensional analysis |
| CAST / type conversion | per-column `cast_to` in mapping | 40 | Currency precision, type-fix joins |

---

## Part 2 — Reliability improvements through an AI-safety lens

Each of the five research areas maps to a concrete platform mechanism.
Several are already partially in place from this session; this section
lists what to extend or harden.

### 2.1 Scalable oversight — humans stay in control even as outputs get complex

**Today the platform produces:** generated SQL, dashboard JSON specs,
wiki notes, recovery commands, delegation verdicts. As Tier B/C land,
SQL complexity grows past easy human verification.

**Extensions:**

| # | Mechanism | Why | LOC | File |
|---|---|---|---|---|
| O.1 | **Builder explain mode** — every emitted SQL is accompanied by the parsed `ParsedKPI` dataclass dump showing which patterns matched and what the builder *intended*. Reviewer can compare intent vs. SQL without reading SQL. | Closes the "I see SQL but I can't tell if it's right" gap. | 60 | `result_view_builder.py` → `build_result_view_sql(..., explain=True)` returns `(sql, explanation)` |
| O.2 | **Verifier subagent** — `kpi-analyst` actually invoked at the completion stage (not just programmatic verdict) with the explanation + sample result rows. Returns a verdict (`semantic_match` / `mismatch`/ `uncertain`). | Closes "we have specialist agents but never invoke them" gap. Real scalable oversight. | 150 | `core/onboarding/kpi/verifier.py` + wire into `flow.py` |
| O.3 | **Sandbox execution against synthetic ground-truth data** — for KPIs that have a known shape, the builder runs its generated SQL against tiny synthetic rows where the answer is hand-computed, then checks. | Catches generator bugs before they ship per-workspace. | 200 | `tests/test_builder_sandbox_execution.py` + `core/onboarding/kpi/sandbox.py` |
| O.4 | **Two-agent disagreement panel** — when two specialist verdicts disagree, surface a panel to the user with both views. | Disagreement = the canonical "human must decide" signal. | 80 | `flow.py` panel + `clarify-ambiguity` skill |

### 2.2 Adversarial robustness — fail-safe under hostile / pathological inputs

**Today the platform trusts:** the user-provided KPI workbook, data
dictionary, profile_index, workspace_settings, and dataset files. None
is sanitized beyond basic type checks.

**Extensions:**

| # | Mechanism | Why | LOC | File |
|---|---|---|---|---|
| R.1 | **SQL-injection-resistant metric parser** — current parser ingests `metric` text into SQL. A malicious metric like `sum(x); DROP TABLE customers; --` would compose into the result view. | Hardening for any future multi-tenant or shared workspace deployment. | 50 | `result_view_builder.py` — reject any token outside the AST our parser produces; never `f"{user_text}"` into SQL |
| R.2 | **Path traversal protection** — `dataset_allowlist` and dictionary paths currently accept any relative path. `../../etc/passwd` would resolve. | Even single-user platform should refuse. | 30 | `core/storage/external_data.py` |
| R.3 | **Schema poisoning resistance** — a hand-edited `profile_index.json` with claimed columns that don't exist could cause the generator to write invalid SQL referencing missing columns. | Validate at load time. | 40 | `core/onboarding/relationships/contracts.py::_profile_index` |
| R.4 | **Resource limits per workflow step** — a KPI metric `sum(*)` on a 100M-row CSV that DuckDB tries to load whole. Add `LIMIT` defaults for sample executions, time budgets. | Production deployment safety. | 80 | `core/onboarding/kpi/execution_harness.py` |
| R.5 | **Fuzz tests for the metric/cuts parser** — use `hypothesis` to generate adversarial KPI text and assert the parser either parses cleanly or rejects, never crashes or composes garbage. | Catches a class of bugs the example-based tests can't reach. | 100 | `tests/test_result_view_builder_fuzz.py` |
| R.6 | **Conflicting evidence resolution** — when data dictionary says `X is PK of Y` but profile shows `X` has duplicates, the platform currently silently trusts the dictionary. | Surface the conflict; require user resolution. | 60 | `core/onboarding/relationships/contracts.py` |

### 2.3 Model organisms of misalignment — controlled failure studies

**Today we have:** synthetic retail workspace, synthetic generic
workspace for relationship + dashboard tests. These are *positive*
fixtures (things should work).

**We do NOT have:** *negative* fixtures (things that should fail or
require user intervention).

**Extensions:**

| # | Fixture | What it probes | LOC |
|---|---|---|---|
| M.1 | **Ambiguous-KPI workspace** — KPIs whose `metric` could mean two different things ("customer churn" with no clear definition). Expected behavior: clarify-ambiguity panel fires; never silently picks. | Tests the clarification fallback. | 80 |
| M.2 | **Conflicting-evidence workspace** — dictionary says one thing, profile says another. Expected: validation-gatekeeper flags it. | Tests conflict resolution. | 60 |
| M.3 | **Trojan-data-dictionary workspace** — dictionary claims `users.password` is a key column (sensitive). Expected: workspace-governance skill refuses to surface in samples. | Tests the secret-display rule. | 50 |
| M.4 | **Drift-over-time workspace** — same KPIs, but datasets change schema between runs. Expected: the state preservation contracts keep user_confirmed decisions but the new schema surfaces as blocker. | Tests schema-drift handling. | 100 |
| M.5 | **Multi-domain fixtures** — retail (have), healthcare (have), plus finance, manufacturing, education. Each with its own vocabulary; cross-test that no leakage occurs across runs. | Forces the vocabulary research to handle real domain breadth. | 200 |

### 2.4 Mechanistic interpretability — make the platform's decisions readable

**Today:** trajectory log, delegation events, manifest, README, wiki
preservation. These ARE interpretability mechanisms. They mostly
capture *what happened*, less *why this specific choice was made*.

**Extensions:**

| # | Mechanism | What it shows | LOC | File |
|---|---|---|---|---|
| I.1 | **Per-decision rationale traces** — every relationship contract, every KPI feature resolution, every chart-type inference writes a `reasoning: list[str]` array explaining the rules that fired. | "Why did the platform decide this column is the PK?" → readable rationale | 100 | Cross-cutting: extend dataclasses in `contracts.py`, `feature_resolver.py`, `inference.py` |
| I.2 | **Builder AST visualizer** — `workspace-flow explain --kpi <id>` prints the `ParsedKPI` tree alongside the SQL with line-by-line annotations. | One command tells you exactly how the builder interpreted the KPI. | 80 | New CLI subcommand in `flow.py` |
| I.3 | **Vocabulary confidence heatmap** — `workspace-dashboard --explain` overlays the vocabulary's per-term confidence on the chart's axes (e.g., "`Refunded` confidence: 0.85; chose as filter literal because cuts text matches"). | Surfaces the workspace research that drove the chart choices. | 120 | `core/dashboard/explain.py` + render in dashboard CSS |
| I.4 | **Decision-diff between sessions** — when running `workspace-flow start` on a workspace that's been run before, emit a panel: "vs prior session: 3 relationships re-confirmed, 1 new feature mapping inferred from updated profile, 0 user_overrides changed." | Lets the user see *what changed* in the platform's understanding. | 100 | `flow.py` start path |

### 2.5 AI welfare — respect prior intent

The platform's "agents" don't have welfare in the AI-research sense
(they're prompts and Python code). But the *principles* — never
silently destroy prior agreed-on state, never burn context
unnecessarily, honor refusal — apply to platform UX.

**Mostly done already:**
- `user_overrides` preservation (dashboard, wiki) — done
- `user_confirmed` relationship state preservation — done
- Workspace allowlist honored at every read — done
- `jitContext: true` — done

**Remaining:**

| # | Mechanism | Why | LOC |
|---|---|---|---|
| W.1 | **Refusal-respect: explicit `excluded_datasets` list in workspace_settings** — once a user says "never use Hospital B data", honor it across every CLI without re-prompting. | Today the allowlist tells the platform what's ALLOWED; we don't have an explicit DENY list. | 40 |
| W.2 | **Decision rollback** — every `apply-*` op is reversible via `revoke-op --op-id <id>`. Restores the pre-apply state. | Honors that the user might change their mind. | 100 |
| W.3 | **Context-budget honoring** — when `workspace-flow context-status` returns `critical`, automatic handoff fires instead of letting the orchestrator hit the wall. | Today only manual handoff. | 50 |

---

## Part 3 — Cross-cutting reliability engineering

Not tied to any one safety area; needed regardless.

| # | Mechanism | Why | LOC |
|---|---|---|---|
| X.1 | **Property-based testing for the SQL builder** — `hypothesis` generates KPI shapes; assert builder either composes cleanly or marks fallback. Never crashes, never silently wrong. | Reaches edge cases example-based tests miss. | 150 |
| X.2 | **Mutation testing** — `mutmut` against `core/onboarding/kpi/result_view_builder.py`. Mutation-killing rate as a CI gate. | Proves tests actually test something. | (CI config + mutmut.cfg) |
| X.3 | **Contract schema versioning + migration** — every JSON artifact gets a `version` field; loader supports forward + backward migration. | Today bumping a contract shape breaks workspaces. | 200 across loaders |
| X.4 | **Replay buffer** — every workflow run is replayable from `trajectory.jsonl` + workspace artifacts. CI replays N recent runs nightly to catch silent regressions. | Catches "works in tests, fails in real session" gaps. | 250 |
| X.5 | **Canary KPIs** — a small set of synthetic KPIs across all 5 tiers (basic, ratio, window, HAVING, complex CTE) that CI exercises end-to-end against the synthetic generic workspace. | Smoke test that's not a unit test. | 80 |
| X.6 | **Numeric tolerance asserts** — for any test that exercises live SQL execution, assert results within tolerance (not exact equality) to allow for floating-point or ordering differences across DuckDB versions. | Prevents flaky CI when the SQL engine updates. | 30 |
| X.7 | **Backwards-compat contract on `workspace_vocabulary.json`** — adding new categories must not break consumers that read old categories. | Lets the schema evolve. | 30 |

---

## Part 4 — Prioritized roadmap

Three buckets. P0 unblocks real production use; P1 hardens it; P2 is
nice-to-have.

### P0 — Required before claiming "production ready"

1. **Tier B compositional CTEs** (B.1) — fixes any nested-aggregation KPI silently producing wrong results.
2. **R.1 SQL-injection-resistant metric parser** — table stakes for any deployment.
3. **R.5 Fuzz tests for parser** — catches the bug class R.1 fixes plus more.
4. **O.1 Builder explain mode** — without it, no human can verify a generated SQL is right.
5. **X.1 Property-based testing** — builder's correctness needs more than example tests.
6. **W.2 Decision rollback** — production users will make mistakes; need a safe undo.

**Estimated total:** ~620 LOC + tests. One focused multi-session sprint.

### P1 — Significantly improves reliability

7. **Tier B self-joins** (B.2)
8. **R.2 Path traversal protection** (R.2)
9. **R.3 Schema poisoning resistance** (R.3)
10. **R.4 Resource limits** (R.4)
11. **R.6 Conflicting evidence resolution** (R.6)
12. **O.2 Verifier subagent actual invocation** (O.2)
13. **O.3 Sandbox execution against synthetic ground-truth** (O.3)
14. **I.1 Per-decision rationale traces** (I.1)
15. **I.2 Builder AST visualizer** (I.2)
16. **M.1, M.2, M.4 Misalignment fixtures** (negative tests)
17. **X.3 Contract schema versioning** (X.3)
18. **X.4 Replay buffer** (X.4)
19. **X.5 Canary KPIs** (X.5)

**Estimated total:** ~2,000 LOC + tests. Two-three sprints.

### P2 — Specialized capabilities + polish

20. All of Tier C (NULL, string cleaning, JOIN types, UNION, PIVOT, CASE buckets)
21. M.3, M.5 Trojan + multi-domain fixtures
22. O.4 Two-agent disagreement panel
23. I.3 Vocabulary confidence heatmap
24. I.4 Decision-diff between sessions
25. W.1 Refusal-respect explicit deny list
26. W.3 Context-budget auto-handoff
27. X.2 Mutation testing
28. X.6 Numeric tolerance asserts
29. X.7 Vocabulary schema back-compat

**Estimated total:** ~1,500 LOC + tests. On-demand as KPIs require them.

---

## Part 5 — What to ship next (concrete recommendation)

If picking one batch:

> **Tier B (B.1 + B.2) + R.1 + O.1 + X.1 + W.2 = the "production-ready
> floor"** (~750 LOC, one focused session). Closes the worst remaining
> SQL coverage gap, hardens against the most likely security failure
> mode, makes generated SQL human-verifiable, locks the builder's
> correctness with property tests, and gives the user an undo button.

If picking the single highest-leverage item:

> **O.3 — Sandbox execution against synthetic ground-truth data.** It's
> the difference between "tests pass" and "the generated SQL produces
> correct numbers for known inputs." Once this is in place, every Tier
> B / Tier C addition gets an automatic correctness oracle. ~200 LOC.

---

## Appendices

### A. AI-safety area ↔ platform mechanism mapping (quick reference)

| Anthropic research area | Platform mechanism today | Extension proposed |
|---|---|---|
| Scalable oversight | delegation pipeline + genericity audit | O.1–O.4 |
| Adversarial robustness | genericity audit, fallback SQL | R.1–R.6 |
| Model organisms of misalignment | synthetic retail/generic fixtures | M.1–M.5 |
| Mechanistic interpretability | trajectory log, delegations, MANIFEST.md, README.md, wiki preservation | I.1–I.4 |
| AI welfare (applied to platform UX) | user_overrides, user_confirmed state, workspace allowlist, jitContext | W.1–W.3 |

### B. Acceptance criteria template

Every item ships with:
1. Generic across workspaces (synthetic retail + generic fixtures pass).
2. No `# noqa: genericity` lines added (audit threshold = 0).
3. At least one regression test under `tests/`.
4. Trajectory event recording it ran.
5. If it's a new contract field, a brief note in this plan.

### C. Files / modules touched by this plan (for grep)

- `core/onboarding/kpi/result_view_builder.py` (Tier B/C, O.1, R.1, X.1)
- `core/onboarding/kpi/sequence_builder.py` (new — B.2)
- `core/onboarding/kpi/verifier.py` (new — O.2)
- `core/onboarding/kpi/sandbox.py` (new — O.3)
- `core/onboarding/kpi/execution_harness.py` (R.4)
- `core/onboarding/relationships/contracts.py` (R.3, R.6, I.1)
- `core/onboarding/workspace/flow.py` (I.2, I.4, W.2, W.3)
- `core/storage/external_data.py` (R.2)
- `core/dashboard/explain.py` (new — I.3)
- `tests/test_result_view_builder.py` (Tier B/C tests)
- `tests/test_result_view_builder_fuzz.py` (new — R.5)
- `tests/test_builder_sandbox_execution.py` (new — O.3)
- `tests/test_sequence_builder.py` (new — B.2)
- `tests/fixtures/misalignment/` (new — M.1–M.5)
- `workspaces/fixtures/synthetic_generic/` (new — M.5 multi-domain)

### D. Open questions for the next session

1. Do we want the verifier subagent to actually invoke an LLM, or to
   run programmatic checks only? LLM-backed verification is the proper
   scalable-oversight pattern but adds cost + non-determinism.
2. Should `revoke-op --op-id` (W.2) restore strictly, or also flag
   downstream artifacts that depended on the now-revoked decision?
3. Tier C PIVOT — is the use case real for this platform, or skip?
4. M.5 multi-domain fixtures — should we generate 5 fixture domains in
   one batch, or only the next one the user actually onboards?
5. Should the genericity audit also forbid `medicare`/`patient`/etc.
   literals inside test files (no, tests are allowed to be specific)
   or only in `core/` + `tools/`?
