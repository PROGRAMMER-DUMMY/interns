# 01 — Architecture & Decisions

This is the load-bearing reference for the entire feature. Every phase doc links back here.

## 1. System diagram

```
                    onboard-workspace
                            ↓
                  resolve-kpi-features
                            ↓
              prepare-kpi-blocker-panel
                            ↓
       ┌──────────── design-medallion ──────────────┐
       │   reads:                                   │
       │     - contracts/domain_model.json          │
       │     - contracts/kpi_registry.json          │
       │     - contracts/kpi_feature_mapping.json   │
       │     - contracts/semantic_contract.json     │
       │     - contracts/workspace_feature_         │
       │         definitions.json                   │
       │     - profiles/profile_index.json          │
       │     - reports/derived_feature_reviews/json │
       │   emits:                                   │
       │     - generated/medallion/manifest.yaml    │
       │     - generated/medallion/star_schema.{    │
       │         json,md}                           │
       │     - generated/medallion/silver_contract  │
       │         .{json,md}                         │
       │     - generated/medallion/lineage.{json,md}│
       │     - generated/medallion/bronze/*.sql/.py │
       │     - generated/medallion/silver/*.sql/.py │
       │     - generated/medallion/gold/*.sql/.py   │
       │     - reports/medallion_design_panel/      │
       │         current.{json,md}                  │
       └────────────────────────────────────────────┘
                            ↓ (human ratification via panel)
       ┌──────────── build-medallion ───────────────┐
       │   - Uses existing ExecutionBackend         │
       │     (DuckDB / Warehouse / Jobs / Connect)  │
       │   - Runs Bronze → Silver → Gold            │
       │   - Runs Silver assertions; routes through │
       │     Governor on failure                    │
       │   - Final step: re-invoke SQL Specialist  │
       │     to regenerate kpi_metrics_v2.sql       │
       │     against Gold + row-equality vs legacy  │
       │   - Emits per-run state dir + MLflow run   │
       └────────────────────────────────────────────┘
                            ↓
              validate-workspace-artifacts (extended)
```

## 2. The 13 locked decisions

These are the contract this implementation must honor. They come from the grilling phase; the PRD has the full options table for each.

| # | Branch | Decision |
|---|---|---|
| 1 | **Output** | Declarative manifests + per-target SQL/PySpark under `workspaces/<ws>/interns/generated/medallion/` |
| 2 | **Gold authority** | Agent proposes star schema → blocker panel ratifies → `star_schema.json` |
| 3 | **Substrate** | Portable per-target (DuckDB local / Delta on Databricks); manifest declares target |
| 4 | **Load strategy** | Bronze append-watermarked · Silver MERGE-on-PK · Gold full-refresh |
| 5 | **Silver contract** | Per-table rule contract + post-load assertions; new error class `SILVER_ASSERTION_FAILED` |
| 6 | **Failure routing** | Stage-typed Governor routing table; per-class retry cap (default 2) → blocker panel |
| 7 | **Compute failure** | Honor existing strict-mode flag; downsize-then-fallback with `degraded_run` flag in permissive mode |
| 8 | **Trigger** | `uv run design-medallion` → `uv run build-medallion`; hash-idempotent on inputs |
| 9 | **KPI SQL migration** | Final build step re-invokes SQL Specialist against Gold; row-equality check; legacy preserved one cycle |
| 10 | **PII at rest** | Bronze raw (restricted), Silver hashed (existing masker + workspace salt), Gold-from-Silver-only validator-enforced |
| 11 | **Multi-source** | Bronze per-source split with `_source_system`; Silver composite-key default; flat-key opt-in via `workspace_feature_definitions.json` |
| 12 | **Cost & models** | Per-run USD cap + content-addressed cache + dynamic tiering via `control-pane` + runtime model discovery + WebSearch ranking |
| 13 | **Lineage** | Static `lineage.json` + `lineage.md` + per-run state dir + MLflow run per `build-medallion` |

## 3. Amendments (post-grilling refinements)

| Amendment | Affects | Summary |
|---|---|---|
| **A — SQL effectiveness** | All phases that emit SQL | Every emitted SQL/PySpark file is parse-validated + plan-validated before being written. New error class `SQL_LINT_FAIL` routes to SQL Specialist. Reuses `tools/optimizer_finder.py` for hotspot detection. |
| **B — Derived features mapping** | Silver contract | `silver_contract.derived_columns` reuses the exact shape of `interns/reports/derived_feature_reviews/json/`. Confirmed derivations lift into Silver as materialized columns. |
| **C — JSON+MD dual format** | All contracts | Every contract artifact has paired `.json` (source of truth) + `.md` (PR review). The MD regenerates from the JSON on every run. |
| **D — CLI `--help` discipline** | All CLIs | Help epilog includes 2–3 example invocations + exit codes table. Matches the discoverability of `gemini --help`, `claude --help`, `codex --help`. |
| **E — Provider/model agnosticism** | Cost & model selection | Replaces static `config/model_tiers.yaml` with runtime discovery via the CLI's own `/model` (or `/models`) command, plus WebSearch-driven classification of each unknown model. No hardcoded model name patterns. Tier assignment derives from the ranked discovered set. |

## 4. Contract catalog

Five contract artifacts. All are written under `workspaces/<ws>/interns/generated/medallion/`. All have paired JSON + MD.

### 4.1 `manifest.yaml` (config — single-format, no MD)

Top-level declarative config. Schema version 1.

Top-level keys: `schema_version`, `workspace`, `generated_at`, `inputs_hash`, `target`, `strict_databricks`, `budget`, `layers.{bronze,silver,gold}`, `kpi_regeneration`.

Per-bronze entry: `name`, `source_file` (repo-relative), `source_system`, `load_strategy=append_watermarked`, `watermark_column`, `natural_key`, `pii_columns`.

Per-silver entry: `name`, `derived_from`, `primary_key`, `load_strategy=merge_on_pk`, `contract` (JSON pointer fragment).

Per-gold entry: `name`, `kind ∈ {fact, dimension}`, `derived_from` (must be `silver.*`), `load_strategy=full_refresh`, `grain` (facts), `scd_type` (dims).

### 4.2 `star_schema.{json,md}`

Star-schema design. Source of truth for grain.

Top-level: `workspace`, `facts[]`, `dimensions[]`, `relationships[]`, `conformed_dimensions[]`, `derivation_reasoning`, `open_questions[]`.

Every `FactTable`, `DimensionTable`, `Relationship` carries `needs_user_confirmation: bool`. Items with `true` must surface through the design panel before `build-medallion` runs.

### 4.3 `silver_contract.{json,md}`

Per-table transformation rules.

Top-level: `workspace`, then `<table_name>: TableContract` per Silver table.

`TableContract` fields: `type_casts`, `null_policies`, `dedup_keys`, `pii_hash_columns`, `hash_salt_ref`, `derived_columns`, `assertions`.

`DerivedColumn` reuses `derived_feature_reviews` shape: `formula_templates.{duckdb_sql, spark_sql, polars}`, `input_columns`, `business_meaning`, `reasoning`, `source_review`, `materialized_at_layer`, `computed_once_reused_by_kpis`.

Five assertion types: `not_null`, `unique`, `row_count_delta`, `referential_integrity`, `sql_plan_property` (the last is evaluated against the pre-commit lint pass).

### 4.4 `lineage.{json,md}`

Column-level derivation graph.

Top-level: `workspace`, `nodes[]`, `edges[]`.

`LineageNode`: `layer`, `table`, `columns[]`.

`LineageEdge`: `from_node`, `from_columns[]`, `to_node`, `to_columns[]`, `transform_type`, `reasoning`.

Helper `trace_to_sources(layer, table, column)` walks edges backwards to Bronze sources.

### 4.5 `data_model_extracted.{json,md}` (only when vision-OCR is invoked)

Materialized output of OCRing `docs/DataModel.png` (or any image data model in `domain_model.data_models[]`). Cached by SHA-256 of the image bytes.

Top-level: `entities[]`, `columns_per_entity{}`, `relationships[]`, `cardinality_hints[]`, `grain_hints[]`, `confidence`, `evidence` (raw OCR snippets).

If a text `data_model.md` exists, this file is not generated — translation mode takes precedence.

## 5. Module map

```
core/medallion/
├── __init__.py                   # public surface
├── manifest.py                   # Manifest + Bronze/Silver/Gold tables + compute_inputs_hash + YAML emit
├── star_schema.py                # StarSchema + Fact/Dim/Relationship + unconfirmed_decisions()
├── silver_contract.py            # SilverContract + TableContract + DerivedColumn + Assertion
├── lineage.py                    # Lineage + Node/Edge + trace_to_sources()
├── contracts_md.py               # JSON → MD renderers (3 contracts)
├── design.py                     # design-medallion orchestrator (P0)
├── design_cli.py                 # CLI entrypoint
├── build.py                      # build-medallion orchestrator (P1, pending)
├── build_cli.py                  # CLI entrypoint (P1)
├── sql_lint.py                   # parse + plan + hotspot lint (P1)
├── delta_emitter.py              # Delta/Spark SQL+PySpark emit (P2)
├── pii.py                        # PII at-rest enforcement helpers (P3)
├── model_discovery.py            # /model dispatch + classification + cache (P4)
├── tier_router.py                # prompt-strategy routing + escalation/de-escalation (P4)
├── budget.py                     # per-run USD cap + cache (P4)
└── mlflow_emit.py                # MLflow run-tagging integration (P5)

interns/
├── medallion_architect.py        # MedallionArchitectIntern

core/orchestration/governor.py    # extended with MEDALLION_ROUTING table (P1)

core/onboarding/
└── workspace_artifact_validator.py  # extended with _validate_medallion_manifest (4 checks P0, +4 in P3)

config/
└── agents.toml                   # medallion_architect added to [interns.healthcare].active

pyproject.toml
└── [project.scripts]
    design-medallion              # P0
    build-medallion               # P1
```

## 6. Integration points (where the new code touches the existing platform)

| Existing file | Change | Phase | Why |
|---|---|---|---|
| `core/agents/registry.py` | Add `medallion_architect` to `_BUILTIN_INTERNS` | P0 | Make the intern instantiable |
| `config/agents.toml` | Add `medallion_architect` to `[interns.healthcare].active` | P0 | Allow it to participate in routing |
| `pyproject.toml` | Add `design-medallion` + `build-medallion` scripts; add `pyyaml` dep | P0/P1 | CLI entrypoints |
| `core/onboarding/workspace_artifact_validator.py` | Add `_validate_medallion_manifest` method | P0 (4 checks) + P3 (+4 checks) | Validate generated manifest |
| `core/orchestration/governor.py` | Add `MEDALLION_ROUTING` dict; teach Governor to dispatch stage-typed errors | P1 | Route Medallion errors back to specialists |
| `core/execution/backend.py` | No code change; the manifest's `target` + `strict_databricks` map onto existing backend selection | P1 + P2 | Reuse existing fallback policy |
| `core/agents/llm_engine.py` | Add `_DISCOVERY_DISPATCH` constant + `discover_models()` helper | P4 | Runtime model discovery via `/model` |
| `interns/sql_specialist.py` | New `regenerate_against_gold()` entry path | P1 | KPI SQL migration |
| `core/onboarding/blocker_question_panel.py` | New `BlockerSource: "medallion_design"` | P1 | Surface star-schema ratifications |
| `tools/optimizer_finder.py` | No code change; called by `sql_lint.py` | P1 | Reuse existing hotspot profiler |

## 7. Data flow

### Design pass (`design-medallion`)

```
read inputs ─→ compute inputs_hash
                     │
                     ▼
            check cache (manifest hash match)
                     │  cache miss
                     ▼
            call MedallionArchitectIntern (LLM, decomposed by tier)
                     │  fallback: deterministic seed proposal
                     ▼
            parse JSON proposal → StarSchema + SilverContract dataclasses
                     │
                     ▼
            derive BronzeTable[] from domain_model + profiles
                     │
                     ▼
            derive GoldTable[] from StarSchema (fact_/dim_ prefixes)
                     │
                     ▼
            build Lineage graph from layer derivation
                     │
                     ▼
            ┌────────┴─────────────────────────────┐
            ▼                                       ▼
       SQL emission (per target)              MD regeneration
       bronze/<name>.<dialect>.sql            star_schema.md
       silver/<name>.<dialect>.sql            silver_contract.md
       silver/_<name>_assertions.sql          lineage.md
       gold/<name>.<dialect>.sql              (data_model_extracted.md when OCR run)
            │                                       │
            └────────┬──────────────────────────────┘
                     ▼
            write manifest.yaml
                     │
                     ▼
            collect unconfirmed_decisions → write design panel
                     │
                     ▼
            print "next command" (build-medallion when zero unconfirmed)
```

### Build pass (`build-medallion`) — P1+

```
read manifest.yaml ─→ verify panel cleared (or proceed --force-with-blockers)
                     │
                     ▼
            select ExecutionBackend (existing factory)
                     │  target=auto → existing build_execution_backend(cfg)
                     ▼
            create per-run state dir at state/medallion/runs/<run_id>/
                     │
                     ▼
            for layer in [bronze, silver, gold]:
                for table in layer:
                    execute SQL via ExecutionBackend
                    on failure → classify error → Governor routing
                                                  │
                            on cap exceeded ─────▶ design panel blocker
                    on Silver: run _<name>_assertions.sql
                               failure → SILVER_ASSERTION_FAILED → Governor
                     │
                     ▼
            invoke SQL Specialist.regenerate_against_gold()
                     │
                     ▼
            row-equality check kpi_metrics_v2.sql vs kpi_metrics.sql
                     │  diff → KPI_ROW_EQUALITY_FAIL blocker
                     ▼
            tag MLflow run; close per-run state dir
                     │
                     ▼
            print summary + degraded_run flag if applicable
```

## 8. The Governor routing table (P1 wiring)

```python
MEDALLION_ROUTING = {
    "BRONZE_LOAD_FAIL":         ("data_engineer",       2),
    "SILVER_TRANSFORM_FAIL":    ("sql_specialist",      2),
    "SILVER_ASSERTION_FAILED":  ("medallion_architect", 2),
    "GOLD_DERIVATION_FAIL":     ("medallion_architect", 2),
    "KPI_ROW_EQUALITY_FAIL":    ("medallion_architect", 1),
    "SQL_LINT_FAIL":            ("sql_specialist",      2),
}
```

Routing rule: classify the error → look up `(specialist, max_retries)` → if retry count < max, dispatch to specialist with the failure context → if cap exceeded, write a blocker entry. No LLM-based error classification. No silent fallback specialist.

## 9. Substrate portability

Two targets in scope: **DuckDB** (local) and **Delta/Spark** (Databricks). The manifest declares one target; the agent emits two parallel files per table:

```
bronze/patients__hospital_a.duckdb.sql      # CREATE OR REPLACE TABLE ... FROM read_csv_auto(...)
bronze/patients__hospital_a.spark.py        # spark.read.format("csv").load(...).write.format("delta")...
silver/patient.duckdb.sql                    # CREATE OR REPLACE TABLE ... WITH unioned AS (...)
silver/patient.spark.py                      # DeltaTable.forName(...).alias("tgt").merge(...)
```

Adding a new substrate (e.g., Iceberg) means: add a new `*_emitter.py` module, extend `Manifest.target` enum, extend the orchestrator's emit phase, extend the validator. No other change.

## 10. Idempotency rules

Two layers of idempotency:

1. **Design pass**: `inputs_hash` is computed as `sha256` of the concatenated `(sorted_relpath, sha256(file_bytes))` for every input artifact. A second `design-medallion` run with unchanged inputs reads the existing manifest, compares the hash, and exits with `cache_hit: true` (no files written, no LLM calls). `--force` overrides.

2. **Build pass**: each table executes idempotently per its declared `load_strategy`:
   - `append_watermarked` (Bronze): dedup on natural_key + load_ts; re-runs against unchanged source files are no-ops.
   - `merge_on_pk` (Silver): MERGE-on-PK; re-runs apply only changed rows.
   - `full_refresh` (Gold): CREATE OR REPLACE; re-runs rebuild deterministically.

## 11. Failure modes and their exit codes

| Code | Phase | Cause | Resolution |
|---|---|---|---|
| `RUN_ONBOARD_FIRST` | design | Missing `domain_model.json` or `profile_index.json` | `uv run onboard-workspace --workspace <ws>` |
| `NO_KPIS_DEFINED` | design | Empty `kpi_registry.json` | `uv run resolve-kpi-features --workspace <ws> --domain <d>` |
| `KPI_BLOCKERS_UNRESOLVED` | design | Blocker panel has unresolved entries | `uv run prepare-kpi-blocker-panel --workspace <ws> --domain <d>` |
| `EMPTY_WORKSPACE` | design | Zero datasets | Add data, re-onboard |
| `WORKSPACE_BUSY` | design / build | Lockfile held by another run | Wait, or remove stale lock |
| `SQL_LINT_FAIL` | design / build | Lint findings exceeded cap | Inspect blocker panel for lint summary |
| `BUDGET_EXCEEDED` | design / build | `max_usd_per_run` hit | `--resume <run_id>` with raised cap |
| `MODEL_DISCOVERY_FAILED` | design | `/model` returned empty | Pin `--model <id>` or check CLI auth |
| `MODEL_SEARCH_FAILED` | design | WebSearch unavailable + no cache | `--no-search` with explicit `--model` pin |
| `INSUFFICIENT_MODEL_CAPABILITY` | design | Discovered set's best tier < required minimum | Enable a higher-tier model |
| `MEDALLION_BUILD_FAIL` | build | Compute failure exceeded retry cap | Check `state/medallion/runs/<run_id>/run.json` |

## 12. Cross-cutting non-negotiables

These are repeated from `README.md` for emphasis; every phase must honor them.

1. Generated artifacts are never hand-edited.
2. `inputs_hash` is the idempotency anchor.
3. Gold derives from Silver only (validator-enforced).
4. Bronze raw + Silver hashed PII; never raw PII in Silver.
5. Composite natural keys by default for multi-source.
6. Every assertion failure routes through the Governor.
7. No silent demotion of compute target.
8. No silent model defaults.
9. Cache key includes `(model_tier, prompt_strategy_version)`.
10. Every contract has paired JSON + MD.
