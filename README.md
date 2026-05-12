# Autoresearch

Autoresearch is a governed optimization control plane for scoreable data-engineering artifacts. The first supported domain is SQL/data-pipeline optimization: propose a candidate change, run it in an isolated backend, evaluate correctness and performance, trace the run, and record what was learned.

## Current Capabilities

- SQL optimization benchmark for `workspaces/sql_optimization/prescriber_features.sql`
- Local DuckDB execution backend
- Optional Databricks execution and telemetry scaffolding
- Semantic contracts from KPI registries, methodology JSON, and task-level rules
- Optimization memory for adaptive learning across runs
- Enterprise policy contracts for execution modes, SLA, approvals, failure behavior, and downcasting
- Governance decisions with evidence packs, policy gates, approval state, and human alerts
- Metadata-first data model profiling with sample/exact bounds and conservative downcast recommendations
- Dataset profiling, representation checks, and guardrail scoring
- Dashboard for run history, intern activity, governance decisions, and human alerts

## Core Layout

The platform engine is organized by responsibility:

- `core/orchestration/` wires runs together.
- `core/execution/` owns local and Databricks execution.
- `core/governance/` owns contracts, policies, approvals, and promotion gates.
- `core/optimization/` owns planning, memory, diff classification, and decision strategy.
- `core/profiling/` owns data model diagnostics.
- `core/agents/` owns intern and LLM routing.
- `core/observability/` owns metric parsing and telemetry.
- `core/storage/` owns SQLite/Git state.


## Promotion Model

Candidate optimizations are evaluated in a sandbox first. An improved metric is
not enough to promote a change: the run must pass semantic, SLA, correctness,
mode, and policy gates. Production defaults require human review; global
exploration can discover options but cannot auto-promote.

The default downcast policy is conservative:

- Integer downcasts require exact min/max proof.
- Float and decimal downcasts require explicit approval.
- Sample min/max values are diagnostic only; exact bounds are authoritative for production.

## Fresh Start

This repository intentionally excludes local datasets, state databases, run logs, credentials, caches, and generated profiler outputs. Add enterprise data through catalog/backend configuration, not by committing raw data files.

Never commit `.env`, tokens, local `state/`, DuckDB databases, CSV/PDF source dumps, parquet outputs, or generated hotspot/profile artifacts.

## Verify

```bash
uv run python -m unittest tests.test_enterprise_optimization
uv run python -m compileall core interns tools tests dashboard.py
```

The SQL benchmark requires local/catalog data to be provisioned first.
