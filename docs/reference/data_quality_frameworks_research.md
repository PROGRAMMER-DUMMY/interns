# Data Quality Framework Research — Databricks + dbt Cloud-First Stack

Researched 2026-08-05. Web research against vendor docs, dbt Labs, Databricks docs/blog, and
practitioner write-ups. Not exhaustive; treat framework "current status" claims as best-effort
snapshots of a fast-moving space and re-check before a hard commit.

## Executive Summary

1. There is no single winning framework in 2026 — teams on Databricks+dbt converge on a **two-tool
   split**: dbt tests (core + `dbt-expectations` + optionally Elementary) for warehouse/model-layer
   assertions, plus a Spark-native or platform-native tool for pre-landing/bronze checks.
2. Databricks has been actively building out its own DQ story: **Lakeflow Declarative Pipelines
   expectations** (in-pipeline, warn/drop/fail), **DQX** (Databricks Labs, Python/PySpark, quarantine
   pattern, at-rest + in-transit), and **Unity Catalog Data Quality Monitoring** (formerly Lakehouse
   Monitoring — profiling + anomaly detection over time, GA-adjacent as of late 2025/early 2026).
3. **Deequ/PyDeequ** is Spark-native and historically strong for constraint suggestion, but shows
   maintenance-health warning signs: the core JVM repo's own README still pins to Spark 3.1.x while
   PyDeequ's PyPI releases (1.4.x) claim Spark 3.5 support — the two halves of the project are out
   of sync. Treat it as usable but not a strategic bet for new build.
4. **Great Expectations** and **Soda Core** are both viable, general-purpose, engine-agnostic
   frameworks with working Databricks integrations, but adopting either as a *third* stack (on top
   of dbt + Databricks-native tooling) adds a dependency, a config surface, and — for GX Cloud / Soda
   Cloud — a subscription, without covering anything dbt tests and Databricks-native checks don't
   already cover for a Databricks-first, dbt-transformation shop.
5. **DQX is explicitly positioned as complementary, not a replacement**: it augments dbt/Lakeflow
   rather than displacing them, focuses on row/column-level diagnostics and a dead-letter/quarantine
   pattern, and typically sits at the bronze→silver boundary.
6. The industry pattern for check placement by medallion layer is consistent across sources: bronze
   = schema/freshness/volume, silver = uniqueness/referential/null-rate/type, gold = business-rule +
   reconciliation against upstream totals.
7. Severity tiers (warn / fail / quarantine) map cleanly onto Lakeflow's native `expect` /
   `expect_or_drop` / `expect_or_fail`, and onto a "row is written to a dead-letter table with
   structured failure metadata" quarantine pattern used broadly outside Databricks too.
8. Write-Audit-Publish (WAP) — write to a staging table, run checks, only then swap/publish to the
   production table — is the dominant pattern for keeping bad data from ever being visible
   downstream, and composes with dbt (staging model + production model) or Lakeflow (expectations
   gate the flow into the published table).
9. Auto-derivable checks from profiles have prior art in both GE's profiler/`OnboardingDataAssistant`
   and Deequ's constraint-suggestion engine: not-null rate, dtype conformance, cardinality-bounded
   accepted-values, uniqueness of PK candidates, numeric min/max bounds. Referential-integrity
   *semantics*, business-rule thresholds, and reconciliation baselines are not profile-derivable —
   they require a business answer.
10. **Recommendation for this platform**: standardize on **dbt tests as the primary, generated
    check surface** (already generated per-workspace via `dbt_project_generator.py`), backed by
    **Databricks-native expectations/DQX at the bronze ingestion boundary** where dbt doesn't see
    the data yet. Do not add GE, Soda, or Deequ as a fourth framework — the home-grown 5-dimension
    Python DQ set already covers what those would add, and a third framework is unjustified
    lock-in/maintenance cost for a platform that generates its own pipelines.

---

## 1. Framework Comparison (Databricks + dbt stack)

| Framework | Where it runs | What it covers | Maintenance health (2025-2026) | Lock-in | Cost |
|---|---|---|---|---|---|
| **dbt tests** (core, generic + singular) | Inside dbt, on built models (warehouse-side SQL) | not_null, unique, relationships, accepted_values, custom SQL assertions | Core: healthy, actively developed by dbt Labs; dbt 2.0 (Rust engine, alpha June 2026) folding Fusion improvements into Core | Low — plain SQL/YAML, portable across warehouses | Free (dbt Core); dbt Cloud/Fusion adds seat pricing (~$200-400/dev/mo Enterprise) but tests run fine on Core |
| **dbt-expectations** | Inside dbt, same execution model as dbt tests | GE-style richer assertions (distributions, regex, type checks) as dbt macros | Maintained by Datadog as an OSS package; stable, incremental updates | Low — dbt package, no new runtime | Free |
| **Elementary** | dbt package + optional Elementary Cloud | Anomaly-detection tests that learn historical baselines (freshness, volume, schema-change, distribution) instead of hardcoded expectations | Actively maintained, frequently cited alongside dbt-expectations as the "top 3" testing packages | Low for OSS package; Cloud tier adds a hosted dependency | Free OSS; Cloud tier paid |
| **Great Expectations (GX)** | Standalone Python, has a Spark/Databricks execution backend | Expectation suites, profiler/`OnboardingDataAssistant` for auto-suggesting expectations, rich human-readable "Data Docs" | Actively maintained OSS; Databricks integration guides note real friction (DBFS storage config, version compatibility, evolving APIs) | Medium — separate config/metadata store, own DSL, own execution context | Free OSS; GX Cloud adds hosted/paid tier |
| **AWS Deequ / PyDeequ** | Spark job (EMR, Glue, Databricks, notebooks) | Metrics + constraint-based checks, incremental state via `MetricsRepository`, constraint-suggestion engine that profiles data and proposes rules | Mixed signal: PyPI PyDeequ 1.4.0 claims Spark 3.5 support, but core `awslabs/deequ` README still documents "2.x only runs with Spark 3.1"; 66 open issues / 4 open PRs at last check — slow-moving | Medium — Spark-native, ties checks to Spark DataFrame API | Free OSS |
| **Soda Core / SodaCL** | Standalone Python + SQL push-down (works against Databricks SQL warehouses) | YAML-first checks (SodaCL), native anomaly-score/change-over-time clauses without hand-authored baselines | Actively developed; Databricks-specific integration ("SodaBricks" by Xebia, Soda's own Databricks launch page) exists | Medium — own YAML DSL and check language | Free OSS Core; Soda Cloud dashboard/alerting is paid |
| **Databricks Lakeflow (Declarative Pipelines) expectations** | Inside a Lakeflow/DLT pipeline, declared as SQL boolean constraints per table | Row-level `expect` (warn), `expect_or_drop` (quarantine-by-omission), `expect_or_fail` (halt); metrics land in the pipeline event log; as of late 2025 expectations can be stored/versioned centrally in Unity Catalog tables | Actively developed, GA feature, frequent 2026 release-note entries (queued execution mode, UC-backed expectation storage) | High — DLT/Lakeflow-pipeline-specific syntax, not portable off Databricks | Included in Lakeflow Pipelines compute cost, no separate license |
| **Databricks Labs DQX** | PySpark library, works inside or outside Lakeflow pipelines, has UI for authoring/reviewing rules | Batch + streaming; rule engine with configurable thresholds; reaction strategies (quarantine, log, stop); at-rest and in-transit validation | Actively developed (repo updated within the last day at time of research), but explicitly labeled "provided for exploration only... not formally supported... with SLAs" — a Labs project, not a supported product | Medium-high — PySpark/Databricks-specific API, no SLA | Free OSS (Databricks Labs) |
| **Unity Catalog Data Quality Monitoring** (formerly Lakehouse Monitoring) | Managed service over UC tables | Automated profiling + anomaly detection over time (freshness, completeness, statistical drift) without hand-written checks; agentic monitoring signals (Feb 2026) | Actively developed, UI refresh Oct 2025, "agentic data quality monitoring" push into 2026 | High — Unity Catalog / Databricks-only | Databricks compute + platform cost, no separate license |

Sources:
[Databricks DQX GitHub](https://github.com/databrickslabs/dqx) ·
[DQX Motivation docs](https://databrickslabs.github.io/dqx/docs/motivation/) ·
[DQX PyPI](https://pypi.org/project/databricks-labs-dqx/) ·
[Manage data quality with pipeline expectations (Azure Databricks / Microsoft Learn)](https://learn.microsoft.com/en-us/azure/databricks/ldp/expectations) ·
[Lakeflow Spark Declarative Pipelines release notes 2026 (AWS)](https://docs.databricks.com/aws/en/release-notes/dlt/2026) ·
[Data quality monitoring (Unity Catalog, AWS docs)](https://docs.databricks.com/aws/en/data-governance/unity-catalog/data-quality-monitoring/) ·
[Anomaly detection (Unity Catalog, AWS docs)](https://docs.databricks.com/aws/en/data-governance/unity-catalog/data-quality-monitoring/anomaly-detection) ·
[Data Observability Best Practices for Databricks 2026 (Atlan)](https://atlan.com/know/data-observability-best-practices-databricks/) ·
[awslabs/deequ GitHub](https://github.com/awslabs/deequ) ·
[pydeequ PyPI](https://pypi.org/project/pydeequ/) ·
[Great Expectations Databricks integrations](https://greatexpectations.io/integrations/) ·
[Great Expectations + Databricks getting-started guide](https://docs.greatexpectations.io/docs/0.18/oss/get_started/get_started_with_gx_and_databricks/) ·
[Soda Databricks integration](https://www.soda.io/integrations/databricks) ·
[Introducing SodaBricks (Xebia)](https://xebia.com/blog/introducing-sodabricks/) ·
[dbt-expectations & Elementary comparison (Elementary)](https://www.elementary-data.com/post/add-observability-to-your-dbt-project-top-3-dbt-testing-packages) ·
[dbt Fusion / dbt 2.0 explainer (Datacoves)](https://datacoves.com/post/dbt-fusion) ·
[dbt Cloud pricing 2026 (Paradime)](https://www.paradime.io/guides/dbt-cloud-pricing)

## 2. The Pragmatic Answer

No single named enterprise case study surfaced with a fully-attributed public write-up of "we picked
X over Y at scale on Databricks+dbt" — most public material is vendor/consultancy blogs rather than
named-account engineering postmortems. What *is* consistent across independent sources (Servian,
PipeCode, dbt Labs, Astronomer's own Airflow+dbt data-quality docs) is the same division of labor:

- **dbt tests own the warehouse/model layer** — anything already expressed as a dbt model gets
  `not_null` / `unique` / `relationships` / `accepted_values` plus `dbt-expectations` for richer
  assertions, because the checks live next to the model they validate, run in the same DAG, and
  block `dbt run`/`dbt build` on failure for free.
- **A Spark-native or platform-native tool owns pre-dbt / ingestion-layer checks** — dbt only sees
  data once it's already a table dbt can `ref()`. For raw/bronze validation (schema drift, null
  spikes, volume anomalies, freshness) teams reach for whatever runs *before* dbt touches the data:
  historically Deequ or GE-on-Spark; on Databricks specifically in 2025-2026, Lakeflow expectations
  or DQX are now more often the answer, being native to the ingestion layer.
- GX and Soda both explicitly market themselves as sitting *between* those two roles (validate raw,
  multi-source data before it becomes a warehouse table) — useful in a heterogeneous multi-warehouse
  shop, less differentiated in a Databricks-only shop where Databricks now ships its own answer to
  that exact gap.

Astronomer's own guidance (an Airflow vendor, so a credible "orchestration + dbt + DQ" source) frames
DQ checks as pipeline gates: run checks as Airflow tasks, fail/branch the DAG on result, and treat
dbt tests as one of several check sources Airflow can gate on, not the only one.

Sources:
[Data Quality and Testing Frameworks (Servian)](http://servian.dev/data-quality-and-testing-frameworks-316c09436ab2/) ·
[Data Quality Frameworks: GX vs dbt Tests vs Soda Core (PipeCode)](https://pipecode.ai/blogs/data-quality-frameworks-great-expectations-vs-dbt-tests-vs-soda-core) ·
[Data quality and Airflow (Astronomer docs)](https://www.astronomer.io/docs/learn/data-quality) ·
[dbt Tests and Data Quality Checks (Conduktor)](https://www.conduktor.io/glossary/dbt-tests-and-data-quality-checks)

## 3. Check-Writing Patterns

**Placement by medallion layer** (consistent across Databricks docs, DQX positioning, and general
data-eng practice):

| Layer | Checks | Typical severity |
|---|---|---|
| Bronze/raw | Schema conformance, freshness (data arrived on time), volume/row-count anomaly vs. trailing baseline | warn or quarantine — don't halt ingestion on a single bad batch |
| Silver | Uniqueness of PK candidates, referential integrity (FK resolves), null-rate thresholds on required columns, type/format conformance | fail or quarantine — this is where dbt tests + the platform's existing `null_keys`/`dim_uniqueness`/`referential`/`no_fanout`/`lossless`/`type_conformance` checks (`core/dashboard/model/dq.py`) already live |
| Gold | Business-rule assertions (valid ranges, KPI-specific invariants), reconciliation against upstream/raw totals | fail — a wrong number in gold is the worst place for it to be wrong |

**Severity tiers**: the dominant three-level pattern is warn / error / fail(-hard), matching
Lakeflow's `expect` (log, keep row) / `expect_or_drop` (log, drop row = implicit quarantine) /
`expect_or_fail` (halt the update). DQpOps and general DQ literature describe the same three tiers,
with "warning" explicitly *not* counted as a failed check so it doesn't page anyone.

**Anomaly vs. assertion checks**: assertion checks are static, hand-written thresholds (`null_rate <
1%`); anomaly checks compare against a trailing statistical baseline and flag deviation without a
hardcoded number — Elementary and Soda's `anomaly score`/`change` clauses, and Unity Catalog's
anomaly detection, all do this so teams don't have to hand-tune every threshold per column.

**Avoiding alert fatigue**: aggregate correlated failures into one alert instead of one per row/check
(a schema change shouldn't fire 1,000 alerts); alert on rate/depth of a quarantine queue rather than
every single quarantined row; prune or retune alerts that are repeatedly dismissed as false positives.

**Quarantine / dead-letter pattern**: failing rows are written to a separate table/queue enriched with
structured failure metadata (which check failed, when, why) rather than silently dropped or allowed
through — this is DQX's explicit design (`expect_or_drop`-style at the row level with a dead-letter
sink) and matches the general dead-letter-queue pattern from streaming systems.

**Write-Audit-Publish (WAP)**: write new data to a staging/audit table (in dbt: a staging model that
gets fully overwritten each run), run quality checks against that staging table, and only "publish"
(swap into or `MERGE` into the production table) if checks pass — so consumers only ever see data
that has already been audited, and a failed run leaves the previous good state visible rather than
partially-updated bad data.

Sources:
[Write-Audit-Publish in dbt (Medium/Cortland Goffena)](https://medium.com/@cortlandgoffena/dbt-write-audit-publish-9b5fc6bbd73d) ·
[Write-Audit-Publish Pattern in Pipelines (Dagster)](https://dagster.io/blog/python-write-audit-publish) ·
[Fail Fast or Quarantine? Two Data Quality Patterns (Medium)](https://medium.com/towards-data-engineering/fail-fast-or-quarantine-two-data-quality-patterns-every-spark-engineer-should-know-111598f31ada) ·
[Data Quality Alerts: Setup, Best Practices & Reducing Fatigue (Atlan)](https://atlan.com/know/data-quality-alerts/) ·
[What is a Data Quality Check? (DQOps)](https://dqops.com/docs/dqo-concepts/definition-of-data-quality-checks/) ·
[Manage data quality with pipeline expectations (Databricks/Microsoft Learn)](https://learn.microsoft.com/en-us/azure/databricks/ldp/expectations)

## 4. Generated Checks: Profile-Derived vs. Business-Rule-Derived

Prior art for auto-deriving expectations from a data profile is well established:

- **Great Expectations profiler / `OnboardingDataAssistant`**: scans a dataset and auto-generates an
  expectation suite from what it observes (column types, null rates, value ranges, categorical
  cardinality).
- **Deequ constraint suggestion**: runs analyzer jobs to profile the data, then a heuristics engine
  proposes constraints — e.g. zero nulls observed → propose `isComplete`; a few nulls observed →
  propose `hasCompleteness > 0.99` instead of a hard 100% requirement.

**What's safely auto-derivable from a profile** (already partially implemented in this repo's
`core/onboarding/kpi/data_quality_panel.py`, which emits `not_null` and `accepted_values`
`check_type` candidates from profile stats):
- Not-null / completeness thresholds (from observed null rate, not just "0 nulls = required")
- Type/format conformance (declared dtype vs. observed values, e.g. "date-named column is really a
  date" — mirrors this repo's existing `type_conformance` check)
- Uniqueness of PK candidates (from observed cardinality vs. row count)
- Accepted-values / categorical bounds (from low-cardinality observed distinct values)
- Numeric min/max bounds as soft anomaly guards (not hard business limits)

**What must come from a business answer, not a profile**:
- Referential-integrity *semantics* — which FK relationships are supposed to hold, and what an
  acceptable orphan rate is (a profile can measure orphan rate, but not decide if 2% orphans is fine)
- Business-rule thresholds with real-world meaning (a valid discount percentage range, a valid claim
  status transition) — a profile shows what *is* observed, not what's *allowed*
- Reconciliation baselines (gold total must match upstream total within X%) — requires knowing which
  upstream figure is the source of truth
- Freshness/SLA windows — "should have arrived by when" is an operational commitment, not a data fact

This maps directly onto the platform's existing blocker-panel pattern: profile-derivable checks
should be proposed automatically (as `data_quality_panel.py` already partly does); business-rule
checks should stay in the KPI blocker-question flow that asks the user, never be silently invented.

Sources:
[Great Expectations profiling reference](https://great-expectations.readthedocs.io/en/0.13.17/reference/spare_parts/profiling_reference.html) ·
[Data Quality with Deequ: Automated Profiling and Constraints generation (Medium/Data Reply)](https://medium.com/data-reply-it-datatech/data-quality-with-deequ-automated-profiling-and-constraints-generation-for-tabular-data-307b4447c8d9)

## 5. DQ in Orchestration (Airflow/dbt DAGs)

- **Fail the task**: the default for silver/gold assertion failures — a failed dbt test fails
  `dbt build`/`dbt test`, which fails the Airflow task, which (via normal Airflow trigger rules)
  blocks downstream tasks from running on bad data. This is the right default for anything gold-layer
  or business-facing.
- **Mark and continue**: for warn-tier/anomaly checks (volume drift, non-critical column null-rate
  creep) — log the result, emit a metric/event, don't block the DAG. Lakeflow's `expect` (vs.
  `expect_or_fail`) is this pattern natively; in Airflow it's a task that always succeeds but
  publishes a result to wherever alerting reads from.
- **Quarantine branch**: for row-level bad data that shouldn't block the whole batch — branch (or, in
  Lakeflow, `expect_or_drop`) so bad rows land in a dead-letter/quarantine table while good rows
  continue to publish. Airflow's `BranchPythonOperator`/trigger-rule patterns (`none_failed_min_one_
  success`, `all_done`) are the general-purpose version of this when checks live outside Lakeflow.
- **SLA/freshness monitoring**: increasingly handled *outside* the DAG's pass/fail logic — Unity
  Catalog Data Quality Monitoring profiles tables on a schedule and raises anomalies/freshness
  breaches independent of whether the ingesting DAG succeeded, which is the right layer for "did this
  table go stale even though nothing technically failed."

Sources:
[Data quality and Airflow (Astronomer docs)](https://www.astronomer.io/docs/learn/data-quality) ·
[Orchestrating dbt with Airflow, Dagster & Prefect (Medium)](https://medium.com/tech-with-abhishek/%EF%B8%8F-orchestrating-dbt-with-airflow-dagster-prefect-advanced-patterns-and-best-practices-2025-72ddc4691d0d) ·
[Data quality monitoring (Unity Catalog docs)](https://docs.databricks.com/aws/en/data-governance/unity-catalog/data-quality-monitoring/)

---

## Recommendation for This Platform

**The call**: standardize on **dbt tests (core + `dbt-expectations`) as the generated, primary check
surface**, backed by **Databricks-native expectations (Lakeflow, or DQX where PySpark-side quarantine
is needed) at the bronze ingestion boundary**. Do **not** adopt Great Expectations, Soda Core, or
Deequ as an additional framework.

**Honest trade-off**: GX and Soda are both genuinely good, general-purpose, warehouse-agnostic tools
— if this platform ever needs to support a non-Databricks execution engine as a first-class target
(not just a parity check), one of them becomes more attractive because dbt-expectations and Lakeflow
expectations don't travel off their respective platforms. But today the repo already generates a dbt
project (`core/onboarding/kpi/dbt_project_generator.py`) and a Spark-native medallion pipeline with a
home-grown 5-dimension DQ set. Bolting on a third DSL, a third metadata store, and (for GX Cloud/Soda
Cloud) a third paid tier duplicates what dbt tests + Lakeflow/DQX already cover, for a platform whose
entire premise is *generating* pipelines — the fewer runtime dependencies the generator has to target,
the fewer places generated code can drift out of sync with what the tool actually supports.

**Layer-by-layer check placement**:

| Layer | Check types | Where they should be generated |
|---|---|---|
| Bronze/raw | Schema conformance, freshness, volume/row-count anomaly | Lakeflow expectations (if the workspace uses Lakeflow) or DQX/PySpark checks at ingestion; `warn` or `expect_or_drop`, not `expect_or_fail` |
| Silver | Uniqueness (PK candidates), referential integrity (FK orphan rate), null-rate thresholds, type/format conformance | dbt generic tests (`unique`, `not_null`, `relationships`) + `dbt-expectations` for richer assertions; this is a like-for-like target for the existing home-grown `null_keys`/`dim_uniqueness`/`referential`/`no_fanout`/`type_conformance` checks in `core/dashboard/model/dq.py` — worth expressing as dbt tests on the silver models rather than a separate Python certifier, since dbt already runs in this stack |
| Gold | Business-rule assertions, reconciliation against silver/raw totals | dbt singular tests (custom SQL) generated from KPI definitions + the existing `lossless`/gold-reconciliation check; gate on `fail`, tied into the existing kpi-analyst review gate |

**Auto-derive from profiles** (no user question needed): not-null/completeness thresholds, dtype/type
conformance, PK-candidate uniqueness, low-cardinality accepted-values lists, numeric min/max as
soft/anomaly bounds — this is exactly the `not_null`/`accepted_values` candidate generation already in
`core/onboarding/kpi/data_quality_panel.py`; extend the same profile-driven derivation to silver dbt
tests instead of treating it as a separate code path.

**Ask the user** (never invent): which FK relationships must hold and what orphan tolerance is
acceptable, business-rule value ranges with real-world meaning, reconciliation baselines (which
upstream total is truth), and freshness/SLA commitments — route these through the existing KPI
blocker-question panel flow, not a freehand prompt.
