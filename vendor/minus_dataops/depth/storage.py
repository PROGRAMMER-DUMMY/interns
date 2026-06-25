"""Deep dossier for Step 4 -- Storage & File Format.

This is the *deep* knowledge layer behind ``minus_dataops.framework`` step
``storage``. It explains not just which file format or database to pick, but why:
how columnar layouts enable column pruning and predicate pushdown, what lives
inside a Parquet file (row groups, column chunks, pages, encodings, statistics),
why raw files in object storage lack ACID and accumulate small files, and how
open table formats (Delta / Iceberg / Hudi) layer a transaction log on top to fix
that. Compression tradeoffs, copy-on-write vs merge-on-read, object-storage
semantics, and database-family selection round it out.

Content stays tool- and cloud-agnostic: Parquet, Avro, ORC, Delta, Iceberg, Hudi,
S3/ADLS/GCS, and warehouses are named as *examples* of categories, never as the
only valid choice. ASCII only, no emojis; use "->" and "x".
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
        name="Columnar vs row layout and why it matters",
        summary=(
            "The physical layout -- values stored column-by-column or row-by-row -- "
            "decides which workload is cheap. Analytics reads few columns over many "
            "rows; transactional writes touch whole records. They pull in opposite "
            "directions."
        ),
        points=(
            "Columnar stores each column's values contiguously, so a query that needs "
            "3 of 200 columns reads only those 3 (column pruning / projection "
            "pushdown) instead of scanning every byte of every row.",
            "Contiguous same-type values compress far better (long runs, low "
            "cardinality, similar magnitudes) and feed vectorized, SIMD-friendly scans "
            "that process a batch of values per CPU instruction.",
            "Row layout writes/reads a full record in one place -- ideal for OLTP "
            "insert/update/point-lookup and for streaming where you append one event "
            "at a time and never re-shred it into columns.",
            "Reconstructing a single full row from columnar is expensive (gather one "
            "value from each column chunk), which is exactly why columnar is wrong for "
            "high-rate single-record writes.",
            "Rule: read-heavy, wide-table, few-column analytical scans -> columnar; "
            "write-heavy, whole-record, point-access -> row.",
        ),
        examples=(
            "SELECT avg(amount) FROM sales reads one column out of fifty -> columnar "
            "skips the other forty-nine entirely.",
            "A Kafka consumer appending one JSON event per message -> row-based Avro, "
            "not Parquet.",
        ),
    ),
    Topic(
        name="Parquet internals",
        summary=(
            "Parquet is the de-facto columnar analytical format. Knowing its internal "
            "hierarchy explains every tuning knob -- file size, row-group size, "
            "encoding, and how engines skip data they never read."
        ),
        points=(
            "Hierarchy: file -> row groups (a horizontal slice of rows) -> one column "
            "chunk per column within a row group -> pages (the smallest unit of "
            "encoding/compression, typically ~1MB).",
            "Each column chunk carries min/max/null-count statistics; an engine reads "
            "footer metadata first and skips entire row groups whose min/max cannot "
            "satisfy the WHERE predicate (predicate pushdown / file skipping).",
            "Encodings are applied per column: dictionary encoding maps repeated values "
            "to small integer codes, then RLE/bit-packing collapses runs and packs "
            "those codes tightly -- huge wins on low-cardinality columns.",
            "Optional per-column bloom filters answer 'is value X possibly present?' so "
            "high-cardinality equality predicates can skip row groups that min/max "
            "ranges alone cannot prune.",
            "Compression is layered on top of encoding, per page; the footer (schema + "
            "row-group offsets + stats) sits at the end so readers seek once to the "
            "footer, then jump straight to the needed column chunks.",
            "Row-group size is the key tuning knob: too small -> stats are useless and "
            "metadata bloats; too large -> coarse skipping and high memory per read.",
        ),
        examples=(
            "WHERE event_date = '2026-06-01' -> row groups whose date min/max exclude "
            "that day are skipped without reading their data pages.",
            "A status column with 5 distinct values dictionary-encodes to ~3 bits each "
            "before compression.",
        ),
    ),
    Topic(
        name="Avro -- the row-based serialization format",
        summary=(
            "Avro is compact binary, row-oriented, and carries its schema with the "
            "data. It is the natural format for write-heavy ingestion and streaming "
            "buffers where columnar restructuring would be wasted work."
        ),
        points=(
            "Row-based and append-friendly: a producer serializes one whole record in "
            "one shot, with no need to buffer and pivot into columns first.",
            "The writer schema is embedded (in the file header, or referenced by id via "
            "a schema registry for streams), so a reader always knows how to decode.",
            "Strong, well-defined schema evolution: reader and writer schemas are "
            "resolved field-by-field, supporting added/removed fields with defaults "
            "and aliases for renames.",
            "Excellent as a Kafka payload and as a landing buffer for raw events; a "
            "later batch job converts accumulated Avro/event data into Parquet for "
            "analytics (the row-in, column-out pattern).",
            "Poor for analytical scans: no column pruning, so an aggregate over one "
            "column still reads every field of every row.",
        ),
        examples=(
            "Kafka topics serialized as Avro with a Confluent-style schema registry "
            "enforcing producer/consumer compatibility.",
            "Bronze landing zone stored row-wise, then compacted into columnar Silver.",
        ),
    ),
    Topic(
        name="ORC -- columnar with stripes and built-in indexes",
        summary=(
            "ORC is the other major columnar format, conceptually close to Parquet but "
            "organised into stripes with richer lightweight indexes; common in "
            "Hive/Hadoop lineages."
        ),
        points=(
            "Data is split into stripes (large row ranges, often ~64MB-256MB), each "
            "self-contained with its own column data, index, and footer.",
            "Lightweight indexes carry min/max per column plus optional row-group-level "
            "(default ~10k-row) index entries and optional bloom filters for fine "
            "skipping inside a stripe.",
            "Strong compression and good predicate pushdown; functionally it competes "
            "with Parquet -- the choice usually follows engine/ecosystem support more "
            "than a deep technical gap.",
            "Like Parquet, it is a columnar *file* format, not a table format: on its "
            "own it has no transactions, no time travel, and no compaction.",
        ),
        examples=(
            "Hive-era warehouses standardising on ORC; modern stacks more often default "
            "to Parquet under an open table format.",
        ),
    ),
    Topic(
        name="Compression codecs and the speed-vs-ratio tradeoff",
        summary=(
            "Compression trades CPU for I/O. The right codec depends on whether you are "
            "read-bound or write-bound, and whether the codec stays splittable for "
            "parallel reads."
        ),
        points=(
            "Snappy: fast compress/decompress, modest ratio (~2-3x), block-splittable -- "
            "the safe analytical default where CPU is the bottleneck.",
            "Zstd: better ratio than Snappy at comparable or slightly higher CPU, with a "
            "tunable level dial (low for speed, high for storage savings) -- "
            "increasingly the default for cold/archival data.",
            "Gzip: high ratio but slow, and CPU-heavy on the read path; classic gzip is "
            "non-splittable, which can serialize a whole file onto one task and kill "
            "parallelism.",
            "LZ4: fastest, lowest ratio -- chosen when latency dominates and storage is "
            "cheap (e.g. hot intermediate data).",
            "Splittability matters more than raw ratio for big-data parallelism: a "
            "non-splittable file forces one reader per file regardless of cluster size; "
            "columnar formats stay splittable because compression is applied per "
            "page/block, not over the whole file.",
            "Encoding (dictionary/RLE) does most of the heavy lifting on columnar data; "
            "the codec then squeezes the already-encoded bytes.",
        ),
        examples=(
            "Hot, frequently-scanned tables -> Snappy or LZ4; cold history rarely read "
            "-> Zstd at a high level for cheaper storage.",
        ),
    ),
    Topic(
        name="Why raw files need an open table format",
        summary=(
            "A pile of Parquet files in object storage is a 'data lake' with no "
            "guarantees. It cannot give multi-file atomic writes, breaks under "
            "concurrent writers, and drowns in small files. Open table formats add a "
            "transaction layer to fix this."
        ),
        points=(
            "Object storage has no multi-object transaction: a job writing many files "
            "can fail halfway and leave a partially-updated dataset that readers see as "
            "corrupt or double-counted.",
            "Concurrent writers racing on the same prefix can clobber or interleave each "
            "other; there is no isolation, so 'last writer wins' silently loses data.",
            "Streaming and frequent micro-batches produce thousands of tiny files (the "
            "small-file problem), inflating metadata, listing time, and per-file "
            "overhead until reads crawl.",
            "Open table formats add a transaction log / metadata layer (a commit log of "
            "which files belong to which snapshot) giving ACID, snapshot isolation, "
            "schema enforcement, and atomic multi-file commits.",
            "That same log enables time travel (read an old snapshot), atomic partition "
            "overwrite for backfills, and OPTIMIZE/compaction to coalesce small files "
            "into right-sized ones.",
            "Net: the underlying files are still Parquet/ORC; the table format is the "
            "log that makes a set of files behave like one transactional table.",
        ),
        examples=(
            "A streaming append every few seconds -> millions of <1MB files in a year "
            "without scheduled compaction.",
            "Two backfill jobs overwriting the same date with raw Parquet -> "
            "interleaved files; the same overwrite via a table format is one atomic "
            "commit.",
        ),
    ),
    Topic(
        name="Open table formats compared -- Delta / Iceberg / Hudi",
        summary=(
            "The three open table formats all deliver ACID over object storage but "
            "differ in metadata design, partitioning model, and write strategy. The "
            "category matters more than the brand; the differences guide the pick."
        ),
        points=(
            "All three provide ACID transactions, schema enforcement + evolution, time "
            "travel, and compaction/clustering over Parquet (Hudi/Delta) or "
            "Parquet/ORC/Avro (Iceberg) files in object storage.",
            "Metadata: Delta uses an ordered JSON transaction log (with periodic "
            "checkpoints); Iceberg uses a snapshot tree of manifest lists -> manifests "
            "-> data files; Hudi uses a timeline of commits with an index over record "
            "keys.",
            "Partitioning: Iceberg offers hidden partitioning (queries need not name "
            "the partition column) and partition evolution (change partition spec "
            "without rewriting old data); Delta leans on Z-order/liquid clustering "
            "rather than rigid physical partitions.",
            "Write model: Hudi is record-key/upsert-centric (built for CDC + frequent "
            "updates) and natively offers both copy-on-write and merge-on-read; Delta "
            "and Iceberg also support MOR but historically lead with copy-on-write.",
            "Optimization: all support compaction; Delta exposes OPTIMIZE + Z-order, "
            "Iceberg exposes rewrite/compaction + sort/clustering, Hudi runs "
            "clustering and async compaction of its delta logs.",
            "Choose by ecosystem and access pattern, not hype: heavy streaming upserts/"
            "CDC lean Hudi; engine-neutral, partition-evolution needs lean Iceberg; "
            "tight-engine + simple log + Z-order lean Delta.",
        ),
        examples=(
            "Reorganising partitioning from daily to hourly without rewriting history "
            "-> Iceberg partition evolution.",
            "A CDC stream of upserts keyed by customer_id -> Hudi merge-on-read.",
        ),
    ),
    Topic(
        name="Copy-on-write vs merge-on-read",
        summary=(
            "How a table format applies updates/deletes is a core read-vs-write "
            "tradeoff. Copy-on-write rewrites files on update for fast reads; "
            "merge-on-read logs deltas for fast writes and merges on read."
        ),
        points=(
            "Copy-on-write (COW): an update rewrites the whole affected data file with "
            "the new values, so readers always hit clean, fully-merged columnar files "
            "-- fastest reads, but every small update pays a full-file rewrite (write "
            "amplification).",
            "Merge-on-read (MOR): updates/deletes are written as compact delta/log "
            "files; the base data file stays put, and readers merge base + deltas on "
            "the fly -- fast, cheap writes, but slower reads until compaction.",
            "MOR needs background compaction to fold delta logs back into base files; "
            "skip it and read latency and file counts both climb.",
            "Pick COW for read-heavy tables with infrequent updates; pick MOR for "
            "write/update-heavy or streaming-CDC tables where ingest latency matters "
            "more than instant read speed.",
            "It is a per-table choice, not a one-size policy: a hot dashboard table and "
            "a high-churn CDC table can sit in the same lake with different strategies.",
        ),
        examples=(
            "A frequently-queried reporting table updated nightly -> COW.",
            "A CDC sink taking thousands of upserts/minute -> MOR + scheduled "
            "compaction.",
        ),
    ),
    Topic(
        name="Object storage semantics",
        summary=(
            "Cloud object stores (S3 / ADLS / GCS) are the lake substrate. They behave "
            "like a flat key-value store, not a filesystem -- their consistency, "
            "throughput, and listing behaviour shape every layout decision."
        ),
        points=(
            "Objects are addressed by key; 'directories' are just shared key prefixes. "
            "There are no real folders, only string prefixes and a listing API.",
            "Throughput often scales per key-prefix, so concentrating all writes/reads "
            "under one prefix hot-spots that prefix; spreading keys across prefixes "
            "spreads request load.",
            "Modern object stores give read-after-write consistency for new objects, "
            "but overwrites/deletes and especially LIST can lag -- a freshly written "
            "file may not appear in a listing immediately, which is exactly why table "
            "formats track files in a log instead of trusting LIST.",
            "Objects are effectively immutable: you replace, not edit-in-place. This "
            "favours append + compaction patterns over mutate-in-place.",
            "LIST is a paginated, per-request-priced operation; millions of small files "
            "make listing slow and expensive, compounding the small-file problem.",
            "Storage is cheap and durable but request/egress and listing costs are not "
            "-- layout that minimises object count and cross-region reads saves real "
            "money.",
        ),
        examples=(
            "Partition path date=2026-06-21/ is a key prefix, not a directory; the "
            "engine LISTs that prefix.",
            "A table format reads its own manifest/log to know the file set instead of "
            "LISTing the prefix, sidestepping eventual-consistency listing.",
        ),
    ),
    Topic(
        name="Database family selection and the lakehouse",
        summary=(
            "Beyond files, pick the serving store by access pattern and consistency "
            "need. OLTP relational, OLAP columnar warehouses, the NoSQL families, and "
            "NewSQL each fit a shape of query -- and the lakehouse blurs lake and "
            "warehouse."
        ),
        points=(
            "OLTP relational (row-store): strict ACID, joins, point reads/writes, "
            "normalized schema -> transactional systems of record.",
            "OLAP columnar / MPP warehouse: scan-and-aggregate over wide tables, "
            "distributed across nodes -> analytics and BI.",
            "NoSQL families by access pattern: key-value for O(1) lookups by key "
            "(caches, sessions); document for nested, evolving JSON aggregates; "
            "wide-column for huge, sparse, high-write tables queried by partition+"
            "clustering key; graph for relationship traversal (who-knows-whom, "
            "fraud rings).",
            "NewSQL aims for relational ACID + SQL with horizontal scale, closing the "
            "gap when you need both strong consistency and large scale-out.",
            "Lakehouse: an open table format over object storage gives warehouse-like "
            "ACID, schema, and BI performance directly on the lake -- one copy of "
            "data, queryable by many engines, instead of copying lake -> warehouse.",
            "Choose for the dominant access pattern and consistency requirement, not "
            "familiarity: needing multi-row joins and strict consistency rules out most "
            "NoSQL; needing flexible, denormalized, high-write aggregates rules out a "
            "rigid relational schema.",
        ),
        examples=(
            "A shopping cart -> key-value; a product catalog of varied attributes -> "
            "document; time-series IoT events -> wide-column; recommendations over a "
            "social graph -> graph DB.",
            "BI dashboards reading Silver/Gold tables straight off the lake via an open "
            "table format -> lakehouse, no separate warehouse copy.",
        ),
    ),
    Topic(
        name="Storage tiering, small files, and compaction",
        summary=(
            "Cost and performance at scale come down to two operational realities: "
            "tier data by access temperature, and keep file sizes sane through "
            "compaction."
        ),
        points=(
            "Tier by temperature: hot (frequent, low-latency) on standard storage; "
            "warm (occasional) on cheaper tiers; cold/archive (rarely read, "
            "compliance) on the cheapest tiers with higher retrieval latency/cost.",
            "Lifecycle policies move objects down tiers automatically by age/access, so "
            "old partitions cost a fraction of hot ones -- but archive retrieval is "
            "slow and per-request priced, so never tier data you query interactively.",
            "The small-file problem: many tiny files inflate metadata, multiply LIST/"
            "open overhead, and starve parallel scan efficiency; it is the single most "
            "common lake performance killer.",
            "Compaction (OPTIMIZE/rewrite) coalesces small files into right-sized ones "
            "and is effectively mandatory for any streaming/micro-batch table -- "
            "schedule it, do not hope.",
            "Clustering/Z-order during compaction co-locates related values so file-"
            "level min/max statistics prune more aggressively on common predicates.",
        ),
        examples=(
            "Last 30 days of events hot, 31-365 days warm, older archived by a "
            "lifecycle rule.",
            "A streaming table compacted nightly from thousands of 2MB files into "
            "~256MB files.",
        ),
    ),
)


# ---------------------------------------------------------------------------
# Comparisons
# ---------------------------------------------------------------------------

_COMPARISONS: tuple[Comparison, ...] = (
    Comparison(
        question="Parquet vs Avro vs ORC -- which file format?",
        options=(
            Option(
                name="Parquet",
                when="Read-heavy analytical scans over wide tables; the lake default.",
                pros=(
                    "Columnar -> column pruning + predicate pushdown via per-column "
                    "min/max stats and optional bloom filters",
                    "Strong compression from dictionary/RLE encoding; splittable",
                    "Broadest engine support; the substrate for Delta/Iceberg/Hudi",
                ),
                cons=(
                    "Poor for single-record writes -- must buffer and pivot into columns",
                    "Whole-row reconstruction is expensive",
                ),
            ),
            Option(
                name="Avro",
                when="Write-heavy streaming ingestion and Kafka payloads / landing buffers.",
                pros=(
                    "Row-based, append-friendly, compact binary; serializes a record "
                    "in one shot",
                    "Embedded schema + first-class schema evolution (defaults, aliases)",
                ),
                cons=(
                    "No column pruning -> slow analytical scans",
                    "Not a query format; usually converted to columnar for analytics",
                ),
            ),
            Option(
                name="ORC",
                when="Columnar analytics in Hive/Hadoop-lineage ecosystems.",
                pros=(
                    "Columnar with stripes + lightweight indexes and bloom filters",
                    "Strong compression and predicate pushdown, on par with Parquet",
                ),
                cons=(
                    "Narrower ecosystem momentum than Parquet today",
                    "Still just a file format -- no ACID/time travel on its own",
                ),
            ),
        ),
        rule_of_thumb=(
            "Columnar (Parquet/ORC) for reads, row (Avro) for writes/streaming; "
            "ingest as Avro, store analytics as Parquet."
        ),
    ),
    Comparison(
        question="Delta vs Iceberg vs Hudi -- which open table format?",
        options=(
            Option(
                name="Delta Lake",
                when="Tight-engine stacks wanting a simple log + Z-order/clustering.",
                pros=(
                    "Ordered JSON transaction log + checkpoints; mature OPTIMIZE + "
                    "Z-order",
                    "Simple mental model; strong support in its native engine",
                ),
                cons=(
                    "Historically engine-leaning; less hidden/partition-evolution focus",
                ),
            ),
            Option(
                name="Apache Iceberg",
                when="Engine-neutral lakehouse needing hidden + evolvable partitioning.",
                pros=(
                    "Snapshot-tree metadata (manifest lists -> manifests -> files)",
                    "Hidden partitioning + partition evolution without rewriting history",
                    "Broad multi-engine interoperability",
                ),
                cons=(
                    "Metadata model is more moving parts to operate",
                ),
            ),
            Option(
                name="Apache Hudi",
                when="Streaming upserts / CDC with frequent record-level mutations.",
                pros=(
                    "Record-key index built for upserts; native COW and MOR",
                    "Timeline of commits + async compaction tuned for write churn",
                ),
                cons=(
                    "Upsert/index machinery adds operational complexity for "
                    "append-only analytics",
                ),
            ),
        ),
        rule_of_thumb=(
            "All give ACID + time travel + compaction over object storage; pick by "
            "access pattern -- CDC/upserts -> Hudi, partition evolution + engine "
            "neutrality -> Iceberg, simple log + Z-order -> Delta."
        ),
    ),
    Comparison(
        question="Copy-on-write vs merge-on-read?",
        options=(
            Option(
                name="Copy-on-write (COW)",
                when="Read-heavy tables with infrequent updates.",
                pros=(
                    "Readers hit clean, fully-merged files -> fastest reads",
                    "No read-time merge or background log to manage",
                ),
                cons=(
                    "Every update rewrites whole files -> write amplification",
                    "Costly for high-frequency small updates",
                ),
            ),
            Option(
                name="Merge-on-read (MOR)",
                when="Write/update-heavy or streaming-CDC tables.",
                pros=(
                    "Cheap, low-latency writes via compact delta/log files",
                    "Ingest is not blocked by full-file rewrites",
                ),
                cons=(
                    "Read merges base + deltas -> slower reads until compaction",
                    "Requires scheduled compaction or file counts/latency climb",
                ),
            ),
        ),
        rule_of_thumb=(
            "Optimize for the dominant operation: reads -> COW, writes/updates -> MOR "
            "(+ compaction). It is a per-table choice."
        ),
    ),
    Comparison(
        question="Relational vs which NoSQL family?",
        options=(
            Option(
                name="Relational (OLTP) / NewSQL",
                when="Multi-row joins, normalized schema, strict ACID consistency.",
                pros=(
                    "Joins, constraints, transactions; mature SQL",
                    "NewSQL adds horizontal scale while keeping ACID + SQL",
                ),
                cons=(
                    "Rigid schema; classic single-node relational scales out poorly",
                ),
            ),
            Option(
                name="Key-value",
                when="O(1) lookups by a known key (caches, sessions, carts).",
                pros=("Extremely fast point access; trivial horizontal sharding",),
                cons=("No rich queries/joins; you fetch by key or not at all",),
            ),
            Option(
                name="Document",
                when="Nested, evolving JSON aggregates read as a unit.",
                pros=("Flexible schema; whole aggregate stored/fetched together",),
                cons=("Cross-document joins and strong consistency are weak spots",),
            ),
            Option(
                name="Wide-column / Graph",
                when=(
                    "Huge sparse high-write tables by partition+clustering key "
                    "(wide-column); relationship traversal (graph)."
                ),
                pros=(
                    "Wide-column: massive write throughput, time-series friendly",
                    "Graph: cheap multi-hop traversal of relationships",
                ),
                cons=(
                    "Wide-column: query only along the designed key; no ad-hoc joins",
                    "Graph: narrow fit; poor for bulk aggregate scans",
                ),
            ),
        ),
        rule_of_thumb=(
            "Pick from the access pattern + consistency need: need joins/strict ACID "
            "-> relational/NewSQL; otherwise match the NoSQL family to the query shape."
        ),
    ),
    Comparison(
        question="Which compression codec?",
        options=(
            Option(
                name="Snappy",
                when="General analytical default; CPU-bound scans.",
                pros=("Fast both ways; splittable; balanced", "Low CPU on reads"),
                cons=("Only ~2-3x ratio -> more storage/I/O than Zstd",),
            ),
            Option(
                name="Zstd",
                when="Cold/archival or storage-cost-sensitive data.",
                pros=("Better ratio than Snappy; tunable level dial", "Good decode speed"),
                cons=("Higher CPU at high levels than Snappy/LZ4",),
            ),
            Option(
                name="Gzip",
                when="Max ratio, read-rarely data; legacy interchange.",
                pros=("High compression ratio",),
                cons=(
                    "Slow and CPU-heavy; classic gzip is non-splittable -> kills "
                    "parallel reads",
                ),
            ),
            Option(
                name="LZ4",
                when="Latency-critical hot/intermediate data.",
                pros=("Fastest compress/decompress",),
                cons=("Lowest ratio -> most storage",),
            ),
        ),
        rule_of_thumb=(
            "Hot/scanned -> Snappy or LZ4; cold/archive -> Zstd; avoid non-splittable "
            "Gzip for big-data parallelism. Prize splittability over raw ratio."
        ),
    ),
    Comparison(
        question="Raw files vs an open table format?",
        options=(
            Option(
                name="Raw Parquet/ORC in object storage",
                when="Throwaway scratch, one-shot exports, or single-writer immutable dumps.",
                pros=(
                    "Zero extra metadata layer; trivially portable files",
                    "Read by anything that speaks the file format",
                ),
                cons=(
                    "No ACID / atomic multi-file writes -> corruption on partial or "
                    "concurrent writes",
                    "No schema enforcement, time travel, or compaction; small-file sprawl",
                ),
            ),
            Option(
                name="Open table format (Delta/Iceberg/Hudi)",
                when="Any shared, mutated, streamed, or production lakehouse table.",
                pros=(
                    "ACID + snapshot isolation + schema enforcement on object storage",
                    "Time travel, atomic partition overwrite for backfills, "
                    "OPTIMIZE/compaction",
                ),
                cons=(
                    "Transaction log/manifests to maintain; compaction must be scheduled",
                ),
            ),
        ),
        rule_of_thumb=(
            "If more than one writer, any updates, streaming, or production reads touch "
            "it -> open table format. Raw files only for immutable single-writer dumps."
        ),
    ),
    Comparison(
        question="Warehouse vs lakehouse vs lake?",
        options=(
            Option(
                name="Data warehouse",
                when="Curated, governed BI on structured data with tight SLAs.",
                pros=(
                    "Mature SQL, governance, and fast curated query performance",
                    "Strong concurrency and optimization out of the box",
                ),
                cons=(
                    "Proprietary storage / lock-in; pricier; weaker for raw/ML/"
                    "semi-structured data",
                ),
            ),
            Option(
                name="Lakehouse (table format over object storage)",
                when="One copy of data serving BI, ML, and SQL with ACID.",
                pros=(
                    "Open storage + ACID + schema + BI performance; many engines, "
                    "one copy",
                    "Avoids the lake->warehouse copy and its drift",
                ),
                cons=(
                    "Operational maturity (compaction, metadata tuning) is on you",
                ),
            ),
            Option(
                name="Raw data lake",
                when="Cheap landing/archive of raw and semi-structured data.",
                pros=("Cheapest storage; holds any format; schema-on-read flexibility",),
                cons=(
                    "No guarantees -> a 'data swamp' without governance, ACID, or "
                    "compaction",
                ),
            ),
        ),
        rule_of_thumb=(
            "Default to lakehouse: lake-cost open storage with warehouse-grade ACID and "
            "performance, one copy for BI + ML. Pure lake for raw landing only."
        ),
    ),
)


# ---------------------------------------------------------------------------
# Heuristics
# ---------------------------------------------------------------------------

_HEURISTICS: tuple[Heuristic, ...] = (
    Heuristic(
        name="Target file size",
        rule=(
            "Aim for ~128MB-1GB per data file (256MB-1GB is a common sweet spot). "
            "Big enough to amortize open/metadata overhead, small enough for "
            "parallelism and selective skipping."
        ),
        example="Thousands of 2MB streaming files -> compact to ~256MB-512MB files.",
    ),
    Heuristic(
        name="Row-group size",
        rule=(
            "Set Parquet row-group size ~128MB-512MB. Too small wastes stats and "
            "bloats footer metadata; too large makes skipping coarse and memory-heavy."
        ),
        example="A 1GB file at 256MB row groups -> ~4 independently-skippable groups.",
    ),
    Heuristic(
        name="When to compact",
        rule=(
            "Compact when average file size in a partition drops below ~128MB or when "
            "you see thousands of small files per partition. Treat it as scheduled, "
            "not optional, for streaming/micro-batch tables."
        ),
        example="Nightly OPTIMIZE on a CDC sink folding delta logs into base files.",
    ),
    Heuristic(
        name="Columnar compression factor",
        rule=(
            "Columnar analytical data typically compresses ~3-10x via dictionary/RLE "
            "encoding + a codec, far better than row formats on the same data."
        ),
        example="A low-cardinality status column dictionary-encodes to a few bits/value.",
    ),
    Heuristic(
        name="Codec speed vs ratio",
        rule=(
            "Snappy ~2-3x at high speed; Zstd beats Snappy on ratio with a tunable "
            "level; LZ4 fastest/lowest ratio; Gzip highest ratio but slow + "
            "non-splittable. Pick by read-bound vs storage-bound."
        ),
        example="Hot table -> Snappy; year-old cold partitions -> Zstd high level.",
    ),
    Heuristic(
        name="Spread keys across prefixes",
        rule=(
            "Object-store throughput scales per key-prefix, so one prefix/partition "
            "concentrates request load -> spread writes across prefixes to spread "
            "throughput and avoid hot-spotting."
        ),
        example="Bucketing/sharding a key prefix so writes fan out instead of stacking.",
    ),
    Heuristic(
        name="Partition cardinality",
        rule=(
            "Keep partitions coarse enough that each holds at least a few right-sized "
            "files; over-partitioning (e.g. by a high-cardinality id) creates one tiny "
            "file per value and explodes metadata."
        ),
        example="Partition by event_date, not by user_id.",
    ),
    Heuristic(
        name="Ingest row, store column",
        rule=(
            "Land write-heavy/streaming data row-wise (Avro/event log), then batch-"
            "convert to columnar (Parquet under a table format) for analytics."
        ),
        example="Avro from Kafka -> compacted Parquet Silver tables.",
    ),
    Heuristic(
        name="COW vs MOR by update rate",
        rule=(
            "Read-heavy + infrequent updates -> copy-on-write; write/update-heavy or "
            "streaming CDC -> merge-on-read plus scheduled compaction."
        ),
        example="Nightly-refreshed report -> COW; per-second upsert sink -> MOR.",
    ),
)


# ---------------------------------------------------------------------------
# Pitfalls
# ---------------------------------------------------------------------------

_PITFALLS: tuple[str, ...] = (
    "Dumping raw Parquet straight into object storage for a shared/production table -- "
    "no ACID, small-file sprawl, and corruption/double-counts when writers run "
    "concurrently or a write fails halfway.",
    "Using a columnar format (Parquet/ORC) as a streaming write buffer -- you pay "
    "to pivot single records into columns on every append; use a row format (Avro) "
    "and compact to columnar later.",
    "Files too small: thousands of tiny files explode metadata, slow LIST, and "
    "starve parallel scans (the small-file problem).",
    "Files too large: a few giant files give coarse skipping, high memory per read, "
    "and poor parallelism across a cluster.",
    "Over-partitioning by a high-cardinality column -> one tiny file per value, "
    "metadata blowup, and slow listing.",
    "Ignoring compression splittability -- e.g. non-splittable Gzip forces one reader "
    "per file and blocks parallelism regardless of cluster size.",
    "Reaching for NoSQL when the workload needs multi-table joins or strict "
    "consistency -- you end up re-implementing joins/transactions in the application.",
    "Trusting object-store LIST as the source of truth -- eventual-consistency listing "
    "can miss just-written files; let the table format's log track the file set.",
    "Never scheduling compaction on a merge-on-read or streaming table -- read latency "
    "and file counts climb until queries crawl.",
    "Leaving everything on hot storage -- old, rarely-read partitions should tier down "
    "to warm/cold/archive to control cost.",
)


# ---------------------------------------------------------------------------
# At scale
# ---------------------------------------------------------------------------

_AT_SCALE: tuple[str, ...] = (
    "Small-file metadata explosion: millions of tiny files turn footer/manifest "
    "metadata and per-file open overhead into the dominant cost, not the data scan.",
    "Listing/metadata cost dominates: LIST over millions of objects is slow and "
    "per-request priced; the table format's manifest/log lookup replaces brute-force "
    "listing at scale.",
    "Compaction becomes mandatory, not optional -- without scheduled OPTIMIZE/rewrite, "
    "streaming tables drown in small files within days.",
    "Prefix/throughput hot-spotting: concentrating PB-scale traffic under one key "
    "prefix throttles it; layout must spread keys to spread request capacity.",
    "Statistics + bloom filters become the performance lever: good min/max stats and "
    "clustering let engines skip the vast majority of files; bad layout reads "
    "everything.",
    "Storage cost dominates total spend at PB scale -> tiering, higher-ratio codecs "
    "(Zstd), and retention/expiry policies move the cost needle most.",
    "Table-format metadata itself scales: a snapshot/manifest tree or transaction log "
    "with millions of files needs checkpointing/manifest rewriting or planning slows.",
    "Backfills must be atomic partition overwrites; rewriting PB of history as raw "
    "files is both slow and unsafe without the table format's atomic commit.",
)


# ---------------------------------------------------------------------------
# Interview signals
# ---------------------------------------------------------------------------

_INTERVIEW_SIGNALS: tuple[str, ...] = (
    "Matches the file format to the workload -- columnar (Parquet/ORC) for read-heavy "
    "analytics, row (Avro) for write-heavy streaming -- and says why (column pruning "
    "vs whole-record append).",
    "Reaches for an open table format for ACID, schema enforcement, time travel, and "
    "compaction instead of dumping raw Parquet; can explain the small-file and "
    "concurrent-write problems it solves.",
    "Tunes file size (~128MB-1GB) and Parquet row-group size, and treats compaction as "
    "a scheduled job, not an afterthought.",
    "Knows copy-on-write vs merge-on-read and picks per table from the read-vs-write "
    "ratio, mentioning compaction for MOR.",
    "Picks a database family from access pattern + consistency need -- relational/"
    "NewSQL for joins/ACID, KV/document/wide-column/graph by query shape -- rather "
    "than by familiarity.",
    "Mentions predicate/projection pushdown, per-column min/max statistics, and "
    "optional bloom filters as the mechanism behind columnar query speed.",
    "Calls out compression tradeoffs (Snappy vs Zstd vs Gzip vs LZ4) and, crucially, "
    "splittability for parallelism -- not just raw ratio.",
    "Understands object-storage semantics: key-prefix layout, per-prefix throughput, "
    "eventual-consistency listing, immutability, and listing cost.",
    "Distinguishes lake vs warehouse vs lakehouse and argues for one governed copy of "
    "data over a lake->warehouse copy when ACID-on-lake suffices.",
    "Thinks about cost: storage tiering (hot/warm/cold/archive), retention/expiry, and "
    "codec choice as the levers that dominate spend at scale.",
)


# ---------------------------------------------------------------------------
# Export
# ---------------------------------------------------------------------------

DEEP = DeepStep(
    step_key="storage",
    title="Storage & File Format",
    overview=(
        "Where and in which format data lives drives both cost and query speed. The "
        "core forks: row vs columnar file format (write-heavy streaming vs read-heavy "
        "analytics), raw files vs an open table format (ACID, time travel, compaction "
        "over object storage), copy-on-write vs merge-on-read update strategy, "
        "compression codec (speed vs ratio vs splittability), and the database family / "
        "lakehouse pattern that serves the data. Resolve each from the workload's "
        "read/write mix, consistency need, and scale -- then size files and schedule "
        "compaction so the layout stays fast as volume grows."
    ),
    topics=_TOPICS,
    comparisons=_COMPARISONS,
    heuristics=_HEURISTICS,
    pitfalls=_PITFALLS,
    at_scale=_AT_SCALE,
    interview_signals=_INTERVIEW_SIGNALS,
)


__all__ = ["DEEP"]
