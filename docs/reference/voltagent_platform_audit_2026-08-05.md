# VoltAgent Platform Audit — Consolidated Findings (2026-08-05)

Three independent read-only reviews of the cloud-first platform by the installed
VoltAgent reviewers: `data-engineer` (pipeline), `database-optimizer` (storage/query),
`data-analyst` (consumption). Every issue below carries code evidence; items already
tracked in `docs/plans/2026-08-05-finish-cloud-first-restructure.md` were excluded by
instruction. Full per-agent reports are in the session record; this file is the merged,
deduplicated, severity-ranked list.

## A. ISSUES — exists, but flawed (ranked)

| # | Sev | Finding | Evidence | Fix direction |
|---|-----|---------|----------|---------------|
| A1 | HIGH | Marts are ALWAYS full-refresh (`materialized='table'`), and WAP publish does a SECOND full `CREATE OR REPLACE ... AS SELECT` copy — every scheduled run rewrites every mart twice, forever. The incremental-model validation block is dead code guarding a materialization the generator never emits. | `dbt_project_generator.py:265-274` (emit), `:888-935` (dead validator), `:591-634` (double-copy publish) | Emit `incremental` for marts whose grain/event-time allows it (own research row 5); make publish swap metadata-only (RENAME/clone), not a second copy |
| A2 | HIGH | JDBC ingestion emits unconditional `.mode("append")` with no watermark/upsert — any retry or re-trigger duplicates the whole source table. Only defense is a code comment. `validate_generated_project` never inspects `ingestion/`. | `core/provisioning/ingestion.py:339-350` | Derive watermark/key from discovery and emit merge-by-default, or refuse to emit without one (same refuse-over-unsafe convention as the dbt emitters) |
| A3 | HIGH | `optimization_playbook.yaml`'s 29 rules have ZERO production callers — nothing reads query history/spill/skew/queue metrics to populate `consult()`'s inputs. The detect half of the design is unwired; only tests import it. | `core/blueprint/playbook.py` (sole importer: its test); `core/observability/warehouse_cost.py` feeds cost ledger only | Telemetry collector job: `system.query_history` + table detail -> metrics dict -> `consult()` -> findings report artifact |
| A4 | HIGH | Declared Delta retention policies are never applied — design report promises bronze-forever/silver-365d/gold-730d but no generated table sets `deletedFileRetentionDuration`/`logRetentionDuration`; DBR default 7-day time-travel cap silently governs. | `core/medallion/design.py:1018` vs zero `TBLPROPERTIES` matches in emitters | Turn declared policy into real TBLPROPERTIES at emission; publish the real restore window |
| A5 | MED | Dashboard local reads full-scan bronze/silver Delta into Polars memory per callback (`SELECT * delta_scan`, no pushdown/cache); Databricks gold path re-fetches every call with no TTL/version check. | `core/dashboard/model/layers.py:78-90` (`_read_delta`), `:250-259` (`_read_databricks_gold`); `conformed.py:278,288`; `crossfilter.py:150` | Column/filter pushdown into `delta_scan`; Delta-version-keyed TTL cache on both paths |
| A6 | MED | DQ blueprint decides "freshness" but the emitted `sources.yml` never writes `freshness:`/`loaded_at_field`, and the validator doesn't require it — blueprint shows a decision the shipped project can't evaluate. | `core/blueprint/tables/dq_placement.yaml:24-31` vs `dbt_project_generator.py:563-582` | Emit freshness config when a `loaded_at_field` resolves; else record an explicit open question |
| A7 | MED | Cluster-key heuristic: no-GROUP-BY fallback takes first-4 SELECT columns (can cluster on the MEASURE); ties ignore profiler `cardinality_ratio` and `importance.py` ranking. Clustering is also required unconditionally even below the playbook's own 64MB-1GB write floor (inert config, and no OPTIMIZE ever runs to compact it). | `dbt_project_generator.py:839-866`, `:936-949`; playbook `clustering_write_below_floor` | Exclude measures from fallback; rank keys by cardinality/importance; skip-with-note below the write floor |
| A8 | MED | Failure alerting is a bare optional webhook; the intake's on-call answer (`ownership.on_call`) is wired to nothing — default deployment pages nobody. No KPI threshold/anomaly alerting exists either. | `core/orchestration/airflow_dag.py:85-120`; `core/intake/interview.py:118` | Thread on-call into DAG owner + alert route; post-run Z-score/IQR check on KPI movement -> alert |
| A9 | MED | Kafka ingestion stub reads entire topic backlog in one micro-batch (`startingOffsets=earliest`, no `maxOffsetsPerTrigger`) — the object-store path has throttles, Kafka doesn't. | `core/provisioning/ingestion.py:353-404` | Emit `maxOffsetsPerTrigger` default like Auto Loader's `maxFilesPerTrigger` |
| A10 | MED | Dashboard charts can't trace a number to its KPI definition/lineage (no definition/grain/source panel per chart); self-serve analysts have no documented query surface; result packets are on-demand only. | `core/dashboard/renderer.py:45-70`; intake `delivery_surface` unserved | Definition/lineage overlay per chart; generated self-serve notebook/SQL template; delivery manifest |
| A11 | LOW | Ghost-table reconcile is correct but never scheduled — orphans accumulate until a human runs the CLI. | `dbt_project_generator.py:~994-1042` unused by DAG emitters | Emit as periodic DAG task (report-only) |

## B. MISSING ENTIRELY (ranked)

**HIGH:**
1. **Late-arriving dimension / early-arriving fact handling** — no inferred-member/unknown-member row anywhere in modeling rules or generator; facts silently dropped by the star's joins. First step: R10 modifier in `modeling.yaml` + placeholder-member macro.
2. **Right-to-be-forgotten / PII erasure lifecycle** — PHI gate masks on read, but nothing deletes a person across bronze/silver/gold on request (zero hits for erasure/DSAR). Regulated targets require it. First step: `delete-subject-data` report-only command reusing profiler PII detection, behind the destructive gate.
3. **ML feature serving** — intake collects `ml_features` consumers, platform builds nothing: no feature tables, point-in-time joins, or store sync. First step: feature-table emitter from KPI grain with timestamp keys.
4. **Operational / reverse-ETL sync** — `operational_app` consumers have no path (no CDC/upsert-to-OLTP generator). First step: idempotent-upsert dbt macro or DAG task with declared keys.
5. **Regulatory serving** — no bitemporal/effective-dated gold ("as of date X"), no audit-trail/lineage export despite intake collecting the requirement. First step: intake `required` forces SCD2 + valid_from/to on gold + manifest/`system.table_lineage` export.
6. **Scheduled OPTIMIZE/VACUUM maintenance** — one inline OPTIMIZE line in a PySpark emitter; zero VACUUM anywhere; no maintenance task in any DAG. First step: periodic maintenance task per mart, playbook-gated.

**MEDIUM:**
7. Semantic layer / metric definitions (no `metrics.yml`/UC Metric Views — every new dashboard is a new hand-written query). Low effort, high leverage.
8. Scheduled report delivery (email/Slack digest per freshness cadence) — quick win (~days).
9. External data product (Delta Sharing / versioned file drop / API) for `external_share` consumers.
10. Kafka schema registry support (values read as raw strings; discovery already names the gap at `core/intake/discovery.py:308`).
11. Delta table tuning: deletion vectors, predictive optimization enablement, `TBLPROPERTIES` (zero repo-wide matches).
12. Warehouse lifecycle: no auto-stop/sizing logic anywhere (`CREATE WAREHOUSE`/`auto_stop`: zero matches).
13. Concurrent-write conflict handling: racing runs on one mart hit Delta optimistic-concurrency conflict with no retry/backoff.
14. Materialized rollups / query-result caching for dashboard latency (crossfilter re-aggregates silver in-process).
15. Post-load statistics (`ANALYZE TABLE`) / stats freshness after builds.

**LOW:**
16. KPI annotation/commentary surface; detail-row/cohort export; visual lineage explorer from dbt manifest.
17. Streaming watermarks/stateful backpressure — placeholder only; today's streaming is append-only ingest, no stateful operators to protect yet.

## C. Independently confirmed solid (all three reviewers)

Backfill primitives (Airflow params + dbt event-time + honest degradation); WAP staging+swap gated on tests; emitted-text enforcement of merge/unique_key, on_schema_change, replace_where ordering, hash keys, cluster_by, no-partition-<1TB; hashed schedules + catchup=False; query_tags cost attribution; additive-vs-destructive gate model with human provenance; AG-Grid server-side pushdown; playbook content quality (the gap is callers, not rules); local SQL hotspot loop (optimizer_finder) genuinely wired.
