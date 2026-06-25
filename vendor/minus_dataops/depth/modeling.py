"""Deep knowledge dossier for Step 3 -- Data Modeling.

This module encodes the modeling decisions a strong data engineer makes *after*
requirements and pipeline shape are known: how to layer raw vs trusted vs serving
data (the medallion architecture), which schema pattern to draw (star / snowflake /
One Big Table), how to declare the grain, how to model dimensions and their history
(Slowly Changing Dimensions), and how to lay data out physically (partitioning,
clustering, bucketing) so it survives at TB+ scale.

It is intentionally tool- and cloud-agnostic: Delta, Iceberg, Spark, and Snowflake
appear only as representative examples of a category, never as the only valid
choice. ASCII only; "->" and "x" instead of arrows and crosses.
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
        name="Medallion architecture (Bronze / Silver / Gold)",
        summary=(
            "Three trust tiers. Each layer has a single, non-negotiable "
            "responsibility -- the discipline is keeping transformation logic in the "
            "layer where it belongs, not smearing it across all three."
        ),
        points=(
            "Bronze = raw, immutable, append-only landing of the source exactly as it "
            "arrived: JSON dumps, CDC change rows, Kafka payloads, CSV drops. No "
            "cleaning, no de-dup, no type coercion -- only structural metadata "
            "(ingest_ts, source_file, batch_id). It is the replay/audit safety net: "
            "if Silver logic has a bug you reprocess from Bronze, never from source.",
            "Silver = cleaned, de-duplicated, type-cast, conformed. Parse JSON to "
            "typed columns, drop/quarantine malformed rows, de-duplicate on the "
            "natural key, standardise units/codes/timezones, and resolve into "
            "conformed facts and dimensions at *row grain* (one row per business "
            "event / per entity version). This is the trusted, queryable core.",
            "Gold = pre-aggregated and pre-joined for consumption: BI marts, "
            "dashboard tables, feature tables, metric rollups. Joins and GROUP BYs "
            "are paid for once at build time so the read path is cheap. Gold is "
            "rebuildable from Silver and is allowed to be denormalized / shaped per "
            "consumer.",
            "The cardinal rule: cleaning belongs in Bronze->Silver, business "
            "aggregation belongs in Silver->Gold. Putting cleaning in Bronze destroys "
            "the immutable replay guarantee; pulling aggregation into Silver couples "
            "the trusted layer to one report's needs.",
            "Live dashboards and ML should source from Silver (cleaned, row-grain) "
            "for correctness and re-rootable cuts; Gold exists for parity and for hot, "
            "predictable read paths -- never aggregate straight from raw Bronze.",
            "Layers are logical, not physical: Bronze/Silver/Gold can be schemas, "
            "path prefixes, or catalogs. The boundary is about *guarantees* "
            "(immutability, typed, conformed, aggregated), not about storage tech.",
        ),
        examples=(
            "Bronze: raw_orders as landed JSON + ingest_ts; Silver: fct_orders typed, "
            "deduped on order_id, USD-normalized; Gold: daily_revenue_by_region "
            "pre-joined to dim_region and pre-summed.",
            "A bug in revenue logic is fixed by recomputing Silver+Gold from existing "
            "Bronze -- the upstream system is never re-queried.",
        ),
    ),
    Topic(
        name="Declare the grain first",
        summary=(
            "The grain is the precise meaning of one row in a table. Declaring it "
            "before drawing any columns is the single most important modeling "
            "decision -- every other choice flows from it."
        ),
        points=(
            "State the grain as one sentence: 'one row per order line', 'one row per "
            "account per day', 'one row per shipment from pick to delivery'. If you "
            "cannot say it in a sentence, the model is not ready to draw.",
            "Grain determines which measures are additive: you can only SUM a measure "
            "across dimensions if it is consistent with the declared grain. Mixing "
            "grains (order header + order line in one fact) silently double-counts.",
            "Grain fixes the table's primary key and therefore the join keys, the "
            "de-dup rule, and the upsert MERGE condition. Get it wrong and every "
            "downstream aggregate is wrong in a way that is hard to detect.",
            "Choose the *lowest* grain the requirements need (atomic transactions), "
            "not a pre-aggregated grain -- you can always roll up, but you can never "
            "recover detail you did not keep. Pre-aggregation is a Gold-layer choice, "
            "not a Silver-layer one.",
            "Each fact table holds exactly one grain. If two processes are measured at "
            "different grains, they are two fact tables, conformed through shared "
            "dimensions -- not one wide table.",
        ),
        examples=(
            "Grain 'one row per order line item' -> PK (order_id, line_no), measures "
            "quantity and extended_price are additive across product and date.",
            "A 'sales' fact that mixes header-level shipping_fee with line-level "
            "amounts double-counts shipping when summed by product.",
        ),
    ),
    Topic(
        name="Dimensional modeling (Kimball star schema)",
        summary=(
            "Organise the trusted layer as facts (the measurable events / numbers) "
            "surrounded by dimensions (the descriptive context you filter and group "
            "by). The result is the readable, BI-friendly star."
        ),
        points=(
            "Facts are mostly numeric and skinny: foreign keys to dimensions plus "
            "additive measures. Dimensions are wide and textual: the who/what/where/"
            "when/how attributes used in WHERE and GROUP BY.",
            "Facts are deep and grow with events (billions of rows); dimensions are "
            "shallow and grow with entities (thousands to millions). This asymmetry "
            "drives layout: partition/cluster the fact, broadcast-join the dimension.",
            "Measures come in three additivity classes: fully additive (sum across all "
            "dimensions, e.g. revenue), semi-additive (sum across some but not time, "
            "e.g. account balance), non-additive (ratios/percentages -- store the "
            "numerator and denominator, compute the ratio after aggregation).",
            "A star is one fact joined to its dimensions one hop out; it is "
            "deliberately denormalized inside each dimension for fast, simple joins. "
            "BI tools and analysts read stars natively.",
            "Dimensions carry surrogate keys so the fact never depends on a volatile "
            "business identifier, and so SCD history can be represented (multiple "
            "surrogate keys for one business entity over time).",
        ),
        examples=(
            "fct_sales (date_key, product_key, store_key, customer_key, quantity, "
            "amount) joined to dim_date, dim_product, dim_store, dim_customer.",
            "Conversion rate is stored as conversions + visits in the fact; the rate "
            "is computed as SUM(conversions)/SUM(visits) after grouping.",
        ),
    ),
    Topic(
        name="Fact table types",
        summary=(
            "Not all facts are transaction logs. Picking the right fact type matches "
            "the table to how the measured process actually behaves over time."
        ),
        points=(
            "Transaction fact: one row per event at the moment it happens (an order, a "
            "click, a payment). Append-only, the most common and most granular. Grain "
            "is the event.",
            "Periodic snapshot fact: one row per entity per regular interval (account "
            "balance per day, inventory per location per night). Captures state at a "
            "cadence; measures are typically semi-additive (not summable over time).",
            "Accumulating snapshot fact: one row per long-running process instance "
            "(an order from placed -> packed -> shipped -> delivered), with multiple "
            "date columns and milestone measures that get *updated in place* as the "
            "process advances. Good for lifecycle/latency analysis.",
            "Factless fact: a row records that an event/relationship occurred with no "
            "numeric measure (student attended class, promotion covered product). You "
            "count rows, or use it as a coverage/eligibility bridge.",
            "Accumulating snapshots are the exception to append-only facts: they "
            "require an upsert (MERGE) on the process-instance key because rows mutate "
            "as milestones land.",
        ),
        examples=(
            "Transaction: fct_payment (one row per payment). Periodic: "
            "fct_account_balance_daily (one row per account per day). Accumulating: "
            "fct_order_pipeline with order_date / ship_date / deliver_date columns "
            "filled in over time.",
        ),
    ),
    Topic(
        name="Dimension design patterns",
        summary=(
            "Dimensions carry the analytical vocabulary. A handful of named patterns "
            "cover almost every real situation -- knowing them avoids re-inventing "
            "schemas badly."
        ),
        points=(
            "Conformed dimension: one shared dimension (dim_date, dim_customer) used "
            "identically across multiple facts, so metrics from different processes "
            "can be compared and drilled with the same filters. Conformance is what "
            "makes an enterprise model coherent rather than a pile of marts.",
            "Role-playing dimension: one physical dimension referenced multiple times "
            "with different meanings (dim_date as order_date, ship_date, "
            "return_date). Implement once, expose as views/aliases per role.",
            "Degenerate dimension: a dimension attribute that lives on the fact with "
            "no separate table -- a high-cardinality identifier like order_id or "
            "invoice_no kept for drill-through/grouping but not worth a dimension.",
            "Junk dimension: collapse several low-cardinality flags/indicators "
            "(is_gift, channel, payment_type) into one small dimension of distinct "
            "combinations, so the fact carries one FK instead of many boolean columns.",
            "Mini-dimension: split fast-changing, frequently-queried attributes (e.g. "
            "customer income band, credit score band) out of a large slow dimension "
            "into a small banded dimension, so SCD2 churn does not explode the main "
            "dimension's row count.",
            "Outrigger: a dimension that references another dimension (dim_store -> "
            "dim_region). Use sparingly -- it is a controlled, shallow snowflake and "
            "trades a join for normalization.",
        ),
        examples=(
            "dim_date used as both order_date and ship_date roles in fct_orders.",
            "A junk dimension dim_order_flags with one row per distinct "
            "(channel x payment_type x is_gift) combination replaces three fact "
            "columns with one FK.",
        ),
    ),
    Topic(
        name="Slowly Changing Dimensions (SCD)",
        summary=(
            "How a dimension records changes to an entity's attributes over time. The "
            "choice per attribute is driven by one question: does anyone ever need to "
            "see history as it was at a past point in time?"
        ),
        points=(
            "Type 0 (retain original): never change the value once set (date_of_birth, "
            "original signup channel). The attribute is frozen by policy.",
            "Type 1 (overwrite): update in place, keep no history. Cheap and simple, "
            "but point-in-time correctness is lost -- past facts now appear under the "
            "new attribute value. Use only when history genuinely does not matter "
            "(typo fixes, current-state attributes).",
            "Type 2 (add new row): on change, expire the current row "
            "(valid_to / is_current=false) and insert a new row with a new surrogate "
            "key and valid_from. The fact's surrogate FK pins each event to the "
            "attribute value that was true *when the event happened*. This is the "
            "history-preserving workhorse.",
            "Type 3 (add new column): keep a limited 'previous_value' (and maybe "
            "'effective_date') column alongside current. Good for exactly one or two "
            "levels of remembered prior value (e.g. previous sales region) -- not "
            "full history.",
            "Type 4 (history table): keep the current dimension lean and push old "
            "versions into a separate history/mini-dimension table. Useful when the "
            "main dimension must stay small for hot joins but full history is still "
            "needed elsewhere.",
            "Type 6 (hybrid 1+2+3): Type 2 rows for full history, plus a Type 1 "
            "'current_value' attribute carried on every historical row (so you can "
            "report 'as-was' or 'as-is' without a self-join), plus a Type 3 prior "
            "column where useful. Named 6 = 1+2+3.",
            "SCD is decided per attribute, not per table: a dim_customer can have "
            "Type 0 dob, Type 1 email, and Type 2 address simultaneously.",
        ),
        examples=(
            "Customer moves region: SCD2 expires the old row (valid_to=move_date) and "
            "adds a new surrogate; revenue booked before the move stays attributed to "
            "the old region.",
            "Type 1 email correction overwrites in place -- no one queries historical "
            "email-as-of-date, so no history is kept.",
        ),
    ),
    Topic(
        name="Surrogate keys, natural keys, and idempotent upserts",
        summary=(
            "The fact must join to dimensions on a key that is stable, system-owned, "
            "and able to represent history. That key is almost always a surrogate, "
            "while the natural/business key is preserved as an attribute."
        ),
        points=(
            "Natural / business key: the identifier the source system uses "
            "(email, SSN, SKU, order_id from the OLTP). It can be reused, reformatted, "
            "or change ownership over time -- so it is a fragile join key.",
            "Surrogate key: a system-generated, meaningless, immutable key (sequence "
            "or hash) owned by the warehouse. It decouples the model from source "
            "volatility and is what makes SCD2 possible -- one business entity maps to "
            "many surrogate keys across its history.",
            "Keep the natural key as a column on the dimension for lineage and "
            "joining back to source; just do not use it as the fact's FK.",
            "For idempotent loads, derive a deterministic upsert key (hash of the "
            "natural key + grain columns) so re-running a batch MERGEs onto the same "
            "rows instead of inserting duplicates. Idempotent natural keys are what "
            "make backfills and retries safe.",
            "Late-arriving facts (fact for a dimension version not yet loaded) need an "
            "inferred/placeholder dimension member or a deferred resolution; "
            "early-arriving dimensions (attribute change arrives before the related "
            "fact) need the SCD2 effective-dating logic to pick the right version at "
            "join time.",
        ),
        examples=(
            "dim_product.product_key = surrogate; dim_product.sku = natural key kept "
            "as an attribute. fct_sales joins on product_key, so a re-issued SKU does "
            "not corrupt history.",
            "Upsert key = sha2(order_id || line_no); re-running the daily job MERGEs "
            "and inserts zero duplicates.",
        ),
    ),
    Topic(
        name="Normalization vs denormalization (and OBT)",
        summary=(
            "Normalization removes redundancy for write integrity; denormalization "
            "reintroduces redundancy for read speed. Analytics deliberately leans "
            "toward denormalization, with One Big Table as the extreme."
        ),
        points=(
            "1NF: atomic columns, no repeating groups/arrays-as-columns. 2NF: no "
            "partial dependency on part of a composite key. 3NF: no transitive "
            "dependency (non-key attribute depending on another non-key). 3NF "
            "minimizes update anomalies and is the OLTP default.",
            "OLTP source systems are normalized (3NF) to keep writes consistent; "
            "analytics layers denormalize because reads dominate and wide-shuffle "
            "joins are the expensive part of a query.",
            "Star schema is a moderate denormalization: dimensions are flattened "
            "internally, but facts and dimensions stay separate. Snowflake "
            "re-normalizes dimensions into sub-tables (more joins, less redundancy).",
            "One Big Table (OBT) is full denormalization: pre-join fact + all needed "
            "dimension attributes into one flat wide table so a dashboard query does "
            "zero joins. Reads are fast and predictable; the cost is a heavier build "
            "pipeline and redundant attribute storage, and SCD/attribute changes must "
            "be re-stamped across many rows.",
            "Pick the shape from the query pattern: ad-hoc, many-dimension slicing -> "
            "star; a fixed, hot, predictable dashboard scanned constantly -> OBT built "
            "in Gold from the conformed Silver star.",
        ),
        examples=(
            "Gold table daily_kpi_wide pre-joins fct_sales to product, store, and date "
            "attributes so the executive dashboard issues a single scan with no join.",
            "Snowflaking dim_product into dim_product + dim_category + dim_brand saves "
            "storage but adds two joins to every product slice.",
        ),
    ),
    Topic(
        name="Partitioning, clustering, and bucketing",
        summary=(
            "Physical layout decides how much data a query must read. Partition "
            "pruning, clustering, and bucketing all aim to turn full-table scans into "
            "reads of just the relevant bytes."
        ),
        points=(
            "Partitioning splits a table by a column's value into separate directories/"
            "manifests so the engine skips (prunes) partitions the WHERE clause cannot "
            "match. The classic and usually correct partition key is event_date, "
            "because almost every analytic query is time-bounded.",
            "Partition on a low-to-medium cardinality column you actually filter on. "
            "Over-partitioning (high-cardinality keys like user_id, or date+hour+"
            "region) shatters data into millions of tiny files -- the 'small-file "
            "explosion' -- which is slow to list and slow to scan.",
            "Range vs hash partitioning: range (by date) gives natural time pruning "
            "and easy partition-level backfill/overwrite; hash spreads load evenly to "
            "avoid hotspots but loses pruning by value. Date-range is the analytics "
            "default; hash is for write-distribution / skew control.",
            "Clustering / Z-order / liquid clustering co-locates rows by one or more "
            "columns *within* files and records min/max stats so the engine skips "
            "files (data skipping) without rigid physical partition directories. Reach "
            "for clustering when the column you filter on is too high-cardinality to "
            "partition by, or when you filter on several columns.",
            "Bucketing hashes a column into a fixed number of buckets/files so that "
            "two tables bucketed the same way can join without a shuffle (sort-merge "
            "bucket join). It optimizes joins, where partitioning optimizes scans.",
            "Liquid clustering decouples the clustering key from the physical "
            "partition layout, so you can change cluster keys later without rewriting "
            "partition structure -- useful when query patterns evolve.",
        ),
        examples=(
            "Partition fct_events by event_date; a 'last 7 days' query prunes to 7 "
            "partitions instead of scanning years.",
            "A high-cardinality customer_id is clustered (Z-order), not partitioned, so "
            "point lookups skip files via min/max stats without millions of dirs.",
        ),
    ),
    Topic(
        name="Modeling philosophies: Kimball vs Inmon vs Data Vault",
        summary=(
            "Three coherent schools for organising an enterprise warehouse. They "
            "differ on where conformance and integration happen, and on how much they "
            "optimize for auditability vs analyst ergonomics."
        ),
        points=(
            "Kimball (dimensional / bottom-up): build conformed star-schema marts per "
            "business process, integrated through a shared 'bus' of conformed "
            "dimensions. Fast to deliver, analyst-friendly, BI-native. The dominant "
            "approach for the serving/Gold layer.",
            "Inmon (top-down / corporate information factory): build a normalized (3NF) "
            "enterprise warehouse as the single integrated source of truth, then spin "
            "dimensional marts off it. Strong on integration and consistency; slower "
            "and heavier up front.",
            "Data Vault: an insert-only, highly auditable integration layer of Hubs "
            "(business keys), Links (relationships between keys), and Satellites "
            "(descriptive, time-stamped attributes). Built for traceability, parallel "
            "loading, and absorbing source change without refactoring -- then "
            "dimensional marts are built downstream for consumption.",
            "These are not mutually exclusive in practice: a common modern stack uses "
            "a Data Vault or 3NF integration layer (the 'Silver-ish' trusted core) and "
            "Kimball stars/OBT in Gold for serving.",
            "Pick by driver: time-to-dashboard and analyst usability -> Kimball; "
            "regulated, audit-heavy, many volatile sources -> Data Vault; a single "
            "tightly-governed enterprise truth -> Inmon.",
        ),
        examples=(
            "Data Vault: hub_customer (business key) + link_customer_order + "
            "sat_customer_address (effective-dated attributes) feed a Kimball "
            "dim_customer + fct_orders mart for BI.",
        ),
    ),
)


# ---------------------------------------------------------------------------
# Comparisons
# ---------------------------------------------------------------------------

_COMPARISONS: tuple[Comparison, ...] = (
    Comparison(
        question="Star schema vs Snowflake vs One Big Table (OBT)?",
        options=(
            Option(
                name="Star schema",
                when="General-purpose BI with ad-hoc, multi-dimension slicing.",
                pros=(
                    "Clean fact/dimension separation, easy to understand and govern.",
                    "Native to BI tools; flexible drill across many dimensions.",
                    "Dimensions reused (conformed) across many facts.",
                ),
                cons=(
                    "Every query pays for joins (shuffle cost at large scale).",
                    "Hot dashboards repeat the same joins on each refresh.",
                ),
            ),
            Option(
                name="Snowflake schema",
                when="Dimensions are large with normalized sub-hierarchies and "
                "storage/consistency matter more than join count.",
                pros=(
                    "Less attribute redundancy; cleaner hierarchy maintenance.",
                ),
                cons=(
                    "More joins per query -> slower scans for BI.",
                    "Harder for analysts; rarely worth it for read-heavy marts.",
                ),
            ),
            Option(
                name="One Big Table (OBT)",
                when="A fixed, hot, predictable dashboard scanned constantly.",
                pros=(
                    "Zero joins on read -> fast, predictable dashboard latency.",
                    "Simple for the consumer; great for a single dense scan.",
                ),
                cons=(
                    "Heavier build pipeline; redundant attribute storage.",
                    "Attribute/SCD changes must be re-stamped across many rows.",
                    "Inflexible if query patterns change.",
                ),
            ),
        ),
        rule_of_thumb=(
            "Model the trusted layer as a conformed star; materialize OBT in Gold only "
            "for the specific hot dashboards whose query shape is known and stable. "
            "Avoid snowflaking BI tables that need fast scans."
        ),
    ),
    Comparison(
        question="Which SCD type for this attribute -- Type 1 vs 2 vs 3 vs 6?",
        options=(
            Option(
                name="SCD Type 1 (overwrite)",
                when="Current state is all that matters; history is never queried "
                "point-in-time (typo fixes, mutable current attributes).",
                pros=(
                    "Simplest; dimension stays small.",
                ),
                cons=(
                    "Silently loses point-in-time correctness -- past facts re-attribute "
                    "to the new value.",
                ),
            ),
            Option(
                name="SCD Type 2 (add row)",
                when="History must be reconstructable as-of any past date.",
                pros=(
                    "Full history; facts pin to the value true at event time.",
                    "The auditable default for changing analytical attributes.",
                ),
                cons=(
                    "Dimension grows with every change; needs surrogate keys and "
                    "effective-dating logic.",
                ),
            ),
            Option(
                name="SCD Type 3 (add column)",
                when="Only one (or two) prior values are needed -- e.g. previous "
                "region for before/after comparison.",
                pros=(
                    "Cheap; no row growth.",
                ),
                cons=(
                    "Remembers only a fixed depth of history, not the full timeline.",
                ),
            ),
            Option(
                name="SCD Type 6 (hybrid 1+2+3)",
                when="You need full history AND a current-value view without a "
                "self-join.",
                pros=(
                    "Report as-was and as-is from the same rows.",
                ),
                cons=(
                    "Most complex load logic; current-value must be re-stamped on all "
                    "historical rows.",
                ),
            ),
        ),
        rule_of_thumb=(
            "If anyone ever asks 'what was it at the time?' -> Type 2. If only the "
            "current value matters -> Type 1. Decide per attribute, not per table."
        ),
    ),
    Comparison(
        question="Surrogate key vs natural/business key as the join key?",
        options=(
            Option(
                name="Surrogate key",
                when="Almost always, as the dimension PK and the fact FK.",
                pros=(
                    "Immutable and system-owned; survives source-key reuse/reformatting.",
                    "Enables SCD2 (one entity -> many keyed versions).",
                    "Narrow integer/hash key -> smaller, faster joins.",
                ),
                cons=(
                    "Requires a key-assignment step and lineage back to the natural key.",
                ),
            ),
            Option(
                name="Natural / business key",
                when="As a stored attribute for lineage, and as the basis of the "
                "idempotent upsert key -- but not as the fact's join FK.",
                pros=(
                    "Human-meaningful; ties straight back to source.",
                ),
                cons=(
                    "Can change/reformat/be reused -> corrupts joins and history if used "
                    "as the FK.",
                ),
            ),
        ),
        rule_of_thumb=(
            "Join on surrogate keys; keep the natural key as an attribute and use it "
            "(hashed with grain) only to make upserts idempotent."
        ),
    ),
    Comparison(
        question="Normalized (3NF) vs denormalized for this layer?",
        options=(
            Option(
                name="Normalized (3NF)",
                when="Write-heavy OLTP sources and integration layers where update "
                "integrity dominates.",
                pros=(
                    "No redundancy; no update anomalies; single place to change a value.",
                ),
                cons=(
                    "Many joins on read -> expensive for analytic scans.",
                ),
            ),
            Option(
                name="Denormalized (star / OBT)",
                when="Read-heavy serving layers (BI, dashboards, ML features).",
                pros=(
                    "Fewer/zero joins on read -> fast analytic queries.",
                    "Matches how analysts and BI tools consume data.",
                ),
                cons=(
                    "Redundant storage; attribute changes must be propagated to many "
                    "rows.",
                ),
            ),
        ),
        rule_of_thumb=(
            "Normalize where you write (sources/integration), denormalize where you "
            "read (Gold serving). Reads dominate analytics, so the warehouse leans "
            "denormalized."
        ),
    ),
    Comparison(
        question="Kimball vs Inmon vs Data Vault as the modeling philosophy?",
        options=(
            Option(
                name="Kimball (dimensional)",
                when="Fast time-to-dashboard, analyst-friendly serving marts.",
                pros=(
                    "Quick delivery; BI-native; conformed dimensions for comparability.",
                ),
                cons=(
                    "Less suited as a raw, fully-auditable integration layer for many "
                    "volatile sources.",
                ),
            ),
            Option(
                name="Inmon (3NF enterprise warehouse)",
                when="One tightly-governed, integrated enterprise source of truth, "
                "marts derived from it.",
                pros=(
                    "Strong integration and consistency across the enterprise.",
                ),
                cons=(
                    "Heavy and slow up front; marts still needed for consumption.",
                ),
            ),
            Option(
                name="Data Vault (hubs/links/satellites)",
                when="Regulated, audit-heavy environments with many changing sources.",
                pros=(
                    "Insert-only auditability; absorbs source change without refactor; "
                    "parallel-loadable.",
                ),
                cons=(
                    "Not consumable directly -> needs downstream dimensional marts; "
                    "more tables/complexity.",
                ),
            ),
        ),
        rule_of_thumb=(
            "Common modern stack: Data Vault or 3NF integration core for "
            "auditability, Kimball stars/OBT on top for serving. Pick the core by "
            "audit/governance pressure, the serving by analyst ergonomics."
        ),
    ),
    Comparison(
        question="Partition-by-date vs clustering / liquid clustering?",
        options=(
            Option(
                name="Partition by event_date",
                when="Queries are time-bounded and you backfill/overwrite by date "
                "(the common case).",
                pros=(
                    "Strong, predictable partition pruning on the time filter.",
                    "Partition-level atomic overwrite makes backfills clean.",
                ),
                cons=(
                    "Wrong/high-cardinality key -> small-file explosion and over-"
                    "partitioning.",
                    "Physical layout is rigid once chosen.",
                ),
            ),
            Option(
                name="Clustering / Z-order / liquid clustering",
                when="Filter columns are high-cardinality or multiple, or query "
                "patterns evolve.",
                pros=(
                    "Data skipping via min/max stats without rigid partition dirs.",
                    "Liquid clustering lets you change cluster keys later.",
                ),
                cons=(
                    "Needs periodic optimize/compaction to stay effective.",
                    "Less obvious pruning guarantee than a partition column.",
                ),
            ),
            Option(
                name="Bucketing",
                when="The dominant cost is a repeated large join on a high-cardinality "
                "key.",
                pros=(
                    "Co-located buckets enable shuffle-free sort-merge bucket joins.",
                ),
                cons=(
                    "Fixed bucket count; benefits only when both sides share it.",
                ),
            ),
        ),
        rule_of_thumb=(
            "Default: partition by a low-to-medium-cardinality date you filter on; "
            "switch to clustering when partitioning would over-shard; bucket only to "
            "kill a specific repeated join shuffle."
        ),
    ),
)


# ---------------------------------------------------------------------------
# Heuristics
# ---------------------------------------------------------------------------

_HEURISTICS: tuple[Heuristic, ...] = (
    Heuristic(
        name="Declare the grain before drawing tables",
        rule="State 'one row = ____' in a sentence first; the PK, join keys, de-dup, "
        "and additivity all follow from it. If you cannot state it, do not model yet.",
        example="'One row per order line item' -> PK (order_id, line_no), measures "
        "additive across product and date.",
    ),
    Heuristic(
        name="Target file size ~128MB-1GB",
        rule="Aim for files roughly 128MB to 1GB after compaction. Many files under "
        "~128MB (tiny-file problem) waste listing/scan time; multi-GB files hurt "
        "parallelism and pruning.",
        example="A partition holding 50,000 x 2MB files should be OPTIMIZE/compacted "
        "into a few ~512MB files.",
    ),
    Heuristic(
        name="Keep partitions meaningfully large",
        rule="A partition should hold enough data to be worth a directory -- avoid "
        "partitions of only a few MB. Rule of thumb: do not partition so finely that "
        "partitions fall well under ~1GB or counts run into the hundreds of thousands.",
        example="Partition fct_events by date (not date+hour+region) so each "
        "partition is GBs, not MBs.",
    ),
    Heuristic(
        name="Partition on a low-to-medium-cardinality column you filter on",
        rule="The partition key must (a) appear in WHERE clauses and (b) have low-to-"
        "medium cardinality. event_date is the default winner; user_id/uuid is the "
        "classic wrong choice.",
        example="Partition by event_date (365 values/yr), never by user_id (millions).",
    ),
    Heuristic(
        name="Point-in-time history -> SCD2",
        rule="If a dimension attribute is ever queried 'as of' a past date, it must be "
        "SCD Type 2; otherwise Type 1 (overwrite) is fine. Decide per attribute.",
        example="Customer address (used for as-of-date revenue attribution) -> SCD2; "
        "email typo fix -> SCD1.",
    ),
    Heuristic(
        name="Cluster when partitioning would over-shard",
        rule="When the column you filter on is too high-cardinality to partition by "
        "(or you filter on several columns), cluster/Z-order/liquid-cluster instead "
        "of partitioning, and rely on file-level min/max data skipping.",
        example="Filter on customer_id and product_id -> cluster on both rather than "
        "creating millions of partitions.",
    ),
    Heuristic(
        name="One grain per fact table",
        rule="Never mix grains in a fact. If two measures live at different grains "
        "(header vs line), split into two facts conformed by shared dimensions.",
        example="shipping_fee (per order) and amount (per line) belong in separate "
        "facts, not one wide table.",
    ),
    Heuristic(
        name="Make the upsert key deterministic and idempotent",
        rule="Derive the MERGE key as a hash of natural key + grain columns so re-runs "
        "match existing rows. Idempotent keys make retries and backfills safe.",
        example="upsert_key = sha2(order_id || '|' || line_no) -> re-running the batch "
        "inserts zero duplicates.",
    ),
    Heuristic(
        name="Compute ratios after aggregation",
        rule="Store the numerator and denominator as additive measures; never store a "
        "pre-computed ratio in a fact, because ratios are non-additive.",
        example="Store conversions and visits; report SUM(conversions)/SUM(visits), "
        "not AVG(rate).",
    ),
    Heuristic(
        name="OBT only for hot, stable query shapes",
        rule="Build One Big Table in Gold only for dashboards whose query pattern is "
        "fixed and frequently scanned; keep everything else as a conformed star so "
        "cuts stay flexible.",
        example="Executive daily KPI page -> OBT; the analyst exploration sandbox -> "
        "star.",
    ),
)


# ---------------------------------------------------------------------------
# Pitfalls
# ---------------------------------------------------------------------------

_PITFALLS: tuple[str, ...] = (
    "Drawing tables before declaring the grain -> mixed grains, double-counting, and "
    "a PK that does not mean anything.",
    "Over-partitioning (partitioning by a high-cardinality key like user_id, or by "
    "date+hour+region) -> the small-file explosion: millions of tiny files that are "
    "slow to list and scan.",
    "Using SCD Type 1 where history was needed -> silent loss of point-in-time "
    "correctness; past facts re-attribute to the new value and no one notices.",
    "Putting cleaning/de-dup/type-cast logic in Bronze -> destroys the immutable "
    "replay/audit guarantee that Bronze exists to provide.",
    "Treating Bronze as mutable (updating/overwriting landed rows) -> you can no "
    "longer reprocess Silver from a faithful copy of the source.",
    "Using a volatile natural/business key as the fact's join FK -> reused or "
    "reformatted source keys silently corrupt joins and history.",
    "Snowflaking BI tables that need fast scans -> extra joins per query for marginal "
    "storage savings; rarely worth it on read-heavy marts.",
    "Storing a pre-computed ratio/percentage as a fact measure -> non-additive; "
    "summing it gives nonsense. Keep numerator and denominator instead.",
    "Aggregating dashboards straight from raw Bronze -> wrong numbers on dirty, "
    "duplicated, untyped data; source from cleaned Silver.",
    "Forgetting late/early-arriving dimension handling -> facts join to a missing or "
    "wrong-version dimension member and quietly drop or misattribute rows.",
)


# ---------------------------------------------------------------------------
# What breaks at scale
# ---------------------------------------------------------------------------

_AT_SCALE: tuple[str, ...] = (
    "At TB+, join shuffle is the dominant cost: a star with several large-fact joins "
    "moves enormous data across the network. This is the core motivation for "
    "materializing OBT/Gold tables that pre-pay the joins once.",
    "Partition pruning shifts from 'nice optimization' to 'mandatory': without a "
    "filterable partition (or clustering) key, every query full-scans the table and "
    "cost/latency become unacceptable.",
    "SCD2 dimensions grow without bound -- a frequently-changing attribute on a large "
    "entity set can produce more dimension rows than facts. Mini-dimensions and "
    "Type 4 history tables exist to contain this.",
    "Dimension explosion: collapsing many flags into the fact (instead of a junk "
    "dimension) or carrying high-churn attributes in a big dimension blows up row "
    "counts and join cost.",
    "Streaming/CDC ingestion creates a constant tiny-file stream; without scheduled "
    "compaction (OPTIMIZE) the file count grows until listing and planning dominate "
    "query time.",
    "Skewed partitions / hot keys (one date or one tenant holding most rows) "
    "bottleneck on a few tasks while others idle -> salting or hash distribution is "
    "needed to rebalance.",
    "Wide-shuffle joins on hot dashboards repeated every refresh waste compute; the "
    "fix is to denormalize that exact query path into Gold, not to scale the cluster.",
    "Accumulating-snapshot and Type 2 upserts (MERGE) get expensive as the target "
    "grows -- they must prune to the affected partitions, or the MERGE rescans the "
    "whole table each run.",
)


# ---------------------------------------------------------------------------
# Interview signals
# ---------------------------------------------------------------------------

_INTERVIEW_SIGNALS: tuple[str, ...] = (
    "Declares the grain in one sentence before drawing any table, and ties PK, "
    "de-dup, and additivity back to it.",
    "Layers Bronze/Silver/Gold with the correct responsibility per layer (raw "
    "immutable / cleaned conformed row-grain / pre-aggregated serving) and resists "
    "smearing cleaning into Bronze or aggregation into Silver.",
    "Picks star vs OBT from the query pattern -- conformed star for flexible slicing, "
    "OBT in Gold only for a hot, predictable dashboard path.",
    "Chooses an SCD type per attribute by whether history is queried point-in-time "
    "(Type 2) or not (Type 1), rather than applying one type to the whole table.",
    "Partitions on a low-to-medium-cardinality column that appears in the WHERE "
    "clause (usually event_date) and explicitly avoids over-partitioning.",
    "Mentions conformed dimensions as what makes metrics comparable and drillable "
    "across multiple facts.",
    "Uses surrogate keys for fact FKs while keeping the natural key as an attribute, "
    "and explains why (source-key volatility, SCD2).",
    "Reaches for clustering / liquid clustering / bucketing when partitioning would "
    "over-shard or to remove a repeated join shuffle -- not as a reflex.",
    "Names the right fact type for the process (transaction / periodic snapshot / "
    "accumulating snapshot / factless) instead of defaulting to a transaction log.",
    "Makes loads idempotent with a deterministic upsert key so retries and backfills "
    "do not duplicate, and accounts for late/early-arriving dimensions.",
)


# ---------------------------------------------------------------------------
# Export
# ---------------------------------------------------------------------------

DEEP = DeepStep(
    step_key="modeling",
    title="Data Modeling",
    overview=(
        "Modeling is where raw, trustworthy, and serving data get separated, and "
        "where the schema is justified under scale -- not just drawn. The work: layer "
        "data with the medallion architecture (Bronze raw/immutable, Silver cleaned "
        "and conformed at row grain, Gold pre-aggregated for BI); declare the grain "
        "before anything else; shape the trusted layer as a conformed Kimball star of "
        "facts and dimensions, materializing One Big Table only for hot, predictable "
        "dashboards; model each dimension's history with the right Slowly Changing "
        "Dimension type per attribute; join on stable surrogate keys with idempotent "
        "upserts; and lay data out physically (partition by a filtered date column, "
        "cluster/bucket where partitioning would over-shard) so joins and scans "
        "survive at TB+ scale."
    ),
    topics=_TOPICS,
    comparisons=_COMPARISONS,
    heuristics=_HEURISTICS,
    pitfalls=_PITFALLS,
    at_scale=_AT_SCALE,
    interview_signals=_INTERVIEW_SIGNALS,
)


__all__ = ["DEEP"]
