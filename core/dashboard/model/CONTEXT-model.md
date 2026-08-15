# Core Dashboard Model Context: `core/dashboard/model`

This document provides an exhaustive reference for all components in `core/dashboard/model`.

---

## Executive Overview & Architectural Model

The `core/dashboard/model` package owns the live in-memory semantic data model for the Power BI-style dashboard layer. Instead of re-executing solution SQL for every UI interaction, it reads validated medallion layers (gold result tables and silver feature tables) and performs type-safe, case-insensitive in-memory re-aggregations.

```
┌─────────────────────────────────────────────────────────┐
│                      Medallion                          │
│  (Bronze / Silver / Gold Delta & Databricks Tables)     │
└──────────────────────────┬──────────────────────────────┘
                           │ read_gold / read_silver / read_bronze (layers.py)
                           ▼
┌─────────────────────────────────────────────────────────┐
│                   Conformed Model                       │
│    (Cleaned Star Schema & Governance / PII Redaction)   │
└──────────────────────────┬──────────────────────────────┘
                           │ aggregate() / apply_filters() (aggregate.py)
                           ▼
┌─────────────────────────────────────────────────────────┐
│               Crossfilter & Parity Engine               │
│   (Canvas Slicing, Drill-Downs, & Parity Certification) │
└─────────────────────────────────────────────────────────┘
```

---

## File Details

### 1. [`__init__.py`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/dashboard/model/__init__.py)

- **Exact Purpose**: Package initializer exporting the primary live dashboard model API and types.
- **Key Functions / Classes**:
  - Exports [`KpiModel`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/dashboard/model/__init__.py#L17-L24), [`NonAdditiveError`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/dashboard/model/__init__.py#L15), [`aggregate`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/dashboard/model/__init__.py#L15), [`build_kpi_model`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/dashboard/model/__init__.py#L18), [`check_parity`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/dashboard/model/__init__.py#L30), [`classify_measure`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/dashboard/model/__init__.py#L19), [`headline_agg`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/dashboard/model/__init__.py#L20), [`measure_fmt`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/dashboard/model/__init__.py#L21), [`measure_func`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/dashboard/model/__init__.py#L22), [`metric_goal`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/dashboard/model/__init__.py#L23), [`list_gold_kpis`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/dashboard/model/__init__.py#L26), [`parity_report`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/dashboard/model/__init__.py#L30), [`read_gold`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/dashboard/model/__init__.py#L27), [`read_silver`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/dashboard/model/__init__.py#L28).
- **Inputs & Outputs**:
  - *Inputs*: Internal modules within `core.dashboard.model`.
  - *Outputs*: Public package interface symbols via `__all__`.
- **Failure Modes & Edge Cases**:
  - Re-export failure if submodules fail to compile or have circular dependencies.

### 2. [`aggregate.py`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/dashboard/model/aggregate.py)

- **Exact Purpose**: Implements case-insensitive filtering and type-safe in-memory re-aggregation over Polars frames while enforcing additivity invariants.
- **Key Functions / Classes**:
  - [`NonAdditiveError`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/dashboard/model/aggregate.py#L26-L32): Raised when attempting to roll up non-additive measures (ratios/averages) across multiple rows.
  - [`is_numeric_dtype(dtype)`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/dashboard/model/aggregate.py#L35-L41): Determines whether a Polars data type is numeric.
  - [`resolve_column(name, columns)`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/dashboard/model/aggregate.py#L44-L54): Performs case-insensitive matching of a requested column against actual frame columns.
  - [`apply_filters(frame, filters)`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/dashboard/model/aggregate.py#L68-L87): Applies scalar, list (IN), or range filters to a Polars DataFrame.
  - [`aggregate(frame, *, measure, additive, group_by, filters)`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/dashboard/model/aggregate.py#L112-L163): Aggregates a measure over grouped dimensions after applying active slicers.
- **Inputs & Outputs**:
  - *Inputs*: Polars `DataFrame`, measure column name, boolean additivity flag, grouping column list, dictionary of slicer filters.
  - *Outputs*: Filtered and aggregated Polars `DataFrame`.
- **Failure Modes & Edge Cases**:
  - Throws `NonAdditiveError` if a non-additive measure is requested for grouping that merges multiple source rows into one cell.
  - Throws `KeyError` if the specified measure column cannot be resolved in the frame.

### 3. [`conformed.py`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/dashboard/model/conformed.py)

- **Exact Purpose**: Builds a workspace-agnostic conformed star schema from raw bronze entities, applying silver-standard cleaning, deduplication, timestamp normalization, PII dropping, and temporal axis derivation.
- **Key Functions / Classes**:
  - [`Measure`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/dashboard/model/conformed.py#L42-L48): Dataclass representing a derived measure (label, column, agg function, display format).
  - [`Edge`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/dashboard/model/conformed.py#L51-L56): Dataclass representing an executable foreign-key relationship between tables.
  - [`ConformedModel`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/dashboard/model/conformed.py#L59-L73): Dataclass containing the cleaned, joined star schema Polars frame and metadata.
  - [`approved_edges(layout)`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/dashboard/model/conformed.py#L91-L111): Reads `relationship_contracts.json` to extract executable foreign-key joins.
  - [`build_conformed_model(layout)`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/dashboard/model/conformed.py#L263-L323): Assembles raw bronze tables into a cleaned, PII-stripped star schema.
  - [`aggregate_measure(model, measure_name, ...)`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/dashboard/model/conformed.py#L331-L354): Aggregates a named measure live over the conformed model frame.
- **Inputs & Outputs**:
  - *Inputs*: `WorkspaceLayout`, bronze Delta tables, `relationship_contracts.json`, PII redaction patterns.
  - *Outputs*: `ConformedModel` instance with joined star schema frame and defined measures/dimensions.
- **Failure Modes & Edge Cases**:
  - Returns `None` if no bronze tables exist or no fact table can be inferred from relationships.
  - Drops PII columns completely to prevent accidental leakage in dashboard visualizers.

### 4. [`crossfilter.py`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/dashboard/model/crossfilter.py)

- **Exact Purpose**: Multi-KPI cross-filtering and drill-down engine that applies global slicers across all gold KPIs exposing shared dimensions.
- **Key Functions / Classes**:
  - [`CanvasModel`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/dashboard/model/crossfilter.py#L32-L53): Dataclass holding all gold KPI models and DataFrames in a workspace.
  - [`load_canvas(layout)`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/dashboard/model/crossfilter.py#L56-L66): Loads all materialized gold KPIs into a `CanvasModel`.
  - [`apply_global_filters(canvas, filters)`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/dashboard/model/crossfilter.py#L69-L81): Applies global filter dictionaries to each KPI possessing matching column names.
  - [`panel_data(kpi_model, gold, panel, filters)`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/dashboard/model/crossfilter.py#L97-L118): Generates aggregated Polars DataFrame for a specific UI panel recommendation.
  - [`drill(layout, kpi_model, gold, group_by, filters)`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/dashboard/model/crossfilter.py#L121-L165): Re-aggregates gold frame to an arbitrary grain, falling back to silver when finer columns are absent.
- **Inputs & Outputs**:
  - *Inputs*: `WorkspaceLayout`, `CanvasModel`, filter dicts, panel spec dicts.
  - *Outputs*: Sliced and aggregated Polars DataFrames or `None` if drill is unavailable.
- **Failure Modes & Edge Cases**:
  - Returns `None` when drilling to a grain unsupported by both gold and silver layers or when encountering a non-additive recombination.

### 5. [`cuts.py`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/dashboard/model/cuts.py)

- **Exact Purpose**: Per-KPI spec and schema classifier that resolves measure columns, cut dimensions, additivity kinds (`sum`, `share`, `ratio`), display formats, and business-facing card labels.
- **Key Functions / Classes**:
  - [`KpiModel`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/dashboard/model/cuts.py#L40-L54): Dataclass capturing metadata and schema structure of a single KPI.
  - [`classify_measure(*, agg, y_format, metric)`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/dashboard/model/cuts.py#L56-L69): Classifies measure into `(kind, additive)` tuple.
  - [`measure_func(metric, measure)`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/dashboard/model/cuts.py#L80-L95): Extracts leading aggregation function (e.g. `sum`, `count`, `avg`).
  - [`metric_goal(metric, measure, title)`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/dashboard/model/cuts.py#L114-L121): Determines whether a metric is `"higher"` or `"lower"` for semantic coloring.
  - [`measure_fmt(metric, measure, y_format)`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/dashboard/model/cuts.py#L124-L139): Returns formatting string (`percent`, `int`, `currency`, `float`).
  - [`business_label(metric, measure, fmt, title, ...)`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/dashboard/model/cuts.py#L216-L251): Derives a short human-readable card label from the KPI question.
  - [`build_kpi_model(layout, kpi_id, gold)`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/dashboard/model/cuts.py#L285-L322): Constructs `KpiModel` from actual gold DataFrame and spec hints.
- **Inputs & Outputs**:
  - *Inputs*: `WorkspaceLayout`, `kpi_id`, gold Polars DataFrame, KPI spec JSON.
  - *Outputs*: Populated `KpiModel` object.
- **Failure Modes & Edge Cases**:
  - Gracefully falls back to numeric column detection if spec hints point to non-existent columns.

### 6. [`dq.py`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/dashboard/model/dq.py)

- **Exact Purpose**: Data-quality certifier for the conformed model, checking null keys, primary key uniqueness, referential integrity, lack of fan-out, and lossless additive totals against raw bronze/gold.
- **Key Functions / Classes**:
  - [`run_quality_checks(model)`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/dashboard/model/dq.py#L40-L96): Evaluates intrinsic data quality rules against a `ConformedModel`.
  - [`reconcile_with_gold(model)`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/dashboard/model/dq.py#L99-L120): Verifies that conformed totals cover or equal gold KPI totals.
  - [`certify(model)`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/dashboard/model/dq.py#L123-L135): Runs full certification suit and returns status summary dictionary.
  - [`certification_report(model)`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/dashboard/model/dq.py#L138-L146): Generates human-readable ASCII certification report.
- **Inputs & Outputs**:
  - *Inputs*: `ConformedModel` instance.
  - *Outputs*: Certification dictionary or formatted text report.
- **Failure Modes & Edge Cases**:
  - Returns `ok=False` if orphan FK rate exceeds 2%, row counts fan out, or numeric totals fail reconciliation within tolerance (`1e-6`).

### 7. [`layers.py`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/dashboard/model/layers.py)

- **Exact Purpose**: Local-first reader for materialized medallion gold/silver/bronze tables, querying local Delta tables via DuckDB or remote Databricks Unity Catalog dbt marts when local data is absent.
- **Key Functions / Classes**:
  - [`list_gold_kpis(layout)`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/dashboard/model/layers.py#L109-L123): Lists all available KPI IDs with materialized gold tables.
  - [`read_gold(layout, kpi_id)`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/dashboard/model/layers.py#L126-L133): Reads a gold KPI result table as a Polars DataFrame.
  - [`read_silver(layout, kpi_id)`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/dashboard/model/layers.py#L136-L144): Reads row-grain silver features for a KPI.
  - [`list_bronze_tables(layout)`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/dashboard/model/layers.py#L147-L152): Lists raw bronze entity tables in workspace layout.
  - [`read_bronze(layout, table)`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/dashboard/model/layers.py#L155-L162): Reads a raw bronze entity table as a Polars DataFrame.
  - [`GoldSourceStatus`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/dashboard/model/layers.py#L279-L289): Dataclass representing data source origin and staleness timestamp.
  - [`gold_source_status(layout)`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/dashboard/model/layers.py#L311-L340): Determines current gold source location (`local_delta`, `databricks_dbt_mart`, or `unavailable`).
- **Inputs & Outputs**:
  - *Inputs*: `WorkspaceLayout`, `kpi_id` or table name.
  - *Outputs*: Polars `DataFrame` or `None` on failure/absence.
- **Failure Modes & Edge Cases**:
  - Remote Databricks reads require explicit `AUTORESEARCH_ALLOW_REMOTE_EXECUTION=1` environment variable.
  - Logs errors to stderr rather than raising exceptions to allow graceful fallback rendering.

### 8. [`parity.py`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/dashboard/model/parity.py)

- **Exact Purpose**: Parity gate module verifying that in-memory re-aggregations over gold frames perfectly match validated gold KPI results without identity drift or total preservation loss.
- **Key Functions / Classes**:
  - [`check_parity(layout)`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/dashboard/model/parity.py#L57-L103): Runs full identity and scalar sum parity checks for all gold KPIs in a workspace.
  - [`parity_report(layout)`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/dashboard/model/parity.py#L106-L124): Generates human-readable ASCII summary of parity checks across all KPIs.
- **Inputs & Outputs**:
  - *Inputs*: `WorkspaceLayout`.
  - *Outputs*: List of per-KPI check result dictionaries or formatted text report.
- **Failure Modes & Edge Cases**:
  - Returns `ok=False` for any KPI where full-cut regrouping produces row count or value mismatches compared to gold.

---

## 🧹 Code Hygiene & Integrity Audit

- 💀 **Dead Code**: None. All submodules (`aggregate`, `conformed`, `crossfilter`, `cuts`, `dq`, `layers`, `parity`) are actively imported and exposed through `__init__.py`.
- 🔌 **Unwired Components**: None. Every public function and class is wired into the dashboard profile and CLI pipeline.
- 👯 **Logic & Code Duplication**: None. Dataset display stems delegate cleanly to `core.profiling.dataset_identity` and financial tokens reuse `GENERIC_FINANCIAL_SEED`.
- ⚠️ **Broken References & Mismatches**: None found.
