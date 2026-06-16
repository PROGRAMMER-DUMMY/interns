# Handoff — Power BI dashboard via vendored MinusAnalyst

Branch: `feature/dashboard-powerbi-live` (pushed; PR not opened — `gh`/MCP auth was unavailable)
Status: working end-to-end on `workspaces/Healthcare-RCM-Data-Platform`. 61 dashboard tests green.
Last commit: `9c7f29c`.

## 1. What this is

The per-workspace dashboard is now the **real MinusAnalyst Power BI app** (vendored into
`vendor/minus/`), driven from the interns CLI for ANY workspace, on a **DQ-certified conformed
model** built from the medallion. Two sections: **KPIs** (scorecard of the workspace's defined
KPIs, from gold) and **Analysis** (silver-layer exploration with dense, multi-cut charts).

## 2. How to run / verify

```
# serve (loopback only; DQ-gated)
uv run workspace-dashboard --workspace workspaces/Healthcare-RCM-Data-Platform --live
#   -> http://127.0.0.1:8060  (use --port to change)

# regenerate config+data without serving (for a scheduler), DQ-gated:
uv run workspace-dashboard --workspace <ws> --refresh [--refresh-seconds 900]

# tests (uv run is hook-blocked for tests; use the venv interpreter):
.venv/Scripts/python.exe -m unittest tests.test_dashboard_model tests.test_dashboard_crossfilter \
  tests.test_dashboard_conformed tests.test_dashboard_minus_adapter tests.test_dashboard_live_cli
# full repo gate:
green-gate
```
Generated MinusAnalyst project + parquet land under `workspaces/<ws>/interns/state/minus/`
(gitignored, regenerated each run/refresh).

## 3. Architecture / key files

- `core/dashboard/model/` — the live model (reusable, no UI deps):
  - `layers.py` read gold/silver/bronze Delta; `cuts.py` per-KPI model (measure/cuts/label/kind);
    `aggregate.py` additive vs non-additive re-aggregation (+ type-tolerant filters);
    `conformed.py` builds ONE clean star from bronze (dedup, type-norm, age-from-DOB before PII
    drop, approved-contract joins, PII removed; derives `month`, `ar_days`);
    `dq.py` certifier (null/fan-out/RI/lossless/gold-reconciliation); `parity.py` gold parity;
    `crossfilter.py` canvas/cross-filter helpers (used by tests).
- `core/dashboard/minus_adapter.py` — turns a workspace into a MinusAnalyst project: measures
  (Total Paid, Total Amount, Record Count, Collection Rate, Days in A/R), KPIs scorecard page,
  Analysis page (combo / small-multiples / stacked / grouped / donut / detail tables), DQ-gated
  `generate()`. **This is where dashboard composition lives.**
- `tools/workspace_dashboard.py` — `--live` / `--refresh` / `--refresh-seconds` (launches the
  vendored app via `minus.render.app.create_app`); loopback/token guard retained.
- `vendor/minus/` — vendored MinusAnalyst. Local changes vs upstream:
  - `config/models.py`: Widget `tab`, `subtitle`; Measure `target`, `goal`; new widget types
    `stacked_bar`, `combo`, `small_multiples`.
  - `render/widgets/render.py`: KPI card target-color + subtitle; combo/stacked/small_multiples.
  - `render/layout.py` + `render/callbacks.py`: `dcc.Tabs` grouping + active-tab persistence.
  - `data/{connectors,model,duckdb_exec}.py`: `scan_source()` -> pushdown reads parquet IN PLACE.
  - `agent/watcher.py`: watchdog made optional. `themes/base.css`: tab + kpi-sub styles.
- Tests: `tests/test_dashboard_{model,conformed,crossfilter,minus_adapter,live_cli}.py`.
- Plan: `.claude/plans/have-you-created-the-scalable-firefly.md`; research notes in chat history.

## 4. What remains

### High value
- [ ] **Open the PR** — branch is pushed; `gh` not installed + GitHub MCP token invalid.
      Link: https://github.com/PROGRAMMER-DUMMY/interns/pull/new/feature/dashboard-powerbi-live
- [ ] **Production hardening (deferred by request):**
      - Result **caching** (Flask-Caching / functools) — fixes the click-render lag (MinusAnalyst
        re-renders the whole page server-side per interaction).
      - **Row-level security / role-based views** (HIPAA); only a loopback-token guard exists today.
      - **Mobile layout**, **data-freshness badge**.
      - Serve behind a **production WSGI** (gunicorn) instead of the Flask dev server.
- [ ] **Incremental medallion upstream** — the KPI pipeline still does FULL recompute. For TB scale
      it must become bronze-append + silver-MERGE (the `core/medallion/merge_emitter.py` design) +
      partition-scoped gold; `--refresh` already consumes whatever gold exists.

### Medium
- [ ] **KPI exec polish:** auto **insight line** per card ("↑4% vs last quarter, above target");
      **targets** for the paid KPIs (need business input); trend arrows only show where gold has a
      date (kpi_001). Optional **sparkline** per card.
- [ ] **More RCM KPIs** if the source carries claim-status/denial data: Denial Rate (<5%),
      Clean Claim Rate (>95%), First-Pass Resolution. Only Collection Rate (>=96%) + Days-in-A/R
      (<30) are derived today.
- [ ] **Richer Analysis charts:** scatter with size+color (3-4 dims), treemap; combo currently
      assumes a date axis.

### Lower / Stage 4 (TB scale — design only, needs infra)
- [ ] Distributed/warehouse **pushdown connector** (Databricks SQL / Trino / ClickHouse / StarRocks);
      only the DuckDB-over-parquet file-scan slice is built.
- [ ] **Liquid clustering** on filter/join keys; **materialized views / per-KPI rollup cube** for
      sub-second cross-filter at TB.
- [ ] **Cube.dev semantic layer** (governed metrics + MCP + pre-aggregations) — converges the
      precompute/MCP/governed-KPI goals.

## 5. Known issues / caveats

- **Days-in-A/R is unrealistic on the synthetic data** (paid often same-day; negatives now nulled).
  The metric logic is correct; it needs real data to be meaningful.
- **Click target:** users must click the bar/slice itself, not its label/empty space (Plotly).
- **Vendored MinusAnalyst** is a fork copy — upstream changes won't flow in automatically; local
  edits are listed in §3.
- The original `core/dashboard/` renderer (legacy `--export`/non-live path) is untouched and still
  present; the retired interns UI reimplementation (`core/dashboard/ui/*`) was removed.

## 6. Open decisions for the next owner
- Vendored-fork vs git-submodule for MinusAnalyst (currently vendored).
- Whether to keep `Days in A/R` given the data, or gate KPIs on a data-realism check.
- Caching strategy + auth model before any non-loopback / multi-user deployment.
