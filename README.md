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

## Agent Guidance

Agents should start with `AGENTS.md`. Repo-native operating skills live in `skills/` and cover
ambiguity handling, stakeholder interviews, preference memory, domain modeling, task onboarding,
workspace governance, and evolution.

## Workspace Output Layout

New projects live under `workspaces/<project>/`. Platform-generated outputs for
that project are grouped under `workspaces/<project>/interns/` so the project
root stays readable:

```text
workspaces/<project>/
  interns/
    state/        # workspace.db, run.log
    runs/         # per-run artifacts
    generated/    # contracts, profiles, evidence, solutions, requirements, memory
    reports/      # human-readable reports
```

`workspaces/**/interns/` is ignored by git because it contains local run output.

Fresh workspace onboarding:

```powershell
uv run onboard-workspace --workspace workspaces/<project>
```

The onboarding command discovers data, KPI/metric registries, and data model
artifacts, profiles datasets with the canonical profiler, and generates baseline
contracts, reports, runner/evaluator scripts, and query artifacts under
`workspaces/<project>/interns/`.

## Metadata Store

Autoresearch uses hybrid storage:

- executable artifacts stay as files under `workspaces/<project>/interns/`
  (`kpi_metrics.sql`, evaluator scripts, reports, logs, DuckDB state);
- structured JSON state is written through a metadata store
  (`contracts`, `profiles`, `requirements`, `bootstrap`, mappings, decisions).

Local mode requires no setup and stores structured metadata as local Delta tables under:

```text
workspaces/<project>/interns/state/delta_metadata/
```

If Delta writes fail, the system falls back to JSON under:

```powershell
workspaces/<project>/interns/state/metadata_store/
```

Enterprise Databricks deployments can map the same collections to Delta tables
in Unity Catalog. MongoDB remains optional for document-store environments:

```powershell
$env:AUTORESEARCH_METADATA_BACKEND = "mongo"
$env:AUTORESEARCH_MONGO_URI = "mongodb://..."
$env:AUTORESEARCH_MONGO_DB = "autoresearch"
uv sync --extra enterprise-metadata
```

If MongoDB is unavailable, writes fall back to the local JSON metadata store and
record a warning under the workspace reports.

The experiment loop also performs local-safe auto-bootstrap. If required
`interns/` artifacts are missing or stale, it fingerprints workspace inputs,
reruns onboarding, and then executes the local baseline. Existing artifacts are
reused when the input fingerprint is current.

Databricks is never used for remote execution just because credentials exist.
The system may health-check Databricks, but remote execution requires explicit
approval via:

```powershell
$env:AUTORESEARCH_ALLOW_REMOTE_EXECUTION = "1"
```


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
