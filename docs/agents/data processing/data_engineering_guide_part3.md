# Part 3: Processing Engines, Orchestration, Modeling, Quality, and Optimization

---

# 11. Batch vs Streaming vs Micro-Batch

## Comparison Table

| Factor | Batch | Streaming (True) | Micro-Batch |
|---|---|---|---|
| **Latency** | Minutes to hours | Milliseconds to seconds | Seconds to minutes |
| **Cost** | Lowest (pay per job run) | Highest (always-on cluster) | Medium (always-on, but efficient) |
| **Complexity** | Lowest | Highest (watermarks, state, exactly-once) | Medium |
| **Reliability** | Very high (retry whole batch) | Complex (checkpoint-based recovery) | High (checkpoint-based, simpler than true streaming) |
| **Best use case** | Daily reports, ML training, large transforms | Fraud detection, real-time dashboards, alerts | Streaming ETL, near-real-time warehouse loads |
| **Failure mode** | Entire batch reruns; idempotency required | Job restart replays from checkpoint | Micro-batch reruns from checkpoint |
| **State management** | Stateless (or full scan) | Complex stateful (RocksDB, TTL) | Stateful (simpler than Flink) |
| **Watermark handling** | N/A | Per-event watermark advancement | Per-batch watermark advancement |
| **Tools** | Spark, dbt, BigQuery, Redshift | Apache Flink | Spark Structured Streaming |

## The Latency-Cost-Complexity Triangle

```
            Complexity
                ▲
                │
    True        │
    Streaming   │
    (Flink)     │
                │
    Micro-Batch ●
    (Spark SS)  │
                │
    Batch       │
    (Spark)     └──────────────────────► Cost (always-on infra)
    
Lower complexity and cost = batch
Higher freshness = more complexity and cost
```

---

# 12. Spark vs Flink

## Apache Spark Deep Dive

### Architecture

```
SPARK EXECUTION ARCHITECTURE

Driver Program (SparkContext)
  │
  ├─► DAG Scheduler: converts logical plan → stages
  ├─► Task Scheduler: assigns tasks to executors
  └─► Catalyst Optimizer: optimizes logical/physical plan

Cluster Manager (YARN/Kubernetes/Databricks)
  │
  ├─► Executor 1 (Worker Node)
  │     ├── Task 1.1 (process partition 0)
  │     ├── Task 1.2 (process partition 1)
  │     └── Block Manager (cache)
  ├─► Executor 2 (Worker Node)
  │     ├── Task 2.1 (process partition 2)
  │     └── Block Manager (cache)
  └─► Executor N ...

EXECUTION FLOW:
  1. User submits DataFrame transformations (lazy — not executed yet)
  2. .collect()/.write() triggers action
  3. Catalyst converts DataFrame API → Logical Plan
  4. Logical Plan → Optimized Logical Plan (predicate pushdown, projection pruning)
  5. Optimized Plan → Physical Plan (join strategies, partition counts)
  6. Physical Plan → DAG of stages (split at shuffle boundaries)
  7. Stages execute, exchanging data via shuffle between stages
```

### Catalyst Optimizer

The Catalyst optimizer is Spark's query optimization engine. It applies rule-based and cost-based optimizations:

**Key optimizations:**
1. **Predicate pushdown**: `filter(col("date") == "2024-01-01")` pushed to file scan — skips entire partitions/row groups
2. **Projection pruning**: Only reads columns referenced in the query
3. **Join reordering**: Reorders multi-table joins to start with smallest tables
4. **Constant folding**: `2 + 2` evaluated at planning time, not runtime
5. **Subquery optimization**: Converts correlated subqueries to joins where possible

### Shuffles — The Performance Killer

A shuffle is data redistribution across executor nodes. It requires:
1. All nodes to write intermediate data to disk
2. Network transfer of data to target nodes
3. Target nodes reading and sorting received data

**Shuffle is triggered by:** `groupBy`, `join`, `repartition`, `distinct`, `orderBy`

```python
# SHUFFLE MINIMIZATION STRATEGIES

# Strategy 1: Broadcast join (eliminates shuffle for small tables)
from pyspark.sql.functions import broadcast

# Spark will shuffle large orders table for a regular join
orders.join(countries, "country_code")  # BAD for small countries table

# Broadcast join: send countries table to ALL executors, no shuffle needed
orders.join(broadcast(countries), "country_code")  # GOOD (countries < 100MB)
# Config: spark.sql.autoBroadcastJoinThreshold (default 10MB, tune to 50-100MB)

# Strategy 2: Repartition before multiple operations on same key
# BAD: shuffle happens twice
orders.groupBy("user_id").agg(...).join(users, "user_id")  # 2 shuffles

# GOOD: repartition once, then both operations use same partition layout
orders.repartition("user_id") \
      .groupBy("user_id").agg(...) \
      .join(users.repartition("user_id"), "user_id")  # 1 shuffle

# Strategy 3: Reduce shuffle partitions for small data
spark.conf.set("spark.sql.shuffle.partitions", "200")  # Default 200
# For small datasets: use 2-8 partitions to avoid overhead
# Adaptive Query Execution (AQE) can auto-tune this in Spark 3.x
```

### Adaptive Query Execution (AQE) — Spark 3.x

AQE re-optimizes the query plan at runtime using actual data statistics collected during execution:

- **Automatic shuffle partition coalescing**: Merges small post-shuffle partitions automatically
- **Runtime join strategy switching**: Changes sort-merge join to broadcast join if one side is smaller than expected at runtime
- **Skew join optimization**: Splits skewed partitions and replicates the other side for balanced processing

```sql
-- Enable AQE (default in Spark 3.2+)
SET spark.sql.adaptive.enabled = true;
SET spark.sql.adaptive.coalescePartitions.enabled = true;
SET spark.sql.adaptive.skewJoin.enabled = true;
```

---

## Apache Flink Deep Dive

### Architecture

```
FLINK EXECUTION ARCHITECTURE

Job Manager (Master)
  ├─► Resource Manager (allocates TaskManager slots)
  ├─► Dispatcher (accepts job submissions)
  └─► Job Master per job (manages execution, checkpoints)

Task Managers (Workers)
  ├─► Task Slot 1 (a "thread" running one or more operators)
  ├─► Task Slot 2
  └─► Task Slot N

DATAFLOW GRAPH:
  Kafka Source ──► Map ──► KeyBy ──► Window ──► Sink (Delta Lake)
       │              │        │          │          │
  (parallelism=4) (p=4)   (p=8)     (p=8)       (p=4)
  
  Each operator has configurable parallelism.
  Data flows through the graph record-by-record.
  Shuffle (network exchange) happens between operators with different keys.
```

### Stateful Processing in Flink

```java
// FLINK STATEFUL FUNCTION EXAMPLE
public class FraudDetector extends KeyedProcessFunction<String, Transaction, Alert> {
    
    // State: per-user flag set when suspicious pattern detected
    private transient ValueState<Boolean> flagState;
    // State: timer to clear flag after 1 minute
    private transient ValueState<Long> timerState;
    
    @Override
    public void open(Configuration config) {
        ValueStateDescriptor<Boolean> flagDescriptor = 
            new ValueStateDescriptor<>("flag", Boolean.class);
        flagState = getRuntimeContext().getState(flagDescriptor);
    }
    
    @Override
    public void processElement(Transaction tx, Context ctx, Collector<Alert> out) {
        Boolean lastTransactionWasSmall = flagState.value();
        
        if (lastTransactionWasSmall != null && lastTransactionWasSmall) {
            if (tx.getAmount() > 1000) {
                // Pattern: small txn followed by large txn = fraud signal
                out.collect(new Alert(tx.getAccountId()));
            }
        }
        
        // Update state
        if (tx.getAmount() < 1.00) {
            flagState.update(true);
            // Set timer to clear flag after 1 minute
            long timer = ctx.timerService().currentProcessingTime() + 60_000L;
            ctx.timerService().registerProcessingTimeTimer(timer);
            timerState.update(timer);
        } else {
            flagState.clear();
        }
    }
    
    @Override
    public void onTimer(long timestamp, OnTimerContext ctx, Collector<Alert> out) {
        flagState.clear();  // Clear fraud flag after timeout
        timerState.clear();
    }
}
```

### Flink Checkpointing and Savepoints

```
FLINK CHECKPOINT MECHANISM (Chandy-Lamport Algorithm)

1. Job Manager injects CHECKPOINT BARRIER into Kafka source partitions
2. Source operators read data up to the barrier, then snapshot offset state
3. Barrier flows downstream through the dataflow graph
4. Each operator: when barrier received from ALL upstream edges:
     a. Takes snapshot of own state (async, written to S3/HDFS)
     b. Forwards barrier downstream
5. When all operators + sinks acknowledge: CHECKPOINT COMPLETE
6. On failure: restart from last completed checkpoint, replay from saved offsets

SAVEPOINT vs CHECKPOINT:
  Checkpoint: automatic, triggered by Flink, used for failure recovery
    → Deleted by Flink when no longer needed
    → Not portable between different job versions
    
  Savepoint: manually triggered, used for planned maintenance
    → Never deleted automatically
    → Portable: can restart modified job from savepoint
    → Used for: job upgrades, A/B testing, backfills
    
flink savepoint :jobId s3://checkpoints/savepoints/  # Create savepoint
flink run -s s3://checkpoints/savepoints/sp-001 job.jar  # Restart from savepoint
```

## Spark vs Flink Decision Table

| Requirement | Spark | Flink |
|---|---|---|
| **Large batch ETL** | ✅ Best | ⚠️ Possible but not optimal |
| **Low-latency streaming** | ❌ Micro-batch (seconds) | ✅ Best (milliseconds) |
| **Stateful event processing** | ⚠️ Limited | ✅ Best (KeyedProcessFunction) |
| **ML feature pipelines** | ✅ Best (MLlib, pandas UDFs) | ⚠️ Limited |
| **Lakehouse processing** | ✅ Best (Delta Lake native) | ✅ Good (Iceberg integration) |
| **Operational simplicity** | ✅ Easier to operate | ❌ Complex (state, checkpoints, backpressure) |
| **Complex event-time logic** | ⚠️ Watermarks advance per-batch | ✅ Best (per-event watermarks) |
| **Cost efficiency (batch)** | ✅ Very efficient | ❌ Always-on overhead |
| **Cost efficiency (streaming)** | ✅ Micro-batch amortizes overhead | ⚠️ Always-on per-record cost |
| **Team expertise required** | Medium | High |
| **Ecosystem maturity** | ✅ Very mature | ✅ Mature (streaming-specific) |

**Production guidance:** The vast majority of data engineering workloads are best served by Spark (batch) or Spark Structured Streaming (micro-batch). Introduce Flink only when you have confirmed requirements for sub-second latency, complex event-time logic, or long-running stateful computations that micro-batch cannot handle. The operational complexity of running Flink in production is non-trivial.

---

# 13. Orchestration: Airflow vs Dagster vs Prefect

## The Orchestration Mental Model Shift

```
FIRST GENERATION (Airflow): Task-centric
  "Run Task A, then Task B, then Task C"
  Focus: Scheduling and dependency management
  Observability: Did the task succeed or fail?
  
SECOND GENERATION (Dagster, Prefect): Asset-centric  
  "Produce Asset X from Asset Y"
  Focus: Data lineage and asset health
  Observability: Is this asset fresh? Does it meet quality? Who owns it?
```

## Apache Airflow

**Architecture:**
```
AIRFLOW COMPONENTS

Metadata DB (Postgres/MySQL)
  └── Stores: DAG definitions, task instances, run history, variables, connections

Scheduler
  └── Reads DAGs from filesystem
  └── Checks dependencies, triggers tasks at scheduled times
  └── Sends tasks to executor

Executor (Celery/Kubernetes/Local)
  └── Runs tasks in worker processes
  
Web Server
  └── Airflow UI: DAG view, run history, task logs

Workers (Celery mode)
  └── Processes that execute task Python code
```

**Production Airflow DAG patterns:**

```python
from airflow import DAG
from airflow.operators.python import PythonOperator
from airflow.providers.databricks.operators.databricks import DatabricksRunNowOperator
from airflow.providers.slack.operators.slack_webhook import SlackWebhookOperator
from datetime import datetime, timedelta

default_args = {
    "owner": "data-platform",
    "retries": 3,
    "retry_delay": timedelta(minutes=5),
    "retry_exponential_backoff": True,
    "max_retry_delay": timedelta(minutes=30),
    "email_on_failure": True,
    "email": ["data-alerts@company.com"],
    "sla": timedelta(hours=2),  # Alert if DAG takes > 2 hours
}

with DAG(
    dag_id="daily_revenue_pipeline",
    default_args=default_args,
    schedule_interval="0 2 * * *",  # 2 AM UTC daily
    start_date=datetime(2024, 1, 1),
    catchup=False,  # Don't backfill missed runs automatically
    max_active_runs=1,  # Prevent concurrent runs
    tags=["revenue", "gold", "daily"],
) as dag:
    
    # Step 1: Run Bronze ingestion on Databricks
    ingest_bronze = DatabricksRunNowOperator(
        task_id="ingest_bronze_orders",
        job_id="{{ var.value.databricks_bronze_job_id }}",
        databricks_conn_id="databricks_prod",
        job_run_metadata_key="bronze_run_id"
    )
    
    # Step 2: Run Silver transformation
    transform_silver = DatabricksRunNowOperator(
        task_id="transform_silver_orders",
        job_id="{{ var.value.databricks_silver_job_id }}",
        databricks_conn_id="databricks_prod"
    )
    
    # Step 3: Run dbt gold models
    dbt_gold = BashOperator(
        task_id="dbt_gold_models",
        bash_command="dbt run --select tag:gold --target prod"
    )
    
    # Step 4: Data quality checks
    quality_check = PythonOperator(
        task_id="quality_check_gold",
        python_callable=run_quality_checks,
        op_args=["{{ ds }}"]  # Pass execution date
    )
    
    # Step 5: Alert on success
    slack_success = SlackWebhookOperator(
        task_id="slack_success_alert",
        slack_webhook_conn_id="slack_alerts",
        message=f"✅ Daily revenue pipeline complete for {{{{ ds }}}}",
        trigger_rule="all_success"
    )
    
    ingest_bronze >> transform_silver >> dbt_gold >> quality_check >> slack_success
```

**Airflow production pain points:**
- Scheduler is a single point of failure (mitigated with HA mode, but complex to configure)
- No native data asset awareness — Airflow knows about tasks, not data
- Backfills require manual intervention (`airflow dags backfill -s 2024-01-01 -e 2024-01-31 dag_id`)
- Dynamic DAGs (DAGs generated from configuration) have terrible UX in Airflow UI
- Heavy Python dependencies between DAGs are hard to manage
- Limited built-in data quality integration

---

## Dagster

**Paradigm shift:** Dagster introduces "software-defined assets" (SDAs) — the core primitive is the data asset (a table, a model, a file), not the task. This fundamentally changes how pipelines are defined and observed.

```python
# DAGSTER SOFTWARE-DEFINED ASSETS

from dagster import asset, AssetIn, FreshnessPolicy, AutoMaterializePolicy
import pandas as pd

@asset(
    # This asset depends on two upstream assets
    ins={
        "raw_orders": AssetIn(),
        "customers": AssetIn(key_prefix="silver")
    },
    # Business metadata
    group_name="silver",
    owners=["data-platform@company.com"],
    # Quality and freshness
    freshness_policy=FreshnessPolicy(maximum_lag_minutes=60),
    # Auto-materialize when upstream changes
    auto_materialize_policy=AutoMaterializePolicy.eager(),
    # Partitioning
    partitions_def=DailyPartitionsDefinition(start_date="2024-01-01"),
)
def orders_silver(context, raw_orders: pd.DataFrame, customers: pd.DataFrame) -> pd.DataFrame:
    """Cleaned and enriched orders, joined with customer data."""
    
    # Validate inputs
    assert raw_orders["order_id"].notna().all(), "order_id cannot be null"
    
    # Transform
    cleaned = (
        raw_orders
        .merge(customers[["customer_id", "region"]], on="customer_id", how="left")
        .assign(
            amount=lambda df: pd.to_numeric(df["amount"], errors="coerce"),
            created_at=lambda df: pd.to_datetime(df["created_at"], utc=True)
        )
        .dropna(subset=["order_id", "amount"])
    )
    
    # Emit data quality metadata
    context.add_output_metadata({
        "row_count": len(cleaned),
        "null_amount_pct": cleaned["amount"].isna().mean(),
        "preview": MetadataValue.md(cleaned.head().to_markdown())
    })
    
    return cleaned

@asset(
    ins={"orders_silver": AssetIn()},
    group_name="gold",
    freshness_policy=FreshnessPolicy(maximum_lag_minutes=30),
)
def daily_revenue(orders_silver: pd.DataFrame) -> pd.DataFrame:
    """Daily revenue aggregation for executive dashboard."""
    return (
        orders_silver
        .groupby(["business_date", "region"])
        .agg(
            gross_revenue=("amount", "sum"),
            order_count=("order_id", "count")
        )
        .reset_index()
    )
```

**Dagster advantages over Airflow:**
- Asset lineage graph is automatic — Dagster builds the dependency graph from `ins` declarations
- Asset freshness monitoring: Dagster alerts when an asset hasn't been materialized within its `freshness_policy`
- Better partitioning: native support for time-partitioned and dynamically partitioned assets
- Testing: `dagster asset materialize --select orders_silver` runs a single asset in isolation
- Local development: full pipeline testable locally without a running Dagster server

**Dagster pain points:**
- Higher learning curve than Airflow
- Smaller ecosystem (fewer integrations) than Airflow
- Asset-based model is less intuitive for pure task orchestration (e.g., "run this shell script every hour")

---

## Prefect

**Position:** Prefect is the "developer-friendly" middle ground. It uses a task/flow model like Airflow but with dramatically better developer experience, dynamic workflows, and cloud-native deployment.

```python
# PREFECT FLOW EXAMPLE

from prefect import flow, task
from prefect.tasks import task_input_hash
from datetime import timedelta

@task(
    retries=3,
    retry_delay_seconds=60,
    cache_key_fn=task_input_hash,   # Cache result based on inputs
    cache_expiration=timedelta(hours=1),
    timeout_seconds=3600,
    tags=["bronze", "orders"]
)
def ingest_bronze_orders(execution_date: str) -> int:
    """Ingest raw orders for given date. Returns row count."""
    df = read_from_source(execution_date)
    df.write_delta("bronze.orders_raw", mode="append")
    return len(df)

@task(retries=2, timeout_seconds=7200)
def transform_silver_orders(execution_date: str, bronze_count: int):
    """Transform bronze to silver. Validates against expected count."""
    if bronze_count < 100:
        raise ValueError(f"Bronze count {bronze_count} too low — possible ingestion failure")
    run_spark_transform(execution_date)

@flow(
    name="daily-orders-pipeline",
    description="Daily order processing pipeline",
    on_failure=[notify_slack_on_failure],
    on_completion=[notify_slack_on_success],
)
def daily_orders_pipeline(execution_date: str = None):
    if execution_date is None:
        execution_date = str(date.today())
    
    bronze_count = ingest_bronze_orders(execution_date)
    transform_silver_orders(execution_date, bronze_count)
    run_dbt_models(tags=["gold"])
    validate_gold_quality(execution_date)

# Deploy with schedule
daily_orders_pipeline.serve(
    name="daily-orders-deployment",
    cron="0 2 * * *"  # 2 AM UTC
)
```

**Prefect advantages:**
- Dynamic workflows: tasks can be created and run based on runtime data (loop over a list, run tasks conditionally)
- Better developer experience: `prefect run` locally, identical to production
- Prefect Cloud handles infrastructure; no self-hosted Airflow cluster needed
- Native async support

**Prefect pain points:**
- Prefect Cloud has per-run costs (vs self-hosted Airflow which has fixed infrastructure cost)
- Less mature ecosystem than Airflow
- The task/flow model doesn't have Dagster's asset observability

## Orchestration Decision Table

| Requirement | Airflow | Dagster | Prefect | Databricks Workflows |
|---|---|---|---|---|
| **Traditional batch DAGs** | ✅ Best | ✅ Good | ✅ Good | ✅ Good (for Databricks jobs) |
| **Asset lineage** | ❌ Manual | ✅ Best | ⚠️ Limited | ⚠️ Limited |
| **Python-native development** | ⚠️ Complex | ✅ Best | ✅ Excellent | ⚠️ Notebook/job only |
| **Data quality integration** | ⚠️ External | ✅ Built-in metadata | ⚠️ External | ✅ DLT expectations |
| **Databricks-native jobs** | ⚠️ Via operator | ⚠️ Via integration | ⚠️ Via integration | ✅ Best |
| **Backfills** | ⚠️ Manual CLI | ✅ Partition-native | ✅ Good | ✅ Workflow retry |
| **Operational simplicity** | ❌ Complex | ⚠️ Medium | ✅ Cloud-managed | ✅ Managed |
| **Ecosystem/integrations** | ✅ Best (1000+ providers) | ⚠️ Growing | ✅ Good | ⚠️ Databricks-focused |
| **Best for** | Large enterprises with existing Airflow | New platforms, asset-centric teams | Developer-first teams | Databricks-native pipelines |

---

# 14. Data Modeling

## 14.1 Star Schema

The star schema is the most common analytical data model. A central fact table surrounded by dimension tables.

```
                      DIM_DATE
                    (date_key, year, quarter, month, day, is_holiday)
                         │
DIM_PRODUCT ─────────────┤
(product_key, name,       │
 category, brand)         │
                    FACT_ORDERS
DIM_CUSTOMER ──────(order_id, date_key,
(customer_key, name,  customer_key,
 segment, region)     product_key,
                      store_key,
DIM_STORE ───────────  amount,
(store_key, city,      quantity,
 state, region)        discount,
                       net_revenue)
```

**Grain:** The grain of a fact table is the most atomic level of measurement it records. Define grain FIRST before designing the fact table. Example: FACT_ORDERS grain = one row per order line item (not per order, not per day).

**Types of facts:**
- **Additive**: Can be summed across all dimensions (revenue, quantity)
- **Semi-additive**: Can be summed across some dimensions (inventory balance — can sum by product, but not by time)
- **Non-additive**: Cannot be meaningfully summed (unit price, discount %)

**Slowly Changing Dimensions (SCD) — production patterns:**

In a star schema, dimension attributes change over time. The SCD type determines historical accuracy:
- **SCD Type 1**: Overwrite — simple but loses history
- **SCD Type 2**: Add new row with validity dates — full history, used for "as of" queries
- **SCD Type 6**: Combines Type 1 + 2 + 3 — current value in fact, history in dimension

```sql
-- SCD Type 2 dimension query: revenue at the time of sale
-- (uses the region that was current WHEN the order was placed, 
--  not the customer's current region)

SELECT 
    o.business_date,
    c.region,    -- ← Region as of the order date (SCD2)
    SUM(o.net_revenue) AS revenue
FROM fact_orders o
JOIN dim_customer c 
    ON o.customer_key = c.customer_key
    AND o.order_date BETWEEN c.valid_from AND COALESCE(c.valid_to, '9999-12-31')
GROUP BY 1, 2
```

## 14.2 Data Vault 2.0

Data Vault is designed for enterprise data warehouses where auditability, historical tracking, and flexibility to accommodate unknown future requirements are priorities.

```
DATA VAULT COMPONENTS

HUB: Business keys (what the business cares about)
  HUB_CUSTOMER: {hub_customer_hk, customer_id, load_date, record_source}
  HUB_ORDER:    {hub_order_hk,    order_id,    load_date, record_source}

LINK: Relationships between hubs (many-to-many, historical)
  LINK_ORDER_CUSTOMER: {
    link_order_customer_hk,
    hub_customer_hk,    ← FK to HUB_CUSTOMER
    hub_order_hk,       ← FK to HUB_ORDER
    load_date,
    record_source
  }

SATELLITE: Descriptive attributes (with full history)
  SAT_CUSTOMER_DETAILS: {
    hub_customer_hk,   ← FK to HUB_CUSTOMER
    load_date,
    load_end_date,
    record_source,
    hash_diff,         ← Hash of attributes; detect changes
    customer_name,
    email,
    city
  }
```

**Data Vault advantages:**
- Extremely auditable: every row has load_date, record_source, and hash
- Handles multiple sources: same customer can arrive from CRM and ERP; Data Vault links them without overwriting
- No business rules in the vault: business rules applied in information marts (gold layer) on top
- Parallel loading: Hubs, Links, and Satellites can be loaded independently

**Data Vault disadvantages:**
- Complex to query: 5-10 table joins for a simple "customer order history" query
- High storage overhead (load_date, hash columns on every row)
- Requires dedicated information mart layer for BI (essentially builds a star schema on top)
- Steep learning curve; requires DV2.0-trained team

**When to use Data Vault:**
- Enterprise data warehouse with 10+ source systems
- Strong regulatory audit requirements
- Data from multiple sources that must be reconciled (MDM use case)
- When requirements change frequently (DV accommodates new sources/attributes without structural changes)

**When NOT to use Data Vault:**
- Startup or mid-size company with 1-3 source systems
- Simple analytics requirements
- Small team without DV expertise
- Time-sensitive implementation (DV takes 2-3x longer to implement than star schema)

## 14.3 Wide Tables (Denormalized)

Modern analytics platforms (Snowflake, BigQuery, Databricks) have made wide, fully denormalized tables the performance-optimal choice for high-traffic analytics.

```sql
-- Traditional approach: join at query time
SELECT 
    o.order_id, o.amount,
    c.customer_name, c.region,
    p.product_name, p.category,
    d.month, d.year
FROM fact_orders o
JOIN dim_customer c ON o.customer_key = c.customer_key
JOIN dim_product p ON o.product_key = p.product_key
JOIN dim_date d ON o.date_key = d.date_key

-- Wide table approach: denormalize everything into one table
-- Pre-join at write time, avoid join at query time

CREATE TABLE gold.orders_wide AS
SELECT 
    o.order_id, o.amount,
    c.customer_name, c.region,
    p.product_name, p.category,
    d.month, d.year,
    -- Add as many denormalized attributes as analysts need
    c.lifetime_orders, c.customer_segment,
    p.brand, p.subcategory, p.unit_cost
FROM fact_orders o
JOIN dim_customer c ON ...
JOIN dim_product p ON ...
JOIN dim_date d ON ...
```

**Wide table advantages:**
- Zero join cost at query time: analysts write simple `SELECT ... FROM orders_wide WHERE ...`
- BI tool performance: Tableau/Looker queries run in seconds instead of minutes
- Columnar storage efficiency: for a query that only uses 5 columns from a 100-column table, columnar storage reads only 5 columns

**Wide table disadvantages:**
- Storage duplication (customer name stored in every order row, not just dim_customer)
- Consistency risk: if customer name changes, all rows in the wide table are stale until refresh
- Governance complexity: PII columns (email, phone) appear in more tables, wider blast radius

## 14.4 Semantic Layer

The semantic layer is an abstraction layer between raw data and business users that defines business metrics in a centralized, consistent, and reusable way.

```
WITHOUT SEMANTIC LAYER:
  Analyst A's SQL: SUM(amount) WHERE status='completed' AND refunded=false
  Analyst B's SQL: SUM(amount) WHERE status IN ('shipped','delivered') AND is_returned=false
  Executive Dashboard: SUM(net_revenue)   ← Uses yet another definition
  → Three different "revenue" numbers in the same company

WITH SEMANTIC LAYER (dbt Semantic Layer / Cube / LookML):
  Defined once:
    metric: net_revenue
      label: "Net Revenue"
      description: "Gross revenue minus refunds and discounts"
      calculation: SUM(amount) - SUM(refund_amount) - SUM(discount_amount)
      filter: "status IN ('confirmed', 'shipped', 'delivered')"
  
  All tools query the semantic layer:
  → Tableau uses net_revenue definition
  → Looker uses net_revenue definition  
  → AI/LLM text-to-SQL uses net_revenue definition
  → All return the same number
```

**dbt Semantic Layer (MetricFlow):**
```yaml
# metrics.yml
metrics:
  - name: net_revenue
    label: Net Revenue
    type: simple
    type_params:
      measure:
        name: net_revenue_amount
        agg: sum
    filter: |
      {{ Dimension('order__status') }} IN ('confirmed', 'shipped', 'delivered')
    
  - name: avg_order_value
    label: Average Order Value
    type: ratio
    type_params:
      numerator: net_revenue
      denominator: order_count
```

---

# 15. Data Quality, Observability, Contracts, and Governance

## Data Observability Stack

```
DATA OBSERVABILITY PYRAMID

        ┌─────────────────────┐
        │   BUSINESS IMPACT   │  ← Did bad data affect a business decision?
        │   (Dashboards, KPIs)│    Manual + automated reconciliation
        └─────────────────────┘
       ┌───────────────────────┐
       │    DISTRIBUTION DRIFT │  ← Did statistical distributions change?
       │    (ML data quality)  │    Anomaly detection, z-scores
       └───────────────────────┘
      ┌─────────────────────────┐
      │   SCHEMA MONITORING     │  ← New columns? Dropped columns? Type changes?
      │   (Structural quality)  │    Schema registry, contract enforcement
      └─────────────────────────┘
     ┌───────────────────────────┐
     │   VOLUME + FRESHNESS      │  ← Did expected data arrive? On time?
     │   (Operational quality)   │    Threshold checks, SLA monitoring
     └───────────────────────────┘
    ┌─────────────────────────────┐
    │   ROW-LEVEL QUALITY         │  ← Nulls, duplicates, constraint violations
    │   (Cell-level quality)      │    dbt tests, Great Expectations, Soda
    └─────────────────────────────┘
```

## The Monte Carlo / Anomaly Detection Approach

Production data observability tools (Monte Carlo, Bigeye, Acceldata) use ML-based anomaly detection on pipeline metadata to automatically detect issues:

```python
# SIMPLIFIED ANOMALY DETECTION (what observability tools do internally)

import numpy as np
from scipy import stats

def detect_freshness_anomaly(table_name: str, max_lag_hours: float = 2.0):
    """Detect if data is stale beyond expected SLA."""
    last_update = get_table_last_modified(table_name)
    lag_hours = (datetime.utcnow() - last_update).total_seconds() / 3600
    
    if lag_hours > max_lag_hours:
        alert(f"FRESHNESS: {table_name} is {lag_hours:.1f}h stale (SLA: {max_lag_hours}h)")

def detect_volume_anomaly(table_name: str, partition_col: str, window_days: int = 30):
    """Detect if today's row count is anomalous based on 30-day history."""
    historical = get_daily_row_counts(table_name, partition_col, window_days)
    today_count = get_today_row_count(table_name, partition_col)
    
    # Use IQR method for outlier detection (robust to non-normal distributions)
    q1, q3 = np.percentile(historical, [25, 75])
    iqr = q3 - q1
    lower_bound = q1 - 3 * iqr
    upper_bound = q3 + 3 * iqr
    
    if today_count < lower_bound or today_count > upper_bound:
        alert(f"VOLUME: {table_name} count {today_count} outside bounds [{lower_bound:.0f}, {upper_bound:.0f}]")
    
    # Also check for percent change from yesterday
    yesterday_count = historical[-1]
    pct_change = abs(today_count - yesterday_count) / yesterday_count
    if pct_change > 0.5:  # >50% change day-over-day
        alert(f"VOLUME: {table_name} changed {pct_change:.0%} vs yesterday")
```

## Data Lineage

Data lineage tracks the origin and transformation path of every data asset. It answers:
- "Where did this revenue number come from?"
- "If I change this upstream table, what downstream assets break?"
- "Which pipelines touched the user_id field in this table?"

**OpenLineage standard:** An open standard for collecting lineage metadata from any data tool (Spark, Airflow, dbt, Flink). Tools emit lineage events to a central collector (Marquez, DataHub, Unity Catalog).

```
LINEAGE GRAPH EXAMPLE

orders_raw (S3)
    │ [Auto Loader, Job: bronze_ingest, Runtime: 2024-01-07 02:00 UTC]
    ▼
bronze.orders_raw (Delta)
    │ [DLT Pipeline: silver_transform, Runtime: 2024-01-07 02:15 UTC]
    ▼
silver.orders (Delta)
    │ [dbt model: daily_revenue, Runtime: 2024-01-07 02:45 UTC]
    ▼
gold.daily_revenue (Delta)
    │ [Tableau Live Connection]
    ▼
Executive Revenue Dashboard
    │
    └── Used by: CEO, CFO, VP Sales (5 users in last 7 days)
```

---

# 16. Production Reliability Methods

## Idempotency Patterns

```python
# PRODUCTION IDEMPOTENCY PATTERNS

# Pattern 1: Overwrite partition (batch)
spark.conf.set("spark.sql.sources.partitionOverwriteMode", "dynamic")
df.write.mode("overwrite").partitionBy("processing_date").saveAsTable("silver.orders")

# Pattern 2: Delta MERGE for upserts
spark.sql("""
    MERGE INTO silver.orders target
    USING updates source ON target.order_id = source.order_id
    WHEN MATCHED THEN UPDATE SET *
    WHEN NOT MATCHED THEN INSERT *
""")

# Pattern 3: Write-to-staging, validate, swap
df.write.mode("overwrite").saveAsTable("silver.orders_staging")
validate_staging("silver.orders_staging")  # Throws on failure
spark.sql("ALTER TABLE silver.orders SWAP WITH silver.orders_staging")

# Pattern 4: Idempotency key in streaming sink
def write_to_delta(df, epoch_id):
    # Use epoch_id as idempotency key — same epoch = same data
    df.withColumn("_batch_id", lit(epoch_id)) \
      .write.format("delta") \
      .mode("append") \
      .save("/delta/orders")
```

## Circuit Breakers for Data Pipelines

```python
# CIRCUIT BREAKER PATTERN

class DataPipelineCircuitBreaker:
    def __init__(self, failure_threshold=3, reset_timeout_minutes=30):
        self.failure_count = 0
        self.state = "CLOSED"  # CLOSED=normal, OPEN=blocking, HALF_OPEN=testing
        self.failure_threshold = failure_threshold
        self.last_failure_time = None
        self.reset_timeout = timedelta(minutes=reset_timeout_minutes)
    
    def can_execute(self):
        if self.state == "CLOSED":
            return True
        elif self.state == "OPEN":
            if datetime.now() - self.last_failure_time > self.reset_timeout:
                self.state = "HALF_OPEN"
                return True  # Allow one test attempt
            return False
        elif self.state == "HALF_OPEN":
            return True  # Allow the test attempt
    
    def on_success(self):
        self.failure_count = 0
        self.state = "CLOSED"
    
    def on_failure(self):
        self.failure_count += 1
        self.last_failure_time = datetime.now()
        if self.failure_count >= self.failure_threshold:
            self.state = "OPEN"
            alert(f"Circuit breaker OPEN: {self.failure_count} consecutive failures")
```

## Failure Handling Taxonomy

```
FAILURE TYPE                RECOVERY STRATEGY              AUTOMATION
─────────────────────────────────────────────────────────────────────
Transient (network glitch)  Automatic retry + backoff      Fully automated
Resource exhaustion         Scale up + retry                Partially automated
Upstream data missing       Wait + retry (sensor pattern)  Automated (with timeout)
Bad data record             Quarantine + continue          Automated
Schema change               Alert + halt pipeline          Alert automated; fix manual
Bug in transform logic      Rollback + fix + redeploy      Manual
Infrastructure failure      Failover to standby            Partially automated
Data corruption             Restore from backup            Manual
```

---

# 17. Cost and Performance Optimization

## The Cost Optimization Framework

```
COST LEVERS IN CLOUD DATA PLATFORMS

1. COMPUTE COSTS
   ├── Right-size clusters (don't over-provision)
   ├── Spot/preemptible instances for batch (60-80% savings)
   ├── Auto-scaling (scale down when idle)
   ├── Serverless for bursty workloads (pay per query second)
   └── Avoid idle clusters (Databricks auto-termination)

2. STORAGE COSTS  
   ├── Parquet compression (5-10x vs CSV)
   ├── VACUUM old Delta versions (don't retain years of history)
   ├── Lifecycle policies (move cold data to archival storage)
   ├── Remove duplicate/redundant copies
   └── Right-size replication (1 copy vs 3 copies)

3. QUERY COSTS
   ├── Partition pruning (write date-partitioned, query with date filter)
   ├── Column pruning (select only needed columns)
   ├── Result caching (repeated BI queries return cached result)
   ├── Materialized views (pre-compute expensive aggregations)
   └── Query termination policies (kill runaway queries)

4. NETWORK/EGRESS COSTS
   ├── Minimize cross-region data movement
   ├── Process data in the same region as storage
   └── Use direct-read connectors (avoid intermediate copies)
```

## Databricks Cost Optimization

```python
# CLUSTER SIZING GUIDELINES

# Batch ETL: 
#   - Use spot instances (interruption tolerance: idempotent jobs can restart)
#   - Driver: 4 cores, 16GB (don't over-provision driver)  
#   - Workers: 8-16 cores per worker, auto-scaling 2-20 workers
#   - Use Photon: 2-5x speedup for SQL workloads, often cost-neutral due to shorter runtime

# Streaming:
#   - On-demand instances (spot instances cause checkpoint issues)
#   - Fixed cluster size (auto-scaling not recommended for streaming jobs)
#   - Size to steady-state + 20% buffer

# SQL Warehouses (BI queries):
#   - Start small (Small or Medium warehouse), auto-scale up
#   - Enable result caching (reuse identical query results)
#   - Serverless SQL warehouses: pay per query second, no idle cost

# COST MONITORING QUERY
%sql
SELECT 
    usage_date,
    sku_name,
    SUM(dbus) as total_dbus,
    SUM(dbus * price_per_dbu) as estimated_cost_usd
FROM system.billing.usage
WHERE usage_date >= CURRENT_DATE - 30
GROUP BY 1, 2
ORDER BY 3 DESC;
```

## File Compaction and Small File Problem

```
SMALL FILE PROBLEM

Root cause: Streaming jobs write many small files (one file per micro-batch per partition)
  
Streaming job writing every 1 minute × 100 partitions = 100 files/minute
After 24 hours: 144,000 files
After 30 days: 4,320,000 files

Impact:
  - Every query must open and read metadata for all files (slow)
  - Cloud storage API costs multiply with file count
  - OOM in Spark executors listing millions of files

SOLUTIONS:

1. Delta OPTIMIZE (compaction)
   OPTIMIZE silver.orders;  -- Compact all small files into 256MB-1GB target size files
   
   Automate: Run OPTIMIZE on a schedule or via Databricks Predictive Optimization
   
2. Target file size configuration
   spark.conf.set("spark.databricks.delta.targetFileSize", "256mb")
   
3. Auto-Optimize (Databricks)
   TBLPROPERTIES ('delta.autoOptimize.autoCompact' = 'true')
   -- Automatically compacts files during write operations
   
4. Liquid clustering (preferred for new tables)
   CREATE TABLE orders CLUSTER BY (order_id)
   -- Liquid clustering automatically compacts as part of OPTIMIZE
```

## Query Performance Optimization

```sql
-- PERFORMANCE ANTI-PATTERN: SELECT * on wide table
SELECT * FROM gold.orders_wide;  -- Reads 100 columns even if you need 5

-- OPTIMIZED: Explicit column selection
SELECT order_id, amount, customer_name, business_date 
FROM gold.orders_wide;  -- Reads only 4 columns (96% I/O reduction on wide table)

-- ANTI-PATTERN: Non-partition-pruning query
SELECT SUM(amount) FROM silver.orders 
WHERE order_status = 'completed';  -- Full table scan if partitioned by date!

-- OPTIMIZED: Include partition column in filter
SELECT SUM(amount) FROM silver.orders 
WHERE business_date >= '2024-01-01'    -- Partition pruning!
  AND order_status = 'completed';      -- Then filter within partitions

-- ANTI-PATTERN: Unbounded explode
SELECT user_id, item FROM orders LATERAL VIEW explode(items) t AS item
-- If orders has 100M rows × avg 10 items = 1 BILLION rows exploded

-- OPTIMIZED: Filter before explode
SELECT user_id, item FROM orders 
WHERE business_date = '2024-01-07'  -- Filter first, reduce data size
LATERAL VIEW explode(items) t AS item
```
