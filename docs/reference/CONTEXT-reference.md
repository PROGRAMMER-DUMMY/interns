# Reference Architecture Context: `docs/reference`

This document provides an exhaustive reference for all components in [`docs/reference`](file:///C:/Users/shubh/OneDrive/Desktop/interns/docs/reference).

---

## Executive Overview & Architectural Model

The `docs/reference` directory serves as the ground-truth technical repository and research foundation for data engineering practices, CLI tool specifications (`dbt`, `databricks`, `airflow`), medallion lakehouse architecture, and platform audit findings.

```
┌─────────────────────────────────────────────────────────────┐
│                   docs/reference/index.md                   │
│             (Reference Index & Component Sitemap)           │
└──────────────────────────────┬──────────────────────────────┘
                               │
       ┌───────────────────────┼───────────────────────┐
       ▼                       ▼                       ▼
┌──────────────┐        ┌──────────────┐        ┌──────────────┐
│  Operator    │        │  Data &      │        │  Engine &    │
│  CLI Specs   │        │  Medallion   │        │  Quality     │
│(dbt, airflow,│        │  Practices   │        │  Research    │
│ databricks)  │        │(medallion,   │        │(engine_roles,│
│              │        │ senior_de...)│        │  data_qual..)│
└──────────────┘        └──────────────┘        └──────────────┘
```

---

## File Details

### 1. [`airflow_cli_reference.md`](file:///C:/Users/shubh/OneDrive/Desktop/interns/docs/reference/airflow_cli_reference.md)

- **Exact Purpose**: Operator-grade reference for Apache Airflow 3.x and Astro CLI, mapping CLI operations to the platform's DAG generators ([`airflow_dag.py`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/orchestration/airflow_dag.py), [`cosmos_dag.py`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/orchestration/cosmos_dag.py), [`dbt_backfill.py`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/orchestration/dbt_backfill.py)).
- **Key Sections & Content**:
  - [`Installation & Container Runtime`](file:///C:/Users/shubh/OneDrive/Desktop/interns/docs/reference/airflow_cli_reference.md#L28-L86): Recommends Astro CLI + Docker runtime over in-venv `pip install` to prevent dependency downgrades.
  - [`Command Inventory`](file:///C:/Users/shubh/OneDrive/Desktop/interns/docs/reference/airflow_cli_reference.md#L87-L178): Documents `dags`, `tasks`, `backfill`, `db`, and `connections` subcommands.
  - [`Dev Loop & Verification`](file:///C:/Users/shubh/OneDrive/Desktop/interns/docs/reference/airflow_cli_reference.md#L180-L200): Outlines `astro dev parse`, `airflow tasks test`, and `airflow dags test` dev loops.
- **Inputs & Outputs**:
  - *Inputs*: Airflow DAG modules, Astro CLI environment configurations.
  - *Outputs*: Verified Airflow DAG execution and backfill workflows.

### 2. [`chart_selection_guide.md`](file:///C:/Users/shubh/OneDrive/Desktop/interns/docs/reference/chart_selection_guide.md)

- **Exact Purpose**: Decision tree reference and knowledge base supporting [`core/dashboard/chart_knowledge.py`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/dashboard/chart_knowledge.py) for picking dashboard chart types from KPI result shapes.
- **Key Sections & Content**:
  - [`Data-to-Viz Family Mapping`](file:///C:/Users/shubh/OneDrive/Desktop/interns/docs/reference/chart_selection_guide.md#L9-L24): Maps ordered time series, one categorical, two categorical, and measure-only data shapes to specific chart types.
  - [`Encoded Chart Caveats`](file:///C:/Users/shubh/OneDrive/Desktop/interns/docs/reference/chart_selection_guide.md#L25-L37): Enforces pie/donut slice limits (<=5), spaghetti line limits (<=6 series), and lollipop replacements.
- **Inputs & Outputs**:
  - *Inputs*: Data shapes and column dtypes from KPI result tables.
  - *Outputs*: Selected Plotly chart specifications.

### 3. [`data_quality_frameworks_research.md`](file:///C:/Users/shubh/OneDrive/Desktop/interns/docs/reference/data_quality_frameworks_research.md)

- **Exact Purpose**: Comprehensive evaluation of data quality frameworks (dbt tests, Deequ, Great Expectations, Soda Core, Databricks expectations, DQX) for Databricks + dbt stacks.
- **Key Sections & Content**:
  - [`Executive Summary & Comparison Matrix`](file:///C:/Users/shubh/OneDrive/Desktop/interns/docs/reference/data_quality_frameworks_research.md#L8-L84): Establishes a two-tool split (dbt tests at model layer, Lakeflow/DQX at bronze ingestion boundary).
  - [`Check-Writing Patterns & Layer Placement`](file:///C:/Users/shubh/OneDrive/Desktop/interns/docs/reference/data_quality_frameworks_research.md#L116-L159): Defines Bronze (schema/freshness), Silver (uniqueness/referential), and Gold (business rules) check tiers.
  - [`Profile-Derived vs. Business-Rule Checks`](file:///C:/Users/shubh/OneDrive/Desktop/interns/docs/reference/data_quality_frameworks_research.md#L160-L197): Separates auto-derivable checks (null rates, types, uniqueness) from business-rule inputs.
- **Inputs & Outputs**:
  - *Inputs*: Data quality framework specs, Lakeflow documentation.
  - *Outputs*: DQ architecture standards for the platform's generators.

### 4. [`data_workflow_medallion_reference.md`](file:///C:/Users/shubh/OneDrive/Desktop/interns/docs/reference/data_workflow_medallion_reference.md)

- **Exact Purpose**: Reference guide detailing the Medallion Architecture (Bronze, Silver, Gold layers), ingestion methods, data cleaning, and analytical serving patterns.
- **Key Sections & Content**:
  - [`Medallion Architecture Overview`](file:///C:/Users/shubh/OneDrive/Desktop/interns/docs/reference/data_workflow_medallion_reference.md#L85-L150): Defines Bronze (raw append-only), Silver (cleaned/conformed), and Gold (aggregated business-ready).
  - [`Cleaning Techniques`](file:///C:/Users/shubh/OneDrive/Desktop/interns/docs/reference/data_workflow_medallion_reference.md#L151-L200): SQL deduplication with `ROW_NUMBER()`, Polars null handling, and type standardization.
- **Inputs & Outputs**:
  - *Inputs*: Raw source datasets (SQL, APIs, files, streams).
  - *Outputs*: Cleaned Silver tables and analytical Gold marts.

### 5. [`databricks_cli_reference.md`](file:///C:/Users/shubh/OneDrive/Desktop/interns/docs/reference/databricks_cli_reference.md)

- **Exact Purpose**: Operator-grade reference for the Databricks unified Go CLI (v1.7.0), documenting command groups, authentication resolution, and Unity Catalog execution syntax.
- **Key Sections & Content**:
  - [`Command-Group Inventory`](file:///C:/Users/shubh/OneDrive/Desktop/interns/docs/reference/databricks_cli_reference.md#L18-L67): Classifies 90+ CLI command groups into CORE, USE, and SKIP.
  - [`Auth Mechanics & Profile Conflicts`](file:///C:/Users/shubh/OneDrive/Desktop/interns/docs/reference/databricks_cli_reference.md#L68-L148): Unified auth resolution order and diagnosis of same-host profile conflicts.
  - [`Unity Catalog Operations`](file:///C:/Users/shubh/OneDrive/Desktop/interns/docs/reference/databricks_cli_reference.md#L155-L200): Command syntax for storage credentials, external locations, catalogs, schemas, volumes, and grants.
- **Inputs & Outputs**:
  - *Inputs*: Databricks workspace configurations, CLI parameters.
  - *Outputs*: Executed CLI commands for provisioning and deployment.

### 6. [`databricks_production_practices.md`](file:///C:/Users/shubh/OneDrive/Desktop/interns/docs/reference/databricks_production_practices.md)

- **Exact Purpose**: Platform standard for Databricks production environments, detailing Delta Lake optimization, Lakeflow pipelines, Auto Loader, SQL warehouses, and governance.
- **Key Sections & Content**:
  - [`Architecture Defaults & Delta Standards`](file:///C:/Users/shubh/OneDrive/Desktop/interns/docs/reference/databricks_production_practices.md#L70-L130): Enforces UC managed tables, liquid clustering over Z-ORDER, and predictive optimization.
  - [`Medallion & Lakeflow Pipelines`](file:///C:/Users/shubh/OneDrive/Desktop/interns/docs/reference/databricks_production_practices.md#L131-L192): Expectations strategy (`warn`, `drop`, `fail`) and Auto Loader notification modes.
- **Inputs & Outputs**:
  - *Inputs*: Production data models and Delta table configurations.
  - *Outputs*: Governed production Delta tables and Lakeflow pipelines.

### 7. [`databricks_token_scopes.md`](file:///C:/Users/shubh/OneDrive/Desktop/interns/docs/reference/databricks_token_scopes.md)

- **Exact Purpose**: Reference guide mapping Databricks OAuth and PAT security scopes to platform requirements for minimal-privilege token generation.
- **Inputs & Outputs**:
  - *Inputs*: Service principal roles and access requirements.
  - *Outputs*: Scoped token parameters for CLI and SDK access.

### 8. [`dbt_agentic_cli_reference.md`](file:///C:/Users/shubh/OneDrive/Desktop/interns/docs/reference/dbt_agentic_cli_reference.md)

- **Exact Purpose**: Specialized reference guiding autonomous agents on executing dbt commands safely via shell tools, interpreting exit codes, and inspecting manifests.
- **Inputs & Outputs**:
  - *Inputs*: Generated dbt project files.
  - *Outputs*: Agent CLI execution results and parsed JSON metadata.

### 9. [`dbt_cli_reference.md`](file:///C:/Users/shubh/OneDrive/Desktop/interns/docs/reference/dbt_cli_reference.md)

- **Exact Purpose**: Operator-grade reference for `dbt-core` (v1.11) and `dbt-databricks` (v1.12), covering node selection, slim CI deferral, microbatch backfills, and artifacts.
- **Key Sections & Content**:
  - [`Command Inventory & Flags`](file:///C:/Users/shubh/OneDrive/Desktop/interns/docs/reference/dbt_cli_reference.md#L27-L88): Detailed analysis of `dbt build`, `dbt parse`, `dbt ls`, `dbt retry`, `dbt clone`, and `dbt docs generate`.
  - [`Node Selection & Deferral`](file:///C:/Users/shubh/OneDrive/Desktop/interns/docs/reference/dbt_cli_reference.md#L89-L167): Graph operators (`+`, `@`), selection methods (`state:modified`, `result:fail`), and slim CI with `--defer`.
  - [`Generated Pipeline Drivers`](file:///C:/Users/shubh/OneDrive/Desktop/interns/docs/reference/dbt_cli_reference.md#L171-L200): `dbt parse` generation gate and WAP publish mechanics.
- **Inputs & Outputs**:
  - *Inputs*: `dbt_project.yml`, model SQL files, state manifests.
  - *Outputs*: Target artifacts (`manifest.json`, `run_results.json`, `sources.json`).

### 10. [`end_users_data_modeling_research.md`](file:///C:/Users/shubh/OneDrive/Desktop/interns/docs/reference/end_users_data_modeling_research.md)

- **Exact Purpose**: Research specification connecting consumer classes (exec reporting, analysts, BI, ML, reverse-ETL, compliance) to specific lakehouse data modeling choices (Star vs. OBT vs. Data Vault).
- **Key Sections & Content**:
  - [`Consumer Taxonomy & Design Deltas`](file:///C:/Users/shubh/OneDrive/Desktop/interns/docs/reference/end_users_data_modeling_research.md#L38-L127): Evaluates freshness SLAs, grain, and serving surfaces across 7 consumer classes.
  - [`14-Question Intake List`](file:///C:/Users/shubh/OneDrive/Desktop/interns/docs/reference/end_users_data_modeling_research.md#L129-L168): High-leverage questions driving modeling choices.
  - [`Lakehouse Modeling Technique Selection`](file:///C:/Users/shubh/OneDrive/Desktop/interns/docs/reference/end_users_data_modeling_research.md#L170-L200): Vendor positions (Databricks, dbt) and decision rules for star schemas vs. OBT projections.
- **Inputs & Outputs**:
  - *Inputs*: Stakeholder intake answers, analytical query shapes.
  - *Outputs*: Selected data modeling technique and dbt model layout.

### 11. [`engine_compute_selection_research.md`](file:///C:/Users/shubh/OneDrive/Desktop/interns/docs/reference/engine_compute_selection_research.md)

- **Exact Purpose**: Research reference for choosing a single execution engine (SQL/dbt, PySpark, Polars) and compute tier per workspace pipeline based on working set size and latency SLAs.
- **Key Sections & Content**:
  - [`Data Characterization & Decision Table`](file:///C:/Users/shubh/OneDrive/Desktop/interns/docs/reference/engine_compute_selection_research.md#L46-L120): 10-row decision matrix mapping scanned working set size (<5GB, 5-50GB, 50GB-1TB, >1TB) to engine choice.
  - [`Workspace Single-Engine Principle`](file:///C:/Users/shubh/OneDrive/Desktop/interns/docs/reference/engine_compute_selection_research.md#L121-L180): Establishes ingestion as platform infrastructure and transform DAG as single-engine.
  - [`Compute Tier Sizing & Serverless`](file:///C:/Users/shubh/OneDrive/Desktop/interns/docs/reference/engine_compute_selection_research.md#L181-L200): Serverless vs. classic job cluster sizing rules and break-even thresholds.
- **Inputs & Outputs**:
  - *Inputs*: Measured workspace file sizes, profiler metrics, latency SLAs.
  - *Outputs*: Engine recommendation and compute tier configuration.

### 12. [`engine_roles_at_scale_research.md`](file:///C:/Users/shubh/OneDrive/Desktop/interns/docs/reference/engine_roles_at_scale_research.md)

- **Exact Purpose**: Technical research pass analyzing multi-engine division of labor, the "polyglot tax", Polars single-node production ceilings, and cross-engine parity alternatives.
- **Key Sections & Content**:
  - [`Division of Labor & Cost Benchmarks`](file:///C:/Users/shubh/OneDrive/Desktop/interns/docs/reference/engine_roles_at_scale_research.md#L35-L88): Compares classic jobs, DBSQL serverless, and serverless jobs compute costs.
  - [`dbt Python Models vs. Standalone Spark`](file:///C:/Users/shubh/OneDrive/Desktop/interns/docs/reference/engine_roles_at_scale_research.md#L89-L153): Evaluates dbt Python model limits and submission methods.
  - [`Polars Production Limits & Unity Catalog Constraints`](file:///C:/Users/shubh/OneDrive/Desktop/interns/docs/reference/engine_roles_at_scale_research.md#L154-L200): Analyzes Decathlon's 50 GiB Polars rule and UC external writer limitations.
- **Inputs & Outputs**:
  - *Inputs*: Benchmark data, cloud cost matrices, multi-engine performance logs.
  - *Outputs*: Architecture standards for multi-engine workload routing.

### 13. [`index.md`](file:///C:/Users/shubh/OneDrive/Desktop/interns/docs/reference/index.md)

- **Exact Purpose**: Central sitemap and catalog listing all reference documents inside `docs/reference`.
- **Inputs & Outputs**:
  - *Inputs*: Directory contents of `docs/reference`.
  - *Outputs*: Markdown catalog table of all reference files.

### 14. [`kpi_column_concept_mapping_research.md`](file:///C:/Users/shubh/OneDrive/Desktop/interns/docs/reference/kpi_column_concept_mapping_research.md)

- **Exact Purpose**: Grounding research evaluating academic schema matching (COMA, Cupid, Similarity Flooding), semantic layers (MetricFlow, Cube, Looker), and feature stores to support concept-driven column resolution.
- **Key Sections & Content**:
  - [`Schema Matching Literature`](file:///C:/Users/shubh/OneDrive/Desktop/interns/docs/reference/kpi_column_concept_mapping_research.md#L21-L93): Rahm & Bernstein taxonomy, hybrid/composite matchers, and 1:n candidate correspondences.
  - [`Semantic & Metrics Layers`](file:///C:/Users/shubh/OneDrive/Desktop/interns/docs/reference/kpi_column_concept_mapping_research.md#L94-L162): MetricFlow entities (`expr`), Cube multi-fact views (`FULL JOIN` per fact), and Looker merged results.
  - [`Automated Feature Engineering`](file:///C:/Users/shubh/OneDrive/Desktop/interns/docs/reference/kpi_column_concept_mapping_research.md#L163-L200): Deep Feature Synthesis in Featuretools and Feast/Tecton feature views.
- **Inputs & Outputs**:
  - *Inputs*: Academic papers, semantic layer documentation, feature store specs.
  - *Outputs*: Design rationale for concept-driven feature resolution in [`feature_resolver.py`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/kpi/feature_resolver.py).

### 15. [`pipeline_practices_gap_research.md`](file:///C:/Users/shubh/OneDrive/Desktop/interns/docs/reference/pipeline_practices_gap_research.md)

- **Exact Purpose**: Research pass identifying production gaps in generated pipelines, including backfill primitives across engines, Cosmos DAG scaling, event-driven scheduling, and WAP on Delta Lake.
- **Key Sections & Content**:
  - [`Backfilling at Scale`](file:///C:/Users/shubh/OneDrive/Desktop/interns/docs/reference/pipeline_practices_gap_research.md#L59-L151): Airflow 3 `backfill create`, dbt `--event-time-start/end`, and Lakeflow `ONCE` flows.
  - [`Cosmos DAG Design & Event Triggers`](file:///C:/Users/shubh/OneDrive/Desktop/interns/docs/reference/pipeline_practices_gap_research.md#L155-L200): Manifest-based rendering, asset scheduling, and avoiding sensor traps.
- **Inputs & Outputs**:
  - *Inputs*: Pipeline execution logs, vendor best-practice guides.
  - *Outputs*: Hardening rules for generated dbt and Airflow artifacts.

### 16. [`self_hosted_lakehouse_ops_reference.md`](file:///C:/Users/shubh/OneDrive/Desktop/interns/docs/reference/self_hosted_lakehouse_ops_reference.md)

- **Exact Purpose**: Operations reference for self-hosted lakehouse deployments (MinIO object storage, PySpark baseline configurations, Delta Lake compaction, and Trino/Spark execution).
- **Key Sections & Content**:
  - [`MinIO Bucket Layout & Rules`](file:///C:/Users/shubh/OneDrive/Desktop/interns/docs/reference/self_hosted_lakehouse_ops_reference.md#L11-L52): Bucket paths, `ingest_date` partitioning, and `OPTIMIZE`/`VACUUM` SQL commands.
  - [`Spark Baseline Configuration`](file:///C:/Users/shubh/OneDrive/Desktop/interns/docs/reference/self_hosted_lakehouse_ops_reference.md#L54-L124): Memory settings, AQE configs, shuffle partition tuning, and PySpark job templates.
  - [`Delta Merge Incremental Upsert`](file:///C:/Users/shubh/OneDrive/Desktop/interns/docs/reference/self_hosted_lakehouse_ops_reference.md#L188-L200): PySpark Delta MERGE execution templates.
- **Inputs & Outputs**:
  - *Inputs*: MinIO endpoints, Spark cluster parameters.
  - *Outputs*: Configured self-hosted lakehouse processing scripts.

### 17. [`senior_data_engineer_patterns.md`](file:///C:/Users/shubh/OneDrive/Desktop/interns/docs/reference/senior_data_engineer_patterns.md)

- **Exact Purpose**: Sourced reference document capturing day-to-day operational patterns of senior/staff data engineers (on-call triage, slow query diagnosis, lineage, OBT vs. Star, schema drift, cost control).
- **Key Sections & Content**:
  - [`On-Call Triage & RCA`](file:///C:/Users/shubh/OneDrive/Desktop/interns/docs/reference/senior_data_engineer_patterns.md#L14-L29): Combining logs, deployments, and dependency context.
  - [`Slow Pipeline Diagnosis`](file:///C:/Users/shubh/OneDrive/Desktop/interns/docs/reference/senior_data_engineer_patterns.md#L30-L42): Full table scans, key skew salting, and broadcast joins.
  - [`Dimensional Modeling vs. OBT`](file:///C:/Users/shubh/OneDrive/Desktop/interns/docs/reference/senior_data_engineer_patterns.md#L60-L76): Coexistence model (Star core + OBT serving projections).
  - [`Idempotency & Error Handling`](file:///C:/Users/shubh/OneDrive/Desktop/interns/docs/reference/senior_data_engineer_patterns.md#L123-L141): Replace-don't-append, DLQ, and exponential backoff with jitter.
- **Inputs & Outputs**:
  - *Inputs*: Production incident postmortems, staff DE operational guides.
  - *Outputs*: Foundational engineering rules for platform logic.

### 18. [`voltagent_platform_audit_2026-08-05.md`](file:///C:/Users/shubh/OneDrive/Desktop/interns/docs/reference/voltagent_platform_audit_2026-08-05.md)

- **Exact Purpose**: Merged, deduplicated, and severity-ranked audit report produced by three VoltAgent reviewers (`data-engineer`, `database-optimizer`, `data-analyst`) detailing platform defects and missing features.
- **Key Sections & Content**:
  - [`Ranked Issues A1-A11`](file:///C:/Users/shubh/OneDrive/Desktop/interns/docs/reference/voltagent_platform_audit_2026-08-05.md#L10-L25): High/Medium/Low findings including full-refresh mart double-copy (A1), JDBC un-watermarked append (A2), unwired optimization playbook (A3), unapplied Delta retention (A4), and un-pushed dashboard reads (A5).
  - [`Missing Features B1-B17`](file:///C:/Users/shubh/OneDrive/Desktop/interns/docs/reference/voltagent_platform_audit_2026-08-05.md#L26-L50): Late-arriving dimensions (B1), PII erasure lifecycle (B2), ML feature serving (B3), reverse-ETL sync (B4), and regulatory bitemporal serving (B5).
- **Inputs & Outputs**:
  - *Inputs*: Platform codebase audit, read-only VoltAgent inspector outputs.
  - *Outputs*: Prioritized defect backlog and refactoring specifications.

---

## 🧹 Code Hygiene & Integrity Audit

- 💀 **Dead Code**: None.
- 🔌 **Unwired Components**:
  - [`voltagent_platform_audit_2026-08-05.md`](file:///C:/Users/shubh/OneDrive/Desktop/interns/docs/reference/voltagent_platform_audit_2026-08-05.md) details unwired capabilities (e.g. A3 optimization playbook callers, A4 Delta retention properties, B1-B6 missing features) marked for future integration.
- 👯 **Duplication & Overlap**:
  - Operational patterns overlap across [`senior_data_engineer_patterns.md`](file:///C:/Users/shubh/OneDrive/Desktop/interns/docs/reference/senior_data_engineer_patterns.md), [`databricks_production_practices.md`](file:///C:/Users/shubh/OneDrive/Desktop/interns/docs/reference/databricks_production_practices.md), and [`pipeline_practices_gap_research.md`](file:///C:/Users/shubh/OneDrive/Desktop/interns/docs/reference/pipeline_practices_gap_research.md). Overlap is intentional for domain specificity.
- ⚠️ **Mismatches & Risks**: None.
