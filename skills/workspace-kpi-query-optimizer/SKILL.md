---
name: workspace-kpi-query-optimizer
description: >
  Build, validate, and optimize query logic for any workspace that contains data, a KPI/metric
  registry, and a data model. Use for SQL, Polars, or hybrid KPI/query optimization tasks where
  generated outputs must live under workspaces/<project>/interns/.
---

# Workspace KPI Query Optimizer

Use this skill when a user asks to build, implement, validate, profile, or optimize KPI/metric
logic for a workspace project.

This skill is general purpose. It must work for any `workspaces/<project>/` folder, not only one
domain such as healthcare.

## Step 0: Active Workflow Setup

Do not assume the active workspace from stale config or file names alone.

1. Ask what the user wants to do if the task is unclear.
2. Ask the user to point to the active workspace if it is not already clear.
3. Scan likely files:

```powershell
rg --files workspaces config core tools tests
```

4. Identify and summarize the likely active set:
   - workspace root
   - data files or catalog inputs
   - KPI/metric registry
   - data model, schema docs, diagrams, or metadata
   - existing query/source artifact, if any
   - evaluator/runner, if any
   - task config
5. Ask for confirmation before writing outputs, unless the user explicitly asked for best-effort
   execution.

## Workspace Contract

Treat `workspaces/<project>/` as user/customer input. Do not modify source inputs unless explicitly
asked.

All generated or runtime output belongs under:

```text
workspaces/<project>/interns/
  state/                 # DuckDB/SQLite DBs, run logs, runtime state
  runs/                  # per-run artifacts
  reports/               # human-readable summaries, reviews, open questions
  generated/
    contracts/           # semantic contracts, assumptions, KPI mappings
    profiles/            # schema, stats, bounds, data evidence
    evidence/            # validation and optimization evidence
    solutions/           # generated SQL/Polars/query artifacts
    requirements/        # interpreted user requirements and solution briefs
    memory/              # lessons, decisions, improvement history
```

Generated files such as `kpi_metrics.sql`, `analytics.duckdb`, evaluator scripts, profiles, reports,
and run logs must not be placed directly in the workspace root.

Task-selection conversations, grill-me interviews, accepted recommendations, stakeholder
preferences, and user decisions are generated project artifacts. Save them under
`interns/generated/requirements/`, `interns/generated/memory/`, or `interns/reports/`.

Structured JSON artifacts should be written through the metadata store as well as
their workspace files. Local mode stores metadata as Delta tables under
`interns/state/delta_metadata/`, with JSON fallback under `interns/state/metadata_store/`.
Enterprise Databricks deployments should map the same collections to Delta tables.
Keep executable SQL, runner/evaluator scripts, reports, logs, DuckDB state, raw data,
and user docs as files.

## Data Handling Rule

Use Polars for dataframe and file work by default:

- schema inspection
- sampling
- KPI registry reading when supported
- CSV/parquet/json processing
- profiling
- metadata extraction
- generated helper utilities

Do not introduce pandas for normal workspace work. If a third-party API explicitly requires pandas,
document the reason and keep conversion at the integration boundary.

For Excel KPI registries, prefer Polars/fastexcel when available. If unavailable, use a minimal XLSX
reader fallback before installing new dependencies.

## Required Workflow

1. Read `AGENTS.md`, `CONTEXT.md`, `config/tasks.json`, and relevant repo skills.
2. Confirm the active workspace and input files.
3. Create the `interns/` runtime layout if it does not exist.
4. Read KPI/metric registry and data model.
5. Profile available datasets with the existing profiling system first.
6. Generate or update domain and semantic contracts.
7. Map KPI terms to available tables, columns, joins, filters, and grains.
8. Record ambiguities and missing data model requirements instead of silently guessing.
9. Generate a baseline query implementation under `interns/generated/solutions/`.
10. Create a local runner/evaluator under `interns/` only if one is missing.
11. Run baseline and record correctness, runtime, errors, and assumptions.
12. Optimize only after a baseline exists.
13. Accept an optimization only when correctness is preserved or the semantic risk is explicitly
    marked for human review.
14. Save what changed, why it was expected to help, observed result, and residual risk.

The main loop may run local-safe auto-bootstrap automatically. If generated artifacts are missing
or stale, it fingerprints workspace inputs and regenerates onboarding artifacts under `interns/`.
If artifacts are current, it reuses them.

Remote execution, including Databricks Jobs, SQL Warehouse, or Connect, requires explicit approval.
Do not use remote compute merely because credentials are present. Without approval, continue with
the local DuckDB backend.

## Skill Chain For KPI Mapping

Use the repo skills as an ordered workflow, not as isolated documents:

1. `workspace-governance`: confirm the active workspace and protect source inputs.
2. `domain-model`: extract entities, facts, dimensions, keys, joins, grain, terms, and synonyms.
3. `task-onboarding`: create or refresh profiles, contracts, requirements, and baseline artifacts.
4. `clarify-ambiguity`: ask one targeted question only when a mapping cannot be resolved from files,
   metadata, dictionaries, catalog evidence, or reasonable reversible assumptions.
5. `grill-requirements`: when several business-valid interpretations exist, interview the user/team
   one decision at a time and save accepted answers.
6. `stakeholder-memory`: persist accepted preferences, naming choices, risk tolerance, and recurring
   business definitions.
7. `to-solution-brief`: convert accepted mapping and requirement decisions into implementation
   direction for SQL/Polars generation.
8. `evolution`: record lessons, rejected mappings, useful metadata sources, and future optimizer
   improvements after each run.

KPI term resolution order:

```text
KPI registry
  -> data model docs/diagrams
  -> dataset schema/profile evidence
  -> data dictionary or metadata files
  -> catalog metadata if connected
  -> stakeholder/user clarification
```

If a data dictionary, metadata export, catalog path, SLA file, or contract file is required but
missing, ask the user for that file or location and save the request in `interns/reports/open_questions.md`.

## Metadata And Profiling

Do not create a separate ad hoc metadata extractor unless the existing profiler cannot support the
requirement.

First inspect and reuse or extend:

```text
core/profiling/data_model_profiler.py
```

The profiler should be the canonical path for:

- file metadata
- schemas
- column types
- sample stats
- min/max bounds
- exact bounds when allowed
- downcast evidence
- table/column availability evidence

Write profiling outputs under:

```text
workspaces/<project>/interns/generated/profiles/
```

## Query Implementation Rules

- Prefer SQL for SQL engines and warehouse execution.
- Prefer Polars for local profiling, metadata, and file preparation.
- Use hybrid SQL + Polars only when the task benefits from both.
- Do not hard-code absolute paths.
- Derive paths from the workspace root or script location.
- Keep generated query files under `interns/generated/solutions/`.
- Keep execution databases under `interns/state/`.
- Keep human-readable explanations under `interns/reports/`.

## Ambiguity Handling

KPI registries are often incomplete. When a KPI term cannot be mapped cleanly:

- record the assumption,
- cite the source field or missing field,
- implement the safest executable approximation if useful,
- mark the KPI as `needs_review` when correctness depends on a business decision,
- ask one targeted question only if continuing would waste significant work.

Save assumptions and gaps under:

```text
workspaces/<project>/interns/generated/contracts/
workspaces/<project>/interns/reports/open_questions.md
```

## Baseline And Optimization Evidence

Every optimization run should record:

- baseline query path
- candidate query path
- runtime
- correctness result
- changed pattern, such as predicate pushdown, column pruning, join rewrite, aggregation rewrite,
  type/downcast change, caching/materialization, or expression simplification
- why the change should help
- whether the improvement is accepted, rejected, or needs review

Save evidence under:

```text
workspaces/<project>/interns/generated/evidence/
workspaces/<project>/interns/generated/memory/
```

## Never Do

- Do not modify raw data, KPI registry, or data model files unless explicitly requested.
- Do not place generated artifacts in the workspace root.
- Do not use pandas for normal dataframe work.
- Do not treat stale `config/tasks.json` paths as authoritative when the workspace layout disagrees.
- Do not optimize before establishing a baseline.
- Do not accept faster output when correctness or semantic contract preservation is unclear.
- Do not stage or push `workspaces/<project>/interns/`, raw datasets, databases, logs, or secrets.
