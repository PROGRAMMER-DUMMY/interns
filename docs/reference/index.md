# Reference — Index

Active, ground-truth reference material for building the platform: data-engineering patterns,
Databricks/lakehouse production practices, and agentic-CLI tooling notes. These are consumed by
agents/code or used as ground truth during work — not per-customer output.

| File | What it is | Use for |
|------|------------|---------|
| `data_workflow_medallion_reference.md` | Medallion (bronze/silver/gold) pipeline reference: ingestion, cleaning, serving, modern tooling | Pipeline architecture; quality-tier and serving patterns |
| `databricks_production_practices.md` | Databricks production patterns: Unity Catalog, Lakeflow, Auto Loader, Delta maintenance, SQL warehouses, CI/CD, security, cost, observability | Databricks/Unity Catalog production work |
| `databricks_token_scopes.md` | Every Databricks OAuth/PAT scope + which the project needs; wired into `tools/databricks_setup.py` | Minimum-scope token generation; per-task scope mapping |
| `self_hosted_lakehouse_ops_reference.md` | Self-hosted lakehouse ops: configs, templates, sizing, anti-patterns for a MinIO + Spark + Airflow + Trino stack | Self-hosted infra ops; grounds the PySpark generator's lakehouse rules |
| `dbt_agentic_cli_reference.md` | How an agentic CLI uses dbt as a tool (not a web-app guide) | dbt-as-a-tool usage from a CLI agent |
| `senior_data_engineer_patterns.md` | Real senior/staff DE day-to-day patterns: on-call triage, slow-pipeline diagnosis, lineage, dimensional modeling vs. OBT, schema drift, cost, Airflow-at-scale, idempotency, requirement changes, cross-team contracts — source-audited with confidence notes | Grounding design decisions in real operational practice, not assumption |

When adding a file here: add one row above, then confirm `docs/README.md` still points at this
folder (it points at the folder, not individual files — keep it that way).
