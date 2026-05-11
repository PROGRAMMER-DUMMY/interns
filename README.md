# Autoresearch

Autoresearch is a governed optimization control plane for scoreable data-engineering artifacts. The first supported domain is SQL/data-pipeline optimization: propose a candidate change, run it in an isolated backend, evaluate correctness and performance, trace the run, and record what was learned.

## Current Capabilities

- SQL optimization benchmark for `workspaces/sql_optimization/prescriber_features.sql`
- Local DuckDB execution backend
- Optional Databricks execution and telemetry scaffolding
- Semantic contracts from KPI registries, methodology JSON, and task-level rules
- Optimization memory for adaptive learning across runs
- Dataset profiling, representation checks, and guardrail scoring
- Dashboard for run history and intern activity

## Fresh Start

This repository intentionally excludes local datasets, state databases, run logs, credentials, caches, and generated profiler outputs. Add enterprise data through catalog/backend configuration, not by committing raw data files.

Never commit `.env`, tokens, local `state/`, DuckDB databases, CSV/PDF source dumps, parquet outputs, or generated hotspot/profile artifacts.

## Verify

```bash
uv run python -m unittest tests.test_enterprise_optimization
uv run python -m compileall core interns tools tests dashboard.py
```

The SQL benchmark requires local/catalog data to be provisioned first.
