# Core Onboarding Architecture Context: `core/onboarding`

This document provides an exhaustive reference for components in [`core/onboarding`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding).

---

## Executive Overview & Architectural Model

The `core/onboarding` package forms the pipeline engine responsible for data discovery, KPI extraction, feature mapping, derived feature synthesis, blocker question panel generation, evidence graph building, and automated onboarding workflow orchestration.

```
┌─────────────────────────┐        ┌─────────────────────────┐
│     data_quality.py     ├───────►│    evidence_graph.py    │
└────────────┬────────────┘        └────────────┬────────────┘
             │                                  │
             ▼                                  ▼
┌─────────────────────────┐        ┌─────────────────────────┐
│   data_source_panel.py  ├───────►│  pipeline_sql_gen.py    │
└─────────────────────────┘        └─────────────────────────┘
```

---

## Subdirectories & Context Maps

- [`kpi/`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/kpi): KPI intent contracts, blocker question panels, and SQL/Polars/PySpark query generators. See [`kpi/CONTEXT-kpi.md`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/kpi/CONTEXT-kpi.md).
- [`workspace/`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/workspace/CONTEXT-workspace.md): Workflow CLI runners, bootstrap, onboarding flow, and delegation. See [`workspace/CONTEXT-workspace.md`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/workspace/CONTEXT-workspace.md).
- [`data_model/`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/data_model/CONTEXT-data_model.md): Data understanding, model generation, and ERD image parsing. See [`data_model/CONTEXT-data_model.md`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/data_model/CONTEXT-data_model.md).
- [`lexicon/`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/lexicon/CONTEXT-lexicon.md): Lexicon vocabulary builder and domain phrasing. See [`lexicon/CONTEXT-lexicon.md`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/lexicon/CONTEXT-lexicon.md).
- [`sources/`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/sources/CONTEXT-sources.md): Source catalogs, external data intake, and discovery. See [`sources/CONTEXT-sources.md`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/sources/CONTEXT-sources.md).
- [`features/`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/features/CONTEXT-features.md): Feature derivation libraries, expression parsing, and derived feature option synthesis. See [`features/CONTEXT-features.md`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/features/CONTEXT-features.md).
- [`harness/`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/harness/CONTEXT-harness.md): Test harnesses and verification suites. See [`harness/CONTEXT-harness.md`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/harness/CONTEXT-harness.md).
- [`memory/`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/memory/CONTEXT-memory.md): Stakeholder memory, business rules, and accepted user definitions. See [`memory/CONTEXT-memory.md`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/memory/CONTEXT-memory.md).
- [`relationships/`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/relationships/CONTEXT-relationships.md): Referential integrity checks and join relationship discovery. See [`relationships/CONTEXT-relationships.md`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/relationships/CONTEXT-relationships.md).
- [`databricks/`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/databricks/CONTEXT-databricks.md): Databricks source mode onboarding handlers. See [`databricks/CONTEXT-databricks.md`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/databricks/CONTEXT-databricks.md).
- [`documents/`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/documents/CONTEXT-documents.md): Document candidate consumption and intake classification. See [`documents/CONTEXT-documents.md`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/documents/CONTEXT-documents.md).
- [`benchmark/`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/benchmark/CONTEXT-benchmark.md): Benchmark suites for onboarding performance. See [`benchmark/CONTEXT-benchmark.md`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/benchmark/CONTEXT-benchmark.md).

---

## File Details

### 1. [`artifact_contracts.py`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/artifact_contracts.py)

- **Exact Purpose**: Defines schema contracts and validation rules for generated onboarding artifacts (KPI registries, feature mappings, profile indices).
- **Key Functions / Classes**:
  - [`validate_artifact_contract(artifact_type, payload)`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/artifact_contracts.py#L20-L65): Validates artifact dictionaries against mandatory schemas.

### 2. [`blueprint.py`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/blueprint.py)

- **Exact Purpose**: LEGACY blueprint producer. `prepare_main` (`prepare-solution-blueprint`) is now a **deprecation redirect** to `core.blueprint.cli.prepare_blueprint_main` and writes nothing of its own (Task D1) — two producers for one artifact is how they drift, and `prepare-blueprint` is the one the intake-playback gate, `plan-provisioning` and `confirm-blueprint` all read. `_forwardable_argv` forwards `--workspace`/`--repo-root`/`--catalog` and the three schema flags; `--source-root` and `--ingestion-mode` are dropped with a named reason on stderr (the source comes from `declare-source`; ingestion is `generate-ingestion` + the separately-gated `run-ingestion`). **`build_blueprint` is still live** — `apply_main` (`apply-blueprint-answer`) calls it to edit a pre-redirect artifact and still stamps `generated_by: prepare-solution-blueprint`, which is why `core/blueprint/renderer.py`'s `current.legacy.{json,md}` preservation must not be deleted yet.
- **Key Functions / Classes**:
  - [`generate_blueprint(workspace_dir)`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/blueprint.py#L30-L100): Assembles intake decisions into structured blueprint document.

### 3. [`bronze_silver_standards.py`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/bronze_silver_standards.py)

- **Exact Purpose**: Defines medallion architecture transformation rules, naming conventions, and data standardization patterns for Bronze and Silver layers.

### 4. [`catalog_contract.py`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/catalog_contract.py)

- **Exact Purpose**: Source catalog contract definitions for managing tables, views, and external data sources.

### 5. [`cli_deprecation.py`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/cli_deprecation.py)

- **Exact Purpose**: Handles backward compatibility redirects for deprecated CLI entry points.

### 6. [`data_quality.py`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/data_quality.py)

- **Exact Purpose**: Profiling data quality rule generation, null checks, duplicate detection, and value range checking.

### 7. [`data_source_panel.py`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/data_source_panel.py)

- **Exact Purpose**: Prepares and renders data source selection panels for choosing between local files and Unity Catalog sources.

### 8. [`evidence_graph.py`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/evidence_graph.py)

- **Exact Purpose**: Builds knowledge evidence graph linking business terms, KPI measures, dataset columns, join paths, and dictionary definitions.

### 9. [`panel_contract.py`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/panel_contract.py)

- **Exact Purpose**: Schema definition for interactive blocker, approval, and options panels.

### 10. [`pipeline_deployment_plan.py`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/pipeline_deployment_plan.py)

- **Exact Purpose**: Generates deployment blueprints for scheduling dbt and Airflow pipeline execution.

### 11. [`pipeline_plan.py`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/pipeline_plan.py)

- **Exact Purpose**: Formulates end-to-end data processing pipeline plans combining medallion transformations and KPI queries.

### 12. [`pipeline_sql_generator.py`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/pipeline_sql_generator.py)

- **Exact Purpose**: Translates pipeline plans and semantic definitions into executable SQL CTE chains.

### 13. [`source_family_contracts.py`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/source_family_contracts.py)

- **Exact Purpose**: Standard contracts for different source data families (e.g. CSV, Parquet, Delta, JDBC, Kafka).
