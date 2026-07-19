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

### Scoped-area N/A — SIGNED OFF (conditional), 2026-07-18

- [x] **A7.3 — streaming event-time watermarks + windowed aggregation +
  watermark-stall alerting → N/A, conditional on batch-only ingestion; revisit if
  any streaming or event-time source is added.** Approved on batch-only grounds
  (watermarks are meaningless without event-time streaming), but recorded as a
  *conditional* N/A: "batch-only" is a current fact, not a permanent property, and
  the report covers streaming CDC as a likely future ingestion mode. A *batch*
  high-water-mark exists and is verdicted separately (A7 table). This is the only
  N/A in a Phase 1–4 scoped area.

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
| Watermarks on event-time + windowed agg + stall alert | **N/A** *(conditional)* | Platform is batch/file-based; no streaming windowed aggregation. Batch analog only: high-water-mark column (`load_strategy: append_watermarked`, `manifest.py:42-43`; `detect_watermark` `design_naming.py:63`). | Streaming watermark/stall pattern does not apply **while ingestion is batch-only**. Signed off 2026-07-18 as N/A *conditional on batch-only ingestion; revisit if any streaming/event-time source is added.* | Phase 3 |
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

## Gate — CLEARED 2026-07-18

No code changes were produced. Both human decisions are now made and recorded
below. Phase 1 work has **not** started; the 1a/1b spec is pending.

1. **A7.3 N/A — signed off (conditional).** See §1 checklist and the A7 table row.
2. **Phase 1–5 scope — set (companion doc `modern-data-engineering-report.md`
   committed alongside as the grading standard).** See "Scope decisions" below.

---

## Scope decisions (accepted 2026-07-18)

### Phase 1 splits in two (the halves are in different states)

- **Phase 1a — token capture, RETARGETED (near-greenfield).** Instrument the
  **outer/driving CLI agent's native telemetry, not `llm_engine.py`.** Claude Code
  and Codex via OTel/structured output; Gemini via `usageMetadata` on the API path
  (**verify the Vertex-vs-API `candidatesTokenCount`/`thoughtsTokenCount` split
  empirically per endpoint — do not trust the docs**). Persist a per-run ledger
  keyed by `run_id`/`workspace_id`/`pipeline_stage`, schema designed so surfacing
  it later is additive. Optionally capture `usageMetadata` in `APIEngine`
  (`core/agents/llm_engine.py:31-35`) as a bonus — but record explicitly that this
  meters the **out-of-scope loop/interns subsystem, not the platform.** (Matrix
  A1.1–A1.4, A1.6.)

  *Why this retarget (recorded so it is not re-litigated):* `llm_engine.py` serves
  `core/agents/` only (`registry.py:32-34`) — the loop/interns subsystem that P0.1
  severed from launch scope; grep returned **zero platform callers**. The launched
  platform is deterministic Python that delegates judgment to the outer CLI agent
  via the `cli_agent_proposal_needed` pattern (`blocker_question_panel.py:668`), so
  its real token spend **never touches `llm_engine.py`**. Instrumenting the shim
  would meter the out-of-scope subsystem and miss the in-scope one entirely. This
  also aligns with the companion report, which prescribes collection at the **agent
  telemetry layer** rather than via a wrapper.
- **Phase 1b — budget enforcement (wiring job).** Wire `BudgetTracker`
  (`core/medallion/budget.py`) into the build/loop and read
  `manifest.max_usd_per_run`. **Land it with a startup assertion that caps are
  actually active**, mirroring the `assert_installed()` pattern from P0.2. This is
  the *second* fully-built-but-dead safety module found in this codebase
  (redaction was the first) — "module exists and passes tests" being mistaken for
  "live" is treated as a **systemic pattern**, defended structurally, not a
  one-off. (Matrix A1.5.)

**Pulled forward into Phase 1:**
- Bronze `_batch_id` emission (the U1 emitter half) — already *declared*
  (`generation_workflow.py:741`), just not emitted; it is the debugging lifeline
  the report calls out. Add it to the emitters + reconcile the `_load_ts` vs
  `_ingested_at` name.
- `budget.py` wiring (Phase 1b above).

The other two cheap wins (freshness-breach→alert, PK-assertion coverage gate) stay
in **Phase 2**, where they naturally sit.

### Phase 2 — data contracts / quality (unchanged scope)
Matrix A5 (all four sub-items) + A3.1 (PK-assertion coverage gate) + A7.4
(schema-drift evolution policy) + freshness-breach→alert (A10.1/A5.4).

### Phase 2.5 — medallion/dbt maintenance + assertion scaffolding (NEW)
Bundle of related unscoped findings — the layer is structurally sound but missing
its maintenance/assertion scaffolding; individually small, done together:
- **U1 (remainder)** — `_load_ts` vs `_ingested_at` naming reconciliation
  (the `_batch_id` emission half moved to Phase 1).
- **U2** — scheduled compaction/VACUUM/snapshot-expiry; ZORDER/clustering.
- **U3** — row-level incremental (`unique_key`) + late-arrival lookback window
  (today: fingerprint table-skip only).
- **U5** — grain test-enforcement (today: grain declared, not asserted).

### Phase 3 — lineage (SHRUNK)
Column-level lineage already exists (`core/medallion/lineage.py`), so this is
**"emit OpenLineage over working internals" — a serialization layer, not a
build**. Rescoped: A6.1 (OpenLineage events) + A6.2 (backend, *if* required) only;
A6.3/A6.4 are already HAVE. Also owns the DLQ/idempotency Phase-3 items (A7.1).

### Phase 3.x (own scoped item) — U4: orchestration claims vs code (NEW, separate)
Assets with no `retry_policy`/`partitions_def`/backfill **while the docstring
claims them** (`dagster_defs.py:44`) is the same assumed-live pattern as
`budget.py` and the redaction module — a documentation claim the code doesn't
honor. Treated as a **correctness-of-claims** issue and given its own scoped item,
**not** folded into the Phase 2.5 bundle.

### Phase 4 — tunable objectives (CONFIRMED LAST)
Near-empty (A10, all PARTIAL) and dependent on Phase 1a's cost data. Nothing to
pull forward.

### Phase 5 — query optimization (backlog, unchanged)
Matrix A9 (all three sub-items).

---

## Systemic pattern flagged for owners

Five related "the claim and the reality diverge" cases have now been found:
1. `install_log_redaction()` — built + tested, never called (fixed in P0.2).
2. `BudgetTracker` (`budget.py`) — built, never called (Phase 1b).
3. Orchestration retries/backfill — claimed in docstring, absent in code (U4).
4. **Token capture retarget (Phase 1a)** — a capability *audited as present in the
   wrong subsystem*. `llm_engine.py` was the natural place to look for agent-token
   cost, but it serves the out-of-scope loop, not the launched platform.
5. **The join key that doesn't join (Phase 1a.1)** — the cost ledger's anchor was
   about to key on the platform's `session_snapshot` id, which is
   `sha256(workspace|tool|now)[:10]` (`tools/session_snapshot.py:1025-1028`) and
   never joins against anything the agent emits. Anchored on it, 1a.1 would have
   shipped complete, tested, and green with rows that could never be matched to a
   token count — found only in 1a.2 with the collector already up. The real join
   key is the agent-native `CLAUDE_CODE_SESSION_ID` from the environment. A
   mechanism that *looks* complete and is structurally inert — same family as
   built-but-not-wired.

6. **Coverage, not liveness — a distinct sub-family (Phase 1a.1).** The cost-ledger
   anchor is wired correctly, tested, gated, and green — and observes ~**1%** of
   the surface it exists to observe (seam-invoked runs are 1 of ~96 recorded
   invocations; ~99% of spend goes through individually-invoked `uv run` commands
   the seam never sees). Cases 1–5 are mechanisms *not wired*; this one *is* wired
   and still fails its purpose. **Live and sufficient are independent properties.**
   The liveness gate structurally cannot catch it — a run that never anchors has no
   rows to fail on — so the gap is invisible from inside the mechanism.

7. **The classifier that misclassifies the thing it most needed to catch (Phase
   1a.2a).** The exemption rule for which commands need no cost anchor was sound
   ("no `--workspace`, nothing to key"). But *deriving* the list mechanically —
   grep the entry-point module for a literal `--workspace` — misclassified
   `medallion` and `harness`, which dispatch `--workspace` to subcommands so the
   literal never appears at module level. It would have dropped `medallion` (the
   **#2 most-invoked command**) into the exempt bucket and passed everything:
   coverage green, exemptions all reasoned, nothing to catch it. The rule was
   right; the derivation of what satisfied it was wrong, and it failed on the
   highest-value item. Fixed by reading `--workspace` from argv generically +
   curating the list with a **decorate-when-uncertain** default.

"Module exists and passes tests" is being mistaken for "live" (cases 1–3, 5);
"the finding names a real mechanism" is being mistaken for "the mechanism is the
one in scope" (case 4); "the mechanism is live" is being mistaken for "the
mechanism covers its surface" (case 6); and "the derivation is sound" is being
mistaken for "the derivation classifies the high-value cases correctly" (case 7).
The target errors (4, 5) were caught by **empirical verification, not by reasoning
from the report** (grep for callers; check the env for the actual identifier); the
coverage error (6) by **measuring** the seam-vs-direct ratio (inference said "more
common," measurement said 99%); and the classifier error (7) by checking the
derivation against its **highest-value excluded item**, not a random sample. The
empirical rule paid for itself repeatedly. Five structural defenses:
- **For built-but-not-wired:** a startup assertion that the mechanism is actually
  active (`assert_installed()` from P0.2 is the template). Phase 1b lands one for
  budget caps; U4 audits the claim for orchestration; Phase 1a.1's
  `assert_ledger_active` fails a run that produced zero anchors.
- **For audited-against-the-wrong-target:** before building against an audit
  finding, confirm the finding refers to the subsystem actually in scope (grep for
  who calls it; check it against the launch-scope boundary, not just that the code
  exists).
- **For the join key / identifier:** verify the join key **empirically** before
  building against it — check the runtime/env for the actual value, don't assume
  two identically-named-looking ids are the same one.
- **For coverage:** measure a mechanism's **coverage**, not just its liveness — the
  fraction of its intended surface it actually observes/guards. A green, live
  mechanism at 1% coverage is a false comfort; a coverage test (below) is what
  keeps it from decaying by attrition.
- **For derivations that exclude:** when a derivation produces an exemption or
  exclusion list, verify it against the **highest-value items it excludes**, not a
  random sample — a classifier fails most expensively exactly on the case the list
  existed to protect.

Owners should treat any new safety/enforcement module as not-done until it has such
an assertion, any audit finding as unconfirmed until its subsystem is checked
against launch scope, any join key as unconfirmed until it is matched against the
real emitted value, any observer/guard as unconfirmed until its coverage of the
intended surface is measured — not just its liveness — and any exclusion list as
unconfirmed until its derivation is checked against the highest-value items it drops.
