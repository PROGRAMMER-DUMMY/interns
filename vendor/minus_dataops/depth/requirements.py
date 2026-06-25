"""Deep dossier for step 1 -- Requirements Gathering.

The cheapest minutes in any data-platform design are the opening ones spent
asking clarifying questions. The architecture is not a free choice: it is forced
by who the consumer is, the latency SLA they need, the volume they generate, the
sensitivity of the data, and what it is allowed to cost. This module encodes that
reasoning -- personas, functional + non-functional requirements, back-of-the-
envelope sizing, the decision forks, and the numeric heuristics -- so the rest of
MinusDataOps can reason from one source of truth.

Tool- and cloud-agnostic throughout: named technologies are examples of a
category, never the only option. ASCII only.
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
        name="End-user personas drive the architecture",
        summary=(
            "The single most architecture-shaping question is 'who consumes this and "
            "how?'. Each persona implies a different serving layer, grain, freshness, "
            "and query shape. Identify them before drawing a box."
        ),
        points=(
            "SQL / BI analyst: wants curated, pre-joined, aggregated tables and an "
            "interactive query engine (warehouse / lakehouse). Tolerates minutes-to-"
            "hours freshness; cares about column pruning, partition pruning, and "
            "concurrency, not single-row lookups.",
            "ML feature-store consumer: needs point-in-time-correct features with no "
            "label leakage, a row grain that matches the training join key, and an "
            "offline store (batch training) plus an online store (low-latency serving) "
            "that must agree -- train/serve skew is the failure mode.",
            "Real-time product surface: a user-facing feature (live counter, fraud "
            "check, recommendation) needs sub-second point reads from a key-value / "
            "OLAP serving store fed by a streaming path; analytical scans are the wrong "
            "tool here.",
            "Exec dashboard: small number of high-trust aggregates, refreshed on a "
            "predictable cadence; correctness and consistency matter far more than "
            "freshness. Often satisfied by a nightly batch + a Gold table.",
            "Reverse-ETL back to operational systems: analytics output is pushed into "
            "CRM / marketing / ops tools, so the warehouse becomes a *source* -- now you "
            "owe idempotent writes, dedupe keys, and rate limits against downstream APIs.",
            "Embedded / customer-facing analytics: dashboards shipped to external "
            "tenants raise multi-tenancy, per-tenant isolation, row-level security, "
            "and noisy-neighbour concerns to first-class requirements.",
        ),
        examples=(
            "Same clickstream feeds an analyst (Gold daily rollups), an ML team (point-"
            "in-time feature table), and a live 'trending now' widget (streaming counter "
            "in a key-value store) -- three serving layers off one source.",
            "An exec KPI tile that must tie out to finance to the cent argues for batch + "
            "a reconciliation gate, not a best-effort streaming estimate.",
        ),
    ),
    Topic(
        name="Functional requirements -- who / what / how",
        summary=(
            "Functional requirements define what the system must do, in three axes: the "
            "consumer (who), the exact data need and its grain (what), and the access "
            "mechanism (how)."
        ),
        points=(
            "Who: name the consumer and their skill/tooling -- a SQL analyst, an ML "
            "pipeline, a product backend, an external customer. The answer fixes the "
            "serving contract.",
            "What -- data need: raw event grain, cleaned row grain, aggregated metrics, "
            "or historical snapshots. Asking for the wrong grain is expensive to undo.",
            "What -- grain is a hard requirement: you can always aggregate up from a "
            "finer grain but you can never recover a finer grain you didn't keep. When "
            "unsure, land the finest grain you can afford in Bronze.",
            "How -- access path: direct SQL, a BI tool (Tableau / Power BI / Looker), a "
            "REST/gRPC API, a feature-store SDK, or a file drop. Each path implies a "
            "different store and contract.",
            "Pull vs push: does the consumer query when they want (pull), or must the "
            "platform deliver/notify on change (push, e.g. webhook, queue, reverse-ETL)?",
            "Define 'done': the acceptance criteria and who signs off (the analyst, the "
            "data steward, finance, security). A requirement without an owner is a wish.",
        ),
        examples=(
            "'Analysts need revenue by region by day' -> grain = (region, day), access = "
            "SQL/BI, store = Gold aggregate table.",
            "'The app needs a user's lifetime order count in <50 ms' -> grain = per-user "
            "single row, access = key-value point read, fed incrementally.",
        ),
    ),
    Topic(
        name="Latency SLA tiers and what each forces",
        summary=(
            "Freshness is the requirement that most often decides batch vs streaming and "
            "the whole serving stack. Pin it to a tier with a number, not adjectives "
            "like 'real-time'."
        ),
        points=(
            "Sub-second: forces a streaming ingestion path + a low-latency serving store "
            "(in-memory cache, key-value, real-time OLAP) and precomputed/incremental "
            "state; full scans at query time are out.",
            "Seconds: streaming or micro-batch; an indexed serving store; you can do some "
            "on-read computation but heavy joins must be precomputed.",
            "Minutes: micro-batch or frequent incremental batch usually suffices; a "
            "warehouse/lakehouse with incremental MERGE is enough -- full streaming is "
            "often over-engineering here.",
            "Hours: scheduled batch; cheapest and simplest; columnar files + a query "
            "engine. The default for most analytics.",
            "Daily / longer: classic nightly ETL; optimize for cost and reliability, not "
            "latency; biggest backfill windows available.",
            "Distinguish *freshness* (how old the newest data is) from *query latency* "
            "(how fast a single query returns) -- they are independent SLAs and you must "
            "state both.",
        ),
        examples=(
            "Fraud decision at checkout: sub-second -> Kafka-style log + stream processor "
            "+ key-value feature lookup.",
            "Daily finance reconciliation: hours/daily -> nightly Spark batch into Gold, "
            "no streaming infrastructure to operate.",
        ),
    ),
    Topic(
        name="Volume tiers and their compute/storage consequences",
        summary=(
            "Per-day and total retained volume decide single-node vs distributed compute "
            "and the storage layout. Estimate it in step 1; don't discover it in prod."
        ),
        points=(
            "GB/day, working set that fits in one box's memory (~100-200 GB): a single-"
            "node engine (Pandas / Polars / DuckDB) is faster and far cheaper than a "
            "cluster -- no shuffle, no coordination overhead.",
            "TB/day: distributed compute (Spark / distributed warehouse), columnar files, "
            "and partition pruning become mandatory; you cannot full-scan affordably.",
            "PB total: cost and data layout dominate every decision -- tiered storage, "
            "aggressive compression, file compaction, and z-order/clustering to make "
            "scans skip data; metadata and small-file management become real engineering.",
            "Storage layout follows access pattern: partition by the column most queries "
            "filter on (usually event date) so engines prune whole partitions.",
            "Compression and format choice change the footprint by 5-10x; raw JSON to "
            "columnar Parquet is a large, free-on-write win on both storage and scan IO.",
            "Replication for durability (typically 3x in distributed stores) multiplies "
            "raw footprint -- size for replicated bytes, not logical bytes.",
        ),
        examples=(
            "20 GB/day of logs with a 30-day window = ~600 GB -> fits a single beefy node "
            "with Polars/DuckDB; a Spark cluster is wasted spend.",
            "5 TB/day clickstream -> distributed engine + date partitioning + columnar "
            "format is the floor, not an optimization.",
        ),
    ),
    Topic(
        name="Availability -- SLA vs SLO vs SLI, error budgets, RTO/RPO",
        summary=(
            "Availability is a quantified requirement with distinct vocabulary. Conflating "
            "the terms is a classic tell; separating them is a strong signal."
        ),
        points=(
            "SLI (indicator): the measured metric, e.g. % of pipeline runs that land "
            "fresh data by the deadline, or query success rate.",
            "SLO (objective): the internal target for the SLI, e.g. 99.5% of daily loads "
            "complete by 06:00.",
            "SLA (agreement): the externally promised guarantee with consequences "
            "(credits/penalties). Always set the SLA looser than the SLO so you keep "
            "headroom.",
            "Error budget = 1 - SLO. It quantifies how much failure is allowed and turns "
            "reliability vs feature-velocity into a budget conversation rather than an "
            "argument.",
            "RPO (recovery point objective): how much data loss is tolerable on failure "
            "-> drives ingestion checkpoint/commit frequency and the source of truth/"
            "replay log.",
            "RTO (recovery time objective): how fast you must be back -> drives "
            "redundancy, multi-AZ/region, and backfill/replay speed. Tight RTO+RPO is "
            "expensive; make the business state the number.",
        ),
        examples=(
            "RPO = 0 means you cannot lose a single event -> a durable replayable log "
            "(Kafka-style) and exactly-once/at-least-once + dedupe.",
            "99.9% availability is ~43 min downtime/month of error budget; 99.99% is ~4 "
            "min -- each extra nine multiplies cost.",
        ),
    ),
    Topic(
        name="Retention, lifecycle, consistency, freshness and cost",
        summary=(
            "Several non-functional requirements that are routinely treated as "
            "afterthoughts but reshape storage tiers, write semantics, and the bill."
        ),
        points=(
            "Retention & lifecycle: define hot (frequent access, fast store), warm (less "
            "frequent), cold (rare, cheap object storage), and archive (compliance-only, "
            "glacier-class). TTL and tiering policies, plus legal hold that overrides "
            "deletion, must be stated up front.",
            "Consistency: does the consumer need strongly consistent reads (read-your-"
            "writes, exact aggregates) or is eventual consistency acceptable for lower "
            "latency/cost? Analytics usually tolerates eventual; financial/operational "
            "often does not.",
            "Freshness vs cost trade: tighter freshness means more frequent runs or "
            "streaming, which costs more compute -- price the freshness requirement, "
            "don't assume 'real-time' is free.",
            "Cost as a first-class requirement: a stated budget rules out architectures "
            "as hard as a latency SLA does. Capture $/TB stored, $/query, and steady-"
            "state monthly compute, and design to it.",
            "Read:write ratio and access shape: read-heavy analytical scans, write-heavy "
            "ingestion, and point-lookup workloads each want different stores and "
            "indexing; ask which dominates.",
            "Success metrics: agree the measurable definition of success (adoption, query "
            "p95, freshness hit-rate, cost/query) and who signs off -- otherwise scope "
            "creeps and 'done' is undefined.",
        ),
        examples=(
            "Logs: 7 days hot for debugging, 90 days warm for trend analysis, archive to "
            "cheap cold storage for 7 years for compliance, then expire.",
            "'Keep cost under $X/month' immediately rules out always-on streaming when a "
            "4x/day micro-batch meets the freshness SLA.",
        ),
    ),
    Topic(
        name="Sensitivity, governance and compliance",
        summary=(
            "Data sensitivity is a requirement, not a later 'security review'. It dictates "
            "masking, access control, residency, lineage, and sometimes the whole "
            "architecture. Name the constraints in step 1."
        ),
        points=(
            "Classify the data: PII (names, emails), PHI (HIPAA-regulated health data), "
            "financial/PCI, or non-sensitive. Classification drives every downstream "
            "control.",
            "Regulatory regime: GDPR/CCPA grant deletion + access rights (right to be "
            "forgotten forces per-subject deletes, which clashes with append-only "
            "immutability -- plan for it); HIPAA mandates audit + minimum-necessary "
            "access; PCI scopes cardholder data tightly.",
            "Data residency / sovereignty: data may be legally required to stay in a "
            "region -> constrains storage location, replication topology, and cross-"
            "region processing.",
            "Masking and least privilege: column-level masking, row-level security, "
            "tokenization/hashing of identifiers, and role-based access decided at design "
            "time -- bolting it on later means re-platforming.",
            "Lineage and audit: who touched what, where a field came from. At scale this "
            "is mandatory for both compliance and debugging trust failures.",
            "Right-to-be-forgotten vs immutable Bronze: reconcile by keying deletes, "
            "crypto-shredding (delete the key), or partitioning subjects so deletes are "
            "cheap -- decide before you build the immutable layer.",
        ),
        examples=(
            "PHI workload -> encryption at rest + in transit, column masking on names/"
            "dates, audited access, residency-pinned storage, retention tied to legal "
            "schedule.",
            "GDPR delete request -> crypto-shred per-user keys instead of rewriting "
            "petabytes of immutable history.",
        ),
    ),
    Topic(
        name="Back-of-the-envelope sizing",
        summary=(
            "A number justifies every later choice. Before any architecture, estimate "
            "volume and throughput from first principles, then adjust for peak, growth, "
            "replication, and compression."
        ),
        points=(
            "Daily ingest: bytes/day = DAU x sessions/user x events/session x "
            "payload_bytes. This single number can rule out streaming or force a "
            "distributed engine.",
            "Project out: per-month ~= bytes/day x 30; per-year ~= x 365; retained = "
            "bytes/day x retention_days. Size storage on *retained*, not daily.",
            "Throughput: average QPS = daily_requests / 86,400. Peak QPS = average x "
            "peak_factor (traffic is bursty; peak factor 2-5x is typical). Provision for "
            "peak, not average.",
            "Read:write ratio scales the read side independently -- a 100:1 read:write "
            "workload sizes the serving/cache tier off reads, the ingest tier off writes.",
            "Growth rate compounds: a 10%/month growth roughly doubles volume in ~7 "
            "months -- design for 12-24 months out, not today's number.",
            "Apply replication (raw stored bytes = logical x replication_factor, often "
            "3x) and compression (columnar typically 3-5x, JSON->Parquet 5-10x) -- they "
            "pull the stored footprint in opposite directions, so include both.",
        ),
        examples=(
            "Worked example: 10M DAU x 5 sessions x 20 events x 1 KB = 1e7 x 5 x 20 x "
            "1000 = 1e12 bytes/day = ~1 TB/day raw. Per year ~365 TB raw. With 5x "
            "columnar compression -> ~73 TB/year stored; with 3x replication on the "
            "stored bytes -> ~219 TB/year of physical storage.",
            "Throughput from the same: 1e7 x 5 x 20 = 1e9 events/day. Average ~11,600 "
            "events/s; at peak factor 3x -> ~35,000 events/s -> clearly a distributed "
            "streaming ingest, not a single node.",
            "Counter-example: 50k DAU x 3 x 10 x 500 B = ~0.75 GB/day; 30-day retention "
            "~22 GB -> fits one node, single-node engine, no cluster.",
        ),
    ),
)


# ---------------------------------------------------------------------------
# Comparisons
# ---------------------------------------------------------------------------

_COMPARISONS: tuple[Comparison, ...] = (
    Comparison(
        question="Is streaming justified, or does batch meet the SLA?",
        options=(
            Option(
                name="Batch / micro-batch",
                when="Freshness SLA is minutes-to-daily and cost/simplicity matter.",
                pros=(
                    "Cheaper and simpler to operate",
                    "Easy to reprocess/backfill",
                    "Mature tooling and clear correctness",
                ),
                cons=(
                    "Data is stale between runs",
                    "Cannot serve sub-second/seconds freshness",
                ),
            ),
            Option(
                name="Streaming",
                when="Freshness SLA is sub-second-to-seconds (fraud, live product surfaces).",
                pros=(
                    "Low end-to-end latency",
                    "Reacts to events as they arrive",
                ),
                cons=(
                    "More moving parts and higher always-on cost",
                    "Harder exactly-once, state, and reprocessing semantics",
                    "Often over-engineering when batch already meets the SLA",
                ),
            ),
        ),
        rule_of_thumb=(
            "Let the latency SLA decide. If minutes-to-hours is acceptable, batch; reserve "
            "streaming for sub-minute requirements you can actually name."
        ),
    ),
    Comparison(
        question="Does the consumer need strong or eventual consistency?",
        options=(
            Option(
                name="Strong consistency",
                when="Reads must reflect the latest writes (finance, operational decisions, "
                "exact aggregates).",
                pros=(
                    "Read-your-writes correctness",
                    "No reconciliation surprises",
                ),
                cons=(
                    "Higher latency and cost",
                    "Harder to scale horizontally / across regions",
                ),
            ),
            Option(
                name="Eventual consistency",
                when="Brief staleness is acceptable for lower latency/cost (most analytics, "
                "trends, dashboards).",
                pros=(
                    "Lower latency, higher availability",
                    "Scales out cheaply",
                ),
                cons=(
                    "Reads may lag writes",
                    "Confusing if a user expects read-your-writes",
                ),
            ),
        ),
        rule_of_thumb=(
            "Default analytics to eventual; demand strong consistency only where a wrong/"
            "stale read causes a real business error."
        ),
    ),
    Comparison(
        question="Does the consumer pull, or must the platform push?",
        options=(
            Option(
                name="Pull (consumer queries)",
                when="Consumer decides when to read -- analysts, BI tools, ad-hoc SQL.",
                pros=(
                    "Simple; platform owns no delivery state",
                    "Consumer controls freshness/cost of their own reads",
                ),
                cons=(
                    "Consumer may read stale data unknowingly",
                    "No reaction to change",
                ),
            ),
            Option(
                name="Push (platform delivers on change)",
                when="Downstream must react to new data -- alerts, reverse-ETL, event "
                "consumers.",
                pros=(
                    "Timely reaction; decouples consumers",
                    "Good fit for operational/streaming surfaces",
                ),
                cons=(
                    "Platform owns delivery, retries, ordering, and dedupe",
                    "Backpressure and rate limits against downstreams",
                ),
            ),
        ),
        rule_of_thumb=(
            "Pull for human analytics; push when a machine downstream must act on change."
        ),
    ),
    Comparison(
        question="Is the consumer workload OLTP-shaped or OLAP-shaped?",
        options=(
            Option(
                name="OLTP-style (point reads/writes)",
                when="High-QPS single-row lookups/updates by key (product backends, online "
                "feature serving).",
                pros=(
                    "Millisecond point access",
                    "High concurrency on small operations",
                ),
                cons=(
                    "Poor at large scans/aggregations",
                    "Expensive for analytical queries",
                ),
            ),
            Option(
                name="OLAP-style (scans/aggregations)",
                when="Large scans, joins, group-bys over many rows (analytics, reporting, "
                "ML training sets).",
                pros=(
                    "Columnar pruning + partition pruning make wide scans cheap",
                    "Built for aggregation and large joins",
                ),
                cons=(
                    "Bad at single-row point lookups",
                    "Not for high-QPS operational reads",
                ),
            ),
        ),
        rule_of_thumb=(
            "Match the store to the access shape: key-value/OLTP for point reads, "
            "columnar/OLAP for scans. Don't serve a product feature off a scan engine."
        ),
    ),
    Comparison(
        question="Build the capability or buy a managed service?",
        options=(
            Option(
                name="Build (self-managed / open source)",
                when="The capability is core/differentiating, or scale/cost makes managed "
                "uneconomical and you have the team to run it.",
                pros=(
                    "Full control and no vendor lock-in",
                    "Can be cheaper at very large, steady scale",
                ),
                cons=(
                    "You own ops, on-call, upgrades, reliability",
                    "Slow time-to-value",
                ),
            ),
            Option(
                name="Buy (managed / SaaS)",
                when="The capability is undifferentiated plumbing and speed-to-value or a "
                "small team matters.",
                pros=(
                    "Fast to stand up; vendor owns ops/SLA",
                    "Elastic scaling",
                ),
                cons=(
                    "Recurring cost and potential lock-in",
                    "Less control over internals and data location",
                ),
            ),
        ),
        rule_of_thumb=(
            "Buy undifferentiated heavy lifting; build only what is core to the business or "
            "where scale economics clearly flip."
        ),
    ),
    Comparison(
        question="Which availability/SLA tier does the requirement actually need?",
        options=(
            Option(
                name="Best-effort (no hard SLA)",
                when="Internal exploratory/analytics where occasional lateness is fine.",
                pros=(
                    "Cheapest; minimal redundancy",
                    "Simple operations",
                ),
                cons=(
                    "No guarantee; not safe for anything external/operational",
                ),
            ),
            Option(
                name="High availability (e.g. 99.9%)",
                when="Important internal/customer reporting where downtime is noticed.",
                pros=(
                    "Reasonable redundancy at moderate cost",
                    "~43 min/month error budget",
                ),
                cons=(
                    "Needs monitoring, failover, and on-call",
                ),
            ),
            Option(
                name="Mission-critical (99.99%+)",
                when="Revenue/safety-critical real-time surfaces.",
                pros=(
                    "Multi-AZ/region resilience; tight RTO/RPO",
                ),
                cons=(
                    "Each extra nine multiplies cost and complexity",
                    "~4 min/month error budget leaves no slack",
                ),
            ),
        ),
        rule_of_thumb=(
            "Make the business state the number of nines and the RTO/RPO; don't gold-plate "
            "availability the requirements don't ask for."
        ),
    ),
)


# ---------------------------------------------------------------------------
# Heuristics
# ---------------------------------------------------------------------------

_HEURISTICS: tuple[Heuristic, ...] = (
    Heuristic(
        name="Daily volume formula",
        rule="bytes/day = DAU x sessions/user x events/session x payload_bytes. Compute it "
        "before choosing any technology.",
        example="10M x 5 x 20 x 1 KB = ~1 TB/day raw.",
    ),
    Heuristic(
        name="QPS from daily requests",
        rule="average QPS = daily_requests / 86,400. Peak QPS = average x peak_factor.",
        example="1e9 events/day -> ~11,600/s average; at 3x peak -> ~35,000/s.",
    ),
    Heuristic(
        name="Peak factor",
        rule="Real traffic is bursty; peak is 2-5x average. Provision for peak, not "
        "average, or you fall over at the worst moment.",
        example="Average 10k QPS with peak factor 3 -> size for 30k QPS.",
    ),
    Heuristic(
        name="Headroom",
        rule="Plan ~2x headroom over projected peak for spikes, retries, and growth before "
        "you have to re-architect.",
        example="Expected peak 30k QPS -> design the path to sustain ~60k.",
    ),
    Heuristic(
        name="Replication factor",
        rule="Durable distributed stores keep ~3 copies; physical storage = logical x 3. "
        "Size and budget on replicated bytes.",
        example="100 TB logical -> ~300 TB physical at 3x replication.",
    ),
    Heuristic(
        name="Compression ratios",
        rule="Columnar compression is typically 3-5x; converting verbose JSON to columnar "
        "Parquet is often 5-10x. Always apply a compression ratio to raw estimates.",
        example="365 TB/year raw JSON -> ~50-73 TB/year as columnar Parquet.",
    ),
    Heuristic(
        name="Fits-in-memory test",
        rule="If the working set fits in one box's memory (~100-200 GB), a single-node "
        "engine beats a cluster -- no shuffle, no coordination tax.",
        example="600 GB over 30 days but ~20 GB hot working set -> Polars/DuckDB on one "
        "node, not Spark.",
    ),
    Heuristic(
        name="Growth doubling",
        rule="~10%/month compounding roughly doubles volume in ~7 months. Size for 12-24 "
        "months ahead, not today.",
        example="1 TB/day today at 10%/month -> ~2 TB/day within a year.",
    ),
    Heuristic(
        name="Retained, not daily",
        rule="Storage cost is bytes/day x retention_days (x replication, / compression). A "
        "small daily feed with a long retention window can still be large.",
        example="50 GB/day x 365 days = ~18 TB before replication.",
    ),
    Heuristic(
        name="Nines to downtime",
        rule="Translate SLA to an error budget: 99.9% ~ 43 min/month, 99.99% ~ 4 min/"
        "month, 99.999% ~ 26 s/month. Each nine ~10x cost.",
        example="A 99.99% promise leaves only ~4 minutes/month of allowed unavailability.",
    ),
)


# ---------------------------------------------------------------------------
# Pitfalls
# ---------------------------------------------------------------------------

_PITFALLS: tuple[str, ...] = (
    "Drawing the architecture before clarifying requirements -- committing to a design "
    "before knowing the consumer, SLA, and volume.",
    "Saying 'real-time' without a number -- 'real-time' spans sub-second to minutes and "
    "each tier forces a different stack; always pin the latency SLA.",
    "Sizing on average and ignoring peak -- a system fine at average QPS collapses under a "
    "2-5x peak.",
    "Forgetting replication and compression in sizing -- estimates that skip x3 replication "
    "or /5 compression are off by an order of magnitude.",
    "Sizing on daily volume instead of retained volume -- ignoring the retention window "
    "undercounts storage badly.",
    "Treating retention, governance, and compliance as an afterthought -- PII/PHI, "
    "residency, and right-to-be-forgotten can force a re-platform if bolted on late.",
    "Over-provisioning streaming when batch meets the SLA -- paying always-on streaming "
    "cost/complexity for a minutes-or-daily freshness need.",
    "Conflating SLA, SLO, and SLI (or RTO and RPO) -- it hides what is actually promised "
    "versus targeted versus measured.",
    "Choosing the wrong grain -- aggregating away detail you later need, with no way to "
    "recover a finer grain that was never stored.",
    "Ignoring cost as a requirement -- designing for latency/scale with no budget, then "
    "discovering the architecture is uneconomical in production.",
)


# ---------------------------------------------------------------------------
# At scale
# ---------------------------------------------------------------------------

_AT_SCALE: tuple[str, ...] = (
    "Single-node memory is the first wall: a working set past ~100-200 GB no longer fits "
    "one box, forcing distributed compute with its shuffle and coordination overhead.",
    "Full scans become unaffordable: at TB/PB you must partition-prune and cluster/z-order "
    "so engines skip data; a query that 'just scans the table' is a cost incident.",
    "Network shuffle dominates runtime: at scale, moving data between nodes (joins, group-"
    "bys) is the bottleneck, not CPU -- data layout and partitioning decide performance.",
    "The small-files problem appears: high-volume/streaming ingest creates millions of tiny "
    "files that wreck scan performance, requiring compaction (OPTIMIZE) as routine ops.",
    "Cost becomes the binding constraint: at PB scale storage tiering, compression, and "
    "scan-avoidance matter more than any single engine choice -- the bill, not latency, "
    "drives design.",
    "Governance and lineage become mandatory, not optional: at scale, untracked PII, "
    "unknown field provenance, and unauditable access are compliance and trust failures.",
    "Backfills and reprocessing become projects: re-running a definition change over years "
    "of PB-scale history needs partitioned, idempotent, atomic-overwrite reprocessing, not "
    "a quick rerun.",
    "Peak vs average diverges harder: at scale a 3-5x peak is the difference between a "
    "fully-provisioned cluster and a degraded one, so elasticity/autoscaling becomes a "
    "requirement.",
)


# ---------------------------------------------------------------------------
# Interview signals
# ---------------------------------------------------------------------------

_INTERVIEW_SIGNALS: tuple[str, ...] = (
    "Asks clarifying questions before proposing any architecture -- who consumes it, how "
    "fresh, how big, how sensitive, what budget.",
    "Quantifies with back-of-the-envelope math -- derives bytes/day and QPS from DAU x "
    "sessions x events x payload, then projects retained volume.",
    "Ties every technology choice back to a stated requirement -- 'streaming *because* the "
    "SLA is sub-second', not 'streaming because it is modern'.",
    "Separates SLA, SLO, and SLI, and distinguishes RTO from RPO, using error-budget "
    "language.",
    "Names compliance/sensitivity constraints early -- PII/PHI, GDPR/CCPA/HIPAA, residency, "
    "masking -- as design inputs, not a later review.",
    "Accounts for peak vs average, replication, compression, and growth in sizing rather "
    "than quoting a single raw daily number.",
    "Distinguishes freshness from query latency, and consistency from availability, as "
    "independent requirements with their own numbers.",
    "Identifies the access pattern (read:write ratio, point-read vs scan, pull vs push) and "
    "matches the store to it.",
    "Treats cost as a first-class requirement and pushes back on over-engineering when a "
    "simpler tier meets the SLA.",
    "States the grain explicitly and defends keeping the finest affordable grain in the raw "
    "layer.",
    "Asks who signs off and what measurable success looks like before designing.",
)


# ---------------------------------------------------------------------------
# The deep dossier
# ---------------------------------------------------------------------------

DEEP = DeepStep(
    step_key="requirements",
    title="Requirements Gathering",
    overview=(
        "The opening minutes of a data-platform design are spent asking, not drawing. The "
        "architecture is forced -- not freely chosen -- by who consumes the data and how "
        "(persona), what they need at which grain (functional), and the non-functional "
        "envelope: latency SLA tier, volume tier, availability (SLA/SLO/SLI, RTO/RPO), "
        "retention/lifecycle, consistency/freshness, cost, sensitivity/compliance, and "
        "multi-tenancy. Back-of-the-envelope sizing -- bytes/day = DAU x sessions x events "
        "x payload, adjusted for peak, growth, replication, and compression -- turns vague "
        "asks into numbers that justify or rule out every later choice. Get this step "
        "wrong and every downstream decision inherits the error."
    ),
    topics=_TOPICS,
    comparisons=_COMPARISONS,
    heuristics=_HEURISTICS,
    pitfalls=_PITFALLS,
    at_scale=_AT_SCALE,
    interview_signals=_INTERVIEW_SIGNALS,
)


__all__ = ["DEEP"]
