# PLAN — Power BI-grade dashboard upgrade (silver + gold + MinusAnalyst UI)

Status: proposed
Branch: feature/dashboard-explorer-drilldown (or a fresh branch off it)
Owner: dashboard
Last updated: 2026-06-15

## 1. Goal

Turn the per-workspace dashboard from a "shows the KPI answers" report into a live,
Power BI-grade BI tool, WITHOUT changing the KPI pipeline that produces the numbers.

Keep what is already good (interns chart recommendation: `core/dashboard/profile.py`
+ `chart_knowledge.py`). Replace the weak parts (re-running SQL to render, frozen
per-KPI grid) with:

- a fast, validated data source (read the **gold** Delta layer, do not recompute),
- a live re-aggregation engine over **silver/gold** (cross-filter, roll-up, drill, slice
  at any grain),
- the MinusAnalyst look-and-feel + interactivity (warm/dark theme, 12-col grid, KPI
  cards with period-over-period deltas, conditional-format tables, slicers, CSV export),
  ported in from `PycharmProjects/MinusAnalyst`.

## 2. Non-goals

- No change to the KPI pipeline, generated SQL, registry, relationship contracts, or
  medallion build. The dashboard is a pure consumer of existing artifacts.
- No live recompute from **bronze/raw**. Exploration is bound to each KPI's cuts.
- No new analysis the KPI did not define. Cuts are the explorable surface.

## 3. Before vs after

| Dimension | Before | After |
| --- | --- | --- |
| Result source | re-runs solution SQL 3-4x/render from bronze | reads gold Delta once (validated) |
| Startup | O(KPIs x re-execs), serial, no timeout | read parquet, in-memory aggregate |
| Interactivity | first-cut cross-filter, frozen at SQL grain, per-KPI | global cross-filter, roll-up/slice/drill at any grain |
| Cross-KPI linkage | none | shared-dim cross-filter across the canvas |
| Look & feel | functional Dash, CDN-only Plotly | MinusAnalyst theme + grid |
| Chart recommendation | good | unchanged (kept) |
| Governance/auth | redaction + loopback token | must be re-ported into richer UI |

## 4. Data contract (the spine)

The medallion layers already materialized per workspace:

```
medallion/silver/<kpi>_features   row grain, joined + derived, PRE-filter, PRE-aggregation
medallion/gold/<kpi>_results      KPI filter + GROUP BY cuts applied = the validated answer
```

Cuts are already defined per KPI (registry/spec); gold columns = cuts + measure.

```
DEFAULT panels         -> read gold  <kpi>_results        (validated, fast)
roll-up / slice /      -> re-aggregate gold               (ADDITIVE measures)
  cross-filter            re-aggregate silver             (NON-ADDITIVE measures)
drill below cut-grain  -> read silver <kpi>_features
cuts                   -> from registry/spec (the slicer dims; not inferred)
bronze / raw           -> NOT used
```

Reconciliation guarantee: gold == silver filtered by the KPI predicate, grouped by the
full cut set. Therefore rolling gold up (additive) or recomputing from silver
(non-additive) can never contradict the validated headline — it is the same population.

Additive vs non-additive is read from the spec flag interns already writes
(`y_format: percent` / share detection). Non-additive measures must NEVER be summed
across a slice; they recompute from silver counts.

## 5. Architecture

New subsystem under `core/dashboard/` (beside the current renderer, not replacing it
until parity is proven):

```
core/dashboard/model/
  layers.py        read gold/silver Delta tables for a workspace (deltalake/duckdb)
  cuts.py          load each KPI's cuts + measure + additive flag from registry/spec
  aggregate.py     re-aggregation engine: (rows, group_by, filters, measure, additive)
                   -> frame; additive -> sum gold; non-additive -> recompute from silver
  crossfilter.py   apply a global dimension filter to every KPI that has that column
  parity.py        assert engine(gold/silver, kpi cuts, kpi filter) == gold; build gate
core/dashboard/ui/        (ported + adapted from MinusAnalyst render/ + themes/)
  app.py           Dash app factory (Power BI canvas: slicers + grid + cross-filter)
  layout.py        12-col grid layout from interns panels[]
  widgets.py       KPI card (w/ delta), bar/line/donut/table/etc. renderers
  theme/           claude.css (light) + dark.css ported from MinusAnalyst
  callbacks.py     slicer/cross-filter/drill callbacks against the model engine
```

Reused unchanged:
- interns `profile.decide_panels` / `chart_knowledge` -> chart selection (the brain).
- interns `pii_redaction` + `governance/data_policy` -> redaction set.
- the spec contract (`spec.py`) -> panel layout + additive flag.

Ported from MinusAnalyst (adapted to the gold/silver source, not its YAML model):
- `query/measures.py` semantics (agg + derived), `render/widgets`, `render/interactions`
  (cross-highlight), `render/theme`, conditional-format table, CSV export.

Engine: Polars for in-memory re-aggregation, DuckDB only to read Delta (already deps).

## 6. Phases

### Phase 0 — Decision + skeleton (0.5d)
- Confirm: build beside the current dashboard (new entrypoint), cut over only after
  parity passes. Pick branch.
- Create `core/dashboard/model/` package skeleton + tests scaffold.

### Phase 1 — Data + re-aggregation core (RISK FIRST) (2d)
- `layers.py`: read `medallion/gold/<kpi>_results` and `medallion/silver/<kpi>_features`
  as Polars frames. No SQL re-run.
- `cuts.py`: per-KPI cuts, measure, additive flag from spec/registry.
- `aggregate.py`: the additive (sum gold) / non-additive (recompute silver) split.
- `parity.py`: per-KPI parity vs gold; wire as a build gate.
- Tests: parity for all 3 sample KPIs (incl. the share KPI_002), additive roll-up
  correctness, non-additive recompute correctness.

### Phase 2 — Cross-filter + drill engine (2d)
- `crossfilter.py`: a global filter applied to every KPI frame exposing that column.
- Drill: change group_by to any subset/superset of cuts; below-cut-grain reads silver.
- Tests: cross-filter consistency (sum of filtered == filtered sum), drill grain changes.

### Phase 3 — UI port (3d)
- Port MinusAnalyst `render/` + `themes/` into `core/dashboard/ui/`, sourced from the
  model engine instead of its YAML semantic model.
- Map interns panel chart_types -> MinusAnalyst widgets; add missing renderers
  (treemap, lollipop, histogram, bubble_map) or nearest fallback.
- KPI cards with period-over-period deltas; conditional-format tables; CSV export.

### Phase 4 — Governance + auth parity (1.5d) [SHIP GATE]
- Carry interns redaction set into slicers/table/CSV: a redacted column can never be a
  slicer field, table column, or CSV export column.
- Port the loopback-token guard from `tools/workspace_dashboard.py` (refuse non-loopback
  bind without `AUTORESEARCH_DASHBOARD_TOKEN`).
- Tests: redacted column absent from every surface; non-loopback refused without token.

### Phase 5 — Wire-in + cutover (1d)
- New CLI flag / entrypoint (e.g. `workspace-dashboard --live`) that builds the new app.
- Auto-run hook: `workspace-flow complete` can target the new app once parity is green.
- Cut over default; keep the old renderer behind a flag for one release.

## 7. Risk register

| Risk | Mitigation |
| --- | --- |
| Non-additive roll-up wrong (shares/avg) | additive flag from spec; non-additive recompute from silver; parity test on KPI_002 |
| Re-derivation drift (age etc.) | drill on cuts uses gold's already-derived columns; below-grain reads silver as-is; no re-derivation in the additive path |
| Redaction leak via richer UI | Phase 4 ship gate: redacted cols excluded from slicer/table/CSV |
| No auth in ported UI | port loopback-token guard before any non-loopback bind |
| Cross-KPI dims not truly conformed | cross-filter only applies to KPIs that actually expose that column name |
| Scope creep into bronze/live model | hard non-goal; engine reads silver/gold only |

## 8. Acceptance criteria

- [ ] All KPI parity tests pass: live engine reproduces gold at the KPI's cuts+filter.
- [ ] Additive roll-up and non-additive recompute both verified correct.
- [ ] Global cross-filter refilters the whole canvas; numbers stay internally consistent.
- [ ] Drill to any grain (incl. below-cut via silver) works.
- [ ] No redacted column appears as a slicer field, table column, or CSV export.
- [ ] Non-loopback bind refused without token.
- [ ] First paint reads gold (no SQL re-run); measurably faster than current startup.
- [ ] Chart selection identical to current (interns recommendation untouched).
- [ ] Old renderer still available behind a flag until cutover.

## 9. Estimate

~10 working days. Critical path: Phase 1 (parity/additivity) and Phase 4 (governance/auth)
are the must-land risks; Phase 3 (UI) is the most visible but lowest-risk.
