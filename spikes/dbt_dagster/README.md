# spikes/dbt_dagster — Phase 2 scoped spike

A throwaway-by-design proof: express the platform's medallion + KPIs for
`Healthcare-RCM-Data-Platform` as a standard **dbt Core** project (DuckDB) with
**Dagster** orchestration, keeping governance, and check it reproduces gold.

**Read [`FINDINGS.md`](./FINDINGS.md) for the verdict and the build-vs-adopt
recommendation.** This README is just the map.

## Layout
```
dbt_project.yml / profiles.yml      dbt Core config (duckdb + delta)
models/
  sources.yml                       bronze Delta tables as dbt sources
  staging/    stg_*.sql             L1: cast/rename + PII drop (no joins)
  intermediate/ int_claims_enriched L2: conformed star, approved edges only
  marts/      fct_kpi_00{1,2,3}.sql L3: one fact per KPI (matches gold)
tests/                              singular tests: no-PII gate + gold parity
dagster_defs.py                     Dagster @dbt_assets job (orchestration shape)
validate_spike.py                   OFFLINE proof: builds models in DuckDB +
                                    reconciles to gold (no dbt/dagster needed)
```

## Run
```bash
# Offline proof — works today, no extra deps:
.venv/Scripts/python.exe spikes/dbt_dagster/validate_spike.py

# Real dbt (optional):     pip install dbt-duckdb && (cd spikes/dbt_dagster && dbt build)
# Real Dagster (optional): pip install dagster dagster-dbt && dagster dev -f spikes/dbt_dagster/dagster_defs.py
```

## Status
Spike only — **not** wired into the product. Nothing here is imported by the
app or the KPI pipeline. Safe to delete if we decide not to adopt dbt.
