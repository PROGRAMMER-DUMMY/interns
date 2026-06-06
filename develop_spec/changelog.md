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

### 2026-06-06  Complete partial-completion threading for mixed KPI sets (Issue #2)
- what: finished threading the deferred (undefined) KPI set through the
  feature-blocker panel and the source-to-target gate so a MIX of defined +
  undefined KPIs reaches generation and yields partial results instead of stalling
  at the feature-blocker stage. Blocker panel now filters out deferred KPIs'
  unresolved features (and empty clusters); source-to-target planner marks
  undefined KPIs `deferred` (excluded from `blocked_kpi_count`, new
  `deferred_kpi_count`); `compute_workflow_diff` records deferred KPIs as deferred
  gaps with no recovery commands. Both the panel and planner self-derive the
  deferred set (consistent with flow._undefined_kpis / the validator), so no flow
  plumbing is required.
- why: Phase 5 (partial) only handled the all-undefined definition gate; a mix
  still stopped at the feature-blocker stage asking about the undefined siblings.
- files: core/onboarding/kpi/blocker_question_panel.py (deferred_kpi_ids threading
  + new `_deferred_kpi_ids_from_registry` helper -- the missing piece that left the
  interrupted Wave-2a stash red); core/onboarding/relationships/
  source_to_target_planner.py (deferred status + count, `_kpi_is_undefined`);
  core/onboarding/workspace/flow.py (compute_workflow_diff deferred gap);
  tests/test_partial_completion_deferred.py (new).
- tests: 8 (deferred-id derivation incl. enumerated ids; feature filtering;
  undefined detection).
- verify: enterprise + workspace_flow suites (106) OK; green-gate 345/0.
- note: panel/planner restored from the interrupted Wave-2a stash and completed;
  stash@{0} now fully salvaged (safe to drop).

### 2026-06-06  Wire parallel-completion planner into run-kpi-pipeline (Issue #4)
- what: `pipeline_main` (run-kpi-pipeline) now calls `dispatch_parallel_completion`
  between the relationship gate and `workspace-flow start`: it builds the
  dependency-aware completion plan and records the parallel-vs-sequential decision
  (instead of the plan only being emitted on demand). Above the ready-KPI threshold
  it recommends the `parallel_kpi_completion` delegation route; at/under it the
  pipeline runs sequentially as before. Advisory + defensive (try/except) so a
  planning failure degrades to a note and never breaks the run. Actual worker
  spawning stays with the delegation layer per the module's contract.
- why: the planner previously only emitted an artifact; the fan-out decision was
  never made/recorded in the deterministic chain.
- files: core/onboarding/workspace/flow.py (pipeline_main wiring);
  core/onboarding/kpi/parallel_completion.py (resolve_parallel_threshold,
  count_ready_kpis, decide_worker_count, DispatchDecision,
  dispatch_parallel_completion -- salvaged from the interrupted Wave-2a stash);
  tests/test_parallel_completion.py.
- tests: tests.test_parallel_completion (16) OK.
- verify: flow.py compiles; green-gate 345/0; pipeline-wrapper test shows only the
  documented pre-existing relationship-gate failure (stash-confirmed, not mine).
- remaining: autonomous concurrent dispatch of the parallel route from the runner
  (today it records the recommendation; the delegation layer performs fan-out).

### 2026-06-06  Verify grain_bucketing facet end-to-end (panel -> apply -> decision)
- what: confirmed #1's `grain_bucketing` facet flows through the live path without
  any panel code change: `blocker_question_panel` -> `intent_facet_panel_questions`
  surfaces the routed facet; `record_intent_answer(facet="grain_bucketing")` mirrors
  to `pipeline_decisions.json` (re-read by the SQL generator); the panel converges
  after the answer is recorded.
- why: #1 unit-tested the facet + recorder in isolation; this closes the
  panel-emission and apply-dispatch seam in-process.
- files: tests/test_kpi_intent_contract.py (TestGrainBucketingPanelE2E).
- tests: 1 in-process e2e (emit -> persist -> converge).
- verify: green-gate 345/0.

### 2026-06-06  Align executable-relationship default (conservative, fail-safe)
- what: `contracts.py::_executable_allowed` defaulted `allowed_in_sql_generation`
  to True when absent; the validator's `validation.py::_relationship_executable`
  defaulted to False. Aligned the contract side to the conservative default
  (`... is True`) so an absent/unknown policy means NOT executable in both places.
- why: a single source of truth for "is this relationship usable in generated SQL."
  Builders always emit the key, so this only hardens malformed/partial contracts;
  101 relationship/enterprise tests stayed green (nothing relied on the True default).
- files: core/onboarding/relationships/contracts.py;
  tests/test_relationship_contracts.py (ExecutableDefaultAlignmentTests).
- tests: 4 (absent->not-exec in both; explicit true/false agree; non-exec state).
- verify: green-gate 345/0.

### 2026-06-06  Workflow guard: completion claim no longer masks unrecovered apply failures (Issue #6)
- what: a run of failed mutation/apply commands followed by a completion/results
  claim was not blocked. The recovery heuristic counted any later `uv run ...`
  (including the completion command itself) as recovery, silencing
  `failed_without_recovery`. Added `_check_completion_after_unrecovered_failures`
  emitting error `completion_claim_over_unrecovered_failures`; tightened
  `_has_retry_or_recovery` + added `_is_mutation_command` / `_is_completion_claim`
  and MUTATION/COMPLETION/RECOVERY token sets.
- why: an "all KPIs proven / complete" claim could be made over unrecovered mutation
  failures; readiness was never blocked. Now propagates workflow-guard -> reliability
  -> project harness as a blocker.
- files: core/onboarding/harness/workflow_guard_harness.py;
  tests/test_workflow_guard_failed_apply_before_completion.py (new).
- tests: 4 (masking reproduction + new error + genuine-retry clean + no-claim clean)
- verify: green-gate 345/0; project_harness + reliability suites green. Generic
  (no domain/CLI-brand hardcoding).

### 2026-06-06  Triage two pre-existing test failures (pytest module + sql-gen stub)
- what: (A) converted tests/test_relationship_state_preservation.py from a pytest
  module to unittest (venv has no pytest; assertions unchanged 1:1). (B) implemented
  the real catalog-bootstrap contract in core/onboarding/pipeline_sql_generator.py
  ::generate() (was a stub) — emits `-- BEGIN/END CATALOG BOOTSTRAP` wrapping
  `catalog_raw_<stem>` readers + per-object bronze_/silver_/gold_<stem> layer views
  referencing only bootstrap views. Removed the two now-passing
  test_pipeline_sql_generator entries from green_gate.py KNOWN_BASELINE.
- why: (A) import error in venv; (B) generator never emitted the bootstrap markers /
  layer names the tests and the kpi/sql_generator.py contract require.
- files: tests/test_relationship_state_preservation.py,
  core/onboarding/pipeline_sql_generator.py, core/dev/green_gate.py.
- tests: 11 (2 + 9) OK
- verify: green-gate 345/0; sweep 0 regressions / 1 known-baseline.

### 2026-06-06  Close derivation-pattern detection gaps (duration-threshold + within-N-days recurrence)
- what: Phase 4 patterns were wired but did not FIRE for the named phrasings.
  Duration: added comparator verbs (exceeds/exceeding/beyond/past/at most/lasting/
  up to/no more|less than) + new explicit-subtraction form `_DURATION_DIFF_RE`
  ("STOP - START > 24 hours"). Recurrence: replaced the `_RECURRENCE_HINTS` substring
  list with `_RECURRENCE_RE` word-stem regex covering noun+verb forms
  (recurrence/recurring/reorder/repeat/readmission/readmitted) without
  false-positiving on release/review/region.
- why: detectors missed exactly the two KPI shapes the follow-up targeted; "readmit"
  is not a substring of "readmission" and noun forms were uncovered.
- files: core/onboarding/features/derivation_patterns.py;
  tests/test_derivation_patterns.py (new cases + genericity guard on the module).
- tests: duration verb/subtraction, recurrence noun-form, re-prefix non-false-positive,
  GenericityGuardTest (31 OK with test_metric_derivation)
- verify: green-gate 345/0.

### 2026-06-06  Hard-block exploded grain for share metrics cut by raw continuous dimensions
- what: escalate the raw exact-age/days-since continuous-cut grain explosion from a
  non-blocking WARN (e9d9d2c) to a hard BLOCKER for share/percentage metrics; the
  generator now PROPOSES fixed-width bands instead of a per-exact-value GROUP BY,
  surfaced via a new low-confidence `grain_bucketing` intent facet routed into the
  blocker panel and persisted to pipeline_decisions like `denominator_scope`.
- why: a share KPI cut by exact integer age fragmented results into ~7,400 rows each
  ~0.2% (meaningless denominator); the WARN did not stop the bad output.
- files: core/onboarding/kpi/result_view_builder.py (share-metric + raw-continuous-cut
  detectors; grain_bucketing param + grain_bucketing_block on ParsedKPI; block before
  GROUP BY; blocked-marker render), core/onboarding/kpi/intent_contract.py (grain_bucketing
  facet + answer mirror), core/onboarding/pipeline_plan.py (record_grain_bucketing),
  core/onboarding/kpi/sql_generator.py (load+thread decision),
  tests/test_result_view_builder.py, tests/test_kpi_intent_contract.py.
- tests: GrainBucketingBlockTests (8) + TestGrainBucketingFacet (6) (OK; re-run by parent)
- verify: tests.test_result_view_builder + tests.test_kpi_intent_contract (14 OK);
  green-gate 345 tests, 0 failing; genericity guard OK.

### 2026-06-06  relationship-approval count non-monotonic (display-only, fixed at envelope)
- what: VERDICT display-only. On-disk counts were always correct (apply recomputes the
  summary from the full persisted list inside workspace_lock); the governed CLI
  idempotent-replay branch echoed the FIRST apply's cached payload, so a re-issued
  approval reported a stale lower executable_relationship_count (e.g. 3->7->4->5->6).
  Fixed at source: on replay, re-run fn() under workspace_lock and report current
  persisted state; fall back to cached payload only if the refresh raises.
- why: not a race (cross-process mutex verified) and not a bad recompute (sequential
  return verified) — only the replay echo was stale.
- files: core/onboarding/workspace/cli_runner.py; tests/test_relationship_apply_count.py
  (new); tests/test_workspace_lock.py.
- tests: test_sequential_apply_count_is_correct_and_monotonic,
  test_replay_reports_current_disk_count_not_stale_payload (reproduces+guards),
  test_concurrent_apply_no_lost_update, test_cross_process_mutual_exclusion (8 OK).
- verify: tests.test_relationship_apply_count + tests.test_workspace_lock (8 OK);
  green-gate 345 tests, 0 failing.

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
