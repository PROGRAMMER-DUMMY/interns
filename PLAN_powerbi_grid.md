# Plan — Power-BI-style Slice-and-Dice Grid (governed SSRM on DuckDB)

Goal: a production-grade, Power-BI-like data grid inside the live dashboard —
infinite scroll over **any** dataset size/shape, server-side filter/sort/group/pivot,
dynamic columns — built on the DuckDB engine we already run. License-free
(community AG Grid + our own DuckDB `GROUP BY` pivot). Migrates into the existing
`core/dashboard` app; KPI panels + Explore stay as-is.

Branch: `feature/dashboard-explorer-drilldown` (continue) or a new
`feature/powerbi-grid`. Commit incrementally.

## Non-negotiable invariants (every phase)
- **No SQL injection.** Column identifiers → `core.sql_safety.assert_safe_identifier`
  + `quote_ident_sql`. Filter VALUES → **bound DuckDB parameters** (`?`), never
  f-string. Sort dirs + operators → fixed allow-lists. (The reference snippet's
  f-string SQL is the anti-pattern — do not copy it.)
- **Governance.** Columns the workspace marks sensitive (`data_policy` +
  `pii_redaction.workspace_redaction_patterns`) are DROPPED from the projection
  AND the column defs, and cannot be filtered/sorted/grouped. The grid is a
  rendered surface — same redaction as the Data table. Age Safe-Harbor honored.
- **Scale.** Server-Side Row Model: the grid requests only the visible block;
  DuckDB does filter/sort/group/limit; raw data never ships whole to the browser.
- **Workspace-agnostic.** Columns/types inspected dynamically from DuckDB schema;
  no hardcoded columns/domains.
- **Presentation-only.** No change to generated SQL/engines/KPI results.
- Tests via `./.venv/Scripts/python.exe -m pytest …` (NOT `uv run`). Verify the
  live app by screenshotting `http://127.0.0.1:8060/` with
  `core.dashboard.screener._screenshot(url, png)` (headless Edge,
  `--virtual-time-budget=30000`).

## Phases
### A — Governed grid backend (foundation, security-critical)  [land first]
- Add `dash-ag-grid` to deps (dashboard extra).
- New `core/dashboard/grid_backend.py`:
  - `grid_sources(layout)` → selectable sources: each raw dataset (CSV/Delta) +
    each KPI result view.
  - `generate_column_defs(con, source, workspace)` → AG Grid coldefs from the
    DuckDB schema; dtype→filter (`agTextColumnFilter`/`agNumberColumnFilter`/
    `agDateColumnFilter`); EXCLUDE redacted columns.
  - `build_rows_query(source, request, allowed_cols)` → parameterized
    (sql, params, count_sql, count_params) for filter + sort + LIMIT/OFFSET.
  - `serve_rows(con, source, request, workspace)` → `{rowData, rowCount}`, with
    redacted values scrubbed defensively even if a column slips through.
- Tests `tests/test_dashboard_grid_backend.py`: injection attempt is neutralized
  (value bound, not executed); redacted column absent from coldefs + rows + cannot
  be filtered/sorted; pagination/sort/filter correctness; unknown column rejected.

### B — Wire the grid into the live app
- A "Data" surface in `renderer.build_dash_app`: a source dropdown (raw datasets +
  KPI results) + a `dag.AgGrid` with `rowModelType="infinite"`, `getRowsRequest`→
  `getRowsResponse` callback calling `serve_rows`. Replace/augment the 200-row
  `dash_table.DataTable`.
- Static export: keep the existing capped Data table (SSRM needs a server).

### C — Pivot / row-grouping (license-free, DuckDB GROUP BY)
- Extend `build_rows_query` for AG Grid `rowGroupCols` + `valueCols` + `groupKeys`
  → server-side `GROUP BY` with agg; expand one level per `groupKeys`. A small
  pivot control (row dims, measure, agg). Redacted cols never groupable.

### D — (optional) derivative transforms in Explore
- A "Transform" dropdown on Explore: period-over-period %, running total (the two
  universally useful ones). Only if cheap; not required for the grid.

### Done
- Full dashboard suite green; screener 0 findings; live app screenshot shows the
  grid filtering/sorting/paginating with no PII columns. PR when asked.
