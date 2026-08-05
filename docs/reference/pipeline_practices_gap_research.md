# Pipeline Practices — Gap Research

Version: 2026-08-05 research pass
Scope: gaps beyond `docs/reference/senior_data_engineer_patterns.md`, framed for a platform that
*generates* dbt + Airflow(Cosmos) + Databricks UC pipelines rather than hand-writes them.
Sources are official vendor docs and named-company engineering blogs unless marked otherwise.

## Executive summary

1. Backfill is not one pattern. Airflow 3 has a first-class `backfill create` with reprocess
   policy, its own `max_active_runs` and `--run-backwards`; dbt has `--event-time-start/end`;
   Databricks has append-`ONCE` backfill flows. A generator should emit *all three* seams, not one.
2. `catchup=True` on a generated DAG with a historical `start_date` is a foot-gun that schedules
   thousands of runs; generated DAGs should default `catchup=False` and route history to explicit
   backfill.
3. Cosmos per-model task granularity is the standard, but it trades DAG parse time — the render
   mode (manifest vs `dbt ls`) and `retries>=2` are the two parameters that decide whether it
   survives at scale.
4. Airflow event-driven scheduling (`AssetWatcher` + `BaseEventTrigger`) is the correct source-arrival
   pattern; naive `S3KeyTrigger`-style "does the file exist" sensors fire forever — documented trap.
5. Streaming/batch is settled as *coexistence*: streaming bronze + triggered/batch gold, with a
   periodic batch reconciliation path for data past the watermark. Neither pure lambda nor kappa.
6. On Databricks, dbt `microbatch` compiles to `replace_where`, which **inserts by column order,
   not name** — a silent data-corruption path a generator must defend against.
7. Environment isolation is catalog-per-env (`dev`/`test`/`prod`) with identical schema/table names
   and a distinct service principal per env; UC privileges inherit downward, so grants belong at
   schema level, not per generated table.
8. Write-Audit-Publish does **not** work natively on Delta the way it does on Iceberg branches —
   the Databricks-side equivalent is staging table + tests + shallow clone/atomic swap.
9. Recoverability has a hard clock: Databricks Runtime 18.0+ blocks time travel older than
   `deletedFileRetentionDuration` (default 7 days). "We can always restore" is false by default.
10. dbt never drops a model you deleted. A regenerating pipeline generator silently accumulates
    orphaned tables forever unless it emits a reconcile/decommission step.

## Already covered by `senior_data_engineer_patterns.md` — not repeated here

- On-call triage, RCA workflow, postmortems (§1).
- Slow pipelines: full scans, skew, salting, broadcast, AQE, spill, small files (§2, §16-17).
- Lineage use cases and dbt `exposures` (§3).
- Kimball vs OBT coexistence (§4, §19).
- Schema drift as *silent* success; detect at source (§5).
- Cost: scan-less-first, system tables, liquid clustering vs partitioning, the
  OPTIMIZE-plus-predictive-optimization double-billing trap (§6, §11).
- Airflow scaling knobs: `parsing_processes`, `min_file_process_interval`, PGBouncer,
  multi-scheduler (§7).
- Idempotency canon: replace-don't-append, DLQ, backoff+jitter, watermark determinism,
  truncate-and-reload trap, "exactly-once is an illusion", staging+atomic swap (§8, §15).
- The dbt incremental strategy table and the Snowflake `insert_overwrite` divergence (§12).
- Airflow 3 assets, DAG versioning, native backfill existence (§13).
- Lakeflow/DLT object model, streaming tables vs materialized views, AUTO CDC existence (§14).
- HLL/sketches, metadata-before-data profiling (§18).
- Checkpoints/bookmarks, partial-re-execution pathology, resilient shuffle (§20).
- Watermarks and the on-time / late / too-late split (§21).
- Slim CI headline (`state:modified+`, `--defer`, `--empty`, clone-incrementals) (§"Efficiency and CI").

---

## 1. Backfilling at scale

### The three engines each own a different backfill primitive

**Airflow 3 native backfill.** Triggered from CLI (`airflow backfill create`), UI (Trigger →
Backfill) or REST. The parameters that matter to a generator:

- `reprocess_behavior`: `none` (skip dates that already have a run), `failed` (only re-run failures),
  `completed` (re-run failed *and* completed). A run that is currently running or queued is never
  duplicated.
- `max_active_runs` on the backfill is **independent of** the DAG's own `max_active_runs` — this is
  the throttle that stops a backfill from starving scheduled runs.
- `--run-backwards`: newest partitions first. Right default when the business wants recent data
  correct first and history to trickle.
- `dag_run_conf` JSON is passed through, so a generated DAG can accept a "this is a backfill" flag.
- For partition-based timetables, a date range creates **one DAG run per partition** — this is what
  makes backfill partition-aligned by construction rather than by convention.

Source: [Airflow — Backfill](https://airflow.apache.org/docs/apache-airflow/stable/core-concepts/backfill.html),
[Airflow — DAG Runs](https://airflow.apache.org/docs/apache-airflow/stable/core-concepts/dag-run.html)

**Catchup is the wrong tool and should be off.** A DAG with `catchup=True` and a `start_date` years
back creates thousands of runs at once on first deploy. Practitioner consensus and Astronomer
guidance is `catchup=False` for operational DAGs, and if catchup is genuinely wanted, cap it with
`max_active_runs` (a commonly cited value is 3).

Source: [OneUptime — How to Implement Airflow Backfills](https://oneuptime.com/blog/post/2026-01-28-implement-airflow-backfills/view),
[RisingWave — Avoiding Airflow Backfill Pitfalls](https://risingwave.com/blog/avoiding-airflow-backfill-pitfalls-expert-advice/)

**dbt backfill is an argument, not a job.** With `microbatch`, `dbt run --event-time-start
"2024-09-01" --event-time-end "2024-09-04"` reprocesses exactly that window as independent batch
queries. `--full-refresh` combined with the same flags rebuilds a bounded historical range instead
of the whole table. Each batch replaces its entire time period, so a failed batch leaves no partial
data, and `dbt retry` re-runs **only the failed batches**. All values are UTC; there is no timezone
support.

Source: [dbt — About microbatch incremental models](https://docs.getdbt.com/docs/build/incremental-microbatch)

**Databricks/Lakeflow backfill is a separate flow object.** A backfill is an *append once* flow
against the same streaming table:

```sql
CREATE FLOW backfill_2024 AS INSERT INTO ONCE target_table BY NAME
SELECT * FROM read_files(path, format => "json");
```

Key semantics: the backfill flow stays in the pipeline graph but goes idle after completing; it is
**not** re-run on a normal refresh, but it **is** re-run on a full refresh. Databricks explicitly
recommends splitting a large backfill into multiple flows (e.g. per year) so they process in
parallel. Full refresh with backup-and-backfill is named as "very expensive… last resort"; the
cheaper move for reprocessing from a point in time is resetting the streaming checkpoint (rewind and
replay) while preserving the table.

A hard constraint worth encoding: full refresh is impossible where upstream retention has expired
(Kafka with 24h retention cannot be replayed after 24h). Backfill capability is a function of
*source retention*, not of pipeline design.

Source: [Databricks — Backfilling historical data with pipelines](https://docs.databricks.com/aws/en/ldp/flows-backfill),
[Databricks — Full refresh for streaming tables](https://docs.databricks.com/aws/en/ldp/full-refresh-st),
[Databricks — Recover a pipeline from streaming checkpoint failure](https://learn.microsoft.com/en-us/azure/databricks/ldp/recover-streaming)

### Backfilling under SCD2

Lakeflow `AUTO CDC INTO ... STORED AS SCD TYPE 2` requires `__START_AT` / `__END_AT` columns typed
identically to `sequence_by`. `sequence_by` is what makes out-of-order and replayed events safe —
the engine orders by it rather than by arrival, and a `STRUCT` expression gives multi-column
tiebreaking. This is the mechanism that makes an SCD2 backfill produce the same history as a
real-time run, and it is precisely what a hand-rolled MERGE does not give you.

Source: [Databricks — AUTO CDC INTO](https://docs.databricks.com/aws/en/ldp/developer/ldp-sql-ref-apply-changes-into),
[Databricks — The AUTO CDC APIs](https://learn.microsoft.com/en-us/azure/databricks/ldp/cdc)

### Cost controls on large backfills

- Airflow **pools** cap concurrency for a class of tasks; a dedicated `backfill` pool with few slots
  keeps a replay from starving scheduled runs. `priority_weight` floats latency-sensitive tasks
  above backfill tasks in the same pool. Shopify combines pools, `priority_weight` and isolated
  Celery queues for exactly this.
- The backfill's own `max_active_runs` (above) is the Airflow-3-native version of the same control.
- `--run-backwards` bounds *business* cost by making the useful half of the backfill land first.
- Splitting into per-year flows (Databricks) or per-batch queries (dbt microbatch) makes cost
  incremental and interruptible instead of one unbounded job.

Source: [Shopify Engineering — Lessons Learned From Running Apache Airflow at Scale](https://shopify.engineering/lessons-learned-apache-airflow-scale),
[AWS — Guide to Airflow worker pool optimization in MWAA](https://aws.amazon.com/blogs/big-data/a-guide-to-airflow-worker-pool-optimization-in-amazon-mwaa/)

### Making backfill first-class rather than incident response

The through-line across all three engines: **backfill is first-class when the unit of work is a
declared time partition.** Airflow partition timetables emit one run per partition; dbt microbatch
emits one query per batch; Lakeflow emits one `ONCE` flow per range. If the generated model has no
declared `event_time`/partition grain, backfill degrades to "full refresh and pray" — which is why
declaring the time grain is a generator requirement, not a modeling nicety.

---

## 2. DAG design for dbt-on-Airflow (Cosmos)

**Per-model tasks are the standard; parse time is the bill.** Cosmos renders each dbt node as an
Airflow task, giving per-model status, lineage and targeted retry of only failed models. The stated
trade-offs are slower DAG parsing and worker slots proportional to node count. Large projects hit
`DagBag import timeout`, fixed by raising `core.dagbag_import_timeout` — and, more durably, by
choosing a cheaper render mode.

**Render mode is the real scaling knob.** Cosmos can parse the project by invoking `dbt ls` at
parse time (accurate, slow, runs a subprocess on every parse) or by reading a pre-generated
`manifest.json` (fast, but the manifest must be produced in CI and shipped with the DAGs). For a
*generated* platform this is easy: the generator already produces the project, so it should produce
and ship the manifest too, and pin Cosmos to manifest-based rendering. Astronomer also documents an
experimental "watcher" execution mode claiming up to 80% DAG-execution-time reduction.

**`DbtTaskGroup` over `DbtDag` when there is anything else in the pipeline.** Astronomer's tutorial
leads with `DbtTaskGroup` precisely so the dbt work embeds alongside ingestion/export tasks in one
DAG; `DbtDag` is for the pure-dbt case.

**Retries: at least 2 on every dbt-model task** — Astronomer states this as a best practice, and it
is safe exactly because idempotent incremental strategies make a re-run a no-op-or-replace.

Source: [Astronomer — Orchestrate dbt Core projects with Airflow and Cosmos](https://www.astronomer.io/docs/learn/airflow-dbt),
[astronomer-cosmos](https://github.com/astronomer/astronomer-cosmos),
[GetYourGuide — Airflow & dbt: leveraging Astronomer Cosmos](https://www.getyourguide.careers/posts/airflow-dbt-leveraging-astronomer-cosmos)

### One DAG per pipeline vs per layer

The evidence points to: **split on ownership and cadence, not on medallion layer.** Ingestion and
transform have different failure modes, different retry costs, different SLAs and often different
owners; splitting them into separate DAGs joined by an **asset** (not a sensor) gives each its own
schedule and its own backfill. Splitting bronze/silver/gold into three DAGs of the same pipeline
buys nothing and costs you a cross-DAG dependency for every model edge.

### Ingestion → transform dependency: assets, not sensors

Airflow 3 asset-aware scheduling lets the transform DAG be scheduled *by* the ingestion DAG's
asset outlet, with 3.2 adding triggering on **specific partition updates** (`CronPartitionTimetable`)
rather than on any change. This removes the polling sensor entirely for the internal hop.

Source: [Airflow — Asset-Aware Scheduling](https://airflow.apache.org/docs/apache-airflow/stable/authoring-and-scheduling/asset-scheduling.html)

### Source-data arrival: event-driven, and the documented sensor trap

For *external* arrival, Airflow's event-driven scheduling attaches an `AssetWatcher` to an asset;
the watcher wraps a trigger that must inherit `BaseEventTrigger`. `MessageQueueTrigger` supports
Amazon SQS and Apache Kafka out of the box. External systems can alternatively push asset events via
the REST API.

The trap, stated in the official docs: triggers that check a *persistent state* — "does key X exist
in bucket Y", "is job Z succeeded", "does this row exist" — remain true forever once satisfied, so
they re-fire the DAG indefinitely. `S3KeyTrigger` is named explicitly. Only triggers that model a
genuine *event* (a queue message consumed) are safe. A generator that wires "wait for the file" must
therefore use the event notification, not existence-polling — or use a poke-based sensor inside a
run rather than as a scheduling trigger.

Source: [Airflow — Event-driven scheduling](https://airflow.apache.org/docs/apache-airflow/stable/authoring-and-scheduling/event-scheduling.html),
[AIP-82 External event driven scheduling](https://cwiki.apache.org/confluence/display/AIRFLOW/AIP-82+External+event+driven+scheduling+in+Airflow),
[Astronomer — Event-driven scheduling](https://www.astronomer.io/docs/learn/airflow-event-driven-scheduling)

### Late-arriving data at the DAG level

Two complementary mechanisms, and a generator should choose deliberately:
- **Lookback** inside the model (dbt `lookback=N` batches) — reprocesses the last N windows every
  run, absorbing lateness without any orchestration change. Cost is linear in `N`; community
  guidance is to keep it as low as the data justifies (drop 48h → 24h/12h when lateness is rare).
- **Scheduled bounded backfill** — a low-frequency DAG that re-runs a wider window (weekly re-run of
  the last 30 days), which catches lateness beyond the lookback without paying for it hourly.

Source: [dbt — About microbatch incremental models](https://docs.getdbt.com/docs/build/incremental-microbatch),
[Databricks Community — dbt Cloud + Databricks SQL Warehouse with microbatching (48h lookback)](https://community.databricks.com/t5/data-engineering/dbt-cloud-databricks-sql-warehouse-with-microbatching-48h/td-p/136694)

### Retry/idempotency semantics per task type

| Task type | Safe to retry blindly? | Why / what to set |
|---|---|---|
| dbt model, `merge`/`replace_where`/`insert_overwrite`/`microbatch` | yes | replace semantics; `retries>=2` |
| dbt model, `append` | **no** | duplicates per retry; never generate bare `append` |
| dbt `test` / `source freshness` | yes | read-only |
| Auto Loader / streaming ingest | yes | checkpoint dedupes; but checkpoint must survive (below) |
| `foreachBatch` custom sink | **no** unless `txnAppId`+`txnVersion` set | see §3 |
| Lakeflow `ONCE` backfill flow | idle after success; re-runs on full refresh | make full refresh a gated action |
| External API extract | only with a dedupe key at the landing table | otherwise DLQ + idempotent upsert |

### Shopify's DAG-fleet lessons (directly applicable to a generator)

At 10,000+ DAGs / 150,000+ daily runs: enforce a **DAG policy** at the cluster level (DAG IDs must
match a registered namespace; tasks confined to declared queues and pools); keep a **manifest of DAG
ownership**; prune metadata (28-day retention) or the metadata DB becomes the bottleneck; and
**deterministically randomize schedules by hashing the DAG ID** instead of letting every generated
DAG land on `0 * * * *`. That last one is a one-line change in a generator and it is the difference
between a smooth scheduler and a thundering herd on the hour.

Source: [Shopify Engineering — Lessons Learned From Running Apache Airflow at Scale](https://shopify.engineering/lessons-learned-apache-airflow-scale)

---

## 3. Streaming and batch coexistence

### The 2026 position: neither pure lambda nor pure kappa

Lambda runs parallel batch and streaming paths and merges; kappa runs streaming only and replays the
log for reprocessing. Current writing frames both as historical: the lakehouse ("delta architecture")
answer is one storage layer with ACID that both batch and streaming write into, with the medallion
tiers organising *quality*, not *processing mode*. Medallion explicitly does not separate batch from
stream. Kappa is achievable only where the streaming platform gives exactly-once and ordering **and**
the sinks are idempotent.

Source: [RisingWave — Lambda vs Kappa Architecture: Which Is Better in 2026?](https://risingwave.com/blog/lambda-vs-kappa-architecture-2026/),
[Flexera — Kappa vs Lambda Architecture: a detailed comparison (2026)](https://www.flexera.com/blog/finops/kappa-vs-lambda-architecture/),
[DataGardeners — Medallion vs Lambda Architecture](https://datagardeners.ai/blog/medallion-vs-lambda-architecture)

### Triggered vs continuous is a cost decision with a stated latency boundary

Databricks gives explicit thresholds:
- **Continuous** — when updates are wanted every ~10 seconds to a few minutes. Requires an
  always-on cluster; more expensive, lower latency.
- **Triggered** — refresh available data and stop. Every 10 minutes / hourly / daily. Cluster runs
  only for the update.

Lakeflow Declarative Pipelines is the recommended way to run Auto Loader for most production
ingestion; where latency is not a requirement, run Auto Loader as a **triggered batch job with
`Trigger.AvailableNow`** — which still gets rate-limited multi-micro-batch processing and async file
discovery, at batch cost.

Source: [Databricks — Triggered vs. continuous pipeline mode](https://docs.databricks.com/aws/en/ldp/pipeline-mode),
[Databricks — Configure Auto Loader for production workloads](https://docs.databricks.com/aws/en/ingestion/cloud-object-storage/auto-loader/production)

### Auto Loader production constraints a generator must respect

- **File discovery mode**: file events (recommended, one queue per bucket, incremental discovery),
  classic file notification, or directory listing (costly with continuous triggers because of
  repeated `LIST`).
- **Checkpoint location must not sit under an object lifecycle policy.** If lifecycle rules delete
  checkpoint files, "the stream state is corrupted". This is the single most common generated-infra
  mistake — the platform writes checkpoints into the same bucket prefix that has a 30-day expiry.
- Run file-event streams **at least once every 7 days** or you pay for a full directory listing.
- `cloudFiles.maxFilesPerTrigger` (default 1000, hard limit) and `cloudFiles.maxBytesPerTrigger`
  (soft) — whichever hits first governs. These are the ingest-side cost throttle.
- `cloudFiles.maxFileAge` (minimum 14 days; 90 days recommended conservatively) and
  `cloudFiles.backfillInterval` for files missed due to notification-retention limits.
  `cloudFiles.cleanSource` archives/deletes processed files to keep discovery cheap.

Source: [Databricks — Configure Auto Loader for production workloads](https://docs.databricks.com/aws/en/ingestion/cloud-object-storage/auto-loader/production)

### Keeping KPI marts consistent over a streaming bronze

The practical pattern that falls out of the sources:

1. Bronze streams continuously (or triggered) — append-only, retains raw so replay is possible.
2. Silver applies CDC/dedupe with a `sequence_by`, so out-of-order and replayed events converge.
3. Gold/KPI marts run **triggered on a defined grain**, not continuously — so a KPI number is a
   function of a closed window and is reproducible. A KPI computed over a continuously-updating
   stream has no stable "as of", which is the same non-determinism class as `CURRENT_DATE` in
   generated SQL (baseline §15).
4. A periodic batch reconciliation reprocesses records that fell past the watermark (baseline §21) —
   this is the residual "batch layer" that survives from lambda, and it is what makes the mart
   eventually correct rather than merely fresh.

Watermarking basics a generator must respect: the event-time column and lateness threshold must be
declared per stateful operation; without one, state grows unbounded; records past the watermark are
**dropped silently** by the streaming job, so the batch reconciliation path is not optional if
completeness matters (baseline §21).

### Exactly-once at the sink: `txnAppId` / `txnVersion`

Structured Streaming's exactly-once guarantee via checkpoints **does not extend to `foreachBatch`**.
Any custom multi-table write inside `foreachBatch` can duplicate on retry unless each write carries
`txnAppId` (a stable unique string, e.g. the StreamingQuery ID) and `txnVersion` (monotonically
increasing batch id). Delta then recognises and ignores duplicate writes. Critical caveat: if the
checkpoint is deleted and the query restarted with a new checkpoint, you **must** change `txnAppId`,
or the restart replays batch ids that Delta will suppress as duplicates.

Source: [Databricks — Delta Lake table streaming reads and writes](https://docs.databricks.com/aws/en/structured-streaming/delta-lake),
[Databricks — Use foreachBatch to write to arbitrary data sinks](https://docs.databricks.com/aws/en/ldp/for-each-batch)

---

## 4. Incremental processing correctness (Databricks specifics)

The baseline covers the strategy table. What it does not cover is the Databricks adapter's actual
behaviour, which is where the correctness bugs live.

### dbt-databricks strategies and their traps

| Strategy | File format | Requires | Trap |
|---|---|---|---|
| `append` | all | — | duplicates; never generate it bare |
| `merge` (default) | delta/hudi | `unique_key` (else behaves as `append`) | **no `unique_key` silently degrades to append** |
| `insert_overwrite` | all | `partition_by` or `liquid_clustered_by` recommended | **without partitions it atomically replaces the whole table** |
| `replace_where` | delta, DBR 12.0+ | `incremental_predicates` | **inserts by column order, not name** |
| `delete+insert` | delta, DBR 12.2 LTS+ | `unique_key` | separate DELETE+INSERT below DBR 17.1 (non-atomic) |
| `microbatch` | delta | `event_time` | implemented *as* `replace_where` — inherits its column-order hazard |

Two of those are silent-wrong-data paths, not errors:
- **`merge` without `unique_key` acts like `append`.** A generator that emits `merge` but cannot
  resolve a key produces a duplicating model that looks correct in config.
- **`replace_where` (and therefore `microbatch`) inserts by column order.** Reorder a `select` and
  values land in the wrong columns with no error. dbt-databricks 1.11.0 fixes the related
  `on_schema_change: sync_all_columns` corruption by using `INSERT BY NAME`, which requires
  **DBR 12.2 LTS or higher** — below that, pin to 1.10.x.

Source: [dbt — Databricks configurations](https://docs.getdbt.com/reference/resource-configs/databricks-configs)

### `on_schema_change` is a correctness config, not a convenience

Default is `ignore`: **new columns are silently dropped** on incremental runs. Options:
`fail` (loud, right for development and for contract-enforced marts), `append_new_columns` (adds
columns, existing rows get NULL — the pragmatic production choice), `sync_all_columns` (adds *and
drops*, inclusive of type changes — will delete a column's data if a refactor drops it from the
`select`). A generator must set this explicitly; leaving the default is choosing silent drift.

Source: [dbt — Configure incremental models](https://docs.getdbt.com/docs/build/incremental-models),
[Vermorel — on_schema_change in dbt incremental models](https://adriennevermorel.com/notes/on-schema-change-in-dbt-incremental-models/)

### Microbatch specifics that decide cost

- `event_time`, `begin`, `batch_size` (`hour|day|month|year`) are all **required**.
- `lookback` defaults to **1** batch. Late data older than one batch is missed by default.
- `concurrent_batches` controls parallel vs sequential batch execution.
- **`event_time` must be declared on upstream parents too**, or dbt performs a **full table scan per
  batch**. This is the single largest microbatch cost mistake, and it is invisible until the bill
  arrives. `.render()` opts out of auto-filtering — never generate it for large tables.
- All values are UTC; no timezone support.

Source: [dbt — About microbatch incremental models](https://docs.getdbt.com/docs/build/incremental-microbatch)

### Which strategy for which grain

Synthesis of the above, in the form a generator can encode:

- **Time-series fact, declared event time, large** → `microbatch` (batch_size at the natural grain,
  `lookback` from measured lateness). Gets bounded backfill and per-batch retry for free.
- **Time-series fact, no partition/event column** → do not generate an incremental model; either
  derive an event column or materialize as `table`. `append` is not an answer.
- **Entity/dimension keyed by a stable PK, corrections expected** → `merge` with `unique_key` plus
  `matched_condition` on a change timestamp so unchanged rows are not rewritten.
- **Bounded predicate rebuild (e.g. "always rebuild the last 30 days")** → `replace_where` with
  explicit `incremental_predicates`, with a generated column-order guard.
- **SCD2 history** → Lakeflow `AUTO CDC ... SCD TYPE 2` with `sequence_by`, not a hand-rolled MERGE.
  Baseline §12's noted defect (MERGE-on-PK cannot correct a changed PK value) is structural; the
  engine-native CDC path is the documented escape.

### Exactly-once vs at-least-once in practice

Baseline §15 already names exactly-once as largely illusory. The Databricks-specific refinement:
at-least-once delivery + idempotent write is achieved concretely by (a) replace-semantics
incremental strategies, (b) `txnAppId`/`txnVersion` for custom sinks, (c) `sequence_by` for CDC
ordering. All three are configuration, not code — which is exactly what a generator can guarantee
and a human reviewer routinely forgets.

---

## 5. Operability grey areas for a *generated* pipeline

### Environments: catalog-per-env, identical names below it

Consensus across Databricks and dbt docs: **catalog is the primary unit of isolation**; one catalog
per environment (`dev` / `test` / `prod`), one metastore per region, medallion layers as schemas
inside each catalog. Parameterize the catalog by environment and keep schema and table names
identical so code promotes without renaming.

dbt's UC guidance adds the access matrix:

| Role | Source/bronze | Dev catalog | Prod catalog | Test catalog |
|---|---|---|---|---|
| Developers | `SELECT` | `SELECT`+`MODIFY` | `SELECT` or none | none |
| Prod service principal | `SELECT` | none | `SELECT`+`MODIFY` | none |
| Test/CI service principal | `SELECT` | none | none | `SELECT`+`MODIFY` |

Plus: raw data is a dbt **source**, read-only in every environment; **all environments read the same
source data** so dev results replicate in prod; **only top-level catalogs are created manually** —
dbt creates schemas.

Source: [Databricks — Unity Catalog best practices](https://learn.microsoft.com/en-us/azure/databricks/data-governance/unity-catalog/best-practices),
[dbt — Best practices for dbt and Unity Catalog](https://docs.getdbt.com/best-practices/dbt-unity-catalog-best-practices)

### Grants automation as pipelines create new schemas

UC privileges **inherit downward**: a privilege granted on a schema applies to all tables, views,
volumes and functions in it, **including ones created in the future**. That resolves the whole
"generator makes a new table, who can read it" problem: grant at the schema (or catalog) level once,
and generated objects inherit. Per-table grants are the anti-pattern — they must be re-issued on
every regeneration.

Where per-object grants are genuinely needed, dbt's `grants` config expresses them declaratively and
dbt computes the minimal permission change. For the catalog/schema scaffolding itself, Terraform
(`databricks_grants`) is the documented IaC path.

Source: [Databricks — Unity Catalog permissions model concepts](https://learn.microsoft.com/en-us/azure/databricks/data-governance/unity-catalog/access-control/permissions-concepts),
[Databricks blog — Simplify access policy management with privilege inheritance](https://databricks.com/blog/simplify-access-policy-management-privilege-inheritance-unity-catalog),
[dbt — grants config](https://docs.getdbt.com/reference/resource-configs/grants)

### CI/CD for dbt: the caveats the baseline's one-paragraph summary omits

`state:modified` + `--defer` is right, but it is leaky, and a generator that regenerates files
wholesale will hit every leak:

- **Seeds >1 MiB** are compared by path only, not content — a changed large seed is invisible.
- **Macros** propagate transitively: everything depending on a changed macro is "modified". A
  generator that rewrites a shared macro marks the entire project modified, defeating slim CI.
- **`var()` / `env_var()`** changes are not reliably tracked; env-var-driven source configs produce a
  *false positive* on every run (a known issue), fixed partly by the
  `state_modified_compare_more_unrendered_values` behaviour flag.
- **Tests with multiple parents** (e.g. `relationships`) run across two environments when one parent
  is deferred — referential-integrity tests are meaningless in that state. Documented workaround:
  `dbt test -s state:modified --exclude test_name:relationships`.
- **Never set `--state` and `--target-path` to the same path** — non-idempotent, breaks comparison.
  dbt overwrites `manifest.json` during parsing, hence the "Saved manifest not found" error; write
  the production manifest to a dedicated `state/` folder (or object storage) instead.
- **Static `partitions=` in model config** is a known false-positive source.

Source: [dbt — Caveats to state comparison](https://docs.getdbt.com/reference/node-selection/state-comparison-caveats),
[dbt-core #3645 — partition config causes state:modified false positive](https://github.com/dbt-labs/dbt-core/issues/3645),
[dbt-core #10518 — env_vars and state:modified](https://github.com/dbt-labs/dbt-core/discussions/10518)

**Unit tests are the right CI gate for generated SQL.** dbt unit tests (1.8+) validate modeling
logic against static inputs before materializing anything, run in dev/CI only, and are the correct
place to prove that generated SQL handles nulls/zeros/negatives — data tests validate *data*, unit
tests validate *code*. For a platform that generates SQL, the generator should also generate the
unit test: it is the only artifact that fails when the generation logic breaks rather than when the
data does.

Source: [dbt — Unit tests](https://docs.getdbt.com/docs/build/unit-tests),
[Datacoves — dbt Testing: data tests, unit tests, testing packages](https://datacoves.com/post/dbt-test-options)

### Write-Audit-Publish: not free on Delta

WAP (Netflix origin) is write to a staging/audit table → run quality checks → publish to production.
On **Iceberg** this is a branch: enable WAP, write to a branch, fast-forward to main. On **Delta you
cannot do this directly** — the Databricks approach is a shallow clone plus hand-rolled publish
logic, or the plainer staging-table + tests + atomic swap (which is also baseline §15's prescription
for the truncate-and-reload trap). Worth stating plainly so the generator does not promise
branch-like semantics it cannot deliver on Delta.

Source: [lakeFS — How to implement Write-Audit-Publish](https://lakefs.io/blog/how-to-implement-write-audit-publish/),
[Cortland Goffena — dbt Write-Audit-Publish](https://medium.com/@cortlandgoffena/dbt-write-audit-publish-9b5fc6bbd73d),
[handsondata — The Write-Audit-Publish Pattern](https://handsondata.substack.com/p/the-write-audit-publish-pattern)

### Disaster recovery / restore has a 7-day default clock

- `delta.logRetentionDuration` defaults to **30 days** (how long history is kept).
- `delta.deletedFileRetentionDuration` defaults to **7 days** (when VACUUM may remove unreferenced
  data files).
- Time travel needs **both** log and data files. Once VACUUM removes files, "you can't restore a
  table to an older version that references those files."
- **DBR 18.0+ blocks time travel queries older than `deletedFileRetentionDuration`** — so the
  effective restore window is 7 days unless you raise it (and pay storage).
- `RESTORE TABLE t TO VERSION AS OF n` / `TO TIMESTAMP AS OF ts`; requires `MODIFY`; **RESTORE is a
  data-changing operation and can cause duplicates in downstream streaming workloads.**

A generator that materializes a "we can always roll back" story must actually set the retention
properties on the tables where that story needs to hold, and must record the restore's effect on
downstream streams.

Source: [Databricks — Work with Delta Lake table history](https://docs.databricks.com/aws/en/delta/history)

### Environment promotion of the Databricks side: Asset Bundles

`databricks.yml` declares `targets` (dev/staging/prod) with per-target workspace URL and resource
overrides; `mode: development` vs `mode: production`; **presets** such as `name_prefix` and
`trigger_pause_status: PAUSED` for staging (presets override mode defaults; per-resource settings
override presets). Production deploys `run_as` a **service principal**. CI shape:
`bundle validate` on PR → `bundle deploy --target prod` on merge.

Source: [Databricks — Bundle deployment modes](https://docs.databricks.com/aws/en/dev-tools/bundles/deployment-modes),
[Databricks Community — CI/CD with Asset Bundles and GitHub Actions](https://community.databricks.com/t5/community-articles/ci-cd-on-databricks-with-asset-bundles-dabs-and-github-actions/td-p/149565)

### Observability: what a generated pipeline must emit

The emerging vocabulary (OpenTelemetry semantic-conventions proposal for data pipelines) names the
metric set directly: `pipeline.rows.written`, `pipeline.run.duration`, `pipeline.freshness.lag`,
`pipeline.quality.rules.failed`, `pipeline.cost.usd`. OpenLineage is the interchange standard —
`apache-airflow-providers-openlineage` emits run events per task automatically; dbt integration emits
START / COMPLETE (with row-count and schema facets) / FAIL per model, and carries a
`DataQualityMetrics` facet (row counts, null counts) and `ColumnLineage`.

Freshness as a contract: dbt `freshness` with `warn_after` / `error_after` (count + period) and
`loaded_at_field`, configured under `config` in dbt 1.9+; model-level freshness is in progress
(dbt-core #12719). Completeness thresholds ("99.9% of PKs present vs source of truth") are the
paired metric.

Source: [OpenTelemetry semantic-conventions #3762 — pipeline.\* conventions](https://github.com/open-telemetry/semantic-conventions/issues/3762),
[OpenLineage as the spine of data observability](https://datalakehousehub.com/blog/2026-05-openlineage-observability/),
[dbt — freshness](https://docs.getdbt.com/reference/resource-properties/freshness),
[dbt-core #12719 — Model freshness checks](https://github.com/dbt-labs/dbt-core/issues/12719)

### Cost observability per pipeline: query tags are the missing link

dbt-databricks **v1.11+** supports `query_tags` at profile level (`team`, `cost_center`,
`project_name`, `env`) and model level (model wins), and **auto-injects** `@@dbt_model_name`,
`@@dbt_materialized`, `@@dbt_core_version`, `@@dbt_databricks_version`. Tags surface as a
`MAP<STRING,STRING>` in **`system.query_history`** and in the Query Profile UI:

```sql
SELECT query_tags['team'], query_tags['project_name'], execution_time_ms
FROM system.query_history
WHERE query_start_time > current_date() - 7
```

This gives per-model, per-team, per-env, per-materialization cost attribution with zero bespoke
instrumentation. For serverless, **budget policies** propagate tags into `system.billing.usage`'s
`custom_tags` column. Best practice named in the vendor post: always tag environments separately so
dev queries are excluded from cost analysis.

Source: [Databricks blog — Granular usage attribution for dbt pipelines with query tags](https://www.databricks.com/blog/granular-usage-attribution-dbt-pipelines-query-tags),
[Databricks — Attribute usage with serverless budget policies](https://docs.databricks.com/aws/en/admin/usage/budget-policies),
[Databricks — Monitor costs using system tables](https://docs.databricks.com/aws/en/admin/usage/system-tables)

---

## 6. What both the existing report and the question list missed

### 6.1 Deletion and decommission — dbt never drops what you removed

If a model is deleted or renamed in the project, **dbt does not drop the old relation**; this is
deliberate (safety over cleanliness). Over time this produces orphaned tables that downstream users
still query, stale numbers that look live, and pure storage cost. For a *generator* this is
qualitatively worse than for a hand-written project: every regeneration that renames or drops a model
leaves a ghost, and there is no human who remembers it existed. Community answer is a reconcile
macro/package (`dbt-orphan`) that diffs the manifest against the information schema and emits
`DROP`s, **dry-run by default, destructive action opt-in**.

Source: [dbt — How do I remove deleted models from my data warehouse?](https://docs.getdbt.com/faqs/Models/removing-deleted-models),
[dbt Community — Clean your warehouse of old and deprecated models](https://discourse.getdbt.com/t/clean-your-warehouse-of-old-and-deprecated-models/1547),
[Data Engineer Things — Automatically dropping orphaned tables in dbt](https://blog.dataengineerthings.org/automatically-dropping-orphaned-tables-in-dbt-bb037984437a)

### 6.2 Late-arriving dimensions / early-arriving facts

Neither the baseline nor the question list covers this, and it is the most common *silent* wrongness
in a generated star schema: a fact arrives carrying a dimension business key that has no dimension
row yet. Kimball's answer is the **inferred member**: insert a placeholder dimension row immediately
carrying the business key, an `is_inferred` flag, and unknown/default attributes; assign its
surrogate key to the fact; then **type-1 overwrite** the placeholder when the real attributes arrive.
The failure modes if you do not: facts silently dropped by an inner join, or facts pointing at an
"unknown" member forever with no path back.

A generator that emits dimension loads must decide this explicitly, because the alternative default
(inner join, drop the fact) loses revenue rows.

Source: [Kimball Group — Late Arriving Dimension](https://www.kimballgroup.com/data-warehouse-business-intelligence-resources/kimball-techniques/dimensional-modeling-techniques/late-arriving-dimension/),
[Kimball Design Tip #57 — Early Arriving Facts (PDF)](http://www.kimballgroup.com/wp-content/uploads/2012/05/DT57EarlyArriving.pdf),
[Kimball Design Tip #171 — Unclogging the fact table surrogate key pipeline](https://www.kimballgroup.com/2015/01/design-tip-171-unclogging-fact-table-surrogate-key-pipeline/)

### 6.3 Checkpoint and state lifecycle as first-class infrastructure

Streaming checkpoints, dbt `state/` manifests and Cosmos's cached manifest are all *stateful
artifacts the generator produces*, and each has a documented way to be destroyed by ordinary infra
policy: object-lifecycle rules deleting a checkpoint (corrupt stream), `--state` pointed at
`--target-path` (broken slim CI), a stale cached manifest (DAG renders yesterday's graph). Treat
these as named, versioned, lifecycle-exempt locations, not as scratch.

### 6.4 Schedule spread and the thundering herd

Covered above under Shopify, but worth calling out as its own item because it is unique to
*generated* fleets: the generator picks the cron. Hash the pipeline identity into the minute offset.
A hundred generated DAGs all defaulting to `0 2 * * *` is a self-inflicted incident.

### 6.5 Backfill capability is bounded by source retention

Restated as a design rule because it silently invalidates the whole backfill story: Kafka retention,
Auto Loader `maxFileAge`, `cloudFiles.cleanSource` deleting processed files, and Delta
`deletedFileRetentionDuration` each independently cap how far back "we can reprocess" is true. A
generated pipeline should record its **replay horizon** as a declared property, computed from the
minimum of those, rather than implying unlimited backfill.

---

## What the pipeline generator must encode

Checklist of behaviours and parameters the blueprint and generated artifacts must carry. Each item
traces to a section above.

**Time grain and backfill (§1, §4)**
- [ ] Every incremental model declares an `event_time` (or partition) column; if none can be derived,
      refuse to generate an incremental model rather than degrading to `append`.
- [ ] `event_time` is also declared on **upstream** parents, or the model is flagged as
      full-scan-per-batch with an explicit cost note.
- [ ] `batch_size`, `begin`, and `lookback` are explicit parameters, `lookback` derived from measured
      lateness rather than left at the default of 1.
- [ ] A backfill entrypoint is generated per pipeline: dbt `--event-time-start/--event-time-end`,
      Airflow `backfill create` with `reprocess_behavior` + backfill-scoped `max_active_runs`
      (+ `--run-backwards` where recency-first is right), and/or a Lakeflow `ONCE` append flow.
- [ ] Large backfills are split into bounded ranges (per year/month), never one unbounded job.
- [ ] A dedicated Airflow **pool** and lower `priority_weight` for backfill tasks.
- [ ] The pipeline records a declared **replay horizon** = min(source retention, `maxFileAge`,
      `deletedFileRetentionDuration`, checkpoint availability).
- [ ] Full refresh is a gated action (it re-runs `ONCE` backfill flows and may be unreplayable).

**DAG shape (§2)**
- [ ] `catchup=False` on every generated DAG; history goes through explicit backfill.
- [ ] Split DAGs by ownership/cadence (ingest vs transform), joined by an **asset**, not a sensor.
- [ ] Cosmos render mode = pre-generated `manifest.json` shipped by the generator; not `dbt ls` at
      parse time. Raise `core.dagbag_import_timeout` as a fallback, not the fix.
- [ ] `retries >= 2` on every dbt model task; retries disabled or guarded on any non-idempotent task.
- [ ] Source-arrival triggers use event-driven `AssetWatcher` + `BaseEventTrigger`
      (`MessageQueueTrigger` for SQS/Kafka) — never an existence-check trigger like `S3KeyTrigger`
      as a *scheduling* trigger.
- [ ] Cron minute/hour offset derived from a hash of the pipeline identity (schedule spreading).
- [ ] DAG id conforms to a registered namespace; owner recorded in a manifest; tasks confined to
      declared pools/queues (DAG policy enforcement).
- [ ] Airflow metadata retention policy declared (Shopify: 28 days).

**Incremental correctness (§4)**
- [ ] `merge` is never emitted without a resolved `unique_key` (silently becomes `append`).
- [ ] `insert_overwrite` is never emitted without `partition_by` / `liquid_clustered_by` (silently
      replaces the whole table).
- [ ] `replace_where` / `microbatch` models emit columns in a **stable, name-asserted order**, and a
      generated test asserts column order/names (insert-by-position hazard). Require
      dbt-databricks >= 1.11 + DBR >= 12.2 LTS where `INSERT BY NAME` is needed.
- [ ] `on_schema_change` set explicitly per model — `fail` for contract-enforced marts,
      `append_new_columns` for evolving ones; never left at the `ignore` default.
- [ ] SCD2 emitted via engine-native CDC (`AUTO CDC ... SCD TYPE 2` with `sequence_by`, `__START_AT`
      / `__END_AT` typed to `sequence_by`), not hand-rolled MERGE-on-PK.
- [ ] Late-arriving-dimension policy declared per fact: inferred-member placeholder + type-1
      overwrite, or explicit unknown-member, never a silent inner-join drop.

**Streaming (§3)**
- [ ] Triggered vs continuous chosen from a declared latency SLO (continuous only below ~a few
      minutes); default triggered + `Trigger.AvailableNow`.
- [ ] Checkpoint locations written to a path **exempt from object lifecycle policies**, and recorded
      as durable state.
- [ ] File discovery mode declared (file events preferred); `cloudFiles.maxFilesPerTrigger` /
      `maxBytesPerTrigger` set as the ingest cost throttle; `maxFileAge` / `backfillInterval` set.
- [ ] Any `foreachBatch` sink carries `txnAppId` + `txnVersion`; `txnAppId` changes whenever the
      checkpoint is reset.
- [ ] KPI/gold marts computed on a **closed window** with a pinned as-of, never continuously.
- [ ] A periodic batch reconciliation job exists for records past the watermark.

**Environments, CI/CD, access (§5)**
- [ ] Catalog-per-environment (`dev`/`test`/`prod`); schema and table names identical across envs;
      catalog parameterized, not name-mangled.
- [ ] Distinct service principal per environment with the documented grant matrix; developers have
      no write to prod; CI writes only to test.
- [ ] Grants issued at **schema/catalog** level so generated objects inherit; per-table grants only
      as declared exceptions via dbt `grants`.
- [ ] Raw/bronze exposed as dbt `sources`, read-only in all envs; all envs read the same source data.
- [ ] Slim CI: `state:modified+ --defer`, production manifest in a dedicated `state/` path in object
      storage (never `--state == --target-path`), `state_modified_compare_more_unrendered_values`
      enabled, `relationships` tests excluded from deferred CI runs, and awareness that a
      regenerated shared macro invalidates the whole selector.
- [ ] Generated **unit tests** (static inputs) accompany generated SQL — they fail when generation
      logic breaks, which data tests cannot detect.
- [ ] Publish path is staging table → tests → atomic swap (Delta has no Iceberg-style WAP branch);
      shallow clone only where publish logic is hand-rolled deliberately.
- [ ] Databricks-side resources deployed via Asset Bundles targets with `mode: production` and
      `run_as` a service principal; staging preset `trigger_pause_status: PAUSED`.

**Recoverability and observability (§5, §6)**
- [ ] `delta.deletedFileRetentionDuration` / `logRetentionDuration` set deliberately on tables whose
      restore window matters; the effective restore window is published, not assumed.
- [ ] RESTORE runbook notes its duplicate risk for downstream streaming consumers.
- [ ] Each run emits: rows written, run duration, freshness lag, failed quality rules, cost — via
      OpenLineage events (Airflow provider + dbt) with row-count/schema/column-lineage facets.
- [ ] `freshness` (`warn_after` / `error_after` / `loaded_at_field`) declared on every source; a
      completeness threshold declared alongside.
- [ ] dbt-databricks `query_tags` set at profile level (`team`, `cost_center`, `project_name`, `env`)
      so per-model cost is queryable from `system.query_history`; serverless budget policies applied
      so tags reach `system.billing.usage.custom_tags`; dev tagged distinctly from prod.
- [ ] A **reconcile/decommission** step diffs the manifest against the information schema and reports
      orphaned relations, dry-run by default, destructive drop opt-in.

---

## Confidence notes

- **High** (first-party docs, direct quotes verifiable): Airflow backfill semantics, event-driven
  scheduling caveat, dbt microbatch config, dbt-databricks strategy table and its traps,
  `on_schema_change` semantics, state-comparison caveats, Auto Loader production settings, Delta
  retention/RESTORE, UC privilege inheritance, dbt UC grant matrix, query tags, Lakeflow backfill
  flows, `txnAppId`/`txnVersion`.
- **High** (named-company engineering blog): Shopify's Airflow-at-scale lessons.
- **Medium**: the "2026 answer is neither lambda nor kappa" framing — consistent across several
  secondary sources but vendor-adjacent; the Cosmos "watcher mode up to 80% faster" figure is a
  vendor claim about an experimental mode; orphaned-table cleanup packages are community, not
  first-party.
- **Medium**: the WAP-on-Delta limitation is stated by practitioner sources rather than by
  Databricks docs directly — the *workaround* (staging + swap) is well supported, the framing
  "Delta cannot do branch-WAP" is secondary-sourced.
