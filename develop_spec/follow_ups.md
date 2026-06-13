# develop_spec/follow_ups.md — open next items

Loose ends to pick up. Check an item off (move it to changelog.md when done with
a dated entry). Keep this honest — it is our shared backlog.

Item template: `- [ ] <area>: <what> — <why / where>`

## Open

- [x] **dashboard: LIVE Dash app built to the full bar (DONE 2026-06-10, Phase 2).** Rebuilt
      `build_dash_app` into a fit-to-viewport overview+drill app on the data-driven panels +
      DESIGN.md theme: KPI tiles -> drill, show/hide panel checklist, KPI dropdown, native
      legend-toggle/hover, nested-KPI grouping, lazy/server-side rendering. Browser-verified via
      dashboard-verify + agent-browser click/hover. See changelog 2026-06-10 (Phase 2a-2e).
- [x] **dashboard palette: categorical separation (DONE 2026-06-10, Phase 1c).** Multi-series now
      uses a colorblind-safe Okabe-Ito ramp; single-series keeps the editorial accent; the verify
      gate's delta-E check enforces separation. (Now token-driven via DESIGN.md.)

- [x] **dashboard chart fixes V1-V6 (DONE 2026-06-10, visually verified).** All six implemented in
      `core/dashboard/renderer.py` and confirmed by screenshotting the rendered board: V1 no duplicate
      titles, V2 line aggregates by date, V3 share is true 0-100% + top-N/Other cap (fixed the
      5000%/10000% double-scale), V4 ranked_bar ranks by non-constant categorical, V5 responsive +
      automargin (no clip), V6 clean top legend. See changelog 2026-06-10. Done at RENDER time
      (data-driven) rather than in inference, since inference only sees SQL column names not row
      values — more robust.
- [x] **dashboard D1 defect: ranked_bar ranks by the wrong dimension (FIXED 2026-06-10 via V4).**
      Resolved at render time by `_first_non_constant_categorical` (rank by the highest-cardinality
      non-constant column, ignoring filter-pinned constants). kpi_003 now shows the real top-10 payers
      (PAYOR8395..PAYOR4165). Guarded by the ranked_bar renderer tests.

- [x] **dashboard/wiki: new test modules now gate (DONE 2026-06-11).** Added the 7 dashboard/token/
      wiki test modules to `green_gate.CURATED_MODULES`. green-gate went 346 -> 420 tests; they now
      protect against regressions instead of only passing on demand.
- [ ] **wiki W3/W5 + dashboard D3/D4 (second wave — partial).** DONE: D1 (inference), D2 (theme),
      D5 (dashboard-engineer clarify/self-grill, Phase 3), W1 (no inlined SQL), W2 (lineage/links/
      decisions). REMAINING: dashboard D3 one-command viewable, D4 richer specs (filters/drill-down);
      wiki W3 durable business-context sections, W4 formatting polish, W5 wiki-as-brain (workspace
      home note + agents reading wiki as context, extending `core/onboarding/memory/wiki_memory.py`).
- [x] **cli: UnicodeEncodeError (cp1252) on non-ASCII output (FIXED 2026-06-11).** `workspace-flow`
      main() now reconfigures stdout/stderr to UTF-8 errors=replace, and the generated Gold-results
      comment uses ASCII `->` instead of `→`. No longer crashes mid-output on a piped cp1252 console.
- [~] **kpi_002 age via CURRENT_DATE — investigated 2026-06-11, NOT a bug.** Earlier flagged as a
      correctness bug; on inspection the as-of-event logic already exists (`_detect_event_date_column`
      + `as_of_expr`) and works (kpi_001 anchors age on ServiceDate). kpi_002's features are
      PatientID/Name/VisitType/Gender/DOB — NO date column at all, so there is no event date to anchor
      on; CURRENT_DATE is the only/defensible option for an age-snapshot and is recorded via the
      `_age_fallback` note for the kpi-analyst gate. Possible future improvement: broaden event-date
      detection to a lone date-typed FEATURE column (not just time-bucket cuts) for KPIs that have one.

- [x] **results presentation: agent paraphrased instead of forwarding; user had to ask
      (FIXED 2026-06-09).** Re-test after the subagent fix: pipeline completed but the
      auto-emitted packet inlined full bootstrap SQL for every KPI (~500 lines), the CLI
      truncated it, and the agent PARAPHRASED the tables ("consolidated key findings" —
      BUG-015-style) + made the user type "show me results". Root = oversized output ->
      truncation. Fixed by a COMPACT auto-surface (SQL linked, not inlined; 0 inline-SQL
      fences; compact packet -61% tokens/lines) at complete + review-gate, while explicit
      `workspace-flow results` stays full. Also added `results --kpi <id>` for single-KPI.
      Guarded by `EmitResultPacketTests` + `test_complete_compact_explicit_full_and_kpi_filter`.
      See changelog 2026-06-09. FOLLOW-ON (smaller): explicit `results` (full) and a future
      multi-KPI review brief still truncate — fine since `--kpi` + compact cover the common
      paths; revisit only if a full-packet truncation causes a real failure.

- [x] **token bloat: Gemini pins ~56k tokens of context on EVERY turn (FIXED 2026-06-08, measured 2026-06-09).**
      MEASURED via the new `token-report` tool: gemini fixed context 55,057 -> 16,336 tok
      (-38,721, -70%), grand total 62,378 -> 23,657 (-62.1%, bytes/4 heuristic, excludes the
      also-removed includeDirectoryTree runtime cost). Reproduce:
      `uv run token-report --workspace workspaces/<project> --label after --baseline <before.json>`.
      `.gemini/settings.json` `context.fileName` force-loads six files into context on every
      message: GEMINI.md (~3.4k) + AGENTS.md (~9.5k) + TOOLS.md (~16k) + `.agents/tools.json`
      (~23k, a 93KB machine registry) + workspace-workflow-prompt.md (~1.9k) +
      gemini-cli-reference.md (~1.85k) ~= **56k tokens fixed base**, plus `includeDirectoryTree:true`
      and 46 skill blurbs. Claude only auto-loads CLAUDE.md (~2.5k) by comparison. In the
      2026-06-08 Hospital-A run this fixed base was carried across ~40 tool calls and was the
      bigger silent tax (on top of the visible re-read loop — see the KPI-result-forward item).
      PLAN: drop `TOOLS.md` and `.agents/tools.json` from the pinned array (they are read-on-demand
      references per GEMINI.md:4 / AGENTS.md:468; `jitContext:true` already set but the explicit
      array overrides it), and set `includeDirectoryTree:false` (use `list-workspace-files`).
      Saves ~39k/turn (~70% of the base) with no capability loss — the registry stays readable.
      ROLLBACK BASELINE (restore this exact block to revert):
      ```json
      "context": {
        "fileName": [
          "GEMINI.md",
          "AGENTS.md",
          "TOOLS.md",
          ".agents/tools.json",
          ".gemini/workspace-workflow-prompt.md",
          "docs/agents/gemini-cli-reference.md"
        ],
        "includeDirectoryTree": true
      },
      ```
      VERIFY AFTER: a fresh Gemini session footer should report fewer context files / lower
      context%; the tool registry must still be reachable via ReadFile when an agent needs it.

- [x] **KPI result forward: CLI agent looped on UI truncation instead of presenting (FIXED 2026-06-08).**
      In the 2026-06-08 Hospital-A run the pipeline completed (`ready 3, blocked 0`) and wrote
      `interns/runs/2026-06-08/results.md` (+ per-KPI `kpi_00N.md`) and the `reports/kpi_results/current.md`
      alias, but Gemini read `current.md`, misread the `"... first N lines hidden ..."` UI banner as a
      read failure, and fell into the forbidden re-read loop (`-TotalCount 25/50/75/200/1000`, `-Raw`,
      `-Tail`, `Select-String ".*"`, `SearchText '^.*$'`) + read the 109KB `current.json` and 384KB
      `session.json` + a recursive dir dump — never forwarding the packet; the user cancelled. The
      grep tools also returned empty (ignore-filtered path) while raw Get-Content succeeded, manufacturing
      a contradiction that escalated the search. Doc rule already exists (GEMINI.md:180-192) and still
      failed. PLAN: make completion advertise the active run as a stable surface
      (`Active run -> interns/runs/<date>/results.md`) so any CLI anchors on the dated path and forwards
      ONCE; consider a structural guard so the wrapper emits the packet itself on completion rather than
      relying on the agent to fetch-and-verify a truncated read.
      RECURRED 2026-06-09 at the BLOCKER PANEL (not results): on a fresh re-test the long
      blocker panel's truncation banner was misread as an incomplete read and Gemini escalated
      by spawning a `generalist` subagent to re-read it; the subagent can't read git-ignored
      interns/ via ReadFile (subagents hard-respect .gitignore) and looped on policy-denied
      fallbacks until cancelled. Fixed: (1) `prepare-kpi-blocker-panel` next_step now carries
      the "render ONCE; truncation=success; do not re-read or delegate" guard (next_step is
      short / always visible); (2) `.gemini/settings.json` enableAgents=false removes the
      subagent escalation path (matches the Agent Delegation Rule). Guarded by
      `test_prepare_kpi_blocker_panel_next_step_carries_truncation_guard`. See changelog
      2026-06-09. NOTE: the generic root (truncation indistinguishable from failure) is now
      guarded on results AND the blocker panel; extend the same next_step guard to any other
      long governed panel (data-model / duplicate-review / pipeline-format) if they recur.
      RESOLVED 2026-06-08: `_print_cli_panel` now prints an `## Active Run` block at the
      visible tail of completion/`results` output naming the stable dated surface
      (`interns/runs/<date>/results.md` + per-KPI `kpi_*.md`) with explicit "read ONCE /
      `... first N lines hidden ...` means SUCCESS, do not re-read" guidance. New
      `_active_run_paths` helper resolves today's run (fallback: latest dated dir).
      Verified live on session wf_20260608T145532Z; guarded by
      `tests.test_workspace_flow.ActiveRunPathsTests` (3 tests). The structural-guard
      idea (wrapper emits packet itself) is deferred — the tail pointer is sufficient.

- [x] **cli: UnicodeEncodeError (cp1252) on non-ASCII output (FIXED 2026-06-11).** `workspace-flow`
      main() reconfigures stdout/stderr to UTF-8 (errors=replace) and the generated Gold-results
      comment uses ASCII `->`. No longer crashes mid-print on a piped cp1252 Windows console.

- [x] **gemini visibility: interns/ artifacts unreadable by native tools -- BUG-018
      actually fixed (2026-06-07).** Root cause: a gitignore DIRECTORY exclusion
      (`workspaces/**/interns/`) that child `!negations` can't undo, so the old
      `.geminiignore` re-includes were inert. Switched Gemini to respectGitIgnore=false
      + a self-contained `.geminiignore` denylist; git commit behavior unchanged. See
      changelog 2026-06-07. RESIDUAL: respectGitIgnore is now OFF globally for Gemini,
      so any NEW secret/PHI/heavy path must be added to `.geminiignore` explicitly --
      git's denylist no longer protects agent file visibility.
- [x] **intent-contract apply path mis-keys decisions to an empty kpi_id (FIXED 2026-06-08).**
      `record_intent_answer` for `grain_bucketing` (and `denominator_scope`) was
      observed writing `pipeline_decisions.json` under key `""` instead of the real
      kpi_id (e.g. `{"grain_bucketing_decisions": {"": "band_continuous_cuts"}}`),
      so the generator (which looks up `[kpi_id]`) never applied it. The direct CLI
      `apply-pipeline-decision --kpi-id ... --grain-bucketing ...` (added 2026-06-07)
      keys correctly and is the reliable path; the panel/intent-answer path still
      needs its kpi_id plumbed through `record_intent_answer`'s caller. Until fixed,
      the blocker-panel route to a grain answer remains unreliable even though the
      deadlock is gone.
      UPDATE 2026-06-08: also observed that when the harness blocks on grain, the blocker
      question panel surfaces NO options at all (the grain facet is a pipeline decision, not a
      feature blocker), so `apply-kpi-panel-answer`/`workspace-flow answer` error out and an
      agent loops (a Gemini run burned ~7% quota). Mitigated operator-side via a new GEMINI.md
      "Grain-Bucketing Blocker Rule" (route to `apply-pipeline-decision`).
      RESOLVED 2026-06-08: (1) `_load_registry_with_features` backfills positional
      `kpi_{idx:03d}` so questions/answers key correctly; (2) `BlockerQuestionPanelBuilder`
      promotes the hard-blocking `grain_bucketing` facet to `current` so
      apply-kpi-panel-answer resolves it. Verified live + green-gate (see changelog
      2026-06-08). The GEMINI.md deterministic-route guardrail still stands as a backstop.
- [x] **pre-existing: `tests.test_failure_contracts` 3 failures FIXED (2026-06-11).**
      Added the pipeline-SQL content contract to `WorkspaceArtifactValidator`
      (`_validate_pipeline_harnesses`): non-empty, and raw dataset-path reads only inside
      the BEGIN/END CATALOG BOOTSTRAP block. 14/14 green; module added to green-gate
      (434 tests). No false positive on legitimate pipeline SQL. See changelog 2026-06-11.

- [x] **generation: band_continuous_cuts emitted a NO-OP (FIXED 2026-06-07).** The
      grain block (2026-06-06) and the facet seam (line 22) only proved the decision
      *unblocked* generation — `grain_bucketing` was never consumed by the SQL, so
      `band_continuous_cuts` produced the SAME exact-value explosion as the block
      warned about (a Gemini session hand-edited kpi_002.sql and shipped ~7.4k
      0.2%-each rows). Now `band_continuous_cuts` emits `FLOOR(v/width)*width AS
      <cut>_band`; `exact_value_grain` keeps exact grain. `result_view_builder.py` +
      tests. See changelog 2026-06-07.
- [x] **presentation: band label now a readable range string (FIXED 2026-06-07).**
      `age_band` displays `20-29` via `CONCAT` while GROUP BY / ORDER BY / PARTITION BY
      use the numeric `CAST(FLOOR(v/width) AS BIGINT)*width` lower bound, so bands sort
      numerically (100-109 after 20-29, not lexically). `Dimension.display_expression`
      decouples SELECT from the group key. `result_view_builder.py` + tests
      (`test_band_groups_and_orders_by_numeric_not_label`).
- [x] **operating docs: auto-forward result packet now doc-enforced cross-CLI
      (FIXED 2026-06-07).** AGENTS.md "KPI Result Packet Forwarding Rule" strengthened
      to "present automatically on completion; needing to type 'show results' is a bug";
      added an equivalent "KPI Result Packet Forwarding Rule" section to GEMINI.md
      (previously had none). CLAUDE.md already carried the rule.

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
