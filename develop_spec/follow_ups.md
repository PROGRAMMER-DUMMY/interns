# develop_spec/follow_ups.md — open next items

Loose ends to pick up. Check an item off (move it to changelog.md when done with
a dated entry). Keep this honest — it is our shared backlog.

Item template: `- [ ] <area>: <what> — <why / where>`

## Open

- [x] **generation: raw continuous-grain explosion now BLOCKS for share metrics
      (FIXED 2026-06-06).** Escalated the exact-age/days-since continuous cut from
      WARN (e9d9d2c) to a hard blocker for share/percentage metrics; generator
      proposes fixed-width bands and routes a `grain_bucketing` intent facet into the
      blocker panel (mirrors `denominator_scope`). `result_view_builder.py` +
      `intent_contract.py` + `pipeline_plan.py` + `sql_generator.py` + tests.
- [x] **flow: relationship-approval count non-monotonic — Issue #5 (FIXED
      2026-06-06).** Display-only artifact: the governed idempotent-replay branch in
      `cli_runner.py` echoed the first apply's cached payload. Replay now re-reads
      current persisted state under the lock. On-disk counts were always correct (not
      a race; cross-process mutex verified). Guarded by
      `tests.test_relationship_apply_count`.
- [x] **grain_bucketing: end-to-end seam verified (2026-06-06).** Confirmed the
      facet flows generically: `blocker_question_panel` calls
      `intent_facet_panel_questions` -> routed `grain_bucketing` facet surfaces;
      `record_intent_answer(facet="grain_bucketing")` mirrors to
      `pipeline_decisions.json` (re-read by the generator); panel converges after
      answer. In-process e2e test
      `tests.test_kpi_intent_contract.TestGrainBucketingPanelE2E` (no panel code
      change needed). A live subprocess CLI run is still worthwhile but the wiring
      is proven.
- [ ] **grain_bucketing: derive band width from evidence.** Currently a fixed
      `_DEFAULT_BUCKET_WIDTH=10`; prefer deriving from profile evidence (observed
      min/max range) per "derive, don't curate."
- [x] **executable-relationship default divergence (FIXED 2026-06-06).** Aligned
      `contracts.py::_executable_allowed` to the conservative default (`... is True`)
      to match `validation.py::_relationship_executable`; absent key -> not
      executable in both. Builders always emit the key so no runtime change on real
      workspaces (101 relationship/enterprise tests stayed green). Guarded by
      `tests.test_relationship_contracts.ExecutableDefaultAlignmentTests`.
- [x] **pre-existing failures triaged (FIXED 2026-06-06).**
      `tests.test_relationship_state_preservation` converted pytest->unittest;
      `tests.test_pipeline_sql_generator` was a real stub regression — implemented
      the catalog-bootstrap contract in `core/onboarding/pipeline_sql_generator.py`
      and removed the two now-passing entries from `green_gate.py` KNOWN_BASELINE.
- [x] **reliability: completion claim masked unrecovered apply failures — Issue #6
      (FIXED 2026-06-06).** `workflow_guard_harness.py` recovery heuristic counted any
      later `uv run` (incl. the completion command) as recovery. Added
      `_check_completion_after_unrecovered_failures` (error finding) that blocks
      readiness through workflow-guard -> reliability -> project harness.
- [ ] **reliability: structured completion marker.** Have run-kpi-pipeline /
      workspace-flow results record an explicit `completion`/`results` trajectory
      event_type so the new check keys on a marker instead of broad token matching.

- [x] **generation: phantom age dimension (FIXED 2026-06-05).** `_AGE_PATTERN`'s
      `age of/from <col>` alternative lacked a leading `\b`, so "percent`age of`
      total" / "aver`age of` X" matched and the trailing noun became a fake
      date column -> broken `date_diff('year', CAST("total" AS DATE)) AS age`.
      Fixed in `core/onboarding/kpi/result_view_builder.py` + regression test.
      (The earlier "global result_format" hypothesis was WRONG — result_format is
      not read by the generator at all.)
> NOTE 2026-06-06 (RESOLVED): the interrupted Wave-2 attempt (#2 partial-completion
> + #4 parallel-planner) was salvaged from `git stash` (`stash@{0}`) and completed
> inline -- both are now DONE and green (see the [x] items below + changelog). The
> stash is fully salvaged and safe to drop (`git stash drop stash@{0}`).

- [x] **flow: partial completion — COMPLETE (Issue #2, FIXED 2026-06-06).**
      Phase 5 handled the all-undefined definition gate; now the deferred set is
      threaded through the feature-blocker panel (filters deferred KPIs' features +
      empty clusters via `_deferred_kpi_ids_from_registry`) and the source-to-target
      gate (undefined KPIs -> `deferred`, excluded from `blocked_kpi_count`), plus
      `compute_workflow_diff` records them as deferred gaps. A mix of defined +
      undefined KPIs now reaches generation and yields partial results. Guarded by
      `tests.test_partial_completion_deferred`.
- [x] **kpi_003 / kpi_010 definitions — detection gap closed (FIXED 2026-06-06).**
      The duration-threshold and within-N-days recurrence DETECTORS in
      `core/onboarding/features/derivation_patterns.py` did not fire for these
      phrasings ("readmit" not a substring of "readmission"; noun forms uncovered).
      Added comparator verbs + `_DURATION_DIFF_RE` (subtraction form) and a
      `_RECURRENCE_RE` word-stem regex + genericity guard. Per-workspace application
      is now just the normal `resolve-kpi-features` / `prepare-kpi-blocker-panel` run.
- [x] **derivation accuracy (measure rank) — FIXED 2026-06-05.** Measure
      selection now (a) tie-breaks by SPECIFICITY (a non-entity term like "net"
      outranks the entity word like "order"/"ticket") and (b) is grounded in the
      data-dictionary GLOSS, wired generically via `_load_column_glosses`. The
      engine no longer maps on column-name similarity alone (AGENTS.md Data Model
      rule). Verified: kpi_007 self-derives `avg(TOTAL_CLAIM_COST)`.
- [x] **wire parallel planner into the flow (Issue #4, FIXED 2026-06-06).**
      `pipeline_main` now calls `dispatch_parallel_completion` and records the
      parallel-vs-sequential decision (threshold-gated) recommending the
      `parallel_kpi_completion` route above the ready-KPI threshold. Module
      salvaged from the interrupted Wave-2a stash; wiring + green verified.
      REMAINING (smaller): the runner records the parallel recommendation but does
      not itself spawn concurrent workers -- the delegation layer performs fan-out.

## Known limitations (accepted, not bugs)

- Cross-table KPIs that need a join + a date choice (e.g. "unique patients
  admitted each quarter") cannot be derived correctly by single-table evidence;
  the derived grain may be wrong (kpi_008 -> `quarter(BIRTHDATE)`). These are
  meant to be caught at the kpi-analyst review gate. Improving them needs join
  evidence / dictionary signals, not more single-table heuristics.
