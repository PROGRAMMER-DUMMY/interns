# Engine Roles at Scale: SQL/dbt, PySpark, and Polars in a Cloud-First Databricks Stack

Research date: 2026-08-04. Scope: what the evidence says about dividing work between warehouse
SQL (dbt), PySpark, and single-node engines (Polars) in a Databricks + dbt + Airflow stack at
TB scale, and what cross-engine parity should mean.

---

## Executive summary

1. The standard enterprise division is **SQL/dbt by default, Python/Spark by exception** — dbt's own
   docs say well-written SQL is preferable when both are possible.
2. dbt Python models on Databricks *are* PySpark, but they cannot run on a SQL warehouse and cannot
   be `view` or `ephemeral`; they are the "escape hatch inside the DAG", not a second modeling layer.
3. Polars has a real, documented production role — Decathlon runs it for all new pipelines with
   inputs under 50 GiB, on Kubernetes, and reports Spark cold-start (8 min) exceeding total Polars
   runtime (2–3 min).
4. Polars' streaming engine changed the ceiling: the same job went from 100 GiB/8 CPU to
   10 GiB/4 CPU, and Polars now claims single-node viability "up to TiB of input data".
5. Writing Delta from non-Spark clients into Unity Catalog **managed** tables is only Public Preview
   and requires `EXTERNAL USE SCHEMA`; this is the hard constraint on Polars as a *writer*.
6. **No credible source shows a team maintaining the same business logic hand-written in three
   engines as a steady state.** That is not an industry practice.
7. The industry answer to multi-engine portability is **one definition, many compilation targets** —
   Ibis (20+ backends, compiles via SQLGlot), SQLGlot itself, and dbt's semantic layer.
8. Dual-run reconciliation *is* a real, respected pattern — but as a **time-boxed migration
   technique** (typically 1–4 weeks) with an explicit decommission date, not a permanent gate.
9. Costs diverge sharply by compute class: classic jobs compute is cheapest, DBSQL serverless ~2x,
   serverless jobs ~5x, at roughly equal runtime on 10–485 GB benchmarks.
10. Recommendation: keep three engines for **three workload classes**, not three copies of one KPI;
    convert parity from an N-way equality gate into a routing + migration-time reconciliation gate.

---

## Q1. Division of labor in real Databricks + dbt + Airflow stacks

### The default is SQL-first

dbt Labs states the rule directly:

> "As a general rule, if there's a transformation you could write equally well in SQL or Python, we
> believe that well-written SQL is preferable: it's more accessible to a greater number of
> colleagues, and it's easier to write code that's performant at scale."
> — [dbt Python models docs](https://docs.getdbt.com/docs/build/python-models)

The accompanying cost argument is explicit: *"Python models are slower to run than SQL models, and
the cloud resources that run them can be more expensive. Running Python requires more general-purpose
compute."* The warehouse optimizes SQL; it runs Python as general compute
([Datafold](https://www.datafold.com/blog/dbt-python/),
[Airbyte](https://airbyte.com/data-engineering-resources/build-dbt-python-models)).

### The orchestration split

The consensus framing across practitioner sources is that the three tools own different concerns:
dbt is the transformation **source of truth** (models, tests, lineage, DAG dependency resolution),
Airflow is the **scheduler and error-handling router**, and Databricks is the **execution engine**.
dbt "compiles transformation logic into SQL that executes on Databricks SQL warehouses, creating a
clear separation between transformation logic and execution infrastructure"
([Metafactor](https://www.metafactor.ca/historians/from-orchestration-to-transformation-harnessing-apache-airflow-and-dbt-for-modern-data-workflows/),
[Medium/Kujawski](https://medium.com/@mariusz_kujawski/databricks-orchestration-databricks-workflows-azure-data-factory-and-airflow-fb44560fac08)).

Note the competitive pressure on this split: Databricks now ships dbt Platform task types in
Lakeflow Jobs, explicitly positioned as "eliminating the need for separate orchestration tools"
([Databricks docs](https://docs.databricks.com/aws/en/jobs/how-to/use-dbt-in-workflows)). A platform
committing to Airflow-as-THE-orchestrator is making a defensible but non-default choice, and should
expect Databricks-native alternatives to keep encroaching.

### When each wins

| Dimension | SQL / dbt on warehouse | PySpark job | Polars (single node) |
|---|---|---|---|
| Cost | Cheapest per unit of set-based work; warehouse optimizer applies | Classic jobs compute is the cheapest Databricks tier but pays cluster overhead | No DBUs at all; no cluster |
| Latency floor | Serverless SQL warehouse starts in ~5–10 s | Classic cluster cold start measured at ~8 min at Decathlon | Process start, effectively zero |
| Capability | Joins, aggregations, window functions, incremental merge | Distributed shuffle, ML, UDFs, arbitrary Python at scale, streaming | Arbitrary Python/Rust on data that fits one node; no distributed shuffle |
| Breaks on | Logic SQL cannot express; row-by-row/iterative algorithms | Small data (overhead dominates); spin-up cost | Data exceeding one node's memory + disk; multi-join complexity |

Concrete cost numbers, from a benchmark across TPC-DS-style scale factors SF100 (9.66 GB),
SF1,000 (97 GB) and SF5,000 (485 GB): classic jobs compute was cheapest; DBSQL serverless ~2x
classic; serverless jobs compute ~5x classic and ~2x DBSQL — while *"DBSQL and Jobs Serverless were
about the same in terms of runtime"*
([Sync Computing](https://medium.com/sync-computing/databricks-compute-comparison-classic-jobs-vs-serverless-jobs-vs-sql-warehouses-235f1d7eeac3)).
Broader pricing guidance: serverless SKUs run roughly $0.70–$0.95/DBU vs $0.40–$0.55 for equivalent
classic compute ([DoiT](https://www.doit.com/blog/databricks-pricing-explained-dbus-tiers-and-cost-control)).

The operative rule from those sources: **short, infrequent jobs belong on serverless; long,
predictable jobs need a cost comparison first.**

---

## Q2. How dbt and PySpark coexist on Databricks

### dbt Python models *are* PySpark

On Databricks, a dbt Python model receives a live `SparkSession` and may return a Spark DataFrame,
a pandas DataFrame, or a pandas-on-Spark DataFrame
([dbt docs](https://docs.getdbt.com/docs/build/python-models)). So the choice is not "dbt vs Spark" —
it is "Spark inside the dbt DAG (with `ref`, lineage, tests, docs) vs Spark outside it."

### Documented limits of dbt Python models

From [dbt's Python models docs](https://docs.getdbt.com/docs/build/python-models) and the
[dbt-databricks configs reference](https://docs.getdbt.com/reference/resource-configs/databricks-configs):

- **Cannot run on a SQL warehouse.** Python requires an all-purpose cluster, job cluster, serverless
  cluster, or workflow job. If your default `databricks_compute` is a SQL warehouse, you must
  override `http_path` per model.
- **Materialization is restricted to `table` and `incremental`.** No `view`, no `ephemeral`.
- **Ephemeral models cannot be referenced** from a Python model
  ([dbt-core#7288](https://github.com/dbt-labs/dbt-core/issues/7288)).
- **Not supported for non-model resources** — no Python tests, no Python snapshots.
- **No `print()` output in dbt logs.** *"The data platform runs and compiles your Python model
  without dbt's oversight."* Debuggability is materially worse than SQL models.
- **"These capabilities are very new. We reserve the right to change the underlying implementation
  for executing Python models in future releases."** dbt Labs' own stability caveat.
- **Time and cost.** Slower and more expensive than SQL models (quoted above).
- Platform support is limited to Snowflake, BigQuery, and Databricks.

### Submission methods (the real cost lever)

`dbt-databricks` v1.9+ offers four submission methods, and the choice is a cost/latency decision:

| Method | Character |
|---|---|
| `all_purpose_cluster` (default) | Most responsive; recommended for **development** |
| `job_cluster` | Cheaper; slower start/stop; recommended for **long-running production models** |
| `serverless_cluster` | Lower ops overhead (v1.9+) |
| `workflow_job` | Persistent reusable workflow; max flexibility; can run outside dbt |

Also relevant: **incremental Python models execute in two phases** — Python on the cluster to build a
staging table, then the merge SQL on `databricks_compute` (which *can* be a SQL warehouse). The
platform's cost model must account for both.

### dbt Python model vs standalone Spark job

The decision rule that falls out of the sources:

- **SQL model** — anything expressible in SQL. Default.
- **dbt Python model** — the transformation cannot be written in SQL (or would take 1000 lines of
  Jinja-SQL), *and* it is still a table-shaped node in the analytics DAG that downstream models
  `ref`. You gain lineage/tests/docs; you accept table/incremental-only and worse debugging.
- **Standalone Spark job** — anything that isn't a table in the analytics DAG: ingestion (Auto
  Loader / Lakeflow Connect / COPY INTO), streaming, ML training, large non-idempotent maintenance,
  or work needing cluster configuration dbt doesn't express. dbt's counter-argument is worth
  quoting: *"spinning up separate infrastructure to orchestrate Python transformations in production
  and different tooling to integrate with dbt is much more time-consuming and expensive"* — so don't
  reach for a standalone job just to avoid a Python model.

Practitioner guidance converges on the same shape: *"Let SQL handle heavy aggregation by pushing
joins, window functions, and large group-bys to upstream SQL models"*; keep Python models "small,
focused, and intentional" ([Airbyte](https://airbyte.com/data-engineering-resources/build-dbt-python-models)).

---

## Q3. Where Polars legitimately fits

### The strongest evidence: Decathlon

[Decathlon's case study on the Polars blog](https://pola.rs/posts/case-decathlon/) is the most
concrete production account found, and it is not vendor marketing fluff — it names hardware, times,
and an explicit adoption rule. It was independently covered by
[InfoQ](https://www.infoq.com/news/2025/12/decathlon-spark-polars).

- Prior state: PySpark on cloud clusters, **180 GiB RAM / 24 cores across 6 workers**.
- Migration test: a Spark job over a **50 GiB Parquet table** (100+ GiB as CSV), moved to Polars'
  streaming engine.
- **Cluster launch: 8 minutes (Spark) vs 2 minutes (Kubernetes).** Their line:
  *"Most of the time, a Polars job will be completed while we are still waiting for the Spark
  cluster to coldstart."*
- Adoption rule: **Polars for all new pipelines where input tables are < 50 GiB with stable size
  over time, and complexity is reasonable** — explicitly excluding "multiple joins, dozens of
  aggregations, or exotic functions."
- Streaming engine effect (Polars v1.27.1): a job that needed **100 GiB / 8 CPUs in-memory dropped
  to 10 GiB / 4 CPUs** — "10 times less" memory — at the same 2–3 minute runtime. They conclude
  *"Polars can now be used for pipelines up to TiB of input data."*
- Caveat they name: Kubernetes adds operational complexity and needs DataOps ownership.

### Databricks' own position on Polars

Databricks publishes guidance on Polars and does not treat it as a competitor to route around:
*"Polars can handle surprisingly large ETL workloads on a single machine where memory efficiency is
critical"* ([Databricks blog](https://www.databricks.com/blog/polars-vs-pandas)). Databricks also
ships and recommends **single-node clusters** as "a cost-efficient option for single machine
workloads" ([Databricks](https://www.databricks.com/blog/2020/10/19/announcing-single-node-clusters-on-databricks.html)),
and community guidance is direct: *"If your data is small enough to fit on a single driver node, you
can continue to use Polars/Pandas, or choose a larger driver node"*
([Databricks Community](https://community.databricks.com/t5/data-engineering/running-python-functions-written-using-polars-on-databricks/td-p/152762)).
So Polars *inside* a Databricks single-node cluster is a supported deployment, not a smuggled one.

### The Delta / Unity Catalog integration constraint — read this carefully

`delta-rs` implements the Delta protocol in Rust with Python bindings and no Spark dependency, and
Polars writes Delta directly. But the governed path into **Unity Catalog** has real limits
([Databricks credential vending docs](https://docs.databricks.com/aws/en/external-access/credential-vending)):

- UC **managed** Delta tables: read yes; **write and create are Public Preview**.
- UC **external** Delta tables: read, write, create — all supported.
- Requires `EXTERNAL USE SCHEMA` on the schema, and the metastore must be explicitly enabled for
  external access.
- Not supported: row-filtered or column-masked tables, views, materialized views, online tables,
  OpenSharing tables.

That last bullet matters for a governance-heavy platform: **if a table carries row filters or column
masks, an external Polars writer cannot touch it at all.** Any Polars write path must be designed
around external tables or accept preview-status managed-table writes.

### Lightweight Airflow-task transforms

This is a legitimate Polars slot, with one known sharp edge: *"Airflow's embedded XCom Backend is not
natively compatible with Polars or pandas and can only handle low amounts of data"* — the workaround
is to pass **paths** through XCom and read/write frames to storage
([polars-airflow](https://github.com/qremplak/polars-airflow)). Any Polars-in-Airflow design must
pass references, never frames.

### Verdict on Q3

Polars in production alongside a cloud lakehouse is **real and credibly attested** (Decathlon on
Kubernetes; endjin's practice writeup; Databricks' own single-node guidance). Its honest slots:

1. Local dev/test loops against sampled data (fast, no cluster, no DBUs).
2. Small-to-medium production transforms (Decathlon's < 50 GiB rule) as Airflow tasks or single-node
   Databricks jobs, where **Spark cold-start alone exceeds the whole job**.
3. Reading Delta without a cluster for lightweight checks, profiling, validation, and serving.

Its honest non-slots: anything requiring distributed shuffle, anything writing to a masked/filtered
UC managed table, anything with unbounded or unpredictable input growth.

---

## Q4. Cross-engine parity: is maintaining 3 implementations an industry practice?

**Plainly: no.** Nothing in this research found a credible team hand-maintaining identical business
logic in three engines as a permanent architecture. What exists is three *other* patterns, all of
which avoid the duplication.

### Pattern A — one definition, many compilation targets (the dominant answer)

**Ibis** is exactly this: *"a Python dataframe API that executes on any query engine"*, with 20+
backends, compiling expressions to backend-specific SQL **via SQLGlot** and delegating execution.
The stated payoff is precisely the platform's goal: *"Portable queries run identically on local
DuckDB and production BigQuery"* — develop on DuckDB, deploy to Spark/Databricks *"by changing a
single line of code."* Overhead is minimal because Ibis compiles rather than executes:
*"Ibis performance is effectively DuckDB performance (or BigQuery performance)."*
([Why Ibis?](https://ibis-project.org/why), [ibis-project/ibis](https://github.com/ibis-project/ibis),
[codecentric: "Write Analytical Logic Once, Run It With Everything"](https://www.codecentric.de/en/knowledge-hub/blog/ibis-selecting-the-right-execution-engine-without-rewriting-your-logic))

**SQLGlot** is the lower-level version: a no-dependency parser/transpiler/optimizer across 30+
dialects including DuckDB, Spark/Databricks, Snowflake, BigQuery, Trino. Its AST *"acts as a
dialect-neutral intermediate representation"*
([tobymao/sqlglot](https://github.com/tobymao/sqlglot), [sqlglot.com](https://sqlglot.com/sqlglot.html)).
Note it also ships a Python executor intended *"for unit testing and running SQL natively across
Python objects"* — i.e. even SQLGlot's own multi-engine execution story is framed as a testing tool.

**Substrait** — the cross-engine IR — did not surface in credible production-adoption reporting in
this research. Treat it as not-yet-load-bearing.

**dbt semantic layer / MetricFlow** solves the same problem one level up: metric definitions live in
the modeling layer so downstream tools query one canonical source, avoiding "metric drift (the same
KPI computed slightly differently)"
([dbt Semantic Layer docs](https://docs.getdbt.com/docs/use-dbt-semantic-layer/dbt-sl),
[b-eye](https://b-eye.com/blog/dbt-semantic-layer-scale/)). For a platform whose product *is*
generated KPI pipelines, this is the closest analogue to what the platform already does — and the
industry's answer there is emphatically **one definition, generated many ways**, never *N*
hand-maintained definitions.

### Pattern B — dual-run reconciliation, time-boxed

Parallel running two implementations and diffing outputs is real and respected — as a **migration**
technique. The consistently reported shape: run both systems **1–4 weeks** (a typical plan cites a
14-day parallel run), reconcile with row counts, checksums, and business-rule checks, then
**decommission the old path**
([Streamkap](https://streamkap.com/resources-and-guides/data-migration-best-practices),
[DQOps](https://dqops.com/data-migration-testing-definition-examples/)). Record-level diffing
(e.g. Spark SQL `EXCEPT`) is used where correctness is critical, being *"far more precise than just
counting rows"* ([Datagaps](https://www.datagaps.com/blog/data-reconciliation-best-practices/)).

The defining property is the **end date**. A parallel run without a decommission plan isn't a
migration pattern; it's permanent double maintenance wearing a migration's clothes.

### Pattern C — engine selection per workload, single implementation each

Decathlon is the clean example: they did not keep the Spark version running beside the Polars one.
They defined a threshold rule (< 50 GiB, stable, low complexity), routed each pipeline to exactly one
engine, and migrated.

### Assessment

Three hand-parallel implementations of the same KPI has a real cost profile and thin benefit:

- **Cost is 3x forever.** Every KPI change lands three times, in three languages, reviewed three
  times.
- **Parity gates detect divergence, they don't prevent it** — and they mostly detect *your own*
  transcription bugs, a class of bug that only exists because you wrote it three times.
- **Coverage is illusory at scale.** A parity gate can only run all three engines on data small
  enough for the weakest engine, so the gate validates GB behavior while production runs TB.
- **Some semantics genuinely differ.** Null ordering, float accumulation order, decimal handling,
  and window-function tie-breaking differ across engines; N-way exact equality forces either
  tolerance fudging or lowest-common-denominator logic.

The one genuine benefit — an independent oracle catching a real engine bug or a misread spec — is
obtainable far more cheaply by Pattern B (bounded, sampled, at migration time) than by Pattern A-in-
reverse (permanent triple authorship).

---

## Q5. What breaks at TB scale, per engine

### Polars — the single-node ceiling

- The old ceiling was RAM. The 2025 streaming engine rewrite (morsel-driven, pull-based, with
  **spillable sinks** for joins, group-bys and sorts, spill-to-disk under `POLARS_TEMP_DIR`) moved
  it materially: *"A 100GB inner join can now finish on a 16GB laptop instead of hitting Killed from
  the OOM killer"*
  ([Polars streaming engine](https://deepwiki.com/pola-rs/polars/5.2-streaming-engine),
  [Python News](https://python-news.com/inside-polars-streaming-engine-how-spillable-sinks-handle-larger-than-ram-joins)).
- Decathlon's measured version of the same shift: **100 GiB/8 CPU → 10 GiB/4 CPU**, same runtime.
- Real ceiling now: **no distributed shuffle**. Polars' own answer to that is Polars Cloud's
  distributed engine, launched 2026 — and critically, *"the distributed engine is only available in
  Polars Cloud and there are no plans to make it available in the open source project"*
  ([Polars Cloud launch](https://pola.rs/posts/polars-cloud-launch/),
  [distributed engine docs](https://docs.pola.rs/polars-cloud/run/distributed-engine/)). The launch
  post carries **no published benchmark numbers** and admits unsupported operations *"fall back to a
  single node"*. Do not plan on OSS Polars scaling out.
- OOM under lazy+streaming is still reported in the wild
  ([polars#25180](https://github.com/pola-rs/polars/issues/25180)) — streaming is a large improvement,
  not a guarantee.
- Practical rule from the evidence: **Decathlon's < 50 GiB with stable size** is the conservative
  production threshold; "up to TiB" is achievable on a beefy node for simple pipelines (scans,
  filters, projections, single joins) but is not a claim to design a governed platform against.

### PySpark — overhead at the small end, shuffle at the large end

- **Small end:** JVM init, scheduling, and cluster provisioning dominate. The measured figure here is
  Decathlon's **8-minute cold start**, versus a job that finishes in 2–3 minutes. On Databricks, that
  is minutes of DBUs before the first row is read. Community framing is blunt: *"Spark is complex and
  has a lot of overhead when running on a single node, and it wasn't made to do that."*
- **Large end:** the failure modes are shuffle-shaped, not size-shaped — shuffle spill (RAM → disk →
  RAM), skewed partitions, and `FetchFailedException` from lost shuffle blocks
  ([Databricks KB](https://kb.databricks.com/jobs/job-fails-with-spark-shuffle-fetchfailedexception-error)).
  A partition on the small side of a join that exceeds executor memory spills or OOMs. Spark's win
  at TB is that it *has* a distributed shuffle at all; its tax is that tuning that shuffle is the job.

### Warehouse SQL — cost shape, not capability

- Serverless SQL warehouses start in **~5–10 seconds**, versus minutes for classic clusters — the
  decisive advantage for bursty, per-KPI query workloads.
- But the DBU premium is real: serverless SKUs at ~$0.70–$0.95/DBU vs ~$0.40–$0.55 classic. The
  benchmark finding is that **DBSQL serverless and serverless jobs had comparable runtime with ~2.5x
  cost difference between them** — meaning compute-class choice, not query tuning, is often the
  dominant cost lever.
- The stated rule: bursty/ad-hoc → serverless (no idle payment); steady high-volume → classic on
  reserved instances.

### The size-distribution argument that frames all of this

Jordan Tigani (BigQuery co-founder, MotherDuck CEO) reports that **the median company's analytical
working set is under 100 GB, with the 99th percentile under 10 TB**, and argues the thesis is not
that large *datasets* don't exist but that *big compute* — distributed execution — is unnecessary for
the large majority of workloads
([MotherDuck](https://motherduck.com/videos/the-death-of-big-data-and-why-its-time-to-think-small-jordan-tigani-ceo-motherduck/)).
He also cites BigQuery's **~400 ms minimum overhead** as the coordination tax of distribution.

This is a vendor with an interest in the conclusion, and should be weighted accordingly — but the
underlying observation (queries touch a recent slice, not the whole warehouse) is consistent with
Decathlon's independent finding that most of their pipelines fit under 50 GiB.

---

## Recommended role split for this platform

### The core reframe

Keeping three engines is defensible. Keeping **three implementations of every KPI** is not — the
evidence gives it no industry precedent, a 3x permanent maintenance cost, and a parity gate whose
coverage is capped by the weakest engine. "Keep all three" should mean **three workload classes with
one implementation each**, plus a *generated* second implementation only when a KPI is moving between
classes.

### Proposed distinct job per engine

**SQL / dbt on Databricks SQL — the default, and the system of record.**
Every KPI that can be expressed in SQL is a dbt model, materialized `table` or `incremental`,
executed on a SQL warehouse, orchestrated by Airflow via cosmos. This is where lineage, tests, docs,
governance, masking, and the semantic definition live. Target: the large majority of generated KPIs.
Justification: dbt's own SQL-preferred rule; warehouse optimizer; ~5–10 s serverless start; cheapest
per unit of set-based work.

**PySpark — the distributed and non-SQL-expressible tier.**
Reserved for: (a) platform-owned ingestion (Auto Loader / Lakeflow Connect / COPY INTO) — never a
KPI concern; (b) KPIs whose logic genuinely cannot be SQL (iterative, ML-adjacent, complex UDF); (c)
any KPI whose input exceeds single-node viability or needs a distributed shuffle. Prefer **dbt Python
models with `submission_method: job_cluster`** for (b) and (c) so the node stays in the dbt DAG —
accepting the documented limits (table/incremental only, no `print()`, no ephemeral refs, dbt Labs'
own stability caveat). Drop to a standalone Spark job only for work that isn't a table in the
analytics DAG.

**Polars — the fast/cheap tier and the local loop. Not a parallel KPI implementation.**
Two jobs, both real:
1. **Local development and CI against sampled data.** Zero DBUs, no cluster, sub-second iteration.
   This is where the platform's own test suite should run.
2. **Production small/medium transforms** where Spark cold-start would exceed total runtime — adopt
   Decathlon's rule explicitly: **input < 50 GiB, stable size over time, low join/aggregation
   complexity**. Run as an Airflow task (passing storage paths through XCom, never frames) or a
   Databricks single-node job.
   Constraint to encode: Polars **writes** must target UC **external** Delta tables, or accept
   Public-Preview managed-table writes, and must never target a row-filtered or column-masked table —
   credential vending does not support those at all.

### What the parity gates should become

Current gate (every KPI computed in all three engines, outputs must match) should be retired and
replaced with four narrower gates:

1. **Routing gate (replaces N-way parity).** Each KPI is assigned exactly **one** production engine
   by a recorded, evidence-based rule: input volume, volume stability, join/aggregation complexity,
   SQL-expressibility, and target-table governance flags. The gate asserts the routing decision is
   present, justified, and re-evaluated when profiled volume crosses the threshold. This is the
   Decathlon pattern.

2. **Migration reconciliation gate (replaces parity as a permanent check).** When a KPI *changes*
   engine class, run both for a bounded window and diff at record level (`EXCEPT`-style, not row
   counts), then decommission the old path. Time-boxed with a required decommission date — a parallel
   run without one is double maintenance, not validation.

3. **Reference-oracle gate for the local loop.** Keep a single cheap engine (Polars or DuckDB) as a
   *sampled* correctness oracle in CI against the production SQL — this is the legitimate residue of
   the current parity gate, and it costs one extra implementation of the *executor*, not one extra
   implementation per KPI.

4. **Semantics-tolerance policy, written down.** Where a diff is expected rather than a bug — null
   ordering, float accumulation order, decimal scale, window tie-breaking — record the tolerance
   explicitly rather than distorting the logic to force bit-equality.

### The generation change this implies

Because the platform *generates* KPI code, it is already positioned for the industry's actual answer:
**one definition, many compilation targets**. The KPI definition should be the single authored
artifact; SQL, PySpark, and Polars become *emitted backends* selected by the routing gate, not three
authored files. Where the platform's own mini-DSL falls short, the precedent to study is
[Ibis](https://ibis-project.org/why) (one dataframe API, 20+ backends, compiled via SQLGlot) and
[SQLGlot](https://github.com/tobymao/sqlglot) (dialect-neutral AST, Databricks/DuckDB/Trino among 30+
dialects) — either of which could subsume hand-maintained per-engine emitters.

This is also the fix for the `derived_formula` raw-SQL escape hatch already noted as debt in this
repo: an escape hatch that permanently forfeits Polars/PySpark parity is only a problem under an
N-way-parity model. Under a routing model, a SQL-only KPI is simply a KPI routed to the SQL tier —
which is the default tier anyway.

---

## Source list

**dbt / Python models**
- https://docs.getdbt.com/docs/build/python-models
- https://docs.getdbt.com/reference/resource-configs/databricks-configs
- https://github.com/dbt-labs/dbt-core/issues/7288
- https://github.com/dbt-labs/dbt-core/discussions/5261
- https://www.datafold.com/blog/dbt-python/
- https://airbyte.com/data-engineering-resources/build-dbt-python-models
- https://docs.getdbt.com/docs/use-dbt-semantic-layer/dbt-sl
- https://b-eye.com/blog/dbt-semantic-layer-scale/

**Databricks**
- https://docs.databricks.com/aws/en/jobs/how-to/use-dbt-in-workflows
- https://docs.databricks.com/aws/en/external-access/credential-vending
- https://docs.databricks.com/aws/en/tables/managed
- https://www.databricks.com/blog/expanded-interoperability-unity-catalog-open-apis
- https://www.databricks.com/blog/polars-vs-pandas
- https://www.databricks.com/blog/2020/10/19/announcing-single-node-clusters-on-databricks.html
- https://kb.databricks.com/jobs/job-fails-with-spark-shuffle-fetchfailedexception-error
- https://community.databricks.com/t5/data-engineering/running-python-functions-written-using-polars-on-databricks/td-p/152762

**Cost / compute comparison**
- https://medium.com/sync-computing/databricks-compute-comparison-classic-jobs-vs-serverless-jobs-vs-sql-warehouses-235f1d7eeac3
- https://www.doit.com/blog/databricks-pricing-explained-dbus-tiers-and-cost-control
- https://www.cloudforecast.io/guides/databricks-pricing-costs-guide/

**Polars**
- https://pola.rs/posts/case-decathlon/
- https://www.infoq.com/news/2025/12/decathlon-spark-polars
- https://pola.rs/posts/polars-cloud-launch/
- https://docs.pola.rs/polars-cloud/run/distributed-engine/
- https://deepwiki.com/pola-rs/polars/5.2-streaming-engine
- https://python-news.com/inside-polars-streaming-engine-how-spillable-sinks-handle-larger-than-ram-joins
- https://github.com/pola-rs/polars/issues/25180
- https://endjin.com/blog/polars-faster-pipelines-simpler-infrastructure-happier-engineers
- https://github.com/qremplak/polars-airflow
- https://www.edgarbahilo.com/poor-mans-data-lake-with-polars-deltalake/

**Portability / parity**
- https://ibis-project.org/why
- https://github.com/ibis-project/ibis
- https://www.codecentric.de/en/knowledge-hub/blog/ibis-selecting-the-right-execution-engine-without-rewriting-your-logic
- https://github.com/tobymao/sqlglot
- https://sqlglot.com/sqlglot.html
- https://www.datagaps.com/blog/data-reconciliation-best-practices/
- https://streamkap.com/resources-and-guides/data-migration-best-practices
- https://dqops.com/data-migration-testing-definition-examples/
- https://github.com/microsoft/Functional-Validation-Testing-Spark-SQL

**Scale framing**
- https://motherduck.com/videos/the-death-of-big-data-and-why-its-time-to-think-small-jordan-tigani-ceo-motherduck/
- https://www.metafactor.ca/historians/from-orchestration-to-transformation-harnessing-apache-airflow-and-dbt-for-modern-data-workflows/

---

## Confidence and gaps

- **High confidence:** dbt Python model limits (primary docs), UC credential vending read/write
  matrix (primary docs), Decathlon numbers (named primary case study, independently covered), Ibis
  and SQLGlot as the portability answer (primary project docs).
- **Medium confidence:** Databricks compute cost ratios (one benchmark, one author, TPC-style
  workloads that may not match KPI query shapes — worth re-benchmarking on real KPI SQL).
- **Low confidence / gaps:** Substrait adoption (no credible production evidence surfaced — treat as
  immature); Polars Cloud distributed performance (launch post publishes no numbers); no source
  found documenting *any* team maintaining 3 hand-parallel engine implementations, which is itself
  the finding but is an argument from absence.
