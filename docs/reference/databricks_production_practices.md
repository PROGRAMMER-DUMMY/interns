# Databricks Production Practices

Version: 2026-05-25 research refresh

This reference is the project standard for Databricks-backed data engineering work. It replaces
generic Databricks advice with current, source-backed practices that fit this repo's governed
workflow: local-safe planning first, Unity Catalog as the production control plane, Databricks as
the production evidence plane, and no remote mutation without explicit approval.

## Source Policy

Use these sources first when Databricks behavior or syntax matters:

| Area | Primary Source |
|---|---|
| Delta Lake table behavior and maintenance | Databricks Delta best practices: https://docs.databricks.com/gcp/delta/best-practices |
| Delta OSS internals and optimization semantics | Delta Lake docs: https://docs.delta.io/optimizations-oss.html |
| Lakeflow Spark Declarative Pipelines | Databricks Lakeflow docs: https://docs.databricks.com/aws/en/delta-live-tables/ |
| DLT rename and Python API migration | https://docs.databricks.com/gcp/en/ldp/where-is-dlt |
| Auto Loader | https://docs.databricks.com/aws/en/ingestion/cloud-object-storage/auto-loader/ |
| Auto Loader file notification mode | https://docs.databricks.com/aws/en/ingestion/cloud-object-storage/auto-loader/file-notification-mode |
| Unity Catalog governance | https://docs.databricks.com/aws/en/data-governance/unity-catalog/best-practices |
| Unity Catalog lineage | https://docs.databricks.com/en/data-governance/unity-catalog/data-lineage.html |
| System tables | https://docs.databricks.com/aws/en/admin/system-tables/ |
| SQL warehouse sizing | https://docs.databricks.com/aws/en/compute/sql-warehouse/warehouse-behavior |
| Serverless compute | https://docs.databricks.com/en/compute/serverless/best-practices.html |
| Structured Streaming production | https://docs.databricks.com/aws/en/structured-streaming/production |
| Structured Streaming checkpoints | https://docs.databricks.com/aws/en/structured-streaming/checkpoints |
| Cost optimization | https://docs.databricks.com/aws/en/lakehouse-architecture/cost-optimization/best-practices |
| Declarative Automation Bundles and CI/CD | https://docs.databricks.com/gcp/en/dev-tools/ci-cd/best-practices |
| Compute policies | https://docs.databricks.com/en/admin/clusters/policies.html |
| Service principals | https://docs.databricks.com/aws/en/security/auth/access-control/service-principal-acl |
| Secrets | https://docs.databricks.com/gcp/en/security/secrets/index.html |
| DBFS and Unity Catalog | https://docs.databricks.com/aws/en/dbfs/unity-catalog |

Use Databricks blogs, Delta Lake blogs, and community posts as design context, not as final syntax
authority. Product names, SQL syntax, and managed-service behavior must be checked against docs.

## Project Operating Position

Databricks is the production execution and governance target for enterprise work. Local DuckDB,
Polars, and generated SQL are smoke-test and planning surfaces. Production claims need Databricks
evidence: Unity Catalog object references, job or pipeline run evidence, system-table observability,
lineage where available, and cost/performance data.

Remote execution is never implied by credentials. In this repo, remote Databricks execution or
mutation requires explicit approval through the established environment gate:

```powershell
$env:AUTORESEARCH_ALLOW_REMOTE_EXECUTION = "1"
```

Before generating or deploying Databricks artifacts, use the repo workflow:

```powershell
uv run build-relationship-contracts --workspace workspaces/<project>
uv run plan-source-to-target --workspace workspaces/<project> --target-engine sql
uv run prepare-data-engineering-route --workspace workspaces/<project> --track auto --target-engine sql
uv run prepare-pipeline-plan --workspace workspaces/<project> --track auto --target-engine sql --table-format auto
uv run generate-pipeline-sql --workspace workspaces/<project>
uv run run-pipeline-execution-harness --workspace workspaces/<project>
uv run run-data-quality-harness --workspace workspaces/<project>
uv run validate-workspace-artifacts --workspace workspaces/<project>
```

Do not generate executable Databricks SQL, Polars, PySpark, or medallion code from column-name
similarity alone. Source tables, joins, grain, temporal anchors, and layer contracts must be proven
from KPI requirements, data model evidence, profiles, catalog metadata, or accepted user decisions.

## Architecture Defaults

Default stance:

- Use Unity Catalog managed tables for curated production data unless an external table is required
  by ownership, data-sharing, or source-system constraints.
- Use Unity Catalog external locations or volumes for landing and staging areas. Avoid DBFS root and
  DBFS mounts in Unity Catalog workspaces.
- Use Delta Lake for production lakehouse tables. Avoid plain Parquet tables for governed mutable
  datasets unless there is a specific interoperability reason.
- Prefer Lakeflow Spark Declarative Pipelines for canonical managed batch/streaming pipelines that
  need expectations, dependency management, and operational visibility.
- Prefer Lakeflow Jobs for production orchestration when the workflow is Databricks-native.
- Prefer serverless SQL warehouses for BI and interactive SQL when available, with system-table
  monitoring and query-profile review.
- Prefer Declarative Automation Bundles for CI/CD-managed jobs, pipelines, warehouses, permissions,
  and environment parameters.

When these defaults do not fit, record the exception in the workspace's generated memory or
deployment plan with the reason, risk, owner, and rollback path.

## Delta Lake

Treat `_delta_log/` as the table source of truth. Never manually add, rename, or delete data files
under a Delta table path. Any write must go through the Delta protocol.

Current production defaults:

- Use `CREATE OR REPLACE TABLE` instead of dropping and recreating a table in the same location.
- Use Unity Catalog managed tables when possible.
- Use predictive optimization for Unity Catalog managed tables where the account, catalog, or schema
  policy allows it.
- Use liquid clustering for most new large tables instead of designing complex static partition and
  Z-order strategies up front.
- Remove old explicit Delta tuning properties during runtime upgrades when Databricks docs say the
  legacy setting can block newer optimizations.
- Use `VACUUM` only with reviewed retention. Do not run low-retention vacuum in production without a
  maintenance window and time-travel impact review.

Current predictive optimization syntax:

```sql
ALTER CATALOG prod_catalog ENABLE PREDICTIVE OPTIMIZATION;
ALTER SCHEMA prod_catalog.silver ENABLE PREDICTIVE OPTIMIZATION;
ALTER SCHEMA prod_catalog.silver INHERIT PREDICTIVE OPTIMIZATION;
```

Do not use outdated DB properties such as `predictive_optimization = enable` as the project
standard.

Partitioning rule:

- For small and medium tables, avoid partitioning until query patterns prove the need.
- For very large append-heavy tables, partition only on low-to-medium cardinality columns that are
  stable and frequently filtered.
- Do not partition by high-cardinality identifiers such as user, claim, event, order, or encounter
  IDs.
- Use liquid clustering or Databricks layout recommendations before hand-designing many partitions.

## Medallion Layers

Bronze is the replay layer, not the business truth layer.

Bronze standards:

- Preserve source payloads as faithfully as possible.
- Add ingestion metadata: source system, source file/object, ingestion timestamp, pipeline run ID,
  and source batch/checkpoint metadata.
- Allow schema evolution only where the drift is expected and captured.
- Do not permanently discard records in Bronze unless policy requires quarantine before storage.

Silver is the conformance layer.

Silver standards:

- Enforce declared schema, types, keys, grain, and data-quality expectations.
- Deduplicate only with a proven key or an accepted business rule.
- Keep rejected or ambiguous rows traceable through quarantine, rescue, or issue tables.
- Use approved relationship contracts before multi-source joins.

Gold is the consumption layer.

Gold standards:

- Optimize around business access patterns, not raw source convenience.
- Keep tables narrow where possible.
- Materialize stable aggregates and semantic outputs used by BI, APIs, KPI proof, and ML features.
- Store metric definitions, filters, grain, and temporal anchors in reviewed artifacts.

## Lakeflow Spark Declarative Pipelines

Delta Live Tables was renamed to Lakeflow Spark Declarative Pipelines. Existing `import dlt` code
still works, but new documentation recommends the newer pipeline API names where available:

```python
from pyspark import pipelines as dp
```

Use Lakeflow Spark Declarative Pipelines when:

- The pipeline has multiple dependent tables or materialized views.
- Batch and streaming flows should be managed as one graph.
- Data-quality expectations need first-class visibility.
- The domain owns a canonical Bronze to Silver to Gold pipeline.

Use Lakeflow Jobs or plain package code when:

- The task is a one-off migration.
- The task needs special Spark settings per step.
- The DAG has complex external branching or cross-system orchestration.
- The code needs conventional Python package testing and simple job execution.

Expectation strategy:

| Mode | Use For |
|---|---|
| Warn | New or uncertain rules that need measurement before enforcement |
| Drop | Known bad records that should be excluded but counted and reviewed |
| Fail | Critical invariants where partial output is worse than no output |

Never hide dropped rows. Dropped, rescued, or quarantined rows need count evidence and review paths.

## Auto Loader

Use Auto Loader for incremental cloud file ingestion. Avoid production ingestion with recursive
`spark.read` globs over large object-store folders.

Current preference:

- For Unity Catalog external locations, use managed file events where supported.
- For classic notification mode, use `cloudFiles.useNotifications` only when the queue/subscription
  model is intentionally managed.
- Design ingestion for out-of-order file discovery. File notification mode improves scale but does
  not guarantee processing order.
- Use a schema location in governed storage.
- Capture rescued data and monitor rescue rate.
- Run regular completeness checks or managed backfill behavior where the SLA requires no missed
  files.

Representative options:

```python
df = (
    spark.readStream
    .format("cloudFiles")
    .option("cloudFiles.format", "json")
    .option("cloudFiles.useManagedFileEvents", "true")
    .option("cloudFiles.schemaEvolutionMode", "addNewColumns")
    .option("cloudFiles.schemaLocation", schema_location)
    .option("cloudFiles.rescuedDataColumn", "_rescued_data")
    .load(raw_path)
)
```

Treat classic queue options such as `cloudFiles.queueName` and `cloudFiles.useNotifications` as
cloud- and mode-specific, not universal boilerplate.

## Structured Streaming

Production streaming rules:

- Do not run production streams on all-purpose compute.
- Use jobs compute or Lakeflow Spark Declarative Pipelines.
- Always set a durable checkpoint location.
- Use one checkpoint per stream output. Never share checkpoint directories.
- Do not delete checkpoints unless a full replay is intended and approved.
- Prefer `Trigger.AvailableNow()` for scheduled incremental workloads.
- Be careful with autoscaling for stateful streaming jobs; Databricks docs recommend disabling
  autoscaling for Structured Streaming jobs.
- Apply watermarks deliberately for stateful aggregations, stream-stream joins, and deduplication.

Checkpoint paths should include pipeline, table, and compatible schema/state version:

```text
/Volumes/<catalog>/<schema>/<volume>/checkpoints/<pipeline>/<table>/v<state_version>
```

Changing stateful query shape, state schema, or checkpoint path can cause replay, duplicates, or
restart failure. Treat checkpoint changes as migrations.

## SQL Warehouses

Use SQL warehouses for BI, ad hoc SQL, Databricks SQL dashboards, and serving read-heavy Gold tables.
Databricks recommends serverless SQL warehouses for most workloads when available.

Operational rules:

- Size by concurrency and query complexity, not by guesswork.
- Use query profile before scaling up.
- Watch bytes scanned, spill to disk, queue time, compilation time, and skew.
- Enable auto-stop for non-always-on warehouses.
- Use system tables to review query history and usage trends.
- Keep BI-facing tables physically optimized for actual filters and joins.

Useful system tables:

```sql
SELECT *
FROM system.query.history
WHERE start_time >= current_timestamp() - INTERVAL 1 DAY;

SELECT *
FROM system.billing.usage
WHERE usage_date >= current_date() - INTERVAL 30 DAY;
```

Do not treat SQL warehouse size tables from articles as universal. Sizing changes with serverless
availability, warehouse type, concurrency, query shape, cloud, and account limits.

## Unity Catalog Governance

Unity Catalog is the production governance boundary.

Standards:

- Provision users, groups, and service principals at the account level through the identity
  provider.
- Use groups for most grants. Avoid direct user grants unless there is a reviewed exception.
- Use service principals for production jobs and automation.
- Prefer catalog-level managed storage as the isolation boundary.
- Avoid external clients directly accessing managed table storage.
- Register landing and staging paths as external locations or volumes.
- Do not use DBFS mounts for new Unity Catalog workloads.
- Tag sensitive tables and columns.
- Use row filters, column masks, and least-privilege grants for regulated data.

Lineage expectations:

- Use UC-registered tables and Databricks SQL/DataFrame APIs to maximize lineage capture.
- Expect lineage gaps for operations outside Databricks or outside supported paths.
- Use lineage system tables and Catalog Explorer for impact analysis before schema migrations.

## Security

Secrets:

- Never store credentials in notebooks, source files, job JSON, bundle YAML, or environment dumps.
- Use Databricks secret scopes or cloud-native secret managers integrated through approved patterns.
- For Databricks Apps, avoid exposing raw secret values in environment variables; use `valueFrom`
  patterns where applicable.
- Rotate credentials when ownership changes.

Identity:

- Production jobs run as service principals, not human users.
- CI/CD should use workload identity federation where supported instead of long-lived tokens.
- Grant minimum privileges needed for each job, warehouse, app, or pipeline.

Network:

- Use customer-managed VPC/VNet, Private Link/private endpoints, and egress restrictions for
  regulated workloads.
- Restrict direct cloud-storage access that bypasses Unity Catalog.
- Log administrative and user actions through audit/system tables.

## Cost Engineering

Avoid hard-coded DBU price examples in project docs. Use account-specific pricing, cloud bills, and
`system.billing.usage`.

Cost rules:

- Use job compute or serverless jobs for production batch work.
- Limit all-purpose clusters to development and interactive exploration.
- Enforce compute policies for auto-termination, allowed node families, runtime versions, library
  installation policy, and maximum cluster size.
- Use serverless SQL warehouses with Intelligent Workload Management where available.
- Use auto-stop on SQL warehouses that do not need to stay warm.
- Use spot/preemptible workers only when the workload can tolerate loss or fallback behavior.
- Keep drivers reliable; losing the driver usually kills the job.
- Monitor DBU and cloud compute separately.
- Optimize data layout to reduce runtime before increasing cluster size.

Use cost evidence, not anecdotes:

```sql
SELECT
  usage_date,
  usage_metadata.job_id,
  usage_metadata.warehouse_id,
  SUM(usage_quantity) AS usage_quantity
FROM system.billing.usage
WHERE usage_date >= current_date() - INTERVAL 30 DAY
GROUP BY usage_date, usage_metadata.job_id, usage_metadata.warehouse_id
ORDER BY usage_date DESC;
```

## CI/CD

Current Databricks docs call bundles Declarative Automation Bundles, formerly Databricks Asset
Bundles. Use bundles as the default production deployment unit.

CI/CD standards:

- Keep dev, staging, and prod isolated by workspace and/or catalog.
- Store code and bundle definitions in version control.
- Package production Python as wheels where practical.
- Run unit tests before deployment.
- Run `databricks bundle validate` before `databricks bundle deploy`.
- Parameterize environment-specific values such as catalog, schema, warehouse, cluster policy, and
  service principal.
- Deploy to staging before production.
- Require PR review for production changes.
- Do not depend on manual UI clicks for production definitions.

Minimal bundle flow:

```bash
databricks bundle validate --target staging
databricks bundle deploy --target staging
databricks bundle run <job_or_pipeline> --target staging
databricks bundle deploy --target prod
```

## Observability

Production readiness requires evidence.

Minimum signals:

- Job or pipeline run state, duration, retries, and failure messages.
- Input and output row counts by layer.
- Data-quality expectation counts, rescued rows, quarantine counts, and duplicate findings.
- Query profile for expensive SQL.
- Streaming progress, input rows/sec, processed rows/sec, state size, and lag.
- System-table billing and query history.
- Unity Catalog lineage for critical tables.
- Deployment version, Git commit, bundle target, and service principal.

Use system tables as the account-level operational store when available. For workspace-specific
governed work in this repo, also write proof under `workspaces/<project>/interns/generated/` and
`workspaces/<project>/interns/reports/`.

## Production Checklist

Data and governance:

- [ ] Tables are registered in Unity Catalog.
- [ ] Managed tables are used unless an external-table reason is recorded.
- [ ] External locations or volumes are used instead of DBFS mounts.
- [ ] Sensitive columns are tagged and protected by masks, filters, or grants.
- [ ] Source-to-target plan proves source tables, joins, grain, and temporal anchors.
- [ ] Relationship contracts allow executable use.

Pipeline:

- [ ] Bronze preserves source data and ingestion metadata.
- [ ] Silver enforces schema, grain, deduplication rules, and quality checks.
- [ ] Gold tables match business consumption patterns.
- [ ] Auto Loader uses managed file events or a reviewed classic notification setup.
- [ ] Streaming checkpoints are durable, unique, and versioned.
- [ ] Data-quality, pipeline-execution, and workspace validators pass.

Performance and cost:

- [ ] Query profile has been reviewed for large SQL workloads.
- [ ] Layout strategy uses predictive optimization, liquid clustering, or a reviewed alternative.
- [ ] SQL warehouse and job compute sizes are justified by evidence.
- [ ] Auto-stop and compute policies are enforced.
- [ ] Billing/system-table queries are available for cost review.

Security and deployment:

- [ ] Jobs run as service principals.
- [ ] Secrets are in secret scopes or approved cloud secret managers.
- [ ] CI/CD uses bundles, validation, staged deployment, and PR review.
- [ ] Remote mutation approval is recorded before any Databricks apply step.
- [ ] Rollback or restore path is documented.

## Articles Worth Reading, With Caveats

Useful context sources:

- Delta Lake liquid clustering blog: https://delta.io/blog/liquid-clustering/
- Delta Lake Z-order blog: https://delta.io/blog/2023-06-03-delta-lake-z-order/
- Databricks Lakeflow and Data + AI Summit material:
  https://www.databricks.com/dataaisummit/session/getting-most-out-spark-declarative-pipelines-deep-dive-whats-new-and

Caveat: blogs explain design intent and tradeoffs, but docs are the authority for current syntax,
support status, limits, and defaults.
