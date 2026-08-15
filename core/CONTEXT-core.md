# Core Module Context: `core`

This document provides an exhaustive reference for the top-level modules in [`core`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core).

---

## Executive Overview & Architectural Model

The `core` directory forms the main logic engine of the platform, orchestrating data engineering workloads, onboarding pipelines, governance policies, profiling, and metadata storage. Top-level files in `core` provide environment configuration, file path resolution, safety guards, system diagnostic functions, failure reporting, and service wrappers.

```
┌──────────────────────────┐       ┌──────────────────────────┐
│   platform_readiness.py  ├──────►│   Databricks / dbt Check │
└────────────┬─────────────┘       └──────────────────────────┘
             │
             ▼
┌──────────────────────────┐       ┌──────────────────────────┐
│        config.py         ├──────►│  paths.py / Databricks   │
└────────────┬─────────────┘       └──────────────────────────┘
             │
             ▼
┌──────────────────────────┐       ┌──────────────────────────┐
│      sql_safety.py       ├──────►│  AST / Query Validation  │
└──────────────────────────┘       └──────────────────────────┘
```

---

## File Details

### 1. [`config.py`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/config.py)

- **Exact Purpose**: Manages platform configuration settings, Databricks credential resolution, environment variable overrides, enterprise multi-tenant configuration, and runtime environment settings.
- **Key Functions / Classes**:
  - [`resolve_databricks_config(workspace_dir, enterprise_id)`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/config.py#L40-L120): Resolves Databricks host, token, catalog, schema, warehouse ID, and authentication settings from lockfiles or environment variables.
  - [`get_workspace_settings(workspace_dir)`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/config.py#L125-L160): Reads workspace-specific settings from `workspace_settings.json`.
- **Inputs & Outputs**:
  - *Inputs*: Environment variables (`DATABRICKS_HOST`, `DATABRICKS_TOKEN`, `AUTORESEARCH_*`), `config/lock.toml`, `workspace_settings.json`.
  - *Outputs*: Configuration dictionaries and settings objects used by execution backends.
- **Failure Modes & Edge Cases**:
  - Returns fallback/local defaults if lockfiles or Databricks configs are missing.
  - Raises ValueError on invalid enterprise IDs or missing lockfile paths when explicit enterprise is specified.

### 2. [`dashboard_services.py`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/dashboard_services.py)

- **Exact Purpose**: Shared service layer powering the web dashboard backend and interactive visualization elements.
- **Key Functions / Classes**:
  - [`load_workspace_summary(workspace_dir)`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/dashboard_services.py#L30-L90): Gathers KPI status, execution runs, profiles, and state information for rendering in UI components.
  - [`get_kpi_results_summary(workspace_dir)`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/dashboard_services.py#L95-L150): Parses KPI execution results and formats dataset previews.
- **Inputs & Outputs**:
  - *Inputs*: `workspaces/<ws>/interns/` generated artifacts and state databases.
  - *Outputs*: JSON-serializable dictionaries for API endpoints and dashboard UI state.
- **Failure Modes & Edge Cases**:
  - Gracefully handles missing `interns/` directories or empty run histories by returning empty fallback schemas.

### 3. [`failures.py`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/failures.py)

- **Exact Purpose**: Standardized exception types and failure classification primitives across core sub-systems.
- **Key Functions / Classes**:
  - [`PlatformError`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/failures.py#L10-L25): Base exception class for platform errors.
  - [`BlockerUnresolvedError`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/failures.py#L26-L40): Exception raised when execution attempts to proceed with unresolved business definition blockers.
  - [`ContractValidationError`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/failures.py#L41-L55): Raised when artifact validation fails against JSON contracts.
- **Inputs & Outputs**:
  - *Inputs*: Failure messages, error codes, contextual data.
  - *Outputs*: Exception instances with structured metadata.
- **Failure Modes & Edge Cases**:
  - Ensures clean exception propagation without leaking sensitive credentials in stack traces.

### 4. [`paths.py`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/paths.py)

- **Exact Purpose**: Central repository path resolver for workspaces, generated artifacts, configuration files, and state logs.
- **Key Functions / Classes**:
  - [`get_workspace_dir(project_name)`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/paths.py#L15-L35): Returns normalized Path object for a given workspace name.
  - [`get_interns_dir(workspace_dir)`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/paths.py#L36-L55): Resolves `workspaces/<project>/interns/` directory structure.
  - [`get_generated_dir(workspace_dir)`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/paths.py#L56-L75): Resolves `interns/generated/` folder.
- **Inputs & Outputs**:
  - *Inputs*: Workspace names or paths.
  - *Outputs*: `pathlib.Path` objects.
- **Failure Modes & Edge Cases**:
  - Normalizes relative and absolute paths across Windows and POSIX systems.

### 5. [`platform_readiness.py`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/platform_readiness.py)

- **Exact Purpose**: Diagnostic utility checking system dependencies, Databricks connectivity, dbt availability, and Airflow/Cosmos environment readiness.
- **Key Functions / Classes**:
  - [`check_platform_readiness(workspace_dir, enterprise_id)`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/platform_readiness.py#L30-L120): Evaluates local tool installations and cloud authentication status.
- **Inputs & Outputs**:
  - *Inputs*: Optional workspace directory and enterprise ID.
  - *Outputs*: Readiness status dictionary containing `databricks`, `dbt`, `airflow`, and overall status (`ready`, `blocked`, `not_configured`).
- **Failure Modes & Edge Cases**:
  - Non-destructive execution; never mutates external credentials or files. Catching network timeouts gracefully.

### 6. [`sql_safety.py`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/sql_safety.py)

- **Exact Purpose**: SQL injection prevention, unsafe pattern sanitization, and AST static checks for generated SQL queries.
- **Key Functions / Classes**:
  - [`validate_sql_safety(query)`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/sql_safety.py#L20-L75): Scans SQL string for DDL, unsafe drops, inline file reading, or injection hazards.
- **Inputs & Outputs**:
  - *Inputs*: SQL query string.
  - *Outputs*: Boolean safety flag or raises `UnsafeSQLError`.
- **Failure Modes & Edge Cases**:
  - Rejects dangerous SQL syntax while allowing standard analytical CTEs, aggregations, and joins.
