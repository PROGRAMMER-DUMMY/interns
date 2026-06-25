"""Deep dossier for step 5 -- Data Quality & Observability.

Quality is not a final "validate then ship" stage; it is a set of contracts and
checks pushed as far left as possible (at the producer boundary) plus continuous
observability over the running pipeline. The base framework names the five DQ
dimensions and data contracts; this module goes deep: concrete checks per
dimension, contract compatibility modes and enforcement points, the categories of
validation frameworks, the observability pillars, anomaly detection, lineage,
data SLOs/error budgets, and failure handling (quarantine, dead-letter, circuit
breakers, fail-open vs fail-closed).

All content stays tool- and cloud-agnostic: Great Expectations, dbt tests, Deequ,
DataDog, CloudWatch, Confluent Schema Registry are named only as examples of a
category, never as the only valid choice. ASCII only; use "->", "x", ">=".
"""

from __future__ import annotations

from minus_dataops.depth._schema import (
    Comparison, DeepStep, Heuristic, Option, Topic,
)


DEEP = DeepStep(
    step_key="quality",
    title="Data Quality & Observability",
    overview=(
        "Prevent, detect, and handle corrupted data and failing infrastructure "
        "before it reaches consumers. Two halves work together: (1) data quality "
        "-- assertions over the data itself across the five dimensions "
        "(completeness, accuracy, consistency, freshness, uniqueness) plus "
        "validity, timeliness, and referential integrity; enforced through data "
        "contracts at the producer boundary so bad data is blocked, not silently "
        "cleaned downstream ('shift quality left'). (2) Observability -- "
        "continuous monitoring of the pipeline over the five pillars (freshness, "
        "volume, schema, distribution, lineage), anomaly detection on those "
        "signals, lineage for root-cause and impact analysis, and data SLOs with "
        "error budgets and tiered alerting. Bad data is quarantined to a "
        "dead-letter table and a circuit breaker halts downstream so one bad "
        "batch never poisons Gold. The goal is to minimize 'data downtime' -- the "
        "time data is missing, wrong, or stale."
    ),
    topics=(
        Topic(
            name="Completeness",
            summary=(
                "Is all the data that should be present actually present -- no "
                "missing rows, no missing values in required fields?"
            ),
            points=(
                "Row-count / volume checks: alert when today's row count drops "
                ">30% (or rises >X%) day-over-day vs a rolling baseline; a sudden "
                "drop usually means an upstream job partially failed or a source "
                "stopped emitting.",
                "Null-rate checks per column: required fields should be non-null "
                "100%; track null rate against a baseline and alert on spikes "
                "(e.g. customer_id null rate jumps 0% -> 5%).",
                "Empty-partition / arrival checks: expect N files or one partition "
                "per source per day; a missing partition is a completeness failure "
                "even if the rows that did arrive look fine.",
                "Coverage / cardinality checks: expect ~all stores or regions to "
                "report each day; a drop in distinct dimension values (e.g. 50 "
                "states -> 41) signals a partial source outage.",
                "Distinguish 'missing because absent' from 'missing because late' "
                "-- a freshness check catches the latter; completeness alone can "
                "false-alarm on slow sources.",
            ),
            examples=(
                "expect_table_row_count_to_be_between(min=0.7*baseline, "
                "max=1.3*baseline)",
                "WHERE customer_id IS NULL -> count must be 0 for a required key",
            ),
        ),
        Topic(
            name="Accuracy",
            summary=(
                "Is each value correct and plausible per business rules -- "
                "cross-field logic, allowed sets, and numeric ranges?"
            ),
            points=(
                "Cross-field business rules: delivery_ts >= order_ts; "
                "ship_ts <= delivery_ts; end_date >= start_date. These catch "
                "logically impossible rows that pass type checks.",
                "Allowed-set / membership checks: status IN ('new','paid', "
                "'shipped','cancelled'); currency IN known ISO codes; a new "
                "unexpected value is either a bug or an undocumented enum change.",
                "Range / bounds checks: 0 <= discount_pct <= 100; age BETWEEN 0 "
                "AND 120; amount >= 0. Out-of-range values often indicate "
                "unit-of-measure or encoding errors.",
                "Reasonableness checks against external truth: compute a metric "
                "two ways and compare (sum of line items == order total) within a "
                "tolerance.",
                "Accuracy is the hardest dimension to test automatically because "
                "'correct' often requires a source of truth; encode whatever "
                "business rules exist as assertions and reconcile the rest.",
            ),
            examples=(
                "CHECK (delivery_ts >= order_ts) -- no delivery before order",
                "expect_column_values_to_be_in_set('status', ALLOWED_STATUSES)",
            ),
        ),
        Topic(
            name="Consistency",
            summary=(
                "Does the data agree with itself and with other systems -- "
                "cross-system reconciliation and referential integrity?"
            ),
            points=(
                "Cross-system reconciliation / control totals: lake revenue for a "
                "day must equal the payment-processor revenue within tolerance; a "
                "mismatch means rows were dropped, double-counted, or "
                "mis-converted somewhere in the pipeline.",
                "Referential integrity: every fact.customer_id must exist in "
                "dim_customer; orphan foreign keys break joins and silently drop "
                "rows in downstream aggregates.",
                "Cross-table agreement: aggregate in Silver must equal the "
                "pre-aggregated Gold value for the same grain (parity check) -- a "
                "drift means a transformation diverged.",
                "Definition consistency: the same metric computed in two pipelines "
                "(e.g. 'active user') must use the same logic; inconsistent "
                "definitions are a governance problem surfaced as a reconciliation "
                "gap.",
                "Run reconciliation on a stable grain (day x source) so a single "
                "late record does not trip a daily total.",
            ),
            examples=(
                "ABS(lake_revenue - processor_revenue) / processor_revenue "
                "<= 0.001",
                "SELECT f.customer_id FROM fact f LEFT JOIN dim d USING "
                "(customer_id) WHERE d.customer_id IS NULL -> must be empty",
            ),
        ),
        Topic(
            name="Freshness and Timeliness",
            summary=(
                "Is the data recent enough relative to its SLA -- and did it "
                "arrive on time?"
            ),
            points=(
                "Freshness check: now() - max(event_ts) <= SLA (e.g. <= 60 min "
                "for a near-real-time table, <= 26 h for a daily table); a "
                "breach means the source or the load is stalled.",
                "Distinguish event-time freshness (max business timestamp) from "
                "load-time freshness (max ingestion timestamp); a backlog can keep "
                "load fresh while event-time falls behind.",
                "Timeliness = did the table land within its scheduled window; a "
                "table that is correct but two hours late still breaks downstream "
                "SLAs and dashboards.",
                "Freshness is the single most common data-incident signal -- a "
                "stale table looks 'fine' (no errors) yet serves yesterday's "
                "numbers; monitor it explicitly, do not infer it.",
                "Set SLA per table from its consumer's need, not globally; a "
                "fraud table needs minutes, a monthly finance mart needs a day.",
            ),
            examples=(
                "ASSERT now() - max(event_ts) <= INTERVAL '60' MINUTE",
                "alert if table not updated by 06:00 (scheduled window breach)",
            ),
        ),
        Topic(
            name="Uniqueness",
            summary=(
                "Are there unexpected duplicate records -- primary-key and "
                "dedup checks?"
            ),
            points=(
                "Primary-key uniqueness: count(*) == count(distinct pk); a "
                "duplicate key inflates every downstream sum and breaks MERGE "
                "upserts.",
                "Natural-key / composite-key checks where no surrogate exists "
                "(e.g. (order_id, line_no) unique); at-least-once delivery from "
                "streaming makes duplicates the default, not the exception.",
                "Dedup window for streams: duplicates can arrive seconds or hours "
                "apart on retries; dedup on a key within a bounded window rather "
                "than assuming exactly-once.",
                "Fuzzy / entity-resolution uniqueness is a harder variant ('John "
                "Smith' vs 'J. Smith') -- usually a Silver-layer concern, not a "
                "hard assertion.",
                "Uniqueness on critical tables warrants a full scan, not a sample "
                "-- a single duplicate that a sample misses still corrupts "
                "totals.",
            ),
            examples=(
                "expect_compound_columns_to_be_unique(['order_id','line_no'])",
                "SELECT pk, count(*) FROM t GROUP BY pk HAVING count(*) > 1",
            ),
        ),
        Topic(
            name="Validity and Integrity",
            summary=(
                "Beyond the five headline dimensions: format/type conformance "
                "(validity) and referential/structural integrity."
            ),
            points=(
                "Validity / conformance: values match the declared type and "
                "format -- email matches a pattern, dates parse, numbers are not "
                "strings, JSON conforms to a schema. Type drift (int -> string) "
                "is a frequent silent breaker.",
                "Schema conformance: required columns present, types unchanged, no "
                "extra columns sneaking through; this overlaps with the contract "
                "and is the cheapest check to run first.",
                "Integrity: referential integrity (FK -> PK), structural "
                "integrity (no truncated rows, consistent column count), and "
                "uniqueness together guarantee the data forms a coherent model.",
                "Validity is usually checkable purely from the data + schema "
                "(no source of truth needed), so it is cheap and belongs at the "
                "boundary; accuracy and consistency are more expensive and often "
                "need reconciliation.",
                "Order checks cheap -> expensive: schema/type -> null/range -> "
                "uniqueness -> cross-system reconciliation; fail fast on the cheap "
                "ones before paying for the dear ones.",
            ),
            examples=(
                "expect_column_values_to_match_regex('email', EMAIL_RE)",
                "expect_column_values_to_be_of_type('amount', 'DOUBLE')",
            ),
        ),
        Topic(
            name="Data Contracts",
            summary=(
                "A formal, versioned agreement between data producers and "
                "consumers, enforced at the boundary before data enters the lake."
            ),
            points=(
                "A contract pins schema (fields, types, nullability), semantics "
                "(units, allowed values), and SLAs (freshness, volume) so a "
                "producer cannot silently break a consumer; it is the API "
                "contract for data.",
                "Enforcement point matters: validate at the boundary -- block the "
                "bad write before it lands in Bronze/the lake -- rather than "
                "discovering the break three layers downstream. This is 'shift "
                "quality left'.",
                "A schema registry (e.g. Avro subjects in a registry) can reject "
                "an incompatible producer schema at publish time; the registry "
                "enforces the compatibility mode you configure.",
                "Compatibility modes define what schema changes are allowed: "
                "BACKWARD = new schema can read old data (safe to add optional / "
                "drop fields, lets consumers upgrade first); FORWARD = old schema "
                "can read new data (lets producers upgrade first); FULL = both "
                "(only fully safe changes); NONE = no checks (anything goes -- "
                "dangerous).",
                "Contracts are organizational as much as technical: they make the "
                "producer team own quality, turning a vague 'someone changed the "
                "data' into a versioned, reviewable change with a deprecation "
                "path.",
            ),
            examples=(
                "registry compatibility=BACKWARD -> adding an optional field is "
                "allowed; renaming a field is rejected at publish",
                "boundary check: reject the batch if required columns missing or "
                "types changed, before it is committed to Bronze",
            ),
        ),
        Topic(
            name="Validation Frameworks",
            summary=(
                "Three categories of tooling that run the assertions -- "
                "described by what they do, not by vendor."
            ),
            points=(
                "Declarative expectation suites (e.g. Great Expectations): you "
                "declare expectations ('column X not null', 'values in set') as "
                "data; the suite runs them, produces a pass/fail report and data "
                "docs, and can gate a pipeline. Best for explicit, reviewable "
                "assertions decoupled from transform code.",
                "Transformation tests (e.g. dbt tests): tests live next to the "
                "SQL models -- generic tests (unique, not_null, "
                "accepted_values, relationships) plus custom singular tests -- "
                "and run as part of the build, so quality is gated where the "
                "model is defined.",
                "JVM / Spark profilers (e.g. Deequ, built on Spark): compute "
                "column statistics and constraints at scale on a cluster, and can "
                "suggest constraints from a profile; best when the data is too "
                "large to validate on a single node.",
                "These categories overlap and compose: a registry enforces "
                "schema at the boundary, an expectation suite or dbt tests assert "
                "values inside the pipeline, and a Spark profiler does the heavy "
                "distribution work at TB scale.",
                "Whatever the tool, the assertion should fail the run (or "
                "quarantine the batch), not just log -- a check nobody acts on is "
                "decoration.",
            ),
            examples=(
                "declarative: an expectation suite gates the load and emits a "
                "pass/fail report",
                "transform test: dbt 'unique' + 'relationships' tests run in the "
                "model build",
            ),
        ),
        Topic(
            name="Pipeline Observability",
            summary=(
                "Continuous monitoring of the running pipeline over five pillars, "
                "so you detect 'data downtime' fast."
            ),
            points=(
                "Freshness: is each table updating on schedule (now - max ts vs "
                "SLA)? The most common incident signal.",
                "Volume: row counts / byte counts per load vs baseline -- catches "
                "partial loads, dropped sources, and runaway duplication.",
                "Schema: track schema changes over time (added/dropped/retyped "
                "columns); schema drift is the most frequent breaker of "
                "downstream consumers.",
                "Distribution / values: track per-column stats (null rate, "
                "min/max, mean, cardinality, category mix) vs baseline -- catches "
                "the data that is present, typed correctly, but quietly wrong "
                "(e.g. all amounts suddenly in cents).",
                "Lineage: table- and column-level dependency graph that ties the "
                "other four pillars together for root-cause ('what upstream "
                "change caused this') and impact ('what breaks if I change "
                "this').",
                "Distinguish pipeline/system observability (DAG run times, task "
                "failures, cluster CPU/memory via the orchestrator or DataDog / "
                "CloudWatch) from data observability (the five pillars over the "
                "data itself) -- you need both.",
            ),
            examples=(
                "five pillars: freshness, volume, schema, distribution, lineage",
                "system metrics: DAG duration, task retries, cluster utilization "
                "via orchestrator-native or DataDog / CloudWatch",
            ),
        ),
        Topic(
            name="Anomaly Detection",
            summary=(
                "Automatically flag deviations from a learned baseline across the "
                "observability signals instead of hand-coding every threshold."
            ),
            points=(
                "Volume anomalies: today's row count deviates from a rolling "
                "baseline / seasonality (weekday vs weekend) beyond a band -- "
                "catches partial and duplicate loads.",
                "Freshness / staleness anomalies: the gap between expected and "
                "actual update time exceeds the SLA or the historical update "
                "cadence.",
                "Schema drift: a column was added, dropped, or retyped vs the "
                "last run -- detected structurally, no statistics needed.",
                "Distribution / statistical drift: mean, percentiles, "
                "cardinality, or category mix shifts; tests like "
                "Kolmogorov-Smirnov or population-stability-index quantify how far "
                "a distribution moved from baseline.",
                "Null-rate spikes: a column's null rate jumps above its baseline "
                "band -- often the first sign an upstream field was renamed or "
                "stopped populating.",
                "Baselines must account for seasonality and trend or they "
                "false-alarm; static thresholds are simpler and more debuggable, "
                "learned baselines scale to thousands of tables but need warm-up "
                "history.",
            ),
            examples=(
                "PSI > 0.2 between today's distribution and the 30-day baseline "
                "-> alert on drift",
                "row_count outside mean +/- 3*stddev of the trailing 28 days",
            ),
        ),
        Topic(
            name="Data Lineage",
            summary=(
                "The dependency graph of how data flows, at table and column "
                "level -- the backbone of root-cause and impact analysis."
            ),
            points=(
                "Table-level lineage shows which sources feed which tables; "
                "column-level lineage shows which input columns produce which "
                "output column -- the finer grain pinpoints exactly what a change "
                "affects.",
                "Root-cause analysis: when Gold is wrong, walk lineage upstream to "
                "find the first table where the anomaly appears; without lineage "
                "this is hours of manual tracing across jobs.",
                "Impact analysis: before changing a column or schema, walk lineage "
                "downstream to find every dashboard, model, and table that breaks "
                "-- turns a risky change into a reviewed one.",
                "Lineage can be parsed from SQL/transform code (static) or "
                "captured from query/run logs (runtime); runtime catches dynamic "
                "and cross-system flows that static parsing misses.",
                "Tie lineage to the observability pillars so an alert links "
                "directly to upstream and downstream context, collapsing "
                "mean-time-to-resolution.",
            ),
            examples=(
                "column-level: gold.revenue <- silver.order_total <- "
                "bronze.amount x bronze.fx_rate",
                "impact query: 'what consumes dim_customer.email?' before "
                "dropping the column",
            ),
        ),
        Topic(
            name="SLOs, Alerting and Failure Handling",
            summary=(
                "Run data like a production service: SLAs/SLOs with error "
                "budgets, tiered alerting, quarantine, dead-letter, and circuit "
                "breakers."
            ),
            points=(
                "Data SLAs/SLOs: per-table targets (e.g. freshness <= 1 h for "
                "99% of days, completeness >= 99.9%); an error budget is the "
                "allowed miss (the 1%), spent on incidents and burn-tracked over "
                "time.",
                "Severity tiers prevent alert fatigue: SEV1 page on-call (a "
                "consumer-facing table is stale/wrong now); SEV2 file a ticket "
                "(degraded but tolerable); SEV3 log/dashboard (informational). "
                "Route by consumer impact, not by how easy the check was to "
                "write.",
                "Quarantine / dead-letter tables: rows that fail validation are "
                "routed to a side table with the failure reason instead of being "
                "dropped or blocking everything; good rows flow on, bad rows are "
                "triaged and replayed.",
                "Circuit breaker: if a check fails badly (e.g. >X% of rows "
                "invalid, or a reconciliation gap), halt the run so the bad batch "
                "does not propagate into Silver/Gold; downstream jobs stay on "
                "yesterday's good data rather than today's poison.",
                "Fail-closed (block on failure) protects critical/financial "
                "tables; fail-open (flag and continue) keeps availability for "
                "tables where stale-but-flowing beats stopped -- choose per table "
                "from consumer risk.",
                "'Data downtime' is the metric to minimize: the wall-clock time "
                "data is missing, erroneous, or stale; observability + fast "
                "alerting + lineage all exist to shrink mean-time-to-detect and "
                "mean-time-to-resolve.",
            ),
            examples=(
                "SLO: freshness <= 60 min on 99% of days; error budget = 1% of "
                "days/quarter",
                "bad rows -> dead_letter table with reason; if invalid_rate > 5% "
                "trip the breaker and halt the load",
            ),
        ),
    ),
    comparisons=(
        Comparison(
            question=(
                "Enforce quality on write (schema-on-write) or validate on read "
                "(schema-on-read)?"
            ),
            options=(
                Option(
                    name="Enforce-on-write (block bad data at the boundary)",
                    when=(
                        "Critical / consumer-facing data where a bad write is "
                        "expensive; producers can be held to a contract."
                    ),
                    pros=(
                        "Bad data never lands -> downstream is trustworthy",
                        "Failure surfaces at the source, where it is cheapest to "
                        "fix",
                        "Encodes the contract; producer owns quality",
                    ),
                    cons=(
                        "Can block ingestion / lose data if too strict",
                        "Requires producer cooperation and an agreed schema",
                        "Less flexible to fast-evolving sources",
                    ),
                ),
                Option(
                    name="Validate-on-read (tolerate, then check)",
                    when=(
                        "Raw exploratory / Bronze landing where capturing "
                        "everything matters more than upfront conformance."
                    ),
                    pros=(
                        "Never loses data; captures malformed input for replay",
                        "Tolerates schema evolution and messy sources",
                        "Flexible for unknown / fast-changing schemas",
                    ),
                    cons=(
                        "Bad data can spread before anyone checks",
                        "Pushes the cost of cleaning downstream",
                        "Easy to forget the 'then check' half",
                    ),
                ),
            ),
            rule_of_thumb=(
                "Tolerant at Bronze (land everything, validate on read), strict "
                "at the Silver boundary (enforce on write); never let unvalidated "
                "data reach Gold."
            ),
        ),
        Comparison(
            question="Which contract compatibility mode for schema changes?",
            options=(
                Option(
                    name="BACKWARD compatible",
                    when=(
                        "Default for most pipelines -- consumers should keep "
                        "reading old data after a schema change."
                    ),
                    pros=(
                        "Consumers upgrade on their own schedule",
                        "Allows adding optional fields and dropping fields safely",
                        "Most common registry default",
                    ),
                    cons=(
                        "Producers cannot freely add required fields",
                        "Does not protect old readers from new data",
                    ),
                ),
                Option(
                    name="FORWARD compatible",
                    when=(
                        "Producers must be free to emit new fields before "
                        "consumers are updated (producer-led evolution)."
                    ),
                    pros=(
                        "Producers can add required fields / evolve first",
                        "Old consumers can read data written by a new schema",
                    ),
                    cons=(
                        "Consumers cannot rely on new fields existing",
                        "Dropping a required field is unsafe",
                    ),
                ),
                Option(
                    name="FULL (backward AND forward)",
                    when=(
                        "High-stakes shared contracts where producers and "
                        "consumers deploy independently and neither can break."
                    ),
                    pros=(
                        "Only fully-safe changes allowed -> maximum safety",
                        "Either side can deploy first",
                    ),
                    cons=(
                        "Most restrictive -- many useful changes are rejected",
                        "Slows schema evolution",
                    ),
                ),
                Option(
                    name="NONE",
                    when=(
                        "Throwaway / experimental topics only -- effectively no "
                        "guarantee."
                    ),
                    pros=("Maximum flexibility for prototyping",),
                    cons=(
                        "Any change can break any reader silently",
                        "Unsafe for anything a consumer depends on",
                    ),
                ),
            ),
            rule_of_thumb=(
                "Default to BACKWARD (consumers upgrade last); pick FULL for "
                "shared, independently-deployed contracts; never NONE for "
                "anything with a real consumer."
            ),
        ),
        Comparison(
            question=(
                "On a failed check: fail-closed / quarantine or fail-open / "
                "flag-and-continue?"
            ),
            options=(
                Option(
                    name="Fail-closed (block / quarantine)",
                    when=(
                        "Financial, regulatory, or any table where wrong data is "
                        "worse than late data."
                    ),
                    pros=(
                        "Bad data cannot reach consumers",
                        "Forces a fix before propagation",
                        "Pairs with quarantine + circuit breaker",
                    ),
                    cons=(
                        "Halts the pipeline -> availability hit",
                        "A too-sensitive check can stop a healthy pipeline",
                    ),
                ),
                Option(
                    name="Fail-open (flag and continue)",
                    when=(
                        "Tables where stale-but-available beats stopped, and "
                        "consumers can tolerate flagged rows."
                    ),
                    pros=(
                        "Keeps data flowing / preserves availability",
                        "Surfaces issues without blocking",
                    ),
                    cons=(
                        "Bad data reaches consumers (flagged but present)",
                        "Flags get ignored -> quality erodes",
                    ),
                ),
            ),
            rule_of_thumb=(
                "Fail-closed + quarantine the bad rows for critical tables; "
                "fail-open + flag for tolerant ones -- decide per table from "
                "consumer risk, not globally."
            ),
        ),
        Comparison(
            question="Full-scan checks or sampling?",
            options=(
                Option(
                    name="Full-scan",
                    when=(
                        "Correctness-critical checks where a single missed row "
                        "matters -- PK uniqueness, referential integrity, control "
                        "totals."
                    ),
                    pros=(
                        "Catches every violation -- no false 'pass'",
                        "Required for uniqueness and reconciliation to mean "
                        "anything",
                    ),
                    cons=(
                        "Expensive at TB+ scale",
                        "Adds latency to every load",
                    ),
                ),
                Option(
                    name="Sampling / file statistics",
                    when=(
                        "Expensive distribution / profiling checks on huge tables "
                        "where an estimate is good enough."
                    ),
                    pros=(
                        "Cheap and fast even at PB scale",
                        "Can lean on existing file-level min/max/count statistics",
                    ),
                    cons=(
                        "Can miss rare violations (a single duplicate)",
                        "Estimates only -- wrong tool for uniqueness / totals",
                    ),
                ),
            ),
            rule_of_thumb=(
                "Full-scan PK uniqueness and reconciliation on critical tables; "
                "sample (or use file statistics) for expensive distribution "
                "checks at scale."
            ),
        ),
        Comparison(
            question=(
                "Circuit-breaker (halt downstream) or warn-and-continue on a "
                "detected problem?"
            ),
            options=(
                Option(
                    name="Circuit breaker (halt downstream)",
                    when=(
                        "A check fails badly enough that propagating would "
                        "corrupt Silver/Gold (high invalid rate, big "
                        "reconciliation gap)."
                    ),
                    pros=(
                        "Stops one bad batch from poisoning downstream",
                        "Downstream keeps serving yesterday's good data",
                        "Contains blast radius",
                    ),
                    cons=(
                        "Downstream goes stale until the breaker is reset",
                        "Needs a clear reset / replay procedure",
                    ),
                ),
                Option(
                    name="Warn-and-continue",
                    when=(
                        "Minor / informational deviations where stopping the "
                        "pipeline is the bigger harm."
                    ),
                    pros=(
                        "No availability loss",
                        "Good for low-severity, high-frequency signals",
                    ),
                    cons=(
                        "Bad data propagates while the warning sits unread",
                        "Warnings accumulate into noise",
                    ),
                ),
            ),
            rule_of_thumb=(
                "Trip the breaker on threshold breaches that would corrupt "
                "downstream; warn-and-continue only for low-severity signals -- "
                "and make the breaker's reset/replay path explicit."
            ),
        ),
        Comparison(
            question=(
                "In-pipeline assertions or an external observability platform?"
            ),
            options=(
                Option(
                    name="In-pipeline assertions",
                    when=(
                        "You need to gate a specific load -- fail/quarantine the "
                        "batch synchronously at a known point."
                    ),
                    pros=(
                        "Can block / quarantine inline before commit",
                        "Tests live with the transform; versioned in code",
                        "Deterministic, debuggable",
                    ),
                    cons=(
                        "You must hand-author each check",
                        "Hard to cover thousands of tables exhaustively",
                        "Sees one pipeline, not cross-system trends",
                    ),
                ),
                Option(
                    name="External observability platform",
                    when=(
                        "Broad monitoring / anomaly detection across many tables "
                        "and systems with auto-baselining."
                    ),
                    pros=(
                        "Auto-baselines freshness/volume/schema/distribution at "
                        "scale",
                        "Cross-system view + lineage + incident routing",
                        "Low per-table authoring effort",
                    ),
                    cons=(
                        "Usually detects after the fact -> cannot block inline",
                        "Another system to run and tune (alert fatigue risk)",
                    ),
                ),
            ),
            rule_of_thumb=(
                "Both: in-pipeline assertions to BLOCK at boundaries on critical "
                "tables, an observability platform to DETECT anomalies broadly "
                "across everything else."
            ),
        ),
    ),
    heuristics=(
        Heuristic(
            name="Day-over-day volume band",
            rule=(
                "Alert on a >20-30% day-over-day row-count change (up or down) vs "
                "a rolling, seasonality-aware baseline."
            ),
            example=(
                "today 0.6M rows vs 28-day median 1.0M -> -40% -> page: a source "
                "likely stopped emitting."
            ),
        ),
        Heuristic(
            name="Freshness formula",
            rule="Freshness check = now() - max(event_ts) <= SLA.",
            example=(
                "near-real-time table SLA 60 min: alert when the newest event is "
                "older than 60 minutes."
            ),
        ),
        Heuristic(
            name="Block at the boundary",
            rule=(
                "Enforce the contract at the producer/Bronze boundary; never "
                "silently clean bad data several layers downstream."
            ),
            example=(
                "reject the batch if a required column is missing -- do not "
                "coalesce nulls in Gold and hide the break."
            ),
        ),
        Heuristic(
            name="Sample expensive, full-scan critical",
            rule=(
                "Sample (or use file statistics) for expensive distribution "
                "checks; full-scan PK uniqueness and reconciliation on critical "
                "tables."
            ),
            example=(
                "profile mean/percentiles on a 1% sample; run count(*) == "
                "count(distinct pk) on the whole order table."
            ),
        ),
        Heuristic(
            name="Tiered severity",
            rule=(
                "Page (SEV1) only when a consumer-facing table is stale/wrong "
                "now; ticket (SEV2) for degraded-but-tolerable; "
                "log/dashboard (SEV3) for informational -- route by consumer "
                "impact, not check ease."
            ),
            example=(
                "Gold revenue table stale -> page; a Bronze profiling drift -> "
                "ticket."
            ),
        ),
        Heuristic(
            name="Null-rate baseline band",
            rule=(
                "Track each column's null rate against its baseline; alert when "
                "it leaves a tolerance band (and require non-null = 100% on keys)."
            ),
            example=(
                "customer_id null rate baseline ~0% -> jumps to 5% -> alert; "
                "upstream field likely renamed."
            ),
        ),
        Heuristic(
            name="Reconciliation tolerance",
            rule=(
                "Reconcile control totals across systems within an explicit "
                "tolerance (e.g. <= 0.1%), on a stable grain (day x source)."
            ),
            example=(
                "ABS(lake_revenue - processor_revenue)/processor_revenue <= 0.001 "
                "per day, else SEV1."
            ),
        ),
        Heuristic(
            name="Circuit-breaker threshold",
            rule=(
                "Trip the breaker and halt the load when the invalid-row rate "
                "exceeds a threshold (e.g. >5%) or reconciliation gap exceeds "
                "tolerance; quarantine the bad rows."
            ),
            example=(
                "8% of rows fail validity -> halt; downstream keeps serving "
                "yesterday's good partition."
            ),
        ),
        Heuristic(
            name="Cheap checks first",
            rule=(
                "Order checks cheap -> expensive: schema/type -> null/range -> "
                "uniqueness -> cross-system reconciliation; fail fast before "
                "paying for the dear ones."
            ),
            example=(
                "a schema break should fail in seconds, before you scan the table "
                "for duplicates."
            ),
        ),
        Heuristic(
            name="SLOs with error budgets",
            rule=(
                "Set per-table SLOs (freshness, completeness) and an error budget "
                "= allowed miss; track budget burn instead of reacting to every "
                "single breach."
            ),
            example=(
                "freshness <= 60 min on 99% of days -> 1% budget; spend it before "
                "you escalate process."
            ),
        ),
    ),
    pitfalls=(
        "Ad-hoc asserts scattered in transform code instead of an enforced, "
        "versioned data contract -- nobody owns them and they drift.",
        "Silently cleaning bad data downstream (coalescing nulls, dropping bad "
        "rows) so the real problem is hidden and recurs forever.",
        "Alert fatigue from thresholds set too tight or every check paging -- "
        "the one real incident drowns in noise and gets ignored.",
        "No quarantine / dead-letter path, so one bad batch either blocks the "
        "whole pipeline or poisons Gold.",
        "Checking only at the end (the BI table) instead of at the boundaries -- "
        "you detect the break three layers and several hours too late.",
        "No lineage, so root-cause of a wrong number takes hours of manual "
        "tracing across jobs.",
        "Testing schema only (columns/types) but never values or distributions "
        "-- data that is well-typed yet quietly wrong (amounts in cents) sails "
        "through.",
        "Treating freshness as implied rather than monitored -- a stale table "
        "throws no error and serves yesterday's numbers indefinitely.",
        "Sampling a uniqueness or reconciliation check -- a sample that 'passes' "
        "still hides the duplicate that corrupts every total.",
        "Setting one global SLA/severity for all tables instead of per-consumer "
        "need, so a monthly mart pages like a fraud feed.",
        "No circuit breaker or reset path -- the breaker trips but there is no "
        "defined replay, so downstream stays dark.",
    ),
    at_scale=(
        "Full-scan checks become too costly at TB+/PB scale; shift to sampling, "
        "file-level statistics (min/max/count in Parquet/Delta footers), and "
        "incremental checks over only the new partitions.",
        "Distribution-drift detection needs maintained baselines per "
        "table/column; computing and storing those baselines (with seasonality) "
        "is itself a data pipeline at scale.",
        "Lineage and observability stop being optional: with thousands of "
        "tables, manual root-cause is infeasible -- automated lineage + anomaly "
        "detection are mandatory infrastructure.",
        "Schema drift multiplies across many independent producers; without a "
        "registry enforcing compatibility, every producer is a potential silent "
        "breaker.",
        "Contract enforcement is an org problem across many teams -- you need "
        "ownership, deprecation windows, and CI on schema changes, not just a "
        "technical check.",
        "Reprocessing quarantined data has real cost: replaying large "
        "dead-letter backlogs competes for the same compute as live loads -- "
        "budget and schedule it.",
        "Checks add latency to every load; at scale you must parallelize / "
        "push checks into the distributed engine (e.g. a Spark profiler) rather "
        "than pulling data to a single node.",
        "Alerting volume scales with table count; auto-baselining and severity "
        "routing are required so a 10k-table estate does not generate 10k "
        "pages.",
    ),
    interview_signals=(
        "Names the five DQ dimensions with a concrete check for each: "
        "completeness (row-count drop >30%), accuracy (delivery_ts >= order_ts), "
        "consistency (lake revenue == processor revenue), freshness "
        "(now - max(event_ts) <= SLA), uniqueness (count == count distinct pk).",
        "Puts data contracts at the producer boundary and names a specific "
        "compatibility mode (e.g. BACKWARD) and what it allows -- 'shift quality "
        "left'.",
        "Lists the observability pillars: freshness, volume, schema, "
        "distribution, lineage -- and separates data observability from system "
        "observability.",
        "Designs failure handling: quarantine / dead-letter table for bad rows "
        "plus a circuit breaker that halts downstream so one batch cannot poison "
        "Gold; picks fail-closed vs fail-open per table.",
        "Sets data SLOs with error budgets and tiered severity (page vs ticket "
        "vs log) instead of paging on everything.",
        "Mentions lineage (column- and table-level) for root-cause and impact "
        "analysis, and the goal of minimizing 'data downtime'.",
        "Describes validation tooling by category (declarative expectation "
        "suites, transformation tests, Spark profilers) rather than pitching one "
        "vendor.",
        "Knows to full-scan uniqueness/reconciliation on critical tables but "
        "sample expensive distribution checks at scale.",
        "Calls out anomaly detection on volume/freshness/schema/distribution "
        "with baselines, not just static thresholds.",
        "Frames quality as enforced contracts + continuous observability, not a "
        "one-off 'validate at the end' step.",
    ),
)


__all__ = ["DEEP"]
