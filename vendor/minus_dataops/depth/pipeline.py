"""Deep knowledge for step 2 -- Pipeline Design.

How data physically moves and transforms from source to destination. The base
framework picks the *track* (batch / stream / hybrid) from the latency SLA; this
dossier explains how each track is actually built and run: ingestion patterns,
the streaming spine (log + processor + serving store), micro-batch as the middle
ground, Lambda vs Kappa, ETL vs ELT, change data capture, orchestration of
idempotent DAGs, delivery semantics (incl. honest exactly-once), event-time
windowing, and error handling.

Tool- and cloud-agnostic: Kafka, Flink, Spark, Airflow / Dagster / Prefect,
Redis, DynamoDB are named only as representative examples of a category. ASCII
only, no emojis.
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
        name="Batch processing",
        summary=(
            "Bounded data processed on a schedule by an orchestrated DAG. The "
            "default when the latency SLA is hours/days and cost matters more "
            "than freshness."
        ),
        points=(
            "Shape: extract -> land raw files in object storage (a partitioned "
            "landing zone) -> transform through progressive layers (raw -> "
            "cleaned -> aggregated). Each layer is a re-runnable table, not a "
            "side effect, so a failed stage is recomputed, not patched.",
            "Work is one job over a known, finite input, so the engine can plan "
            "globally: it sees the whole partition, sorts and shuffles once, and "
            "writes wide files. This is why batch is the cheapest per-row track.",
            "Scheduling is time-driven (cron-like) or data-driven (a sensor "
            "waits for an upstream partition to land). Prefer data-driven so a "
            "late source delays the run instead of producing a half-empty table.",
            "Process whole partitions atomically: write to a temp path, then "
            "swap/overwrite the partition in one commit. A killed job leaves the "
            "old partition intact, so re-running is safe (idempotent by design).",
            "Heavy lifting is distributed compute (e.g. Spark); orchestration is "
            "a DAG scheduler (e.g. Airflow / Dagster / Prefect). Keep transform "
            "logic out of the orchestrator -- it schedules and retries, it is "
            "not the engine.",
            "Backfill is a first-class mode: the same DAG run parameterized by a "
            "historical date range, reprocessing old partitions with today's "
            "code.",
        ),
        examples=(
            "Daily revenue mart: at 02:00 a sensor confirms yesterday's order "
            "partition landed, Spark joins orders+payments, overwrites the "
            "date=YYYY-MM-DD partition of the gold table.",
            "Hourly clickstream rollup that tolerates a 1-2h lag because the "
            "dashboard it feeds is reviewed once a morning.",
        ),
    ),
    Topic(
        name="Stream processing",
        summary=(
            "Unbounded data processed record-by-record (or in tiny groups) for "
            "sub-minute latency. The spine is always log -> processor -> "
            "low-latency serving store."
        ),
        points=(
            "Ingestion / buffering / decoupling: a durable, partitioned, "
            "replayable log (e.g. Kafka / a managed equivalent) sits between "
            "producers and consumers. It absorbs bursts, lets producers and "
            "consumers scale and fail independently, and retains events so they "
            "can be replayed.",
            "The log is the contract, not just a transport. Because it persists "
            "an ordered offset per partition, a consumer that crashes resumes "
            "from its last committed offset -- no data loss, and reprocessing is "
            "just resetting the offset.",
            "A stream processor (e.g. Flink, Spark Structured Streaming) does "
            "the stateful work: parse, filter, enrich via lookups/joins, and "
            "aggregate over time windows. State lives in a local state store "
            "and is checkpointed for fault tolerance.",
            "Output goes to a low-latency serving store (e.g. an in-memory KV "
            "store, a wide-column store) for point lookups, or back onto the log "
            "for the next stage. Do not serve a real-time dashboard by scanning "
            "the lake -- precompute into a fast store.",
            "Parallelism is bounded by partition count: one partition is "
            "consumed by at most one consumer in a group, so max consumer "
            "parallelism = partition count. Pick a partition key that spreads "
            "load and preserves the ordering you need (per-key order is kept "
            "within a partition).",
            "Streaming is operationally heavier: always-on jobs, state to "
            "manage, watermarks to tune, and exactly-once to engineer. Only pay "
            "this when the SLA truly demands it.",
        ),
        examples=(
            "Fraud scoring: card events -> log -> Flink keyed by card, holds a "
            "rolling 5-min feature window in state, writes a risk score to an "
            "in-memory store the auth service reads in single-digit ms.",
            "Live ops dashboard fed by a streaming job that upserts per-minute "
            "counts into a fast KV store; the dashboard reads the store, never "
            "the raw log.",
        ),
    ),
    Topic(
        name="Micro-batch (the middle ground)",
        summary=(
            "Process the stream in small, frequent, bounded groups (e.g. every "
            "few seconds to a minute). Near-streaming latency with batch-style "
            "simplicity and cost."
        ),
        points=(
            "Mechanically a tight loop of small batches: read the next slice of "
            "the log since the last offset, transform it as a bounded job, "
            "commit the result and the offset together.",
            "Gives you batch semantics for free -- whole-slice commits are "
            "naturally idempotent and easy to reason about -- while keeping "
            "end-to-end latency in the seconds-to-a-minute range.",
            "Higher per-record latency floor than true per-event streaming "
            "because work waits for the slice to fill; the trigger interval is a "
            "direct latency-vs-throughput-vs-cost knob.",
            "Often the pragmatic default: most 'real-time' dashboards and "
            "near-real-time enrichment are fine at 10-60s freshness, and "
            "micro-batch is cheaper and simpler to operate than event-at-a-time "
            "engines.",
            "Same engine can frequently do both batch and micro-batch, so one "
            "codebase covers the historical reprocess and the incremental feed.",
        ),
        examples=(
            "A structured-streaming job with a 30s trigger that incrementally "
            "appends to a silver table -- the table is queryable like any batch "
            "table but is never more than ~30s stale.",
            "Near-real-time inventory sync that reads the change log every 15s; "
            "true per-event was unnecessary because the storefront caches for a "
            "minute anyway.",
        ),
    ),
    Topic(
        name="Hybrid architectures: Lambda vs Kappa",
        summary=(
            "Patterns for serving both accurate history and fresh data. Lambda "
            "runs two paths and merges; Kappa runs one streaming path over a "
            "replayable log."
        ),
        points=(
            "Lambda: a batch (cold) path produces the authoritative, "
            "fully-corrected view on a schedule; a speed (hot) path produces an "
            "approximate, low-latency view; the serving layer merges them so "
            "recent data is fresh and older data is eventually exact.",
            "Lambda's cost is duplicated logic: the same transformation is "
            "implemented and maintained twice (batch and stream), and the two "
            "can silently drift, producing different numbers for the same "
            "metric.",
            "Kappa: a single streaming pipeline is the only code path. There is "
            "no separate batch layer; the long-retained log IS the system of "
            "record. To 'reprocess history' you replay the log from offset 0 "
            "through the new code into a new output, then cut over.",
            "Kappa trades storage and replay time for one-codebase simplicity: "
            "you must retain the log long enough (or tier it to object storage) "
            "to rebuild any derived state, and a full replay can be expensive.",
            "Choose by where correctness comes from: if late-arriving "
            "corrections and large recomputations are routine and heavy, a batch "
            "path (Lambda) may still earn its keep; if the streaming engine can "
            "express the full logic and replay is feasible, Kappa removes a "
            "whole class of drift bugs.",
        ),
        examples=(
            "Lambda billing: nightly batch reconciles exact charges from "
            "settled records; a speed path shows provisional usage during the "
            "day; the UI labels today's numbers 'provisional'.",
            "Kappa metrics platform: every metric is a streaming aggregation; a "
            "logic change is shipped by replaying the retained event log into a "
            "v2 table, then swapping the serving pointer.",
        ),
    ),
    Topic(
        name="ETL vs ELT",
        summary=(
            "Where transformation happens relative to load. ELT (load raw, then "
            "transform in the warehouse/lakehouse) won for analytics; ETL still "
            "fits constrained or compliance-bound sinks."
        ),
        points=(
            "ETL: transform in a separate compute tier before loading; the sink "
            "only ever sees clean, conformed data. Classic when the destination "
            "was an expensive, rigid warehouse you did not want to load with raw "
            "junk.",
            "ELT: land raw data first (an immutable bronze/landing layer), then "
            "transform in place using the lakehouse/warehouse's own scalable "
            "compute. The raw copy is a permanent safety net.",
            "ELT won for the lakehouse because storage is cheap and compute is "
            "elastic and decoupled: keeping raw costs little, and you can "
            "re-derive any downstream table later when requirements or business "
            "logic change -- without re-extracting from the source.",
            "ELT also shortens the ingest path (fewer moving parts before the "
            "data is durable) and lets transformation be version-controlled SQL "
            "over the lake rather than bespoke pre-load code.",
            "ETL still wins when you must NOT land raw (regulatory / PII "
            "minimization), when the sink is small/rigid, or when heavy "
            "cleansing must happen off a constrained target. The two also "
            "compose: ETL the sensitive bits, ELT the rest.",
        ),
        examples=(
            "ELT: dump source JSON to bronze unchanged, then build silver/gold "
            "with versioned SQL/Spark in the lakehouse; reprocess by re-running "
            "the SQL, never re-pulling the source.",
            "ETL: tokenize/redact PII in a transform tier so raw identifiers are "
            "never written to the analytics store at all.",
        ),
    ),
    Topic(
        name="Change Data Capture (CDC)",
        summary=(
            "Continuously capture row-level changes (inserts/updates/deletes) "
            "from an operational database so the lake mirrors it without "
            "re-extracting the whole table."
        ),
        points=(
            "Log-based CDC: tail the database's transaction/replication log "
            "(WAL/binlog/redo). Captures every change including deletes, in "
            "commit order, with near-zero load on the source and low latency. "
            "The strongly preferred method when available.",
            "Query-based / trigger-based CDC: poll with a query filtered on an "
            "updated_at high-watermark, or fire DB triggers into an audit table. "
            "Simple and portable but adds load to the source, can miss "
            "hard-deletes and intra-poll updates, and depends on a trustworthy "
            "updated_at column.",
            "Bootstrap correctly: take a consistent snapshot of the table for "
            "the initial state, then stream incremental changes from the log "
            "offset captured at snapshot time -- snapshot + stream, stitched "
            "with no gap and no overlap.",
            "Apply CDC as upserts, not appends: land the raw change events in "
            "bronze (an immutable changelog), then MERGE by primary key into a "
            "current-state silver table, ordered by the change sequence so the "
            "latest version wins. Map delete events to tombstones or removals.",
            "Order and idempotency are everything: process changes per key in "
            "commit/LSN order, and make the MERGE replay-safe so re-delivering a "
            "change event does not corrupt state.",
        ),
        examples=(
            "Log-based: a connector reads the binlog into a per-table topic; a "
            "streaming job MERGEs each change into the silver mirror keyed by "
            "primary key, ordered by LSN.",
            "Query-based fallback for a source with no log access: poll WHERE "
            "updated_at > last_watermark every few minutes -- accepting that "
            "hard deletes won't be seen.",
        ),
    ),
    Topic(
        name="Ingestion patterns",
        summary=(
            "How new data is pulled from or pushed by a source. Pick by source "
            "capability, data volume, and how cleanly change can be detected."
        ),
        points=(
            "Full extract (snapshot): copy the entire source each run. Simple "
            "and self-correcting but O(table size) every time -- only viable for "
            "small/slowly-changing reference data.",
            "Incremental / high-watermark: pull only rows newer than the last "
            "stored watermark (max updated_at or an ID). Cheap and the workhorse "
            "for batch, but blind to hard deletes and to updates that don't "
            "advance the watermark; store the watermark transactionally with the "
            "load.",
            "CDC: stream row-level changes from the source's log (see the CDC "
            "topic). Lowest latency and captures deletes; needs log access and "
            "more machinery.",
            "Event push: the source emits events to an endpoint or log (webhooks "
            "/ producers onto Kafka). The source owns delivery; you own a "
            "durable buffer and must handle duplicates and retries on your side.",
            "Match pattern to source: an API with a since= cursor -> "
            "incremental; an OLTP DB you can tail -> CDC; an app you control -> "
            "event push; a tiny lookup table -> full extract.",
        ),
        examples=(
            "Incremental: nightly pull of orders WHERE updated_at > watermark, "
            "then advance the watermark only after the write commits.",
            "Event push: services emit domain events to a log; the platform "
            "consumes, dedups by event_id, and lands them -- producers never "
            "block on the warehouse.",
        ),
    ),
    Topic(
        name="Orchestration (DAGs)",
        summary=(
            "The control plane that runs pipeline tasks in dependency order with "
            "retries, SLAs, sensors, and backfills. It schedules and recovers "
            "work; it is not the compute engine."
        ),
        points=(
            "Model the pipeline as a DAG: nodes are tasks, edges are data "
            "dependencies. A task runs only when its upstreams succeed, which "
            "makes partial failure recoverable and the data lineage explicit.",
            "Make every task idempotent and re-runnable: a task that is retried "
            "or re-run for the same logical date/partition must produce the same "
            "result (overwrite-partition or MERGE, never blind append). This is "
            "what makes automatic retries and backfills safe.",
            "Reliability controls: bounded retries with backoff for transient "
            "failures, SLAs/alerts on lateness, and sensors that gate a task on "
            "an external condition (a file landed, an upstream partition is "
            "ready) instead of guessing with a fixed sleep.",
            "Scheduling vs event-driven triggering: time schedules are simple "
            "but run blind to data readiness; event/sensor triggers fire when "
            "the data actually arrives, avoiding empty or partial runs. Prefer "
            "event-driven where the source signal exists.",
            "Backfills and dynamic mapping: parameterize runs by logical "
            "date/partition so the same DAG reprocesses history; use dynamic "
            "task mapping to fan out one task per partition/file discovered at "
            "runtime instead of hardcoding the count. Bound backfill concurrency "
            "so it doesn't starve live runs.",
            "Keep the orchestrator thin: it dispatches work to the engine "
            "(Spark/SQL/etc.) and tracks state. Heavy data should not flow "
            "through the scheduler process -- pass references (paths/partitions), "
            "not rows.",
        ),
        examples=(
            "A daily DAG: sensor(source partition) -> extract -> transform "
            "(MERGE into silver) -> build gold -> data-quality check; a failed "
            "transform retries 3x then pages on SLA breach.",
            "Backfill 90 days by triggering the same DAG over a date range with "
            "max 4 concurrent runs so production scheduling is not starved.",
        ),
    ),
    Topic(
        name="Delivery & processing semantics",
        summary=(
            "What guarantee the pipeline gives about how many times each record "
            "is processed and reflected: at-most-once, at-least-once, "
            "exactly-once. Be honest about how exactly-once is really achieved."
        ),
        points=(
            "At-most-once: ack/advance the offset before processing. No "
            "duplicates but data can be lost on failure. Acceptable only for "
            "lossy telemetry where a dropped event is harmless.",
            "At-least-once: process, then commit the offset. No loss, but a "
            "crash between processing and commit replays records -> duplicates. "
            "This is the realistic default for most systems.",
            "Exactly-once is not magic delivery; networks can always redeliver. "
            "What you actually engineer is at-least-once delivery + an "
            "idempotent or transactional sink so duplicates have no observable "
            "effect. The output is exactly-once even though delivery is not.",
            "Two concrete recipes: (a) idempotent sink -- write keyed by a "
            "deterministic id (event_id / (key, window)) via MERGE/upsert so a "
            "replayed record overwrites rather than doubles; (b) transactional "
            "sink -- commit the output and the consumed offsets in one atomic "
            "transaction (with checkpointing) so either both land or neither.",
            "End-to-end exactly-once requires every hop to cooperate (source "
            "offsets + processor checkpoint + sink commit in one unit). A single "
            "non-transactional, non-idempotent sink in the chain breaks the "
            "guarantee no matter what the engine claims.",
            "Exactly-once costs throughput and latency: transactions, "
            "two-phase-commit-style coordination, and more frequent "
            "checkpoints/barriers. Don't pay for it where an idempotent upsert "
            "or simple dedup window already makes duplicates harmless.",
        ),
        examples=(
            "Idempotent sink: aggregation writes one row per (key, window) via "
            "MERGE; replaying the same window overwrites the same row -> "
            "exactly-once result on at-least-once delivery.",
            "Transactional sink: the processor commits output records and input "
            "offsets in one transaction tied to its checkpoint, so a recovery "
            "never re-emits already-committed output.",
        ),
    ),
    Topic(
        name="Stream-time semantics & windowing",
        summary=(
            "In streaming, time is ambiguous. Distinguish when an event happened "
            "(event time) from when it was processed (processing time); use "
            "watermarks and windows to handle late, out-of-order data."
        ),
        points=(
            "Event time = the timestamp inside the record (when it really "
            "happened). Processing time = the wall clock when the engine sees "
            "it. They diverge under network delay, retries, mobile/offline "
            "buffering, and backlogs. Compute business metrics on EVENT time, or "
            "results shift with system lag.",
            "Watermark = the engine's assertion that 'event time has advanced to "
            "T; expect almost nothing older than T'. It is the heuristic that "
            "lets the engine decide a window is complete and can be emitted. A "
            "watermark lag of L means tolerating events up to L late.",
            "Windowing types: tumbling (fixed, non-overlapping buckets -- counts "
            "per minute); sliding (fixed size, overlapping by a step -- 5-min "
            "count updated every 1 min); session (dynamic, closed by a gap of "
            "inactivity -- a user's activity burst). Pick by the question being "
            "asked.",
            "Allowed lateness: events arriving after the watermark passes their "
            "window. Within an allowed-lateness grace period the window is "
            "updated/re-emitted; beyond it they are dropped or routed to a "
            "side/late output. This is the explicit accuracy-vs-latency knob.",
            "Out-of-order handling lives in this triad: a larger watermark lag "
            "and allowed lateness give more correctness for late data but delay "
            "emission and grow state; tighter values emit fast but drop "
            "stragglers. Choose deliberately against the SLA.",
            "Trigger/emit policy is separate from windowing: you can emit early "
            "(speculative) results before the watermark closes the window, then "
            "emit a final corrected result -- useful for live dashboards that "
            "want both speed and eventual accuracy.",
        ),
        examples=(
            "Sessionize web activity with a 30-min inactivity gap on event time, "
            "watermark lag 5 min, allowed lateness 10 min -> late mobile events "
            "still fold into the right session.",
            "Per-minute order counts on event time so a 90s ingestion backlog "
            "doesn't smear counts into the wrong minute.",
        ),
    ),
    Topic(
        name="Error handling, replay & backpressure",
        summary=(
            "Production streams must survive poison records, downstream "
            "slowdowns, and reprocessing. Quarantine bad data, never block on "
            "it, and keep the log replayable."
        ),
        points=(
            "Dead-letter queue (DLQ): records that repeatedly fail to "
            "parse/process are routed to a side channel after bounded retries, "
            "WITH their error context, instead of being retried forever or "
            "silently dropped. The main stream keeps flowing.",
            "Poison message: a single malformed/unprocessable record that, "
            "without a DLQ, fails the task, gets retried, fails again, and stalls "
            "the whole partition behind it (head-of-line blocking). A DLQ is the "
            "fix.",
            "Replay: because the log is durable and offset-addressable, you can "
            "reset a consumer to an earlier offset to reprocess after a bug fix, "
            "or fix-and-resubmit DLQ records once the code/schema is corrected. "
            "Replay safety depends on idempotent sinks (see semantics).",
            "Backpressure: when a consumer/processor can't keep up, the slowness "
            "must propagate upstream (the engine slows reads / the log buffers) "
            "rather than the system dropping data or running out of memory. Good "
            "engines apply backpressure automatically; a fixed-size durable log "
            "is the buffer that absorbs spikes.",
            "Buffering decouples producer and consumer speed: the log holds a "
            "retention window of data so a temporary downstream outage doesn't "
            "lose events -- the consumer simply catches up from its offset when "
            "it recovers.",
            "Distinguish retryable (transient: timeout, throttle) from "
            "non-retryable (poison: schema/parse) failures. Retry the first with "
            "backoff; DLQ the second immediately. Conflating them either stalls "
            "the pipeline or discards recoverable data.",
        ),
        examples=(
            "A schema-violating event after 3 failed parses goes to a DLQ topic "
            "with the raw payload + error; an operator fixes the mapping and "
            "replays the DLQ.",
            "A downstream store outage triggers backpressure: the processor "
            "slows its log reads, the log buffers within its retention window, "
            "and nothing is lost when the store returns.",
        ),
    ),
)


# ---------------------------------------------------------------------------
# Comparisons
# ---------------------------------------------------------------------------

_COMPARISONS: tuple[Comparison, ...] = (
    Comparison(
        question="Batch, micro-batch, or true streaming?",
        options=(
            Option(
                name="Batch",
                when="Latency SLA is hours/days; cost and simplicity dominate.",
                pros=(
                    "Cheapest per row -- one planned job over a finite input.",
                    "Simple to reason about, test, and backfill.",
                    "Whole-partition commits are naturally idempotent.",
                ),
                cons=(
                    "Freshness limited to the schedule interval.",
                    "Bursty re-compute can spike cluster cost at run time.",
                ),
            ),
            Option(
                name="Micro-batch",
                when="SLA is seconds-to-a-minute and batch-style simplicity is "
                "worth a small latency floor.",
                pros=(
                    "Near-real-time at lower complexity/cost than per-event.",
                    "Slice commits give easy idempotency.",
                    "Often one engine/codebase for batch and incremental.",
                ),
                cons=(
                    "Per-record latency floor set by the trigger interval.",
                    "Not suitable for sub-second decisions.",
                ),
            ),
            Option(
                name="True streaming",
                when="Sub-second to low-seconds latency is a hard requirement "
                "(fraud, real-time control).",
                pros=(
                    "Lowest end-to-end latency, per-event processing.",
                    "Rich event-time windowing and stateful operators.",
                ),
                cons=(
                    "Always-on jobs, state and watermarks to operate.",
                    "Exactly-once and ordering must be engineered.",
                    "Highest operational and per-row cost.",
                ),
            ),
        ),
        rule_of_thumb=(
            "Derive the track from the latency SLA: < ~1 min -> streaming; "
            "seconds-to-a-minute -> micro-batch; hours/days -> batch. Do not buy "
            "streaming complexity the SLA doesn't require."
        ),
    ),
    Comparison(
        question="Lambda or Kappa for serving fresh + accurate data?",
        options=(
            Option(
                name="Lambda (batch path + speed path)",
                when="Heavy, frequent late corrections / recomputations make a "
                "dedicated batch path worth maintaining.",
                pros=(
                    "Batch path gives an authoritative, fully-corrected view.",
                    "Speed path covers freshness independently.",
                ),
                cons=(
                    "Same logic implemented twice -> maintenance + drift risk.",
                    "Serving-layer merge adds complexity.",
                ),
            ),
            Option(
                name="Kappa (single streaming path)",
                when="The streaming engine can express the full logic and the "
                "log can be retained/replayed.",
                pros=(
                    "One codebase -> no batch/stream drift.",
                    "Reprocess history by replaying the log through new code.",
                ),
                cons=(
                    "Must retain (or tier) the log long enough to rebuild state.",
                    "Full replays can be slow/expensive.",
                ),
            ),
        ),
        rule_of_thumb=(
            "Prefer Kappa to kill batch/stream drift when replay is feasible; "
            "keep Lambda only when a separate batch path genuinely earns its "
            "double-maintenance cost."
        ),
    ),
    Comparison(
        question="ETL or ELT?",
        options=(
            Option(
                name="ELT (load raw, transform in place)",
                when="Lakehouse/warehouse target with cheap storage and elastic "
                "decoupled compute (the analytics default).",
                pros=(
                    "Immutable raw layer -> re-derive any table without "
                    "re-extracting.",
                    "Transforms are versioned SQL/Spark over the lake.",
                    "Shorter path to durable landing.",
                ),
                cons=(
                    "Raw (possibly sensitive) data lands before cleansing.",
                    "Pushes transform compute onto the target.",
                ),
            ),
            Option(
                name="ETL (transform before load)",
                when="Sink is rigid/expensive, or raw data must not land "
                "(PII minimization / compliance).",
                pros=(
                    "Sink only ever sees clean, conformed data.",
                    "Sensitive fields can be redacted before any write.",
                ),
                cons=(
                    "Re-deriving downstream needs re-extracting from source.",
                    "Bespoke pre-load transform tier to maintain.",
                ),
            ),
        ),
        rule_of_thumb=(
            "Default to ELT on a lakehouse (cheap storage + elastic compute "
            "make a raw safety net almost free); switch to ETL when you must not "
            "persist raw or the target is constrained."
        ),
    ),
    Comparison(
        question="Log-based or query-based CDC?",
        options=(
            Option(
                name="Log-based CDC",
                when="You can read the source's transaction log "
                "(WAL/binlog/redo).",
                pros=(
                    "Captures every change incl. hard deletes, in commit order.",
                    "Near-zero load on the source; low latency.",
                ),
                cons=(
                    "Needs log access/permissions and a connector.",
                    "More operational machinery (offsets, snapshots).",
                ),
            ),
            Option(
                name="Query-based / trigger-based CDC",
                when="No log access; you have a trustworthy updated_at or can "
                "add triggers.",
                pros=(
                    "Portable, simple, works on almost any source.",
                ),
                cons=(
                    "Adds query/trigger load to the source.",
                    "Misses hard deletes and intra-poll updates.",
                    "Depends on a reliable updated_at column.",
                ),
            ),
        ),
        rule_of_thumb=(
            "Prefer log-based CDC whenever the log is reachable; fall back to "
            "query-based only when it isn't, and accept it can miss deletes."
        ),
    ),
    Comparison(
        question="Exactly-once, or at-least-once + idempotent sink?",
        options=(
            Option(
                name="At-least-once + idempotent/transactional sink",
                when="Almost always -- the practical, cheaper way to get "
                "exactly-once *results*.",
                pros=(
                    "Simple delivery; no data loss.",
                    "Upsert/MERGE by a deterministic key makes replays "
                    "harmless.",
                ),
                cons=(
                    "Sink must support idempotent upsert or transactions.",
                    "Need a stable deterministic id per output row.",
                ),
            ),
            Option(
                name="Engine end-to-end exactly-once",
                when="Sink can't dedup/upsert and you need the engine to "
                "coordinate offset+output commits.",
                pros=(
                    "Strong guarantee handled by the framework.",
                ),
                cons=(
                    "Transactions/2PC + frequent checkpoints cost throughput.",
                    "Breaks if any hop is non-transactional/non-idempotent.",
                ),
            ),
        ),
        rule_of_thumb=(
            "Exactly-once = at-least-once delivery + an idempotent or "
            "transactional sink. Reach for true end-to-end exactly-once only "
            "when you can't make the sink idempotent."
        ),
    ),
    Comparison(
        question="Time-schedule or event/sensor-trigger the run?",
        options=(
            Option(
                name="Time schedule (cron-like)",
                when="Source readiness is predictable or no arrival signal "
                "exists.",
                pros=(
                    "Trivial to configure and reason about.",
                ),
                cons=(
                    "Runs blind to readiness -> empty/partial runs if the "
                    "source is late.",
                ),
            ),
            Option(
                name="Event / sensor trigger",
                when="An arrival signal exists (file landed, upstream partition "
                "ready, message published).",
                pros=(
                    "Runs exactly when data is actually ready.",
                    "Avoids fixed sleeps and partial reads.",
                ),
                cons=(
                    "Needs a reliable signal and sensor management.",
                ),
            ),
        ),
        rule_of_thumb=(
            "Prefer event/sensor triggering so a late source delays the run "
            "rather than producing a half-empty table; fall back to a schedule "
            "only when no readiness signal exists."
        ),
    ),
    Comparison(
        question="Push or pull ingestion?",
        options=(
            Option(
                name="Pull (you extract from the source)",
                when="Source exposes a queryable API/DB with a cursor or log.",
                pros=(
                    "You control pacing, batching, and the watermark.",
                    "No source-side change needed.",
                ),
                cons=(
                    "Polling adds source load and a latency floor.",
                    "Can miss changes between polls (query-based).",
                ),
            ),
            Option(
                name="Push (source emits events to you)",
                when="You control the source / it can publish to a log or "
                "webhook.",
                pros=(
                    "Lowest latency; producers don't block on the warehouse.",
                    "Naturally decoupled via a durable log buffer.",
                ),
                cons=(
                    "You must own a durable buffer, dedup, and retries.",
                    "At-least-once delivery -> duplicates to handle.",
                ),
            ),
        ),
        rule_of_thumb=(
            "Push (onto a durable log) when you own the source and want low "
            "latency + decoupling; pull when the source is external and only "
            "offers a query/cursor."
        ),
    ),
)


# ---------------------------------------------------------------------------
# Heuristics
# ---------------------------------------------------------------------------

_HEURISTICS: tuple[Heuristic, ...] = (
    Heuristic(
        name="Latency SLA picks the track",
        rule="< ~1 min -> streaming; seconds-to-a-minute -> micro-batch; "
        "hours/days -> batch. The SLA, not the hype, chooses.",
        example="A dashboard reviewed each morning has an hours SLA -> batch, "
        "even if 'real-time' was requested.",
    ),
    Heuristic(
        name="Partition = unit of parallelism",
        rule="A log partition is consumed by at most one consumer in a group, "
        "so max consumer parallelism = partition count. Size partitions >= "
        "target consumer instances, with headroom to scale out.",
        example="Need 8 parallel consumers eventually -> create >= 8 (often "
        "12-16) partitions up front; you cannot exceed partition count.",
    ),
    Heuristic(
        name="Exactly-once is a recipe, not a feature",
        rule="Exactly-once result = at-least-once delivery + idempotent or "
        "transactional sink. If the sink can't upsert/dedup, you don't have "
        "it.",
        example="MERGE by (key, window) makes a replayed micro-batch overwrite "
        "the same row -> exactly-once output.",
    ),
    Heuristic(
        name="Size the watermark to the tail, not the worst case",
        rule="Set watermark lag near the high-percentile (e.g. p99) event "
        "delay, not the absolute max. Bigger lag = more correctness but later "
        "emission and larger state.",
        example="If 99% of events arrive within 5 min, watermark lag ~5 min + a "
        "small allowed-lateness grace, not 60 min.",
    ),
    Heuristic(
        name="Checkpoint interval trades recovery vs overhead",
        rule="Shorter checkpoints -> less reprocessing on restart but more "
        "runtime overhead; longer -> cheaper steady-state but more replay after "
        "a crash. Tune to acceptable recovery time.",
        example="A few-second checkpoint for tight recovery on a fraud stream; "
        "tens of seconds where a minute of reprocess is fine.",
    ),
    Heuristic(
        name="Bounded retries, then DLQ",
        rule="Retry transient failures a small fixed number of times with "
        "backoff, then route to a DLQ. Never retry a poison record forever -- "
        "it stalls the partition behind it.",
        example="3 retries with exponential backoff; on the 4th, send the raw "
        "payload + error to a dead-letter topic and move on.",
    ),
    Heuristic(
        name="Always keep the raw layer (ELT bias)",
        rule="Land raw immutably before transforming. On a lakehouse, cheap "
        "storage means a permanent raw copy costs little and lets you "
        "re-derive any table without re-extracting from source.",
        example="Bronze keeps source JSON verbatim; a metric redefinition is a "
        "re-run of silver/gold SQL, not a re-pull.",
    ),
    Heuristic(
        name="CDC bootstrap = snapshot + stream",
        rule="Initialize state with a consistent table snapshot, then stream "
        "changes from the log offset captured at snapshot time -- no gap, no "
        "overlap. Apply changes as ordered MERGEs.",
        example="Snapshot at LSN X, then consume the change log from X forward "
        "and upsert by primary key.",
    ),
    Heuristic(
        name="Every task idempotent before any auto-retry/backfill",
        rule="Use overwrite-partition or MERGE, never blind append. Idempotency "
        "is the precondition that makes retries and backfills safe.",
        example="Re-running 'date=2026-06-20' overwrites that partition, so a "
        "retry never doubles rows.",
    ),
    Heuristic(
        name="Cap unbounded window state",
        rule="Stateful windows/joins must expire state via watermarks + bounded "
        "allowed lateness, or the state store grows without limit. No "
        "watermark = a memory/state leak.",
        example="Set a watermark so closed session windows drop from state "
        "instead of being retained forever.",
    ),
    Heuristic(
        name="Sensor-gate, don't sleep",
        rule="Gate a downstream task on an arrival signal (file/partition/"
        "message), not a fixed sleep. Fixed sleeps either waste time or fire on "
        "incomplete data.",
        example="Wait on the source partition's _SUCCESS marker rather than "
        "'sleep 30m and hope it landed'.",
    ),
)


# ---------------------------------------------------------------------------
# Pitfalls
# ---------------------------------------------------------------------------

_PITFALLS: tuple[str, ...] = (
    "Choosing streaming when batch (or micro-batch) already meets the SLA -- "
    "paying for always-on jobs, state, and exactly-once that the requirement "
    "never asked for.",
    "Conflating event time and processing time -- computing business metrics on "
    "wall-clock arrival, so numbers shift with system lag, backlogs, and "
    "retries.",
    "No dead-letter queue -- one poison record fails, retries, fails again, and "
    "stalls the whole partition behind it (head-of-line blocking).",
    "Non-idempotent tasks -- a blind INSERT/append that duplicates rows on every "
    "retry or backfill, instead of overwrite-partition or MERGE.",
    "Tight producer/consumer coupling with no buffer -- a slow or down consumer "
    "back-pressures and stalls producers, or events are dropped, because there "
    "is no durable log in between.",
    "Unbounded state in windowed aggregations/joins -- no watermark or allowed "
    "lateness, so the state store grows until the job OOMs.",
    "Claiming exactly-once from the engine while a downstream sink is "
    "non-transactional and non-idempotent -- the guarantee is broken at that "
    "hop regardless of engine config.",
    "Query-based CDC treated as complete -- silently missing hard deletes and "
    "intra-poll updates because it only sees the latest updated_at.",
    "Putting heavy data through the orchestrator -- routing rows through the "
    "scheduler process instead of dispatching to the engine and passing "
    "partition/path references.",
    "Skewed partition key -- one hot key/partition becomes a single overloaded "
    "consumer while the rest sit idle, capping real parallelism.",
    "Lambda logic drift -- the batch and speed paths implement the same metric "
    "differently and quietly disagree.",
)


# ---------------------------------------------------------------------------
# At-scale notes
# ---------------------------------------------------------------------------

_AT_SCALE: tuple[str, ...] = (
    "Consumer parallelism caps at partition count: at high throughput you can't "
    "add consumers past the number of partitions, and repartitioning a live "
    "topic to add more is disruptive -- size partitions for future scale up "
    "front.",
    "Shuffle / network becomes the dominant cost: wide joins and aggregations "
    "move data across the cluster, so at TB/PB the win comes from reducing data "
    "scanned/shuffled (partition pruning, pre-aggregation, broadcast small "
    "sides), not from raw CPU.",
    "Watermark and state-store growth: long windows, large allowed lateness, "
    "and high-cardinality keys blow up keyed state; bound it with watermarks, "
    "TTLs, and incremental checkpoints or the job OOMs / checkpoints slow to a "
    "crawl.",
    "Backpressure under spikes: without a durable log buffer and "
    "backpressure-aware reads, a downstream slowdown either drops data or "
    "exhausts memory; the log's retention window is the shock absorber.",
    "The orchestrator scheduler itself becomes a bottleneck: thousands of "
    "tasks/partitions, dense backfills, and tight schedules overload the "
    "scheduler -- cap concurrency, prefer dynamic mapping over huge static "
    "DAGs, and keep heavy data out of the scheduler process.",
    "Exactly-once costs more at high throughput: transactional commits, "
    "two-phase coordination, and frequent checkpoints add latency and reduce "
    "throughput -- prefer idempotent upserts where they suffice.",
    "Small-file explosion from streaming/micro-batch: frequent tiny writes "
    "create millions of files that wreck downstream read performance; schedule "
    "compaction/OPTIMIZE to bundle them.",
    "Backfills compete with live load: reprocessing months of history can "
    "starve production runs -- bound backfill concurrency and isolate its "
    "compute so the live SLA holds.",
)


# ---------------------------------------------------------------------------
# Interview signals
# ---------------------------------------------------------------------------

_INTERVIEW_SIGNALS: tuple[str, ...] = (
    "Derives the track (batch / micro-batch / stream) from the stated latency "
    "SLA and cost, instead of defaulting to streaming because it sounds "
    "impressive.",
    "For streaming, names the full spine -- a durable replayable log + a "
    "stateful processor + a low-latency serving store -- and explains why each "
    "is there (buffer/decouple, stateful compute, fast serving).",
    "Explains exactly-once honestly: at-least-once delivery + an idempotent or "
    "transactional sink, not a magic checkbox; identifies which hop would break "
    "the guarantee.",
    "Designs for replay and backfill from the start -- durable log/offsets, "
    "immutable raw layer, idempotent sinks, and parameterized re-runs -- rather "
    "than treating reprocessing as an afterthought.",
    "Separates event time from processing time and reaches for watermarks, "
    "windows (tumbling/sliding/session), and allowed lateness to handle "
    "out-of-order data.",
    "Defaults to ELT on a lakehouse and can justify it (cheap storage + elastic "
    "compute + a re-derivable raw layer), while knowing when ETL is required.",
    "Prefers log-based CDC and remembers query-based CDC misses hard deletes; "
    "describes the snapshot+stream bootstrap.",
    "Makes tasks idempotent (MERGE / overwrite-partition) and gates them with "
    "sensors, so retries and backfills are safe and runs don't fire on "
    "incomplete data.",
    "Plans error handling explicitly -- DLQ for poison records, bounded retries "
    "for transient failures, and a durable buffer for backpressure -- instead "
    "of letting one bad record stall the pipeline.",
    "Reasons about partition count as the parallelism unit and calls out "
    "shuffle, state growth, and small-file compaction as the things that break "
    "at TB/PB scale.",
)


# ---------------------------------------------------------------------------
# The dossier
# ---------------------------------------------------------------------------

DEEP: DeepStep = DeepStep(
    step_key="pipeline",
    title="Pipeline Design",
    overview=(
        "Decide how data physically moves and transforms from source to "
        "destination. The latency SLA picks the track -- batch (orchestrated "
        "DAGs landing raw in object storage, then layering), true streaming "
        "(log -> stateful processor -> low-latency serving store), or "
        "micro-batch in between -- and hybrid patterns (Lambda's batch+speed "
        "paths vs Kappa's single replayable streaming path) cover when you need "
        "both fresh and accurate. The hard parts are ingestion pattern (full / "
        "incremental-watermark / CDC / push), ELT-vs-ETL, orchestration of "
        "idempotent retry-safe tasks, delivery semantics (and honest "
        "exactly-once = at-least-once + idempotent sink), event-time windowing "
        "with watermarks, and error handling (DLQ, replay, backpressure). "
        "Tool-agnostic throughout: Kafka, Flink, Spark, Airflow/Dagster/Prefect "
        "are examples of categories, not mandates."
    ),
    topics=_TOPICS,
    comparisons=_COMPARISONS,
    heuristics=_HEURISTICS,
    pitfalls=_PITFALLS,
    at_scale=_AT_SCALE,
    interview_signals=_INTERVIEW_SIGNALS,
)


__all__ = ["DEEP"]
