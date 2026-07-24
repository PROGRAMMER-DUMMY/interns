# DataOps Build Plan

> How an enterprise data pipeline is built and operated at GB -> TB scale, mapped
> stage-by-stage to services, internal (medallion) layers, what to do, and when to
> do it -- with worked examples in the **healthcare / revenue-cycle (RCM)** domain.
>
> Operating principle: **the AI/agent is a build-time author, not a run-time
> dependency.** The platform runs end-to-end once to emit a governed, tested,
> incremental, *scheduled* pipeline package. From then on the customer's
> orchestrator runs it unattended (daily / weekly / monthly). Humans (and the
> agent) return only when a KPI definition or a source schema changes.

Status: design / roadmap. Domain examples use the `Healthcare-RCM-Data-Platform`
workspace shape (claims, remittances, encounters, eligibility, AR).

---

## 0. The two clocks

There are two completely separate clocks, and conflating them is the classic
mistake:

| Clock | Cadence | Who/what drives it | What happens |
| --- | --- | --- | --- |
| **Build / change-time** | Rare (a new KPI, a schema change, a redefinition) | Analyst + agent + PR review | Author/regenerate models, contracts, tests, schedule; deploy through CI |
| **Run-time** | Daily / weekly / monthly | The **orchestrator on cron** | Ingest -> transform (incremental) -> test -> publish, unattended |

"Run pipelines by AI" only makes sense on the **build clock**. The **run clock**
is owned by a scheduler. Everything below is organized so the build clock emits a
package the run clock can execute without a human in the hot path.

---

## 1. Reference architecture (GB -> TB scale)

```
   SOURCES               INGEST                 STORE (lakehouse / warehouse)        TRANSFORM            SERVE
 ┌───────────┐    ┌────────────────────┐   ┌────────────────────────────────┐   ┌──────────────┐   ┌────────────┐
 │ EHR (Epic/│    │ CDC (Debezium/Kafka)│   │ BRONZE  raw, immutable, append │   │ dbt / Spark  │   │ BI: Power  │
 │ Cerner)   │───▶│ Clearinghouse SFTP  │──▶│ SILVER  cleaned, conformed     │──▶│ medallion    │──▶│ BI/Tableau │
 │ Practice  │    │ Payer 835/271 files │   │ GOLD    marts / KPI aggregates │   │ models, run  │   │ semantic   │
 │ Mgmt, PMS │    │ Fivetran/Airbyte    │   └────────────────────────────────┘   │ on a schedule│   │ layer +    │
 │ Payer APIs│    │ Batch file loaders  │            (Delta / Iceberg)           └──────────────┘   │ reverse ETL│
 └───────────┘    └────────────────────┘                                                            └────────────┘
        ORCHESTRATION    Airflow / Dagster / dbt Cloud  ── triggers everything on cron ──
        GOVERNANCE       Unity Catalog / DataHub, data contracts, OpenLineage, PHI/HIPAA controls
        QUALITY+OBS      dbt tests, Great Expectations / Soda, Monte Carlo / Elementary
        CI/CD            git -> dev / staging / prod, slim CI on a sample
```

At GB -> low-TB/day this is almost always **ELT** (load raw cheaply, transform
in-place), **warehouse/lakehouse-centric**, **incremental-by-default**, with
**dbt** as the transform workhorse and **Airflow/Dagster** as the scheduler.
Distributed Spark enters as volume pushes into multi-TB.

---

## 2. The operating cadence model

What runs, and when, once the pipeline is deployed:

| Cadence | What runs | Load pattern | Healthcare example |
| --- | --- | --- | --- |
| **Continuous / micro-batch** | CDC + SaaS syncs land into Bronze | append / upsert | Encounters streaming off the EHR as they are charted |
| **Hourly -> Daily** (workhorse) | Orchestrator DAG: ingest -> `dbt build` incrementals -> DQ tests -> publish to BI + reverse-ETL | **incremental MERGE on a watermark** | Nightly: load yesterday's 837 claims + 835 remits, refresh denial + AR marts |
| **Weekly** | Heavier rebuilds, SCD2 dimension snapshots, `OPTIMIZE`/compaction, vacuum | incremental + targeted full-refresh | Re-snapshot payer/provider dimensions; recompute 30/60/90 AR aging buckets |
| **Monthly** | Full reconciliation vs source-of-truth, partition archival/tiering, cost review | full-refresh / audit | Financial close: net collection rate, cost-to-collect, payer contract performance |
| **On-demand** | Backfills when a bug or definition changes | parameterized run -> **atomic partition overwrite** | Payer reprocesses 3 months of 835s after a remit error -> overwrite those service-date partitions |

Three rules make this safe to run unattended:

1. **Incremental + idempotent** -- `MERGE`/upsert on a stable key + high-watermark,
   never flat `INSERT`. Re-running last night's job yields the same result.
2. **Backfills are atomic partition overwrites** -- reprocess June without
   touching July.
3. **Every step is a DAG task** with retries, an SLA, and dependency edges -- a
   failure pages on-call but never corrupts downstream.

---

## 3. Per-stage build plan

Each stage lists: **Services** (tool categories + examples), **Internal layers**
it touches, **What to do**, **When to do it**, a **Healthcare example**, and
**What the platform emits** so the stage runs unattended.

### Stage 1 -- Requirements & Sizing

- **Services:** requirements/metrics layer (dbt MetricFlow, Cube, LookML), data
  catalog crawl, stakeholder interview.
- **Internal layers:** none yet -- this defines the *contract* the layers must meet.
- **What to do:** pin functional (who consumes: RCM analysts, CFO dashboards, a
  payer-scorecard API) and non-functional (latency SLA, volume tier, retention,
  HIPAA constraints) requirements; do back-of-envelope sizing.
- **When to do it:** at workspace onboarding, and whenever a new KPI or consumer
  is added.
- **Healthcare example:** KPI *First-Pass Denial Rate* = denied-on-first-submission
  claims / total claims, grain = claim, refreshed **daily**, retention 7 yrs
  (regulatory), consumers = RCM ops dashboard + monthly CFO close. Sizing: 2M
  claims/month x ~5 KB/claim (837 + lines) ~= 10 GB/month raw -> single-node /
  small-warehouse tier, batch nightly is sufficient (no streaming needed).
- **What the platform emits:** a KPI registry + acceptance criteria + sizing note
  that drives the engine/track choice (this repo: `prepare-kpi-blocker-panel`,
  KPI registry).

### Stage 2 -- Ingestion

- **Services:** CDC (Debezium + Kafka), SaaS connectors (Fivetran, Airbyte),
  file/SFTP loaders, clearinghouse feeds.
- **Internal layers:** writes **Bronze** only.
- **What to do:** land source data *exactly as it arrives* into Bronze --
  append-only, immutable, with audit columns (`ingest_ts`, `source_file`,
  `batch_id`). No cleaning. Choose incremental capture (CDC / high-watermark) so
  each run pulls only new data.
- **When to do it:** continuous (CDC) or per the source's drop schedule (payers
  drop 835 remits daily/weekly; clearinghouses return acknowledgements through the
  day).
- **Healthcare example:** Bronze tables `bronze_claim_837` (raw EDI 837 segments),
  `bronze_remit_835` (raw 835), `bronze_eligibility_271`, `bronze_encounter`
  (CDC off the EHR). A nightly SFTP job lands yesterday's payer 835 files; each
  file is appended with `source_file = BCBS_835_20260620.edi`, never overwritten.
- **What the platform emits:** Bronze table DDL + an ingestion task per source with
  its watermark/CDC config and schedule.

### Stage 3 -- Storage & File Format

- **Services:** object storage (S3 / ADLS / GCS), open table format (Delta /
  Iceberg / Hudi), or a native warehouse (Snowflake / BigQuery / Databricks SQL).
- **Internal layers:** physical substrate under **all three** medallion tiers.
- **What to do:** pick columnar Parquet for analytics + an open table format for
  ACID, schema enforcement, time travel, and compaction; partition by a filtered
  date column; target ~128 MB-1 GB files; schedule `OPTIMIZE`/compaction.
- **When to do it:** format/partition decided at build time; compaction + vacuum
  run **weekly**.
- **Healthcare example:** all medallion tables as **Delta**, partitioned by
  `service_date` (the column every RCM query filters on). `fct_claim_line`
  partitioned by `service_date` month; weekly `OPTIMIZE ... ZORDER BY (payer_id,
  provider_id)` so payer/provider-scoped queries skip most files. Cold partitions
  (> 24 months) tiered to cheaper storage but retained 7 years.
- **What the platform emits:** table-format + partition + clustering spec
  (this repo: `pipeline_plan.json` `table_format` / layers) and a weekly
  maintenance task.

### Stage 4 -- Transformation & Modeling (the medallion build)

- **Services:** dbt (dominant at this scale) or Spark; the medallion pattern.
- **Internal layers:** **Bronze -> Silver -> Gold** (detailed in section 4).
- **What to do:** Silver = clean, de-duplicate, type-cast, conform to facts +
  dimensions at row grain; Gold = pre-join/pre-aggregate for the KPIs. Declare the
  grain of each table first. Model dimension history with the right SCD type. Use
  surrogate keys + idempotent `MERGE`.
- **When to do it:** incrementally **every run** (Silver/Gold incrementals);
  SCD2 dimension snapshots **weekly**; occasional full-refresh **monthly**.
- **Healthcare example:**
  - Silver `fct_claim_line` (grain: one row per claim line), conformed to
    `dim_patient`, `dim_provider`, `dim_payer`, `dim_procedure` (CPT/HCPCS),
    `dim_diagnosis` (ICD-10), `dim_date`.
  - Silver `fct_remittance` (grain: one row per 835 service-line adjustment), with
    CARC/RARC denial-reason codes.
  - `dim_payer` and `dim_provider` are **SCD2** (a payer's contract terms or a
    provider's NPI affiliation changes over time -> keep `valid_from/valid_to`).
  - Gold `gold_denial_daily` (denials by payer x reason x day), `gold_ar_aging`
    (open AR bucketed 0-30/31-60/61-90/90+), `gold_collection_monthly`.
- **What the platform emits:** the medallion model set (Silver facts/dims + Gold
  marts) as deployable, incremental SQL/dbt models (this repo: `generate-kpi-sql`,
  `generate-pipeline-sql`, bronze/silver standards).

### Stage 5 -- Data Quality & Observability

- **Services:** dbt tests, Great Expectations / Soda (assertions), Monte Carlo /
  Elementary (continuous observability), lineage (OpenLineage).
- **Internal layers:** gates the promotion **Bronze->Silver** and **Silver->Gold**,
  and monitors **Gold** freshness for BI.
- **What to do:** enforce the five DQ dimensions as checks that *gate the publish*;
  quarantine bad rows (dead-letter), don't silently clean; set data SLOs + alerts;
  wire lineage for root-cause.
- **When to do it:** **every run** (tests block publish on failure); anomaly /
  drift monitors run continuously; reconciliation **monthly**.
- **Healthcare example:**
  - Completeness: claim count drop > 25% day-over-day -> page (a clearinghouse feed
    likely failed).
  - Accuracy: `paid_amount <= billed_amount`; `service_date <= paid_date`;
    `cpt_code IN dim_procedure` (valid code set).
  - Consistency: sum of 835 paid amounts reconciles to the payer remit control
    total; Gold revenue == sum of Silver line payments.
  - Freshness: `max(ingest_ts)` for `bronze_remit_835` within the payer's SLA.
  - Uniqueness: `(claim_id, line_no)` is unique; no duplicate 835 postings.
  - A claim failing accuracy goes to `quarantine_claim_line` with the failed rule,
    not into Silver.
- **What the platform emits:** a standing DQ test suite + quarantine tables +
  freshness SLOs, run on every cycle (this repo: `harness data-quality`, DQ
  certification gate).

### Stage 6 -- Orchestration, Scheduling & DataOps

- **Services:** Airflow / Dagster / dbt Cloud scheduler; CI/CD (git + slim CI);
  backfill runbooks; cost controls (auto-suspend, autoscaling).
- **Internal layers:** orchestrates the flow **across all layers** and owns
  promotion through **dev -> staging -> prod**.
- **What to do:** wire the DAG (ingest -> transform -> test -> publish) with
  retries + SLAs; schedule it (daily/weekly/monthly); make loads idempotent;
  provide a parameterized backfill; deploy changes through CI on a sample first.
- **When to do it:** the DAG itself runs on cron (this is the run clock); DAG/model
  *changes* ship at build time through CI.
- **Healthcare example:** Dagster job `rcm_daily` at 02:00 ET: sync clearinghouse
  acks + payer 835s -> `dbt build --select silver+ gold+` (incremental MERGE on
  `(claim_id, line_no)` watermark by `ingest_ts`) -> run DQ tests -> refresh Power
  BI dataset + push payer scorecards via reverse-ETL. Weekly `rcm_weekly`:
  SCD2 dim snapshots + `OPTIMIZE`. Monthly `rcm_close`: full reconciliation +
  net-collection-rate. Backfill job `rcm_backfill --from 2026-04-01 --to
  2026-06-30` overwrites those `service_date` partitions atomically after a payer
  remit correction.
- **What the platform emits:** the **orchestration DAG + schedule config +
  incremental runtime + backfill job** -- the missing packaging that turns the
  generated models into a self-running pipeline (this repo: today
  `run-kpi-pipeline`/`workspace-flow` is a build-time chain; the gap is emitting a
  *deployable scheduled* DAG).

### Stage 7 -- Serving & Consumption

- **Services:** BI (Power BI / Tableau / Looker), semantic layer, reverse-ETL
  (Hightouch / Census), data API.
- **Internal layers:** reads **Gold** (and Silver for re-rootable cuts).
- **What to do:** publish Gold marts to BI with a governed semantic layer; push
  curated metrics back to operational tools; refresh on the same schedule as the
  build.
- **When to do it:** refresh **after each successful run** (post-DQ-gate).
- **Healthcare example:** RCM ops dashboard (denials, AR aging, clean-claim rate)
  refreshes nightly; the CFO close dashboard refreshes monthly; a payer-scorecard
  reverse-ETL pushes denial-rate-by-payer back into the contract-management tool.
  Live dashboards source from **Silver** (row-grain, re-rootable) per the dashboard
  layer rule; Gold is for parity + hot read paths.
- **What the platform emits:** the live MinusAnalyst dashboard + deck/PDF exports
  (this repo: `workspace-dashboard`, `workspace-dashboard-deck/-pdf`) and the
  DataOps system-design review (`dataops review`).

---

## 4. The medallion layers in healthcare detail

| Layer | Responsibility | Healthcare-RCM contents | Cadence |
| --- | --- | --- | --- |
| **Bronze** | Raw, immutable, append-only; audit columns only | `bronze_claim_837`, `bronze_remit_835`, `bronze_eligibility_271`, `bronze_encounter`, `bronze_charge` -- exactly as landed (raw EDI / CDC rows) | per source drop (daily) |
| **Silver** | Cleaned, de-duped, typed, conformed facts + dimensions at row grain | Facts: `fct_claim_line`, `fct_remittance`, `fct_charge`, `fct_payment`. Dims: `dim_patient` (de-identified), `dim_provider` (SCD2), `dim_payer` (SCD2), `dim_procedure` (CPT/HCPCS), `dim_diagnosis` (ICD-10), `dim_date` | incremental every run; SCD2 snapshots weekly |
| **Gold** | Pre-joined / pre-aggregated marts per KPI | `gold_denial_daily`, `gold_ar_aging`, `gold_clean_claim_rate`, `gold_collection_monthly`, `gold_payer_scorecard` | incremental daily; close monthly |

**Cardinal rule:** cleaning belongs Bronze->Silver, business aggregation belongs
Silver->Gold. A bug in denial logic is fixed by recomputing Silver+Gold from
existing Bronze -- the payer/clearinghouse is never re-queried.

---

## 5. The emitted artifact bundle (what makes a workspace a drop-in scheduled pipeline)

Running the platform end-to-end once should produce this deployable bundle:

| Artifact | Purpose | Run cadence it serves |
| --- | --- | --- |
| **Medallion models** (Bronze DDL + Silver/Gold incremental SQL/dbt) | The transformation logic | every run |
| **Orchestration DAG + schedule config** (Airflow/Dagster, cron) | Runs the flow unattended | daily / weekly / monthly |
| **Incremental + idempotent runtime** (MERGE-on-watermark) | Safe re-runs, only-new-data | every run |
| **DQ test suite + quarantine tables** | Gate publish, isolate bad data | every run |
| **Data contracts + lineage** | Block breaking schema changes, root-cause | enforced continuously |
| **Backfill job** (parameterized, partition overwrite) | Reprocess history cleanly | on-demand |
| **CI/CD config** (dev->staging->prod, slim CI on a sample) | Ship changes safely | build time |
| **Runbook + SLOs + alert routes** | On-call operability | continuous |

This repo already produces the first row strongly (models) and rows 4-5 partially
(DQ harness, relationship/source-to-target contracts). The build-out gap is the
**scheduling/orchestration/incremental/backfill packaging** (rows 2, 3, 6, 7).

---

## 6. Healthcare-specific concerns (non-negotiable)

- **PHI / HIPAA:** the 18 HIPAA identifiers must be masked/redacted per policy;
  enforce minimum-necessary access; encrypt at rest + in transit; audit every
  access. This repo's PHI gate + `data_policy.json` + SQL masking + display
  redaction already cover much of this -- keep them on the promotion path
  (Bronze may hold PHI; Silver/Gold expose only what a consumer is entitled to).
- **De-identification:** `dim_patient` in Silver/Gold should carry a surrogate
  key, never raw MRN/SSN/DOB on analytic surfaces (DOB->age derived, kept as a raw
  date input only where a calculation needs it).
- **Regulatory retention:** 7-year retention is typical -> retention/tiering, not
  deletion; backfills must preserve immutable Bronze.
- **EDI formats:** 837 (claim), 835 (remittance/payment), 270/271 (eligibility),
  276/277 (claim status), 834 (enrollment) -- parsing belongs Bronze->Silver.
- **Reference data:** ICD-10, CPT/HCPCS, NPI registry, payer master, fee schedules
  are conformed dimensions; version them (code sets change annually).
- **Denial taxonomy:** CARC/RARC reason codes drive denial KPIs; keep them as a
  governed dimension.

---

## 7. Build roadmap (closing the gap to enterprise-grade)

Ordered by leverage:

1. **Incremental runtime** -- emit MERGE-on-watermark Silver/Gold instead of
   full-compute SQL. Prerequisite for everything scheduled.
2. **Schedule + DAG emission** -- generate an Airflow/Dagster job (or dbt schedule)
   from `pipeline_plan.json` with the chosen cadence (daily/weekly/monthly).
3. **Standing DQ suite** -- export the DQ harness as tests that gate each scheduled
   run + quarantine tables, not just a one-time certification.
4. **Backfill job** -- parameterized partition-overwrite runbook from the pipeline
   plan.
5. **Wire DataOps step 4-6 knowledge into Stage 6** -- the `minus_dataops` vendor's
   incremental/partition/compaction/idempotency/backfill knowledge becomes the
   defaults baked into the emitted package, not after-the-fact advice.
6. **Data CI/CD** -- dev->staging->prod promotion with slim CI on a sample.
7. **Continuous observability** -- freshness/volume/distribution monitors across
   the catalog (beyond per-run events).

The throughline: keep this platform's **governance + requirements + source-to-target
edge** (which is ahead of typical enterprise), and deepen the **execution plane**
(incremental, scheduled, observable, backfillable) so a workspace graduates from a
governed KPI factory into a self-running enterprise pipeline.

---

_Related: `docs/plans/tb-scale-dashboard-plan-2026-06-17.md`, the `minus_dataops`
vendor (`vendor/minus_dataops/`, `dataops` CLI), and the workspace workflow Stage
index in `AGENTS.md`._
