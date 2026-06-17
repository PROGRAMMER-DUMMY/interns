# Phase 3 — Scale-out design (TB-scale dashboard + KPI compute)

**Status: DESIGN ONLY.** Everything here needs infrastructure (a distributed
warehouse / object store / cluster) that does not exist in the local dev box, so
nothing in this file is built. It is the considered plan for when the
single-node DuckDB slice stops being enough. Phases 0–2 (caching, connection
reuse, dbt spike) are done; this is the part that is deliberately deferred until
data volume justifies the operational cost.

## When to trigger (don't build early)
Single-node DuckDB-over-parquet is genuinely fine to surprisingly large sizes.
Only start Phase 3 when one of these is true on real (not synthetic) data:
- A conformed star that no longer fits comfortably in box RAM (~tens of GB+),
  OR cold cross-filter queries that exceed ~2–3s even with the result cache warm.
- Concurrent users beyond a single analyst (the loopback assumption breaks).
- A medallion rebuild that can't finish in the batch window.

Until then, the Phase 1 result cache + the Phase 2 dbt incremental marts are the
right answer and Phase 3 is over-engineering.

## The three pieces (in dependency order)

### 3a. Distributed pushdown connector
Today only `duckdb_exec.run_pushdown` exists (DuckDB over local parquet). The
live model's query path is already engine-shaped: `scan_source()` returns a
SQL-scannable source and the engine builds join+group-by+filter SQL. To scale,
add a **sibling executor** behind the same `supported()` / `run_pushdown()`
contract that targets a distributed SQL engine instead of a local DuckDB
connection.

- **Candidates:** Databricks SQL (the platform already has a Databricks backend
  + scopes config), Trino, ClickHouse, or StarRocks. Databricks is the path of
  least resistance given existing integration.
- **Shape:** `core/dashboard/model/` gains `warehouse_exec.py` implementing the
  same signature as `duckdb_exec.run_pushdown`; the engine picks the executor by
  config (`dashboard.execution_backend`). The result cache (Phase 1) sits
  *above* this unchanged — it caches results regardless of executor.
- **Governance:** the same approved-edge join graph and PII-drop apply; the SQL
  is generated identically, only the connection/dialect changes.
- **Reuses:** the `[execution_backend] Databricks configured; remote execution
  requires explicit approval` gate already in the repo — remote execution must
  stay behind `AUTORESEARCH_ALLOW_REMOTE_EXECUTION` + human approval.

### 3b. Liquid clustering + materialized rollup cube
Cross-filter at TB scale is too slow to compute from base rows per interaction,
even pushed down. Two complementary precompute layers:
- **Clustering on filter/join keys** (liquid clustering on Delta, or
  `ZORDER`/partitioning) so filter predicates prune files. Keys = the dashboard's
  slicer fields + join keys (LineOfBusiness, PayorID, DeptID, month).
- **Per-KPI rollup cube**: a `marts`-adjacent dbt model that pre-aggregates each
  measure across the *combinations of dashboard dimensions* (a small cube), so a
  cross-filter is a sub-second lookup against the rollup instead of a scan of the
  fact. This is a natural dbt incremental model — it slots directly onto the
  Phase 2 output. Cardinality is bounded by dimension count, not row count.

### 3c. Semantic layer (Cube.dev) — optional convergence
A governed-metrics semantic layer (Cube.dev) would converge three goals the
platform already half-has: governed metric definitions, an MCP surface, and
pre-aggregations.
- **Why it fits:** the KPI registry already *is* a metric catalog; Cube's
  `cubes`/`measures`/`dimensions` map onto it. Cube's pre-aggregations are 3b's
  rollup cube, managed.
- **Why it's last / optional:** it's a whole new service to run and secure, and
  it overlaps with what dbt marts + a rollup model already give us. Only adopt if
  multi-tool metric consistency (BI + API + LLM all agreeing on a number) becomes
  a hard requirement. Otherwise 3a+3b suffice.

## How it composes with Phases 1–2
```
[ Phase 1 result cache ]  unchanged, sits above every executor
          |
[ engine.run ] --supported()--> [ DuckDB exec ]  (small/local)   <- today
                                \ [ Warehouse exec (3a) ]  (TB)   <- Phase 3
                                       reads
[ Phase 2 dbt marts ] --(incremental)--> [ rollup cube (3b) ] --(optional)--> [ Cube.dev (3c) ]
```
- Phase 1 caching is backend-agnostic — it already pays off at any scale.
- Phase 2 dbt marts are where 3b's rollup cube is authored (one more model).
- 3a is the only piece that touches the live query engine, and it's additive
  (a sibling executor behind the existing contract), not a rewrite.

## Explicitly NOT doing now, and why
- No distributed engine stood up — no TB data and no multi-user need yet.
- No rollup cube — the result cache already makes repeat cross-filters instant
  at current scale; a cube is wasted maintenance until base scans get slow.
- No Cube.dev service — overlaps with dbt marts; not worth the operational cost
  until metric consistency across BI+API+LLM is a stated requirement.

## Open decisions for the next owner
- Which distributed engine (Databricks SQL is the incumbent-advantage choice).
- Whether the rollup cube lives in dbt (recommended — reuses Phase 2) or in the
  warehouse as a materialized view.
- Cube.dev: adopt vs. skip — decide only when a second metric consumer appears.
