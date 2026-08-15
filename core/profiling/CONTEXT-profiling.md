# Core Profiling Architecture Context: `core/profiling`

This document provides an exhaustive reference for all components in [`core/profiling`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/profiling).

---

## Executive Overview & Architectural Model

The `core/profiling` package generates statistical profiles of source datasets, inspects column types, computes null counts, samples values, and generates dataset identity hashes.

---

## File Details

### 1. [`data_model_profiler.py`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/profiling/data_model_profiler.py)

- **Exact Purpose**: Profiles local datasets using Polars, computing column statistics, data distributions, distinct counts, and candidate primary/foreign key relationships.
- **Key Functions / Classes**:
  - [`profile_workspace_datasets(workspace_dir)`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/profiling/data_model_profiler.py#L30-L130): Scans `datasets/` folder and writes `*.profile.json` files to `interns/generated/profiles/`.
- **Inputs & Outputs**:
  - *Inputs*: Local CSV, Parquet, or Delta datasets.
  - *Outputs*: Individual dataset profiles and `profile_index.json`.
- **Failure Modes & Edge Cases**:
  - Uses Polars chunking to avoid memory exhaustion on large files.

### 2. [`databricks_table_profiler.py`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/profiling/databricks_table_profiler.py)

- **Exact Purpose**: Remote profiler targeting Unity Catalog tables using SQL-warehouse sampling.
- **Key Functions / Classes**:
  - [`profile_databricks_catalog(catalog, schema, client)`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/profiling/databricks_table_profiler.py#L25-L90): Executes `SHOW TABLES` and descriptive SQL queries to generate remote table profiles.
- **Inputs & Outputs**:
  - *Inputs*: Unity Catalog name, schema name, Databricks execution client.
  - *Outputs*: Remote dataset profile objects.
- **Failure Modes & Edge Cases**:
  - Handles permission errors on restricted tables by profiling available metadata only.

### 3. [`dataset_identity.py`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/profiling/dataset_identity.py)

- **Exact Purpose**: Generates deterministic fingerprints and content hashes for source datasets to detect data updates or drift.
- **Key Functions / Classes**:
  - [`compute_dataset_fingerprint(dataset_path)`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/profiling/dataset_identity.py#L15-L50): Computes hash combining file size, mtime, schema, and sample rows.
- **Inputs & Outputs**:
  - *Inputs*: Dataset file or table URI.
  - *Outputs*: Hash string fingerprint.
- **Failure Modes & Edge Cases**:
  - Quickly detects schema changes without reading full file content.
