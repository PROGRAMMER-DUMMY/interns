# Part 2: Architecture Approaches, Storage Paradigms, and Open Formats

---

# 5. ETL, ELT, ETLT, ELTL, and Reverse ETL

## Decision Table

| Factor | ETL | ELT | ETLT | ELTL |
|---|---|---|---|---|
| **Best for** | Legacy warehouse loads, PII pre-masking, strict transform before load | Cloud warehouse/lakehouse with unlimited compute | Pre-clean required + warehouse-native final transform | Intermediate curated layer → serving system |
| **Cost** | High (external compute for T) | Low (warehouse compute for T) | Medium | Medium-High (two compute layers) |
| **Latency** | Medium (transform before load) | Low (load first, transform later) | Medium | High (two transform passes) |
| **Governance** | Strong (PII never enters warehouse) | Requires column masking in warehouse | Hybrid | Hybrid |
| **Complexity** | High (separate ETL tool + warehouse) | Low (warehouse SQL) | High | High |
| **Data quality** | Pre-validated before load | Raw data in warehouse (risk of junk in) | Pre-cleaned raw | Pre-cleaned raw |
| **Failure risk** | Transform failure blocks load | Raw load succeeds, transform can retry | Transform failure blocks initial load | Load failure in final layer |
| **Example tools** | Informatica, SSIS, Talend | dbt + Snowflake/BQ/Redshift | Spark + dbt, Glue + Redshift | Spark + dbt + Reverse ETL |

---

## 5.1 ETL (Extract-Transform-Load)

**Definition:** Data is extracted from sources, transformed in an intermediate processing engine, then loaded into the destination warehouse. The warehouse only receives clean, ready-to-query data.

**Why it exists:** Born in the 1980s-90s when storage was expensive, warehouses were rigid (IBM DB2, Teradata), and compute was centralized. Transforming before loading minimized storage costs and protected the warehouse from messy data.

**Architecture:**
```
Source DB ──► ETL Server ──► (Transform) ──► Data Warehouse
  Oracle        Informatica      Join, cleanse, aggregate     Teradata/IBM
  SAP           SSIS             PII masking                  SQL Server DW
  Mainframe     Talend           Type casting
```

**When ETL is still correct in 2024:**
- PII must never land in the warehouse (regulatory requirement)
- Source systems require complex pre-processing that warehouses cannot perform (custom parsing, external API calls)
- Target warehouse is legacy and cannot handle raw/messy data
- Strict latency SLA for load completion (pre-transform ensures clean, fast loads)

**Weaknesses:**
- Transform failures block data from ever reaching the warehouse
- External compute clusters are expensive and difficult to scale
- Schema changes in source require updating ETL mappings before data can load
- Poor flexibility — adding a new output column requires redeploying the ETL job

---

## 5.2 ELT (Extract-Load-Transform)

**Definition:** Data is extracted from sources, loaded into the warehouse/lakehouse in raw form, then transformed using the warehouse's native compute power. The raw data is always available.

**Why it emerged:** Cloud warehouses (Snowflake, BigQuery, Redshift) eliminated the cost barrier of storing raw data. Their massively parallel SQL engines are often faster and cheaper than external ETL compute for transformation workloads. dbt made SQL-based transformation professional and testable.

**Architecture:**
```
Source DB ──► Ingestion Tool ──► Raw Layer ──► dbt/SQL ──► Transformed Layer
  Postgres      Fivetran           Snowflake     dbt models   Analytics tables
  Stripe API    Airbyte            BigQuery      SQL macros    
  Kafka         Stitch             Redshift      Tests         
```

**Critical advantage:** Raw data is always preserved. If a transformation has a bug, you can fix the SQL and rerun. No data is lost. This is impossible in ETL where transformation happens before storage.

**Production reality:** Most modern data platforms are ELT-first. dbt has become the de facto transformation standard, enabling SQL-based transformations with version control, testing, and documentation.

**Weaknesses:**
- Raw data (including PII) lands in the warehouse — requires column masking and access controls
- Warehouse compute for transformation costs money (though often less than external ETL)
- Complex transformations that SQL cannot express still require external compute

---

## 5.3 ETLT (Extract-Transform-Load-Transform)

**Definition:** A hybrid pattern where some transformations happen before loading (the first T) and additional transformations happen after loading (the second T). Combines ETL and ELT.

**Why it exists:** Real-world data engineering is messy. Sometimes you need to:
- Pre-mask PII before it enters the data platform (first T in external processing)
- Denormalize, join, and aggregate using the warehouse's powerful SQL (second T in-platform)

**Example use case:**
A healthcare company processes patient records. PHI (Protected Health Information) must be de-identified before it enters the analytics platform (HIPAA requirement). But complex clinical analytics (diagnosis cohort analysis, treatment outcome modeling) are best expressed in SQL on the warehouse.

```
EHR System ──► Spark Job ──► (De-identify PHI) ──► Lakehouse Raw ──► dbt ──► Clinical Analytics
              (First T: PII removal)                              (Second T: clinical transforms)
```

---

## 5.4 ELTL (Extract-Load-Transform-Load)

**Definition:** Data is loaded into a raw/staging area, transformed into curated intermediate datasets, then loaded again into a specialized serving system optimized for the final consumption pattern.

**Why it exists:** The transformation destination (lakehouse, warehouse) may not be the optimal serving system. Curated data often needs to be:
- Exported to a high-performance OLAP cube (ClickHouse, Druid) for sub-second dashboard queries
- Pushed to a feature store (Feast, Tecton) for ML model serving
- Loaded into Elasticsearch for search
- Exported to operational databases for application serving

**Architecture:**
```
Sources ──► Lakehouse (Raw) ──► dbt Transform ──► Lakehouse (Curated) ──► ClickHouse/Druid
                                                                          Feature Store
                                                                          Elasticsearch
                                                                          Redis
```

---

## 5.5 Reverse ETL

**Definition:** Moving transformed, curated data from the analytics warehouse/lakehouse back into operational systems (CRM, marketing automation, support tools, product databases) to enable data-driven operations.

**Why it emerged:** Analytics teams built incredible customer insights in their warehouses but could not operationalize them. A customer health score in Snowflake doesn't trigger a success team outreach in Salesforce unless there's a pipeline to push it there. Reverse ETL is that pipeline.

**Architecture:**
```
Lakehouse/Warehouse ──► Reverse ETL Tool ──► Operational Systems
  Snowflake               Census              Salesforce (CRM)
  BigQuery                Hightouch           HubSpot (Marketing)
  Redshift                dbt + Census        Zendesk (Support)
                                              Intercom (Product)
                                              Amplitude (Analytics)
                                              Slack (Notifications)
```

**Production use cases:**
- Sync customer lifetime value scores to Salesforce for sales prioritization
- Push product usage segments to HubSpot for targeted email campaigns
- Update churn probability scores in customer success tools daily
- Sync order status from warehouse to customer support tools

**Production challenges:**
- **API rate limits**: Salesforce has strict API limits; batching and retry logic is essential
- **Data freshness**: Operational teams expect near-real-time data; warehouse batch pipelines may not meet this
- **Schema mapping**: Warehouse column names rarely match CRM field names
- **Conflict resolution**: If a CRM rep manually updated a field, should the reverse ETL overwrite it?
- **Auditability**: All writes to operational systems must be logged for debugging

**Tools: Census vs Hightouch vs dbt + custom scripts:**

| Feature | Census | Hightouch | Custom (dbt + scripts) |
|---|---|---|---|
| Ease of setup | Very easy | Very easy | Complex |
| Cost | High | High | Infrastructure only |
| Connector library | 200+ | 200+ | Manual |
| Custom logic | Limited | Limited | Full flexibility |
| Observability | Built-in | Built-in | Must build |
| Best for | Mid-market, fast setup | Mid-market, fast setup | Large teams with custom requirements |

---

# 6. OLTP vs OLAP Deep Dive

## The Fundamental Difference

```
OLTP (Online Transaction Processing)      OLAP (Online Analytical Processing)
───────────────────────────────────       ───────────────────────────────────
Purpose: Run the business                 Purpose: Understand the business
Queries: Millisecond point lookups        Queries: Multi-second to minute scans
Data: Current state (normalized)          Data: Historical + current (denormalized)
Volume: Thousands of rows per query       Volume: Millions to billions of rows
Users: Application code (millions)        Users: Analysts/dashboards (thousands)
Writes: Frequent, small, transactional    Writes: Batch or micro-batch
Schema: Highly normalized (3NF)           Schema: Denormalized (star/snowflake)
Examples: Postgres, MySQL, Oracle         Examples: Snowflake, BigQuery, Redshift
Indexes: B-tree on primary/foreign keys   Indexes: Columnar, zone maps, bloom filters
Storage: Row-oriented (fast single row)   Storage: Column-oriented (fast aggregation)
```

## Row Storage vs Columnar Storage

**Why row storage optimizes for OLTP:**
```
ROW STORAGE (PostgreSQL heap page)
Record 1: [user_id=1, name="Alice", email="a@b.com", balance=100, city="NYC"]
Record 2: [user_id=2, name="Bob",   email="b@b.com", balance=200, city="LA"]
Record 3: [user_id=3, name="Carol", email="c@b.com", balance=300, city="NYC"]

Query: SELECT * FROM users WHERE user_id = 2
→ Read one page, find record 2, return all columns
→ Single I/O operation, extremely fast for known row access
```

**Why columnar storage optimizes for OLAP:**
```
COLUMNAR STORAGE (Parquet/Snowflake internal)
user_id column:  [1, 2, 3, 4, 5, ... 10M]
balance column:  [100, 200, 300, 400, 500, ... 10M]
city column:     [NYC, LA, NYC, Chicago, NYC, ...]

Query: SELECT city, SUM(balance) FROM users GROUP BY city
→ Read ONLY city and balance columns
→ Skip user_id, name, email entirely
→ Columnar compression: city has only ~50 unique values → 200:1 compression
→ 99% I/O reduction vs row storage for this query
```

## OLAP Cube vs Columnar Warehouse

Traditional OLAP cubes (SSAS, Hyperion) pre-aggregate data along all dimension combinations. A "cube" physically materializes every combination of (product, region, time) with pre-computed totals.

```
TRADITIONAL CUBE
  Dimensions: Product (100), Region (50), Month (24)
  Pre-computed cells: 100 × 50 × 24 = 120,000 aggregations
  Query: "Sales for Product A, Region West, Jan 2024" → instant lookup
  Problem: 
    - Adding a new dimension explodes the cube size exponentially
    - Pre-computation time grows with dimension cardinality
    - Inflexible: queries must fit the pre-defined dimension hierarchy

MODERN COLUMNAR APPROACH
  No pre-computation
  Store raw fact + dimension data in columnar Parquet files
  Query engine computes aggregation at query time
  Acceleration via: materialized views, result caching, Z-ordering
  
  Query: "Sales for Product A, Region West, Jan 2024"
  → Predicate pushdown: skip all files not matching product/region/month
  → Column pruning: read only sales_amount column
  → Vectorized execution: SIMD CPU instructions process millions of rows/second
  → Result: < 1 second for billions of rows
```

## HTAP (Hybrid Transactional/Analytical Processing)

Emerging pattern where a single system handles both OLTP and OLAP workloads, eliminating the need for a separate analytics database.

**Examples:**
- **Snowflake Hybrid Tables** (Unistore): Row-stored tables in Snowflake for transactional workloads
- **Google AlloyDB / Spanner**: Cloud-native HTAP
- **TiDB**: Open-source HTAP with TiKV (OLTP) + TiFlash (OLAP) storage nodes
- **Databricks Serverless**: OLAP with Delta Lake's ACID giving limited transactional guarantees

**Reality check:** True HTAP at scale is still an engineering challenge. Most production systems still separate OLTP (Postgres/MySQL) from OLAP (Snowflake/Databricks). The cost and complexity of maintaining a single HTAP system often exceeds the benefit for all but the most latency-sensitive analytical workloads.

---

# 7. Data Warehouse vs Data Lake vs Data Lakehouse

## Decision Table

| Factor | Data Warehouse | Data Lake | Data Lakehouse |
|---|---|---|---|
| **Data type support** | Structured SQL only | All types (structured, semi, unstructured) | All types |
| **Storage format** | Proprietary (Snowflake FDN, BigQuery Capacitor) | Open (Parquet, ORC, Avro, raw files) | Open (Parquet + open table format metadata) |
| **Cost** | High (compute + proprietary storage) | Very low (object storage only) | Medium (object storage + compute on demand) |
| **Governance** | Excellent (built-in RBAC, masking) | Poor (no native governance) | Good (Unity Catalog, Iceberg REST catalog) |
| **BI performance** | Excellent (optimized storage, result cache) | Poor (no indexing, raw files) | Good to Excellent (Z-order, liquid clustering) |
| **ML support** | Limited (must export for training) | Excellent (Python/Spark direct access) | Excellent (unified compute for BI + ML) |
| **Flexibility** | Low (schema-on-write only) | High (schema-on-read, any format) | High (schema enforcement + schema evolution) |
| **ACID transactions** | Yes | No | Yes (via Delta/Iceberg/Hudi) |
| **Time travel** | Limited (some products) | No | Yes |
| **Streaming support** | Limited | Via separate tools | Native (Spark Structured Streaming into Delta) |
| **Vendor lock-in** | High | Low | Low |
| **Production risk** | Low (mature, battle-tested) | High (data swamp risk) | Medium (newer, but maturing rapidly) |

## Architecture Diagrams

### Traditional Data Warehouse Architecture
```
Source Systems
  │ (ETL/ELT every hour/day)
  ▼
┌────────────────────────────────────────┐
│         DATA WAREHOUSE                 │
│  ┌─────────────────────────────────┐   │
│  │     Staging Schema              │   │
│  │   (Raw tables, temp)            │   │
│  └─────────────────────────────────┘   │
│  ┌─────────────────────────────────┐   │
│  │     Core/Integration Schema     │   │
│  │   (3NF or Data Vault)           │   │
│  └─────────────────────────────────┘   │
│  ┌─────────────────────────────────┐   │
│  │     Presentation Schema         │   │
│  │   (Star schemas, data marts)    │   │
│  └─────────────────────────────────┘   │
└────────────────────────────────────────┘
  │
  ▼
BI Tools (Tableau, Looker, Power BI)
```

### Data Lake Architecture (and why it becomes a Data Swamp)
```
Source Systems
  │ (Dump everything in)
  ▼
┌────────────────────────────────────────┐
│         DATA LAKE (S3/GCS/ADLS)        │
│  /raw/orders/2024/01/01/               │
│  /raw/events/hour=14/                  │
│  /raw/legacy/old_format/               │
│  /transformed/mystery_table/           │
│  /ml/someone_experiment/               │
│  /test/do_not_delete/                  │
└────────────────────────────────────────┘

Problems:
  ✗ No one knows what /transformed/mystery_table/ is or who owns it
  ✗ No schema enforcement — any format, any schema
  ✗ No ACID — partial writes, no rollback
  ✗ No access control — everyone can read everything
  ✗ No lineage — where did this data come from?
  → Data Lake becomes Data Swamp
```

### Data Lakehouse Architecture
```
Source Systems
  │
  ▼
┌────────────────────────────────────────────────────┐
│              DATA LAKEHOUSE                         │
│                                                     │
│  ┌──────────────────────────────────────────────┐  │
│  │  OPEN TABLE FORMAT METADATA LAYER             │  │
│  │  Delta Lake / Iceberg / Hudi                  │  │
│  │  ─ Transaction log (ACID)                     │  │
│  │  ─ Schema registry                            │  │
│  │  ─ Statistics (min/max, null count)           │  │
│  │  ─ Time travel (snapshot history)             │  │
│  └──────────────────────────────────────────────┘  │
│                    │                                │
│  ┌─────────────────▼────────────────────────────┐  │
│  │  PARQUET DATA FILES (Object Storage)          │  │
│  │  S3 / GCS / ADLS                              │  │
│  └──────────────────────────────────────────────┘  │
│                                                     │
│  COMPUTE ENGINES (all reading same data)            │
│  ┌──────────┐ ┌──────────┐ ┌──────────────────┐   │
│  │  Spark   │ │ Flink    │ │  Trino/Athena    │   │
│  └──────────┘ └──────────┘ └──────────────────┘   │
│  ┌──────────┐ ┌──────────┐ ┌──────────────────┐   │
│  │  dbt     │ │ Databricks│ │  Snowflake EXT  │   │
│  └──────────┘ └──────────┘ └──────────────────┘   │
└────────────────────────────────────────────────────┘
```

---

# 8. Medallion Architecture Deep Dive

## Overview

The Medallion architecture (coined by Databricks) is a data design pattern that organizes data into three progressively refined quality layers: Bronze (raw ingestion), Silver (cleansed/conformed), and Gold (business-ready serving). Each layer has a specific contract, ownership model, and quality guarantee.

```
DATA QUALITY PROGRESSION

Source    Bronze           Silver              Gold
Systems → (Raw/Append) → (Cleansed/Conformed) → (Business Logic)

Quality:  Lowest           Medium              Highest
Trust:    None             Partial             Full
Schema:   Source schema    Conformed schema    Business schema
Access:   Data engineers   Data engineers +    All analysts
          only             data scientists     & business users
Retention:Full history     Full history        Current + recent
          immutable        with corrections
```

## Bronze Layer — Detailed Design

**Purpose:** Faithfully preserve source data exactly as received. Bronze is the system of record for raw ingestion. It must never modify data semantics — only add ingestion metadata.

**Bronze table design:**
```sql
CREATE TABLE bronze.orders_raw (
  -- Raw source columns (preserved exactly)
  order_id        STRING,        -- May be null in bad records
  user_id         STRING,
  amount          STRING,        -- Keep as string — don't cast yet!
  status          STRING,
  items_json      STRING,        -- Embedded JSON string
  
  -- Ingestion metadata (added by pipeline)
  _ingestion_timestamp  TIMESTAMP  DEFAULT CURRENT_TIMESTAMP(),
  _source_file          STRING,    -- Which file/batch/Kafka offset
  _source_system        STRING,    -- "orders-service", "kafka:orders.v2"
  _ingestion_job_id     STRING,    -- Run ID for lineage
  _record_hash          STRING,    -- SHA256 of raw record for dedup
  _is_quarantined       BOOLEAN DEFAULT FALSE  -- Malformed records
)
USING DELTA
PARTITIONED BY (_ingestion_date DATE)
TBLPROPERTIES (
  'delta.appendOnly' = 'true',   -- Immutable! Only inserts allowed
  'delta.enableChangeDataFeed' = 'true'  -- Enable CDC to silver
);
```

**Key Bronze design principles:**
1. **Append-only**: Bronze is immutable. Never update or delete Bronze records. It is the authoritative source of truth for "what arrived when."
2. **Schema-on-read at Bronze, schema-on-write at Silver**: Accept any schema at Bronze (to avoid ingestion failures on schema changes); enforce schema when promoting to Silver.
3. **Preserve bad records**: Don't drop malformed records. Set `_is_quarantined = true` and write them to Bronze anyway. Investigate later.
4. **Always add metadata**: Every Bronze table must know where the data came from, when it arrived, and which pipeline wrote it.

**Handling bad records in Bronze (Databricks Auto Loader):**
```python
(spark.readStream
  .format("cloudFiles")
  .option("cloudFiles.format", "json")
  .option("cloudFiles.schemaLocation", "/checkpoints/orders_schema")
  .option("cloudFiles.inferColumnTypes", "false")  # Keep everything as string
  .option("badRecordsPath", "/bronze/orders_quarantine")  # Segregate bad records
  .load("s3://raw/orders/")
  
  # Add ingestion metadata
  .withColumn("_ingestion_timestamp", F.current_timestamp())
  .withColumn("_source_file", F.input_file_name())
  .withColumn("_record_hash", F.sha2(F.to_json(F.struct("*")), 256))
  
  .writeStream
  .format("delta")
  .outputMode("append")
  .option("checkpointLocation", "/checkpoints/bronze_orders")
  .partitionBy("_ingestion_date")
  .toTable("bronze.orders_raw")
)
```

## Silver Layer — Detailed Design

**Purpose:** Apply business rules, data quality, type casting, deduplication, and schema conformance. Silver data is trustworthy but not yet shaped for specific business use cases.

**Silver table design:**
```sql
CREATE TABLE silver.orders (
  -- Business keys
  order_id          STRING NOT NULL,
  user_id           STRING NOT NULL,
  
  -- Typed, validated business attributes
  amount            DECIMAL(18,4) NOT NULL,
  status            STRING NOT NULL,
  
  -- Parsed nested structures
  item_count        INTEGER,
  total_items_value DECIMAL(18,4),
  
  -- Standardized timestamps
  created_at        TIMESTAMP NOT NULL,
  updated_at        TIMESTAMP NOT NULL,
  
  -- SCD2 / lineage fields
  valid_from        TIMESTAMP NOT NULL,
  valid_to          TIMESTAMP,
  is_current        BOOLEAN NOT NULL DEFAULT TRUE,
  
  -- Technical metadata
  _bronze_ingestion_id  STRING,   -- Lineage back to bronze record
  _silver_processed_at  TIMESTAMP DEFAULT CURRENT_TIMESTAMP(),
  _data_quality_score   FLOAT     -- 0.0-1.0 quality score
)
USING DELTA
CLUSTER BY (order_id)  -- Liquid clustering for efficient point lookups
TBLPROPERTIES (
  'delta.enableChangeDataFeed' = 'true',
  'delta.autoOptimize.optimizeWrite' = 'true'
);
```

**Silver transformation job:**
```python
def bronze_to_silver_orders(processing_date):
    # Read Bronze delta since last checkpoint
    bronze_new = (spark.readStream
                  .format("delta")
                  .option("readChangeFeed", "true")
                  .option("startingVersion", get_last_silver_checkpoint())
                  .table("bronze.orders_raw"))
    
    # Clean, cast, validate
    silver = (bronze_new
      # Skip quarantined records
      .filter(F.col("_is_quarantined") == False)
      
      # Type casting with null on failure
      .withColumn("amount", F.col("amount").cast("decimal(18,4)"))
      .withColumn("created_at", F.to_timestamp("created_at_str", "yyyy-MM-dd HH:mm:ss"))
      
      # Standardize timezone to UTC
      .withColumn("created_at", F.to_utc_timestamp("created_at", "America/Los_Angeles"))
      
      # Validate business rules
      .withColumn("_is_valid_amount", F.col("amount").between(0.01, 999999.99))
      .withColumn("_is_valid_status", F.col("status").isin(VALID_STATUSES))
      
      # Deduplicate (keep latest per order_id)
      .withColumn("_row_num", F.row_number().over(
          Window.partitionBy("order_id").orderBy(F.col("updated_at").desc())
      ))
      .filter(F.col("_row_num") == 1)
      
      # Parse nested JSON
      .withColumn("items", F.from_json("items_json", ITEMS_SCHEMA))
      .withColumn("item_count", F.size("items"))
    )
    
    # MERGE into Silver for CDC/upsert semantics
    (silver.writeStream
      .format("delta")
      .foreachBatch(lambda df, epoch_id: merge_to_silver(df))
      .option("checkpointLocation", "/checkpoints/silver_orders")
      .start())

def merge_to_silver(df):
    df.createOrReplaceTempView("silver_updates")
    spark.sql("""
        MERGE INTO silver.orders AS target
        USING silver_updates AS source
        ON target.order_id = source.order_id AND target.is_current = true
        WHEN MATCHED AND source.updated_at > target.updated_at THEN
          UPDATE SET target.is_current = false, target.valid_to = source.updated_at
        WHEN NOT MATCHED THEN INSERT *
    """)
```

## Gold Layer — Detailed Design

**Purpose:** Business-domain-specific, analytics-ready datasets. Gold tables are owned by business domains, have defined SLAs, are heavily documented, and are the tables analysts and BI tools query.

**Gold table design principles:**
1. **Purpose-built for specific consumers**: A gold table for the finance team reporting on revenue is different from one for the marketing team analyzing acquisition
2. **Denormalized for query performance**: Gold tables often denormalize Silver tables into wide fact tables or pre-aggregated summaries
3. **Semantically rich**: Column names use business language, not technical names
4. **SLA-governed**: Every gold table has a defined freshness SLA (e.g., "updated by 6 AM daily")
5. **Metric-defined**: Business metrics (revenue, conversion rate, churn) are computed in Gold with documented definitions

```sql
-- Gold table: Daily revenue summary for executive dashboard
CREATE TABLE gold.daily_revenue_summary (
  business_date        DATE NOT NULL,
  region               STRING NOT NULL,
  product_category     STRING NOT NULL,
  
  -- Metrics with documented definitions
  gross_revenue        DECIMAL(18,2),  -- Sum of order amounts before discounts
  net_revenue          DECIMAL(18,2),  -- Gross minus discounts and refunds
  order_count          BIGINT,
  unique_customers     BIGINT,
  average_order_value  DECIMAL(18,2),  -- net_revenue / order_count
  
  -- Data lineage
  _sla_target          STRING  DEFAULT '06:00 UTC',
  _last_updated        TIMESTAMP,
  _source_version      BIGINT  -- Delta version of silver.orders used
)
USING DELTA
PARTITIONED BY (business_date);
```

## Data Quality Gates Between Layers

```
QUALITY GATE: Bronze → Silver

MUST PASS:
  □ Row count > 0 (not empty)
  □ No schema drift from expected Bronze schema
  □ Primary key (order_id) completeness > 99.9%
  □ Timestamp parse success rate > 99%
  □ Amount cast success rate > 99%
  
FAILURE ACTION:
  □ Halt Silver pipeline, alert data engineering team
  □ Leave Bronze data in place (immutable)
  □ Send PagerDuty alert with details

QUALITY GATE: Silver → Gold

MUST PASS:
  □ Reconciliation: Silver order_count matches source system count ± 0.1%
  □ Revenue total within expected range (z-score < 3)
  □ All required dimensions present (no orphan fact records)
  □ Freshness: data within last 15 minutes (for streaming Gold)
  
FAILURE ACTION:
  □ Do not publish new Gold data
  □ Leave previous Gold version live (stale but correct is better than fresh but wrong)
  □ Alert data engineers and business data owners
```

---

# 9. Databricks Production Architecture Deep Dive

## Core Architecture Overview

```
┌─────────────────────────────────────────────────────────────────────────┐
│                    DATABRICKS LAKEHOUSE PLATFORM                         │
│                                                                          │
│  ┌──────────────────────────────────────────────────────────────────┐   │
│  │                    UNITY CATALOG                                  │   │
│  │  Catalogs > Schemas > Tables/Views/Functions/Volumes              │   │
│  │  Lineage | Masking | Row Filters | Audit | Access Policies        │   │
│  └──────────────────────────────────────────────────────────────────┘   │
│                                                                          │
│  ┌────────────┐  ┌────────────┐  ┌────────────┐  ┌────────────────┐   │
│  │SQL Warehouse│  │  Cluster   │  │  Job Cluster│  │Serverless Comp.│   │
│  │(BI/SQL)    │  │(Interactive)│  │(Batch/Stream)│  │(Auto-scale)   │   │
│  └────────────┘  └────────────┘  └────────────┘  └────────────────┘   │
│         │               │               │                │              │
│         └───────────────┴───────────────┴────────────────┘              │
│                                    │                                     │
│  ┌──────────────────────────────── ▼ ─────────────────────────────┐    │
│  │               DELTA LAKE STORAGE LAYER                          │    │
│  │  Transaction Log | Parquet Files | Statistics | Z-Order Index   │    │
│  └──────────────────────────────────────────────────────────────────┘   │
│                                    │                                     │
│  ┌─────────────────────────────────▼──────────────────────────────┐    │
│  │              CLOUD OBJECT STORAGE                               │    │
│  │              (S3 / ADLS Gen2 / GCS)                             │    │
│  └────────────────────────────────────────────────────────────────┘    │
│                                                                          │
│  PLATFORM SERVICES:                                                      │
│  Delta Live Tables │ MLflow │ Feature Store │ Auto Loader │ Workflows   │
└─────────────────────────────────────────────────────────────────────────┘
```

## Delta Live Tables (DLT) — Declarative Pipeline Framework

DLT is Databricks' declarative framework for building Medallion pipelines. Instead of writing imperative Spark code with explicit orchestration, you declare the transformation logic and DLT handles scheduling, data quality, error handling, and lineage.

```python
import dlt
from pyspark.sql import functions as F

# BRONZE: Ingest raw orders via Auto Loader
@dlt.table(
    name="orders_raw",
    comment="Raw orders from S3, ingested via Auto Loader",
    table_properties={"quality": "bronze"}
)
def orders_raw():
    return (
        spark.readStream.format("cloudFiles")
        .option("cloudFiles.format", "json")
        .option("cloudFiles.schemaLocation", "/dlt/schema/orders")
        .load("s3://raw/orders/")
        .withColumn("_ingestion_timestamp", F.current_timestamp())
    )

# SILVER: Clean and validate orders
@dlt.table(
    name="orders_cleaned",
    comment="Validated and typed orders",
    table_properties={"quality": "silver"}
)
@dlt.expect_or_drop("valid_order_id", "order_id IS NOT NULL")
@dlt.expect_or_drop("valid_amount", "amount > 0 AND amount < 1000000")
@dlt.expect("valid_status", "status IN ('pending','confirmed','shipped','cancelled')")
def orders_cleaned():
    return (
        dlt.read_stream("orders_raw")
        .withColumn("amount", F.col("amount").cast("decimal(18,4)"))
        .withColumn("created_at", F.to_utc_timestamp("created_at_str", "UTC"))
        .dropDuplicates(["order_id"])
    )

# GOLD: Daily revenue aggregation
@dlt.table(
    name="daily_revenue",
    comment="Daily revenue summary for executive dashboard",
    table_properties={"quality": "gold"}
)
def daily_revenue():
    return (
        dlt.read("orders_cleaned")
        .filter(F.col("status").isin(["confirmed", "shipped", "delivered"]))
        .groupBy(
            F.to_date("created_at").alias("business_date"),
            "region",
            "product_category"
        )
        .agg(
            F.sum("amount").alias("gross_revenue"),
            F.count("order_id").alias("order_count"),
            F.countDistinct("user_id").alias("unique_customers")
        )
    )
```

**DLT expectation modes:**
| Mode | Behavior on Failure |
|---|---|
| `@dlt.expect("name", condition)` | Record violation in metrics; row still passes through |
| `@dlt.expect_or_drop("name", condition)` | Drop violating rows; log to quarantine table |
| `@dlt.expect_or_fail("name", condition)` | Halt entire pipeline on any violation |

## Auto Loader — Production Ingestion

Auto Loader incrementally ingests new files from cloud storage. It is the recommended ingestion mechanism for Bronze layers on Databricks.

**Key features:**
- **File discovery**: Uses cloud storage notifications (SQS/Event Grid) to detect new files — more efficient than listing the entire directory
- **Schema inference + evolution**: Automatically detects schema from sampled files; handles schema evolution with configurable modes
- **Exactly-once semantics**: Tracks processed files in a checkpoint; never processes the same file twice
- **Scalability**: Handles billions of files in cloud storage efficiently

```python
# PRODUCTION AUTO LOADER CONFIGURATION
(spark.readStream
  .format("cloudFiles")
  
  # File format and path
  .option("cloudFiles.format", "parquet")
  .option("cloudFiles.useNotifications", "true")     # SQS-based file discovery (efficient)
  
  # Schema management
  .option("cloudFiles.schemaLocation", "/checkpoints/schema/orders")
  .option("cloudFiles.schemaEvolutionMode", "addNewColumns")  # Add new columns automatically
  .option("cloudFiles.inferColumnTypes", "true")
  
  # Performance
  .option("cloudFiles.maxFilesPerTrigger", "1000")   # Process up to 1000 files per micro-batch
  .option("cloudFiles.maxBytesPerTrigger", "10gb")   # Or 10GB, whichever comes first
  
  .load("s3://raw/orders/")
  
  .writeStream
  .format("delta")
  .outputMode("append")
  .option("checkpointLocation", "/checkpoints/bronze/orders")
  .option("mergeSchema", "true")                     # Accept new columns
  .trigger(availableNow=True)                        # Process all available, then stop (batch mode)
  .toTable("bronze.orders_raw")
)
```

## Liquid Clustering — Next-Generation Data Layout

Liquid clustering replaced Z-ordering and partitioning as the recommended data layout optimization in Databricks. 

**Why liquid clustering over Z-ordering:**
- Z-ordering requires manual `OPTIMIZE ZORDER BY` runs on a schedule
- Z-ordering rewrites all files every time, even unmodified ones
- Liquid clustering is incremental: only newly written files are clustered automatically
- Liquid clustering updates cluster keys without rewriting the entire table

```sql
-- Enable liquid clustering on a Delta table
CREATE TABLE silver.orders (
  order_id STRING,
  user_id  STRING,
  amount   DECIMAL(18,4),
  status   STRING,
  created_at TIMESTAMP
)
USING DELTA
CLUSTER BY (order_id, created_at);  -- Cluster keys

-- No manual OPTIMIZE needed — clustering happens automatically during writes
-- Periodic OPTIMIZE still recommended to compact small files:
OPTIMIZE silver.orders;  -- Will incrementally cluster unoptimized files

-- Change cluster keys without rewriting (liquid clustering advantage)
ALTER TABLE silver.orders CLUSTER BY (user_id, created_at);
```

## Unity Catalog — Enterprise Governance

Unity Catalog is Databricks' unified governance layer. It provides a three-level namespace (catalog.schema.table), fine-grained access control, data lineage, and audit logging across all Databricks workspaces.

```
UNITY CATALOG NAMESPACE HIERARCHY

main (catalog)
├── bronze (schema)
│   ├── orders_raw (table)
│   ├── users_raw (table)
│   └── events_raw (table)
├── silver (schema)
│   ├── orders (table)
│   ├── users (table)
│   └── events (table)
└── gold (schema)
    ├── daily_revenue (table)
    ├── customer_segments (table)
    └── executive_kpis (view)
```

```sql
-- Row-level security filter
CREATE OR REPLACE FUNCTION finance.row_filter_region(region STRING)
  RETURNS BOOLEAN
  RETURN is_member('region_' || region || '_access');

ALTER TABLE gold.daily_revenue
  ADD ROW FILTER finance.row_filter_region ON (region);
-- Now: US team only sees US rows, EU team only sees EU rows

-- Column masking for PII
CREATE OR REPLACE FUNCTION silver.mask_user_email(email STRING)
  RETURNS STRING
  RETURN CASE WHEN is_member('pii_viewers') THEN email
              ELSE CONCAT(LEFT(email,2), '***@', SPLIT(email,'@')[1])
         END;

ALTER TABLE silver.users ALTER COLUMN email SET MASK silver.mask_user_email;
```

## Databricks Workflows vs Apache Airflow

**When to use Databricks Workflows:**
- All pipeline steps run on Databricks (Notebooks, DLT pipelines, SQL, Python)
- Want tight integration with Delta Lake, Auto Loader, DLT
- Teams are Databricks-native and don't need cross-platform orchestration

**When to use Airflow:**
- Pipelines span Databricks AND other systems (dbt Cloud, Snowflake, APIs, Kubernetes jobs)
- Team has existing Airflow infrastructure
- Complex cross-platform dependency management is needed

---

# 10. Open Table Formats: Delta Lake vs Iceberg vs Hudi

## Why Open Table Formats Exist

Before open table formats, analytics data lived in two places:
1. **Warehouses** (Snowflake, BigQuery): ACID, schema enforcement, great governance, but proprietary format and high cost
2. **Data Lakes** (S3 + Parquet files): Cheap, open, flexible, but no ACID, no schema enforcement, no deletes

Open table formats add a metadata layer on top of Parquet files stored in object storage, providing ACID transactions, schema evolution, time travel, and deletes — the warehouse advantages — while keeping data in open, portable Parquet files.

## Architecture Comparison

### Delta Lake

```
DELTA LAKE ARCHITECTURE

S3 Bucket
├── /mytable/
│   ├── _delta_log/          ← Transaction log (JSON + Parquet checkpoint files)
│   │   ├── 00000000000.json  ← First transaction: table creation
│   │   ├── 00000000001.json  ← Second transaction: insert batch 1
│   │   ├── 00000000002.json  ← Third transaction: update some rows
│   │   ├── 00000000010.checkpoint.parquet  ← Checkpoint every 10 commits
│   │   └── _last_checkpoint
│   ├── part-00001.snappy.parquet  ← Data file (added by tx 001)
│   ├── part-00002.snappy.parquet  ← Data file (added by tx 001)
│   └── part-00003.snappy.parquet  ← Data file (added by tx 002, replaces part-00001)

Transaction log entry (simplified):
{
  "add": {"path": "part-00003.snappy.parquet", "stats": {"numRecords": 5000}},
  "remove": {"path": "part-00001.snappy.parquet", "deletionTimestamp": 1704067200000}
}
```

**Delta Lake ACID mechanism:**
- Optimistic concurrency control: Multiple writers can write concurrently; conflicts detected at commit time
- Transaction log is append-only (safe for concurrent writers in S3 via atomic rename or DynamoDB locking)
- SERIALIZE isolation by default; configurable to SNAPSHOT for higher concurrency

### Apache Iceberg

```
ICEBERG ARCHITECTURE

S3 Bucket
├── /mytable/
│   ├── metadata/
│   │   ├── v1.metadata.json           ← Table metadata (schema, partition spec)
│   │   ├── v2.metadata.json           ← Updated metadata after schema evolution
│   │   ├── snap-001.avro              ← Snapshot 1 manifest list
│   │   ├── snap-002.avro              ← Snapshot 2 manifest list
│   │   └── current-metadata.json     ← Pointer to latest metadata
│   ├── data/
│   │   ├── partition=2024-01/
│   │   │   ├── 00001.parquet
│   │   │   └── 00002.parquet
│   │   └── partition=2024-02/
│   │       └── 00003.parquet
│   └── manifests/
│       ├── manifest-001.avro          ← Lists data files in snapshot 1
│       └── manifest-002.avro         ← Lists new files added in snapshot 2

Iceberg metadata hierarchy:
  Catalog (Glue/Hive/REST/Nessie)
    └─► v2.metadata.json (latest metadata file)
           └─► snap-002.avro (manifest list: which manifests are in this snapshot)
                  └─► manifest-002.avro (manifest: which data files are in this manifest)
                         └─► 00003.parquet (actual data file)
```

**Iceberg's key architectural advantage:** Manifest files track file-level statistics. A query for data in 2024-02 reads only `manifest-002.avro` to find relevant files — no directory listing of the entire table needed. For tables with millions of files, this is dramatically faster than Delta's log replay.

### Apache Hudi

```
HUDI ARCHITECTURE

S3 Bucket
├── /mytable/
│   ├── .hoodie/
│   │   ├── hoodie.properties          ← Table configuration
│   │   ├── 20240107120000.commit      ← Completed commit
│   │   ├── 20240107120000.commit.requested
│   │   └── 20240107120000.inflight    ← In-progress (cleaned up on success)
│   ├── partition=2024-01-07/
│   │   ├── base_file.parquet          ← Copy-on-Write: full updated file
│   │   └── log_file.log              ← Merge-on-Read: delta updates in log
│   └── .hoodie_partition_metadata

HUDI STORAGE TYPES:
  Copy-on-Write (COW): 
    Writes create new versions of files with updates applied
    Reads are fast (no merge needed)
    Writes are slow (must rewrite entire file for even a single update)
    
  Merge-on-Read (MOR):
    Updates written to delta log files alongside base Parquet files
    Reads merge base file + delta log at query time (or via compaction)
    Writes are fast
    Reads require merge overhead (unless compacted)
    Compaction converts log files to new base files periodically
```

## Decision Table

| Requirement | Delta Lake | Apache Iceberg | Apache Hudi |
|---|---|---|---|
| **Databricks-first lakehouse** | ✅ Best | ⚠️ Supported | ⚠️ Supported |
| **Multi-engine analytics** | ⚠️ Good (Spark-native, others via connector) | ✅ Best (designed for multi-engine) | ⚠️ Good |
| **Heavy CDC/upserts** | ✅ Excellent (MERGE INTO) | ✅ Excellent (MERGE INTO) | ✅ Best (built for record-level upserts) |
| **Streaming ingestion** | ✅ Excellent (Spark SS) | ✅ Good | ✅ Excellent (MOR) |
| **Batch analytics** | ✅ Excellent | ✅ Excellent | ✅ Good |
| **Schema evolution** | ✅ Excellent | ✅ Best (backward+forward compatible) | ✅ Good |
| **Partition evolution** | ⚠️ Limited (requires rewrite) | ✅ Best (hidden partitioning, no rewrites) | ⚠️ Limited |
| **Time travel** | ✅ Excellent | ✅ Excellent | ✅ Good |
| **Governance** | ✅ Unity Catalog | ✅ REST Catalog, Nessie, Polaris | ⚠️ Improving |
| **Operational complexity** | ✅ Low (Databricks manages) | ⚠️ Medium (catalog required) | ❌ High (COW vs MOR, compaction tuning) |
| **Query engine compatibility** | Spark, Trino (via OSS), Athena, Flink | Spark, Trino, Flink, Presto, Dremio, DuckDB | Spark, Presto, Hive |
| **Best use case** | Databricks platform, unified batch+streaming | Multi-engine open ecosystems | CDC-heavy workloads, near-real-time |

## Partition Evolution — Why Iceberg Wins

This is Iceberg's killer feature. In Delta Lake and Hudi, changing the partition scheme requires rewriting the entire table (e.g., changing from `PARTITIONED BY (year, month)` to `PARTITIONED BY (year, month, day)` as data grows requires rewriting all existing files).

```
ICEBERG HIDDEN PARTITIONING + PARTITION EVOLUTION

-- Initial partition: by month
CREATE TABLE iceberg.orders USING ICEBERG 
PARTITIONED BY (months(order_date));  -- Hidden partition: iceberg computes month internally

-- 6 months later, data volume grew: switch to daily partitioning
ALTER TABLE iceberg.orders 
REPLACE PARTITION FIELD months(order_date) 
WITH days(order_date);

RESULT: 
  - Old data remains in monthly partitions (not rewritten!)
  - New data writes into daily partitions
  - Query engine handles both transparently
  - No downtime, no rewrite cost

DELTA LAKE EQUIVALENT:
  - No built-in partition evolution
  - Must create new table with daily partitions
  - Rewrite all data into new table (expensive, slow)
  - Swap tables with zero-downtime deployment
```

## Time Travel Comparison

```sql
-- DELTA LAKE time travel
SELECT * FROM delta.`s3://mybucket/orders` VERSION AS OF 5;  -- By version
SELECT * FROM delta.`s3://mybucket/orders` TIMESTAMP AS OF '2024-01-01';  -- By date

-- ICEBERG time travel
SELECT * FROM iceberg.orders FOR VERSION AS OF 5;  -- By snapshot ID
SELECT * FROM iceberg.orders FOR TIMESTAMP AS OF '2024-01-01 00:00:00';

-- HUDI time travel
SELECT * FROM hudi_orders WHERE _hoodie_commit_time <= '20240101000000';
-- Note: Hudi time travel is less clean — uses commit time filter
```

## Production Choice Guide

**Choose Delta Lake when:**
- Primary platform is Databricks
- Team wants minimal operational overhead
- Lakehouse operations (DLT, Auto Loader, Workflows) are primary workloads

**Choose Apache Iceberg when:**
- Multi-engine environment (Trino + Spark + Flink reading same data)
- Tables change partition strategy over time (data volume growth)
- Using cloud-native catalogs (AWS Glue, Google BigLake, Snowflake Iceberg tables)
- Open ecosystem and portability are priorities

**Choose Apache Hudi when:**
- Extremely high-frequency upsert/CDC workload (millions of record updates per second)
- Near-real-time serving requirements with MOR tables
- Already deeply invested in Hudi ecosystem

**Production reality (2024-2025):** Iceberg is rapidly becoming the industry standard open format, especially in multi-cloud environments. AWS, Google Cloud, Snowflake, and Databricks all support Iceberg natively. Delta Lake remains the best choice within the Databricks ecosystem. Hudi's market share is declining as Iceberg and Delta cover its use cases.
