# The Top 0.1% Data Engineer — Complete Field Manual (2025–2026)

> This document covers every major paradigm, pattern, tool, and depth of knowledge that separates elite data engineers from the rest. Organised by domain. Each section goes from concept → mechanics → production reality → what experts actually know.

---

## Table of Contents

1. [Foundations — What Makes the 0.1%](#1-foundations)
2. [Storage Architectures](#2-storage-architectures)
3. [Data Modelling](#3-data-modelling)
4. [Pipeline Patterns — Batch](#4-pipeline-patterns--batch)
5. [Pipeline Patterns — Streaming & Real-Time](#5-pipeline-patterns--streaming--real-time)
6. [Compute Engines](#6-compute-engines)
7. [OLAP & Serving Layers](#7-olap--serving-layers)
8. [Orchestration](#8-orchestration)
9. [Data Quality & Contracts](#9-data-quality--contracts)
10. [Data Observability](#10-data-observability)
11. [Data Governance, Cataloguing & Lineage](#11-data-governance-cataloguing--lineage)
12. [Cloud Platforms (AWS / GCP / Azure)](#12-cloud-platforms)
13. [DataOps & CI/CD for Data](#13-dataops--cicd-for-data)
14. [Data Mesh & Platform Thinking](#14-data-mesh--platform-thinking)
15. [Feature Engineering & ML Pipelines](#15-feature-engineering--ml-pipelines)
16. [Performance Engineering & Cost Optimisation](#16-performance-engineering--cost-optimisation)
17. [Security, Privacy & Compliance](#17-security-privacy--compliance)
18. [System Design — Senior Patterns](#18-system-design--senior-patterns)
19. [Soft Skills & Engineering Mindset](#19-soft-skills--engineering-mindset)
20. [The Expert's Tech Radar (2025–2026)](#20-the-experts-tech-radar-20252026)

---

## 1. Foundations

### What separates the 0.1%

Most data engineers can build a pipeline. Elite data engineers design **data systems** — ones that are correct, observable, cheap to run, and cheap to change.

The key mental shift:

| Average DE | Elite DE |
|---|---|
| Builds the pipeline the ticket asks for | Asks *why* the pipeline is needed |
| Treats data quality as someone else's problem | Owns data quality from source to consumer |
| Uses the tool they know | Picks the right tool for the constraint |
| Fixes broken pipelines reactively | Designs pipelines that fail loudly and recover cleanly |
| Writes SQL / Python | Thinks in data contracts, schemas, and SLAs |
| Works on tasks | Works on outcomes |

### Core first-principles every expert must own

- **Distributed systems fundamentals** — CAP theorem, eventual consistency, exactly-once vs at-least-once delivery, idempotency. Every streaming decision bottoms out here.
- **Storage fundamentals** — row vs columnar storage (why Parquet/ORC is faster for analytics), compression codecs (Snappy vs ZSTD), file size tradeoffs (too-small = metadata overhead, too-large = poor parallelism).
- **Compute models** — shared-nothing (Spark/Trino), push vs pull query execution, vectorised execution engines (DuckDB, Arrow).
- **Networking costs in the cloud** — egress, cross-AZ traffic, co-location. Cost engineering starts here.
- **Schema evolution** — backward/forward/full compatibility. If you can't evolve schemas safely, you can't run at production scale.

---

## 2. Storage Architectures

### 2.1 Data Warehouse

**What it is:** A structured, governed repository optimised for analytical queries. Data arrives transformed and modelled. Write-once, query-many.

**Key concepts:**
- Columnar storage engines — query only the columns needed, massive I/O reduction
- MPP (Massively Parallel Processing) — queries split across many nodes
- Storage–compute separation (Snowflake, BigQuery) — scale each independently
- Concurrency handling — multi-cluster warehouses, materialized result caching
- Cost model — bytes scanned (BigQuery), credit consumption (Snowflake), slot usage (BigQuery reservations)

**Production reality:**
- Clustering keys in Snowflake reduce micro-partition scans. Wrong clustering = full table scans = bill shock.
- BigQuery slot reservations vs on-demand: on-demand is fine for ad-hoc; reservations are mandatory for predictable SLAs at scale.
- Partition pruning in Redshift requires SORTKEY alignment with your most common filter predicates.

**Elite knowledge:**
- Snowflake's micro-partition architecture and how metadata pruning works
- BigQuery's Dremel paper internals — columnar nested data, record shredding
- Materialized views with incremental refresh vs full refresh tradeoffs
- Zero-copy cloning for dev/test environments (Snowflake feature few use well)

---

### 2.2 Data Lake

**What it is:** Object storage (S3 / GCS / ADLS) holding raw and processed data in open file formats. Cheap, infinitely scalable, no compute coupling.

**Key formats:**
- **Parquet** — columnar, dictionary encoding, row-group statistics, predicate pushdown. The universal standard.
- **ORC** — similar to Parquet; better in Hive ecosystem; ACID support in older stacks.
- **Avro** — row-based, schema-in-file, ideal for Kafka serialisation.
- **Delta Lake / Apache Iceberg / Apache Hudi / Apache Paimon** — table formats *on top of* Parquet that add ACID, time travel, schema evolution. (See 2.3)

**The "data swamp" problem:** Without governance, cataloguing, and schema management, a data lake becomes unqueryable. The solution is metadata management + table formats.

---

### 2.3 Data Lakehouse & Open Table Formats

**What it is:** The lakehouse brings warehouse-grade reliability (ACID, schema enforcement, query performance) onto object storage, removing the need to copy data into a warehouse.

**The four open table formats — deep comparison:**

| Feature | Delta Lake | Apache Iceberg | Apache Hudi | Apache Paimon |
|---|---|---|---|---|
| **Philosophy** | Spark-native simplicity | Engine interoperability | Streaming upserts | Unified batch + stream |
| **ACID** | Yes | Yes | Yes | Yes |
| **Time travel** | Yes (version + timestamp) | Yes (snapshots) | Yes | Yes |
| **Schema evolution** | Good | Excellent (hidden partitioning) | Good | Good |
| **Merge-on-Read** | Yes (Deletion Vectors) | Yes | Native (MoR table type) | Yes |
| **Copy-on-Write** | Default | Default | Yes (CoW table type) | Default |
| **Engine support** | Databricks-first, Spark | Spark, Flink, Trino, Hive, DuckDB | Spark, Flink | Flink-first |
| **Best for** | Databricks lakehouse | Multi-engine, open ecosystems | CDC-heavy, upsert workloads | Streaming lakehouse (Flink) |

**Production reality:**
- CoW (Copy-on-Write): every write rewrites entire files — fast reads, slower writes. Best for batch.
- MoR (Merge-on-Read): writes to delta/log files, merges at read time — fast writes, slightly slower reads. Best for CDC/streaming.
- **Compaction** is mandatory in production. Without it, too many small files kill read performance. Automate compaction jobs (Delta OPTIMIZE, Iceberg `rewriteDataFiles`, Hudi compaction).
- **Z-Ordering / Sorting** co-locates similar column values in files, enabling data skipping — critical for multi-column filter predicates.
- **Deletion Vectors** (Delta 3.x, Iceberg v2 row-level deletes) allow deleting rows without rewriting whole files — a major performance improvement for GDPR right-to-erasure use cases.

**Expert knowledge:**
- Iceberg's hidden partitioning: partition transforms (identity, bucket, truncate, year/month/day/hour) are stored in metadata, not in the file path. No partition path leaks into queries, unlike Hive-style partitioning.
- Delta's transaction log (\_delta\_log): every operation is a JSON/Parquet commit file. Checkpointing happens every 10 commits. Understanding this is critical for debugging corrupt tables.
- Hudi's timeline and markers: how it tracks in-flight writes and achieves crash consistency.

---

### 2.4 Medallion Architecture

**What it is:** A multi-tier quality pattern layered on top of a lakehouse. The standard at Databricks and widely adopted.

```
Raw sources
    ↓
BRONZE  — Raw ingestion. Exact copy of source. Append-only. No transformation.
            Schema-on-read. Preserve everything. This is your replay layer.
    ↓
SILVER  — Cleaned, deduplicated, type-cast, joined/enriched.
            Schema-on-write. Business rules applied. Still granular (row-level).
    ↓
GOLD    — Aggregated, modelled business entities or metrics.
            Optimised for consumption by BI tools, analysts, ML.
```

**Why it matters:**
- Bronze gives you full reprocessing capability — if downstream logic is wrong, replay from Bronze.
- Silver is the engineering layer — SLA lives here (data quality checks, deduplication).
- Gold is the business layer — owned jointly by DE + analytics engineers (dbt lives here).

**Common mistakes:**
- Skipping Bronze and going straight to Silver — you lose auditability and can't reprocess.
- Putting business logic in Bronze — no, Bronze is raw, always.
- Not partitioning Bronze by ingestion date — makes reprocessing ranges painful.

---

## 3. Data Modelling

### 3.1 Kimball Dimensional Modelling

**What it is:** Bottom-up approach. Build fact and dimension tables per business process. Star schema or snowflake schema. The dominant model for BI-facing warehouses.

**Key components:**
- **Fact table** — numeric measurements of a business event (orders, clicks, transactions). Contains foreign keys to dimensions. Grain is the most critical design decision.
- **Dimension table** — descriptive context (customer, product, date, location). Wide, denormalised.
- **Conformed dimensions** — dimensions shared across multiple fact tables (the Date dimension used by Sales and Marketing facts).
- **Surrogate keys** — integer PKs in the warehouse, decoupled from source system natural keys.

**Slowly Changing Dimensions (SCD):**
| Type | Behaviour | Use case |
|---|---|---|
| SCD Type 1 | Overwrite — no history | Typo fixes, non-historical attributes |
| SCD Type 2 | New row per change — full history | Customer address, product category changes |
| SCD Type 3 | Add column for previous value | Limited history, current + previous only |
| SCD Type 4 | History table separate from current | High-change-rate dimensions |
| SCD Type 6 | Hybrid of 1+2+3 | Current flag + previous value + row-per-change |

**Expert knowledge:**
- **Grain declaration is non-negotiable.** "One row per order line item" vs "one row per order" changes everything downstream. Never start modelling without declaring grain.
- Factless fact tables — facts that record the *occurrence* of an event with no measure (student enrolled in course). Essential for coverage and eligibility analysis.
- Accumulating snapshot fact tables — one row per process instance (loan application), updated as the instance progresses. Requires date role-playing and multiple foreign keys to the date dimension.
- Bridge tables for many-to-many relationships — avoid the fan-out trap (inflated metrics from cross joining).

---

### 3.2 Inmon / 3NF / EDW

**What it is:** Top-down. Build a normalised enterprise data warehouse first (3NF), then derive departmental data marts from it.

- Strong data integrity, no redundancy
- Harder for business users to query directly
- Data marts derived via ETL from the EDW
- Better for single source of truth at enterprise scale
- More up-front design effort

**When to use over Kimball:** Large enterprises with many source systems that need integration before analytics. Often layered: Inmon EDW as an integration layer, Kimball marts as the consumption layer.

---

### 3.3 Data Vault 2.0

**What it is:** A highly scalable, auditable modelling methodology designed for enterprise-scale integration and historisation. Combines elements of Inmon and Kimball.

**Three core entities:**
- **Hub** — unique list of business keys (customer ID, product SKU). No attributes. Immutable.
- **Link** — relationships between hubs (order contains product). Junction table.
- **Satellite** — descriptive attributes and their full history (customer name, address over time). Append-only.

**Why Data Vault over Kimball:**
- Full auditability — every row has load timestamp and record source
- Parallel loading — hubs, links, satellites load independently with no blocking
- Schema-flexible — adding a new source or attribute = add a satellite, don't redesign
- Handles late-arriving data and multiple source systems cleanly

**Production reality:**
- Data Vault is the raw vault (integration layer). You still build business vault + information mart on top for consumption.
- Hash keys are generated from business keys for performance and portability (no dependency on source system sequences).
- dbt + Data Vault: use packages like `dbt_datavault4dbt` or `automate_dv` for vault pattern macros.
- Data Vault 2.0 adds: business vault (soft rules applied), computed satellites, point-in-time (PIT) tables, bridge tables.

---

### 3.4 One Big Table (OBT) & Modern Patterns

**What it is:** Denormalised, wide flat table. Everything pre-joined. Controversial but increasingly practical at cloud scale with columnar engines.

- Eliminates expensive joins at query time
- Easy for analysts and BI tools
- Write amplification on updates
- Best for high-read, low-write fact-type tables
- DuckDB, ClickHouse, BigQuery handle wide tables efficiently

**When to use:** When query speed for analysts matters most and updates are infrequent. Not appropriate as an enterprise integration pattern.

---

## 4. Pipeline Patterns — Batch

### 4.1 ETL (Extract, Transform, Load)

**What it is:** Transform data *before* loading into the destination. Classic pattern for on-premises warehouses with limited compute.

```
Source → [Extract] → Staging → [Transform in middleware] → [Load] → Warehouse
```

**Tools:** Apache Spark, AWS Glue, Informatica, Talend, custom Python.

**When it still makes sense:**
- When you need to reduce data volume before loading (network or licence cost constraints)
- When the transformation requires external lookups or ML model scoring mid-pipeline
- When the destination doesn't have compute for transformation

---

### 4.2 ELT (Extract, Load, Transform)

**What it is:** Load raw data first, transform inside the cloud warehouse using SQL. The dominant modern pattern.

```
Source → [Extract] → Raw table in warehouse → [Transform with SQL/dbt] → Modelled tables
```

**Why ELT won:**
- Cloud warehouses are cheap and infinitely scalable — use their compute
- SQL is accessible to analytics engineers, not just data engineers
- Versioned, testable, documented with dbt
- Separation of concerns: ingestion team ≠ transformation team

**dbt (data build tool) — the standard transformation layer:**
- Every model is a `SELECT` statement, compiled to SQL
- Materialisation types: `table`, `view`, `incremental`, `ephemeral`, `materialized_view`
- `incremental` models — only process new/changed rows. Requires a unique key + updated_at strategy.
- Ref function — `{{ ref('model_name') }}` builds DAG lineage automatically
- Sources — declare raw tables as dbt sources, add freshness checks
- Tests — `not_null`, `unique`, `accepted_values`, `relationships` built-in; custom tests in SQL
- Snapshots — SCD Type 2 implementation in dbt, using `check` or `timestamp` strategy
- Macros — Jinja-templated reusable SQL. Advanced use: cross-database compatibility macros.
- Packages — `dbt_utils`, `dbt_expectations` (Great Expectations-style), `codegen`, `audit_helper`
- Exposures — document BI dashboards and their dbt model dependencies
- Semantic Layer / MetricFlow — define metrics in code, decouple metric logic from BI tool

**Expert dbt patterns:**
- **Staging → Intermediate → Marts** layer convention. Staging = 1:1 with source, typed, renamed. Intermediate = joins and business logic. Marts = consumption-ready.
- `on_schema_change` handling in incremental models — `ignore`, `fail`, `append_new_columns`, `sync_all_columns`. Pick wrong and you silently drop columns.
- Using `dbt-audit-helper` to compare model outputs before/after refactors.
- Contract enforcement (dbt 1.5+) — declare column types and constraints, fail CI if violated.

---

### 4.3 Reverse ETL

**What it is:** Warehouse → operational SaaS tools. The warehouse is the system of record; sync computed metrics or segments back to CRM, marketing, product tools.

**Tools:** Census, Hightouch, Polytomic.

**Use cases:**
- Sync customer health score from warehouse to Salesforce
- Push ML-computed churn segments to Braze for personalised campaigns
- Populate in-app personalisation from a warehouse feature table

**Production considerations:**
- Rate limits on destination APIs are the primary constraint
- Idempotency — upserts, not inserts, to handle re-runs
- Column-level sync to avoid overwriting fields owned by the SaaS tool
- Data freshness SLA negotiation with marketing/product teams

---

## 5. Pipeline Patterns — Streaming & Real-Time

### 5.1 The Streaming Landscape

**When you need streaming (not batch):**
- Fraud detection (decision in <1 second)
- Real-time dashboards (< 30 second latency)
- Event-driven microservices synchronisation
- IoT telemetry at high velocity
- CDC — keeping warehouse in sync with OLTP near-real-time

**Key concepts every expert must know:**
- **Event time vs processing time** — when the event *happened* vs when your system *saw* it. Always use event time for analytics. Processing time causes non-reproducible results.
- **Watermarks** — how late can data arrive and still be included? The watermark is your tolerance. Too tight = dropped late data. Too loose = high memory usage and output latency.
- **Windows** — tumbling (fixed, non-overlapping), sliding (overlapping, step < window size), session (activity-based gaps).
- **Exactly-once semantics** — the holy grail. Requires: idempotent producers, transactional writes to the sink, and state checkpointing. True end-to-end exactly-once is hard — even with Flink + Kafka transactions, the handoff between systems creates gaps.
- **State management** — keyed state (per-key), operator state (per-partition), broadcast state (shared across all parallel operators). Unbounded state = OOM eventually. Build TTL (time-to-live) into all stateful operators.
- **Backpressure** — when a downstream operator is slower than upstream, the system must slow ingestion rather than buffer indefinitely. Flink handles this natively. Spark Structured Streaming requires manual configuration.

---

### 5.2 Apache Kafka

**What it is:** Distributed, fault-tolerant, high-throughput event streaming platform. The backbone of most real-time data architectures.

**Architecture internals:**
- **Topics** — logical category of events. Partitioned for parallelism.
- **Partitions** — ordered, immutable log. A consumer reads a partition sequentially. More partitions = more parallelism, but more broker overhead.
- **Offsets** — position of a message in a partition. Consumers commit offsets to track progress.
- **Consumer groups** — each partition assigned to one consumer in a group. Scale consumers = scale partition count.
- **Replication factor** — how many broker copies. `min.insync.replicas` = how many must acknowledge before a write is confirmed.
- **KRaft mode (Kafka 4.0)** — Zookeeper-free. The metadata is now managed by Kafka itself. Operational simplification, faster failover.
- **Log compaction** — instead of time/size-based retention, keep only the latest value per key. Essential for CDC topics where only current state matters.

**Schema Registry (Confluent):**
- Enforces Avro/JSON Schema/Protobuf schemas per topic
- Backward/forward/full compatibility modes
- Prevents producers from breaking consumers with schema changes
- Critical in CDC pipelines and multi-team architectures

**Producer tuning (expert level):**
- `acks=all` — wait for all ISR replicas to acknowledge (durability)
- `linger.ms` — batch window for higher throughput
- `compression.type=lz4 or zstd` — reduce network cost
- `enable.idempotence=true` — deduplicate retries within a session
- `transactional.id` — enable exactly-once across partitions

**Consumer tuning:**
- `max.poll.records` — how many records per poll call. Too high = processing timeout and rebalance.
- `fetch.min.bytes` + `fetch.max.wait.ms` — batch vs latency tradeoff
- `isolation.level=read_committed` — only read committed transactional messages (exactly-once consumer side)

---

### 5.3 Change Data Capture (CDC)

**What it is:** Capture every INSERT, UPDATE, DELETE from a source OLTP database and stream the changes downstream. The only scalable, non-polling way to replicate OLTP state in real-time.

**Mechanism — Log-based CDC:**
- PostgreSQL: WAL (Write-Ahead Log) via logical replication. Debezium uses `pgoutput` plugin.
- MySQL: binlog (binary log). Debezium reads binlog events.
- SQL Server: Change Tracking / CDC tables. Debezium reads CT tables.
- MongoDB: oplog.
- Oracle: LogMiner / XStream.

**Debezium (dominant open-source CDC engine):**
- Runs on Kafka Connect. Each connector = one source DB.
- Publishes change events to Kafka topics (one topic per table by default).
- Change event schema: `op` (c=create, u=update, d=delete, r=read/snapshot), `before` (old row state), `after` (new row state), LSN/offset for ordering.
- **Incremental snapshots** (Debezium 1.6+) — snapshot without locking the table. Uses watermarking to reconcile snapshot chunks with live CDC stream. Production-safe.
- Debezium 2.x: KRaft mode support, improved exactly-once, better connector management.

**Production CDC pitfalls:**
1. **At-least-once, not exactly-once** — Debezium can replay events on restart. Your sink must be idempotent (MERGE/upsert by primary key, not INSERT).
2. **Schema changes in source** — `ALTER TABLE` mid-stream. Debezium handles many cases, but `DROP COLUMN` or rename requires care. Schema Registry + Schema evolution mode is your safety net.
3. **Snapshot + stream alignment** — during initial snapshot, the table is read in chunks. New CDC events arrive during the snapshot. Incremental snapshots handle this; locked snapshots don't.
4. **LSN lag monitoring** — if Kafka Connect falls behind the WAL retention window, you lose events and must re-snapshot. Alert on replication slot lag.
5. **Tombstone events** — Kafka log compaction requires a null-value "tombstone" message after a delete event to enable compaction. Handle in consumers or they crash on null deserialization.

**The canonical modern CDC stack:**
```
PostgreSQL/MySQL
    ↓ WAL/binlog
Debezium (on Kafka Connect)
    ↓
Kafka Topics (one per table) + Schema Registry
    ↓
Apache Flink / Spark Structured Streaming
    ↓
Bronze Delta/Iceberg tables (raw CDC events, append-only)
    ↓
Silver (MERGE/upsert — current state materialised)
    ↓
Gold (aggregated, business-facing)
```

---

### 5.4 Apache Flink

**What it is:** True streaming-first distributed processing engine. Native event time, exactly-once state, millisecond latency. The production choice for complex streaming.

**Why Flink over Spark Structured Streaming:**
- Flink is streaming-first. Spark treats streaming as micro-batches (100ms–seconds latency minimum).
- Flink's state backend (RocksDB) handles large keyed state efficiently. Spark state is in-memory only.
- Flink's watermark model and window triggers are more expressive.
- Flink CDC connectors (Flink 1.18+) can connect directly to databases without needing Kafka as an intermediary.

**Key Flink concepts:**
- **DataStream API** vs **Table API / SQL** — DataStream for low-latency, complex logic. Table API for SQL-based transformations.
- **Checkpointing** — periodically snapshot operator state to durable storage (S3 / HDFS). On failure, restart from last checkpoint. This is the mechanism behind exactly-once.
- **Savepoints** — manually triggered checkpoints for migrations, upgrades, and A/B deployments.
- **Keyed State** — state partitioned by key (e.g., per user_id). Enables aggregations and joins keyed on business entities.
- **Flink SQL** — full SQL on streams. Window TVF (Table-Valued Functions) for tumbling/sliding/cumulate/session windows. Temporal joins for looking up dimension tables at event time.
- **Flink CDC** — `mysql-cdc`, `postgres-cdc`, `mongodb-cdc` source connectors. Exactly-once from source to sink without Kafka. Lower operational overhead for simpler pipelines.

**Flink + Iceberg / Paimon:**
- Flink → Iceberg: streaming writes to Iceberg tables. Commit per checkpoint. Enables streaming lakehouse.
- Apache Paimon: purpose-built streaming lakehouse table format. Flink-native. Handles upserts and partial updates efficiently. Yelp migrated to Flink + Paimon on S3 and cut storage costs by 80%.

---

### 5.5 Spark Structured Streaming

**What it is:** Micro-batch streaming built on Spark. Good when you already run Spark batch jobs and latency requirements are >1–5 seconds.

- Trigger modes: `ProcessingTime` (micro-batch), `Once` (one-shot batch), `AvailableNow` (drain all available data), `Continuous` (experimental, ~1ms latency)
- Checkpointing to S3/HDFS for fault tolerance
- `foreachBatch` sink — run arbitrary code (including MERGE operations) per micro-batch
- Watermarks declared with `withWatermark("event_time", "10 minutes")`
- State store: HDFS-backed, in-memory-first. Large state = spill and performance degradation.

**When Spark Streaming is the right choice:**
- You already have a Spark-based lakehouse (Databricks, EMR)
- Latency of 5–30 seconds is acceptable
- You want unified batch + streaming code (same Spark job, different trigger)

---

## 6. Compute Engines

### 6.1 Apache Spark — Deep Internals

**Why experts understand internals:** The difference between a 10-minute and a 2-hour Spark job is almost always explainable by misunderstanding DAG execution, shuffles, or memory pressure.

**Execution model:**
```
Driver → SparkContext → DAGScheduler → TaskScheduler → Executor Tasks
```
- **Transformation** (lazy) → builds DAG. Nothing runs.
- **Action** (eager) → triggers DAG compilation and execution.
- **Stage** — set of tasks with no shuffle boundary. Each shuffle creates a new stage.
- **Task** — one unit of work on one partition.

**The Catalyst Optimizer pipeline:**
1. **Unresolved Logical Plan** — parsed SQL/DataFrame API
2. **Analysis** — resolve column names, types against catalog
3. **Logical Optimization** — predicate pushdown, projection pruning, constant folding, filter merge
4. **Physical Planning** — choose join strategies (BroadcastHashJoin vs SortMergeJoin), generate physical plan
5. **Code Generation (Tungsten)** — generate bytecode for tight CPU loops

**Adaptive Query Execution (AQE) — Spark 3.x:**
- **Coalescing shuffle partitions** — merges small post-shuffle partitions automatically. Eliminates the need to manually tune `spark.sql.shuffle.partitions`.
- **Dynamic join switching** — switches from SortMergeJoin to BroadcastHashJoin if runtime statistics show one side is small. Saves enormous shuffle cost.
- **Skew join handling** — splits skewed partitions and processes them separately. No more one-partition bottleneck.
- Enable with: `spark.sql.adaptive.enabled=true` (default true in Spark 3.2+).

**Memory model:**
- **Execution memory** — shuffle, sort, aggregation, join operations
- **Storage memory** — cached RDDs/DataFrames
- **Off-heap** — optional, reduces GC pressure for large datasets
- Memory fraction: `spark.memory.fraction` (default 0.6), `spark.memory.storageFraction` (default 0.5 of that)
- OOM errors: usually spill to disk (slow), or if spill disabled, fail. Check `spark.memory.offHeap.enabled` for large joins.

**Shuffle — the root of all performance evil:**
- Shuffle writes intermediate results to disk, then redistributes across all nodes
- `spark.sql.shuffle.partitions` (default 200) — almost always wrong. Use AQE or set based on data volume.
- Salting for skew: add a random integer column to the key, multiply the other side to match.
- Broadcast joins: if one side < `spark.sql.autoBroadcastJoinThreshold` (default 10MB), it's broadcast. Increase carefully — broadcasted data goes to every executor.

**Production tuning checklist:**
- Use `spark.sql.adaptive.enabled=true` (AQE)
- Set `spark.serializer=org.apache.spark.serializer.KryoSerializer`
- Tune executor memory and cores — sweet spot is 4–5 cores per executor, 16–21GB memory
- Avoid `collect()` on large datasets in production code
- Cache only when a DataFrame is reused 2+ times AND fits in memory
- Use `explain(extended=True)` to inspect physical plan before running expensive jobs
- Prefer `DataFrame` API over RDD API — Catalyst + Tungsten don't apply to RDDs

---

### 6.2 Trino / Presto

**What it is:** Distributed SQL query engine. Query-in-place — no data movement. Connects to many sources (Hive, Iceberg, Delta, Postgres, Kafka, Elasticsearch) with a single SQL dialect.

- Push-down predicates to source connectors when possible
- Connector-specific: `filter_pushdown_enabled`, `projection_pushdown_enabled`
- Cost-based optimizer (CBO) uses table statistics — run `ANALYZE TABLE` to populate
- Split generation: how many parallel tasks read from storage. More splits = more parallelism.
- Memory management: `query.max-memory-per-node`, `query.max-total-memory`
- Exchange (shuffle) types: `REPARTITION`, `REPLICATE`, `GATHER`
- Used extensively in the open-source Iceberg ecosystem (Athena = managed Trino on AWS)

---

### 6.3 DuckDB

**What it is:** In-process OLAP engine. Runs embedded in Python/R/Java. No server, no cluster. Vectorised execution. Reads Parquet, CSV, JSON, Iceberg, Delta natively.

**Why it matters for data engineers:**
- Replace Pandas for data transformation — 10–100x faster on multi-GB datasets
- Local development of lakehouse queries without a Spark cluster
- ELT pipelines on Lambda / serverless (no JVM overhead)
- Emerging as the local development engine for dbt (dbt-duckdb adapter)
- `COPY TO` / `READ_PARQUET` — direct reads from S3 with optional Parquet predicate pushdown

---

## 7. OLAP & Serving Layers

### 7.1 OLAP vs OLTP

| | OLTP | OLAP |
|---|---|---|
| **Purpose** | Transactions (write-heavy) | Analytics (read-heavy) |
| **Schema** | Normalised (3NF) | Denormalised (star/wide) |
| **Query pattern** | Point lookups, small row counts | Full or range scans, aggregations |
| **Latency** | Milliseconds | Seconds to minutes |
| **Examples** | PostgreSQL, MySQL, MongoDB | BigQuery, ClickHouse, Druid, Pinot |

---

### 7.2 ClickHouse

**What it is:** Columnar OLAP database. Handles billions of rows, sub-second aggregations. Insert-optimised, no updates by design.

- **MergeTree** table engine — the core. Data written in parts, merged in background.
- **ReplacingMergeTree** — deduplication by version column during merge (eventual, not immediate)
- **AggregatingMergeTree** — materialise partial aggregates, merge them. Power of `AggregatingMergeTree` + `Materialized Views` = real-time pre-aggregation.
- **Distributed table engine** — shards queries across multiple nodes
- Projection — a sorted pre-aggregated copy of data embedded in the same table
- Ordering key is critical — queries that filter/sort on the ordering key are O(log n), not O(n)
- Limitations: no transactions, updates are "mutations" (expensive async rewrites), not for OLTP

---

### 7.3 Apache Druid & Pinot

**Apache Druid:** Real-time OLAP for time-series event data. Sub-second queries on billions of rows. Used at Airbnb, Netflix, Twitter.
- Segments: immutable data chunks indexed per time interval
- Rollup: pre-aggregate at ingest time (reduces storage and query cost)
- Native support for approximate aggregations (HyperLogLog, quantiles)

**Apache Pinot:** LinkedIn's real-time OLAP. Low-latency (single-digit ms) on fresh data.
- Used for user-facing analytics (show me how many people viewed my post in the last 5 minutes)
- Real-time tables (from Kafka) + offline tables (from Hadoop/S3) unified

---

### 7.4 Semantic Layer / Metrics Layer

**What it is:** A centralised place where metric definitions (revenue, DAU, churn rate) live in code. Decouples how metrics are calculated from how they are queried.

**Tools:** dbt Semantic Layer (MetricFlow), Cube.dev, LookML (Looker), AtScale.

**Why it matters:**
- Single definition of "revenue" — no more four different numbers from four different dashboards
- Metrics change once, propagate everywhere
- Can be queried via SQL, REST API, or BI tools
- The foundation for AI/LLM analytics (natural language → metric query)

---

## 8. Orchestration

### 8.1 Apache Airflow

**What it is:** The most widely deployed workflow orchestrator. DAGs (Directed Acyclic Graphs) defined in Python. Task dependencies, scheduling, retries, alerting.

**Expert-level Airflow:**
- **Executor types:** LocalExecutor (single node), CeleryExecutor (distributed workers with Redis/RabbitMQ queue), KubernetesExecutor (each task = ephemeral pod). KubernetesExecutor is the production standard for scalability and isolation.
- **Dynamic DAGs:** `@task` decorator + TaskFlow API. Generate tasks programmatically from config or DB query.
- **XCom:** inter-task communication. Avoid passing large data via XCom — it stores in the metadata DB. Use S3/GCS for data handoff, XCom for small references.
- **Dataset-driven scheduling (Airflow 2.4+):** trigger a DAG when a dataset is updated by another DAG. Decoupled, event-driven pipelines.
- **Deferrable operators:** release a worker slot while waiting for an external event (S3 file, SQL query result). Massive cost saving at scale.
- **Pools:** limit concurrent task instances to prevent overwhelming downstream systems.
- **SLAs:** define expected task completion time. Airflow emails/alerts if breached.
- **Backfill:** re-run a DAG for a historical date range. Design idempotent tasks — backfill will break non-idempotent pipelines.

**Common anti-patterns:**
- Logic in the DAG file (imports, API calls) — runs at parse time, not execution time. Moves scheduling overhead to the scheduler.
- Passing DataFrames through XCom — crashes the metadata DB
- No retries configured — transient failures cascade
- Monolithic DAGs — one DAG doing 50 tasks with no modularity

---

### 8.2 Prefect & Dagster

**Prefect:**
- Python-native, flow = function. Lower boilerplate than Airflow.
- Hybrid execution: Prefect Cloud schedules, your infra runs.
- Results caching, automatic state management.
- Better for teams who want less YAML/config overhead.

**Dagster:**
- Asset-based orchestration. Model *data assets*, not just tasks.
- Software-defined assets (SDA): a Python function that produces a data asset. Dagster understands lineage, partitions, freshness.
- Asset checks: data quality as first-class citizens in the orchestration layer.
- Best for teams thinking in data products, not just pipelines.
- Tighter integration with dbt, Spark, Fivetran.

**Trend:** The industry is moving toward asset-based orchestration (Dagster pattern). Airflow remains dominant by install base but is being challenged in greenfield projects.

---

## 9. Data Quality & Contracts

### 9.1 Data Quality Dimensions

| Dimension | What it checks |
|---|---|
| **Completeness** | Are required fields null? Is the expected volume present? |
| **Uniqueness** | Duplicate rows / duplicate primary keys |
| **Validity** | Values within expected domain (date ranges, enum values, regex patterns) |
| **Consistency** | Cross-table referential integrity, sum checks vs source system |
| **Timeliness** | Data arrived within the SLA window |
| **Accuracy** | Values match the ground truth source |

### 9.2 Tools

- **dbt tests** — lightweight, SQL-based, run in CI and on schedule. Covers most cases.
- **Great Expectations (GX)** — Python-native expectation suites. More expressive than dbt tests. Generates data docs as human-readable quality reports. Good for complex validation logic.
- **Soda** — declarative YAML-based quality checks. SodaCloud for monitoring over time.
- **Re_data** — dbt-native data monitoring using dbt macros.

### 9.3 Data Contracts

**What they are:** A formal agreement between data producers and consumers defining the schema, semantics, quality, and SLA of a dataset. The most impactful emerging practice in mature data engineering.

**A data contract specifies:**
- Schema (column names, types, nullability)
- Semantics (what does `revenue` mean? Is it net or gross? Which currency?)
- SLA (freshness: data is updated every 15 minutes. Availability: 99.9%)
- Quality rules (no nulls in user_id, transaction_amount > 0)
- Ownership (who to call when it breaks)
- Versioning and breaking change policy

**Implementation approaches:**
- YAML/JSON spec files stored in Git, validated in CI
- dbt contracts (1.5+) — enforce column types and `not null` at model compile time
- Tools: Schemata, Bitol (ODCS spec), OpenDataContract
- Schema Registry (Kafka) — enforces schema contracts at the event level for streaming

**Why it matters at scale:**
Without contracts, every pipeline change is a potential breaking change for unknown downstream consumers. Contracts invert this — producers must honour contracts; breaking changes require versioning and deprecation notices.

---

## 10. Data Observability

**What it is:** The ability to understand the health, freshness, volume, schema, and lineage of your data at all times — automatically.

**The five pillars (Monte Carlo model):**
1. **Freshness** — when was this table last updated? Is it within SLA?
2. **Volume** — is the row count within expected range? Sudden drops or spikes.
3. **Distribution** — have the statistical distributions of column values changed? (Null rate, min/max, mean shifted)
4. **Schema** — were columns added, removed, or changed without notice?
5. **Lineage** — which upstream tables or pipelines feed this table? Which downstream assets depend on it?

**Tools:**
- **Monte Carlo** — market leader. ML-powered anomaly detection on all five pillars. Field-level lineage. Auto-monitors without rule writing. Named Databricks 2025 Governance Partner of the Year.
- **Acceldata** — pipeline-level + data-level observability, cost monitoring.
- **Datafold** — diff-based: compare data between runs or environments. Excellent for dbt PR validation.
- **re_data / Elementary** — dbt-native observability packages.
- **Lightdash / Metaplane** — lighter-weight options.

**Expert patterns:**
- Treat data reliability as an SLA. Define incident SLOs: freshness breach = P2 incident within 30 minutes.
- Column-level lineage: when a downstream report breaks, trace which upstream column caused it. Table-level lineage is insufficient for root cause analysis.
- Integrate observability into CI: run data diff on dbt PRs before merge using Datafold.
- Circuit breakers: if data quality checks fail, stop the downstream pipeline rather than propagating bad data.

---

## 11. Data Governance, Cataloguing & Lineage

### 11.1 Data Catalogue

**What it is:** A searchable inventory of all data assets — tables, columns, pipelines, metrics, dashboards — with metadata, ownership, and quality context.

**Tools:**
- **DataHub (LinkedIn)** — open-source. Push + pull metadata ingestion. Rich lineage. Python SDK. Active community. Best open-source option.
- **Apache Atlas** — Hadoop-era. Still used in enterprise but losing ground.
- **Alation, Collibra, Atlan** — enterprise commercial options with stewardship workflows.
- **OpenMetadata** — newer open-source option, strong UI, built-in data quality.
- **Unity Catalog (Databricks)** — lakehouse-native governance. Column-level security, row filtering, attribute-based access control (ABAC). The de facto standard for Databricks environments.
- **AWS Glue Data Catalog** — AWS-native, used by Athena, EMR, Glue ETL.

### 11.2 Data Lineage

**Column-level lineage** — the gold standard. Knowing that `gold.revenue_report.net_revenue` is derived from `silver.orders.amount` minus `silver.refunds.amount` which came from CDC events from `postgres.orders`.

**OpenLineage** — open standard for lineage metadata. Facets (job, run, dataset). Integrates with Airflow, Spark, dbt, Flink. Backend-agnostic. Store to Marquez (open-source) or DataHub.

### 11.3 Access Control & Security

- **Column-level security** — mask or filter PII columns per user/role
- **Row-level security** — filter rows based on the querying user's attributes (only see your region's data)
- **Attribute-based access control (ABAC)** — tags on columns (PII, SENSITIVE) drive access policies automatically
- **Dynamic data masking** — show `****` for credit card numbers to non-privileged users
- Unity Catalog, Snowflake RBAC, BigQuery column-level security, Apache Ranger (Hadoop)

---

## 12. Cloud Platforms

### 12.1 AWS Data Engineering Stack

| Layer | Service |
|---|---|
| Object storage | S3 |
| Table format | Apache Iceberg (Glue, Athena, EMR) |
| Batch ETL | AWS Glue (serverless Spark) |
| Stream ingestion | Kinesis Data Streams, MSK (Managed Kafka) |
| Stream processing | Kinesis Data Analytics (Flink), EMR Spark |
| Warehouse | Redshift, Athena (Trino on S3) |
| Orchestration | MWAA (Managed Airflow), Step Functions |
| Catalogue | Glue Data Catalog |
| ML | SageMaker, SageMaker Feature Store |

**Expert AWS knowledge:**
- S3 request costs: LIST operations are expensive at scale. Use S3 Inventory instead of listing for auditing.
- Iceberg on Athena: `OPTIMIZE` and `VACUUM` via Athena to manage file compaction and snapshot expiry.
- Redshift Spectrum: query S3 directly from Redshift — hybrid warehouse + lake without data copy.
- EMR on EC2 vs EMR Serverless vs Glue: cost and operational tradeoffs. Glue = serverless, no cluster management, higher cost per DPU. EMR = control, lower cost at scale, more ops overhead.
- AWS Lake Formation: centralised permissions on top of Glue Catalog. Cell-level security, cross-account data sharing.

---

### 12.2 GCP Data Engineering Stack

| Layer | Service |
|---|---|
| Object storage | GCS |
| Warehouse | BigQuery |
| Batch ETL | Dataflow (Apache Beam), Dataproc (Spark) |
| Stream ingestion | Pub/Sub |
| Stream processing | Dataflow (unified batch+stream via Beam) |
| Orchestration | Cloud Composer (Airflow), Workflows |
| Catalogue | Dataplex, Data Catalog |

**Expert GCP knowledge:**
- BigQuery storage: active storage (last 90 days) vs long-term storage (>90 days) — 50% cost reduction for cold data automatically.
- Partition + cluster strategy: partition on a date column, cluster on high-cardinality filter columns (user_id, product_id). Clustering is free in BigQuery.
- BigQuery reservations vs on-demand: on-demand charges per byte scanned, reservations are flat slots. Break-even is ~2TB/day scanned.
- BigQuery Omni: query data in AWS S3 or Azure Blob from BigQuery SQL. Cross-cloud analytics.
- Dataflow Flex Templates: package a Beam pipeline as a Docker container. Parameterise and deploy from CI/CD.

---

### 12.3 Azure Data Engineering Stack

| Layer | Service |
|---|---|
| Object storage | ADLS Gen2 (Azure Data Lake Storage) |
| Warehouse | Synapse Analytics, Fabric |
| Batch ETL | Azure Data Factory, Synapse Pipelines |
| Stream ingestion | Event Hubs (Kafka-compatible) |
| Stream processing | Azure Stream Analytics, HDInsight (Spark) |
| Lakehouse | Microsoft Fabric (OneLake) |
| Orchestration | ADF, Fabric Pipelines |

**Microsoft Fabric (2024–2025):** Microsoft's unified analytics platform. OneLake as a single logical data lake across tenants. Delta Lake format native. Spark, SQL, Power BI, Data Factory all integrated. Rapidly gaining enterprise adoption. Worth knowing for enterprise DE roles.

---

## 13. DataOps & CI/CD for Data

**What it is:** Apply software engineering practices — version control, testing, CI/CD, infrastructure as code — to data pipelines and data assets.

### 13.1 Version Control for Data

- **dbt projects** in Git — every model, test, macro is code. PRs, code review, branching.
- **Airflow DAGs** in Git — DAGs are Python, deploy via CI/CD to the DAGs folder.
- **Terraform / Pulumi** for cloud infrastructure: BigQuery datasets, Snowflake warehouses, IAM roles, Kafka topics — all as code.
- **Schema Registry** for Kafka schemas — schemas are committed to Git and pushed to registry in CI.

### 13.2 Testing Strategy

```
Unit tests      → Test individual dbt macros, Python transformation functions in isolation
Integration     → Test that pipeline produces correct output on a sample dataset
Contract tests  → Validate schema, nullability, referential integrity
Data diff tests → Compare output between old and new pipeline version (Datafold)
Freshness tests → Assert data arrived within SLA
```

### 13.3 CI/CD Pipeline for Data

```
PR opened
    → dbt compile (syntax check)
    → dbt test on dev schema (unit + integration tests)
    → Datafold data diff (compare prod vs new model output)
    → Schema compatibility check (Avro/Protobuf schema registry)
    → Terraform plan (infra changes preview)
Merge to main
    → dbt run on staging
    → Integration tests on staging
    → Terraform apply
    → dbt run on production (blue-green or incremental)
    → Smoke tests
    → Alert if SLA breached within 30 minutes
```

### 13.4 Infrastructure as Code

- **Terraform** — declarative IaC for all cloud resources. Data engineering resources: S3 buckets, IAM roles, Glue jobs, Kinesis streams, BigQuery datasets, Snowflake warehouses.
- **Helm / Kubernetes** — deploy Airflow, Kafka, Spark on K8s. Helm charts for Airflow (official), Kafka (Bitnami).
- **Docker** — standardise Python environments across local dev, CI, and production. Avoid "works on my machine" for Spark jobs.

---

## 14. Data Mesh & Platform Thinking

### 14.1 Data Mesh Principles

**What it is:** An organisational and architectural approach that treats data as a product, owned by the domain that produces it. Four principles (Zhamak Dehghani):

1. **Domain ownership** — the team that produces the data owns the pipeline, quality, and SLA. No central data team bottleneck.
2. **Data as a product** — each domain publishes data products with contracts, documentation, quality guarantees, and versioning. Consumers subscribe.
3. **Self-serve data platform** — a central platform team provides infrastructure primitives (storage, compute, cataloguing, access control) that domain teams use without platform team involvement.
4. **Federated computational governance** — global policies (security, compliance, interoperability) are enforced by the platform, not by a central governance committee.

**What a platform DE builds in data mesh:**
- The self-serve infrastructure: opinionated templates for data pipelines (Terraform modules, dbt project scaffolding), cataloguing integrations, CI/CD pipelines.
- Data product templates: standardised interfaces for publishing and consuming data products.
- Governance automation: automated PII tagging, access control policy enforcement, lineage collection.

**Reality check:** Data mesh is an organisational change as much as a technology change. It requires domain teams to have data engineering skills, which most don't. It adds coordination overhead. Best suited to large organisations with many mature engineering teams.

---

## 15. Feature Engineering & ML Pipelines

### 15.1 Feature Store

**What it is:** A centralised system for creating, storing, versioning, and serving ML features. Solves the "training-serving skew" problem (feature computed differently in training vs production).

**Components:**
- **Offline store** — historical features for model training. Typically a data warehouse or lakehouse. Point-in-time correct lookups (as-of join) are critical — only use feature values that were available at the time of prediction.
- **Online store** — low-latency (< 10ms) feature serving for real-time inference. Redis, DynamoDB, Cassandra.
- **Feature registry** — catalogue of feature definitions, owners, lineage.
- **Materialisation jobs** — batch or streaming jobs that compute and write features to both stores.

**Tools:**
- **Feast** — open-source, cloud-agnostic. Python-first.
- **Tecton** — enterprise managed feature platform. Streaming + batch, monitoring included.
- **Hopsworks** — open-source, full MLOps platform including feature store.
- **Databricks Feature Store** / **Vertex AI Feature Store** — cloud-native options.

**Point-in-time join (as-of join):**
The most critical concept in feature stores. When generating training data, for each training example at time T, only use feature values that were known at time T. Prevents data leakage.

```sql
-- Wrong: uses future feature values
SELECT o.*, f.customer_lifetime_value
FROM orders o JOIN features f ON o.customer_id = f.customer_id

-- Correct: point-in-time correct
SELECT o.*, f.customer_lifetime_value
FROM orders o
ASOF JOIN features f
ON o.customer_id = f.customer_id
AND f.feature_timestamp <= o.order_timestamp
```

### 15.2 ML Pipeline Patterns

- **Batch scoring** — run model inference on a table of records, write predictions back to warehouse. Trigger via Airflow, output to Gold table.
- **Real-time scoring** — model served as an API (FastAPI + Docker + Kubernetes). Features fetched from online feature store per request.
- **Streaming scoring** — Flink/Spark pipeline consumes events, fetches features, runs model, emits predictions downstream.
- **MLflow** for experiment tracking, model registry, and deployment. Integrated natively in Databricks.
- **Data validation before training** — Great Expectations / Evidently AI on training dataset. Catch distribution shifts before retraining.

---

## 16. Performance Engineering & Cost Optimisation

### 16.1 File Management

| Problem | Symptom | Fix |
|---|---|---|
| Too many small files | Slow reads, high metadata overhead | Compaction (OPTIMIZE in Delta/Iceberg), coalesce before write |
| Too few large files | Low parallelism on read | Repartition before write to target file size (128–256MB) |
| No partitioning | Full table scans | Add partition column (date-based) on large tables |
| Wrong partition key | Cardinality too high or low | Low cardinality (date) for large tables; avoid UUID partitioning |
| No Z-order / clustering | Multi-column filter scans entire partition | Z-ORDER BY (Delta), SORT ORDER BY (Iceberg), CLUSTER BY (BigQuery) |

**Target file size:** 128MB–256MB Parquet files. This is the sweet spot for parallelism and metadata overhead across most engines.

### 16.2 Query Optimisation

**SQL anti-patterns to eliminate:**
- `SELECT *` — always project only needed columns; saves I/O in columnar stores
- `COUNT(DISTINCT ...)` on large datasets — use HyperLogLog approximation (`approx_count_distinct`)
- Cross joins without explicit ON clause — usually a bug, always expensive
- Non-SARGable predicates: `WHERE YEAR(event_date) = 2024` prevents partition pruning. Use `WHERE event_date >= '2024-01-01' AND event_date < '2025-01-01'`
- Window functions on unbounded partitions — `ROWS BETWEEN UNBOUNDED PRECEDING AND CURRENT ROW` on large tables; partition by a time bucket

**Expert SQL patterns:**
- Incremental aggregation — maintain a running aggregate table, insert new deltas, roll up; avoid full table recompute
- Array aggregation + explode pattern — aggregate into arrays in one pass, explode in the next; avoids repeated joins
- CTE materialisation control in dbt — `{% set materialized_cte = ... %}` to force/prevent materialisation

### 16.3 Cloud Cost Optimisation

- **Right-size Spark clusters** — auto-scaling is rarely configured optimally. Profile job resource utilisation (Spark UI → executor tab) and right-size.
- **Spot/Preemptible instances** — for fault-tolerant Spark batch jobs, 60–80% cost reduction. Requires checkpointing and retry logic.
- **Storage tiering** — S3 Intelligent Tiering, GCS Autoclass — auto-move cold data to cheaper storage classes.
- **Compression** — Snappy for interactive (fast decompress), ZSTD for archival (higher compression ratio). Never use uncompressed Parquet in production.
- **Athena/BigQuery scan reduction** — the bill is proportional to bytes scanned. Partitioning + clustering + columnar projection reduces this by 90%+ for well-designed tables.
- **Warehouse auto-suspend** — Snowflake virtual warehouses auto-suspend after N minutes of inactivity. Set to 1–5 minutes for interactive workloads.
- **Vacuum and expiry** — Delta `VACUUM`, Iceberg `expire_snapshots`, Hudi `clean` — run these on a schedule or storage cost grows unboundedly.

---

## 17. Security, Privacy & Compliance

### 17.1 PII Handling

- **Data classification** — tag columns as PII, SENSITIVE, PUBLIC in the catalogue. Drive access policy from tags (ABAC).
- **Tokenisation** — replace PII with a reversible token. Token stored in a separate, highly access-controlled vault.
- **Pseudonymisation** — replace PII with a non-reversible hash. Cannot re-identify without the mapping.
- **Anonymisation** — k-anonymity, l-diversity, differential privacy for aggregate releases.
- **Dynamic masking** — show masked value to unprivileged users at query time without storing a separate masked copy.

### 17.2 GDPR / Right to Erasure

**The data engineering problem:** How do you delete a specific user's data from an immutable, append-only lakehouse?

**Solutions:**
- **Iceberg row-level deletes** (position or equality deletes) — mark rows as deleted without rewriting files. Works for interactive deletion SLAs.
- **Delta deletion vectors** (Delta 3.x) — same concept. Non-file-rewriting delete markers.
- **Crypto-shredding** — encrypt PII columns with a user-specific key. To "delete", simply delete the key. Encrypted data becomes irrecoverable gibberish. Best at scale.
- **Purge jobs** — scheduled rewrite of affected files with deleted rows removed. Necessary after accumulating many deletion vectors.

### 17.3 Compliance Frameworks

- **GDPR** (EU) — right to erasure, data minimisation, purpose limitation, DPA requirements
- **CCPA** (California) — right to deletion, opt-out of data sale
- **HIPAA** (US healthcare) — PHI handling, audit logs, encryption at rest and in transit
- **SOC 2** — security, availability, processing integrity, confidentiality, privacy controls
- **Data residency** — data must remain within a geographic region. Multi-region architectures complicate this.

---

## 18. System Design — Senior Patterns

### 18.1 Idempotency

Every data pipeline must be idempotent: running it N times produces the same result as running it once.

**Techniques:**
- **Upsert/MERGE** instead of INSERT — re-running inserts the same rows again (duplicates). MERGE deduplicates.
- **Partition overwrite** — `INSERT OVERWRITE PARTITION(date='2024-01-01')` replaces the entire partition. Running it twice produces the same output. Never append to a partition.
- **Checksum-based deduplication** — hash the row, store hashes in a seen-set, skip duplicates on re-run.
- **Idempotency keys in Kafka producers** — `enable.idempotence=true` deduplicates retries within a session.

### 18.2 Backfill Strategy

- Always design pipelines to accept a `start_date` / `end_date` parameter.
- Use Airflow backfill (`airflow backfill create --dag-id <dag_id> --from-date <start> --to-date <end>`) or Dagster partition backfill.
- Limit concurrency during backfill — don't overwhelm source systems or downstream warehouses.
- Backfill in reverse chronological order if consumers need recent data first.

### 18.3 Schema Evolution Strategy

| Change type | Backward compatible? | Forward compatible? | Action |
|---|---|---|---|
| Add optional field | ✅ | ✅ | Safe to deploy |
| Add required field | ❌ | ✅ | Use default value, deploy consumers first |
| Remove field | ✅ | ❌ | Deploy consumers that don't use field first |
| Rename field | ❌ | ❌ | Add new field, deprecate old, dual-write transition |
| Change field type | ❌ | ❌ | Add new field with new type, migrate, delete old |

### 18.4 Late-Arriving Data

**The problem:** An event with `event_time=2024-01-01T23:55:00` arrives 2 hours late. Your pipeline already aggregated that window and wrote results.

**Solutions:**
- **Lambda architecture** — batch reprocesses historical windows nightly, overwriting streaming results. Complexity: two code paths.
- **Kappa architecture** — everything is streaming. Replay from Kafka for corrections. Simpler code, higher infrastructure complexity.
- **Incremental reprocessing** — detect late data via CDC or watermark, recompute affected partitions only. Best for most use cases.
- **Watermark tolerance** — in Flink/Spark, set watermark slack (e.g., allow 2 hours late). Hold windows open longer. Tradeoff: output latency increases.

### 18.5 The Modern Reference Architecture (2025)

```
                    ┌─────────────────────────────────────────┐
                    │           SOURCE SYSTEMS                │
                    │  PostgreSQL  MySQL  Kafka  S3  APIs     │
                    └──────────┬──────────────────────────────┘
                               │
              ┌────────────────┼────────────────┐
              │                │                │
         CDC (Debezium)   Batch Extract    Streaming Events
              │                │                │
              └────────────────┼────────────────┘
                               │
                    ┌──────────▼──────────┐
                    │   BRONZE LAYER      │
                    │   (Raw, append-only │
                    │   Delta / Iceberg)  │
                    └──────────┬──────────┘
                               │  Spark / Flink
                    ┌──────────▼──────────┐
                    │   SILVER LAYER      │
                    │  (Cleaned, merged,  │
                    │   deduplicated)     │
                    └──────────┬──────────┘
                               │  dbt / Spark
                    ┌──────────▼──────────┐
                    │    GOLD LAYER       │
                    │  (Business models,  │
                    │   metrics, marts)   │
                    └──────┬──────┬───────┘
                           │      │
              ┌────────────┘      └────────────┐
              │                               │
   ┌──────────▼──────────┐      ┌─────────────▼──────────┐
   │  BI / OLAP Layer    │      │  ML / Feature Store     │
   │  ClickHouse/Druid   │      │  Feast / Tecton         │
   │  Tableau/Power BI   │      │  Online: Redis          │
   └─────────────────────┘      └────────────────────────┘

   Orchestration: Airflow / Dagster
   Observability: Monte Carlo / OpenLineage
   Governance:    Unity Catalog / DataHub
   CI/CD:         dbt + Terraform + GitHub Actions
```

---

## 19. Soft Skills & Engineering Mindset

### What the 0.1% do differently (non-technical)

**Own the outcome, not the task.** A task is "build a pipeline for orders." An outcome is "analysts can answer revenue questions within 5 minutes without contacting the data team."

**Communicate in business terms.** Your audience is usually a product manager, analyst, or executive. "We reduced shuffle partitions and fixed data skew, cutting job time by 70%" becomes "the daily report now runs in 8 minutes instead of 27 minutes."

**Write design docs.** Before building anything significant, write a short doc: problem, options considered, chosen approach, tradeoffs, open questions. This alone puts you in the top 5%.

**Think about failure modes first.** Before asking "how will this work?", ask "how will this break?" Partial failures, late data, schema changes, source system outages — design for these upfront, not after the first incident.

**Build for observability from day one.** Add logging, metrics, and data quality checks as part of the initial build, not as an afterthought.

**Leave systems better than you found them.** Every PR is an opportunity to improve documentation, test coverage, or naming.

---

## 20. The Expert's Tech Radar (2025–2026)

### Adopt (use in production now)
- Apache Iceberg — table format standard for multi-engine lakehouses
- dbt Core — SQL transformation layer
- Debezium 2.x + Kafka — CDC backbone
- Apache Flink — streaming-first processing
- DuckDB — local development, serverless analytics
- Dagster — asset-based orchestration (greenfield)
- Delta Lake 3.x with Deletion Vectors
- Unity Catalog — governance for Databricks
- OpenLineage — lineage standard
- Terraform — IaC for data infrastructure
- DataHub — open-source data catalogue

### Trial (evaluate for your context)
- Apache Paimon — streaming lakehouse tables (Flink-native)
- dbt Semantic Layer / MetricFlow — metrics as code
- Microsoft Fabric — if heavily Azure/Power BI
- Iceberg REST Catalog — multi-engine catalogue standard
- Polars — DataFrame library (faster than Pandas, Arrow-native)
- RisingWave — streaming SQL with PostgreSQL compatibility
- Data contracts (Schemata / ODCS) — emerging standard

### Assess (watch, not yet production-ready for most)
- Apache Gluten — Spark execution accelerated on Arrow/Velox native engine
- Project Nessie — Git-like versioning for Iceberg catalogs
- AI-driven pipeline generation — LLM-assisted dbt model creation
- Iceberg v3 spec — row lineage, variants, nanosecond timestamps
- OpenTelemetry for data pipelines — unified observability standard

### Hold (use but don't invest in further)
- Apache Hadoop / HDFS — legacy; migrate to object storage
- Apache Hive — legacy metastore; migrate to Iceberg REST / Unity Catalog
- Apache Oozie — replaced by Airflow/Dagster
- Custom ETL scripts (no framework) — technical debt
- Row-based storage for analytics — always use columnar

---

## Quick Reference — Key Numbers Every Expert Knows

| Metric | Rule of thumb |
|---|---|
| Parquet file size | 128–256 MB per file |
| Spark shuffle partitions | ~200MB per partition as target |
| Kafka partition count | Target 1 partition per consumer core at peak |
| Kafka replication factor | 3 for production; `min.insync.replicas=2` |
| Broadcast join threshold | One side < 10MB (default); tune up to ~1GB safely |
| Delta VACUUM retention | Default 7 days; never below 7 days (time travel) |
| Iceberg snapshot expiry | Keep 5–7 days of snapshots for time travel |
| BigQuery partition limit | 4,000 partitions per table |
| Snowflake micro-partition size | ~16MB compressed |
| SLA for freshness alerts | Set at 2× expected run time |

---

*Last researched and compiled: May 2026. Tools and best practices evolve rapidly — treat this as a living document.*