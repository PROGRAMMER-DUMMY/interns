# 00 — Overview

## Problem

The autoresearch platform onboards a workspace, profiles datasets, resolves KPI feature mappings, and emits KPI SQL. Today that KPI SQL runs **directly against raw unioned CSVs**. There is no transformation layer between source files and KPI answers. This causes three concrete failures for internal ETL/ELT teams:

1. **Silent grain errors.** Unioning `hospital_a/patients.csv` and `hospital_b/patients.csv` without conformed dimensions duplicates KPI rows. Numbers look right; they aren't.
2. **No reproducibility.** Re-derivation re-reads source files and re-applies cleaning inline. Two runs against drifted source data produce different numbers with no audit trail.
3. **Compliance gap.** PII is masked at SQL-gen time only. Data at rest is unprotected. In a HIPAA workspace this is a release blocker.

## Solution

A new agent — the **Medallion Architect** — that proposes a layered pipeline (Bronze → Silver → Gold) for any onboarded workspace, surfaces grain and SCD decisions through the existing blocker question panel for human ratification, and emits idempotent, portable, auditable artifacts that the existing `ExecutionBackend` can run on either DuckDB (local) or Delta/Spark (Databricks).

The agent is a *designer*, not an executor. Execution stays with `ExecutionBackend`. The agent's outputs are:

- A declarative `manifest.yaml` describing every Bronze, Silver, and Gold table.
- A `star_schema.json` with grain, dimensions, conformed dims, relationships.
- A `silver_contract.json` with type casts, null policies, PII columns, derived columns, post-load assertions.
- A `lineage.json` graph showing how every Gold column traces back to source CSVs.
- Per-target SQL/PySpark files (`*.duckdb.sql`, `*.spark.py`) that the existing `ExecutionBackend` runs.
- Paired Markdown files for each contract, regenerated from the JSON, suitable for PR review.

## Success criteria

The feature ships when, on the Healthcare-RCM-Data-Platform workspace:

1. `uv run design-medallion --workspace workspaces/Healthcare-RCM-Data-Platform` produces a complete `medallion/` directory and surfaces star-schema decisions through the blocker panel.
2. After human ratification, `uv run build-medallion` runs Bronze → Silver → Gold → KPI regeneration end-to-end on both `target=duckdb` (local) and `target=delta` (Databricks Jobs backend) without code changes.
3. `kpi_metrics_v2.sql` (regenerated against Gold) produces row-equal output to legacy `kpi_metrics.sql` for unchanged KPIs; changes are surfaced as a blocker, not silently accepted.
4. `validate-workspace-artifacts` passes all eight medallion checks.
5. A deliberately introduced PII column (unmarked in `semantic_contract.json`) is caught by the blocker panel, not silently leaked into Silver.
6. A deliberate Databricks-cluster failure under permissive mode produces `degraded_run: true` and a DuckDB-substrate Gold; under strict mode it halts.
7. Total LLM USD spend for a full first-run on Healthcare RCM stays under the configured `max_usd_per_run` cap; second run with no input changes is ~100% cache hits.
8. `lineage.md` renders a column-level path from any Gold column back to its source CSV.

## Why this scope (not larger, not smaller)

**Why not bigger** — we are not building Airflow, Dagster, dbt, or a streaming CDC framework. The agent emits declarative artifacts the existing `ExecutionBackend` consumes. Every additional capability beyond "design + materialize" widens the blast radius and competes with mature tools.

**Why not smaller** — a Bronze-only ingestion agent would not move the needle. The bug class we are eliminating (silent grain errors, drift-induced KPI changes, PII at rest) requires all three layers + assertions + lineage. Cutting any of them keeps the failure modes.

## Audience map

| Reader | Read | Skip |
|---|---|---|
| Product / stakeholder | [README.md](README.md), this file | Everything else |
| Reviewing a phase PR | The matching phase doc + [01-architecture.md](01-architecture.md) | Other phase docs |
| Implementing a phase | The matching phase doc + [02-conventions.md](02-conventions.md) + [09-testing.md](09-testing.md) | Phases you are not on |
| Operating after ship | [10-operations.md](10-operations.md) + [02-conventions.md](02-conventions.md) | Phase docs |
| Security review | [06-phase-P3-pii-at-rest.md](06-phase-P3-pii-at-rest.md) + [01-architecture.md](01-architecture.md) §PII | Everything else |
| Cost / FinOps review | [07-phase-P4-dynamic-models.md](07-phase-P4-dynamic-models.md) | Everything else |

## Origin and design history

This feature was designed via the `grill-me` skill across thirteen branches of the design tree, plus five amendments and one model-selection refinement. The full transcript of locked decisions lives in the PRD (`docs/PRD_medallion_architect.md` §5). The "why this and not that" rationale for each decision is preserved in the PRD options tables — if you ever wonder why we chose, e.g., composite natural keys by default, the answer is in PRD §13.
