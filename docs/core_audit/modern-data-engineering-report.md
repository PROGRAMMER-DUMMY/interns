# Modern Data Engineering for an Agentic, CLI-Based Data Platform: Landscape Benchmark + Build Guidance

> Companion artifact to `phase-0-gap-matrix.md`. This is the grading standard the
> matrix verdicts are measured against; committed to the repo so any later reader
> can check a verdict against the exact text it cites. Content is the source
> research report as received on 2026-07-18.

## TL;DR
- **Instrument everything through open standards and native meters.** For LLM/agent cost, standardize on OpenTelemetry GenAI semantic conventions (`gen_ai.usage.input_tokens`/`output_tokens`) piped from Claude Code, Codex, and Gemini into one backend (Langfuse/Helicone/LiteLLM gateway), and reconcile against provider billing APIs — never trust local price tables as the source of truth. For pipeline/warehouse cost, use each platform's native meter (Databricks `system.billing.usage` × `list_prices`, Snowflake `ACCOUNT_USAGE`, BigQuery bytes-scanned) with governed tags for chargeback. The single most important design decision is to make token/DBU/credit attribution a first-class, per-run column, not an afterthought.
- **Build the platform on asset-centric, contract-enforced, medallion foundations.** Adopt a lakehouse (Delta/Iceberg) with bronze/silver/gold, dbt Core for transformation, an asset-aware orchestrator (Dagster is the most agent-friendly; Airflow the safest default), data contracts enforced at the producer boundary (Soda/Great Expectations), and OpenLineage lineage from day one. These give AI agents the *structured context* they need — bare metadata is why most agentic data projects fail.
- **Make competing objectives (latency vs cost vs freshness vs accuracy) explicit, tunable parameters.** Expose them as pipeline config the agent reasons over and that maps to concrete engine levers (trigger interval, warehouse size, incremental strategy, materialization, partition/clustering). Bound agent autonomy with policy: generate reviewable code (dbt/SQL/DAGs), run CI on changed models, and require lineage-based impact analysis before deploy.

## Key Findings

1. **Token cost has two layers that must be metered separately but unified in reporting.** The agent/LLM layer is metered per-request via provider `usage` objects and OTel GenAI conventions; the pipeline/compute layer is metered via warehouse system tables. A mature agentic platform joins both into a single per-run/per-workspace cost ledger.
2. **All three of your agent CLIs now emit native telemetry.** Claude Code has built-in OpenTelemetry (metrics GA, traces in beta); Codex exports OTel metrics and structured log events; Gemini's `generateContent` returns a precise `usageMetadata` object. This means you do not need to wrap or proxy them to get token counts — though a gateway (LiteLLM) still helps for budgets and routing.
3. **The medallion pattern is a convention, not a product**, and maps cleanly onto dbt (sources=bronze, staging=silver, marts=gold). Table-format choice (Delta vs Iceberg vs Hudi) is an implementation detail chosen per workload, not a religion.
4. **Data contracts only create value when enforced at runtime**, shifted left to the producer. Great Expectations and Soda are the leading enforcement engines; dbt tests cover the transformation layer; a schema registry (Avro/Protobuf) covers streaming.
5. **Dimensional modeling (Kimball) remains the pragmatic default for BI**, pairs naturally with dbt, and coexists with Data Vault (integration layer), Inmon (regulated EDW), and One Big Table (denormalized serving layer / specific dashboards). Modeling choice should be driven by the downstream consumer.
6. **Query optimization is fundamentally about reading less data.** Partition pruning, predicate pushdown, columnar formats, clustering/Z-ordering, and caching dominate; they simultaneously improve latency *and* cost because cloud warehouses bill on data scanned or compute-seconds.
7. **Production failure modes cluster around schema drift, late/out-of-order data, duplicates/poison records, and partial failures.** The defenses are well-established: schema enforcement + evolution policies, watermarks, idempotent/exactly-once sinks, dead-letter queues, checkpointing, and lineage-driven impact analysis with data SLAs/SLOs and error budgets.

## Details

### 1. Token / Cost Management

#### 1a. Agent / LLM layer

**How token accounting actually works per provider.**
- **Gemini `generateContent`** returns a `usageMetadata` object with exact camelCase fields: `promptTokenCount` (input, including system instructions, tools, and cached tokens), `candidatesTokenCount` (output), `totalTokenCount` (input + output, includes thinking tokens), `cachedContentTokenCount` (tokens served from context cache, counted *within* promptTokenCount), and `thoughtsTokenCount` (reasoning tokens on 2.5+ thinking models). A critical instrumentation caveat: on the **Gemini API (Google AI)**, `candidatesTokenCount` *includes* thinking tokens, but on **Vertex AI** it *excludes* them and reports them separately — so billed output = `candidatesTokenCount + thoughtsTokenCount` on Vertex, but may already include thoughts on the Gemini API. Verify empirically per endpoint. The separate `countTokens` endpoint runs the tokenizer on input only, is free (up to 3000 RPM), and should be called before a request for cost estimation / context-window checks. In streaming (`streamGenerateContent`), `usageMetadata` appears **only on the last chunk**. Thinking tokens are billed as output tokens and count against `maxOutputTokens` (too small a budget produces empty/truncated responses).
- **Claude Code CLI** has OpenTelemetry instrumentation built directly into the CLI: it records spans around each model request and tool execution, and emits metrics for token and cost counters plus structured log events. Enable with `CLAUDE_CODE_ENABLE_TELEMETRY=1` plus OTLP exporters. Subagent (Task tool) spans nest under the parent agent's `claude_code.tool` span, so the full delegation chain appears as one trace; spans carry a `session.id` for grouping multi-step loops. Metrics export every 60s, logs every 5s by default. Per Anthropic's Claude Code costs documentation (updated April 15, 2026, replacing the earlier $6 figure): "Across enterprise deployments, the average cost is around $13 per developer per active day and $150–250 per developer per month, with costs remaining below $30 per active day for 90% of users." Content capture (prompts/responses) is opt-in and off by default. Reconcile OTel against Anthropic's pull-based Usage and Cost API, which is the financial source of truth (OTel misses Claude Code on the web).
- **Codex CLI** exports structured log events and OTel metrics for API requests, tool calls, and sessions. Both Claude Code and Codex do *not* share one token ledger — check the active meter (local `/usage`, plan bar, console spend, or API-key billing) before acting.

**Tooling landscape.**
- **LiteLLM** — a gateway/SDK on the request path unifying ~100 providers; adds routing, fallbacks, virtual keys, and per-team budgets. Cost is computed locally from token counts × a built-in price table. Runs as a proxy (needs Postgres + Redis).
- **Helicone** — a drop-in proxy (change one base URL) logging input tokens, output tokens, per-request cost, and latency across 300+ models at zero markup. Fastest per-user/per-key attribution via `Helicone-User-Id` and `Helicone-Property-*` headers. Mintlify acquired Helicone on March 3, 2026 ("Mintlify acquires Helicone to redefine AI knowledge infrastructure"); founders Justin Torre and Cole Gottdank joined Mintlify. Helicone is now in maintenance mode ("security updates, bug fixes, and new models will keep shipping"), remains Apache 2.0 and self-hostable, and had processed over 14.2 trillion tokens across 16,000 organizations at acquisition.
- **Langfuse** — MIT-licensed observability platform; instrument code with traces/spans (or use SDK wrappers). Its standout feature is the multi-step trace view linking all LLM calls for one agent task into a single trace with per-step cost breakdown — exactly what you need to debug agent cost spikes. Ships tokenizers/pricing for OpenAI, Anthropic, and Google. Cannot enforce budgets (observability layer only). Self-host needs Postgres + ClickHouse + Redis + S3.
- **OpenLLMetry / OpenTelemetry GenAI SIG** — the emerging vendor-neutral standard (GenAI SIG formed April 2024, conventions still in Development status). Key namespace `gen_ai.*`: `gen_ai.request.model`, `gen_ai.provider.name`, `gen_ai.usage.input_tokens`, `gen_ai.usage.output_tokens`, and metric `gen_ai.client.token.usage` (histogram). Datadog natively maps these (v1.37+). This is what lets a LangChain-agent span look identical to a raw OpenAI call, enabling cross-provider dashboards.

**Key nuance — inclusive vs exclusive token counts.** Providers report differently: OpenAI's `prompt_tokens` *includes* cached tokens; Anthropic's `input_tokens` *excludes* cache reads/writes. Langfuse converts inclusive counts into mutually-exclusive buckets before storing. Any accurate cost ledger must normalize these. Also: for reasoning models, if no token counts are ingested, cost cannot be inferred (reasoning/thinking tokens are invisible unless the provider reports them) — always ingest provider-reported usage.

**Best practices for budgeting/caps in agentic loops.** Track cost at the trace level (whole agent task), attribute via tags/virtual keys per run/task/workspace, set separate budgets for interactive dev / CI / reviews / experiments, enforce hard caps at the gateway (LiteLLM budgets; Anthropic workspace spend limits; per-workspace rate limits), and change one lever at a time (clear context, compact, switch model, split task) then re-measure. The gateway is the enforcement point; the observability tool is the explanation point — production stacks run both.

**Build guidance.** (i) Emit OTel GenAI spans from every agent call with `run_id`, `task_id`, `workspace_id`, and `pipeline_stage` as span attributes. (ii) Route all three CLIs through a LiteLLM gateway for unified budgets and fallback, while also capturing native CLI telemetry. (iii) Store a per-run token ledger (input/output/cached/thinking, normalized to exclusive buckets) and reconcile nightly against provider Usage/Cost APIs. (iv) Enforce budget caps at the gateway with graceful degradation (downgrade model, reduce context) before hard failure. (v) Attribute LLM spend to the pipeline it built, so you can compute total cost-to-build-a-pipeline (agent tokens + compute).

#### 1b. Pipeline / compute layer (FinOps for data)

- **Databricks** bills in DBUs; the canonical attribution join is `system.billing.usage` × `system.billing.list_prices` (list prices live in the system table, not an external price book). Tag every SQL Warehouse, Job, and Pipeline at create time with `cost_center`, `env`, `owner_email`, `data_product` — using Unity Catalog *governed tags* with enforced allowed values, because "a beautiful cost mart with 40% of rows tagged UNATTRIBUTED is a social problem, not a SQL problem." Control cost with Compute Policies (`dbus_per_hour` range, `max_clusters_by_user`), Budgets (alert on tag/workspace spend), auto-termination, and Jobs Compute instead of All-Purpose (interactive clusters can waste 40%+ of spend). dbt query tags (`system.query.history`) enable per-model attribution — in one Databricks reference project four mart models accounted for 92% of compute time, invisible without tags.
- **Snowflake** bills in credits: virtual warehouse compute (60–80% of spend), serverless (Snowpipe, auto-clustering, MVs), and cloud services (free up to 10% of daily warehouse usage). Levers: right-size warehouses, aggressive `AUTO_SUSPEND`, isolate BI/ELT/ad-hoc workloads, `min_cluster_count=1` for multi-cluster, resource monitors with hard caps, ECONOMY scaling to avoid thrashing. Attribute via `ACCOUNT_USAGE.WAREHOUSE_METERING_HISTORY` and `QUERY_HISTORY` (bytes_scanned, credits_used).
- **BigQuery** bills on-demand by bytes scanned or in capacity mode by reserved slots (Dremel engine). Partitioning + clustering reduce scanned bytes; avoid `SELECT *` in scheduled jobs so schema growth doesn't inflate scans. Partition filtering can cut query cost up to ~40%.
- **FinOps discipline** — per the FinOps Foundation (finops.org/framework/phases): "The FinOps journey consists of three iterative phases: Inform, Optimize and Operate." Scope now explicitly spans "SaaS platforms, data cloud platforms like Snowflake and Databricks, data centers, and AI infrastructure and workloads" (2026 framework refresh). Third-party tools (Finout, CloudZero, Flexera/Chaos Genius, Vantage) add virtual tagging and cross-cloud TCO (joining AWS CUR with Databricks system tables — noting CUR reprocesses multiple times/day with no primary key).

**Build guidance.** The agent should tag every resource it provisions, expose a per-pipeline cost estimate *before* run (bytes-to-scan / projected DBUs), and surface a unified cost ledger combining agent tokens + warehouse spend per data product. Cost-awareness belongs in *architecture* decisions (don't design sub-second ingestion when daily freshness suffices).

### 2. Data Ingestion

**Batch vs streaming, ETL vs ELT.** The modern default is ELT (load raw, transform in-warehouse) enabled by cheap cloud storage and MPP engines; it preserves raw data for future use cases. Streaming is chosen only when latency requirements genuinely demand it — it carries the highest cost.

**CDC and tooling landscape.**
- **Debezium** — reference log-based CDC; reads transaction logs (binlog/WAL) and emits row-level change events to Kafka in <1s from commit. Maximum control and no per-row metering (Apache 2.0), but carries Kafka/Kafka Connect operational weight. Best for durable, multi-consumer replication backbones and event-driven architectures. Maintains a schema history topic for DDL changes.
- **Fivetran** — fully managed ELT; log-based CDC where supported, priced per Monthly Active Row (MAR). Fastest time-to-first-sync, handles schema drift/normalization automatically, targets warehouses (Snowflake/BigQuery/Redshift/Databricks). Latency seconds-to-minutes. Cost rises with high-churn tables.
- **Airbyte** — open-source; per Airbyte's connector catalog: "600+ Replication Connectors and counting, 50+ Agent Connectors." Debezium-based CDC for Postgres/MySQL/SQL Server/MongoDB; self-host for control or cloud. Airbyte docs classify connectors as Airbyte-maintained "Certified" (roughly the top 50–80 sources) versus community "Marketplace" connectors "not maintained by Airbyte" — so quality varies and a broken community connector may require cracking open source code.
- **Others**: AWS DMS (AWS-native migrations), Estuary Flow (streaming-first, claimed sub-second, exactly-once), Striim/Qlik Replicate (enterprise Oracle/SAP), BladePipe/Streamkap (managed low-latency CDC without Kafka ops).

**Selection heuristic.** Kafka expertise + streaming backbone → Debezium. Many SaaS sources → Fivetran/Airbyte (connector breadth). AWS-native migration → DMS. Sub-second + no infra → managed streaming CDC. SaaS apps don't expose logs, so "CDC" there is really API-based incremental replication.

**Correctness concerns.** Handle deletes explicitly (not all tools capture hard deletes — soft deletes / tombstones / filtering). Plan the initial full load separately from ongoing CDC. Watch schema drift (some tools auto-evolve, others break on new columns). Model costs before committing (usage-based pricing spikes on high-change tables). Don't use CDC when hourly batch suffices.

**Build guidance.** The agent should classify each source (DB with logs / SaaS API / files / event stream) and downstream SLA, then select ingestion mode and idempotency strategy. Land everything append-only in bronze with `_ingested_at` and `_batch_id` metadata columns (the debugging lifeline). Never filter in bronze — a WHERE clause creates a permanent gap in the historical record.

### 3. Data Warehousing & Medallion Architecture

**The layers.** Bronze = raw, append-only, immutable replay point (schema-on-read, no transforms). Silver = cleaned, deduplicated, typed, conformed (where most bugs hide — invest in testing type-casting, dedup, null handling; where MERGE/upsert and SCD logic live). Gold = business-ready, aggregated, joined, purpose-built per consumer (dashboard/feature store/report). "Bronze is where you trust the source, silver where you trust the data, gold where you trust the business definition." Resist "platinum/diamond" layers — three is the right number.

**Table formats.**
- **Delta Lake** — default on Databricks/Fabric; ACID, time travel, schema evolution, Change Data Feed for incremental downstream, deletion vectors (efficient row-level deletes), Liquid Clustering (replaces partitioning + ZORDER with adaptive multi-dimensional clustering, keys redefinable without rewriting).
- **Apache Iceberg** — best multi-engine interop (Spark/Flink/Trino/Snowflake/BigQuery read natively); hidden partitioning, partition evolution without rewrite. Gaining ground for cloud-agnostic setups.
- **Apache Hudi** — built around upserts from day one; best for high-frequency CDC. Copy-on-Write vs Merge-on-Read is a first-class per-table decision.
- **CoW vs MoR tradeoff (format-agnostic):** copy-on-write rewrites whole files (write amplification on update-heavy silver); merge-on-read trades cheaper writes for slower reads (better for streaming/change-heavy layers). Both need scheduled compaction. Read-mostly gold is fine on CoW.

**Maintenance is mandatory.** Small files and snapshots pile up at bronze/silver; run compaction (Delta `OPTIMIZE`, targets ~128MB–1GB files), snapshot expiry, and clustering. The "small files problem" wastes time on per-file overhead.

**Build guidance.** Agent scaffolds all three layers with the chosen format, sets append-only bronze, schema enforcement at silver, atomic-overwrite gold; schedules compaction/OPTIMIZE + ZORDER on high-cardinality filter columns; validates each layer against a distinct consumer and guarantee (skip bronze only when no immutable replay is needed). Map dbt: sources→bronze, staging→silver, marts→gold.

### 4. Transformation Tooling

**dbt Core CLI.** dbt is "a compiler and workflow layer for analytics code" — it turns a folder of fragile SQL scripts into a dependency graph with tests, docs, and reviewable changes. A dbt project is a graph: **sources** (raw edges, with freshness checks), **models** (`ref()`/`source()` transformations), **tests** (assertions), **snapshots** (SCD state), **macros** (reusable Jinja/SQL), **exposures** (downstream consumers), and a **semantic layer** (shared metric definitions).
- **Materializations**: view (stored query, no storage), table (full rebuild, best query perf), incremental (process only new/changed rows via `is_incremental()` + `unique_key`), ephemeral (inlined CTE), materialized_view (auto-refreshed). Snowflake uses Dynamic Tables instead of MVs.
- **Incremental strategies**: append (immutable data), merge (rows change; `unique_key` resolves duplicates and mirrors SCD1), delete+insert, and microbatch (large time-series, processes in batches by `event_time`). Incremental models are stateful — the easiest place to break idempotence. Use them only when runs get too slow, not by default.
- **Late-arrival budget**: a lookback window (`where updated_at >= dateadd(day,-3,max(updated_at))`) is "an explicit late-arrival budget" — size it to how long the source can mutate records, or partition to rebuild affected windows.
- **Snapshots** for SCD2 (dimensions changing in-place: customer tier, plan, owner) — rely on a stable unique key and trustworthy change signal; not a universal audit log.

**Orchestration — Airflow vs Dagster vs Prefect.**
- **Airflow** — the default; largest ecosystem (1000+ providers), widest talent pool, battle-tested at scale (Airbnb runs tens of thousands of DAGs). Airflow 3 (GA 2025) added asset-aware scheduling, DAG versioning, multi-team deployments; 3.1 added Human-in-the-Loop operators; 3.2 (2026) added asset partitioning. OpenLineage built in. Heaviest ops footprint (use managed: MWAA/Composer/Astronomer). Task-centric.
- **Dagster** — asset-centric ("software-defined assets"); lineage and quality are first-class, can skip re-materializing unchanged downstream assets. Best for dbt-heavy modern stacks and, critically, **the most AI-agent-friendly architecture** (assets, partitions, asset checks give agents a structured surface; `dagster-io/skills` for Claude Code/Codex; column-level lineage auto-derived for dbt). Steeper learning curve, smaller ecosystem (per Data Vidhya, 2026: "Dagster's GitHub repo has around 12,000 stars as of early 2026, compared to Airflow's 38,000+"; the apache/airflow topic page now shows ~45.8k).
- **Prefect** — Pythonic (`@flow`/`@task`), dynamic runtime-built graphs, lightest ops, fastest script-to-production. Smaller ecosystem; Marvin 3.0 is its first-party agent framework.
- **Verdict for this platform:** Airflow if you need ecosystem/talent/stability; **Dagster for a greenfield agentic platform** because the asset model gives lineage + quality "for free" and is the friendliest surface for agents to reason over.

**DAG best practices.** Idempotent tasks, sensors for dependencies, dynamic DAG generation, backfills, retries with exponential backoff, and clear task boundaries. Fivetran/dbt are *execution tools the orchestrator triggers* — the orchestrator triggers ingestion, waits for success, then triggers dbt, then ML/reverse-ETL.

**Build guidance.** The agent generates dbt projects (staging→marts), writes tests on every primary key (unique + not_null), documents incremental late-arrival/delete assumptions, and produces reviewable, version-controlled code. It should generate Dagster assets (or Airflow DAGs) with explicit dependencies, run column-level CI validating only changed models' downstream, and open PRs enriched with model diffs + lineage impact.

### 5. Data Quality & Contracts

**Quality dimensions**: completeness, accuracy, timeliness, consistency, uniqueness, validity.

**Tooling.**
- **Great Expectations** — expectation suites (codified, version-controlled rules), checkpoints, data docs; ExpectAI auto-generates expectations. Expectation suites serve as codified data contracts.
- **Soda / Soda Core** — dedicated data-contracts engine: declares expected schema (tables/columns/types), required vs optional fields, quality rules (uniqueness, ranges, allowed values), plus business semantics. AI copilot generates contracts from plain English; Autopilot derives contracts from production data. Enforces continuously in pipelines/CI so breaking changes are caught before they spread.
- **dbt tests** — assertions on the transformation layer (schema tests + custom).
- **Pandera** — dataframe schema validation (Python/pandas).
- **Monte Carlo / Anomalo** — ML-driven anomaly detection (freshness/volume/distribution).

**Data contracts** = formal producer↔consumer agreements on schema + semantic guarantees. The missing layer in most contracts is **execution** — "a contract that cannot be enforced is not a contract, it is documentation with good intentions." Enforce them shift-left at the producer, in pipelines and CI: violations block deployments, stop pipelines, or trigger alerts. Combine contracts (prevent structural/rule breaks) with observability (catch freshness/volume/distribution drift). Streaming uses schema registries (Avro/Protobuf/JSON Schema) for backward/forward compatibility.

**Build guidance.** The agent auto-generates contracts from profiled source data (there is active research on AI-driven contract generation), enforces them at ingestion (quality gates at API/extract points) and at each layer boundary, and treats a contract violation as a first-class pipeline event with routing to the owner.

### 6. Production Edge Cases & Failure Modes

**Schema drift/evolution.** The most common cause of pipeline breakage. Defenses: schema registry with compatibility enforcement (streaming), schema-on-write enforcement at silver, table-format schema evolution (add columns without rewrite), dbt `on_schema_change`, and observability schema monitoring that alerts on column add/remove/type change. Bronze's immutability + time travel lets you reprocess after fixing downstream logic.

**Pipeline failures / exactly-once.** Spark Structured Streaming achieves exactly-once via three components together: replayable idempotent source (Kafka offsets tracked in checkpoint, *not* Kafka's consumer-group offsets), idempotent/transactional sink (Delta MERGE or ACID commit; Kafka idempotent producer `enable.idempotence=true`), and checkpointing/WAL. Never change a running query's checkpoint location. Kafka EOS costs ~2–5ms latency and 10–20% throughput.

**Late/out-of-order data.** Use watermarks (`withWatermark`) on event-time (not processing-time) before any windowed aggregation; `dropDuplicatesWithinWatermark` for bounded-state dedup. **Critical failure mode**: watermark = min across partitions, so a single stalled/dead Kafka partition freezes the global watermark, no windows finalize, and state grows unbounded — alert when watermark lag exceeds 2–3× the late-arrival threshold.

**Duplicates / poison records / partial failures.** Dead-letter queue pattern is non-negotiable: inside `foreachBatch`, split valid vs failed records, route deserialization failures to a DLQ topic/blob instead of stopping the pipeline. Use unique event IDs + idempotent writes; `failOnDataLoss=true` to avoid silent loss; retries with exponential backoff.

**Observability & lineage.** Five pillars: freshness, distribution, volume, schema, lineage. **OpenLineage** is the vendor-neutral runtime lineage standard (Airflow, Spark, dbt, Dagster, Flink emit events); backends include Marquez, DataHub, Amundsen. **DataHub** ingests OpenLineage + does column-level lineage via SQL parsing, enabling blast-radius questions ("what breaks if I rename users.email"). Column-level, cross-platform, runtime-captured lineage is the bar — it turns incident response from "manual archaeology" into a graph query answerable in minutes.

**Data SLAs/SLOs & incident response.** Borrow from SRE: SLI (measured value) → SLO (internal target, tighter) → SLA (external commitment with consequences). Error budgets self-regulate (burn too fast → freeze new pipelines, focus on reliability). Key metrics: data downtime = incidents × (TTD + TTR), freshness SLO compliance (leaders hit 99.5–99.9%). Start monitoring the 3–5 pipelines where downtime hits revenue/compliance — don't monitor everything at once (alert fatigue kills adoption).

**Build guidance.** The agent should generate DLQ routing, idempotent sinks, watermark config, and quality gates by default; emit OpenLineage events for every asset; attach SLOs to gold tables; and on failure, use lineage for automated root-cause and owner routing (the "policy-bounded agentic" pattern from recent literature: agent reasoning constrained by an explicit governance framework, generating reviewable remediation rather than unconstrained action).

### 7. Data Modeling

- **Kimball (dimensional / star schema)** — bottom-up, business-process-driven; fact tables (events at a declared **grain** — the most important design decision, "one row per order line item") surrounded by denormalized dimension tables. Fast, intuitive queries; the pragmatic default for BI, pairs naturally with dbt. Use integer surrogate keys on dimensions (store natural keys as attributes); create "Unknown"/-1 rows to avoid null FKs; prefer star over snowflake (storage cheap, joins expensive). **SCD types**: 0 (never changes), 1 (overwrite, no history), 2 (new row + surrogate key + effective dates = full history, the workhorse), 3 (previous-value column), 4 (mini-dimension), 6/7 (hybrids). SCD2 requires a new surrogate key per change; declaring history intent up front is structural, not a drop-in later.
- **Inmon (3NF EDW)** — top-down normalized enterprise warehouse first, then derived dimensional marts. Cross-domain coherence and single source of truth at the cost of upfront effort and patience; best for large regulated enterprises.
- **Data Vault 2.0** — hubs/links/satellites; built for resilience and incremental change when sources mutate frequently (new payment gateway, changed policy). High governance maturity via immutable historization; template-driven low per-component complexity; requires a separate analytical (dimensional) layer for consumption. Often the integration layer feeding Kimball marts.
- **One Big Table / wide tables** — fully denormalized; leverages MPP columnar engines and cheap storage to skip joins. A convenient serving layer for a specific dashboard (usually *derived from* a dimensional model, not a replacement) or feature tables for ML. The "snapshot dimensions via table partitions" approach (used at Facebook/Airbnb/Lyft) sidesteps SCD engineering.

**Mapping to medallion / downstreams.** Silver ≈ conformed entities; gold ≈ dimensional marts (BI), OBT/feature tables (ML), or regulatory reports. Modeling choice is driven by the consumer: BI wants stars, ML wants wide reproducible feature tables, ad-hoc wants OBT. Most teams lean Kimball, often hybrid with OBT or Data Vault.

**Build guidance.** The agent should ask/infer the downstream consumer and pick the model accordingly; always declare and document fact grain; enforce grain + referential-integrity tests in dbt; use window functions to build SCD2 valid-from/valid-to ranges; handle source deletes explicitly (soft-delete flag).

### 8. Query Optimization

**Universal principle: read less data.**
- **Partition pruning** — skip partitions via predicates known at compile time; **dynamic partition pruning** (Spark 3+, on by default) prunes at runtime for star-schema fact/dimension joins (DPP = predicate pushdown + broadcast hash join). Databricks adds **dynamic file pruning** at file level.
- **Predicate pushdown** — filter at the data source ("bare metal"); works on Parquet/Delta/JDBC, not text/JSON. Apply filters right after table reads, before joins. Broken by `SELECT *` (defeats column pruning) and UDFs (black boxes to Catalyst).
- **Join strategies** — broadcast small tables (broadcast hash join) to avoid shuffle; sort-merge for large-to-large; bucketed joins for predictable distribution.
- **Columnar formats + statistics** — Parquet/Delta; Delta collects stats on first 32 columns (`dataSkippingNumIndexedCols`).
- **Clustering/Z-ordering** — Z-order co-locates data on high-cardinality filter/join columns (not low-cardinality dates — those are partition columns); Delta Liquid Clustering is the modern adaptive replacement.
- **File compaction** — `OPTIMIZE` (bin-packing to ~128MB–1GB), optimizeWrite, autoCompact.
- **Caching** — Snowflake's three caches: result cache (24h, zero compute on identical query/unchanged data), local disk/warehouse cache (lost on suspend — the auto-suspend tradeoff), metadata cache (MIN/MAX answered without scan). Spark `.cache()` for reused DataFrames.
- **Cost-based optimizer + AQE** — Spark Catalyst (Analysis→Logical Opt→Physical Planning→Codegen) + Adaptive Query Execution; tune shuffle partitions (default 200 is often wrong; ~2–3× total cores, or use AQE). Photon (vectorized engine) accelerates scan/join/aggregation transparently.

**Engine specifics.**
- **Spark/Databricks** — Spark UI to find the bottleneck; enable AQE + Photon + adjust shuffle partitions for ~80% of wins; watch data skew (uneven partitions); `repartition` (shuffle) vs `coalesce` (no shuffle).
- **Snowflake** — micro-partitions auto-pruned via metadata (skips 90%+ on selective queries); clustering keys on frequent WHERE/JOIN/GROUP BY columns cut scanned data 50–70%; MVs; check Query Profile for partitions-scanned ratio and disk spillage; scale up warehouse when spilling to remote storage.
- **BigQuery** — partitioning + clustering reduce scanned bytes (up to ~40–90%); slots/reservations for concurrency; avoid `SELECT *`.
- **DuckDB** — embedded columnar engine, excellent for local/single-node analytics and agent-side data inspection.

**Optimize for the goal.** Faster latency → caching, MVs, larger warehouse, Z-order/clustering, serverless SQL (eliminates cold start for BI). Lower cost → aggressive pruning, smaller/auto-suspended warehouses, incremental models, fewer bytes scanned. Higher throughput → right-sized parallelism, compute-optimized clusters, batch over stream.

**Build guidance.** The agent should analyze query patterns (from `query.history`/`QUERY_HISTORY`), choose partition/cluster keys from actual filter/join columns, verify pruning via query profiles / `explain`, and recommend materialization changes — presenting each as a latency-vs-cost tradeoff.

### 9. Downstream-Specific Design

- **Analytics/BI** — dimensional gold marts, pre-aggregated for sub-second dashboards; serverless SQL for concurrency; result caching for fixed-date dashboards.
- **Data warehousing** — conformed dimensions, single source of truth, SLA-bound gold tables.
- **ML / data science** — **feature stores** (Feast, Tecton, Hopsworks, Databricks Feature Store, Vertex AI Feature Store) solve train/serve skew by unifying online + offline access and guaranteeing **point-in-time correctness** (never train on future info). Offline store = training data + batch scoring + reproducibility; online store = low-latency inference. **Reproducibility** requires a data-arrival cutoff discipline (out-of-order data makes re-run training queries non-deterministic — define a cutoff a few hours after event time and fail loud if data arrives later) and backfills when a new feature view is deployed. dbt is increasingly the feature-engineering/governance layer (versioned, tested, contract-bound feature tables); MLflow tracks params/metrics/artifacts for lineage. ML often reads from silver (raw enough), BI from gold.

**Build guidance.** The agent should route the same silver data to consumer-specific gold: stars for BI, point-in-time-correct wide feature tables for ML (with cutoff + backfill logic), OBT for specific dashboards. Reproducibility for ML is a hard requirement — version data and enforce cutoffs.

### 10. Tunable / Demand-Driven Pipelines

The core insight: **latency, cost, freshness, and accuracy are competing objectives that should be explicit, tunable parameters** rather than hard-coded. Architectural patterns that support this:
- **Trigger/incremental spectrum** — continuous streaming (lowest latency, highest cost) → triggered incremental → scheduled batch. Delta Change Data Feed lets gold update from only what changed in silver.
- **Event-driven vs scheduled** — trigger silver/gold when bronze completes (asset-aware orchestration) rather than fixed schedules, reducing latency without full streaming.
- **Lambda/hybrid** — batch path (correctness/backfill) + streaming path (low latency) merged at gold, for mixed SLAs.
- **Materialization choice as a cost/latency dial** — view (fresh, compute-on-read) vs table (fast read, rebuild cost) vs incremental (balance) vs MV (auto-refresh).
- **Warehouse sizing / query acceleration** as throughput/latency dials.

Snowflake's own guidance: match ingestion service (Snowpipe vs Snowpipe Streaming vs batch) to the freshness need; match transformation frequency to SLA; "usage should drive your modeling requirements, not the other way around."

**Build guidance.** Expose a pipeline "objectives" config (e.g., `freshness_sla`, `max_cost_per_run`, `latency_target`) that the agent maps to concrete levers (trigger interval, warehouse size, incremental strategy, materialization, partition/cluster keys). When a user demands "faster analytics," the agent should present the explicit tradeoff (higher cost or lower freshness window) and the specific levers it will pull, then measure before/after. This is the demand-driven pattern that makes the platform genuinely tunable.

## Recommendations

**Stage 1 — Foundations (weeks 1–4).**
1. Stand up unified cost telemetry: OTel GenAI export from all three CLIs → one backend (Langfuse for traces + Helicone/LiteLLM gateway for budgets); enable Databricks `system.billing` + governed tags. Build a per-run ledger joining agent tokens + DBUs. **Threshold to advance:** every agent run and every pipeline has attributable cost with <10% UNATTRIBUTED.
2. Adopt medallion on Delta (or Iceberg if multi-engine), dbt Core for transformation, and Dagster for orchestration (asset model = agent-friendly). Enforce append-only bronze with metadata columns.
3. Enforce data contracts at the producer (Soda or GE) on your 3–5 revenue/compliance-critical pipelines; wire dbt tests on all primary keys.

**Stage 2 — Reliability (weeks 5–10).**
4. Emit OpenLineage from every asset → DataHub for column-level lineage + blast-radius impact analysis. Attach data SLOs (freshness/volume) with error budgets to gold tables.
5. Generate DLQ routing, idempotent/exactly-once sinks, watermarks, and schema-evolution policies by default. Add anomaly detection (Monte Carlo/Anomalo or Soda) for freshness/volume/distribution.
6. Implement budget caps at the gateway with graceful degradation (model downgrade / context reduction before hard fail).

**Stage 3 — Tunability & autonomy (weeks 11+).**
7. Expose the objectives config (latency/cost/freshness/accuracy) and map to engine levers; make the agent present tradeoffs before acting.
8. Adopt policy-bounded agent autonomy: agents generate reviewable code (dbt/SQL/DAGs), run column-level CI on changed models, require lineage impact analysis before deploy, and route incidents to owners — never unconstrained production mutation.
9. Add cost/query optimization agents (partition/cluster recommendations from query history, materialization tuning).

**Benchmarks that change the plan.** If UNATTRIBUTED cost >10% → invest in governed tags before anything else. If data downtime (incidents × (TTD+TTR)) is rising → freeze new-pipeline generation, stabilize. If freshness SLO compliance <99.5% on critical tables → tighten monitoring/error budgets. If agent token spend per pipeline exceeds compute cost → optimize prompts/context/model routing before scaling.

## Caveats
- **Fast-moving landscape.** Orchestrator features, LLM observability tools, and pricing change quarterly (Mintlify acquired Helicone on March 3, 2026; Airflow 3.2 and Dagster/Prefect agent features shipped 2025–2026). Verify current capabilities before committing.
- **Cost numbers are estimates.** LiteLLM/Langfuse/Helicone derive cost from price tables that lag; **always reconcile against provider invoices / Usage APIs** as the financial source of truth. Note the Claude Code figure itself was raised from $6 to $13/developer/active day; per Business Insider (April 2026) Anthropic stated "There was no pricing or product change... with Opus 4.7 now the frontier model in Claude Code, we updated the figures to reflect how usage has evolved" (the $6 figure reflected Sonnet 3.7-era usage). CDC monthly-cost estimates are similarly third-party figures with wide variance.
- **Gemini token-accounting inconsistency is real:** `candidatesTokenCount` includes thinking tokens on the Gemini API but excludes them on Vertex AI; a reported `gemini-3-flash-preview` bug may omit `thoughtsTokenCount`; and docs conflict on whether cached tokens are included in `totalTokenCount` (JSON examples indicate inclusive). Verify empirically per endpoint before building billing on these fields.
- **Vendor-sourced comparisons carry bias.** Fivetran vs Airbyte, Soda vs GE, and CDC "best tool" lists are often authored by vendors ranking themselves first; the workload-driven heuristics above are more reliable than any single vendor's framing.
- **Agentic data engineering is nascent.** Most failed projects fail because the agent operates on bare metadata (no lineage, ambiguous names, no quality signals). The prerequisite for agent autonomy is the governed-context foundation (lineage + contracts + tests) in Stages 1–2 — do not skip to Stage 3.
- **Medallion Silver/Gold boundary is under-specified** by design; treat layer names as shared vocabulary, not a rigid spec, and validate each layer against a distinct consumer and guarantee.
