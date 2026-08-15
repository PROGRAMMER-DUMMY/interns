# Superpowers Architecture Context: `docs/superpowers`

This document provides an exhaustive reference for all components in [`docs/superpowers`](file:///C:/Users/shubh/OneDrive/Desktop/interns/docs/superpowers).

---

## Executive Overview & Architectural Model

The `docs/superpowers` directory houses detailed design specifications and step-by-step implementation plans for major platform enhancements, advanced agent capabilities, profiling upgrades, and cloud-first architectural restructures.

```
┌─────────────────────────────────────────────────────────────┐
│                    docs/superpowers/specs/                  │
│  - 2026-08-03-kpi-column-concept-mapping-design.md          │
│  - 2026-08-04-databricks-cardinality-profiling-design.md    │
│  - 2026-08-05-cloud-first-restructure-design.md             │
└──────────────────────────────┬──────────────────────────────┘
                               │  Drives Implementation Plans
                               ▼
┌─────────────────────────────────────────────────────────────┐
│                    docs/superpowers/plans/                  │
│  - 2026-08-03-kpi-column-concept-mapping.md                 │
│  - 2026-08-04-databricks-cardinality-profiling.md           │
└─────────────────────────────────────────────────────────────┘
```

---

## File Details

### Subdirectory: `plans/`

#### 1. [`2026-08-03-kpi-column-concept-mapping.md`](file:///C:/Users/shubh/OneDrive/Desktop/interns/docs/superpowers/plans/2026-08-03-kpi-column-concept-mapping.md)

- **Exact Purpose**: Implementation plan fixing root causes behind noisy KPI feature-resolution blockers (formula-vocabulary extraction leakage and miscalibrated fallback scoring) and adding cardinality ratio and value pattern profiler signals.
- **Key Sections & Content**:
  - [`Goal & Architecture`](file:///C:/Users/shubh/OneDrive/Desktop/interns/docs/superpowers/plans/2026-08-03-kpi-column-concept-mapping.md#L5-L17): Outlines tasks across 5 core files ([`expression.py`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/features/expression.py), [`blocker_question_panel.py`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/kpi/blocker_question_panel.py), [`data_model_profiler.py`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/profiling/data_model_profiler.py), [`feature_resolver.py`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/kpi/feature_resolver.py), and fixture [`resolver_accuracy.py`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/dev/resolver_accuracy.py)).
  - [`Task 1: Stopword & Formula Vocabulary Filtering`](file:///C:/Users/shubh/OneDrive/Desktop/interns/docs/superpowers/plans/2026-08-03-kpi-column-concept-mapping.md#L60-L312): Filters formula-text tokens (`within`, `std`, `p95`, `dev`) while preserving domain concepts like `LOS`.
  - [`Task 2: Fallback Scorer Recalibration`](file:///C:/Users/shubh/OneDrive/Desktop/interns/docs/superpowers/plans/2026-08-03-kpi-column-concept-mapping.md#L316-L458): Re-aligns fallback scoring thresholds (`score >= 60` for high confidence) and gates recommendations on margin over runner-up.
  - [`Task 3: Profiler Signals (cardinality_ratio & value_pattern)`](file:///C:/Users/shubh/OneDrive/Desktop/interns/docs/superpowers/plans/2026-08-03-kpi-column-concept-mapping.md#L460-L695): Adds `cardinality_ratio` and `value_pattern` to `ColumnProfile`.
  - [`Task 4 & 4b/4c: Contextual Score Plumbing & 2-char ID Guard`](file:///C:/Users/shubh/OneDrive/Desktop/interns/docs/superpowers/plans/2026-08-03-kpi-column-concept-mapping.md#L704-L1032): Feeds profiler signals into `_contextual_score`, forwards them through `column_profile_summary`, and guards bare 2-char ID columns from false bonuses.
  - [`Task 5: Financial Correctness Auto-Proof Bar`](file:///C:/Users/shubh/OneDrive/Desktop/interns/docs/superpowers/plans/2026-08-03-kpi-column-concept-mapping.md#L1034-L1171): Requires dictionary corroboration before auto-proving high-risk financial features.
  - [`Task 6: Re-baseline Resolver Accuracy`](file:///C:/Users/shubh/OneDrive/Desktop/interns/docs/superpowers/plans/2026-08-03-kpi-column-concept-mapping.md#L1173-L1254): Locks end-to-end regression tests and updates resolver baseline fixtures.
- **Inputs & Outputs**:
  - *Inputs*: KPI semantic contracts, column profiles, resolver accuracy fixtures.
  - *Outputs*: Cleaned feature extraction, robust candidate scoring, updated regression test suites.
- **Failure Modes & Edge Cases**:
  - *Gate Regressions*: Test suite enforcement requires exact green-gate baseline preservation.

#### 2. [`2026-08-04-databricks-cardinality-profiling.md`](file:///C:/Users/shubh/OneDrive/Desktop/interns/docs/superpowers/plans/2026-08-04-databricks-cardinality-profiling.md)

- **Exact Purpose**: Implementation plan for adding `cardinality_ratio`, `value_pattern`, and `profile_tier` to Unity Catalog table profiling without triggering data scans or write operations.
- **Key Sections & Content**:
  - [`Goal & Constraints`](file:///C:/Users/shubh/OneDrive/Desktop/interns/docs/superpowers/plans/2026-08-04-databricks-cardinality-profiling.md#L5-L45): Strictly read-only metastore execution via `DESCRIBE TABLE EXTENDED`, avoiding `ANALYZE TABLE` calls on customer-owned tables.
  - [`Task 1: Unity Catalog Profiling Integration`](file:///C:/Users/shubh/OneDrive/Desktop/interns/docs/superpowers/plans/2026-08-04-databricks-cardinality-profiling.md#L48-L373): Implements `_read_cardinality_stats` in [`core/profiling/databricks_table_profiler.py`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/profiling/databricks_table_profiler.py), parsing `distinct_count` safely and setting defaults on missing metadata.
- **Inputs & Outputs**:
  - *Inputs*: Unity Catalog table metadata via `DatabricksClient`.
  - *Outputs*: Enhanced `ColumnProfile` outputs with `cardinality_ratio` and inferred `value_pattern`.
- **Failure Modes & Edge Cases**:
  - *Missing Metastore Statistics*: Absent statistics degrade gracefully to `None` with a warning, preserving existing sample profiling functionality.

---

### Subdirectory: `specs/`

#### 3. [`2026-08-03-kpi-column-concept-mapping-design.md`](file:///C:/Users/shubh/OneDrive/Desktop/interns/docs/superpowers/specs/2026-08-03-kpi-column-concept-mapping-design.md)

- **Exact Purpose**: Architectural design specification explaining root causes of feature resolution noise and establishing multi-signal, concept-driven column resolution.
- **Key Sections & Content**:
  - [`Context & Root Cause Analysis`](file:///C:/Users/shubh/OneDrive/Desktop/interns/docs/superpowers/specs/2026-08-03-kpi-column-concept-mapping-design.md#L3-L66): Details Bug 1 (unfiltered formula text words leaking as features) and Bug 2 (uncalibrated fallback scorer over-promoting generic matches).
  - [`Design Breakdown (Changes 1-6)`](file:///C:/Users/shubh/OneDrive/Desktop/interns/docs/superpowers/specs/2026-08-03-kpi-column-concept-mapping-design.md#L67-L141): Outlines stopword expansion, scorer unification, profiler signal integration, confidence bar recalibration, and financial correctness gating.
  - [`Out of Scope`](file:///C:/Users/shubh/OneDrive/Desktop/interns/docs/superpowers/specs/2026-08-03-kpi-column-concept-mapping-design.md#L142-L155): Explicitly defers persisted Concept Registries and general formula-derivability subsystems.
- **Inputs & Outputs**:
  - *Inputs*: Feature resolution pipeline analysis, schema-matching literature, workspace audit data.
  - *Outputs*: Architectural design decisions driving [`2026-08-03-kpi-column-concept-mapping.md`](file:///C:/Users/shubh/OneDrive/Desktop/interns/docs/superpowers/plans/2026-08-03-kpi-column-concept-mapping.md).

#### 4. [`2026-08-04-databricks-cardinality-profiling-design.md`](file:///C:/Users/shubh/OneDrive/Desktop/interns/docs/superpowers/specs/2026-08-04-databricks-cardinality-profiling-design.md)

- **Exact Purpose**: Design spec detailing non-invasive, zero-scan cardinality retrieval for Unity Catalog tables profiled via Databricks SQL warehouses.
- **Key Sections & Content**:
  - [`Context & Research Findings`](file:///C:/Users/shubh/OneDrive/Desktop/interns/docs/superpowers/specs/2026-08-04-databricks-cardinality-profiling-design.md#L3-L58): Explains why `approx_count_distinct` is unsuited for tight thresholding and documents Databricks metastore behavior with `DESCRIBE TABLE EXTENDED`.
  - [`Design & Error Handling`](file:///C:/Users/shubh/OneDrive/Desktop/interns/docs/superpowers/specs/2026-08-04-databricks-cardinality-profiling-design.md#L59-L127): Details `_read_cardinality_stats` integration, strict read-only execution, and graceful warning degradation.
- **Inputs & Outputs**:
  - *Inputs*: Databricks Unity Catalog table metadata.
  - *Outputs*: Design foundation for [`2026-08-04-databricks-cardinality-profiling.md`](file:///C:/Users/shubh/OneDrive/Desktop/interns/docs/superpowers/plans/2026-08-04-databricks-cardinality-profiling.md).

#### 5. [`2026-08-05-cloud-first-restructure-design.md`](file:///C:/Users/shubh/OneDrive/Desktop/interns/docs/superpowers/specs/2026-08-05-cloud-first-restructure-design.md)

- **Exact Purpose**: Comprehensive architectural spec re-orienting the platform from local-first DuckDB execution to a cloud-first Databricks + Airflow + dbt pipeline.
- **Key Sections & Content**:
  - [`Problem & Core Decisions D1-D7`](file:///C:/Users/shubh/OneDrive/Desktop/interns/docs/superpowers/specs/2026-08-05-cloud-first-restructure-design.md#L7-L32): Establishes cloud-first defaults, external data ingestion ownership, Airflow/Cosmos orchestration, additive-only safety model, and single-production-engine selection.
  - [`Spine Phases (Phases 0-5)`](file:///C:/Users/shubh/OneDrive/Desktop/interns/docs/superpowers/specs/2026-08-05-cloud-first-restructure-design.md#L58-L123): Defines Measure (Phase 0), Ask (Phase 1), Model (Phase 2), Choose (Phase 3), Blueprint (Phase 4), and Autopilot (Phase 5).
  - [`Decision Engine & Engine Selection`](file:///C:/Users/shubh/OneDrive/Desktop/interns/docs/superpowers/specs/2026-08-05-cloud-first-restructure-design.md#L126-L166): Engine matrix based on working set size and workload characteristics (DuckDB/Polars vs SQL/dbt vs PySpark).
  - [`Generator Hardening, DQ & Orchestration`](file:///C:/Users/shubh/OneDrive/Desktop/interns/docs/superpowers/specs/2026-08-05-cloud-first-restructure-design.md#L167-L213): Enforces dbt project rules (unique keys, explicit schema changes, liquid clustering), dbt tests / Lakeflow expectations, and Cosmos DAG patterns with backfill.
  - [`Build Order & Safety Model`](file:///C:/Users/shubh/OneDrive/Desktop/interns/docs/superpowers/specs/2026-08-05-cloud-first-restructure-design.md#L227-L269): 6-slice strangler migration path preserving local DuckDB as `--local` dev mode.
- **Inputs & Outputs**:
  - *Inputs*: Platform audit findings, engine benchmark research, customer workflow gaps.
  - *Outputs*: Approved target architecture for the cloud-first platform transition.

---

## 🧹 Code Hygiene & Integrity Audit

- 💀 **Dead Code**: None.
- 🔌 **Unwired Components**:
  - [`2026-08-05-cloud-first-restructure-design.md`](file:///C:/Users/shubh/OneDrive/Desktop/interns/docs/superpowers/specs/2026-08-05-cloud-first-restructure-design.md) is an approved design spec awaiting step-by-step strangler slice implementation.
- 👯 **Duplication & Overlap**:
  - Implementation plans in `plans/` directly correspond to design specifications in `specs/`. Overlap is intentional for design-to-execution traceability.
- ⚠️ **Mismatches & Risks**: None.
