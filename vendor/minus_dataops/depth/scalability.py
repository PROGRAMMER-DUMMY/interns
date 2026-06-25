"""Deep dossier for step 6: Scalability, Backfills & DataOps.

The framework step is the short, interview-ready spine. This module is the deep
layer: how to make loads idempotent, how to backfill history without breaking the
live pipeline, how to evolve schemas without shattering downstream, how distributed
compute actually scales (and where it stalls -- shuffle, skew, small files), and the
DataOps practices (CI/CD, recovery, cost control) that keep all of it running.

Content stays tool- and cloud-agnostic: Delta/Iceberg/Hudi, Spark/Flink, Airflow,
spot/preemptible instances are named only as representative examples of a category.
ASCII only, no emojis; frozen tuples so the knowledge is immutable.
"""

from __future__ import annotations

from minus_dataops.depth._schema import (
    Comparison,
    DeepStep,
    Heuristic,
    Option,
    Topic,
)


# ---------------------------------------------------------------------------
# Topics
# ---------------------------------------------------------------------------

_TOPICS: tuple[Topic, ...] = (
    Topic(
        name="Idempotency -- the golden rule",
        summary=(
            "A pipeline run is idempotent when running it once or N times on the same "
            "input produces the same output -- no duplicate rows, no destroyed history. "
            "Failure + retry is the normal case at scale, so every load must be safe to "
            "re-run."
        ),
        points=(
            "Flat INSERT is not idempotent: a retried task appends the same rows again "
            "and doubles them. Use MERGE/upsert keyed on a stable primary key so a re-run "
            "updates the existing row instead of inserting a copy.",
            "An idempotency key is the deterministic identity of a record (natural key, or "
            "a hash of the immutable business columns). The sink keys on it so the same "
            "event maps to the same row no matter how many times it is delivered.",
            "Streaming sources give at-least-once delivery, so duplicates are expected. "
            "Dedup inside a bounded window (event-time watermark) on the idempotency key, "
            "then MERGE -- unbounded dedup state grows forever.",
            "True exactly-once at the sink is the combination of an idempotent write "
            "(MERGE/upsert keyed) plus a transactional sink (atomic commit of data + "
            "offset/checkpoint). Either alone leaks duplicates on a mid-write crash.",
            "Make derived columns deterministic: avoid now()/random()/auto-increment ids "
            "inside the transform, or a re-run produces different values for the same input "
            "and breaks idempotency.",
            "Idempotency is per-partition/per-batch: design the load so re-running one "
            "date or one micro-batch only touches that slice, never the whole table.",
        ),
        examples=(
            "MERGE INTO silver t USING staged s ON t.event_id = s.event_id WHEN MATCHED "
            "THEN UPDATE WHEN NOT MATCHED THEN INSERT -- safe to run twice.",
            "Idempotency key = sha256(order_id || line_no) when no single natural key "
            "exists for an order line.",
        ),
    ),
    Topic(
        name="Backfill strategies",
        summary=(
            "A backfill reprocesses historical data -- to fix a bug, to apply a changed "
            "business definition, or to populate a new column. The hard part is doing it "
            "atomically and without starving the live pipeline."
        ),
        points=(
            "Atomic partition overwrite: with an open table format, recompute one date's "
            "partition and replace it in a single atomic commit (dynamic partition "
            "overwrite / replaceWhere). Readers see either the old or the new partition, "
            "never a half-written mix.",
            "Full replay from an immutable log: keep raw Bronze (or the Kafka log) "
            "append-only and re-derive Silver/Gold from it. The source of truth is never "
            "mutated, so any past state is reproducible -- this is the Kappa replay idea.",
            "Shadow / blue-green table: build the corrected table beside the live one, "
            "validate it (row counts, checksums, parity against the old), then atomically "
            "swap pointers (rename / view repoint). Instant cutover, instant rollback.",
            "Throttled, chunked backfill: process history in date-chunks (one day/week at "
            "a time) with capped parallelism so the backfill does not consume the cluster "
            "the live pipeline needs. Checkpoint progress so it can resume.",
            "Coexistence with the live pipeline: run the backfill on a separate compute "
            "pool / lower priority, or on partitions the incremental load is not currently "
            "writing, so the two never contend for the same partition at the same time.",
            "Definition-change reprocessing: when a metric's business rule changes, version "
            "the logic, backfill all affected partitions with the new rule, and record which "
            "version produced which partition for auditability.",
            "Never mutate in place: an UPDATE that rewrites rows directly has no rollback if "
            "it is wrong mid-run. Overwrite whole partitions or build-and-swap instead.",
        ),
        examples=(
            "replaceWhere 'event_date >= 2026-01-01 AND event_date < 2026-02-01' rewrites "
            "exactly January, atomically, leaving every other partition untouched.",
            "Backfill 18 months one week at a time, 4 weeks in flight, off-peak only.",
        ),
    ),
    Topic(
        name="Schema evolution",
        summary=(
            "Upstream schemas change. The skill is absorbing additive changes "
            "automatically while refusing breaking changes before they reach consumers, "
            "and migrating real breaking changes safely with expand-contract."
        ),
        points=(
            "Compatible (additive) changes: adding a nullable column, widening a type "
            "(int -> long, float -> double) -- old readers and old data still work. These "
            "can be auto-applied.",
            "Breaking changes: dropping/renaming a column, narrowing a type, or changing "
            "semantics. Old queries and old files break -- these need a planned migration, "
            "never a silent overwrite.",
            "Flexible Bronze: let raw ingestion auto-capture new columns (mergeSchema / "
            "schema-on-read) so no upstream addition is ever dropped. Bronze's job is to "
            "lose nothing.",
            "Strict Silver/Gold: enforce an explicit schema (schema enforcement / a data "
            "contract) so an unexpected upstream change fails fast at the boundary instead "
            "of silently corrupting dashboards and ML features downstream.",
            "Expand-contract (parallel-change): EXPAND -- add the new column/table and "
            "dual-write old + new; MIGRATE -- backfill the new column and move readers "
            "over; CONTRACT -- drop the old once nothing reads it. A rename done online "
            "with zero downtime and a rollback at every stage.",
            "Contract versioning: version the schema/contract (semantic version), keep N-1 "
            "readable during the migration window, and treat a major bump as a breaking "
            "change requiring consumer sign-off.",
        ),
        examples=(
            "Rename amount -> amount_usd via expand-contract: add amount_usd, dual-write "
            "both, backfill, repoint consumers, then drop amount.",
            "Bronze mergeSchema picks up a new upstream field automatically; Silver's "
            "explicit schema rejects an unexpected type change and pages the on-call.",
        ),
    ),
    Topic(
        name="Distributed compute -- the shuffle",
        summary=(
            "On a distributed engine the wide shuffle (repartitioning data across the "
            "network for joins, group-bys, distinct, window) is the dominant cost. Scaling "
            "is mostly about reducing and balancing shuffles."
        ),
        points=(
            "A shuffle writes intermediate data to disk and moves it across the network "
            "between stages; it is the most expensive operation. Minimise wide operations, "
            "and pre-aggregate / filter before the shuffle to move fewer bytes.",
            "Shuffle partition count sets task granularity. Too few -> huge tasks that "
            "spill and OOM; too many -> scheduler overhead and tiny output files. Target "
            "each task to handle ~100-200 MB of shuffle data.",
            "Spill to disk happens when a task's working set exceeds executor memory; it is "
            "a slowdown signal, not a crash. Persistent spill means partitions are too "
            "large -- raise partition count or fix skew.",
            "Adaptive query execution (AQE) tunes shuffle partition count, coalesces small "
            "post-shuffle partitions, and can switch join strategy at runtime from observed "
            "statistics -- enable it instead of hand-tuning every job.",
            "Caching/materialization pays off when the same expensive intermediate is read "
            "many times (iterative ML, a reused dimension); it wastes memory for a "
            "single-pass job.",
            "Broadcast a small join side to skip the shuffle entirely; fall back to "
            "shuffle/sort-merge when both sides are large.",
        ),
        examples=(
            "A daily job slow on a group-by: cut shuffle partitions from 20000 (tiny tasks) "
            "to a count giving ~150 MB/task, and pre-filter before the aggregation.",
        ),
    ),
    Topic(
        name="Data skew and hot keys",
        summary=(
            "Skew is when one key (or a few) holds a disproportionate share of rows. After "
            "a shuffle those rows land on one task -- a straggler that runs long after every "
            "other task finished, defining total runtime."
        ),
        points=(
            "Diagnose skew from the task duration distribution: most tasks finish fast, one "
            "or two run 10x-100x longer. That long tail is a hot partition, not slow "
            "hardware.",
            "Salting: append a random suffix (0..N) to the hot key so its rows spread "
            "across N partitions; replicate the other join side across the same N salts, "
            "join, then strip the salt. Trades extra data for balanced tasks.",
            "Broadcasting the small side eliminates the shuffle that concentrates the hot "
            "key in the first place -- the cleanest fix when one side is small enough.",
            "AQE skew-join handling auto-splits oversized partitions at runtime; prefer it "
            "before hand-salting where the engine supports it.",
            "NULL and default sentinels are classic hot keys -- a join on a column where "
            "millions of rows are NULL piles them all on one task; filter or special-case "
            "them out.",
            "A common rule: a single value holding more than ~5-10% of rows (or producing a "
            "visible straggler) is worth salting or broadcasting around.",
        ),
        examples=(
            "One customer_id = 8% of all events strangles the join: salt that key into 16 "
            "buckets, replicate the dimension 16x, join, drop the salt.",
        ),
    ),
    Topic(
        name="Joins -- broadcast vs shuffle",
        summary=(
            "Join strategy is the single biggest lever on a distributed join. Broadcast "
            "avoids the shuffle by replicating the small side; shuffle/sort-merge is the "
            "scalable fallback when both sides are large."
        ),
        points=(
            "Broadcast (map-side) join: send the small table to every executor and join "
            "in memory -- no shuffle of the big side, very fast. Bounded by small-side size "
            "fitting in executor memory.",
            "Shuffle / sort-merge join: both sides are repartitioned by the join key across "
            "the network, then sorted and merged. The only option when both sides are large; "
            "pays the full shuffle cost.",
            "Choose by the small side's size, not the big side's: broadcast when the smaller "
            "input fits comfortably (typically tens-to-hundreds of MB after filtering).",
            "An over-eager broadcast of a too-large table OOMs the executors -- size the "
            "broadcast threshold to real memory, and let AQE downgrade to sort-merge when a "
            "runtime estimate exceeds it.",
            "Bucketing both tables on the join key pre-shuffles them once at write time, so "
            "repeated large-large joins skip the shuffle on read.",
        ),
        examples=(
            "Fact (1 TB) join small dim (40 MB): broadcast the dim -> no fact shuffle.",
            "Two 500 GB fact tables: sort-merge, or pre-bucket both on the join key.",
        ),
    ),
    Topic(
        name="Small files, compaction & read pruning",
        summary=(
            "Streaming and frequent micro-batches produce many tiny files; metadata and "
            "open-file overhead then dominate read time. Compaction and data layout for "
            "pruning are ongoing DataOps chores, not one-time setup."
        ),
        points=(
            "The small-file problem: thousands of sub-MB files mean the engine spends more "
            "time listing/opening files and reading footers than reading data. Each file "
            "also carries table-metadata overhead.",
            "Compaction (OPTIMIZE / bin-packing / compaction jobs) rewrites many small files "
            "into fewer right-sized files. Schedule it as a recurring maintenance job, "
            "tuned to off-peak windows.",
            "A common target file size is ~128 MB-1 GB; compact when the average file falls "
            "well below ~128 MB.",
            "Partition pruning: physically partition by a column queries filter on (usually "
            "event date) so the engine skips whole directories. Over-partitioning (high "
            "cardinality) re-creates the small-file problem -- partition coarsely.",
            "Data clustering / Z-order / liquid clustering co-locates rows by frequently "
            "filtered columns within files so file-level statistics let the reader skip "
            "files (data skipping) without rigid partition keys.",
            "Retention/vacuum: compaction leaves old file versions behind; expire them on a "
            "schedule (respecting time-travel needs) to control storage cost and metadata "
            "bloat.",
        ),
        examples=(
            "A streaming Bronze table grows to 2 M files; a nightly OPTIMIZE bin-packs to "
            "~256 MB files and read time drops sharply.",
            "Z-order Silver on (region, event_date) so region-filtered queries skip files.",
        ),
    ),
    Topic(
        name="Cost optimization",
        summary=(
            "At scale cost becomes the binding constraint. The levers are scanning fewer "
            "bytes, buying cheaper compute, sizing right, and tiering cold data -- without "
            "hurting the SLA."
        ),
        points=(
            "Scan fewer bytes: partition pruning + data skipping + columnar formats with "
            "column pruning cut the bytes read, which is what most engines bill on. Layout "
            "for the query pattern is the cheapest win.",
            "Spot/preemptible compute is far cheaper than on-demand but can be reclaimed; "
            "use it for retriable, fault-tolerant batch (with checkpoints), keep "
            "on-demand for the driver and latency-critical paths.",
            "Autoscaling matches cluster size to load and scales to zero when idle; avoid "
            "always-on oversized clusters for spiky workloads.",
            "Right-sizing: match instance type to the workload (memory-bound vs CPU-bound), "
            "and pick partition/file sizes that avoid both spill and tiny-task overhead.",
            "Caching/materialization avoids recomputing an expensive shared intermediate -- "
            "pre-aggregated Gold tables trade storage for repeated compute savings.",
            "Tier cold data: move rarely-read history to cheaper archival storage classes "
            "and expire by retention policy rather than keeping everything hot forever.",
        ),
        examples=(
            "Backfill on spot with checkpointing: ~70% cheaper, and reclaimed nodes simply "
            "resume from the last checkpoint.",
            "Partitioning by date turns a full-table scan into a single-day scan, cutting "
            "scanned bytes and bill by orders of magnitude.",
        ),
    ),
    Topic(
        name="Reliability & recovery",
        summary=(
            "Production pipelines fail; the design must fail gracefully and recover without "
            "reprocessing everything or losing data. This is where RTO/RPO, checkpoints, "
            "and replayability are designed in, not bolted on."
        ),
        points=(
            "Checkpointing records progress (consumed offsets, last committed batch) so a "
            "restart resumes from the last good point instead of reprocessing the whole "
            "history. Commit the checkpoint atomically with the data write.",
            "Retries with exponential backoff + jitter absorb transient failures (throttled "
            "API, brief outage) without a thundering-herd retry storm; cap attempts so a "
            "permanent failure surfaces.",
            "Exactly-once semantics = idempotent write + transactional offset commit; it is "
            "expensive (more coordination/state), so apply it only where duplicates are "
            "unacceptable and accept at-least-once + downstream dedup elsewhere.",
            "Dead-letter queue: route records that fail parsing/validation to a side channel "
            "instead of crashing the pipeline, so one poison record cannot halt the stream "
            "and bad data can be inspected and replayed.",
            "Replayability: keep the source append-only (Bronze / log) so any window can be "
            "reprocessed deterministically -- the foundation of both recovery and backfills.",
            "RTO/RPO and DR: define recovery-time and recovery-point objectives, then "
            "replicate state/storage across zones or regions to meet them. Multi-region "
            "adds replication lag and cost -- size it to the actual objective.",
        ),
        examples=(
            "A streaming job killed mid-batch restarts from its checkpoint, re-reads only "
            "the uncommitted offsets, and MERGEs them -- no gap, no double-count.",
            "Malformed events land in a dead-letter table; the main stream keeps flowing "
            "and the DLQ is reprocessed after the upstream fix.",
        ),
    ),
    Topic(
        name="CI/CD & DataOps practices",
        summary=(
            "DataOps applies software-delivery discipline to data: versioned code and "
            "infrastructure, automated tests on data, safe deploys, and an observability "
            "feedback loop that closes back into the pipeline."
        ),
        points=(
            "Environment isolation: dev/staging/prod with separate catalogs/storage so a "
            "change is validated on representative data before it touches production "
            "tables.",
            "Data tests in CI: run schema, freshness, row-count, and rule checks (e.g. "
            "Great Expectations / dbt tests) against a sample on every change, blocking a "
            "merge that would break a contract -- quality shifted left.",
            "Blue-green / canary data deploys: publish new logic to a shadow table, "
            "validate parity against the live table, then swap; or route a fraction of "
            "partitions through the new path first. Instant rollback by repointing.",
            "Infrastructure-as-code: clusters, jobs, schedules, and permissions are "
            "declared in version control (Terraform / pipeline-as-code), reviewable and "
            "reproducible across environments.",
            "Observability feedback loop: DAG runtimes, freshness SLAs, volume anomalies, "
            "and cost feed back to tune partitioning, compaction cadence, and cluster size "
            "-- DataOps is a loop, not a one-time setup.",
            "Orchestration as code with idempotent, retriable tasks: a DAG where each task "
            "is safe to re-run lets the scheduler recover by simply retrying failed nodes.",
        ),
        examples=(
            "PR pipeline runs dbt/GE tests on a staging slice; a contract-breaking schema "
            "change fails the build before merge.",
            "New Gold logic ships to gold_shadow, parity-checked against gold, then swapped "
            "by repointing the consumer view.",
        ),
    ),
)


# ---------------------------------------------------------------------------
# Comparisons
# ---------------------------------------------------------------------------

_COMPARISONS: tuple[Comparison, ...] = (
    Comparison(
        question="Full refresh vs incremental load?",
        options=(
            Option(
                name="Full refresh",
                when=(
                    "Small/medium tables, or logic so complex that recomputing from scratch "
                    "is simpler and safer than tracking deltas."
                ),
                pros=(
                    "Dead simple and inherently idempotent -- truncate + reload, no delta "
                    "bookkeeping.",
                    "No drift: the output always reflects the full current source.",
                ),
                cons=(
                    "Cost and time grow with total data, not new data -- becomes infeasible "
                    "at TB/PB.",
                    "Rewrites everything every run, hammering storage and compute.",
                ),
            ),
            Option(
                name="Incremental load",
                when=(
                    "Large tables where only a small slice changes per run; the default at "
                    "scale."
                ),
                pros=(
                    "Processes only new/changed data -- cost scales with the delta.",
                    "Fast, frequent runs that keep freshness SLAs cheaply.",
                ),
                cons=(
                    "Needs a reliable change signal (high-water mark / CDC / event time) "
                    "and idempotent MERGE to stay correct.",
                    "Late-arriving and out-of-order data must be handled explicitly.",
                ),
            ),
        ),
        rule_of_thumb=(
            "Full refresh while the table is small; switch to incremental MERGE the moment "
            "full recompute time threatens the SLA or the bill."
        ),
    ),
    Comparison(
        question="Partition overwrite vs MERGE/upsert for a backfill?",
        options=(
            Option(
                name="Atomic partition overwrite",
                when=(
                    "The unit of correction is a whole partition (e.g. recompute all of "
                    "January) and you can regenerate that partition from source."
                ),
                pros=(
                    "Atomic and simple -- replaceWhere swaps the whole partition in one "
                    "commit; clean rollback by re-running.",
                    "No per-row matching cost; ideal for bulk historical reprocessing.",
                ),
                cons=(
                    "Coarse: rewrites the entire partition even to fix a few rows.",
                    "Wrong if the live incremental load is writing the same partition "
                    "concurrently.",
                ),
            ),
            Option(
                name="MERGE / upsert",
                when=(
                    "Only specific rows changed, or the backfill must interleave with live "
                    "writes to the same partition."
                ),
                pros=(
                    "Surgical -- updates only matched keys, leaves the rest untouched.",
                    "Composes with the live incremental pipeline on the same table.",
                ),
                cons=(
                    "More expensive per run (join to find matches, rewrites affected "
                    "files).",
                    "Needs a stable key and careful match condition to stay idempotent.",
                ),
            ),
        ),
        rule_of_thumb=(
            "Whole-partition correction from source -> atomic partition overwrite; targeted "
            "row fixes or overlap with live writes -> MERGE."
        ),
    ),
    Comparison(
        question="Expand-contract migration vs big-bang schema change?",
        options=(
            Option(
                name="Expand-contract (parallel-change)",
                when=(
                    "A breaking change (rename/drop/retype) on a table with live producers "
                    "and consumers that cannot all change at once."
                ),
                pros=(
                    "Zero downtime; old and new coexist so consumers migrate on their own "
                    "schedule.",
                    "Rollback available at every stage -- nothing is dropped until nothing "
                    "reads it.",
                ),
                cons=(
                    "Multi-step and slower; dual-write and backfill add temporary "
                    "complexity.",
                    "Requires tracking who still reads the old shape before contracting.",
                ),
            ),
            Option(
                name="Big-bang change",
                when=(
                    "A tiny, fully-owned table with a single known consumer and a "
                    "maintenance window."
                ),
                pros=(
                    "One step, no transitional dual-write state.",
                ),
                cons=(
                    "Any unmigrated consumer breaks instantly at cutover.",
                    "No graceful rollback -- the old shape is gone.",
                ),
            ),
        ),
        rule_of_thumb=(
            "For any shared/production table, always expand-contract; reserve big-bang for "
            "throwaway tables with one owner."
        ),
    ),
    Comparison(
        question="Broadcast join vs shuffle/sort-merge join?",
        options=(
            Option(
                name="Broadcast (map-side) join",
                when="One side is small enough to fit in executor memory after filtering.",
                pros=(
                    "No shuffle of the large side -- dramatically faster.",
                    "Sidesteps skew on the join key entirely.",
                ),
                cons=(
                    "OOMs executors if the broadcast side is too large.",
                    "Only viable when one side stays small.",
                ),
            ),
            Option(
                name="Shuffle / sort-merge join",
                when="Both sides are large -- the scalable general case.",
                pros=(
                    "Works for arbitrarily large inputs on both sides.",
                ),
                cons=(
                    "Pays the full network shuffle + sort cost.",
                    "Exposed to skew -- a hot key creates a straggler.",
                ),
            ),
        ),
        rule_of_thumb=(
            "Broadcast when the small side fits in memory (~tens-to-hundreds of MB); "
            "otherwise sort-merge and fix skew with salting."
        ),
    ),
    Comparison(
        question="Flexible-Bronze vs strict-Silver/Gold schema policy?",
        options=(
            Option(
                name="Flexible (schema-on-read / mergeSchema) at Bronze",
                when="Raw landing zone whose job is to never lose an upstream field.",
                pros=(
                    "Auto-captures new upstream columns -- nothing is dropped.",
                    "Ingestion does not break when producers add fields.",
                ),
                cons=(
                    "Permissive -- bad/inconsistent data lands and must be caught later.",
                    "Schema drift accumulates if never validated downstream.",
                ),
            ),
            Option(
                name="Strict (schema enforcement / contract) at Silver/Gold",
                when="Curated layers feeding dashboards, ML, and analysts.",
                pros=(
                    "Fails fast at the boundary -- protects downstream consumers.",
                    "Guarantees a stable, contracted shape.",
                ),
                cons=(
                    "An unhandled upstream change blocks the load until reconciled.",
                    "Requires an explicit, maintained schema/contract.",
                ),
            ),
        ),
        rule_of_thumb="Flexible at Bronze (lose nothing), strict at Silver/Gold (break nothing).",
    ),
    Comparison(
        question="Spot/preemptible vs on-demand compute?",
        options=(
            Option(
                name="Spot / preemptible",
                when=(
                    "Fault-tolerant, retriable batch and backfills with checkpointing; "
                    "cost-sensitive non-urgent work."
                ),
                pros=(
                    "Far cheaper (often ~60-90% off on-demand).",
                    "Great for elastic, interruptible batch.",
                ),
                cons=(
                    "Can be reclaimed mid-run -- requires checkpoints/retries to be "
                    "correct.",
                    "Unsuitable for latency-critical or non-resumable steps.",
                ),
            ),
            Option(
                name="On-demand",
                when="Drivers, latency-critical serving, and steps that cannot tolerate interruption.",
                pros=(
                    "Stable -- not reclaimed under you.",
                    "Predictable for SLA-bound paths.",
                ),
                cons=(
                    "Significantly more expensive.",
                    "Wasteful if left always-on for spiky workloads.",
                ),
            ),
        ),
        rule_of_thumb=(
            "Run idempotent, checkpointed workers on spot; keep the driver and "
            "latency-critical paths on on-demand."
        ),
    ),
)


# ---------------------------------------------------------------------------
# Heuristics
# ---------------------------------------------------------------------------

_HEURISTICS: tuple[Heuristic, ...] = (
    Heuristic(
        name="MERGE, never flat INSERT",
        rule=(
            "For incremental loads, upsert with MERGE on a stable key so a re-run updates "
            "instead of duplicating -- flat INSERT is non-idempotent."
        ),
        example="Retrying a failed batch must not double the rows it already wrote.",
    ),
    Heuristic(
        name="Broadcast the small side",
        rule=(
            "Broadcast a join side when it fits in executor memory (~tens-to-hundreds of "
            "MB after filtering); this removes the shuffle and dodges skew."
        ),
        example="1 TB fact x 40 MB dimension -> broadcast the dimension.",
    ),
    Heuristic(
        name="Size shuffle partitions to ~100-200 MB/task",
        rule=(
            "Target shuffle partition count so each task handles ~100-200 MB; too few "
            "spills/OOMs, too many adds scheduler overhead and tiny files."
        ),
        example="Let AQE coalesce post-shuffle partitions toward that target.",
    ),
    Heuristic(
        name="Salt the hot key",
        rule=(
            "When one value is >~5-10% of rows (or causes a visible straggler), salt it "
            "into N buckets and replicate the other side N-fold to balance the join."
        ),
        example="NULL/sentinel keys are classic hot keys -- filter or salt them.",
    ),
    Heuristic(
        name="Compact below ~128 MB average",
        rule=(
            "Run OPTIMIZE/compaction when the average file size drops well below ~128 MB; "
            "target ~128 MB-1 GB files. Schedule it as recurring maintenance."
        ),
        example="A streaming Bronze table needs a nightly compaction job.",
    ),
    Heuristic(
        name="Chunk and throttle backfills",
        rule=(
            "Backfill in date-chunks with capped parallelism on a separate/lower-priority "
            "pool so it never starves the live incremental pipeline; checkpoint to resume."
        ),
        example="18 months one week at a time, 4 in flight, off-peak only.",
    ),
    Heuristic(
        name="Flexible Bronze, strict Silver/Gold",
        rule=(
            "Auto-capture new columns at Bronze (mergeSchema) so nothing is lost; enforce "
            "an explicit schema at Silver/Gold so nothing downstream silently breaks."
        ),
        example="Bronze absorbs a new field; Silver rejects an unexpected type change.",
    ),
    Heuristic(
        name="Checkpoint everything restartable",
        rule=(
            "Commit progress (offsets/last batch) atomically with the data so a failure "
            "resumes from the last good point instead of reprocessing all history."
        ),
        example="A killed streaming job restarts from its checkpoint, not from t=0.",
    ),
    Heuristic(
        name="Overwrite or swap -- never mutate in place",
        rule=(
            "Correct history by atomically overwriting whole partitions or build-and-swap, "
            "not by in-place UPDATE, so every backfill has a clean rollback."
        ),
        example="replaceWhere one month atomically; build gold_shadow then swap.",
    ),
    Heuristic(
        name="Spot for idempotent batch",
        rule=(
            "Run retriable, checkpointed batch and backfills on spot/preemptible compute "
            "for big savings; keep drivers and latency-critical paths on on-demand."
        ),
        example="Reclaimed spot node resumes from the last checkpoint -- ~70% cheaper.",
    ),
)


# ---------------------------------------------------------------------------
# Pitfalls / at-scale / interview signals
# ---------------------------------------------------------------------------

_PITFALLS: tuple[str, ...] = (
    "Flat INSERT on the incremental path -- a retried or re-run batch duplicates rows "
    "because the load is not idempotent.",
    "No clean backfill path: history is mutated in place with UPDATE, so a bad backfill "
    "has no rollback and can corrupt the table mid-run.",
    "Breaking schema changes (drop/rename/retype) pushed straight to curated layers, "
    "shattering downstream dashboards and ML features.",
    "Ignoring data skew: one hot key piles onto a single task and that straggler defines "
    "the whole job's runtime while the cluster sits idle.",
    "Small-file accumulation from streaming with no scheduled compaction -- metadata and "
    "file-open overhead eventually dominate read time.",
    "Unbounded full refresh where incremental would do -- recompute cost grows with total "
    "data and eventually blows the SLA and the budget.",
    "Backfill run at full parallelism on the shared cluster, starving the live pipeline "
    "and breaking freshness for current data.",
    "No checkpointing, so any failure reprocesses everything from the start instead of "
    "resuming from the last committed offset/batch.",
    "Over-partitioning on a high-cardinality column, which recreates the small-file "
    "problem instead of pruning.",
    "Relying on at-least-once delivery with no dedup window -- streaming duplicates leak "
    "into Silver/Gold and inflate every metric.",
)

_AT_SCALE: tuple[str, ...] = (
    "The shuffle/network becomes the bottleneck -- joins and group-bys move terabytes "
    "across the cluster; reducing and balancing shuffles is most of the tuning.",
    "Skew creates stragglers -- a single hot key turns into one task running long after "
    "the rest finish, so total runtime is set by the worst partition.",
    "Small files explode metadata -- millions of tiny files make file listing and footer "
    "reads dominate, forcing compaction into a first-class scheduled job.",
    "Full refresh becomes infeasible -- recompute time scales with total data, so loads "
    "must go incremental (MERGE on a change signal).",
    "Compaction/OPTIMIZE and vacuum become recurring scheduled jobs, not one-time setup, "
    "to keep file sizes and metadata healthy.",
    "Cost becomes the binding constraint -- partition pruning + data skipping cut scanned "
    "bytes, and spot + autoscaling cut compute spend.",
    "Exactly-once gets expensive -- the extra coordination/state means you reserve it for "
    "where duplicates are unacceptable and accept at-least-once + dedup elsewhere.",
    "DR and cross-region replication add real lag and cost overhead -- size multi-region "
    "to the actual RTO/RPO rather than replicating everything by default.",
)

_INTERVIEW_SIGNALS: tuple[str, ...] = (
    "Makes every load idempotent with MERGE/upsert on a stable key and deterministic "
    "transforms -- treats failure + retry as the normal case.",
    "Has a concrete backfill plan: atomic partition overwrite or replay from an immutable "
    "Bronze/log, chunked and throttled so it never starves the live pipeline.",
    "Designs breaking schema changes as expand-contract (parallel-change) migrations with "
    "rollback at every stage, not big-bang overwrites.",
    "Diagnoses skew from the task-duration tail and fixes it with broadcast or salting, "
    "not by throwing more nodes at the problem.",
    "Picks join strategy deliberately -- broadcasts the small side, sort-merges large-"
    "large, and bucketing for repeated joins.",
    "Treats small files as an operational reality and schedules compaction/OPTIMIZE plus "
    "retention/vacuum.",
    "Optimizes cost with partition pruning + data skipping, spot/preemptible for "
    "idempotent batch, and autoscaling -- not always-on oversized clusters.",
    "Builds for recovery: checkpoints, retries with backoff, dead-letter queues, and a "
    "stated RTO/RPO with replayability from an append-only source.",
    "Applies DataOps discipline -- environments, data tests in CI, blue-green/canary data "
    "deploys, infrastructure-as-code, and an observability feedback loop.",
    "Knows flexible-Bronze / strict-Silver-Gold and applies it to keep ingestion robust "
    "while protecting downstream consumers.",
)


# ---------------------------------------------------------------------------
# Export
# ---------------------------------------------------------------------------

DEEP = DeepStep(
    step_key="scalability",
    title="Scalability, Backfills & DataOps",
    overview=(
        "Step 6 is where a working pipeline becomes a production system: safe to re-run, "
        "cheap to reprocess, and able to fail without losing or duplicating data. The core "
        "ideas are idempotency (MERGE/upsert on a stable key so a run is repeatable), "
        "clean backfills (atomic partition overwrite or replay from an immutable log, "
        "chunked so the live pipeline keeps running), graceful schema evolution "
        "(flexible Bronze, strict Silver/Gold, expand-contract for breaking changes), and "
        "the realities of distributed compute -- the shuffle, data skew, small files, and "
        "cost. Around all of it sits DataOps: checkpoints and retries for recovery, and "
        "CI/CD with data tests and safe deploys so changes ship without breaking history."
    ),
    topics=_TOPICS,
    comparisons=_COMPARISONS,
    heuristics=_HEURISTICS,
    pitfalls=_PITFALLS,
    at_scale=_AT_SCALE,
    interview_signals=_INTERVIEW_SIGNALS,
)


__all__ = ["DEEP"]
