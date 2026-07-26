# Senior Data Engineer Day-to-Day Patterns

Version: 2026-07-24 research pass

Grounding material for how senior/staff data engineers actually operate production dbt+Airflow+
warehouse pipelines day to day: on-call/debugging workflows, performance diagnosis, lineage,
dimensional modeling, schema-drift handling, cost, Airflow-at-scale, idempotency, mid-project
requirement changes, and cross-team data contracts. Sourced from official vendor docs, real
engineering blogs, and independent practitioner writeups — each finding is marked with a confidence
note based on source agreement, not accepted at face value. Used to validate/inform this platform's
own design decisions (dbt project generation, Cosmos/Airflow orchestration, contract enforcement,
bounded backfill) — see "Cross-reference to this platform" at the end of each section where relevant.

## 1. DAG failure at 3am — triage workflow

Real root-cause work combines telemetry with change data (what deployed recently) and dependency
context — not log-diving alone. A full on-call cycle (triage → RCA → remediation → postmortem)
commonly takes hours, not minutes. Data-specific DAG failures cluster around a small set of causes:
a job stuck in queue, a timeout, an upstream partner delivering late, or an accidental scheduling
change that silently dropped a task from the DAG. Good postmortems explicitly capture "what went
well" (Google SRE practice), not just blame-free root cause.

**Sources:** [Google SRE Book — Being On-Call](https://sre.google/sre-book/being-on-call/);
[Barr Moses — The Data Engineer's Guide to Root Cause Analysis](https://barrmoses.medium.com/the-data-engineers-guide-to-root-cause-analysis-e407d9e48362);
[resolve.ai — Future of RCA](https://resolve.ai/glossary/what-is-root-cause-analysis)

**Confidence:** High on "combine multiple signal types, not logs alone" (broad consensus, matches
SRE canon). The specific DAG-failure-cause list is more anecdotal — illustrative, not exhaustive.

## 2. Pipelines getting slower over time

Dominant, well-evidenced pattern: full table scans where a partition filter would have pruned most
of the data, plus key skew in joins (one key owning most of the rows, bottlenecking the whole job
on one partition). Standard fixes: salt the skewed key, broadcast join the small side, or repartition
on a higher-cardinality key. Diagnosis in practice uses the query execution plan plus system-table-
driven profiling to find the worst offenders — not guesswork.

**Sources:** [OneUptime — Troubleshooting BigQuery Slow Queries](https://oneuptime.com/blog/post/2026-02-17-how-to-troubleshoot-bigquery-slow-queries-using-information-schema-and-query-execution-plan/view);
[TDWI — What Is Data Skew](https://tdwi.org/blogs/data-101/2026/05/what-is-data-skew.aspx);
[Revefi — Data Pipeline Optimization](https://www.revefi.com/blog/data-pipeline-optimization-faster-lower-cost)

**Confidence:** High — textbook Spark/warehouse-engine behavior, consistent across every source.

## 3. Data lineage in practice

Concrete, believable use cases: impact analysis before touching a source ("what breaks if I change
this column"), and root-causing a wrong number by tracing to source instead of manually walking SQL
files. One vendor cited a 3-day manual investigation cut to minutes with lineage — a dramatic
number; treat as a vendor best-case, not a guaranteed multiplier.

**Sources:** [Sigma — Why Broken Data Lineage Is the Silent Killer](https://www.sigmacomputing.com/blog/data-lineage);
[DataHub — What Is Data Lineage](https://datahub.com/blog/data-lineage-what-it-is-and-why-it-matters/)

**Confidence:** Medium — the use cases are consensus; the time-savings figures are vendor marketing.

**Cross-reference to this platform:** validates `generate-dbt-project`'s `exposures.yml` registration
(the dashboard as a formal dbt-lineage consumer) — that's the real payoff this research points at,
not a nice-to-have.

## 4. Dimensional modeling vs. One Big Table

Current consensus, not a fad take: both coexist by design — dimensional/Kimball models stay the core
warehouse layer, OBT gets built *on top of* the star schema as a serving/ML-feature layer, not as a
replacement. A cited benchmark found OBT outperforming star-schema queries by 10–45% depending on
query shape — real, but query-dependent. OBT's growing relevance is explicitly tied to ML feature
stores and AI agents wanting flat, zero-join access.

**Sources:** [Fivetran — Star Schema vs. OBT for Performance](https://www.fivetran.com/blog/star-schema-vs-obt);
[dbt Developer Blog — Kimball dimensional model with dbt](https://docs.getdbt.com/blog/kimball-dimensional-model);
[ssp.sh — One Big Table](https://www.ssp.sh/brain/one-big-table/)

**Confidence:** High on "coexistence, not replacement."

**Cross-reference to this platform:** matches this platform's own decision almost exactly — star
schema as the default gold layer, OBT only past a measured broadcast-join-size trigger, never as a
default. Good validation, not new information.

## 5. Sudden upstream schema changes

The under-appreciated point: schema drift rarely crashes a pipeline outright — it silently loads
NULLs or misaligned fields into a table that still "succeeds," which is why it's one of the hardest
failure classes to catch. Real mitigation is detection *at the point of change* (continuous source
profiling/monitoring), not at point of downstream failure, since bad data may already be propagated
by the time a job fails.

**Sources:** [Estuary — Managing Schema Drift](https://estuary.dev/blog/schema-drift/);
[Acceldata — Monitor Schema Drift in ETL](https://www.acceldata.io/blog/how-to-monitor-schema-drift-in-etl-pipelines)

**Confidence:** High on the core insight ("silent success is worse than a crash"); the specific
"4-step" processes cited are generic content-marketing framing — don't over-trust the exact steps.

## 6. Warehouse cost optimization

What actually moves the needle, consistently: **scan less** (partition pruning is the single
biggest lever), right-size compute to the actual bottleneck (memory-optimized instances can waste
40-60% of resources on CPU-bound Spark jobs), and use the platform's own system tables (Databricks
system tables / Snowflake `WAREHOUSE_METERING_HISTORY`) to find idle/oversized resources instead of
guessing. Clustering is explicitly *not* a default — Snowflake's own guidance frames it as a
targeted move for large (3TB+), infrequently-updated, frequently-filtered tables.

**Sources:** [Acceldata — Cost Optimization for Snowflake and Databricks](https://www.acceldata.io/blog/cost-optimization-strategies-for-snowflake-and-databricks-an-expert-guide);
[SunnyData — Balancing Compute for Cost & Performance](https://www.sunnydata.ai/blog/snowflake-and-databricks-how-to-balance-compute)

**Confidence:** High on "scan less is the biggest lever" (near-universal); medium on the specific
instance-sizing percentages (single source, plausible but not independently cross-checked).

## 7. Airflow scaling problems

Most concretely-sourced topic — real, specific config knobs from Astronomer's own official docs:
DAG parsing is the single heaviest scheduler operation; if `dag_dir_list_interval` is shorter than
actual parse time (`dag_processing.total_parse_time`), you get real degradation. `parsing_processes`
should be ~2x available vCPUs; `min_file_process_interval` should be raised for complex dynamically-
generated DAGs — exactly this platform's own generated dbt-path DAG shape. Airflow is
"database-connection hungry" at scale; PGBouncer is the standard mitigation for a Postgres-backed
metadata DB. Multiple schedulers scale roughly linearly.

**Sources:** [Astronomer — Scaling Airflow to Optimize Performance](https://www.astronomer.io/docs/learn/airflow-scaling-workers)
(official, high authority); [Apache Airflow — Scheduler docs](https://airflow.apache.org/docs/apache-airflow/2.5.1/scheduler.html);
[Cutting DAG parse time from 60s to milliseconds](https://medium.com/@adrianmroz.7/optimising-airflow-cutting-dag-parse-time-from-60-s-to-milliseconds-a-practical-guide-part-1-d15081f419ae)

**Confidence:** High — official vendor docs plus independent practitioner writeups agree.

## 8. Idempotency and error handling

Very well-established, consensus patterns: **replace, don't append** (DELETE+INSERT partition
replacement, or MERGE/upsert) is naturally idempotent; plain append is not. Dead-letter queues let
good records through while quarantining bad ones instead of failing the whole batch, with an alert
threshold (a cited example: alert past a 5% failure rate). Retry uses exponential backoff **with
jitter** specifically to avoid a thundering-herd retry storm. Backfill best practice is bounded
partition-overwrite (e.g. the specific affected range), never an unbounded full-history rerun.

**Sources:** [dataskew.io — Data Pipeline Design Patterns](https://dataskew.io/blog/data-pipeline-design-patterns/);
[Prefect — Importance of Idempotent Data Pipelines](https://www.prefect.io/blog/the-importance-of-idempotent-data-pipelines-for-resilience);
[Airbyte — Understanding Idempotency](https://airbyte.com/data-engineering-resources/idempotency-in-data-pipelines)

**Confidence:** High — the most textbook-solid topic; strong agreement across vendor-neutral and
vendor sources alike.

**Cross-reference to this platform:** matches `run-dbt-backfill`'s bounded-span design (refuses a
span over the default threshold without human confirmation) almost exactly.

## 9. Mid-project requirement changes / ambiguous asks

Clarify and validate requirements first, then explicitly negotiate scope/priority with stakeholders
rather than silently absorbing scope creep or refusing outright. The repeated theme is regular
check-ins/feedback loops as the actual mechanism, not a one-time requirements doc.

**Sources:** [LinkedIn Collaborative Article — Ambiguous Stakeholder Requirements](https://www.linkedin.com/advice/0/how-do-you-deal-ambiguous-unrealistic-stakeholder);
[mccricardo — The Staff+ Engineer's Superpower](https://mccricardo.com/the-staff-engineers-superpower-transforming-ambiguity-into-action/)

**Confidence:** Low-medium — the softest topic of the ten; sources lean generic-advice rather than
specific incident writeups. Treat as reasonable-consensus guidance, not hard evidence.

**Cross-reference to this platform:** the "blocker grilling" pattern already in `AGENTS.md` (ask one
high-leverage question, offer evidence-backed options, record the decision) is *more* concrete than
general web literature offers on this topic — the platform's own pattern is the stronger artifact
here, not something the research needed to justify.

## 10. Cross-team friction / data contracts

Data contracts shift accountability upstream — the producer team owns meeting agreed
completeness/accuracy/timeliness, instead of the analytics/platform team absorbing the downstream
fix burden. Teams adopting contracts report fewer late-night escalations and faster root-causing
because a broken expectation traces to a specific contract violation instead of an open-ended
investigation.

**Sources:** [Acceldata — How Data Contracts Enforce Pipeline Stability](https://www.acceldata.io/blog/how-data-contracts-guarantee-pipeline-reliability-data-quality-slas);
[DataHub — What Are Data Contracts](https://datahub.com/blog/the-what-why-and-how-of-data-contracts/)

**Confidence:** Medium — directionally strong, but "fewer escalations" claims are self-reported/
vendor-adjacent, not independently measured. The underlying logic (push validation to the producer
boundary) is sound.

**Cross-reference to this platform:** matches `contract: enforced: true` on dashboard-consumed
marts (`generate-dbt-project --enforce-contracts`) — verified live this session to fail loud on a
real type mismatch rather than silently propagate a broken value downstream.

---

**Cross-cutting audit note:** Topics 1-8 and 10 have solid, mutually-reinforcing evidence from a mix
of official docs (Astronomer, Google SRE, Airflow), real company engineering blogs, and specific
technical vendor posts with concrete numbers. Topic 9 (ambiguous requirements) is the
weakest-evidenced — genuinely more "common wisdom" than documented pattern. No topic surfaced a
genuine disagreement between sources; the closest to a live debate is dimensional-modeling-vs-OBT,
and even there the resolution (coexistence, not replacement) was consistent across every source
checked.

---

# Addendum: 2026-07-26 research pass — platform-specific layer

Sections 1-10 above are engine-neutral (dbt + Airflow + "a warehouse"). This addendum covers what
that pass did not: Databricks-specific day-to-day decisions, the dbt features that *are* the
idempotency contract, and Airflow 3 (sections 1-10 cite Airflow 2.5 docs, now two majors behind).
Same rules: sources named, confidence stated, cross-referenced to this platform's own code.

## 11. Databricks data layout — liquid clustering has replaced the partitioning decision

The clearest best-practice reversal in the current docs. Databricks now recommends **liquid
clustering for all new Delta tables**, ahead of both static partitioning and Z-ORDER. It handles low
*and* high cardinality, avoids fixed partition boundaries and the small-file problem, and is
mutually incompatible with partitioning/ZORDER on the same table. Practical guidance: **1-4
clustering keys**, chosen from the columns actually used in filters and joins — more keys dilutes
data skipping. Partitioning only becomes worth evaluating around **100 TB+**, and even then only
after verifying liquid clustering underperforms.

`CLUSTER BY AUTO` hands key selection to Databricks. Predictive optimization automates maintenance
(OPTIMIZE/VACUUM) for eligible Unity Catalog **managed** tables.

**The operational trap:** pick *either* cron-driven `OPTIMIZE` *or* predictive optimization for a
given table — never both. Running both produces redundant, billed work.

**Sources:** [Databricks — Use liquid clustering for tables](https://docs.databricks.com/aws/en/tables/clustering);
[Databricks — When to partition tables](https://docs.databricks.com/aws/en/tables/partitions);
[Databricks Blog — Debunking 8 data layout myths](https://www.databricks.com/blog/debunking-8-data-layout-myths-why-liquid-clustering-outperforms-partitioning);
[Databricks Community — Performance Optimization: What Changed](https://community.databricks.com/t5/community-articles/databricks-performance-optimization-what-changed-what-still/td-p/163324)

**Confidence:** High. Official vendor docs state it directly and independent practitioner posts
agree; the 100 TB threshold and 1-4 key guidance are both in first-party docs.

**Cross-reference to this platform:** `core/medallion/` emits a `storage_strategy.json` with
zstd/partition/OPTIMIZE recommendations derived from volume. That advice is now *behind* the vendor
default — it should recommend liquid clustering first and partitioning only above the verified
threshold. The both-at-once trap is also a concrete check we could emit: a workspace that schedules
OPTIMIZE **and** sits on UC managed tables with predictive optimization enabled is double-paying.

## 12. dbt incremental strategy IS the idempotency contract

Which strategy you pick decides whether reruns are safe. This is not a performance knob:

| strategy | idempotent? | shape |
|---|---|---|
| `append` | **no** | duplicates on every rerun |
| `merge` + `unique_key` | yes | upsert via `MERGE INTO` |
| `insert_overwrite` | yes (per partition) | atomic partition replacement |
| `microbatch` | yes (per batch) | each batch independently rebuildable |
| `delete+insert` | yes | delete key range, reinsert |

**Portability trap worth knowing:** on BigQuery `insert_overwrite` really does replace *partitions*
— the cost-efficient choice for large partitioned tables. On **Snowflake it replaces the entire
table**, and that naming inconsistency has caused real data loss. Redshift does not support it at
all. Databricks supports all five.

`microbatch` (dbt 1.9+) is the current answer for large time-series: dbt splits the model into
multiple queries keyed on a declared `event_time`, and **each batch is an atomic, independently
replaceable unit** — which is what makes bounded backfill natural rather than bolted on.

**Sources:** [dbt Docs — About incremental strategy](https://docs.getdbt.com/docs/build/incremental-strategy);
[dbt Docs — About microbatch incremental models](https://docs.getdbt.com/docs/build/incremental-microbatch);
[dbt Docs — Incremental patterns for near real-time data](https://docs.getdbt.com/best-practices/how-we-handle-real-time-data/2-incremental-patterns);
[dbt-databricks — Incremental Strategies](https://deepwiki.com/databricks/dbt-databricks/3.2-incremental-strategies);
[Vermorel — Incremental Strategy Decision Framework](https://adriennevermorel.com/notes/incremental-strategy-decision-framework/)

**Confidence:** High on strategy semantics and the Snowflake divergence (first-party dbt docs plus
adapter docs). Medium on how widely `microbatch` is adopted in production — it is recent.

**Cross-reference to this platform:** direct hit on a live defect. Our medallion manifest emits
`load_strategy: append_watermarked` with `watermark_column: null` for tables lacking a usable
watermark — which per the table above is the one **non-idempotent** option, i.e. full append on
every run. `microbatch`/`insert_overwrite` semantics are what that emitter should degrade to. Also
relevant to the known `emit_silver_merge` limitation (MERGE keyed on PK cannot correct a *changed*
PK value — stale orphans persist): the literature's answer is staging + validate + atomic swap
(see 15), not a smarter MERGE.

## 13. Airflow 3 — assets, DAG versioning, and backfill as a first-class feature

Three changes that matter to day-to-day work:

**Datasets became Assets** — a superset of the old Dataset model, same inlet/outlet patterns plus an
`@asset` decorator and asset-driven execution paths. As of **3.2**, DAGs can trigger on **specific
partition updates** rather than on every asset change, with `CronPartitionTimetable` for scheduling
against partitions. Partition-driven orchestration handled natively instead of worked around.

**DAG versioning** (the most-requested feature in the annual Airflow survey) — a run completes on
the version it *started* with even if new code is deployed mid-run, and the UI ties every run to the
code, task structure and logs as they were. This kills a classic 3am confusion: logs that do not
match the code you are reading.

**Backfill is now engine-native** — managed by the scheduler and observable, rather than a manual
out-of-band process. Partitioned DAGs can backfill historical partitions without re-triggering
everything downstream.

**Sources:** [Airflow Blog — Airflow 3 is Generally Available](https://airflow.apache.org/blog/airflow-three-point-oh-is-here/);
[Airflow Blog — 3.2.0: Data-Aware Workflows at Scale](https://airflow.apache.org/blog/airflow-3.2.0/);
[Airflow Docs — Asset-Aware Scheduling](https://airflow.apache.org/docs/apache-airflow/stable/authoring-and-scheduling/asset-scheduling.html);
[AWS — Best practices migrating Airflow 2.x to 3.x on MWAA](https://aws.amazon.com/blogs/big-data/best-practices-for-migrating-from-apache-airflow-2-x-to-apache-airflow-3-x-on-amazon-mwaa/)

**Confidence:** High — all first-party Airflow sources plus a cloud-vendor migration guide.

**Cross-reference to this platform:** `core/orchestration/airflow_dag.py` renders our stage graph as
BashOperators with a `dbt retry || dbt build` resumption trick, and `dbt_backfill.py` implements
bounded-span backfill ourselves with a human-confirmation threshold. Airflow 3's native backfill and
partition-aware asset scheduling cover both, and our stage graph is *already* asset-shaped
(`Stage.produces` is literally a list of artifact globs) — the `@asset` mapping is close to
mechanical. DAG versioning is also the orchestrator-level version of the exact gap this session's
audit found in our own runs: no record of which code or model produced which mutation.

## 14. Lakeflow (formerly Delta Live Tables) — the declarative path we do not use

DLT is now **Lakeflow (Spark) Declarative Pipelines**; existing DLT code still works, no migration
required. The model is flows + **streaming tables** + **materialized views** + sinks, in SQL or
Python.

- **Streaming table** — a UC managed table that is also a streaming *target*; takes one or more
  streaming flows (`Append`, `AUTO CDC`).
- **Materialized view** — a UC managed table that is a *batch* target; its flows are always defined
  implicitly by the view definition.

2026 additions: standalone materialized views and streaming tables on **serverless** compute (Beta)
— no dedicated pipeline clusters, less operational overhead; `MANAGE` permissions auto-propagate in
Unity Catalog; and **`REPLACE WHERE` flows** (Beta), aimed at incremental batch processing of joins
and aggregations.

**Sources:** [Databricks — What are Lakeflow pipelines?](https://docs.databricks.com/aws/en/ldp/concepts);
[Databricks — What happened to Delta Live Tables?](https://docs.databricks.com/aws/en/ldp/concepts/where-is-dlt);
[Databricks — Lakeflow pipelines release notes 2026](https://docs.databricks.com/aws/en/release-notes/dlt/2026)

**Confidence:** High on the rename, object model and release-note features (all first-party). Beta
features are Beta — do not design a hard dependency on `REPLACE WHERE` yet.

**Cross-reference to this platform:** we hand-roll what `AUTO CDC` into a streaming table does
natively — `merge_emitter.py` / `delta_emitter.py` generate silver MERGE code, and the cloud path
does not even run them (it goes through dbt instead). This deserves an explicit architectural
decision: hand-rolled MERGE, dbt incremental, or Lakeflow. The repo currently has the first two and
they disagree with each other.

## 15. Idempotency, deeper — the parts section 8 did not reach

Section 8 covered replace-don't-append, DLQ, backoff-with-jitter, bounded backfill. Four additions:

**Watermark determinism is the whole game.** `WHERE event_date = '2026-05-18'` processes identical
data on every run. `WHERE event_date > current_date() - 1` processes *different* data depending on
when it runs. The second form is the most common accidental source of non-idempotency, and it looks
entirely reasonable in review.

**Truncate-and-reload is a trap** — a failed reload leaves the table empty or half-loaded. The
prescribed pattern is write to staging, validate, then **atomic swap**.

**"Exactly-once" is largely an illusion** worth naming as such: it requires transactional
coordination between the messaging system and the processing system, and it costs performance. The
honest posture is at-least-once delivery plus idempotent writes, which is indistinguishable from
exactly-once at the table.

**Late-arriving events** need windowed processing or upsert-on-event-timestamp; without it they are
silently dropped or double-counted. This is the mechanism that makes backfill a routine operation
rather than incident response.

**Sources:** [dataskew.io — Data Pipeline Design Patterns: Idempotency, DLQ, CDC](https://dataskew.io/blog/data-pipeline-design-patterns/);
[systemoverflow — Idempotency, Deduplication, and Exactly Once Illusions](https://www.systemoverflow.com/learn/data-processing/etl-pipelines/idempotency-deduplication-and-exactly-once-illusions-in-distributed-pipelines);
[Towards Data Engineering — Building Idempotent Data Pipelines](https://medium.com/towards-data-engineering/building-idempotent-data-pipelines-a-practical-guide-to-reliability-at-scale-2afc1dcb7251);
[apxml — Idempotency in Data Pipelines](https://apxml.com/courses/building-scalable-data-warehouses/chapter-3-high-throughput-ingestion/idempotency-pipelines)

**Confidence:** High on watermark determinism, staging+swap and the exactly-once caveat (consistent
across vendor-neutral sources and standard distributed-systems canon). The specific alert thresholds
in section 8 remain illustrative.

**Cross-reference to this platform — a defect this research surfaced.** Verified precisely
(2026-07-26): the generator anchors age on the **event date** when the KPI's cuts carry a time-grain
column — `kpi_001_databricks.sql` correctly emits
`date_diff(year, CAST(DOB AS DATE), CAST(ServiceDate AS DATE))`. That is the existing BUG-005
handling and it works. The gap is the *other* case: when no event-date anchor exists in the cuts
(`kpi_002`, cuts = "Department Name, VisitType, Gender, Age (DOB)"), `as_of_expr` falls back to the
literal `CURRENT_DATE`, making that result view **non-reproducible across runs** — re-executing the
same SQL later silently reshapes its own age bands. Semantically the fallback is defensible ("age as
of now"); as an *artifact* it is not, because the query is no longer a function of the data alone.
The fix is to pin `as_of` to a recorded literal at generation time rather than deferring it to
execution time.

Worse, the guard for this already exists and never ran. `intent_coverage.temporal_anchor_findings()`
implements exactly the BUG-005 check, and `validate-kpi-intent-coverage` is a registered CLI — but it
appears **zero times** in the 92-entry cost ledger and is **not wired into
`pipeline_stages.STAGES` or `flow.py`**. That is this platform's recurring failure mode, not a
one-off: correctness guards shipped as optional commands rather than pipeline stages (see also the
screener's opt-in `--screen`, and the fan-trap detector that was simply deleted).

## Efficiency and CI, briefly

`state:modified+` with `--defer` (Slim CI) builds and tests only changed nodes and their children,
cutting wall-clock and compute cost. In 2026 the state-dependent selectors work across mixed dbt
Core / Fusion environments, so migration can be incremental; the recommended pattern is a
centralized production manifest (`DBT_STATE_PATH` pointing at object storage) rather than a manifest
committed to the repo. `--empty` supports schema-only CI builds, and cloning incremental models as a
CI first step avoids full rebuilds of expensive incrementals.

**Sources:** [dbt Docs — Defer](https://docs.getdbt.com/reference/node-selection/defer);
[dbt Docs — Get started with CI tests](https://docs.getdbt.com/guides/set-up-ci);
[dbt Docs — Clone incremental models as the first step of your CI job](https://docs.getdbt.com/best-practices/clone-incremental-models);
[Data Engineer Things — A Data Platform Engineer's Guide to dbt Fusion in 2026](https://blog.dataengineerthings.org/a-data-platform-engineers-guide-to-dbt-fusion-in-2026-eab68945a8bd)

**Confidence:** High on mechanics (first-party dbt docs). Medium on Fusion migration specifics — it
is actively shipping and the surface is moving.

---

# Addendum 2: 2026-07-26 — operating at TB/PB scale

What changes when the data is terabytes, not gigabytes. Sections 1-15 hold at any size; these are
the practices that only appear once a full scan stops being free. Same rules: sources named,
confidence stated, and a closing verdict on where THIS platform breaks at that scale.

## 16. How they analyze — measurement, never guesswork

The consistent professional pattern is: read the execution plan and the platform's own telemetry
before changing anything. On Databricks that means the **Query Profile** (tree view for the slow
operator, graph view for how data moves between stages, and an explicit signal for the presence of
**full table scans**) plus the **system tables**: `system.query` for warehouse query metrics,
`system.compute` for cluster utilisation, `system.workflow` for job performance. Query history has to
be enabled to get any of it retrospectively.

The senior habit that follows: find the *worst offenders* from telemetry, fix those, re-measure.
Not "tune the cluster" as a first move.

**Sources:** [Databricks — Best practices for performance efficiency](https://docs.databricks.com/aws/en/lakehouse-architecture/performance-efficiency/best-practices);
[Unravel — Databricks I/O Performance Tuning](https://www.unraveldata.com/resources/databricks-io-performance-tuning-faster-query-execution);
[e6data — Databricks Performance Optimization: Complete Query Tuning Guide 2026](https://www.e6data.com/query-and-cost-optimization-hub/databricks-performance-optimization-complete-query-tuning-guide-2025);
[dbt Docs — Optimize and troubleshoot dbt models on Databricks](https://docs.getdbt.com/guides/optimize-dbt-models-on-databricks)

**Confidence:** High — first-party docs plus multiple independent tuning guides agree on both the
tooling and the order of operations.

## 17. The optimization ladder at TB scale

In rough order of how often it is the actual answer:

1. **Enable AQE, always, in production.** It fixes shuffle-partition sizing, skew, and join strategy
   *dynamically* at runtime — including converting a sort-merge join to a broadcast hash join when
   runtime stats show one side is small enough, which static planning cannot know. One documented
   case: 45 minutes to 21 minutes from AQE alone.
2. **Kill the full scan.** Partition pruning / data skipping is the single biggest lever; the Query
   Profile flags scans explicitly.
3. **Diagnose spill before adding memory.** Disk spill in the profile means the partition is too big
   for the executor. Three real fixes: raise `spark.executor.memory`, raise
   `spark.sql.shuffle.partitions` so each partition is smaller, or **remove the shuffle entirely**
   via broadcast join. Adding nodes without reading the spill metric is the classic waste.
4. **Handle skew explicitly** when AQE's automatic skew-join splitting is not enough: salt the hot
   key, broadcast the small side, or repartition on a higher-cardinality key.
5. **Raise the broadcast threshold** (50-200 MB) if cluster memory allows, to get more broadcast
   hash joins.
6. **Fix small files.** Metadata overhead on many small files slows reads badly; `OPTIMIZE` bin-packs
   into files typically **128-256 MB**. Optimized Writes tunes partition sizes at write time; Auto
   Compact merges small files after a successful write, skipping already-compacted ones. A cited
   consolidation: 50,000 small files / 100 GB down to ~400 files, 10x+ faster reads.

**Sources:** [Spark — SQL Performance Tuning](https://spark.apache.org/docs/latest/sql-performance-tuning.html)
(first-party, AQE + skew join + adaptive broadcast);
[Flexera — Spark performance tuning: 7 optimization tips (2026)](https://www.flexera.com/blog/finops/spark-performance-tuning/);
[Onehouse — Top 5 tips for scaling Apache Spark](https://www.onehouse.ai/blog/top-5-tips-for-scaling-apache-spark);
[Mittal — From 45 Minutes to 21 Minutes with AQE](https://medium.com/@sumitmittal-trendytech/from-45-minutes-to-21-minutes-how-adaptive-query-execution-aqe-saves-your-spark-jobs-c12e29d14f05);
[Databricks small-file problem: OPTIMIZE and Auto-Optimization](https://medium.com/@omkarspatil2611/how-databricks-solves-the-small-file-problem-with-optimize-and-auto-optimization-6f37fef68388)

**Confidence:** High on AQE, spill diagnosis, skew remedies and the 128-256 MB target (first-party
Spark/Databricks docs). The specific before/after numbers are individual cases, not benchmarks.

## 18. What they do NOT do at scale — exact aggregates over full history

The most transferable lesson in this whole section. **Exact `COUNT(DISTINCT ...)` does not scale**;
at volume it is replaced by probabilistic sketches:

- **HyperLogLog** estimates cardinality in a fixed, tiny footprint (kilobytes) regardless of input
  size, with a small predictable error. Typical accuracy: a true 1,000,000 comes back in the range
  ~983,767-1,016,234 (roughly +/-1.6%).
- Sketches are **mergeable**, which is the real superpower: persist a daily HLL sketch, then union
  sketches to answer any date range without rescanning source data. One reported case: a calculation
  that needed days and terabytes of memory exactly became ~12 hours in under 1 MB.
- Platform functions exist everywhere now (`APPROX_COUNT_DISTINCT` / HLL++ on BigQuery, Snowflake,
  Presto, Databricks, DuckDB). Successors (UltraLogLog, ExaLogLog) improve space further.

The parallel practice for profiling: read **metadata before data** — catalog/table statistics, Delta
log stats, Parquet row-group stats — and only sample rows when metadata cannot answer the question.

**Sources:** [Snowflake — Estimating the Number of Distinct Values](https://docs.snowflake.com/en/user-guide/querying-approximate-cardinality);
[Meta Engineering — HyperLogLog in Presto](https://engineering.fb.com/2018/12/13/data-infrastructure/hyperloglog/);
[Permutive — Petabyte-scale analytics with BigQuery and HLL](https://medium.com/permutive/petabyte-analytics-with-bigquery-hll-af0f7a70b66d);
[MotherDuck — HyperLogLog](https://motherduck.com/glossary/hyperloglog/);
[UltraLogLog paper (arXiv)](https://arxiv.org/pdf/2308.16862);
[ExaLogLog paper (arXiv)](https://arxiv.org/pdf/2402.13726)

**Confidence:** High — vendor docs, a FAANG engineering blog, peer-reviewed papers, and an
independent practitioner case study all agree on both the technique and the error envelope.

**Cross-reference to this platform:** this is the part we already got right. `core/profiling/
data_model_profiler.py` implements exactly the prescribed ladder — *"catalog stats -> Delta log stats
-> Parquet row-group stats -> sample profile"* — with `sample_rows=100_000` and an `exact` opt-in,
and four `tests/regressions/test_profiler_tb_scale_*.py` lock it in (CSV laziness, null-count
pushdown, Databricks stats, Spark quick-profile). Ingest-side scale discipline is real here.

## 19. Modeling at scale — layout and pre-aggregation beat normalization

Section 4 settled dimensional-vs-OBT as coexistence. At TB scale the emphasis shifts:

- **Physical layout is a modeling decision**, not a tuning afterthought. Clustering/partitioning keys
  are chosen from real filter and join predicates (see 11), and that choice moves query cost by
  orders of magnitude — more than schema shape does.
- **Pre-aggregate deliberately**, and prefer mergeable state (HLL sketches, sum/count pairs rather
  than stored averages) so rollups compose across grains without rescanning.
- **Denormalise to avoid the expensive join**, not on principle. The join you remove is only worth
  removing if it was a shuffle.
- **Grain discipline gets stricter, not looser**: a wrong grain that merely produces a confusing
  table at GB scale produces an unrunnable query and a fan-out explosion at TB.

**Confidence:** Medium-high. Layout-over-schema and mergeable pre-aggregation are well supported by
sections 11 and 18's sources; the framing here is synthesis across them rather than a single cited
claim.

## 20. Failure and recovery at scale

**Idempotency is the foundation of every recovery pattern** — without it, restart produces
duplicates, and no amount of checkpointing helps. Given that, the mechanics:

- **Checkpoints / bookmarks** let a job resume from the last successful step instead of restarting
  (Spark RDD checkpointing, AWS Glue job bookmarks, dbt's `dbt retry` off `run_results.json`).
- **A documented pathology at ultra-large scale:** when shuffle data goes missing from a transient
  hardware or network fault, naive frameworks do *partial re-execution* — kill the downstream task,
  re-run upstream to reconstruct, discard all downstream progress. On big jobs this can degenerate
  into repeated re-execution loops that increase latency and can fail the job outright. This is why
  resilient shuffle services exist (Alibaba's FuxiShuffle, ByteDance's StreamShield are published
  examples). At TB scale, *retry* is not automatically cheap.
- **Dead-letter queues** keep good records flowing while quarantining bad ones, with an alert
  threshold, instead of failing the whole batch (section 8).
- **Bounded backfill** over full-history rerun, always — and at TB scale the difference is the
  entire cost argument.

**Sources:** [Zilliz — Robust error handling and recovery in ETL](https://zilliz.com/ai-faq/how-can-you-ensure-robust-error-handling-and-recovery-in-etl);
[Zilliz — How ETL tools handle error recovery and audit trails](https://zilliz.com/ai-faq/how-do-etl-tools-handle-error-recovery-and-audit-trails);
[FuxiShuffle: Adaptive and Resilient Shuffle Service (arXiv)](https://arxiv.org/pdf/2602.22580);
[StreamShield: Production-Proven Flink Resiliency at ByteDance (arXiv)](https://arxiv.org/pdf/2602.03189)

**Confidence:** High on idempotency-as-foundation and checkpoint/bookmark mechanics. High on the
partial-re-execution pathology (two independent production papers). Low on any specific cost figure —
no source gave defensible full-rerun-vs-checkpoint economics, so treat that comparison as
directional.

## 21. Streaming vs batch, and late data at scale

**Watermarks** are the mechanism: declaring a timestamp field plus a lateness threshold tells the
engine "everything before T has been seen", which is what lets it bound and eventually release
streaming state. Without a watermark, stateful streaming state grows without limit.

The three-way split that matters operationally:

- **On time** — processed in its window.
- **Late but inside the threshold** — updates the older window; the aggregate is restated.
- **"Too late" (beyond the watermark)** — dropped by the streaming job. The recommended pattern is a
  **secondary path: a periodic batch job that reprocesses this residue.**

That last point is the honest answer to "streaming or batch": production systems run **both** —
streaming for timeliness, periodic batch for completeness and correctness. Batch handles late data
almost for free (an hour-1 record arriving in hour 2 just gets picked up with hour 2's run), which is
precisely the complexity streaming takes on in exchange for latency. So the decision is a latency
requirement weighed against permanent extra machinery — not a maturity ladder where streaming is the
top rung.

**Sources:** [Databricks — Apply watermarks to control data processing thresholds](https://docs.databricks.com/aws/en/structured-streaming/watermarks);
[Databricks Blog — Event-time Aggregation and Watermarking in Structured Streaming](https://www.databricks.com/blog/2017/05/08/event-time-aggregation-watermarking-apache-sparks-structured-streaming.html);
[Conduktor — Handling Late-Arriving Data in Streaming](https://www.conduktor.io/glossary/handling-late-arriving-data-in-streaming);
[OneUptime — How to Fix 'Late Data' Handling in Streaming](https://oneuptime.com/blog/post/2026-01-24-streaming-late-data/view)

**Confidence:** High on watermark semantics and the on-time / late / too-late split (first-party
Databricks + Spark docs). Medium on the "run both" prescription — strongly implied and widely
practised, stated most explicitly by the secondary sources.

## Verdict: what breaks in THIS platform at TB scale

The split is clean and worth stating plainly: **the ingest side was designed for scale; the serving
side was designed for a laptop.**

**Holds up (already TB-aware):**
- `core/profiling/data_model_profiler.py` — metadata-first ladder, bounded sampling, `exact` opt-in,
  four `tb_scale` regression tests. Matches section 18's prescription.
- `databricks_table_profiler.py` pushdown, per `tests/test_profile_pushdown.py`.

**Breaks:**
1. **The dashboard materialises the whole gold table into Python.**
   `core/dashboard/model/layers.py:245` builds `pl.DataFrame([list(r) for r in rows], ...)` — every
   row of a Databricks gold mart, through a Python list-of-lists, into memory, then written to a
   local `conformed.parquet` that Polars/DuckDB reads single-node. Fine at 4k rows; fatal at TB. The
   fix is aggregate-pushdown: the dashboard should send GROUP BY to the warehouse and read back
   grouped results, not read rows and group locally.
2. **`importance.py` and `screener.py` need the frame in memory** to compute eta-squared /
   concentration and to render. Same ceiling as (1).
3. **`minus`'s connectors are CSV / Parquet / sqlite3, and `duckdb_exec.py` is single-node.** The
   serving tier has no distributed execution path at all.
4. **`load_strategy: append_watermarked` with `watermark_column: null`** — full append every run
   (section 12). At TB scale this is the single most expensive defect in the repo.
5. **`CURRENT_DATE` in generated KPI SQL** (section 15) — non-deterministic, and at TB scale you
   cannot afford to recompute to find out what changed.
6. **The deleted fan-trap detector** (this session's audit, finding 2). At GB scale a fan-out join
   returns a wrong number; at TB scale it returns a job that never finishes.
7. **Medallion runs `target: duckdb` single-node** even on a `databricks_exclusive` workspace — so
   the layer that *does* contain the real silver logic is the one that cannot scale.
8. **No AQE / spill / skew posture anywhere** in the generated PySpark, and no query-profile or
   `system.query` telemetry read-back to find the worst offenders (sections 16-17). We generate SQL
   and PySpark but never look at how either performed.

Items 1-3 are one architectural decision: **push aggregation down to the warehouse instead of pulling
rows up to the app.** That is the difference between a dashboard that works on a demo workspace and
one that works on a customer's.
