# develop_spec/follow_ups.md — open next items

Loose ends to pick up. Check an item off (move it to changelog.md when done with
a dated entry). Keep this honest — it is our shared backlog.

Item template: `- [ ] <area>: <what> — <why / where>`

## Open

- [x] **generation: phantom age dimension (FIXED 2026-06-05).** `_AGE_PATTERN`'s
      `age of/from <col>` alternative lacked a leading `\b`, so "percent`age of`
      total" / "aver`age of` X" matched and the trailing noun became a fake
      date column -> broken `date_diff('year', CAST("total" AS DATE)) AS age`.
      Fixed in `core/onboarding/kpi/result_view_builder.py` + regression test.
      (The earlier "global result_format" hypothesis was WRONG — result_format is
      not read by the generator at all.)
- [~] **flow: partial completion (PARTIAL 2026-06-05).** Definition gate now
      blocks only when EVERY KPI is undefined; generation loop skips deferred
      undefined KPIs. REMAINING: thread the `deferred_kpis` set through the
      feature-blocker panel and source-to-target gate so a MIX of defined+
      undefined reaches generation and produces partial result tables (today a
      mix still stops at the feature-blocker stage asking about the undefined
      ones). Multi-gate change; deferred to avoid rushing the cascade.
- [ ] **kpi_003 / kpi_010 definitions:** kpi_003 needs a derived duration
      column (`STOP-START > 24h`); kpi_010 needs a readmission self-join (within
      30d, like kpi_009). Deferred from the apply-kpi-definition pass.
- [x] **derivation accuracy (measure rank) — FIXED 2026-06-05.** Measure
      selection now (a) tie-breaks by SPECIFICITY (a non-entity term like "net"
      outranks the entity word like "order"/"ticket") and (b) is grounded in the
      data-dictionary GLOSS, wired generically via `_load_column_glosses`. The
      engine no longer maps on column-name similarity alone (AGENTS.md Data Model
      rule). Verified: kpi_007 self-derives `avg(TOTAL_CLAIM_COST)`.
- [ ] **wire parallel planner into the flow:** call `plan-kpi-completion` from
      `run-kpi-pipeline` and auto-fan-out above a KPI-count threshold, instead of
      only emitting the plan artifact. Coordinate with `delegation.STAGE_ROUTING`
      (`parallel_kpi_completion`).

## Known limitations (accepted, not bugs)

- Cross-table KPIs that need a join + a date choice (e.g. "unique patients
  admitted each quarter") cannot be derived correctly by single-table evidence;
  the derived grain may be wrong (kpi_008 -> `quarter(BIRTHDATE)`). These are
  meant to be caught at the kpi-analyst review gate. Improving them needs join
  evidence / dictionary signals, not more single-table heuristics.
