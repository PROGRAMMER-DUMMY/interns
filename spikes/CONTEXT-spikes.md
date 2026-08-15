# Spikes Context: `spikes`

This document provides an exhaustive reference for all components in `spikes`.

---

## Executive Overview & Architectural Model

`spikes` contains experimental, throwaway-by-design proof-of-concept projects evaluating alternative framework integrations. Currently, it houses `spikes/dbt_dagster`, which evaluates expressing the platform's medallion transform and KPI layers as standard dbt Core DuckDB models orchestrated by Dagster while preserving governance invariants.

```
┌─────────────────────────────────────────────────────────┐
│                     Bronze Sources                      │
│                (models/sources.yml)                     │
└──────────────────────────┬──────────────────────────────┘
                           │ Staging Models (stg_*.sql)
                           ▼
┌─────────────────────────────────────────────────────────┐
│                 Conformed Intermediate                  │
│               (int_claims_enriched.sql)                 │
└──────────────────────────┬──────────────────────────────┘
                           │ Gold Marts (fct_kpi_*.sql)
                           ▼
┌─────────────────────────────────────────────────────────┐
│              Governance & Parity Tests                  │
│       (assert_no_pii_in_marts, validate_spike.py)       │
└─────────────────────────────────────────────────────────┘
```

---

## File Details

### 1. [`dbt_dagster/README.md`](file:///C:/Users/shubh/OneDrive/Desktop/interns/spikes/dbt_dagster/README.md)

- **Exact Purpose**: Layout map and execution guide for the Phase 2 dbt Core + Dagster spike.

### 2. [`dbt_dagster/FINDINGS.md`](file:///C:/Users/shubh/OneDrive/Desktop/interns/spikes/dbt_dagster/FINDINGS.md)

- **Exact Purpose**: Executive verdict and technical findings proving dbt Core models reproduce gold KPI results exactly while maintaining governance gates (PII dropping, single-attribution shares, FK integrity).

### 3. [`dbt_dagster/dagster_defs.py`](file:///C:/Users/shubh/OneDrive/Desktop/interns/spikes/dbt_dagster/dagster_defs.py)

- **Exact Purpose**: Dagster software-defined assets wrapper (`@dbt_assets`) turning dbt manifest nodes into an orchestrated asset graph with scheduling and run history.
- **Key Functions / Classes**:
  - [`hospital_a_dbt_assets(context, dbt)`](file:///C:/Users/shubh/OneDrive/Desktop/interns/spikes/dbt_dagster/dagster_defs.py#L32-L37): Streams `dbt build` execution through Dagster.
  - [`defs`](file:///C:/Users/shubh/OneDrive/Desktop/interns/spikes/dbt_dagster/dagster_defs.py#L40-L43): Dagster `Definitions` binding asset functions and `DbtCliResource`.

### 4. [`dbt_dagster/validate_spike.py`](file:///C:/Users/shubh/OneDrive/Desktop/interns/spikes/dbt_dagster/validate_spike.py)

- **Exact Purpose**: Dependency-free offline validation harness that compiles Jinja `{{ source() }}` and `{{ ref() }}` templates to DuckDB views, materializes models, and asserts gold parity and PII redaction.
- **Key Functions / Classes**:
  - [`compile_model(text)`](file:///C:/Users/shubh/OneDrive/Desktop/interns/spikes/dbt_dagster/validate_spike.py#L49-L56): Compiles dbt macro tags into executable DuckDB SQL.
  - [`main()`](file:///C:/Users/shubh/OneDrive/Desktop/interns/spikes/dbt_dagster/validate_spike.py#L65-L120): Builds views and executes parity, governance, and share sum validation checks.

### 5. [`dbt_dagster/dbt_project.yml`](file:///C:/Users/shubh/OneDrive/Desktop/interns/spikes/dbt_dagster/dbt_project.yml)

- **Exact Purpose**: Standard dbt Core project configuration file defining profile (`healthcare_rcm`), model paths, and materialization configs.

### 6. [`dbt_dagster/profiles.yml`](file:///C:/Users/shubh/OneDrive/Desktop/interns/spikes/dbt_dagster/profiles.yml)

- **Exact Purpose**: dbt target profile configuration mapping `healthcare_rcm` to DuckDB in-memory or file database targets.

### 7. Models and Tests Subdirectories

- [`dbt_dagster/models/sources.yml`](file:///C:/Users/shubh/OneDrive/Desktop/interns/spikes/dbt_dagster/models/sources.yml): Defines raw bronze Delta tables as dbt sources.
- [`dbt_dagster/models/staging/staging.yml`](file:///C:/Users/shubh/OneDrive/Desktop/interns/spikes/dbt_dagster/models/staging/staging.yml): Staging models schema documentation and dbt tests.
- [`dbt_dagster/models/staging/stg_departments.sql`](file:///C:/Users/shubh/OneDrive/Desktop/interns/spikes/dbt_dagster/models/staging/stg_departments.sql): Staging department entity model.
- [`dbt_dagster/models/staging/stg_patients.sql`](file:///C:/Users/shubh/OneDrive/Desktop/interns/spikes/dbt_dagster/models/staging/stg_patients.sql): Staging patient entity model dropping raw PII.
- [`dbt_dagster/models/staging/stg_transactions.sql`](file:///C:/Users/shubh/OneDrive/Desktop/interns/spikes/dbt_dagster/models/staging/stg_transactions.sql): Staging transaction entity model.
- [`dbt_dagster/models/intermediate/int_claims_enriched.sql`](file:///C:/Users/shubh/OneDrive/Desktop/interns/spikes/dbt_dagster/models/intermediate/int_claims_enriched.sql): Silver-level conformed star schema joining approved edges only.
- [`dbt_dagster/models/marts/marts.yml`](file:///C:/Users/shubh/OneDrive/Desktop/interns/spikes/dbt_dagster/models/marts/marts.yml): Gold marts schema and column test assertions.
- [`dbt_dagster/models/marts/fct_kpi_001_paid_trend.sql`](file:///C:/Users/shubh/OneDrive/Desktop/interns/spikes/dbt_dagster/models/marts/fct_kpi_001_paid_trend.sql): Gold mart for KPI 001 (paid trend).
- [`dbt_dagster/models/marts/fct_kpi_002_lives_share.sql`](file:///C:/Users/shubh/OneDrive/Desktop/interns/spikes/dbt_dagster/models/marts/fct_kpi_002_lives_share.sql): Gold mart for KPI 002 (lives share with single-attribution window).
- [`dbt_dagster/models/marts/fct_kpi_003_top_payers.sql`](file:///C:/Users/shubh/OneDrive/Desktop/interns/spikes/dbt_dagster/models/marts/fct_kpi_003_top_payers.sql): Gold mart for KPI 003 (top payers).
- [`dbt_dagster/tests/assert_kpi_003_reconciles_gold.sql`](file:///C:/Users/shubh/OneDrive/Desktop/interns/spikes/dbt_dagster/tests/assert_kpi_003_reconciles_gold.sql): Singular dbt test asserting gold reconciliation for KPI 003.
- [`dbt_dagster/tests/assert_no_pii_in_marts.sql`](file:///C:/Users/shubh/OneDrive/Desktop/interns/spikes/dbt_dagster/tests/assert_no_pii_in_marts.sql): Singular dbt test failing if PII columns reach any gold mart.

---

## 🧹 Code Hygiene & Integrity Audit

- 💀 **Dead Code**: None. Spike models are standalone and validated by `validate_spike.py`.
- 🔌 **Unwired Components**: ⚠️ `dbt_dagster` is explicitly a throwaway spike and is not imported by the main application or CLI pipeline.
- 👯 **Logic & Code Duplication**: None.
- ⚠️ **Broken References & Mismatches**: None.
