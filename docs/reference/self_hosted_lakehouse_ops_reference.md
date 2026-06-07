# Self-Hosted Lakehouse — Operations & Configuration Reference

> Architecture overview, layer descriptions, and tool-stack rationale live in
> `data_workflow_medallion_reference.md`.
> Databricks/Unity Catalog production practices live in `databricks_production_practices.md`.
> This file covers what those two do NOT: self-hosted infrastructure, configs, templates,
> sizing, and anti-patterns for a MinIO + Spark + Airflow + Trino stack.

---

## Storage: MinIO

### Bucket Layout

```
s3a://datalake/
  ├── bronze/
  │     ├── source_system_a/
  │     │     └── entity_name/
  │     │           └── ingest_date=2024-01-15/
  │     └── source_system_b/
  ├── silver/
  │     └── domain/
  │           └── table_name/
  └── gold/
        └── mart_name/
```

Partition Bronze by `ingest_date` (not event date) — enables reliable incremental loads and
safe reprocessing without touching future partitions.

### MinIO Operational Rules

- Enable bucket versioning on Bronze for point-in-time recovery.
- Lifecycle policies: Bronze archived after 90 days, Silver after 1 year, Gold never auto-deleted.
- Separate MinIO tenants per environment: `dev`, `staging`, `prod`.
- Use erasure coding (minimum 4-node deployment) for production durability.
- Use Hive-style partitioning (`key=value/`) for Spark partition pruning.

### Delta Lake Maintenance (schedule these)

```sql
-- After every Silver/Gold incremental write
OPTIMIZE delta.`s3a://datalake/silver/domain/table_name`
  ZORDER BY (partition_key, sort_key);

-- Weekly — remove old versions
VACUUM delta.`s3a://datalake/silver/domain/table_name` RETAIN 168 HOURS;
```

Target file sizes of 128 MB – 512 MB after compaction. Alert on > 1,000 small files per partition.

---

## Processing: Spark Configuration Baseline

### spark-defaults.conf

```properties
spark.sql.extensions=io.delta.sql.DeltaSparkSessionExtension
spark.sql.catalog.spark_catalog=org.apache.spark.sql.delta.catalog.DeltaCatalog

# Memory
spark.executor.memory=8g
spark.executor.memoryOverhead=2g
spark.driver.memory=4g

# Parallelism — tune to: input_data_gb * 2
spark.sql.shuffle.partitions=200
spark.default.parallelism=200

# Adaptive Query Execution (Spark 3.x)
spark.sql.adaptive.enabled=true
spark.sql.adaptive.coalescePartitions.enabled=true
spark.sql.adaptive.skewJoin.enabled=true

# Delta auto-optimisation
spark.databricks.delta.optimizeWrite.enabled=true
spark.databricks.delta.autoCompact.enabled=true
```

### Partitioning Rules

- Partition Silver by low-cardinality column (`event_date`, `region`). Never by `user_id` or claim ID.
- Target 1–5 GB uncompressed per partition for optimal Spark task sizing.
- Alert if partition count > 10,000 for a single table.

### Broadcast Joins

```python
from pyspark.sql.functions import broadcast

result = fact_df.join(broadcast(dim_df), "key")
# Force broadcast for dimension tables < 100 MB
```

### Schema Definition (never inferSchema in production)

```python
from pyspark.sql.types import StructType, StructField, StringType, LongType

schema = StructType([
    StructField("order_id", LongType(), nullable=False),
    StructField("customer_id", LongType(), nullable=True),
    StructField("status", StringType(), nullable=True),
])

df = spark.read.schema(schema).parquet("s3a://datalake/bronze/orders/")
```

### Provenance Columns (add at Bronze ingest)

```python
from pyspark.sql import functions as F

df = (
    df
    .withColumn("_ingest_timestamp", F.current_timestamp())
    .withColumn("_source_file", F.input_file_name())
    .withColumn("_batch_id", F.lit(batch_id))
)
```

### PySpark Job Template

```python
"""
job_name: silver_orders_transform
layer: silver
schedule: daily @ 02:00 UTC
"""
import sys
from pyspark.sql import SparkSession
from pyspark.sql import functions as F
from delta.tables import DeltaTable


def build_spark_session(app_name: str) -> SparkSession:
    return (
        SparkSession.builder
        .appName(app_name)
        .config("spark.sql.extensions", "io.delta.sql.DeltaSparkSessionExtension")
        .config("spark.sql.catalog.spark_catalog",
                "org.apache.spark.sql.delta.catalog.DeltaCatalog")
        .getOrCreate()
    )


def read_bronze(spark: SparkSession, path: str, watermark_date: str):
    return (
        spark.read.format("delta").load(path)
        .filter(F.col("_ingest_date") >= watermark_date)
    )


def transform(df):
    return (
        df
        .filter(F.col("order_id").isNotNull())
        .withColumn("order_date", F.to_date("order_timestamp"))
        .withColumn("is_cancelled", F.col("status") == "CANCELLED")
        .drop("_source_file", "_batch_id")
    )


def write_silver(df, path: str):
    (
        df.write
        .format("delta")
        .mode("append")
        .partitionBy("order_date")
        .option("mergeSchema", "false")
        .save(path)
    )


if __name__ == "__main__":
    watermark_date = sys.argv[1]   # passed by orchestrator — never call datetime.now() here
    spark = build_spark_session("silver_orders_transform")
    bronze_df = read_bronze(spark, "s3a://datalake/bronze/orders/", watermark_date)
    silver_df = transform(bronze_df)
    write_silver(silver_df, "s3a://datalake/silver/orders/")
    spark.stop()
```

### Delta Merge (incremental Silver upsert)

```python
from delta.tables import DeltaTable

silver_table = DeltaTable.forPath(spark, "s3a://datalake/silver/orders")

silver_table.alias("target").merge(
    source=incremental_df.alias("source"),
    condition="target.order_id = source.order_id"
).whenMatchedUpdateAll(
    condition="source.updated_at > target.updated_at"
).whenNotMatchedInsertAll() \
 .execute()
```

---

## Orchestration: Airflow DAG Template

```python
from airflow import DAG
from airflow.providers.apache.spark.operators.spark_submit import SparkSubmitOperator
from airflow.operators.python import PythonOperator
from datetime import datetime, timedelta

DEFAULT_ARGS = {
    "owner": "data-engineering",
    "depends_on_past": True,        # required for watermark-based pipelines
    "retries": 2,
    "retry_delay": timedelta(minutes=10),
    "email_on_failure": True,
    "email": ["data-alerts@yourorg.com"],
}

with DAG(
    dag_id="silver_orders_daily",
    default_args=DEFAULT_ARGS,
    schedule_interval="0 2 * * *",
    start_date=datetime(2024, 1, 1),
    catchup=True,
    tags=["silver", "orders"],
    max_active_runs=1,              # prevents concurrent runs on same partition
) as dag:

    ingest_bronze = SparkSubmitOperator(
        task_id="ingest_bronze_orders",
        application="jobs/ingest_bronze_orders.py",
        application_args=["{{ ds }}"],   # logical date as watermark
        conn_id="spark_default",
        executor_memory="4g",
        num_executors=4,
    )

    transform_silver = SparkSubmitOperator(
        task_id="transform_silver_orders",
        application="jobs/silver_orders_transform.py",
        application_args=["{{ ds }}"],
        conn_id="spark_default",
        executor_memory="8g",
        num_executors=8,
    )

    run_dq_checks = PythonOperator(
        task_id="data_quality_checks",
        python_callable=run_great_expectations,
        op_kwargs={"suite": "silver_orders", "batch_date": "{{ ds }}"},
    )

    ingest_bronze >> transform_silver >> run_dq_checks
```

**Key DAG rules:**
- `depends_on_past=True` + `max_active_runs=1` — mandatory for watermark pipelines; prevents parallel backfill corruption.
- Pass logical date (`{{ ds }}`), never wall-clock time, to all Spark jobs.
- DAGs declare dependencies only — no business logic. Logic lives in PySpark jobs.
- Use `sla=timedelta(hours=3)` on critical tasks to alert before downstream consumers are impacted.

---

## Serving: Trino Configuration

```properties
# catalog/delta.properties
connector.name=delta_lake
hive.metastore.uri=thrift://hive-metastore:9083
delta.metadata.cache-ttl=10m
```

- Use Trino for interactive / BI queries; Spark for heavy transforms and large shuffles.
- Gold tables should be query-ready without joins — denormalise at Gold so BI tools don't pay join cost.
- Pre-aggregate common metrics (`gold.daily_orders_by_region`) refreshed once per day.
- Set Trino resource groups to prevent one heavy query exhausting cluster resources.

Metastore wiring:
```
Spark  → Hive Metastore (PostgreSQL) → Delta Lake tables on MinIO
Trino  → Hive Metastore              → same tables (shared catalog)
```

---

## Data Quality: Great Expectations Integration

Integrate GX as a mandatory step after every Silver and Gold write. **Fail-fast: a DQ failure must fail the Airflow task and halt the DAG. Never write bad data silently.**

```python
import great_expectations as gx

context = gx.get_context()
batch = context.sources.pandas_default.read_dataframe(silver_df.toPandas())

batch.expect_column_values_to_not_be_null("order_id")
batch.expect_column_values_to_be_between("order_amount", min_value=0, max_value=1_000_000)
batch.expect_column_values_to_be_in_set("status", ["PLACED", "SHIPPED", "DELIVERED", "CANCELLED"])

results = batch.validate()
if not results["success"]:
    raise ValueError(f"Data quality check failed: {results}")
```

---

## Infrastructure: Kubernetes Reference

```
┌──────────────────────────────────────────────────────┐
│                  Kubernetes Cluster                   │
│                                                       │
│  ┌──────────────┐  ┌────────────────────────────────┐ │
│  │   Airflow    │  │      Spark on K8s              │ │
│  │  (scheduler  │  │  (ephemeral executor pods)     │ │
│  │  + workers)  │  └────────────────────────────────┘ │
│  └──────────────┘                                     │
│  ┌──────────────┐  ┌──────────────┐                   │
│  │    Trino     │  │    Hive      │                   │
│  │  (workers)   │  │  Metastore   │                   │
│  └──────────────┘  └──────────────┘                   │
│  ┌──────────────┐  ┌──────────────┐                   │
│  │  Prometheus  │  │   Grafana    │                   │
│  └──────────────┘  └──────────────┘                   │
└──────────────────────────────────────────────────────┘
          │                          │
          ▼                          ▼
   MinIO Cluster                PostgreSQL
  (object storage)            (HMS backend,
                               Airflow DB)
```

### Hardware Sizing

| Role | CPU | RAM | Storage | Notes |
|------|-----|-----|---------|-------|
| Spark executor | 4–8 cores | 16–32 GB | Ephemeral | Scale horizontally |
| Spark driver | 4 cores | 8–16 GB | — | 1 per job |
| Airflow scheduler | 2 cores | 4 GB | — | 1 instance (HA: 2) |
| Trino coordinator | 8 cores | 32 GB | — | Single instance |
| Trino worker | 8–16 cores | 64–128 GB | — | Scale for concurrency |
| MinIO node | 4 cores | 16 GB | 4–12 TB NVMe | Min. 4 nodes for erasure |
| HMS + PostgreSQL | 4 cores | 8 GB | 500 GB SSD | |

### IaC Rules

- Terraform (or Pulumi) for all infrastructure. No manual cluster setup.
- Pin Helm chart versions in `Chart.lock`.
- Separate K8s namespaces: `data-dev`, `data-staging`, `data-prod`.
- GitOps for DAGs: sync from Git via Airflow's Git-sync sidecar.
- Single base Spark Docker image with all dependencies. Ban per-job `pip install` at runtime. Pin all library versions.

---

## Observability: Key Metrics

**Pipeline health:**
- Job duration (p50/p95/p99) — alert on > 2× historical baseline
- Job success rate — alert on < 99% over 7-day rolling window
- Rows written per batch — alert on > 30% deviation from rolling average

**Spark performance:**
- Executor memory utilisation — target < 80%
- GC time as % of task time — target < 5%
- Shuffle read/write bytes — high values indicate missing broadcast hints or poor partitioning
- Task skew ratio — alert if max task time > 3× median task time

**Storage health:**
- Delta file count per partition — alert on > 1,000 small files
- Vacuum lag (versions retained > policy)

---

## The 12 Rules of Batch Pipeline Design

1. **Immutability.** Bronze is append-only. Never update or delete raw records.
2. **Idempotency.** Same input must produce same output. Use partition overwrite or merge with deterministic keys.
3. **Explicit schemas.** Define schemas in code. Never `inferSchema=True` in production.
4. **Watermark-based processing.** Process by `ingest_date`, not wall-clock time. Pass date as a parameter.
5. **Fail loud.** On DQ failure, fail the task. Never silently skip or warn.
6. **Thin DAGs.** DAGs wire tasks. Business logic lives in Spark jobs, tested independently.
7. **Small file management.** Run `OPTIMIZE` after every incremental write. Automate `VACUUM` weekly.
8. **Partitioning discipline.** Partition by columns in WHERE filters. Never over-partition (> 10k partitions is a warning sign).
9. **No magic timestamps.** Use logical date from the orchestrator. Never call `datetime.now()` inside a Spark job.
10. **Test your transformations.** Unit-test transform functions with `pytest` + small in-memory Spark sessions.
11. **Lineage first.** Document lineage in the catalog before merging a new pipeline. No undocumented datasets in production.
12. **Separate environments strictly.** Dev reads from dev MinIO. Prod never uses dev credentials or buckets.

---

## Anti-Patterns

| Anti-Pattern | Problem | Fix |
|---|---|---|
| Writing directly to Gold without Silver | No intermediate checkpoint; hard to debug | Always go Bronze → Silver → Gold |
| Using `current_timestamp()` as watermark | Non-deterministic; breaks reruns | Pass watermark from orchestrator |
| Inferring schema in production | Inconsistent types; extra scan pass | Define schemas explicitly |
| Over-partitioning by `user_id` | Millions of small files; metastore overload | Partition by date or low-cardinality column |
| No `VACUUM` schedule | Unbounded storage growth | Schedule weekly VACUUM with 7-day retention |
| `.collect()` for validation | OOM on large datasets | Use DQ framework on distributed data |
| `depends_on_past=False` on watermark jobs | Parallel backfill corrupts partitions | Set `depends_on_past=True` + `max_active_runs=1` |
| Hardcoded credentials in Spark jobs | Security risk | Use Vault, Kubernetes secrets, or IAM roles |
| Per-job `pip install` at runtime | Version drift, slow startup | Pin all deps in base Docker image |
