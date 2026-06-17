# Dashboard TB-Scale Plan

- **Date:** 2026-06-17
- **Author:** Claude (pair session)
- **Branch:** `feature/dashboard-powerbi-live`
- **Status:** PROPOSED — not committed, not pushed, not merged to `main`.

## Strategy (one line)
Stop holding data in RAM: make the dashboard **build stream to a partitioned table**
and make **every serve query push down to an engine**. The KPI-generation layer is
already TB-shaped — leave it alone. All work is in the dashboard build + serve path
and the physical data layout.

## Why (what breaks today at TB)
- `core/dashboard/model/conformed.py: build_conformed_model` materializes the whole
  joined star **in memory** (Polars join/dedup) and writes one `conformed.parquet`
  → **OOM at build** before serving ever starts. This is the primary blocker.
- The live engine (`vendor/minus/query/engine.py`) already has a **DuckDB pushdown
  seam** (`data/duckdb_exec.py` scans parquet in place), but falls back to **in-memory
  Polars** when `supported()` is False (ratios/derived/window measures,
  period-over-period delta) → per-click full-frame scans at TB.
- No **partitioning/clustering**, no **pre-aggregation** to dashboard grains, and the
  dashboard's ratio/window measures don't push to SQL.

## Hard constraint
The DQ parity gate (`core/dashboard/model/dq.py`: `no_fanout`, `lossless`) requires the
conformed model to faithfully reproduce bronze totals. **Never drop rows** to "fix"
performance — recompute the gate as SQL aggregates over the written dataset instead.

## Plan (priority order)

### Phase 0 — Baseline (read-only, do first)
Profile a scaled-up synthetic workspace: build RAM, per-click latency, and which widgets
hit the in-memory fallback. Set SLOs: build = bounded RAM; click < ~1.5 s. Confirms the
failure points empirically. Low effort, no risk.

### Phase 1 — Engine-built, partitioned conformed table  (biggest win)
- Replace the in-memory Polars join/dedup in `build_conformed_model` with a **DuckDB (or
  Spark) job over the bronze parquet/Delta**: join on approved edges, dedup, derive
  `month`, write conformed as a **partitioned dataset** (by `month`/date) — never the
  whole star in memory.
- Keep `no_fanout`/`lossless` but compute them as **SQL aggregates** over the written
  dataset, not Polars over a resident frame.
- Effect: kills the build-time OOM.

### Phase 2 — Full pushdown on serve
- Widen `duckdb_exec.supported()` + the SQL builder so **every generated widget pushes
  down** (top-N, stacked totals, ratio/derived measures, period-over-period delta) → zero
  in-memory aggregation at serve.
- Point `scan_source` at the partitioned glob so time filters **prune partitions**.
- Move importance scoring (`core/dashboard/importance.py`) and incomplete-period
  detection to SQL `GROUP BY`s.
- Effect: kills the per-click in-memory scan.

### Phase 3 — Pre-aggregated serving layer  (makes clicks instant)
- At generate time, build **rollup/cube tables at the dashboard's known grains** (the
  importance-chosen cuts). Live widgets hit a small cube, not the TB fact. Refresh
  incrementally (new partitions only).

### Phase 4 — Scale-out backend  (true TB+ / concurrency)
- Past DuckDB's single-box ceiling, point the dashboard at the **Databricks/SQL backend
  already in the repo**: conformed as Delta in Unity Catalog, **liquid clustering /
  Z-order** on filter+join keys, Photon, warehouse result cache. Use the existing
  **PySpark generator (broadcast + AQE)** for heavy KPI computes.

### Phase 5 — Physical / ops hygiene  (cross-cutting)
Partition/cluster on common filter+join keys; `approx_count_distinct` for distinct at TB;
incremental MERGE for new data; keep/extend the engine result cache + cube refresh.

## Decision point
- **DuckDB single-box:** good to ~hundreds of GB. Phases 1–3 get there with **zero new
  infra**.
- **Databricks (Phase 4):** only when past that ceiling or needing concurrency. Don't pay
  earlier.

## Recommended sequence
1. Phase 0 profiling (read-only) — confirm the numbers.
2. Phase 1 + Phase 2 — remove the OOM and keep clicks fast on DuckDB at large scale.
3. Phase 3 cubes if clicks still aren't instant.
4. Phase 4 only past the DuckDB ceiling.

## What stays untouched
The KPI generation engine — layered `features → results` views, CTEs, window functions
(partitioned share, ranking, running/moving with frames), broadcast joins + AQE,
tri-engine SQL/Polars/PySpark parity. It already follows the TB-scale patterns; changing
it adds risk for no gain.

## Related local work from this session (also uncommitted)
Generic, stakeholder-facing dashboard improvements (not the TB work, but in the same
working tree):
- `core/dashboard/importance.py` (new) — η²/concentration dimension importance; drives
  cut/measure selection (removed RCM-substring favoritism).
- Data labels on every bar/segment/slice + stacked-bar totals (`render.py`).
- Incomplete trailing-period flag on trends (mark-only, lossless) + high-cardinality
  bars → horizontal top-N + atomic spec write (`minus_adapter.py`, `conformed.py`,
  `vendor/minus/config/models.py`).
