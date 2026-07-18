# Phase 0 — Data-Engineering Gap Matrix

**Evidence-only audit. No code changes.** Maps the platform's actual capabilities
against *Modern Data Engineering for an Agentic, CLI-Based Data Platform*.

Produced by the local agent that ran the prior P0/P1 audits and made those commits
(same session, repo mounted). Per the handoff, that means no second-reader
spot-check of MISSING/N/A is required — but every such verdict below states the
exact search that makes it falsifiable.

Verdict legend: **HAVE** (implemented, matches report) · **PARTIAL** (exists +
named gap) · **MISSING** (searched, absent) · **N/A** (out of scope, justified).

---

## 1. Summary

### Counts (39 sub-capabilities)

| Verdict | Count |
|---|---|
| HAVE | 12 |
| PARTIAL | 18 |
| MISSING | 8 |
| N/A | 1 |

Per area: A1 cost `0H/2P/4M` · A2 medallion `2H/2P` · A3 dbt `2H/2P` · A4 orch
`2H/1P` · A5 contracts `0H/3P/1M` · A6 lineage `2H/0P/2M` · A7 failure
`1H/2P/0M/1N/A` · A8 modeling `3H/1P` · A9 query-opt `0H/2P/1M` · A10 objectives
`0H/3P`.

### The 3–5 findings that most change Phase 1–5 scope

1. **Agent-token cost is not captured at the source — Phase 1 is near-greenfield
   on the LLM side.** `APIEngine.generate()` reads only the response text and
   never touches Gemini's `usageMetadata` (`core/agents/llm_engine.py:31-35`);
   `CLIEngine` captures stdout only (`:84-88`). No OTel `gen_ai.*` anywhere
   (`grep -ri "gen_ai|opentelemetry|otel" core/` → 0). So there is no per-request
   token count to normalize, ledger, or reconcile. Everything in Phase 1's
   token-cost half starts from zero instrumentation.

2. **`budget.py` is fully built but dead code (the P0.2-redaction pattern
   again).** `BudgetTracker` implements a USD cap, graceful de-escalation, and a
   hard `BudgetExceeded` (`core/medallion/budget.py:26-75`), and
   `manifest.max_usd_per_run` exists (`core/medallion/manifest.py:23`) — but
   nothing imports or calls it (`grep -rn "BudgetTracker(|from core.medallion.budget|import budget" core/ tools/`
   → only the `.pyc`). Phase 1 must not assume budget caps exist; it can salvage
   the class, but wiring + real token inputs + replacing the local `PRICING`
   table (`:12-19`) with reconciliation is the actual work.

3. **Lineage is strong and column-level, but native — not OpenLineage.** A real
   column-level lineage graph exists (`core/medallion/lineage.py:40-61`,
   from_columns→to_columns + transform), with SQL/Spark lineage parsers and an
   evidence-graph query CLI. So Phase 3's lineage work is *not* "build lineage" —
   it is narrowly "emit OpenLineage from orchestrator runs + stand up a backend,
   **if** cross-platform/runtime-standard lineage is actually required." That can
   shrink or refocus Phase 3.

4. **Orchestration assets are bare — resilience is unconfigured.** Both a Dagster
   asset graph and an Airflow DAG render a shared topology
   (`core/orchestration/dagster_defs.py:44`, `airflow_dag.py`), but the assets are
   `@asset(name, deps, description)` with **no** `retry_policy`, `partitions_def`,
   or asset checks (`grep RetryPolicy|Partition|backfill` → only the docstring).
   Retries/backoff/backfill are claimed in prose, absent in code.

5. **Tunable objectives barely exist — Phase 4 is near-greenfield and depends on
   Phase 1 landing first.** The only real objective is a per-silver-table
   freshness `max_lag_hours` that is *report-only* ("Non-fatal: report, don't
   block", `core/medallion/design.py:1079`). `max_usd_per_run` is the unwired
   budget field; there is no `latency_target`, no unified objectives config, and
   no objective→lever mapping.

### Cheap-win PARTIALs (closest to done if a phase is pulled forward)

- **Bronze `_batch_id` (A2.1)** — emitters already write
  `_source_system`/`_source_file`/`_load_ts` (`core/medallion/delta_emitter.py:86-88`)
  and `generation_workflow.py:741` already *declares* `_batch_id` required. Add
  the column to the emitters and reconcile the `_load_ts` vs `_ingested_at` name.
- **Wire `budget.py` (A1.5)** — the tracker is complete; the small part is
  instantiating it in the build loop and reading `manifest.max_usd_per_run`. (The
  large part — real token inputs — is A1.2.)
- **Freshness breach → alert (A10.1 / A5.4)** — the lag-vs-SLA SQL with a
  `breached` boolean is already emitted (`core/medallion/design.py:1076-1091`);
  routing `breached` to an owner/alert instead of report-only is small.
- **PK-assertion coverage gate (A3.1 / A5.3)** — `not_null` + `unique` assertion
  types exist (`core/medallion/silver_contract.py:153-154`); a gate asserting
  every declared PK gets both is bounded.

### Scoped-area N/A proposed — HUMAN SIGN-OFF REQUIRED (single checklist)

- [ ] **A7.3 — streaming event-time watermarks + windowed aggregation +
  watermark-stall alerting → N/A.** Justification: the platform is batch/file-based
  (CSV → Delta/DuckDB); there is no streaming ingestion or windowed streaming
  aggregation, so the streaming-watermark/stall pattern does not apply. A *batch*
  high-water-mark exists and is verdicted separately (A7 table). This is the only
  N/A proposed in a Phase 1–4 scoped area; it is proposed, not self-approved.

*(No other scoped-area N/As. All other absences are MISSING/PARTIAL, i.e. in scope
and falsifiable.)*

---

## 2. Area tables

### Area 1 — Cost telemetry (agent tokens + compute) → Phase 1 *(scoped)*

| Sub-capability | Verdict | Evidence (file:line) | Gap detail | Routes to |
|---|---|---|---|---|
| OTel GenAI instrumentation for Claude Code / Codex / Gemini (`gen_ai.usage.*`) | MISSING | `grep -ri "gen_ai\|opentelemetry\|otel" core/` → 0 hits. Telemetry is MLflow-based (`core/observability/telemetry_backend.py:101`), Databricks-only, loop-subsystem only. | No OTel, no `gen_ai.*`; MLflow spans log char-lengths not tokens (`telemetry_backend.py:160-163`). | Phase 1 |
| Token counts normalized across providers (Gemini Vertex vs API split) | MISSING | `APIEngine.generate()` parses only `candidates[0]...text` (`core/agents/llm_engine.py:31-35`); never reads `usageMetadata`. `CLIEngine` captures stdout only (`:84-88`). | No provider token capture at all → nothing to normalize. | Phase 1 |
| Per-run ledger keyed by `run_id`/`workspace_id`/`pipeline_stage` | PARTIAL | `LocalTelemetry.end_run` logs `{run_id, metric, status, params}` to run log (`core/observability/telemetry_backend.py:72-82`); MLflow params/metrics when Databricks active. | Keyed by `run_name` only; **no token/cost columns**, no workspace/stage cost dimensions. It is an experiment-metric log, not a cost ledger. | Phase 1 |
| Compute-cost attribution to Databricks (`system.billing.usage` × `list_prices`, governed tags) | MISSING | `grep -ri "system.billing\|list_prices" core/` → 0 hits. | No warehouse compute-cost attribution or chargeback tags. | Phase 1 |
| Budget caps with graceful degradation | PARTIAL | `BudgetTracker` implements cap + `should_deescalate` + `BudgetExceeded` (`core/medallion/budget.py:26-75`). **Unwired**: `grep -rn "BudgetTracker(\|from core.medallion.budget\|import budget" core/ tools/` → only `.pyc`. | Dead code (built, tested, never called). Cost basis is a local `PRICING` table (`:12-19`) — the anti-pattern the report warns against. | Phase 1 |
| Reconciliation vs provider Usage/Cost APIs | MISSING | No provider billing API call anywhere; unwired budget computes from local price table only. | Cost, if ever computed, is never reconciled against invoices. | Phase 1 |

### Area 2 — Medallion layer enforcement → Unscoped

| Sub-capability | Verdict | Evidence (file:line) | Gap detail | Routes to |
|---|---|---|---|---|
| Bronze append-only, immutable, schema-on-read, `_ingested_at`/`_batch_id` | PARTIAL | Append-only (`core/medallion/delta_emitter.py:91` `.mode("append")`); metadata `_source_system`/`_source_file`/`_load_ts` (`:86-88`; duckdb `design.py:1390-1391`); bronze schema-flexible `mergeSchema` (`:94`). | Named `_load_ts` not `_ingested_at`; **no `_batch_id`** emitted — yet `generation_workflow.py:741` declares required `_batch_id`/`_ingestion_timestamp` and `pipeline_execution_harness.py:48` expects `_ingested_at`. Contract vs emitter inconsistent. | Unscoped |
| Silver: schema enforcement, typing, dedup, MERGE/upsert or SCD | HAVE | `type_casts`+`null_policies`+`dedup_keys`+`key_rename` (`core/medallion/silver_contract.py:206-216`); MERGE-on-PK `whenMatchedUpdateAll/whenNotMatchedInsertAll` (`delta_emitter.py:196-199`); duckdb `emit_silver_merge` (`build.py:538`). | Real upsert, not "bronze renamed." | — |
| Gold: atomic-overwrite, consumer-specific marts | HAVE | Spark gold `.mode("overwrite")` (`delta_emitter.py:239`); duckdb OBT fact×dims (`:250-286`) + SCD2 + full-refresh variants. | Consumer-specific (OBT for dashboards). | — |
| Table format + scheduled compaction/OPTIMIZE/snapshot-expiry | PARTIAL | Delta (Spark) + DuckDB (local). `OPTIMIZE` emitted inline on gold build when `storage_strategy.optimize` (`delta_emitter.py:41-45,224`); `partitionBy` volume-derived (`:32-38`). | OPTIMIZE only fires on gold rebuild (not scheduled); no VACUUM/snapshot-expiry; no ZORDER/liquid-clustering. | Unscoped |

### Area 3 — dbt / transformation tooling

> **dbt is not in the production path.** It exists only as a spike
> (`spikes/dbt_dagster/`: `dbt_project.yml`, `models/`, `validate_spike.py`) and is
> not a dependency (`grep dbt pyproject.toml` → none). Production transforms are
> native SQL (`core/onboarding/pipeline_sql_generator.py` → `pipeline_layers.sql`)
> + PySpark/Polars/DuckDB emitters. Verdicts below are on the *capability's native
> equivalent*.

| Sub-capability | Verdict | Evidence (file:line) | Gap detail | Routes to |
|---|---|---|---|---|
| PK tests (unique + not_null) on every PK; coverage % | PARTIAL | Native `Assertion` types `not_null` + `unique` + `referential_integrity` (`core/medallion/silver_contract.py:153-157`); run via `_run_assertions` (`build.py:879`). | No gate that *every* declared PK gets both; coverage % unmeasured/unenforced. | Phase 2 |
| Materialization choices deliberate | HAVE | Per-layer deliberate strategy: bronze append / silver MERGE / gold overwrite\|OBT\|SCD2 (`delta_emitter.py`). | Not dbt materializations, but deliberate. | Unscoped |
| Incremental models: `unique_key`, late-arrival lookback | PARTIAL | `incremental.py` is fingerprint-based table-level skip-if-unchanged (`core/medallion/incremental.py:1-13,234-292`). | **No row-level incremental (`unique_key`) and no late-arrival lookback window.** Table-skip only. | Unscoped |
| Snapshots for SCD2 dimensions | HAVE | Native `emit_scd2_merge` w/ valid_from/valid_to (`merge_emitter`, called `delta_emitter.py:291-302`); `scd_type` per dim (`star_schema.py:52`). | — | Unscoped |

### Area 4 — Orchestration → Unscoped *(OpenLineage emission feeds Phase 3)*

| Sub-capability | Verdict | Evidence (file:line) | Gap detail | Routes to |
|---|---|---|---|---|
| Which orchestrator + version | HAVE (optional) | Dagster (`core/orchestration/dagster_defs.py`) + Airflow (`airflow_dag.py`) render shared `STAGES` (`pipeline_stages.py`); plus sequential `run_pipeline()`. | Neither is a dependency (`pyproject`) — both import gracefully / not installed. No version pinned. | Unscoped |
| Asset-aware vs task-centric | HAVE | Dagster asset-aware `@asset(deps=...)` (`dagster_defs.py:44`); Airflow task-centric. | Both surfaces available. | Unscoped |
| Idempotent tasks, retries w/ backoff, backfill | PARTIAL | Assets are bare `@asset(name, deps, description)` (`dagster_defs.py:44`); idempotency comes from governed commands (fingerprint-skip, `incremental.py`). | **No `retry_policy`, `partitions_def`, or backfill config** (`grep RetryPolicy\|Partition\|backfill dagster_defs.py` → docstring only). Resilience unconfigured. | Unscoped |

### Area 5 — Data contracts / quality → Phase 2 *(scoped)*

| Sub-capability | Verdict | Evidence (file:line) | Gap detail | Routes to |
|---|---|---|---|---|
| Soda / Great Expectations / Pandera present | MISSING | `grep -ri "soda\|great_expectations\|pandera" core/` + `pyproject` → 0. | No third-party contract/quality engine. Native assertions only. | Phase 2 |
| Contracts enforced at ingestion (producer boundary) | PARTIAL | Source-family + catalog + intent contracts at onboarding (`source_family_contracts.py`, `catalog_contract.py`, `kpi/intent_contract.py`). | Enforcement is at **silver post-load** assertions, not the bronze/producer boundary (bronze is deliberately schema-flexible). | Phase 2 |
| Contracts enforced at each layer boundary | PARTIAL | Silver assertions `not_null/unique/RI/row_count_delta` (`silver_contract.py:149-167`) run in build (`build.py:879`). | Silver only. Bronze = no enforcement by design; gold = no dedicated contract found. | Phase 2 |
| Violations routed to owner/alert/ticket | PARTIAL | Assertion failures "route through the governor" (`silver_contract.py:7`) → medallion routing/blocker entries (`build.py` `_route`). | Surfaced as build blockers/panels, not owner/alert/ticket. No ownership or alerting. | Phase 2 |

### Area 6 — Lineage → Phase 3 *(scoped)*

| Sub-capability | Verdict | Evidence (file:line) | Gap detail | Routes to |
|---|---|---|---|---|
| OpenLineage events emitted from runs | MISSING | `grep -ri openlineage core/` → 0. | Lineage is a native JSON graph, not OpenLineage run events. | Phase 3 |
| Lineage backend (Marquez/DataHub/Amundsen) | MISSING | No external backend; lineage → `lineage.json` + evidence-graph artifacts (`core/medallion/lineage.py`, `core/onboarding/evidence_graph.py`). | Native artifacts serve the role; whether an external backend is *needed* is the Phase 3 decision. | Phase 3 |
| Column-level vs table-level | HAVE | Column-level: `LineageEdge` from_columns→to_columns + transform (`lineage.py:40-61`); `spark_lineage_parser.py` + `sql_lineage_parser.py`. | — | Phase 3 |
| Blast-radius answerable today | HAVE | `build-workspace-evidence-graph` + `query-workspace-evidence-graph` CLIs (`evidence_graph.py`) over the column-level graph. | Native/queryable; scoped to generated medallion SQL, not runtime cross-platform. | Phase 3 |

### Area 7 — Failure-mode handling

| Sub-capability | Verdict | Evidence (file:line) | Gap detail | Routes to |
|---|---|---|---|---|
| DLQ pattern for bad/poison records | PARTIAL | Batch **quarantine**: route null-key/dup records to quarantine instead of silver (`core/onboarding/data_model/data_understanding.py:600-603`; `data_quality.py:134,172`); `_is_quarantined` bronze metadata. | Quarantine is proposed in panels/design; verify it's emitted in every generated silver. No streaming DLQ (batch platform). | Phase 3 |
| Idempotent / exactly-once sinks | HAVE | Silver Delta MERGE idempotent upsert (`delta_emitter.py:196-199`); duckdb merge (`build.py:538`); fingerprint skip (`incremental.py`). | Streaming Kafka idempotent-producer N/A (no streaming). | Phase 3 |
| Watermarks on event-time + windowed agg + stall alert | **N/A** *(sign-off)* | Platform is batch/file-based; no streaming windowed aggregation. Batch analog only: high-water-mark column (`load_strategy: append_watermarked`, `manifest.py:42-43`; `detect_watermark` `design_naming.py:63`). | Streaming watermark/stall pattern does not apply. **Proposed N/A — Phase-3-scoped, needs human sign-off.** | Phase 3 |
| Schema-drift detection + evolution policy | PARTIAL | Detection: `schema_drift_columns`/`has_schema_drift` = union−common across source files (`source_family_contracts.py:82-84`); bronze `mergeSchema` additive (`delta_emitter.py:94`). | No explicit per-column evolution **policy** (fail/quarantine/accept) beyond bronze auto-append; no drift alerting. | Phase 2 |

### Area 8 — Data modeling → Unscoped

| Sub-capability | Verdict | Evidence (file:line) | Gap detail | Routes to |
|---|---|---|---|---|
| Pattern produced (Kimball/OBT/Data Vault/ad hoc) | HAVE | Kimball star (`core/medallion/star_schema.py` Fact/Dimension) + OBT gold (`delta_emitter.py:250-286`). | Deliberate, user-ratified. | Unscoped |
| Fact-table grain declared/documented | HAVE | `FactTable.grain` required one-sentence field + reasoning (`star_schema.py:21,25`); ratified via blocker panel (`:8-10`). | — | Unscoped |
| SCD handling — deliberate per dimension | HAVE | `DimensionTable.scd_type` 1\|2 per dim (`star_schema.py:52`); SCD2 emitter (`merge_emitter`). | Deliberate, ratified. | Unscoped |
| Referential-integrity / grain tests | PARTIAL | `referential_integrity` assertion (`silver_contract.py:157`) + star-schema relationships w/ evidence (`star_schema.py:87`). | Grain is *declared* not test-enforced; RI via native assertion, not dbt. | Unscoped |

### Area 9 — Query optimization → Phase 5 (backlog)

| Sub-capability | Verdict | Evidence (file:line) | Gap detail | Routes to |
|---|---|---|---|---|
| Partition/cluster-key selection (manual/automated) | PARTIAL | `partition_by` volume-derived automated (`storage_report.py` measures first; `design.py` storage_strategy; applied `delta_emitter.py:32-38`). | Partition only — no clustering/Z-order/liquid-clustering selection. | Phase 5 |
| Query history captured | MISSING | `grep -ri "query.history\|QUERY_HISTORY" core/` → no warehouse-history capture. `optimizer_finder.py` profiles SQL statically/at-run, not from history. | No query-history surface to mine. | Phase 5 |
| Auto-flag `SELECT *` / missing predicate pushdown | PARTIAL | `sql_lint` flags CROSS/Cartesian (`sql_lint.py:69`) + generic hotspots/suggestions via `optimizer_finder` (`:81`). | No specific `SELECT *` / missing-pushdown flag. | Phase 5 |

### Area 10 — Tunable objectives → Phase 4 *(scoped)*

| Sub-capability | Verdict | Evidence (file:line) | Gap detail | Routes to |
|---|---|---|---|---|
| Objectives config (`freshness_sla`/`max_cost_per_run`/`latency_target`) | PARTIAL | Freshness native: per-silver `max_lag_hours` freshness contract + breach report (`core/medallion/design.py:900,1076-1091`). `max_usd_per_run` in manifest (`manifest.py:23`, unwired). | No `latency_target`; no single unified objectives config; freshness is report-only; cost is dead code. | Phase 4 |
| Objective→lever mapping | PARTIAL | Evidence→lever exists for storage (volume→partition/compression/optimize, `design.py` storage_strategy). | Volume-driven, not objective-driven. No freshness/latency/cost → trigger-interval/warehouse-size/materialization mapping. | Phase 4 |
| Present a tradeoff before acting + measure before/after | PARTIAL | Format/design panels present choices (`prepare-pipeline-format-panel`; medallion design panels); `token-report` does before/after token compare (`tools/token_report.py:276-308`). | No latency-vs-cost-vs-freshness tradeoff surfaced before acting on an objective. | Phase 4 |

---

## 3. Unscoped findings — need an explicit "fix it / leave it" before Phase 1

These PARTIAL/MISSING items sit in areas with **no Phase 1–5 owner** (areas 2, 4,
8, and the unscoped items in area 3). They will not be picked up automatically.
(Area 9 items are Phase-5 backlog — owned, listed in the Area 9 table, not here.)

| # | Finding | Verdict | Evidence | Decision needed |
|---|---|---|---|---|
| U1 | Bronze `_batch_id` not emitted; `_load_ts` vs declared `_ingested_at`/`_ingestion_timestamp` naming inconsistency (emitter vs `generation_workflow.py:741`). | PARTIAL | `delta_emitter.py:86-88` vs `generation_workflow.py:741`, `pipeline_execution_harness.py:48` | Cheap win — add `_batch_id`, reconcile name. Fix or leave? |
| U2 | OPTIMIZE only on gold rebuild; no scheduled compaction/VACUUM/snapshot-expiry; no ZORDER/clustering. | PARTIAL | `delta_emitter.py:41-45,224` | Maintenance-on-schedule gap. Own it or defer? |
| U3 | No row-level incremental (`unique_key`) or late-arrival lookback window; only fingerprint table-skip. | PARTIAL | `incremental.py:234-292` | Real capability gap if sources mutate/late-arrive. Fix or leave? |
| U4 | Orchestration assets have no configured retries/backoff/backfill/partitions. | PARTIAL | `dagster_defs.py:44` | Resilience gap; also feeds Phase 3 (OpenLineage from orch). Own it? |
| U5 | Fact grain declared but not test-enforced; RI via native assertion, no dbt. | PARTIAL | `star_schema.py:21`; `silver_contract.py:157` | Add grain-enforcement test, or accept declaration-only? |

---

## Gate

No code changes were produced. Before Phase 1 starts, two human decisions:

1. **Sign off (or overturn) the single scoped-area N/A** — A7.3 (streaming
   watermark/stall alerting → N/A on batch-only grounds). Checklist in §1.
2. **Set real Phase 1–5 scope from this matrix** — including whether any Unscoped
   finding (U1–U5) needs a phase of its own.
