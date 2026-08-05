# End Users and Data Modeling Technique Selection (Lakehouse: Databricks UC + dbt + Airflow)

Research date: 2026-08-05. Sources are official docs, vendor engineering blogs, named-company
engineering posts, and the Kimball Group's own technique pages. Existing repo knowledge in
`vendor/minus_dataops/depth/requirements.py` (end-user personas, latency tiers) and
`depth/modeling.py` (grain, SCD, keys, partitioning, Kimball/Inmon/Data Vault) is **not** repeated
here; this report covers what that dossier does not have: sourced evidence, a consumer-class ->
design-delta matrix, a concrete intake question list, and a decision table that picks a technique.

---

## Executive summary (10 lines)

1. "Who consumes this, and to decide what?" is the one question that forces every other design
   choice; every serious source starts there, and dbt encodes it as first-class metadata (`exposures`).
2. Six consumer classes are enough: BI/exec reporting, self-serve analyst, data science/ML,
   operational/reverse-ETL app, regulatory/compliance, and external/shared data product.
3. Consumer class -- not data volume -- sets freshness SLA, grain, serving surface, and access model.
4. Databricks' own position: star schema and Data Vault are both first-class on the lakehouse;
   Data Vault/3NF belongs in Silver, Kimball star and denormalized marts belong in Gold.
5. dbt Labs' position diverges by whether you run a semantic layer: without one, "denormalize
   heavily"; with one, "stay as normalized as possible" so the metric engine can flex.
6. Benchmarks favor OBT for fixed BI query shapes (25-50% faster on Redshift/Snowflake/BigQuery,
   ~2x storage), but OBT loses flexibility, conformance, and cheap dimension corrections.
7. The mature pattern almost everywhere: conformed star as the core, OBT as a *projection* on top
   for hot, stable query shapes. Not either/or.
8. Kimball's durable parts survive the "storage is cheap" argument: declare the grain, one grain per
   fact, conformed dimensions, SCD2 for point-in-time truth. Its obsolete parts are cost assumptions.
9. Lakehouse mechanics changed: liquid clustering replaces partitioning and ZORDER for new tables;
   PK/FK are informational (`RELY`) but enable join elimination; surrogate keys via identity columns.
10. Honest default for this platform (KPI analytics from workspace-supplied sources): **conformed
    star in Gold, per-KPI OBT projections only where a hot query shape justifies them**, with the
    technique chosen by a 12-question intake, not by convention.

---

## Q1. Pipeline end-user taxonomy, and what each class changes

### The taxonomy that has tooling behind it

dbt makes downstream consumers a declared object. An `exposure` names the consumer, its `type`,
`owner`, `maturity`, and `depends_on` lineage; the allowed types are `dashboard`, `notebook`,
`analysis`, `ml`, and `application`, and you can select on them (`dbt test -s +exposure:x`).
That is a five-way consumer taxonomy shipped as product, and it is the cheapest place to anchor a
platform's own taxonomy.
(https://docs.getdbt.com/docs/build/exposures)

Data mesh generalizes the same idea as **output ports**: "a data product should provide multiple
endpoints ... polyglot output data ports that could be streams, files, SQL query interfaces, or
APIs", explicitly because "reports may require SQL access ... while data scientists want columnar
file access". The consumer picks the port; the port constrains the model.
(https://www.thoughtworks.com/en-us/insights/blog/data-strategy/how-to-select-technology-data-mesh,
https://www.entropy-data.com/learn/what-is-a-data-product)

### Named-company evidence

**Airbnb (Minerva).** Minerva holds "more than 12,000 metrics and 4,000 dimensions with over 200
data producers". Critically it fans out to *distinct consumer classes from one definition*:
dashboarding tools, the experimentation/A-B framework, anomaly detection and lineage tools, ML
training feature repositories, and ad-hoc R/Python analysis. Design consequence Airbnb drew:
Minerva "takes fact and dimension tables as inputs, performs data denormalization, and serves
aggregated data to downstream applications" -- i.e. the *core stays dimensional*, and the
denormalization happens per-consumer at the serving edge.
(https://medium.com/airbnb-engineering/how-airbnb-achieved-metric-consistency-at-scale-f23cc53dea70,
https://medium.com/airbnb-engineering/how-airbnb-enables-consistent-data-consumption-at-scale-1c0b6a8b9206)

**Uber.** Uber tiers *datasets* by consumer criticality rather than by content: "Tier 1-2" is data
impacting compliance, revenue, or brand; "Tier 5" is temporary ad-hoc data deletable after a fixed
period. Tiering "helps with determining the impact of outages and provide guidelines on what tiers
of data should be used for what purposes." Tier then mechanically drives obligations: Tier 1/2
assets auto-onboard into freshness, completeness, duplication, cross-DC consistency and semantic
checks, and every artifact must have an owner -- "Data is code and all code must be owned."
Freshness is defined precisely as "the delay after which data is 99.9% complete".
(https://www.uber.com/us/en/blog/ubers-journey-toward-better-data-culture-from-first-principles/,
https://www.uber.com/us/en/blog/operational-excellence-data-quality/)

**Shopify.** Change events from sharded MySQL are "denormalized into large Hive/Parquet tables",
but the warehouse itself "has mostly stuck with dimensional modeling and materializing into a
snowflake schema", with "individual teams using denormalized and wide tables for critical datasets
based on performance optimizations." That is precisely the star-core + selective-OBT split, in
production, at a company that runs dbt.
(https://www.dataengineeringpodcast.com/episodepage/how-shopify-is-building-their-production-data-warehouse-using-dbt,
https://shopify.engineering/capturing-every-change-shopify-sharded-monolith)

**GoCardless / Convoy / PayPal (consumer-driven contracts).** The contract is the interface between
producer and consumer class. GoCardless defines contracts in Jsonnet, merged to git by the data
owner, which auto-provisions BigQuery/PubSub resources. PayPal's open-sourced Data Contract Template
has eight sections including **stakeholders, roles, and service-level agreement** -- the consumer
and its SLA are part of the schema artifact, not a side document.
(https://medium.com/gocardless-tech/implementing-data-contracts-at-gocardless-3b5c49074d13,
https://en.wikipedia.org/wiki/Data_contract)

**Databricks (operational/reverse-ETL consumer).** Databricks now draws a hard line at the serving
boundary: "the lakehouse is optimized for analytics and enrichment, while Lakebase is designed for
operational workloads that require fast lookup-style queries and transactional consistency."
Gold tables / ML features / predictions are *synced* into Postgres for "sub-10 ms query latency and
thousands of QPS". An operational consumer does not get a different star; it gets a different store.
(https://www.databricks.com/blog/reverse-etl-lakebase-activate-your-lakehouse-data-operational-analytics,
https://docs.databricks.com/aws/en/oltp/projects/reverse-etl)

**ML consumer.** Databricks Feature Engineering in UC enforces the ML-specific modeling constraint:
"point-in-time correctness creates a training dataset that reflects feature values as of the time
each label observation was recorded ... to prevent data leakage." A model version binds to a
training set that records exactly which feature tables and point-in-time lookups produced it. This
makes SCD2/effective-dating a *hard requirement* for the ML consumer, not a nice-to-have.
(https://docs.databricks.com/aws/en/machine-learning/feature-store/time-series,
https://docs.databricks.com/aws/en/machine-learning/feature-store/concepts)

**Regulatory/compliance consumer.** BCBS 239 supervisory guidance requires "complete and up-to-date
data lineage at the data attribute level - starting from data capture to final reporting", and the
ability to "trace every risk metric from source to report". Design consequence: immutable raw
retention, attribute-level lineage, restatement-safe (as-of) history, and reproducible point-in-time
recomputation. This is the one consumer class that can veto a "just rebuild Gold" pipeline.
(https://www.bis.org/publ/bcbs_nl36.htm, https://atlan.com/know/data-governance/bcbs-239-data-lineage/)

### Consumer class -> design deltas

| Consumer class | Freshness / SLA | Grain served | Modeling style | Serving surface | Access control |
|---|---|---|---|---|---|
| Exec reporting / fixed KPI packs | Daily or weekly, predictable cadence; correctness >> latency | Pre-aggregated to reporting periods, plus drill path to atomic | Star + metric view; small number of high-trust aggregates | DBSQL dashboard / metric view; snapshotted for tie-out | Broad read, but locked definitions; changes are announced |
| Self-serve analyst | Hours; must be "as of last night" and stated | Atomic fact grain + conformed dims | Star (flexibility is the product) or semantic layer over it | SQL warehouse, notebooks | Row/column masking on PII; wide read on curated schemas |
| BI dashboard (fixed query shapes) | Minutes-to-hours | Pre-joined to the dashboard's exact cut | OBT projection off the star; materialized view | DBSQL / cached materialization | Same as analyst, plus per-tenant RLS if embedded |
| Data science / ML features | Batch training: daily. Serving: online, sub-second | Row grain == training join key, with event timestamp | Time-series feature tables, denormalized, point-in-time correct | Offline UC feature table + online store | Feature-level lineage; leakage is a correctness bug |
| Operational app / reverse ETL | Continuous or scheduled sync; sub-10 ms reads | Entity/key grain, one row per key | Narrow entity table keyed for point lookup | Postgres/Lakebase synced table, or API | Write-back path needs idempotent keys and rate limits |
| Regulatory / compliance | Fixed reporting calendar; restatement windows | Atomic, immutable, bitemporal (event time + knowledge time) | Data Vault or effective-dated 3NF in Silver; SCD2 everywhere | Governed marts + attribute-level lineage export | Least privilege, full audit trail, retention floors |
| External / shared data product | Contract-defined | Contract-defined, versioned | Stable published interface; versioned schema | Delta Sharing / files / API port | Contract + SLA is the access boundary |

---

## Q2. Intake: the minimal high-leverage question set

Kimball's own four-step design process is the smallest credible core -- "select the business
process, declare the grain, identify the dimensions, and identify the facts" -- where grain answers
"How do you describe a single row in the fact table?" and should be "the most atomic level
possible". But those four steps assume the business goal is already known; the questions below are
what a senior architect asks *before* step 1.
(https://www.kimballgroup.com/data-warehouse-business-intelligence-resources/kimball-techniques/dimensional-modeling-techniques/four-4-step-design-process/)

**A. End goal and consumers (ask first, refuse to skip)**
1. Who reads the output, by name and role? Pick the classes from the table above (multi-select).
2. What decision does each consumer make with it, and what would they do differently if the number
   moved? (If no answer, the asset is Tier 5 by Uber's rule and should not get a pipeline.)
3. What is the delivery surface: dashboard, notebook, exported file, app/API, model training, a
   regulator's template? (This is the dbt `exposure.type`.)
4. Who owns it, and who is paged when it is wrong or late? (Uber: "all code must be owned.")

**B. Freshness and availability**
5. How stale can the number be before the decision is wrong -- minutes, hours, or "as of yesterday"?
6. Is there a reporting calendar or cutoff (month-end close, regulatory submission)?
7. Does late-arriving data have to *restate* a previously published number, or is it appended?

**C. Grain, history, retention**
8. What is one row of the answer? (Force a sentence: "one row per <entity> per <period>".)
9. Do questions need "as it was then" (customer's segment on the order date) or only "as it is now"?
   -- this is the SCD2 switch and it is binary.
10. How far back must history be queryable, and how long must raw be retained for audit or replay?

**D. Query patterns and volume**
11. Which slices/filters will 80% of queries use, and are they stable or exploratory? (Stable +
    narrow -> OBT/materialized view; exploratory -> star. Also picks liquid clustering keys.)
12. Rough fact volume and growth (rows/day, expected total), and concurrency at peak.

**E. Compliance and access**
13. Does the data contain PII/PHI or regulated attributes, and who must *not* see them?
14. Is attribute-level lineage or reproducible point-in-time recomputation required?

Anything beyond 14 questions gets skipped by real users. Volume/latency sizing arithmetic is already
covered by `depth/requirements.py`; don't re-ask what the profiler can measure.

---

## Q3. Technique selection for a lakehouse

### What each vendor actually says

**Databricks.** Their layer mapping is explicit: Bronze = raw landing, source structures as-is;
Silver = "Data Vault (3NF-like), normalized models"; Gold = "Kimball star schemas, denormalized
marts". They state "Data Vault focuses on agile data warehouse development where scalability, data
integration/ETL and development speed are important" and that "sometimes tables in the Gold Layer
can be completely denormalized, typically if the data scientists want it that way". Their conclusion
is hybrid, not exclusive: "Both normalized Data Vault (write-optimized) and denormalized dimensional
models (read-optimized) data modeling styles have a place in the Databricks Lakehouse."
(https://www.databricks.com/blog/2022/06/24/data-warehousing-modeling-techniques-and-their-implementation-on-the-databricks-lakehouse-platform.html)

Their 2024-2026 "myths and truths" post reinforces it: dimensional models are encouraged, PK/FK are
supported (GA in DBR 15.2) with `RELY` as an optimizer hint, CHECK/NOT NULL constraints are native,
Unity Catalog Metric Views give a semantic layer without a proprietary BI tool, and -- notably --
"medallion architecture is required" is listed as a **myth**: it is "a reference architecture, not
mandatory", to be adapted to "company size, regulatory requirements, usage patterns, and team
structure".
(https://www.databricks.com/blog/databricks-lakehouse-data-modeling-myths-truths-and-best-practices)

For Data Vault specifically, Databricks separates Raw Vault (hubs/links/satellites, no business
rules, loaded from staging) from Business Vault (business rules, DQ, cleansing/conforming applied),
noting "access to the Raw Data Vault layer is often more restricted", and that the main win is
"efficient parallel loading ... as there is less dependency between the tables".
(https://www.databricks.com/blog/data-vault-best-practice-implementation-lakehouse)

**dbt Labs.** Marts guidance is entity-grained and wide: "The most important aspect of marts is that
they contain all of the useful data about a particular entity at a granular level", and "in the
modern data stack storage is cheap and it's compute that is expensive ... packing these into very
wide denormalized concepts". Name marts by entity (`customers`, `orders`), group by department only
past ~10 marts, and avoid `finance_orders` / `marketing_orders` splits. Materialization ladder:
view -> table -> incremental. **The important caveat**: "Our guidance here diverges if you use the
Semantic Layer. In a project without the Semantic Layer we recommend you denormalize heavily" --
with a semantic layer, stay normalized so MetricFlow can flex.
(https://docs.getdbt.com/best-practices/how-we-structure/4-marts,
https://docs.getdbt.com/best-practices/how-we-structure/1-guide-overview)

**Practitioners.** The dbt community thread on Kimball's relevance is the honest version of the
debate. Obsolete assumptions named: databases are slow and expensive; SQL is limited (no window
functions); you can never join fact tables; businesses change slowly. Durable parts: "fully
normalized data models are way too complex for non-developers", conformed dimensions for
consistency, and grain definition preventing breakage as models expand. The pragmatic consensus in
the thread is Xavier's hybrid: "normalized foundation with denormalized flat tables covering 80% of
use cases, reserving the normalized layer for edge cases."
(https://discourse.getdbt.com/t/is-kimball-dimensional-modeling-still-relevant-in-a-modern-data-warehouse/225)

**Benchmark.** Fivetran's TPC-DS (scale 100) test across Redshift, Snowflake and BigQuery with
caching disabled: denormalized OBT beat the star by ~25-30% (Redshift), ~25% (Snowflake), and ~50%
(BigQuery) -- at ~60GB vs ~30GB storage. Their own recommendation is not "always OBT": prefer star
for "better ELT/ETL code conceptualization and organization", easier end-user navigation, and lower
storage; prefer denormalized when query performance dominates. They suggest "staging data through
normalized schemas before re-joining for end-user access" -- i.e. star as source of truth, OBT as
projection.
(https://www.fivetran.com/blog/star-schema-vs-obt)

**Activity schema.** A genuinely different option: a single time-ordered activity stream table with
one definition per concept, no foreign-key joins, incremental updates, and modeling separated from
querying; the pitch is that "queries run substantially faster against an activity stream table,
which has fewer columns, requires fewer joins, and can be easily partitioned/indexed by time."
It fits event/journey/funnel analytics and behavioral products. It fits KPI marts over
non-event source systems poorly, and its ecosystem support (BI tools, dbt packages) is thin.
(https://www.activityschema.com/, https://github.com/ActivitySchema/ActivitySchema,
https://www.ssp.sh/brain/activity-schema/)

### Decision table: context -> technique

| Context signal | Technique | Rationale / source |
|---|---|---|
| Many consumers, unknown future questions, multiple business processes sharing entities | **Kimball star with conformed dimensions** (Gold) | Conformed dims "deliver consistent descriptive attributes across dimensional models and support the ability to drill across" (Kimball Group) |
| One dashboard/KPI, stable known query shape, latency-sensitive, few slicers | **OBT / materialized view projected off the star** | 25-50% faster in benchmark; Shopify does exactly this for critical datasets |
| Many heterogeneous source systems, high schema churn, auditability, parallel loading, multiple teams loading independently | **Data Vault 2.0 in Silver**, star in Gold | Databricks places DV in Silver; "less dependency between tables" enables parallel loads |
| Strict regulatory lineage, restatement, "as-of" reproduction | **Effective-dated Silver (DV or 3NF) + SCD2 everywhere + immutable Bronze** | BCBS 239 attribute-level lineage from capture to report |
| ML feature consumption | **Denormalized time-series feature tables**, one row per entity+timestamp | Point-in-time joins prevent leakage (Databricks Feature Engineering) |
| Operational app, point lookups, sub-second | **Narrow key-grain table synced to an OLTP store**; not a star at all | Lakebase synced tables / reverse ETL |
| Behavioral/event product analytics, funnels, journeys | **Activity schema** (consider), else event fact + conformed dims | Activity schema spec |
| Source is a single operational system, small volume, one consumer, exploratory phase | **3NF-ish staged models, defer modeling** | dbt thread: "intentionally accumulate technical debt initially, refactor after proving value" |
| You run a semantic/metric layer (dbt SL, UC Metric Views) | **Keep marts normalized (star)**; let the metric layer denormalize | dbt Labs explicit divergence; UC Metric Views "define metrics once ... group by any available dimension" |

**Honest default for a KPI-analytics platform: conformed star in Gold, atomic fact grain, SCD2 on
dimensions whose history is asked for, plus per-KPI OBT projections only where question 11 says the
query shape is hot and stable.** OBT-first is defensible only when there is exactly one consumer and
one query shape, and it should still be *derived from* the star rather than built beside it.

### SCD2 by technique

| Technique | Where history lives | Mechanics |
|---|---|---|
| Kimball star | SCD2 dimension rows with `valid_from` / `valid_to` / `is_current` + surrogate key per version | dbt `snapshot`; Delta MERGE; fact stores the surrogate key resolved at event time |
| OBT | History is *baked in* at build time -- correcting a dimension attribute rewrites every affected row | The known trap: "updating a customer table may require rewriting millions of rows" |
| Data Vault | Native: satellites are insert-only, hub/link keys are stable; history is a satellite load date | Raw Vault never updates; point-in-time (PIT) and bridge tables reconstruct as-of views |
| 3NF | Effective-dated tables per entity; bitemporal if restatements matter | Heaviest query cost, best audit story |
| Activity schema | Inherent -- the stream is append-only and time-ordered | "as-of" is a window function, not a join |

---

## Q4. Schema-design mechanics at scale (lakehouse specifics)

**Conformed dimensions and the bus matrix.** The enterprise bus matrix -- rows = business processes,
columns = dimensions -- is still the cheapest artifact for planning cross-mart consistency, and it
decomposes the build "into manageable pieces by focusing on business processes, while delivering
integration via standardized conformed dimensions that are reused across processes". A platform can
generate this matrix mechanically from its KPI registry: KPIs are the rows, resolved entities the
columns. Any dimension appearing in two marts with different definitions is a defect, not a variant.
(https://www.kimballgroup.com/data-warehouse-business-intelligence-resources/kimball-techniques/kimball-data-warehouse-bus-architecture/,
https://www.kimballgroup.com/data-warehouse-business-intelligence-resources/kimball-techniques/dimensional-modeling-techniques/enterprise-data-warehouse-bus-matrix/)

**Grain declaration discipline.** Grain is "determined by the physical realities of the operational
system", should be atomic, and one fact table serves one grain. dbt's version of the same rule:
marts are per-entity at a granular level, and time rollups (`orders_per_day`) belong in metrics, not
in mart names. Practical enforcement: a mart must carry a machine-checkable grain declaration and a
uniqueness test on the declared key.

**Surrogate vs natural keys.** Databricks recommends system-generated surrogate keys -- "surrogate
keys are system-generated, meaningless keys so that we don't have to rely on various Natural Primary
Keys and concatenations" -- with native identity columns, and BIGINT surrogates preferred over
strings on dimension PKs. Caveat for a lakehouse: identity columns are not deterministic across a
full rebuild, so a pipeline that must be reproducible (regulatory consumer) should use a
deterministic hash of the natural key + effective date instead. Repo-relevant: a MERGE keyed on a PK
cannot repair a *changed PK value* -- that stays a full-rebuild case.
(https://www.databricks.com/blog/data-modeling-best-practices-implementation-modern-lakehouse)

**Constraints as optimizer hints.** PK/FK are supported (DBR 11.3+, GA 15.2), informational only,
but declaring FKs with `RELY` enables **dynamic join elimination** -- a star with declared
constraints can skip dimension joins the query does not actually need, which closes much of the gap
to OBT for free. Also run `ANALYZE TABLE ... COMPUTE STATISTICS` on dimension keys for AQE.
(https://www.databricks.com/blog/databricks-lakehouse-data-modeling-myths-truths-and-best-practices,
https://medium.com/dbsql-sme-engineering/star-schema-data-modeling-best-practices-on-databricks-sql-8fe4bd0f6902)

**Partitioning vs clustering.** This is the biggest change from classic warehouse advice.
Databricks: "Databricks recommends liquid clustering for all new tables, including streaming tables
and materialized views" and "liquid clustering replaces table partitioning and ZORDER". Up to four
clustering keys; "for smaller tables (less than 10 TB), using more clustering keys can degrade
performance when filtering on a single column". Clustering-on-write only kicks in above size
thresholds (64MB with 1 key up to 1GB with 4 keys for UC managed tables). Automatic liquid
clustering (DBR 15.4 LTS+, managed tables) picks keys from observed query patterns. Older guidance
already warned to "use partitioning sparingly, only when you have many Terabytes of compressed
data". DBSQL practice: cluster **dimensions on frequently filtered attributes**, **facts on the
dimension foreign keys**.
(https://docs.databricks.com/aws/en/delta/clustering)

Model-choice implication: a star's clustering keys are obvious (fact FKs + date); an OBT's are not,
because it absorbs many query shapes at once -- which is another reason OBTs should be narrow in
purpose rather than universal.

**Wide-table performance on Photon/DBSQL vs join-heavy stars.** Photon gives vectorized execution
and predicate pushdown with reported "10-50x faster query performance" versus non-Photon, and
dimension tables in a star are usually small enough to broadcast. Combined with `RELY` join
elimination and liquid clustering on FKs, the join tax on DBSQL is much smaller than the
Fivetran-style benchmarks on other engines suggest. The remaining honest OBT advantages are
predictability (no join-order surprises), simpler BI tool config, and no small-file/shuffle risk on
very large dimension joins. The remaining honest OBT costs are storage multiple (~2x in the
benchmark), rewrite cost on dimension corrections, and loss of conformance.

---

## Q5. How end goal + model choice feed a reviewable pipeline blueprint

A blueprint is reviewable when a human can disagree with a specific sentence. It should state, per
layer, *what is preserved, what is decided, and who is served*.

**Header (from Q2 answers)**
- Consumers (class + named owner + delivery surface, one line each) -- this is the exposure list.
- Decision each consumer makes; the "so what" for the top KPI.
- Freshness SLA per consumer, and the definition of "fresh" used (Uber's: delay until 99.9% complete).
- Criticality tier, derived from consumers, driving which DQ checks are mandatory.
- Compliance flags: PII/PHI columns, retention floor, lineage depth required.
- Chosen technique + the decision-table row that selected it, so a reviewer can attack the premise.

**Bronze -- preserves**
- Source fidelity: land as-is, no business rules, append-only, source file/offset recorded.
- Retention and immutability commitment (regulatory consumer sets the floor).
- Schema-drift policy (mergeSchema, quarantine, or fail).
- Statement: "no consumer reads Bronze directly" -- or, if one does, name it and why.

**Silver -- conforms**
- Grain of each conformed entity, one sentence each: "one row per X per Y".
- Deduplication key and its determinism; the idempotent upsert key.
- Type casts, unit and currency normalization, timezone normalization.
- Which entities are SCD2 (from intake Q9) and their `valid_from`/`valid_to` semantics.
- Data quality contract: the columns with NOT NULL/CHECK, plus freshness/completeness/duplication
  thresholds, and what happens on breach (quarantine vs fail vs warn).
- If the technique is Data Vault: Raw Vault vs Business Vault split, and who can read Raw.

**Gold -- serves whom**
- One block per consumer, not one block per table. Each names: the consumer, the tables/metric views
  it reads, the grain, the measures with their aggregation semantics, and the SLA.
- Conformed dimension list and the bus matrix showing which marts share which dimensions.
- Which measures are defined in a metric view / semantic layer vs materialized -- and the rule that a
  measure is defined exactly once ("define metrics once at the catalog level ... access from
  anywhere").
- Any OBT projection: which star it is derived from, which query shape justifies it, its refresh
  strategy, and its rebuild cost when a dimension attribute is corrected.
- Physical: liquid clustering keys per table with the filter/join pattern that justified each,
  PK/FK + `RELY` declarations, materialization (view -> table -> incremental) and why.
- Access: masking/RLS per consumer class, and the UC grants that implement it.

**Serving edge (only if a non-SQL consumer exists)**
- ML: feature table names, entity + timestamp keys, point-in-time lookup spec, offline/online parity.
- Operational: synced-table target, sync mode (snapshot/scheduled/continuous), latency target,
  write-back idempotency.
- External: contract version, output port type, SLA, deprecation policy.

**Orchestration and ownership**
- Airflow DAG shape, schedule derived from the tightest SLA, backfill/restatement procedure.
- Owner and pager per asset; deprecation date for anything Tier-5-shaped.

---

## Proposed intake + modeling selection flow for this platform

### Step 1 -- Consumer intake (blocking, before any generation)

Ask exactly these, as a panel with structured options. Persist answers as workspace-level
definitions so they are reused across every KPI, matching the existing blocker-panel pattern.

| # | Question | Answer type | Feeds |
|---|---|---|---|
| 1 | Who consumes this workspace's output? | multi-select: exec_reporting, self_serve_analyst, bi_dashboard, ml_features, operational_app, regulatory, external_share | technique, serving layer, SLA |
| 2 | For each consumer: what decision does it drive? | free text, one line each | tier; drop assets with no answer |
| 3 | Delivery surface per consumer | select: dashboard / notebook / analysis / ml / application (dbt exposure types) | blueprint serving section |
| 4 | Owner and on-call for the output | name + contact | ownership gate |
| 5 | Freshness tolerance per consumer | select: real_time_sub_second / minutes / hourly / daily / weekly / monthly_close | schedule, batch-vs-stream, serving store |
| 6 | Is there a reporting calendar or cutoff? | date rule or none | snapshot + restatement policy |
| 7 | Do late-arriving facts restate published numbers? | yes / no | snapshot strategy, audit needs |
| 8 | One row of the answer is ... | forced sentence: "one row per ___ per ___" | grain declaration + uniqueness test |
| 9 | Do you need "as it was then" attribute values? | yes / no, per entity | SCD2 switch |
| 10 | History depth queryable; raw retention required | duration + duration | Bronze retention, partition/cluster keys |
| 11 | Top filters/slicers used by 80% of queries; stable or exploratory? | column list + stable/exploratory | OBT-vs-star; liquid clustering keys |
| 12 | Fact volume and growth; peak concurrency | numbers | materialization, clustering thresholds |
| 13 | PII/PHI or regulated columns, and who must not see them | column list + audience | masking, RLS, PHI gate |
| 14 | Is attribute-level lineage or point-in-time reproducibility required? | yes / no | Data Vault vs plain star; deterministic keys |

Volume/latency arithmetic and persona background stay in `vendor/minus_dataops` -- the intake should
call into that dossier rather than re-encode it.

### Step 2 -- Technique selection (deterministic, from Step 1 answers)

Evaluate in order; first match wins, and record which rule fired so a reviewer can override it.

| Rule | Condition (from intake) | Chosen technique |
|---|---|---|
| R1 | Q14 = yes, or Q1 includes `regulatory`, and sources >= 3 systems | Data Vault (Raw + Business) in Silver, star in Gold, SCD2 mandatory, deterministic hash keys |
| R2 | Q1 includes `ml_features` | Add time-series feature tables at the training-join grain, point-in-time correct; star still serves analytics |
| R3 | Q1 includes `operational_app` and Q5 = real_time_sub_second | Add a narrow key-grain synced table to an OLTP store; do not reshape the analytics model for it |
| R4 | Q1 is a single consumer, Q11 = stable, and KPI count is small | OBT / materialized view, still derived from conformed Silver entities |
| R5 | Q1 has >= 2 consumer classes, or Q11 = exploratory, or >= 2 business processes share an entity | **Conformed Kimball star in Gold** (the default) |
| R6 | Source data is event-stream-shaped and the questions are journeys/funnels | Consider activity schema; otherwise event fact + conformed dims |
| R7 | Q9 = yes for any entity | SCD2 on that dimension regardless of which rule above fired |
| R8 | Q11 identifies a hot, stable query shape under R5 | Add an OBT projection *off* the star for that shape only, with its rebuild cost stated |

### Step 3 -- Defaults this platform should ship

- **Default technique: conformed Kimball star in Gold** (R5). It is what Databricks recommends for
  Gold, what Shopify runs, what Airbnb feeds Minerva from, and the only option that survives a second
  consumer appearing later. The current medallion + star-ish gold is the right default; what is
  missing is that it is currently *assumed* rather than *selected and recorded*.
- **Default grain: atomic**, one grain per fact table, declared as a sentence and tested for uniqueness.
- **Default history: SCD1 unless intake Q9 says otherwise.** Do not pay SCD2 cost by reflex.
- **Default physical: liquid clustering, no partitioning** unless the table is many TB; dimensions
  clustered on filtered attributes, facts on FKs + date; <= 4 keys.
- **Default keys: deterministic hash surrogate** of natural key (+ effective date for SCD2), not
  identity columns, so rebuilds are reproducible.
- **Default constraints: declare PK/FK with `RELY`** and run ANALYZE on dimension keys -- free join
  elimination, and it doubles as documentation.
- **Default metric definition: exactly once**, in a UC metric view or the dbt semantic layer. If a
  semantic layer is in play, keep marts normalized (dbt Labs' explicit divergence) instead of
  denormalizing per dashboard.
- **OBT is a projection, never the source of truth.** Every OBT records the star it derives from and
  the query shape that justified it; if the shape stops being hot, the OBT is deleted.
- **Blueprint is the review artifact**: consumers + tier + SLA at the top, then Bronze
  preserves / Silver conforms / Gold serves-whom, then serving edge, then orchestration. Emit it
  before generating pipeline code, and require sign-off on the technique-selection rule that fired.

---

## Source list

Databricks
- https://www.databricks.com/blog/2022/06/24/data-warehousing-modeling-techniques-and-their-implementation-on-the-databricks-lakehouse-platform.html
- https://www.databricks.com/blog/data-modeling-best-practices-implementation-modern-lakehouse
- https://www.databricks.com/blog/databricks-lakehouse-data-modeling-myths-truths-and-best-practices
- https://www.databricks.com/blog/data-vault-best-practice-implementation-lakehouse
- https://www.databricks.com/blog/five-simple-steps-for-implementing-a-star-schema-in-databricks-with-delta-lake
- https://docs.databricks.com/aws/en/delta/clustering
- https://docs.databricks.com/aws/en/machine-learning/feature-store/time-series
- https://docs.databricks.com/aws/en/machine-learning/feature-store/concepts
- https://docs.databricks.com/aws/en/uc-semantics/metric-views
- https://www.databricks.com/blog/reverse-etl-lakebase-activate-your-lakehouse-data-operational-analytics
- https://docs.databricks.com/aws/en/oltp/projects/reverse-etl
- https://medium.com/dbsql-sme-engineering/star-schema-data-modeling-best-practices-on-databricks-sql-8fe4bd0f6902

dbt Labs
- https://docs.getdbt.com/best-practices/how-we-structure/1-guide-overview
- https://docs.getdbt.com/best-practices/how-we-structure/4-marts
- https://docs.getdbt.com/docs/build/exposures
- https://discourse.getdbt.com/t/is-kimball-dimensional-modeling-still-relevant-in-a-modern-data-warehouse/225

Kimball Group
- https://www.kimballgroup.com/data-warehouse-business-intelligence-resources/kimball-techniques/dimensional-modeling-techniques/four-4-step-design-process/
- https://www.kimballgroup.com/data-warehouse-business-intelligence-resources/kimball-techniques/kimball-data-warehouse-bus-architecture/
- https://www.kimballgroup.com/data-warehouse-business-intelligence-resources/kimball-techniques/dimensional-modeling-techniques/enterprise-data-warehouse-bus-matrix/

Named-company engineering
- https://medium.com/airbnb-engineering/how-airbnb-achieved-metric-consistency-at-scale-f23cc53dea70
- https://medium.com/airbnb-engineering/how-airbnb-enables-consistent-data-consumption-at-scale-1c0b6a8b9206
- https://www.uber.com/us/en/blog/ubers-journey-toward-better-data-culture-from-first-principles/
- https://www.uber.com/us/en/blog/operational-excellence-data-quality/
- https://shopify.engineering/capturing-every-change-shopify-sharded-monolith
- https://www.dataengineeringpodcast.com/episodepage/how-shopify-is-building-their-production-data-warehouse-using-dbt
- https://medium.com/gocardless-tech/implementing-data-contracts-at-gocardless-3b5c49074d13
- https://netflixtechblog.com/optimizing-data-warehouse-storage-7b94a48fdcbe

Other
- https://www.fivetran.com/blog/star-schema-vs-obt
- https://www.activityschema.com/ ; https://github.com/ActivitySchema/ActivitySchema ; https://www.ssp.sh/brain/activity-schema/
- https://www.thoughtworks.com/en-us/insights/blog/data-strategy/how-to-select-technology-data-mesh
- https://en.wikipedia.org/wiki/Data_contract
- https://www.bis.org/publ/bcbs_nl36.htm ; https://atlan.com/know/data-governance/bcbs-239-data-lineage/
