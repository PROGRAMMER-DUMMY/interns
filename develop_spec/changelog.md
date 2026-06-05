# develop_spec/changelog.md — what changed (newest first)

Append a dated entry after every change. Keep entries short: what / why / files /
tests / verification. This is the dev history of the PLATFORM code; it is not the
end-user `session-snapshot` audit and not git itself.

Entry template:

```
### YYYY-MM-DD  <short title>
- what:   <one or two lines>
- why:    <the bug/goal>
- files:  <key files touched>
- tests:  <suites added/run> (result)
- verify: <command(s) run to confirm>
```

---

### 2026-06-05  Activation/reliability loop + semantic grill + derivation patterns (Phases 0-5)
- what: convert advisory/CI-only guards into in-envelope hooks, generic + pipeline-wide.
  - P0/1/2: core/governance/op_signals.py + cli_runner hooks (live tripwire
    reliability + signal->skill activation) + empty-panel carries routing.
  - P3: verify_kpi_output non-blocking semantic gloss mismatch; generation passes
    dictionary glosses into derivation.
  - P4: core/onboarding/features/derivation_patterns.py (duration bucket +
    recurrence self-join), wired into the resolver's undefined-KPI branch.
  - P5 (partial): definition gate blocks only when ALL undefined; generation skips
    deferred. Full partial-result threading deferred (see follow_ups).
- why: skills/reliability tools lived BESIDE the pipeline, never fired on the live
  path (the "why didn't anything activate" gap); engine ignored its own kpi-analyst
  rule; feature-derivation-library had no built-in patterns.
- tests: test_op_signals, test_verify_semantic_gloss, test_derivation_patterns
  (new) + flow/derivation/panel suites green on the venv interpreter.
- commits: 4596bef, 05ad704, c43c663, e3e1ff0 (after base commit 981a0f7).
- NOTE: parallel worktree agents were attempted first but failed -- worktrees
  branch from the last commit and the whole session was uncommitted; committed the
  base (981a0f7) then built inline.

### 2026-06-05  Dictionary-grounded + specificity-aware measure selection
- what: derivation now obeys AGENTS.md "Data Model Driven Generation" (don't map
  on column-name similarity alone). Two generic, scenario-based rules:
  (1) SPECIFICITY tie-break — a non-entity question term outranks the entity/table
      word when ranking measures (`_measure_specificity` in metric_derivation.py);
  (2) GLOSS grounding — column data-dictionary descriptions are wired into
      derivation via onboarding `_load_column_glosses` (+ derive's existing
      dictionary_entries). No file/column hardcoded; convention-based discovery.
- why: kpi_007 "average total claim cost" picked `BASE_ENCOUNTER_COST` because
  "encounter" matched by name; the dictionary that defines `Total_Claim_Cost` was
  never consulted (the system violated its own kpi-analyst skill rule).
- files: core/onboarding/kpi/metric_derivation.py, core/onboarding/workspace/
  onboarding.py; tests/test_metric_derivation.py (MeasureSpecificityTests).
- tests: 63 across derivation-dependent suites green.
- verify: removed the human override and re-onboarded -> engine self-derives
  `avg(TOTAL_CLAIM_COST)` / `PAYER`.

### 2026-06-05  Fix phantom age dimension in result-view SQL
- what: `_AGE_PATTERN` "age of/from <col>" alternative had no leading `\b`, so
  "percent`age of` total" matched and treated "total" as a date column ->
  `date_diff('year', CAST("total" AS DATE)) AS age` on a percentage KPI.
- why: redefined kpi_004 (zero payer coverage %) generated broken, ungrouped SQL.
- files: core/onboarding/kpi/result_view_builder.py; tests/test_result_view_builder.py
  (AgeAsOfEventDateRegressionTests.test_percentage_of_total_name_does_not_emit_phantom_age_dimension).
- tests: result-view suite green; new regression added.
- verify: regenerated kpi_004 -> clean `COUNT(*)/COUNT(*) OVER ()*100 WHERE
  PAYER_COVERAGE = 0`; executed on real data -> 13,586/27,891 = 48.71%.

### 2026-06-05  apply-kpi-definition loop-close (human-confirmed KPI defs)
- what: new `apply-kpi-definition` CLI + decision store
  (`interns/generated/decisions/kpi_definitions.json`) + onboarding re-apply.
  Persists a human-confirmed metric/grain (source: human, --confirmed-by),
  mirrors into the live contract, survives re-onboarding. Closes the loop so the
  `kpi_definition_required` blocker can actually be answered.
- why: the panel could ask for a KPI definition but nothing could apply it ->
  NL KPIs could never complete (the Gemini session's deeper blocker).
- files: core/onboarding/kpi/kpi_definition.py (new), core/onboarding/workspace/
  onboarding.py (_apply_accepted_kpi_definitions), pyproject.toml; tests/
  test_kpi_definition_apply.py (new).
- tests: 64 across affected suites green (.venv).
- verify: applied kpi_004/005/006 on Hospital_Patient_Records ->
  ready_kpi_count 5 -> 8 (kpi_003/010 deferred).
- FOUND (pre-existing, not from this change): generation stamps one global
  result_format on all KPIs -> broken SQL for non-time-series KPIs. See
  follow_ups.md (per-KPI result_format) — this blocks REAL result tables.

### 2026-06-05  KPI dedupe + string-date derivation + parallel-completion planner
- what:
  - Dedupe KPIs by business question across registries (fixes 20-vs-10 double
    count when a generated registry + its source `.sql` were both ingested).
  - Recognize string-typed ISO timestamps as dates via sample-value evidence, and
    recover the count grain from the entity's own table name when distinct-counts
    are absent; onboarding now derives empty metric/cuts from profile evidence.
  - Run the `kpi_definition_incomplete` gate before the feature-blocker panel; the
    resolver emits an answerable definition blocker for empty KPIs (no more silent
    "0 questions" dead-end on the direct panel path).
  - New `plan-kpi-completion` CLI + `core/onboarding/kpi/parallel_completion.py`:
    dependency-aware parallel plan (shared blockers resolved once; independent
    components fanned across 2/4/6 workers). New `parallel_kpi_completion` routing
    stage.
- why: workspace showed 20 blocked KPIs / 0 questions (dead end) + duplicate
  count; goal also to complete many KPIs faster.
- files: core/onboarding/workspace/onboarding.py, core/onboarding/kpi/
  metric_derivation.py, core/onboarding/kpi/feature_resolver.py, core/onboarding/
  kpi/blocker_question_panel.py, core/onboarding/workspace/flow.py, core/
  onboarding/kpi/parallel_completion.py, core/onboarding/workspace/delegation.py,
  pyproject.toml; tests/test_metric_derivation.py, tests/test_kpi_registry_dedupe.py
  (new), tests/test_parallel_completion.py (new), tests/test_workspace_flow.py.
- tests: 75 across the touched + adjacent suites green (.venv unittest).
- verify: re-onboard + resolve on a sample workspace -> 10 KPIs (was 20),
  5 ready_for_sql, blocker panel non-silent; `validate-workspace-artifacts` ok=true;
  `plan-kpi-completion` -> 4 components / 2 workers.
- notes: two PRE-EXISTING failures unrelated to this change — see testing.md.
