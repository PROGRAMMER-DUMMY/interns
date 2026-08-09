# Core Execution Architecture Context: `core/execution`

This document provides an exhaustive reference for all components in [`core/execution`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/execution).

---

## Executive Overview & Architectural Model

The `core/execution` package provides local DuckDB and remote Databricks SQL execution backends, handling query execution, Polars conversion, dataset sampling, and Unity Catalog execution.

```
┌───────────────────┐        ┌─────────────────────────┐
│    backend.py     ├───────►│  databricks_client.py   │
└─────────┬─────────┘        └─────────────────────────┘
          │
          ▼
┌───────────────────┐
│   DuckDB / Polars │
└───────────────────┘
```

---

## File Details

### 1. [`backend.py`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/execution/backend.py)

- **Exact Purpose**: Execution engine supporting local DuckDB processing and dispatching remote queries to Databricks when configured.
- **Key Functions / Classes**:
  - [`ExecutionBackend`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/execution/backend.py#L30-L120): Unified interface for running SQL queries against local Delta/Parquet files via DuckDB or remote Unity Catalog tables.
  - [`execute_sql(query, params)`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/execution/backend.py#L125-L180): Executes SQL string and returns Polars DataFrame.
- **Inputs & Outputs**:
  - *Inputs*: SQL string, parameters, workspace settings, backend mode.
  - *Outputs*: Polars DataFrame or Arrow dataset result.
- **Failure Modes & Edge Cases**:
  - Falls back to local DuckDB execution if Databricks execution is disabled or remote execution approval is not granted.

### 2. [`databricks_client.py`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/execution/databricks_client.py)

- **Exact Purpose**: Databricks SDK and REST API wrapper for statement execution, SQL warehouse interaction, and Unity Catalog metadata queries.
- **Key Functions / Classes**:
  - [`DatabricksClient`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/execution/databricks_client.py#L19): Manages authentication, statement execution, polling, and result fetching.
  - [`DatabricksClient.execute_query(sql, wait_timeout)`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/execution/databricks_client.py#L191-L228): Executes SQL and returns `(column_names, rows)`. Polls past `wait_timeout` while the statement is `PENDING`/`RUNNING`, so a cold-starting warehouse does not read as a failure; raises `RuntimeError` on any non-`SUCCEEDED` terminal state.
- **Inputs & Outputs**:
  - *Inputs*: Host, token, warehouse ID, SQL query.
  - *Outputs*: Query results dictionary or Polars DataFrame.
- **Failure Modes & Edge Cases**:
  - Handles HTTP status errors, auth expiration, and warehouse startup delays gracefully.
