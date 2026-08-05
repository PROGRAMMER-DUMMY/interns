# Engine and Compute Selection: Research Reference

Scope: how to choose ONE execution engine per workspace pipeline (dbt/SQL-on-warehouse, PySpark,
Polars) and the compute tier under it, from measured data properties plus a small set of questions
the platform must ask the user. Researched against vendor docs, first-party engineering blogs, and
published benchmarks. No code changed.

Baseline being replaced: `core/onboarding/kpi/engine_recommender.py` — per-KPI, three size tiers
(500 MB / 10 GB), size estimated as `rows * cols * 16` when byte size is missing, complexity score
from feature/join/dim counts, then a hard override that rewrites every `pyspark`/`hybrid`
recommendation to `polars` because PySpark is not parity-certified. It measures no velocity, no
latency SLA, no growth rate, no concurrency, no cost, and no cardinality/skew.

## Executive summary

1. Size at rest is the weakest of the useful signals. The right unit is the **working set actually
   scanned per run** — 90% of BigQuery-era queries scanned <100 MB, and a 1 PB table often has
   <50 GB of hot data.
2. Published crossovers are consistent: **<10 GB single-node always wins; 10–100 GB single-node
   still wins on small boxes; at 100 GB with ≥8 vCPU Spark wins; >1 TB or larger-than-memory
   shuffles need a distributed engine.**
3. Decathlon's production rule is the best-documented real threshold: Polars for new pipelines where
   **input tables < 50 GiB, size stable over time, and complexity reasonable**; Spark keeps the
   TB-scale medallion.
4. Single-node engines fail hard rather than degrading: DuckDB/Polars OOM where Spark spills. Fault
   tolerance and spill, not speed, are what buy you the heavy tier.
5. Single-node engines also scale poorly with cores: Spark got 4.5x faster for 2x cost going 4→32
   vCPU; DuckDB got 2.4x faster for 3.5x cost. "Throw a bigger box at Polars" has a short runway.
6. Latency class is a separate axis from size. Batch / micro-batch (`AvailableNow`) / continuous is
   decided by a named business decision and its latency, not by "we want real-time".
7. Compute tier: Databricks' own guidance is **serverless first, classic only when serverless can't
   do it**; classic startup is 5–12 min vs 15–30 s serverless, with serverless DBUs roughly 1.5–3x
   the classic rate, so short/bursty jobs favor serverless and long steady jobs favor classic.
8. "One engine per pipeline" is defensible for the transformation layer, but the medallion pattern
   is already two-engine (ingestion engine + transformation engine). Treat **ingestion as platform
   infrastructure**, and apply the single-engine rule only to the KPI/transform DAG.
9. Optimization knowledge is fully enumerable and threshold-driven (AQE defaults, broadcast 10 MB,
   skew factor 5.0 / 256 MB, 1 TB partitioning floor, 1 GB partition floor, ≤4 clustering keys,
   Photon's sub-2-second and UDF/stateful-streaming exclusions). It should be a versioned
   symptom→check→remedy table, not per-incident improvisation.
10. The choice must be revisited on measurable triggers, not on a calendar: tier crossing, spill
    bytes > 0, queue > 5 min, autoscale pinned at max, p95 runtime > 50% of the SLA window, cost per
    run drifting up, full refresh outrunning its window.

---

## Q1. Data characterization → decision

### What enterprise teams actually measure

| Property | How it is measured | Why it changes the answer |
| --- | --- | --- |
| Working set per run (not size at rest) | bytes scanned per query/run; hot-partition share | 90% of queries scan <100 MB; a 1 PB / 10-year table may have <50 GB hot ([MotherDuck](https://motherduck.com/blog/big-data-is-dead/)) |
| Largest single input table | file/table bytes, compressed | Decathlon's rule keys on the biggest input table, not the sum ([Polars/Decathlon](https://pola.rs/posts/case-decathlon/)) |
| Size stability / growth rate | table history, month-over-month bytes | Decathlon requires "stable size over time" before allowing Polars |
| Velocity class | source arrival pattern, event_time presence | Decides batch vs `AvailableNow` micro-batch vs continuous ([Databricks triggers](https://docs.databricks.com/aws/en/structured-streaming/triggers)) |
| Latency SLA | named decision + tolerable staleness | "15–60 s tolerance → micro-batch at ~50% of streaming cost" ([dataarchitect.studio](https://dataarchitect.studio/essays/batch-vs-streaming/)) |
| Join complexity / fan-out | join depth, biggest-side bytes, whether small side < 10 MB | Broadcast-eligible joins are cheap anywhere; big×big shuffles are what break single-node ([Spark tuning](https://spark.apache.org/docs/latest/sql-performance-tuning.html)) |
| Cardinality + skew | NDV per join/group key, top-value share | Skew is the classic Spark failure mode and the classic single-node OOM cause |
| Concurrency | simultaneous dashboard/BI users | ~10 concurrent queries per SQL-warehouse cluster ([warehouse behavior](https://docs.databricks.com/aws/en/compute/sql-warehouse/warehouse-behavior)) |
| Backfill/reprocess depth | retention window × per-day volume | Full-refresh cost, not steady-state cost, usually sets the tier |

### Published decision rules and crossovers

- **Decathlon (production rule).** Polars for all new pipelines where input tables are **< 50 GiB**,
  size stable over time, complexity reasonable. Spark retained for the TB-scale medallion platform.
  Operational numbers: Spark cluster start ~8 min vs Polars-on-k8s ~2 min; moving to the streaming
  engine cut RAM 100 GiB → 10 GiB and CPU 8 → 4 cores at the same 2–3 min runtime. Caveat they name:
  the k8s runway added real ops cost. <https://pola.rs/posts/case-decathlon/>
- **Coiled TPC-H at scale (10 GB → 10 TB).** <10 GB: DuckDB or Polars. 10–100 GB local: DuckDB
  preferred, Polars acceptable. Cloud ≤1 TB: single-node still competitive. ≥10 TB: you need a
  scalable system. Polars "fails hard" on large multi-table joins; Spark is ~10x slower than
  single-node engines at small scale. <https://docs.coiled.io/blog/tpch.html>
- **Miles Cole (Fabric/Spark practitioner, controlled cost-normalized test).** 10 GB: DuckDB wins at
  4 vCPU, tied at 8. 100 GB: DuckDB leads only at 4 vCPU; **Spark wins decisively at 8/16/32 vCPU**;
  Polars OOM'd at 4 and 8 vCPU. Scaling efficiency: Spark 4.5x faster for 2x cost from 4→32 vCPU;
  DuckDB 2.4x faster for 3.5x cost. Also flags the write path: no native `OPTIMIZE`, no deletion
  vectors in DuckDB/Polars.
  <https://milescole.dev/data-engineering/2024/12/12/Should-You-Ditch-Spark-DuckDB-Polars.html>
- **Polars' own PDS-H benchmark (SF1000, ~1 TB).** Polars ~6.4x faster than PySpark on one
  m8id.32xlarge (128 vCPU / 512 GB), and ~3.2x faster distributed. Vendor-run, single-workload, but
  it establishes that the single-node ceiling is now a very large box, not a laptop.
  <https://pola.rs/posts/polars-pyspark-benchmarks/>
- **MotherDuck "Big Data is Dead".** Most customers <1 TB total; heavy-user median well under
  100 GB; 90% of queries <100 MB; most-recent-day data takes ~99% of accesses. Standard cloud
  instances are 64 core / 256 GB, memory-optimized up to 24 TB RAM.
  <https://motherduck.com/blog/big-data-is-dead/>
- **Databricks performance-efficiency best practices.** "Small data set workloads that can be
  analyzed on a single node may be even slower when run on a distributed system." Do not partition
  tables **below 1 TB**; keep each partition **≥1 GB**.
  <https://docs.azure.cn/en-us/databricks/lakehouse-architecture/performance-efficiency/best-practices>
- **Latency framework.** Default to batch; ask "what does a 15-minute delay cost?"; promote to
  streaming only when someone can name the decision, the actor, and the required latency.
  <https://dataarchitect.studio/essays/batch-vs-streaming/>

### Decision table: data profile → engine + compute tier

Read top-down; first row that matches wins. "Working set" = bytes actually scanned per run, not
bytes at rest. All sizes are compressed on-disk bytes.

| # | Data profile | Engine (whole pipeline) | Compute tier | Source of the rule |
| --- | --- | --- | --- | --- |
| 1 | Working set < 5 GB, batch/daily, ≤10 concurrent BI users | **SQL/dbt on warehouse** | Serverless SQL 2X-Small/X-Small | MotherDuck; DBSQL sizing |
| 2 | Working set < 5 GB but no warehouse available / must run in-process | **Polars** (lazy) | 4 vCPU / 16–32 GB single node | Coiled <10 GB |
| 3 | 5–50 GiB largest input, **stable size**, ≤2 joins, no big×big shuffle | **Polars** (lazy + `engine="streaming"`) | 4–8 vCPU / 32–64 GB single node | Decathlon 50 GiB rule |
| 4 | 5–50 GiB but many joins/aggregations, or size not stable, or many BI users | **SQL/dbt on warehouse** | Serverless SQL Small (Photon default) | Decathlon complexity caveat; DBSQL |
| 5 | 50 GB – 1 TB, batch, mostly relational shapes, incremental-able | **SQL/dbt on warehouse** (incremental/microbatch models) | Serverless SQL Small→Medium; size up on spill | dbt incremental; DBSQL scaling |
| 6 | 50 GB – 1 TB with big×big joins, heavy skew, or non-SQL logic (ML/custom libs/complex parsing) | **PySpark** | Serverless jobs, or classic job cluster 8–16 memory-optimized workers, autoscale | Miles Cole 100 GB ≥8 vCPU; Databricks sizing |
| 7 | > 1 TB working set, or shuffle > node RAM, or growth > 2x/year | **PySpark** | Classic job cluster, autoscale 8→32+, Photon on | Coiled ≥10 TB; Databricks large-cluster tier |
| 8 | Any size, latency SLA ≤ 15 min, append-only source with event_time | **PySpark** (Structured Streaming, `Trigger.AvailableNow` micro-batch) | Serverless jobs or compute-optimized classic | Databricks triggers; batch-vs-streaming framework |
| 9 | Latency SLA < 1 min / sub-second operational | **PySpark** continuous (real-time mode) | Always-on classic/serverless streaming compute | Databricks triggers (3–5 s default, ~300 ms real-time mode) |
| 10 | Local/offline workspace, no cloud plane at all | **Polars** (SQL fallback for shapes Polars can't express) | Whatever the box is | repo constraint, not vendor |

Guardrails on top of the table:
- Rows 3 and 6 flip if **peak memory ≈ working set × join fan-out** exceeds ~60% of node RAM.
  Single-node engines OOM rather than spill (Coiled, Miles Cole).
- Never choose the heavy tier for a small pipeline "for headroom": distributed is slower at small
  scale (Databricks best practices; Coiled ~10x).
- Never choose a single-node engine for a table whose size is not stable (Decathlon).

---

## Q2. Workspace-level single-engine choice

**The honest framing: the medallion pattern is already two engines, and pretending otherwise is the
mistake.** Databricks' own reference stack is Auto Loader / Lakeflow / streaming tables for bronze
ingestion, then dbt for silver→gold, presented as "a first-class task type within a unified
pipeline… ingest with Auto Loader, transform with dbt models, then trigger dashboards, all in one
pipeline with unified retry logic".
(<https://docs.databricks.com/aws/en/lakehouse/medallion>,
<https://www.databricks.com/blog/open-platform-unified-pipelines-why-dbt-databricks-accelerating>)

So the defensible rule is a **boundary split, not an engine mix**:

- **Ingestion is platform infrastructure, not a KPI decision.** Landing any source (S3/ADLS/GCS,
  JDBC, SFTP, Kafka) into bronze is owned by the platform's ingestion engine (Auto Loader /
  Structured Streaming / Lakeflow) regardless of which engine the KPIs use. It is generated once,
  parameterized, and not re-decided per workspace.
- **The KPI/transform DAG picks exactly one engine.** This is where "one engine per pipeline" earns
  its keep: one dialect, one set of semantics, one parity surface, one optimization playbook.

Evidence for keeping the transform layer single-engine:
- Ownership splits along the same seam anyway: data engineers own bronze→silver, analytics engineers
  own silver→gold (medallion write-ups above), so the boundary is organizational, not accidental.
- The "polyglot tax": every additional engine multiplies adapters, dialect drift, and semantic
  mismatch — an O(N engines × M formats) integration surface, and the cost lands on governance,
  lineage, and cost attribution, not just on the query layer.
  (<https://devblogs.microsoft.com/azure-sql/the-polyglot-tax/>,
  <https://www.cdomagazine.tech/opinion-analysis/how-to-fix-data-platform-sprawl-3-patterns-and-3-steps-for-better-platform-decisions>)
- This repo already pays that tax visibly: the current recommender refuses to recommend PySpark at
  all because cross-engine parity is only certified for SQL and Polars, and the memory record notes
  `derived_formula` raw-SQL escape hatches permanently losing Polars/PySpark parity. Every extra
  engine in the transform DAG is another parity matrix to keep green.
- dbt's own guidance for the one legitimate in-DAG escape hatch (Python models) is: use them for
  what SQL genuinely can't express, and only strategically, because they are slower and more
  expensive than SQL models. <https://docs.getdbt.com/docs/build/python-models>

**The 90/10 case (90% of KPIs light, 2 heavy).** What mature teams do, in order of preference:

1. **Fix the heavy 2 before switching engine.** Nine times out of ten "heavy" is a full refresh that
   should be incremental. dbt's microbatch strategy exists precisely for this: a model that scanned
   2 TB in 45 min drops to 20 GB in 2 min incrementally, with per-batch retry.
   (<https://docs.getdbt.com/docs/build/incremental-microbatch>,
   <https://docs.getdbt.com/docs/build/incremental-strategy>)
2. **Size the compute for the heavy 2, keep the engine.** A warehouse sized for the worst model is
   usually cheaper than a second engine's maintenance, and DBSQL autoscales back down after 15 min
   of low load.
3. **Only then split** — and split at the layer boundary (heavy pre-aggregation as a Spark/Python
   task producing a silver/gold table, marts stay SQL), never per-KPI inside the same layer. A
   per-KPI mix means two dialects computing the same metric, which is the parity failure this repo
   already has.

Practical rule for the platform: **choose the transform engine by the heaviest KPI in the
workspace**, since one engine must serve all; then report which KPIs drove the choice, so the user
can see that fixing two models would let the whole workspace drop a tier.

---

## Q3. Compute / hardware options per tier

### Databricks compute types

| Option | Startup | Cost shape | Wins when |
| --- | --- | --- | --- |
| Serverless SQL warehouse | seconds (15–30 s) | highest $/DBU, zero idle, Photon included | dbt/BI, bursty, interactive, unpredictable schedules |
| Classic/Pro SQL warehouse | minutes | lower $/DBU, pay for idle/auto-stop | steady all-day BI, in-VPC requirement |
| Serverless jobs | seconds, Photon + autoscaling on by default | higher $/DBU | short (<~30 min) or infrequent jobs |
| Classic job cluster | 5–12 min | cheapest $/DBU, spot-eligible | long steady batch, custom libs/instance control |
| All-purpose cluster | 5–12 min | most expensive per DBU | interactive dev only — never for scheduled jobs |
| Single-node cluster | fast | one VM | small/non-distributed workloads; explicitly cheaper than a fake 2-node cluster |

Databricks' stated default: "Serverless requires no configuration, is always available, and scales
automatically with workloads in seconds. Only configure classic compute manually if serverless does
not support your use case."
<https://docs.databricks.com/gcp/en/lakehouse-architecture/deployment-guide/compute>

Cost anchors (secondary sources — treat as order-of-magnitude, verify against the current pricing
page before quoting to a user): serverless SKUs land roughly **$0.70–0.95/DBU vs $0.40–0.55/DBU**
for the classic equivalent, i.e. ~1.5–3x; the commonly cited break-even is around a **30-minute job
duration** — below it, serverless wins on eliminated startup and idle; above it, classic on reserved
or spot capacity wins. (<https://qubika.com/blog/databricks-cost-series-part-2-serverless-vs-classic/>,
<https://www.cloudzero.com/blog/databricks-pricing/>)

### SQL warehouse sizing and scaling (first-party)

Classic/pro t-shirt sizes map to fixed clusters — 2X-Small = i3.2xlarge driver + 1 worker, Small =
i3.4xlarge + 4, Medium = i3.8xlarge + 8, X-Large = i3.16xlarge + 32, up to 5X-Large = 512 workers.
Scaling rules: **+1 cluster for 2–6 min of estimated load, +2 for 6–12 min, +3 for 12–22 min**, then
+1 per additional 15 min; a query queued **5 minutes** triggers scale-up; downscale after **15
consecutive minutes** of low load; fixed limit of **one cluster per 10 concurrent queries**; max
1,000 queued queries. Guidance: **size up for single-query speed, add clusters for more users**;
start with one larger warehouse and size down; size up when queries spill to disk.
<https://docs.databricks.com/aws/en/compute/sql-warehouse/warehouse-behavior>

### Classic cluster sizing (first-party)

Small 2–8 nodes (dev/test/small data), medium 8–32 (production ETL/analytics), large 32+ (large
batch/ML). Instance family: memory-optimized for ML and shuffle-heavy work, compute-optimized for
streaming and maintenance jobs, storage-optimized for interactive/caching, GPU only for GPU
libraries. Prefer latest-generation instances; spot for workers, on-demand for the driver;
auto-terminate interactive compute (~1 h).
<https://docs.databricks.com/aws/en/lakehouse-architecture/cost-optimization/best-practices>

### Photon

Default on SQL warehouses and serverless jobs. Helps scans/joins/aggregations/writes and stateless
streaming; **does not help queries under two seconds, UDFs, RDD/Dataset APIs, or stateful
streaming** (falls back transparently); consumes DBUs at a different rate than non-Photon.
<https://docs.databricks.com/aws/en/compute/photon>

### What to present to the user, per tier

| Tier | Option A (default) | Option B (cheaper) | Option C (faster/safer) |
| --- | --- | --- | --- |
| GB | Serverless SQL 2X-Small, auto-stop | Polars on the existing single node (~$0 marginal) | Serverless SQL X-Small with result cache |
| ~100 GB | Serverless SQL Small/Medium + incremental models | Classic pro warehouse on a fixed schedule | Serverless jobs + Spark for the one heavy model |
| TB | Classic job cluster, 8–16 memory-optimized workers, autoscale, spot workers, Photon | Same but scheduled off-peak with fewer max workers | Serverless jobs (no capacity risk, ~2–3x DBU) |
| Streaming | Structured Streaming with `Trigger.AvailableNow` on a schedule | Longer `processingTime` interval | Continuous / real-time mode (only with a named sub-minute SLA) |

Always show three numbers per option: estimated run duration, estimated $/run, and worst-case
staleness. The default trigger (0 ms) is a documented cost trap — it issues storage API calls every
few milliseconds. <https://docs.databricks.com/aws/en/structured-streaming/triggers>

---

## Q4. Optimization reference / playbook

### Spark (first-party defaults worth encoding verbatim)

From <https://spark.apache.org/docs/latest/sql-performance-tuning.html>:

- `spark.sql.adaptive.enabled` = true (AQE master switch)
- `spark.sql.autoBroadcastJoinThreshold` = 10 MB — the small side must be under this to broadcast;
  `BROADCAST` hint forces it
- `spark.sql.adaptive.coalescePartitions.enabled` = true; `advisoryPartitionSizeInBytes` = 64 MB;
  `minPartitionSize` = 1 MB
- `spark.sql.adaptive.skewJoin.enabled` = true; `skewedPartitionFactor` = 5.0;
  `skewedPartitionThresholdInBytes` = 256 MB
- `spark.sql.shuffle.partitions` = 200 (the classic wrong-for-your-data default)
- `spark.sql.files.maxPartitionBytes` = 128 MB
- Join hints: `BROADCAST`, `MERGE`, `SHUFFLE_HASH`, `SHUFFLE_REPLICATE_NL`

Delta/Databricks layout, from
<https://docs.databricks.com/aws/en/delta/clustering> and the performance-efficiency page:

- Liquid clustering replaces partitioning + Z-ORDER; **≤4 clustering keys**; `CLUSTER BY AUTO`
  picks keys from query history (DBR 15.4+); `OPTIMIZE` every 1–2 h for frequently-updated tables;
  `OPTIMIZE FULL` when keys change; clustering only triggers above a write-size floor (64 MB for
  1 key, 1 GB for 4 keys on UC)
- Do not partition tables **< 1 TB**; keep partitions **≥ 1 GB**
- Use auto-compaction + optimized writes for small-file problems
- Prefer disk cache over `df.cache()` — Spark caching can consume all memory and slow queries
- Diagnose from the query profile: **bytes spilled to disk > 0 → warehouse/cluster too small**;
  **max task duration > 1.5x the 75th percentile → skew** → salt keys or pre-aggregate
  (<https://learn.microsoft.com/en-us/azure/databricks/optimizations/spark-ui-guide/long-spark-stage-page>,
  <https://docs.databricks.com/aws/en/sql/user/queries/performance-insights>)

### Warehouse SQL / dbt

- Materialization ladder: view → table → incremental → microbatch. Incremental below ~10M rows
  usually costs more in complexity than it saves; misconfigured incremental models cause silent
  data-quality drift. (<https://docs.getdbt.com/docs/build/materializations>,
  <https://docs.getdbt.com/docs/build/incremental-models-overview>)
- Microbatch for large time-series models: batches keyed on `event_time`, independently retryable
  and concurrent — the standard fix for "one model dominates the DAG".
  <https://docs.getdbt.com/docs/build/incremental-microbatch>
- Sizing before rewriting: spill → size up; queueing → add clusters; both documented with explicit
  triggers in the warehouse behavior page.
- Python models only for what SQL can't express; they are slower and pricier.
  <https://docs.getdbt.com/docs/build/python-models>

### Polars

- Lazy API by default (`scan_*` + `collect`), eager only for exploration — predicate and projection
  pushdown are the whole point. <https://docs.pola.rs/user-guide/concepts/lazy-api/>
- `engine="streaming"` for larger-than-RAM; some operators are non-streaming and silently fall back
  in-memory — inspect with `show_graph(plan_stage="physical", engine="streaming")`.
  <https://docs.pola.rs/user-guide/concepts/streaming/>
- Decathlon's measured effect of switching to the streaming engine: 100 GiB → 10 GiB RAM, 8 → 4
  cores, same runtime.
- Dtype hygiene (categoricals for low-NDV strings, narrow ints, avoid object columns), and the
  write-path gap: no native `OPTIMIZE`, no deletion vectors — Delta maintenance still needs the
  warehouse or Spark.

### How to encode it (proposal)

A machine-consultable playbook, not prose. One versioned data file plus one thin consultation step —
the rules are the asset, the code around them should stay boring.

```yaml
# config/optimization_playbook.yaml
- id: spill_to_disk
  engines: [sql, pyspark]
  symptom: "bytes_spilled_to_disk > 0"
  detect:
    metric: query_profile.bytes_spilled_to_disk
    source: system.query.history        # or Spark stage metrics
    threshold: 0
  remedies:
    - action: size_up_compute
      detail: "next warehouse t-shirt size / add executor memory"
    - action: reduce_projection
      detail: "drop unused columns; pre-aggregate before the join"
  doc: https://docs.databricks.com/aws/en/sql/user/queries/performance-insights
  confidence: high

- id: skewed_join
  engines: [pyspark]
  symptom: "max task duration >> p75 task duration"
  detect:
    metric: stage.task_duration_max / stage.task_duration_p75
    threshold: 1.5
  remedies:
    - action: verify_config
      config: {spark.sql.adaptive.skewJoin.enabled: true}
    - action: salt_key
    - action: pre_aggregate
  doc: https://learn.microsoft.com/en-us/azure/databricks/optimizations/spark-ui-guide/long-spark-stage-page
  confidence: high

- id: over_partitioned_small_table
  engines: [sql, pyspark]
  detect: {metric: table.bytes, threshold: 1_000_000_000_000, comparison: "<", extra: "partitioned == true"}
  remedies: [{action: migrate_to_liquid_clustering, detail: "CLUSTER BY AUTO, <=4 keys"}]
  doc: https://docs.databricks.com/aws/en/delta/clustering
  confidence: high
```

Rules for the encoding:

- Every entry carries `detect` (metric + source + threshold), `remedies` (ordered cheapest-first),
  `doc` (vendor URL), and `confidence`. No rule without a citable threshold.
- Thresholds are copied from vendor docs and dated; a rule with an invented number is a bug.
- Consultation is deterministic: read the run's telemetry (Databricks `system.query.history` /
  `system.billing.usage`, Spark stage metrics, Polars `explain`), match rules, emit an ordered
  remedy list into `interns/reports/optimization/current.{json,md}` — same artifact shape the repo
  already uses for `engine_recommendation`.
- Remedies are **advice plus, where safe, a generated diff** (a dbt config change, a cluster-size
  bump). Never auto-apply anything that changes results — only things that change cost/latency.
- The LLM's job is explaining and choosing among the matched remedies, not inventing thresholds.

---

## Q5. When to revisit the engine choice

Emit these as guard checks against the same telemetry, each with the action it implies:

| Trigger | Threshold | Implies |
| --- | --- | --- |
| Working set crosses a tier boundary | 5 GB / 50 GiB / 1 TB, sustained 3 runs | re-run selection |
| Growth rate | largest input +25% month-over-month, or size no longer "stable" | Polars eligibility revoked (Decathlon) |
| Spill | `bytes_spilled_to_disk > 0` on any run | size up now; if repeated after one size-up, re-run selection |
| Queueing | any query queued > 5 min | add clusters (DBSQL rule) |
| Autoscale pinned | max workers hit on ≥3 consecutive runs | tier up |
| OOM / hard failure | any on Polars | leave single-node — it does not degrade gracefully |
| SLA headroom | p95 runtime > 50% of the freshness window | tier up before it breaches |
| Cost drift | $/run up >30% over 4 weeks with flat volume | investigate before re-tiering (usually layout/small files) |
| Full refresh | full-refresh duration > maintenance window | switch to incremental/microbatch before switching engine |
| Latency requirement change | new named decision needs <15 min | move to `AvailableNow`; <1 min → continuous |
| Concurrency | peak concurrent queries / 10 > current cluster count | add clusters |
| Engine feature gap | a KPI needs a shape the chosen engine can't express | do not add a second engine silently — surface it |

Cost/usage telemetry source: `system.billing.usage` is the authoritative record (cost is derived by
joining usage with `system.billing.list_prices`); budget alerts and the prebuilt cost dashboard are
first-party. <https://docs.databricks.com/aws/en/admin/usage/system-tables>,
<https://docs.databricks.com/aws/en/admin/system-tables/billing>

---

## Proposed selection flow for this platform

### Phase 0 — measure (no user involvement)

The platform already has, or can cheaply get, everything here. Run before asking anything.

1. Per source table: compressed bytes, row count, column count, file count, small-file ratio,
   partition/clustering keys. (UC `information_schema` / table detail; profiles for local files.)
2. Per join key: NDV, null share, top-value share (skew proxy). Already partly in profiles.
3. Per KPI: join depth, biggest-side bytes, whether the small side is < 10 MB (broadcast-eligible),
   window/ratio/share shapes, dim count. (Existing `ComplexitySignals`, retained.)
4. Working set: filterable time column present? hot-partition bytes for the KPI's time window —
   this replaces `estimated_bytes` as the primary size signal.
5. Growth: bytes at rest over the last N table versions/commits (Delta history) → % per month, and
   a stability flag.
6. Arrival pattern: file/commit inter-arrival times per source → batch / hourly / continuous.
7. Backfill cost: retention window × per-day bytes → full-refresh size.
8. Existing telemetry, if any prior runs exist: p95 duration, spill bytes, queue time, $/run.

**Kill the `rows * cols * 16` fallback.** If byte size is unknown, say so and ask — a fabricated
size is worse than a missing one, because it silently decides the tier.

### Phase 1 — ask (the exact questions)

Only what measurement cannot answer. Ask once per workspace, store as workspace-level definitions
(same pattern as KPI blocker answers), reuse for every KPI.

1. **Freshness SLA.** "For each KPI consumer: what decision does this number drive, who or what
   makes it, and how stale can it be before that decision goes wrong?" (Reject "real-time" without a
   named decision and latency.)
2. **Cost of delay.** "If this were 15 minutes stale, what breaks? If 24 hours?"
3. **Volume ground truth.** "Roughly how large is the largest source table today, and what did it
   look like 12 months ago?" (Only if measurement is unavailable.)
4. **Growth expectation.** "Any known step-change coming — new source system, new region, new
   client onboarding — that would multiply volume?"
5. **Backfill.** "How far back must we be able to reprocess, and how often does history get
   corrected?"
6. **Concurrency.** "At peak, how many people/dashboards query this simultaneously?"
7. **Budget.** "Is there a monthly compute ceiling? Is paying for idle capacity acceptable, or must
   compute scale to zero?"
8. **Deployment constraints.** "Is serverless allowed, or must compute run inside your own VPC/
   subscription for governance reasons?"
9. **Non-SQL logic.** "Does any KPI need ML inference, custom Python libraries, or parsing that SQL
   can't express?"
10. **Operational fit.** "Who operates this after handover, and what do they already run?"
    (Decathlon's k8s caveat: the engine that wins the benchmark can still lose on ops.)
11. **Failure tolerance.** "If a run fails at 90%, is a full re-run acceptable, or must it resume?"
12. **Source of truth for size.** When bytes are unknown: "Can you point us at the table, or give an
    approximate row count and average row width?"

### Phase 2 — decide

- Compute the profile for the **heaviest KPI** in the workspace (one engine serves all).
- Apply the decision table above, in order, first match wins.
- Emit `interns/reports/engine_selection/current.{json,md}` with: chosen engine, compute tier,
  matched rule id, every measured signal, the answers used, which KPIs drove the choice, and the
  runner-up with the condition that would flip it ("KPI-07's 900 GB full refresh forces PySpark;
  make it incremental and the workspace drops to serverless SQL Small").
- Present two or three compute options with duration / $/run / staleness, and let the user pick.
- Record the revisit triggers from Q5 into the workflow guard alongside the choice.

### Phase 3 — operate

- After each run, evaluate the Q5 triggers against telemetry; on a match, consult
  `config/optimization_playbook.yaml` before proposing an engine change. **Optimization is tried
  first; engine change is the last resort.**
- Keep the honest boundary: ingestion engine is platform-owned and not part of this choice; the
  single-engine rule binds the KPI/transform DAG only. If a workspace genuinely needs a Spark
  pre-aggregation feeding SQL marts, record it as a **layer split with a named boundary table**, not
  as a per-KPI engine mix.

### Notes on trust of sources

First-party (Databricks docs, Spark docs, dbt docs, Polars docs) carry the thresholds. Vendor blogs
(Polars' own benchmark, Databricks' dbt post) are directionally useful but self-interested.
Third-party pricing blogs (CloudZero, Qubika, Flexera) were used only for order-of-magnitude DBU
ratios and the ~30-minute serverless break-even; re-verify against the live pricing page before
quoting a number to a user.

## Sources

- https://pola.rs/posts/case-decathlon/
- https://medium.com/decathlondigital/polars-at-decathlon-ready-to-play-6abc4328d06c
- https://motherduck.com/blog/big-data-is-dead/
- https://docs.coiled.io/blog/tpch.html
- https://milescole.dev/data-engineering/2024/12/12/Should-You-Ditch-Spark-DuckDB-Polars.html
- https://pola.rs/posts/polars-pyspark-benchmarks/
- https://docs.pola.rs/user-guide/concepts/lazy-api/
- https://docs.pola.rs/user-guide/concepts/streaming/
- https://spark.apache.org/docs/latest/sql-performance-tuning.html
- https://docs.databricks.com/gcp/en/lakehouse-architecture/deployment-guide/compute
- https://docs.databricks.com/aws/en/compute/sql-warehouse/warehouse-behavior
- https://docs.azure.cn/en-us/databricks/lakehouse-architecture/performance-efficiency/best-practices
- https://docs.databricks.com/aws/en/lakehouse-architecture/cost-optimization/best-practices
- https://docs.databricks.com/aws/en/compute/photon
- https://docs.databricks.com/aws/en/delta/clustering
- https://docs.databricks.com/aws/en/structured-streaming/triggers
- https://docs.databricks.com/aws/en/lakehouse/medallion
- https://docs.databricks.com/aws/en/sql/user/queries/performance-insights
- https://learn.microsoft.com/en-us/azure/databricks/optimizations/spark-ui-guide/long-spark-stage-page
- https://docs.databricks.com/aws/en/admin/usage/system-tables
- https://docs.databricks.com/aws/en/admin/system-tables/billing
- https://www.databricks.com/blog/open-platform-unified-pipelines-why-dbt-databricks-accelerating
- https://docs.getdbt.com/docs/build/materializations
- https://docs.getdbt.com/docs/build/incremental-models-overview
- https://docs.getdbt.com/docs/build/incremental-strategy
- https://docs.getdbt.com/docs/build/incremental-microbatch
- https://docs.getdbt.com/docs/build/python-models
- https://dataarchitect.studio/essays/batch-vs-streaming/
- https://devblogs.microsoft.com/azure-sql/the-polyglot-tax/
- https://www.cdomagazine.tech/opinion-analysis/how-to-fix-data-platform-sprawl-3-patterns-and-3-steps-for-better-platform-decisions
- https://qubika.com/blog/databricks-cost-series-part-2-serverless-vs-classic/
- https://www.cloudzero.com/blog/databricks-pricing/
