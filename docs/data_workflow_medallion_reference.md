# Data Workflow And Medallion Architecture

From raw sources to analytical Gold: a team reference for governed data work.

## Table Of Contents

1. [The Team](#the-team)
2. [Data Sources](#data-sources)
3. [Ingestion Methods](#ingestion-methods)
4. [Medallion Architecture Overview](#medallion-architecture-overview)
5. [Bronze Layer: Raw](#bronze-layer-raw)
6. [Silver Layer: Cleaned And Conformed](#silver-layer-cleaned-and-conformed)
7. [Gold Layer: Aggregated And Business-Ready](#gold-layer-aggregated-and-business-ready)
8. [Serving Layer](#serving-layer)
9. [Data Cleaning Techniques](#data-cleaning-techniques)
10. [Data Processing Patterns](#data-processing-patterns)
11. [Cross-Cutting Concerns](#cross-cutting-concerns)
12. [Modern Tool Stack](#modern-tool-stack)

## The Team

| Role | Primary Responsibility |
|---|---|
| Data Engineer | Build and maintain pipelines, ingestion, storage, and orchestration |
| Analytics Engineer | Transform raw data into clean, documented models |
| Data Scientist | Build ML models, run experiments, and consume Silver or Gold layers |
| Data Analyst | Query Gold tables, build dashboards, and answer business questions |
| Data Platform Engineer | Own infrastructure, warehouses, lakes, compute, and access control |

## Data Sources

Structured sources:

- Relational databases: PostgreSQL, MySQL, Oracle, SQL Server
- Cloud databases: RDS, Cloud SQL, Aurora
- Warehouses used as sources: Snowflake, Redshift, Databricks, BigQuery

Semi-structured sources:

- REST APIs: Stripe, Salesforce, Google Analytics, HubSpot
- Webhooks from SaaS systems
- GraphQL APIs

Unstructured and file sources:

- Flat files: CSV, TSV, Excel
- JSON and XML files from object-store drops, SFTP, or email attachments
- Parquet, Avro, and ORC from upstream systems

Streaming sources:

- Kafka topics for application events, clickstreams, and IoT
- AWS Kinesis
- Google Pub/Sub
- Azure Event Hubs

SaaS connectors:

- CRM: Salesforce, HubSpot
- Finance: Stripe, QuickBooks, Xero
- Marketing: Google Ads, Meta Ads, Klaviyo
- Product analytics: Mixpanel, Amplitude, Segment

## Ingestion Methods

Batch ingestion loads data on a schedule.

- Full load: pull the entire table every time; simple but expensive at scale.
- Incremental load: pull only new or changed rows using a watermark such as `updated_at` or an auto-increment ID.
- Snapshot: store a periodic full copy as a partition for point-in-time analysis.

Streaming ingestion continuously lands events as they happen.

- Kafka Connect to object storage, Delta Lake, or a warehouse
- Spark Structured Streaming or Flink
- Kinesis Firehose to object storage

Change Data Capture reads a source database transaction log to capture inserts, updates, and deletes without scanning source tables.

- Common tools: Debezium, AWS DMS, Fivetran log-based CDC
- Value: lower source load, delete capture, and near-real-time replication

ELT loads raw data first, then transforms inside the warehouse or lakehouse. ETL transforms before loading and remains useful when storage, compliance, or legacy systems require it.

## Medallion Architecture Overview

```text
Sources
  |
  v
Ingestion layer
  |
  v
Bronze -> Silver -> Gold
  |
  v
Serving layer
```

The pattern was popularized by Databricks for Delta Lake and is now common across modern warehouses and lakehouses, even when teams use different names for the layers.

## Bronze Layer: Raw

Bronze lands data as close to the source as practical. It is the recovery and replay layer.

Characteristics:

- Source schema is preserved.
- Data is append-only or insert-only.
- History is retained for reprocessing.
- Duplicates, nulls, and bad types can exist.
- Metadata columns such as source system, source file, and ingestion timestamp are useful.

Common formats:

| Format | Best For |
|---|---|
| Parquet | Compressed analytical storage |
| Delta Lake | ACID transactions, time travel, schema enforcement |
| Apache Iceberg | Open table format and multi-engine access |
| ORC | Hive and Spark ecosystems |
| JSON or CSV | Small volumes, debugging, and simple ingestion |

Project-relative example layout:

```text
workspaces/<project>/interns/state/medallion/bronze/<source_system>/<table>/year=2026/month=05/
```

## Silver Layer: Cleaned And Conformed

Silver applies quality rules and makes data joinable across domains.

Characteristics:

- Validated, typed, and deduplicated
- Business rules applied consistently
- Cross-domain joins become reliable
- Slowly Changing Dimensions are handled where required
- PII is masked, hashed, or otherwise protected before broad consumption

Typical Silver checks:

- Primary keys are not null.
- Primary keys are unique at the declared grain.
- Foreign keys are valid or explicitly allowed to be missing.
- Date, currency, country, and code fields are standardized.
- Reusable derived columns are computed once and reused downstream.

## Data Cleaning Techniques

### Deduplication

```sql
WITH deduped AS (
  SELECT
    *,
    ROW_NUMBER() OVER (
      PARTITION BY user_id, event_type, event_ts
      ORDER BY ingested_at DESC
    ) AS rn
  FROM bronze.events
)
SELECT *
FROM deduped
WHERE rn = 1;
```

Useful techniques:

- `ROW_NUMBER()` for exact key deduplication
- Fuzzy matching for names and addresses
- Probabilistic record linkage across sources

### Null Handling

| Strategy | When To Use |
|---|---|
| Drop rows | Null in a critical key |
| Fill with default | Categorical field with a known fallback |
| Fill with aggregate | Numeric field where imputation is approved |
| Carry forward | Time-series gaps |
| Flag and keep | Analytical rows where missingness is informative |

Polars example:

```python
import polars as pl

df = df.with_columns(
    pl.col("revenue").fill_null(pl.col("revenue").median()),
    pl.col("country").fill_null("unknown"),
    pl.col("user_id").is_null().alias("is_null_user_id"),
).filter(pl.col("user_id").is_not_null())
```

### Type Casting And Standardization

```sql
SELECT
  CAST(order_id AS VARCHAR) AS order_id,
  CAST(created_at AS TIMESTAMP) AS created_at,
  UPPER(TRIM(country_code)) AS country_code,
  ROUND(CAST(amount_usd AS DOUBLE), 2) AS amount_usd,
  STRPTIME(date_str, '%d/%m/%Y') AS order_date
FROM bronze.orders;
```

Checklist:

- Dates use ISO 8601.
- Timestamps are normalized to UTC.
- Currency uses a single base currency with an exchange-rate table.
- Country codes use ISO 3166-1 alpha-2.
- Phone numbers use E.164 where required.
- Text fields are trimmed and consistently cased.

### Outlier Detection

```python
import polars as pl

q = df.select(
    pl.col("amount").quantile(0.25).alias("q1"),
    pl.col("amount").quantile(0.75).alias("q3"),
).row(0, named=True)

iqr = q["q3"] - q["q1"]
lower = q["q1"] - 1.5 * iqr
upper = q["q3"] + 1.5 * iqr

df = df.with_columns(
    (~pl.col("amount").is_between(lower, upper)).alias("is_outlier")
)
```

### Schema Validation And Data Quality

Quality dimensions:

- Completeness: no unexpected nulls
- Uniqueness: no duplicate primary keys
- Validity: values stay within expected ranges and domains
- Timeliness: freshness checks pass
- Consistency: referential integrity holds across tables

dbt-style test example:

```yaml
models:
  - name: stg_orders
    columns:
      - name: order_id
        tests:
          - unique
          - not_null
      - name: status
        tests:
          - accepted_values:
              values: ["pending", "complete", "cancelled"]
      - name: amount_usd
        tests:
          - dbt_utils.expression_is_true:
              expression: ">= 0"
```

### Slowly Changing Dimensions

| Type | Behavior | Use Case |
|---|---|---|
| Type 1 | Overwrite old value | No history required |
| Type 2 | Add a new row with `valid_from` and `valid_to` | Full history |
| Type 3 | Add a prior-value column | One prior value is enough |

```sql
INSERT INTO dim_customers
SELECT
  customer_id,
  name,
  address,
  CURRENT_TIMESTAMP AS valid_from,
  NULL AS valid_to,
  TRUE AS is_current
FROM staging.customers AS src
WHERE NOT EXISTS (
  SELECT 1
  FROM dim_customers AS tgt
  WHERE tgt.customer_id = src.customer_id
    AND tgt.address = src.address
    AND tgt.is_current = TRUE
);
```

### PII Masking

```python
import hashlib
import polars as pl

def sha256_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()

df = df.with_columns(
    pl.col("email").map_elements(sha256_text, return_dtype=pl.String).alias("email_hash"),
    pl.col("postcode").str.slice(0, 3).alias("postcode_truncated"),
)
```

## Gold Layer: Aggregated And Business-Ready

Gold is the opinionated, business-aligned layer used by analysts, dashboards, APIs, and decision workflows.

Common patterns:

- Star schema: fact tables joined to conformed dimensions.
- One Big Table: a wide denormalized table for simple BI consumption.
- Semantic or metrics layer: central definitions for revenue, active users, churn, and other metrics.
- Feature store: materialized ML feature tables for training and serving.

Example:

```text
dim_date       dim_product       dim_customer
     \             |                 /
      \            |                /
              fact_orders
                   |
              dim_region
```

## Serving Layer

| Consumer | Pattern | Tools |
|---|---|---|
| Analysts and BI | SQL over Gold tables | Looker, Power BI, Tableau, Metabase |
| Data scientists | Notebook or DataFrame access | Jupyter, Databricks, SageMaker |
| Applications | REST or GraphQL API | FastAPI, Hasura, PostgREST |
| Operational teams | Reverse ETL | Census, Hightouch |
| Real-time dashboards | Streaming query | Flink, ksqlDB, Materialize |

## Data Processing Patterns

Incremental processing handles only new or changed rows.

```python
last_run_ts = get_last_run_timestamp()
query = f"""
SELECT *
FROM source_table
WHERE updated_at > '{last_run_ts}'
"""
```

Idempotency means rerunning a pipeline produces the same result.

- Use `MERGE` or `UPSERT` instead of raw inserts.
- Use partition overwrite instead of blind append.
- Use deterministic row hashes for deduplication keys.

Project-relative partition example:

```text
workspaces/<project>/interns/state/medallion/silver/orders/year=2026/month=05/day=20/
```

Late-arriving data can be handled with:

- Watermarking in streaming systems
- Bronze partition reprocessing within a lookback window
- A corrections pipeline that patches Silver under governance

## Cross-Cutting Concerns

Governance:

- Data catalog for discovery and ownership
- Column-level lineage from Gold metrics back to Bronze columns
- Row-level security, column masking, and role-based access control
- Retention policies for raw and generated data

Observability:

| Signal | What To Monitor |
|---|---|
| Freshness | Is the Gold table updated on schedule? |
| Volume | Did row count drop unexpectedly? |
| Schema | Was a column dropped or renamed? |
| Distribution | Did a key metric shift unexpectedly? |

Orchestration:

- Apache Airflow: DAG-based workflows
- Prefect: dynamic Python workflows
- Dagster: asset-based orchestration

DataOps:

```text
Git PR
  -> compile and test
  -> deploy to staging
  -> run data quality checks
  -> merge
  -> deploy to production
  -> alert on failure
```

## Modern Tool Stack

| Function | Open Source | Managed Or SaaS |
|---|---|---|
| Ingestion | Airbyte, Kafka Connect | Fivetran, Stitch |
| Orchestration | Airflow, Dagster, Prefect | Astronomer, Prefect Cloud |
| Lake storage | Delta Lake, Apache Iceberg | S3, GCS, ADLS |
| Warehouse | DuckDB, Trino | Snowflake, BigQuery, Redshift, Databricks |
| Transformation | dbt Core, Spark | dbt Cloud |
| Data quality | Great Expectations, Soda | Monte Carlo, Bigeye |
| Catalog | DataHub, Apache Atlas | Alation, Atlan |
| BI | Metabase, Superset | Looker, Power BI, Tableau |
| ML platform | MLflow, Feast | Databricks, SageMaker, Vertex AI |
| Reverse ETL | Open-source sync tools | Census, Hightouch |

## Summary

Medallion architecture organizes data from raw ingestion to business-ready analytics. Bronze preserves source truth, Silver makes data clean and joinable, and Gold creates business-facing analytical products.
