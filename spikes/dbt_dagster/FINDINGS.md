# Phase 2 spike findings — dbt Core + Dagster for the transform/orchestration layer

**Question:** can the bespoke Python medallion/transform layer be expressed as a
standard **dbt Core** project (over DuckDB), orchestrated by **Dagster**, without
losing the platform's governance — and does it reproduce the validated gold KPIs?

**Scope:** one workspace (`Healthcare-RCM-Data-Platform`), 3 KPIs, bronze→silver
(staging+intermediate)→gold (marts). Built as real dbt artifacts; validated
offline against the existing gold Delta tables via `validate_spike.py`.

## Verdict: VIABLE — recommend adopting dbt Core for the transform layer, behind a feature flag, one workspace at a time. Dagster is the natural orchestrator but is the heavier lift; stage it second.

## What was proven (run `validate_spike.py`)
| Check | Result |
|---|---|
| KPI-001 mart reconciles to gold | ✅ total `474220.16`, `1169` rows — exact |
| KPI-003 mart reconciles to gold | ✅ total `16202.74`, `10` rows — exact |
| No PII column reaches any mart | ✅ governance gate holds |
| KPI-002 single-attribution shares sum to 100% | ✅ `100.0` |

So a standard staging→intermediate→marts dbt project reproduces the platform's
gold **exactly**, and the governance rules survive as ordinary dbt constructs.

## How each governance rule translated to dbt
| Platform rule (today, in Python) | dbt expression |
|---|---|
| `pii_redaction` drops PII before joins | PII columns simply **not selected** in `stg_*` models; a singular test (`assert_no_pii_in_marts.sql`) fails if any leak |
| Approved-edge joins only (`relationship_contracts.json`) | `int_claims_enriched` joins only the two `allowed_in_sql_generation=true` edges; `relationships` schema test enforces FK integrity |
| `share_attribution = single` (sums to 100%) | `ROW_NUMBER()` attribution CTE in `fct_kpi_002`, same deterministic order as the generated SQL |
| `dq.py` gold-reconciliation | `assert_kpi_003_reconciles_gold.sql` singular test compares mart total/rows to gold |
| `age` derived from DOB before redaction | `date_diff('year', dob, service_date)` in the mart; DOB kept in staging only as a raw date input |

## What dbt + Dagster buy us (the two real gaps)
- **Incremental** (`+materialized: incremental`) → closes the full-recompute gap
  (handoff §4). Marts recompute only changed partitions instead of the whole
  history every run.
- **Dagster runtime** → the dbt lineage graph becomes a scheduled, retryable
  asset graph with run history. Closes the "static execution order, no
  scheduler/concurrency" gap from the production-rate review.
- Plus: free DAG/lineage, `unique`/`not_null`/`relationships` tests as a CI
  gate, Slim CI via `state:modified+`, env/secret isolation.

## What it does NOT replace (keep as-is, on top)
dbt is SQL-over-warehouse only. These platform capabilities stay in Python and
must sit **around** dbt, not inside it:
- KPI comprehension gates, agent/skill routing, human-gate provenance.
- The interview/blocker/feature-resolution flow that *authors* the SQL.
- The live dashboard (already on DuckDB; would just point at dbt's marts).

So this is **not** a rip-and-replace of `core/medallion/` + `conformed.py`. It's:
"let the platform keep deciding *what* SQL to emit and keep its governance gates;
let dbt own *running* that SQL incrementally, and Dagster own *scheduling* it."

## Cost / risk to adopt for real
- New deps: `dbt-core`, `dbt-duckdb`, later `dagster`, `dagster-dbt`. None added
  to the repo yet (spike is dep-free — validated with the existing DuckDB).
- The platform's SQL generator would emit dbt models (with `{{ ref }}`/`{{ source }}`)
  instead of standalone `solutions/*.sql`. Medium effort, mechanical.
- Delta sources via the dbt-duckdb delta plugin need a one-time setup.
- Two engines to reason about until migration completes (bespoke + dbt).

## Recommended next steps (if we proceed)
1. Add `dbt-core` + `dbt-duckdb` to a `spike`/optional dependency group.
2. Teach the SQL generator a `--emit dbt` mode that writes models instead of
   `solutions/*.sql`, reusing the exact logic proven here.
3. Convert marts to `incremental` keyed on `service_date`/insert date.
4. Only then add Dagster (`dagster-dbt`) for scheduling — the bigger lift.

## To run the artifacts
- Offline proof (no deps): `.venv/Scripts/python.exe spikes/dbt_dagster/validate_spike.py`
- Real dbt: `pip install dbt-duckdb`, then `cd spikes/dbt_dagster && dbt build`
- Real Dagster: `pip install dagster dagster-dbt`, then `dagster dev -f dagster_defs.py`
