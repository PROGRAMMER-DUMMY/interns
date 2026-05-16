# PRD — Medallion Architect Agent

**Status:** Draft (post-grilling, pre-planning)
**Date:** 2026-05-15
**Owner:** Platform / Multi-Agent Analytics
**Companion plan:** to be written next (`docs/PLAN_medallion_architect.md`)

---

## 1. Problem

The current multi-agent platform (SQL Specialist, Data Engineer, Validation, Governor, PII masker, isolated execution backend) generates KPI SQL that **runs directly against raw unioned CSVs**. There is no transformation through Bronze → Silver → Gold layers, no star schema, no data-quality enforcement between source and KPI, and no audit-grade lineage. This causes three concrete problems for internal teams doing ETL/ELT:

1. **Silent grain errors** — unioning hospital_a + hospital_b raw CSVs without conformed dimensions produces duplicated KPI rows that look right but are wrong.
2. **No reproducibility** — every KPI re-derivation re-reads source CSVs and re-applies cleaning inline, so two runs against drifted source data produce different numbers with no audit trail.
3. **Compliance gap** — PII is masked only at SQL-gen time; data at rest is unprotected. In a HIPAA workspace this is a blocker.

## 2. Goals

- Generate a complete **Bronze / Silver / Gold** pipeline (manifests + per-substrate code) for any onboarded workspace.
- Treat the **star schema** as a first-class, human-ratified contract before any code is emitted.
- **Reuse, not duplicate**, existing platform components: `core/execution/backend.py`, `core/orchestration/governor.py`, the PII masker, the blocker question panel, validators, `control-pane` model selection, and MLflow telemetry.
- Make every run **idempotent**, **diffable**, and **auditable** end-to-end (column-level lineage + per-run state + MLflow metrics).
- Keep developer iteration cheap via caching, dynamic model tiering, and a `--cheap` mode.

## 3. Non-Goals

- Not a workflow orchestrator (Airflow / Dagster replacement). Execution remains the existing `ExecutionBackend`'s job.
- Not a streaming / CDC framework. Watermarked batch only.
- Not a BI tool or KPI definition system — KPI registry remains the source of truth for KPI semantics.
- No new privacy primitives — the existing PII masker is the canonical mechanism.

## 4. Architecture Overview

```
                onboard-workspace
                       ↓
              resolve-kpi-features
                       ↓
          prepare-kpi-blocker-panel  (all KPI blockers resolved)
                       ↓
       ┌────────────  design-medallion  ──────────────┐
       │  (Medallion Architect Agent)                 │
       │   reads: domain_model.json, kpi_registry,    │
       │          kpi_feature_mapping, profiles,      │
       │          workspace_feature_definitions       │
       │   emits: manifest, star_schema.json,         │
       │          bronze/silver/gold SQL+PySpark,     │
       │          silver_contract, lineage            │
       │   surfaces: star-schema decisions via        │
       │             blocker-question-panel           │
       └──────────────────────────────────────────────┘
                       ↓ (human ratifies via panel)
       ┌────────────  build-medallion  ───────────────┐
       │   uses existing ExecutionBackend             │
       │   runs Bronze → Silver → Gold in order       │
       │   runs assertions after each Silver table    │
       │   final step: re-invoke SQL Specialist to    │
       │     regenerate kpi_metrics_v2.sql against    │
       │     Gold + row-equality check vs legacy      │
       │   emits MLflow run + per-run state dir       │
       └──────────────────────────────────────────────┘
                       ↓
            validate-workspace-artifacts (extended)
```

## 5. Locked Design Decisions

The 13 decisions below are the contract this PRD must honor.

| # | Branch | Decision |
|---|---|---|
| 1 | Output | Declarative manifests + per-target SQL/PySpark under `workspaces/<ws>/interns/generated/medallion/` |
| 2 | Gold authority | Agent proposes star schema → blocker panel ratifies → `star_schema.json` |
| 3 | Substrate | Portable per-target (DuckDB local / Delta on Databricks); manifest declares target |
| 4 | Load strategy | Bronze append-watermarked · Silver MERGE-on-PK · Gold full-refresh |
| 5 | Silver contract | Per-table rule contract + post-load assertions; new error class `SILVER_ASSERTION_FAILED` |
| 6 | Failure routing | Stage-typed Governor routing table; per-class retry cap (default 2) → blocker panel |
| 7 | Compute failure | Honor existing strict-mode flag; downsize-then-fallback with `degraded_run` flag in permissive mode |
| 8 | Trigger | `uv run design-medallion` → `uv run build-medallion`; hash-idempotent on inputs |
| 9 | KPI SQL migration | Final build step re-invokes SQL Specialist against Gold; row-equality check; legacy preserved one cycle |
| 10 | PII at rest | Bronze raw (restricted), Silver hashed (existing masker + workspace salt), Gold-from-Silver-only validator-enforced |
| 11 | Multi-source | Bronze per-source split with `_source_system`; Silver composite-key default; flat-key opt-in via `workspace_feature_definitions.json` |
| 12 | Cost & models | Per-run USD cap + content-addressed cache + dynamic tiering via `control-pane` + `/models` discovery; task-class defaults; signal-based escalation/de-escalation |
| 13 | Lineage | Static `lineage.json` + `lineage.md` + per-run state dir + MLflow run per `build-medallion` |

## 6. CLI Commands

| Command | Purpose | Key flags |
|---|---|---|
| `uv run design-medallion --workspace workspaces/<ws>` | Generate manifest + per-target files + star schema; surface ratification blocker panel | `--cheap` (force lowest model tier), `--dry-run` (compute hash + diff, no writes), `--force` (ignore idempotency cache) |
| `uv run build-medallion --workspace workspaces/<ws>` | Execute pipeline via `ExecutionBackend`; emit MLflow run; regenerate KPI SQL | `--target {duckdb,delta,auto}` (override manifest target), `--only-layer {bronze,silver,gold,kpi}`, `--resume <run_id>` |
| `uv run validate-workspace-artifacts --workspace <ws>` | Extended to validate manifest schema, contract completeness, Gold-from-Silver-only invariant | (unchanged signature) |

`auto` target = read `cfg.databricks.is_active()` and the strict-mode flag; identical to how `build_execution_backend` selects today.

## 7. Generated Artifact Layout

```
workspaces/<ws>/interns/generated/medallion/
├── manifest.yaml                    # config (not a contract — single-format)
├── star_schema.json                 + star_schema.md             # facts, dims, grain
├── silver_contract.json             + silver_contract.md         # per-table rules + assertions
├── lineage.json                     + lineage.md                 # column-level derivation
├── data_model_extracted.json        + data_model_extracted.md    # vision-OCR output (if applicable)
├── bronze/
│   ├── patients__hospital_a.duckdb.sql
│   ├── patients__hospital_a.spark.py
│   ├── patients__hospital_b.duckdb.sql
│   ├── patients__hospital_b.spark.py
│   └── ... (one pair per source file)
├── silver/
│   ├── patient.duckdb.sql
│   ├── patient.spark.py
│   ├── _patient_assertions.sql      # post-load assertions
│   └── ...
└── gold/
    ├── dim_patient.duckdb.sql
    ├── dim_patient.spark.py
    ├── fact_claim.duckdb.sql
    ├── fact_claim.spark.py
    └── ...

workspaces/<ws>/interns/state/medallion/
├── medallion_cache/                 # content-addressed LLM cache (decision #12)
├── bronze/  silver/  gold/          # local-target materialized data (.parquet / .duckdb)
└── runs/
    └── <run_id>/
        ├── manifest_hash.txt
        ├── run.json                 # target, started_at, finished_at, per_table_status,
        │                            # row_counts_before_after, assertion_results,
        │                            # degraded_run flag
        └── logs/
```

`bronze/` and `state/medallion/bronze/` are added to workspace `.gitignore`; on Databricks, Bronze lives in `<ws>_medallion_bronze` Unity Catalog schema with restricted grants (decision #10).

**Dual-format contracts**: every contract artifact has paired JSON (machine source of truth) + MD (human review surface), matching the existing `interns/reports/derived_feature_reviews/{json,md}/` pattern. The MD is **regenerated from the JSON on every change** — never hand-edited, never the source of truth. MD sections follow the existing review template: "Why This Was Proposed" (formula/grain/SCD rationale), "Why Not Ground Truth" (missing evidence), "Remaining Risk".

## 8. Manifest Schema (`manifest.yaml`)

```yaml
schema_version: 1                    # bump on breaking change; validator-checked
workspace: Healthcare-RCM-Data-Platform
generated_at: 2026-05-15T10:30:00Z
inputs_hash: sha256:...              # hash of (domain_model + kpi_registry +
                                     #          kpi_feature_mapping + profiles +
                                     #          workspace_feature_definitions)
target: auto                         # duckdb | delta | auto
strict_databricks: false             # mirrors core/execution/backend.py:_strict_databricks
budget:
  max_usd_per_run: 5.00
  cache_dir: interns/state/medallion/medallion_cache
layers:
  bronze:
    - name: patients__hospital_a
      source_file: datasets/EMR/trendytech-hospital-a/patients.csv
      source_system: hospital_a
      load_strategy: append_watermarked
      watermark_column: ModifiedDate
      natural_key: [PatientID]
      pii_columns: [PatientID, Name, DOB, SSN]   # read from semantic_contract.json
  silver:
    - name: patient
      derived_from: [bronze.patients__hospital_a, bronze.patients__hospital_b]
      load_strategy: merge_on_pk
      primary_key: [source_system, patient_id]   # composite by default; flat opt-in
      contract: silver_contract.json#/patient
  gold:
    - name: dim_patient
      kind: dimension
      load_strategy: full_refresh
      derived_from: [silver.patient]
      scd_type: 1
    - name: fact_claim
      kind: fact
      grain: "one row per claim line per service date"
      load_strategy: full_refresh
      derived_from:
        - silver.claim
        - silver.encounter
        - silver.patient (dim)
        - silver.provider (dim)
kpi_regeneration:
  enabled: true
  target_file: interns/generated/solutions/kpi_metrics_v2.sql
  row_equality_check_against: interns/generated/solutions/kpi_metrics.sql
```

## 9. Silver Contract Schema (`silver_contract.json`)

```json
{
  "patient": {
    "type_casts": {
      "DOB": {"from": "String", "to": "Date", "format": "YYYY-MM-DD"}
    },
    "null_policies": {
      "patient_id": "error",
      "dob": "default:1900-01-01",
      "ssn": "drop"
    },
    "dedup_keys": ["source_system", "patient_id"],
    "pii_hash_columns": ["patient_id", "ssn", "first_name", "last_name", "dob"],
    "hash_salt_ref": "workspace.medallion_salt",
    "derived_columns": {
      "age_at_service": {
        "formula_templates": {
          "duckdb_sql": "date_diff('year', DOB, ServiceDate)",
          "spark_sql":  "floor(months_between(ServiceDate, DOB) / 12)",
          "polars":     "((pl.col('ServiceDate') - pl.col('DOB')).dt.total_days() / 365.25).floor()"
        },
        "input_columns": [
          {"column": "DOB",         "dataset": "bronze.patients__hospital_a", "role": "birth_date",  "dtype": "Date"},
          {"column": "ServiceDate", "dataset": "silver.encounter",            "role": "anchor_date", "dtype": "Date"}
        ],
        "business_meaning": "Completed age in years from birth date and service-encounter date.",
        "reasoning": "Lifted from derived_feature_reviews/json/kpi_001_age.json (user_confirmed).",
        "source_review": "interns/reports/derived_feature_reviews/json/kpi_001_age.json",
        "materialized_at_layer": "silver",
        "computed_once_reused_by_kpis": ["kpi_001", "kpi_002"]
      }
    },
    "assertions": [
      {"id": "no_null_pk",   "type": "not_null",        "columns": ["source_system", "patient_id"]},
      {"id": "pk_unique",    "type": "unique",          "columns": ["source_system", "patient_id"]},
      {"id": "row_delta",    "type": "row_count_delta", "tolerance_pct": 5},
      {"id": "fk_provider",  "type": "referential_integrity",
                             "child": "patient.preferred_provider_id",
                             "parent": "silver.provider.provider_id"},
      {"id": "no_cross_join",     "type": "sql_plan_property", "property": "no_cartesian_join"},
      {"id": "partition_filter",  "type": "sql_plan_property", "property": "partition_filter_present"}
    ]
  }
}
```

Assertion failure → `SILVER_ASSERTION_FAILED` → Governor routes to Medallion Architect (decision #6). SQL-plan-property assertions are evaluated against the pre-commit lint pass (Section 18).

**Derived feature integration**: agent reads `interns/reports/derived_feature_reviews/json/*.json` (existing artifact) as upstream input and lifts user-confirmed features into Silver as **materialized columns computed once**. KPI SQL in Gold references `silver.patient.age_at_service` rather than recomputing the formula per-query — the actual performance win of moving from raw-union to Medallion.

## 10. Governor Routing Table Extension

Extend `core/orchestration/governor.py` with:

```python
MEDALLION_ROUTING = {
    "BRONZE_LOAD_FAIL":       ("data_engineer",       2),  # (specialist, max_retries)
    "SILVER_TRANSFORM_FAIL":  ("sql_specialist",      2),
    "SILVER_ASSERTION_FAILED":("medallion_architect", 2),
    "GOLD_DERIVATION_FAIL":   ("medallion_architect", 2),  # then sql_specialist on retry
    "KPI_ROW_EQUALITY_FAIL":  ("medallion_architect", 1),  # surface immediately
    "SQL_LINT_FAIL":          ("sql_specialist",      2),  # parse/plan/perf check failed
}
```

On cap-exceeded → write blocker entry to `interns/reports/blocker_question_panel/current.json` (existing mechanism). No LLM-based error classification.

## 11. Compute Failure & Strict Mode

Wires into existing `core/execution/backend.py:_strict_databricks`:

- **Strict mode** (`cfg.databricks.fallback == "fail"` or `AUTORESEARCH_DATABRICKS_STRICT=1`):
  any compute failure halts the run, writes structured error to log, surfaces blocker. No silent demotion. Run state records `target_actual = target_declared`.
- **Permissive mode**:
  1. Retry on same substrate with downsized parameters (smaller cluster / chunked partitions for OOM / shorter `wait_timeout`).
  2. If retry-cap exhausted, fall back to DuckDB; manifest of *that run* records `degraded_run: true, original_target: delta, fallback_reason: <msg>`.
  3. KPI output downstream of a degraded run is annotated in MLflow tags.

## 12. PII at Rest

- **Bronze**: stores raw values. Path access-restricted:
  - Local: under `interns/state/medallion/bronze/` with workspace `.gitignore` entry; not exported by `list-workspace-files`.
  - Databricks: separate Unity Catalog schema `<ws>_medallion_bronze` with explicit grants.
- **Silver**: every column marked `pii: true` in `semantic_contract.json` or `workspace_feature_definitions.json` is hashed using the **existing PII masker** (SHA-256) with a workspace-scoped salt fetched from `workspace.medallion_salt` (secrets store; never written to manifest or log).
- **Gold**: validator-enforced — every `derived_from` in Gold must reference `silver.*`, never `bronze.*`. Validation fails the run.
- **Unmarked columns**: agent does NOT default to "not PII". Surfaces a blocker entry per unmarked column with a sample value preview (PII redacted in the preview itself).

## 13. Multi-Source Semantics

- **Bronze**: one table per source file; metadata columns `_source_system`, `_source_file`, `_load_ts` always present.
- **Silver default**: composite natural key `(source_system, <id>)`. Joins between Silver tables also use composite keys.
- **Flat-key opt-in**: user declares in `workspace_feature_definitions.json`:

  ```json
  {
    "cross_source_identity": {
      "patient_id": {
        "globally_unique": true,
        "rationale": "National Patient Identifier issued centrally",
        "ratified_by": "data_steward",
        "ratified_at": "2026-05-15"
      }
    }
  }
  ```

  When present, agent re-runs Silver design with a flat PK on those entities.
- **Ambiguity**: when the same ID appears in multiple sources without an explicit flat-key declaration, agent surfaces a blocker with row examples ("PatientID `P-0042` appears in both hospital_a and hospital_b — same person or coincidence?").

## 14. Cost & Model Selection (Provider-Agnostic)

- **Hard budget cap**: `manifest.budget.max_usd_per_run`; agent tracks spend via `core/agents/llm_engine.py` token accounting. On exceed → pause + blocker `"completed N of M tables, resume?"`.
- **Cache**: content-addressed under `interns/state/medallion/medallion_cache/`; key = `sha256(system_prompt + relevant_contract_excerpts + table_schema + task_class + model_tier + prompt_strategy_version)`. Cache key includes tier + strategy version so Gemma-tier outputs never collide with Opus-tier outputs. Cache hit = zero LLM call.

### Provider & model discovery

The platform already abstracts providers in `core/agents/llm_engine.py`:
- `APIEngine` — direct provider API (currently Gemini; pattern extends to any HTTPS endpoint).
- `CLIEngine` with `_CLI_DISPATCH` — drives installed CLIs (`gemini-cli`, `claude-code`, `codex`).

Model and tier inventory lives in **`config/model_tiers.yaml`** (new file, refreshed without code changes):

```yaml
tiers:
  heavy:
    description: Single-shot whole-schema inference; full context
    candidates:
      - {provider: anthropic-cli, model: claude-opus-4-7,    vision: true,  max_input: 200000}
      - {provider: anthropic-cli, model: claude-sonnet-4-6,  vision: true,  max_input: 200000}
      - {provider: gemini-api,    model: gemini-2.5-pro,     vision: true,  max_input: 1000000}
      - {provider: codex-cli,     model: gpt-4.1,            vision: false, max_input: 200000}
  medium:
    description: Decomposed per-entity; trimmed context to relevant tables
    candidates:
      - {provider: anthropic-cli, model: claude-haiku-4-5,   vision: true,  max_input: 200000}
      - {provider: gemini-api,    model: gemini-2.5-flash,   vision: true,  max_input: 1000000}
      - {provider: codex-cli,     model: gpt-4o,             vision: true,  max_input: 128000}
  light:
    description: Heavily decomposed (one call per relationship); strict JSON-schema validation; retry cap 5
    candidates:
      - {provider: gemini-api,    model: gemma-4,            vision: false, max_input: 32000}
      - {provider: gemini-api,    model: gemini-2.5-flash-lite, vision: false, max_input: 1000000}
      - {provider: codex-cli,     model: o4-mini,            vision: false, max_input: 128000}
```

Model IDs above are illustrative ("latest available" of each family); the YAML is the authoritative list and the only file that needs editing when a new model ships. Unavailable models are detected at run start (engine probe) and silently skipped.

### Task-class default tier

Manifest declares per task class:

| Task class | Default tier | Rationale |
|---|---|---|
| `bronze_append_sql` | light  | Mechanical; one SELECT + watermark filter per source file |
| `silver_transform_sql` | medium | Joins + type casts + null policy; non-trivial but bounded |
| `silver_derived_column` | medium | Reuses confirmed `derived_feature_reviews` — translation, not design |
| `star_schema_design` | heavy | Grain + conformed dimensions; highest judgment, lowest tolerance for error |
| `silver_contract_rules` | medium | Assertion thresholds; needs profile context |
| `kpi_sql_regeneration` | medium | Existing SQL Specialist work; medium tier matches its prior calibration |
| `lineage_explanation_md` | light | Templated narrative |

### Prompt tiering (works on Gemma 4 as well as Opus 4.7)

Each task class has three prompt strategies — selected by the **current tier**, not the model ID:

- **Heavy strategy**: one prompt, full context (e.g., "Design the star schema for these KPIs and datasets"). Output is freeform structured JSON.
- **Medium strategy**: decomposed into N smaller prompts (one per entity / fact / dimension). Each prompt gets only the slice of context it needs. Output is JSON validated per call.
- **Light strategy**: heavily decomposed into atomic prompts (one per relationship, one per derived column, one per assertion). Each prompt embeds the **exact JSON output schema** inline and ends with `Return only the JSON object that matches this schema`. Output is `jsonschema`-validated; on schema failure, the agent retries with `Your previous output failed validation: <error>. Return only valid JSON matching this schema.` Retry cap raised to 5 (cheap because tokens are short).

### Escalation and de-escalation signals

- **Escalate** (one tier up) when: (a) two consecutive failures at current tier on the same task, (b) `requires_judgment: true` + cache miss, (c) validator flags low confidence on the prior output.
- **De-escalate** (one tier down + swap to next strategy) when: (a) running budget burn > 60% with > 40% of tables remaining (proportional throttling), (b) cache near-hit (only schema delta), (c) `--cheap` flag passed.
- Tier shift swaps **both** the model and the prompt strategy — not just the ID.

### Pinning

`design-medallion --engine {gemini-api,gemini-cli,claude-code,codex}` lets the user pin one engine. `--model <id>` forces a specific model (skips discovery; validated against `config/model_tiers.yaml`). Defaults read `config/lock.toml`.

## 15. Lineage & Observability

- **Static lineage** (`lineage.json` + `lineage.md`): column-level `derived_from: [{table, columns, transform_type}]`. Markdown is PR-reviewable.
- **Per-run execution log** (`interns/state/medallion/runs/<run_id>/run.json`): `manifest_hash`, `target_declared`, `target_actual`, `started_at`, `finished_at`, `per_table_status`, `row_counts_before_after`, `assertion_results`, `degraded_run`, `retry_history`.
- **MLflow** (when Databricks active): each `build-medallion` is an MLflow run tagged `{workspace, target, manifest_hash, run_id, degraded_run}`; metrics include per-layer row counts, assertion pass rates, total LLM USD spend; artifacts include `manifest.yaml`, `lineage.json`, the run state dir.

## 16. Validator Extensions (`validate-workspace-artifacts`)

Add these checks (errors → block, not warn):

1. `manifest.schema_version` matches a known version.
2. `inputs_hash` matches recomputed hash (catches stale manifests).
3. Every `gold.*.derived_from` entry references `silver.*` (PII invariant from decision #10).
4. Every Bronze table has `natural_key` and `pii_columns` declared.
5. Every Silver table referenced in `manifest.layers.silver` has a matching entry in `silver_contract.json`.
6. Every PII-marked column in `semantic_contract.json` appears in `silver_contract.<table>.pii_hash_columns` for at least one Silver table.
7. `star_schema.json` grain declarations are present for every fact table.
8. No `dim_*` or `fact_*` references a Bronze table.

## 17. Open Edges (Deferred, with Defaults)

The following were not grilled; PRD defaults below. Any can be revisited in PLAN phase.

- **Concurrency lockfile**: default `flock`-style file lock at `interns/state/medallion/.lock`; second concurrent `design-medallion` exits with `WORKSPACE_BUSY` and the holding `run_id`.
- **Manifest semver**: default semantic versioning at `schema_version`; minor bump for additive fields, major bump for breaking. Validator rejects unknown major versions.
- **`--dry-run` semantics**: default = compute `inputs_hash`, compare to cached manifest hash, emit diff to stdout; no file writes, no LLM calls beyond cache hits.
- **Partial-table re-runs**: default = supported via `build-medallion --only-layer silver --only-table patient`; assertions still run; lineage records partial run.
- **Schema drift in source CSVs**: detected via `inputs_hash` mismatch on re-run; surfaces as a blocker per drifted column (added / removed / type-changed); no silent regeneration.

## 18. SQL Effectiveness (Parse + Plan + Hotspot)

Every emitted SQL/PySpark file is validated **before** being written to the manifest. The agent never commits SQL it has not lint-checked.

### Parse pass

- **DuckDB target**: `duckdb.sql("EXPLAIN " + stmt)` against a fixture database that mirrors the actual schema (built once per `design-medallion` run from `domain_model.json`).
- **Delta target**: `sqlglot.parse(stmt, dialect="spark")` — pure-Python, no Spark dependency at design time.
- **PySpark target**: `ast.parse(file)` for syntax, then a dataframe-API linter for known anti-patterns (`.collect()` on non-aggregated, `withColumn` in a Python loop, etc.).

### Plan pass

For the local target only (Spark plans require a live session):

- `EXPLAIN <stmt>` is parsed for these properties, each tied to a Silver assertion id:
  - `no_cartesian_join` — fails if `CROSS_PRODUCT` or unconditional join appears.
  - `partition_filter_present` — fails on partitioned source tables without a partition predicate.
  - `no_scalar_subquery_in_select` — fails on correlated scalar subqueries in a SELECT list (silent N+M anti-pattern).
  - `broadcast_safe` — fails if a non-broadcast join references a table profiled > 1M rows.

### Hotspot pass (reuses `tools/optimizer_finder.py`)

Per `MEMORY.md`, `tools/optimizer_finder.py` is already wired into `evaluator.py` step 0. The Medallion Architect invokes it on:

- Every Gold KPI SQL produced by the SQL Specialist regeneration step.
- Any Silver SQL whose source profile shows row count > a configurable threshold (default 100k).

Findings above the budget threshold do **not** silently commit; they surface through the blocker panel with the hotspot report and suggested rewrite.

### New error class

`SQL_LINT_FAIL` is added to the Governor routing table (Section 10) and routes to the SQL Specialist with retry cap 2. Cap-exceeded surfaces the lint findings as a panel entry showing: the failed stmt, the failed property, the EXPLAIN output, and a one-line "what changed in the proposed fix".

## 19. CLI `--help` Discipline

`design-medallion` and `build-medallion` use the same `argparse` style as the existing `uv run onboard-workspace` (which I verified with `uv run onboard-workspace --help`). Each command's `--help` output contains:

- One-line usage.
- 1–2 sentence description.
- Required vs. optional flag tables with one-line descriptions per flag.
- **2–3 example invocations** baked into the help text via argparse `epilog`:
  - Dry-run cheap path: `uv run design-medallion --workspace workspaces/Healthcare-RCM-Data-Platform --dry-run --cheap`
  - Full run pinned to local target: `uv run build-medallion --workspace workspaces/Healthcare-RCM-Data-Platform --target duckdb`
  - Resume an interrupted run: `uv run build-medallion --workspace workspaces/Healthcare-RCM-Data-Platform --resume <run_id>`
- **Exit codes table** in the epilog:

  | Code | Meaning | Suggested next command |
  |---|---|---|
  | `RUN_ONBOARD_FIRST` | Missing `domain_model.json` or profiles | `uv run onboard-workspace --workspace <ws>` |
  | `NO_KPIS_DEFINED` | Empty `kpi_registry.json` | `uv run resolve-kpi-features --workspace <ws> --domain <domain>` |
  | `KPI_BLOCKERS_UNRESOLVED` | Blocker panel has unresolved entries | `uv run prepare-kpi-blocker-panel --workspace <ws> --domain <domain>` |
  | `EMPTY_WORKSPACE` | Zero source datasets | Manual fix |
  | `WORKSPACE_BUSY` | Lockfile held by another run | Wait or remove stale lock |
  | `SQL_LINT_FAIL` | Lint findings exceeded cap | Inspect blocker panel |
  | `BUDGET_EXCEEDED` | `max_usd_per_run` hit mid-run | `--resume <run_id>` with raised cap |

Discoverability parity with `gemini --help`, `claude --help`, `codex --help` — but as Python `argparse`, no new framework.

## 20. Acceptance Criteria

The Medallion Architect ships when, on the Healthcare-RCM-Data-Platform workspace:

1. `uv run design-medallion --workspace workspaces/Healthcare-RCM-Data-Platform` produces a complete `medallion/` directory and surfaces star-schema decisions through the blocker panel.
2. After human ratification, `uv run build-medallion` runs Bronze → Silver → Gold → KPI regeneration end-to-end on both `target=duckdb` (local) and `target=delta` (Databricks Jobs backend) without code changes.
3. `kpi_metrics_v2.sql` produces row-equal output to legacy `kpi_metrics.sql` for unchanged KPIs; changes are surfaced as a blocker, not silently accepted.
4. `validate-workspace-artifacts` passes all eight new checks.
5. A deliberately introduced PII column (unmarked in `semantic_contract.json`) is caught by the blocker panel, not silently leaked into Silver.
6. A deliberate Databricks-cluster failure under permissive mode produces `degraded_run: true` and a DuckDB-substrate Gold; under strict mode it halts.
7. Total LLM USD spend for a full first-run on Healthcare-RCM stays under the configured `max_usd_per_run` cap; second run with no input changes is ~100% cache hits.
8. `lineage.md` renders a column-level path from any Gold column back to its source CSV.

## 21. Phasing

| Phase | Scope | Exit criterion |
|---|---|---|
| **P0** | Manifest schema, `star_schema.json`, design-time agent (DuckDB target only); blocker panel integration; validator extensions 1–4 | `design-medallion` produces a ratifiable manifest on Healthcare RCM |
| **P1** | `build-medallion` for DuckDB target; Silver contract + assertions; Governor routing table; KPI SQL regeneration + row-equality | Acceptance criteria 1, 3, 4 (subset) |
| **P2** | Delta target (Spark) + Databricks Jobs/Warehouse integration; compute failure handling; `degraded_run` flag; MLflow run | Acceptance criteria 2, 6 |
| **P3** | PII at rest (Bronze restrictions, Silver hashing, Gold-from-Silver invariant); validator extensions 5–8 | Acceptance criterion 5 |
| **P4** | Dynamic model tiering via `control-pane` + `/models`; cache; budget cap; `--cheap` flag | Acceptance criterion 7 |
| **P5** | Column-level lineage (`lineage.json` + `lineage.md`); per-run state dir | Acceptance criterion 8 |

P0–P2 are the minimum viable agent. P3 is a hard prerequisite for any production healthcare use. P4 is a hard prerequisite for cost control at workspace count > 3. P5 unlocks production debugging.
