# 02 — Conventions & Layout

Rules every implementer must follow.

## 1. File layout

### 1.1 Repo-level

```
core/medallion/                # all medallion platform code lives here
interns/medallion_architect.py # the LLM-backed agent
docs/medallion/                # this guide
docs/PRD_medallion_architect.md
```

### 1.2 Per-workspace

```
workspaces/<ws>/interns/generated/medallion/
├── manifest.yaml
├── star_schema.json
├── star_schema.md
├── silver_contract.json
├── silver_contract.md
├── lineage.json
├── lineage.md
├── data_model_extracted.json   # only when vision-OCR was invoked
├── data_model_extracted.md
├── bronze/
│   ├── <table>.duckdb.sql
│   └── <table>.spark.py        # P2+
├── silver/
│   ├── <table>.duckdb.sql
│   ├── <table>.spark.py        # P2+
│   └── _<table>_assertions.sql
└── gold/
    ├── <table>.duckdb.sql
    └── <table>.spark.py        # P2+

workspaces/<ws>/interns/state/medallion/
├── .lock                       # flock-style lockfile (build-medallion)
├── medallion_cache/            # content-addressed LLM cache (P4)
├── bronze/  silver/  gold/     # local-target materialized data (.parquet / .duckdb)
└── runs/
    └── <run_id>/
        ├── manifest_hash.txt
        ├── run.json            # target, started_at, finished_at, per_table_status,
        │                         row_counts_before_after, assertion_results, degraded_run
        └── logs/

workspaces/<ws>/interns/reports/medallion_design_panel/
├── current.json
└── current.md
```

`bronze/` under `state/medallion/` and the entire `state/medallion/bronze/` directory go into the workspace `.gitignore`. Raw Bronze is access-restricted at rest (Decision #10).

## 2. Naming conventions

### 2.1 Table names

| Layer | Convention | Example |
|---|---|---|
| Bronze | `<logical_entity>__<source_system>` | `patient__hospital_a` |
| Silver | `<logical_entity>` (unified across sources) | `patient` |
| Gold facts | `fact_<entity>` | `fact_claim` |
| Gold dimensions | `dim_<entity>` | `dim_patient` |

Bronze double-underscore distinguishes the source-system suffix from any underscores in the entity name. Always lowercase. The agent normalizes by stripping plural `s` and common source suffixes (`_hospital_a`, `_data`, etc.).

### 2.2 File names

| File | Pattern |
|---|---|
| Bronze SQL | `bronze/<table>.duckdb.sql` / `<table>.spark.py` |
| Silver SQL | `silver/<table>.duckdb.sql` / `<table>.spark.py` |
| Silver assertions | `silver/_<table>_assertions.sql` (underscore prefix sorts to top) |
| Gold SQL | `gold/<table>.duckdb.sql` / `<table>.spark.py` |
| Contracts | `<contract>.json` + `<contract>.md` (same stem) |

### 2.3 Column names

| Column | Purpose | Layer |
|---|---|---|
| `_source_system` | Source system identifier | Bronze, Silver |
| `_source_file` | Bronze: relative path of CSV | Bronze |
| `_load_ts` | Bronze: load timestamp | Bronze |
| `source_system` | Promoted in Silver from `_source_system`; part of composite PK | Silver |
| `surrogate_<entity>_id` | Synthetic surrogate key in dimensions | Gold dim |

Underscore-prefixed columns (`_source_system`) are *internal metadata* and never exposed to KPI SQL. The promotion to non-prefixed `source_system` happens at the Bronze→Silver boundary.

### 2.4 Logical entity extraction

Algorithm in `_logical_entity_from_path`:

1. Take filename stem, lowercase.
2. Strip suffix patterns: `(_hospital_[a-z0-9]+|hospital\d+_|_data)$`.
3. Strip prefix patterns: `^hospital\d+_`.
4. Strip trailing plural `s` if present.

Examples:
- `hospital1_claim_data.csv` → `claim`
- `patients.csv` → `patient`
- `cptcodes.csv` → `cptcode`

Override is possible via `workspace_feature_definitions.json`:

```json
{
  "logical_entity_overrides": {
    "datasets/EMR/trendytech-hospital-a/encounters.csv": "encounter_visit"
  }
}
```

## 3. Idempotency rules

### 3.1 The `inputs_hash`

```python
inputs_hash = sha256(
    sorted([
        (str(relpath), sha256(file_bytes))
        for path in hashable_inputs
        if path.exists()
    ])
)
```

`hashable_inputs` is the explicit list:

```python
[
    contracts_dir / "domain_model.json",
    contracts_dir / "kpi_registry.json",
    contracts_dir / "kpi_feature_mapping.json",
    contracts_dir / "semantic_contract.json",
    contracts_dir / "workspace_feature_definitions.json",
    profiles_dir / "profile_index.json",
    *sorted(reports_dir / "derived_feature_reviews" / "json" / "*.json"),
]
```

Adding a new input source to the design pass means: add it to this list AND to the validator's recompute list. **These two lists must stay in lockstep** — drift between them silently breaks the cache-hit path.

### 3.2 The `manifest_hash`

The recorded `manifest.inputs_hash` is the cache key for the design pass:

- New run, hash matches → exit `cache_hit: true`, no writes, no LLM calls.
- New run, hash differs → full regeneration.
- `--force` → ignore cache, regenerate.

### 3.3 Build idempotency

- Bronze re-runs are no-ops on unchanged source files (watermark dedup).
- Silver re-runs apply only changed rows (MERGE).
- Gold re-runs are deterministic (CREATE OR REPLACE).
- Assertions re-run every build (cheap, catches regressions).

## 4. Path conventions

### 4.1 All paths in artifacts are repo-relative POSIX

The manifest never contains absolute paths or Windows backslashes. Reason: artifacts must move with the repo without breaking.

Helper: `_safe_relative_posix(path, repo_root)` in `core/medallion/design.py`.

### 4.2 Workspace paths

In `Manifest.workspace`, store the **directory name only** (e.g., `Healthcare-RCM-Data-Platform`), not the full path. Reason: workspace can be moved between repos.

### 4.3 SQL files reference files by repo-relative path

```sql
FROM read_csv_auto('workspaces/Healthcare-RCM-Data-Platform/datasets/claims/hospital1_claim_data.csv', HEADER=TRUE)
```

This runs from `repo_root` as cwd. If a deployment scenario needs a different cwd, set `AUTORESEARCH_DATA_PREFIX` env var; the SQL emitter is aware of it (P2+).

## 5. Dual-format JSON + MD pattern

### 5.1 The rule

Every contract artifact has paired JSON and MD. JSON is the source of truth; MD is regenerated on every change. **Never hand-edit the MD.** If a reviewer wants to amend the contract, they edit the JSON (or fix the agent's prompt) and re-run `design-medallion`.

### 5.2 MD section template

Match the existing `interns/reports/derived_feature_reviews/md/` template:

```markdown
# <Contract Name> — <Workspace>

Generated by the Medallion Architect. Source of truth is `<contract>.json`.
This document is regenerated; do not hand-edit.

## Why This Was Proposed

<the `derivation_reasoning` / `reasoning` field from the JSON>

## <Per-entity section>

### `<entity>`

- **<field>**: <value>
...

**Why This Was Proposed**

<reasoning field if present>

**Evidence**

- <evidence_sources items>

> **Needs user confirmation** — <which fields are agent proposals>

## Remaining Risk / Open Questions

- <open_questions items>
```

### 5.3 Why this pattern

- JSON for automation: easy to diff, easy to validate against schema, easy to consume programmatically.
- MD for review: GitHub renders it nicely in PRs; reviewers see structured human-readable form.
- Regeneration prevents drift: there's only ever one source of truth.

## 6. Secret handling

### 6.1 What is a secret here

- The PII hash salt (`workspace.medallion_salt`).
- LLM API keys (provider tokens, OpenAI/Anthropic/Google keys).
- Databricks tokens.

### 6.2 Where secrets live

- **Salt**: a workspace secret store (P3). Concrete options in priority order:
  1. Databricks secret scope (`workspace.medallion_salt`) when Databricks is configured.
  2. `~/.config/autoresearch/secrets.toml` (gitignored).
  3. Environment variable `AUTORESEARCH_WORKSPACE_SALT__<workspace>`.
- **API keys**: existing `core/config.py` patterns (`google_api_key`, etc.). No change.

### 6.3 What must never happen

- Salt or token values never appear in `manifest.yaml`, `state/medallion/runs/`, MLflow logs, or stdout.
- The CLAUDE.md "secret display is a hard stop" rule applies. Only `hash_salt_ref: workspace.medallion_salt` appears in artifacts — never the salt value.
- If a stack trace would print a token, the relevant code wraps the call in a try-except and re-raises with a redacted message.

## 7. Error handling discipline

### 7.1 Categories

| Category | Surface | Example |
|---|---|---|
| **Hard fail (deterministic)** | Exit with `MedallionExit(code, ..., next_command)` | Missing onboarding artifacts |
| **Recoverable via Governor** | Raise typed error → Governor routes to specialist | Silver assertion failure |
| **Soft surfaced via blocker panel** | Write to `medallion_design_panel/current.json` | Ambiguous relationship |
| **Logged but not surfaced** | `logger.info(...)` | Cache hit |

### 7.2 No silent fallbacks

- Compute failure under permissive mode falls back to DuckDB **with `degraded_run: true` recorded**. The fallback is loud — not hidden.
- LLM failure during design falls back to the deterministic seed proposal **with `llm_used: false` recorded** in the result. The fallback is loud.
- Cache hits are logged. Misses are logged. Tier classifications are logged.

### 7.3 No swallowed exceptions

Every `except Exception:` block either re-raises, returns a typed result the caller checks, or records the error to the run state. Never silently `pass`.

## 8. Validation discipline

### 8.1 When to validate

Validators run at three boundaries:

1. **At design end**: `validate-workspace-artifacts` checks 1–4 (P0) and 5–8 (P3) against the freshly written manifest.
2. **At build start**: re-runs the same validator before executing any SQL. Catches manual edits to the manifest.
3. **In CI** (recommended): run validators on every PR that touches `workspaces/*/interns/generated/medallion/`.

### 8.2 Validation failure = block

A validation error is a hard fail, not a warning. The build does not proceed. This is the structural defense against PII leaks and grain errors that bypass other checks.

## 9. Logging discipline

### 9.1 Levels

- **INFO**: every public state transition (run started, table generated, assertion passed).
- **WARNING**: degraded mode (LLM fallback, DuckDB fallback, light-tier model).
- **ERROR**: validation failure, assertion failure, build halt.
- **DEBUG**: prompt content, full LLM response, EXPLAIN output.

### 9.2 Format

Use the existing `logging` module configured in `core/orchestration/governor.py`. New modules import `logger = logging.getLogger(__name__)`. No `print()` in library code — only CLI entrypoints (`design_cli.py`, `build_cli.py`) print.

### 9.3 Per-run logs

Every `build-medallion` run writes a structured `state/medallion/runs/<run_id>/logs/build.log`. Format: JSONL, one event per line. Schema: `{ts, level, table, layer, event, details}`.

## 10. The "don't reinvent" list

Before writing new code, check if the platform already provides it:

| Need | Existing surface |
|---|---|
| Argparse + entrypoint | `core/onboarding/workspace_onboarding.py` pattern |
| Workspace path resolution | `core.storage.workspace_layout.WorkspaceLayout` |
| LLM call | `core.agents.llm_engine.{APIEngine, CLIEngine}` |
| Intern lifecycle | `interns/base.py: InternBase` |
| Config load | `core.config.load()` |
| Profile read | `interns/generated/profiles/profile_index.json` |
| KPI registry | `interns/generated/contracts/kpi_registry.json` |
| Blocker panel | `core/onboarding/blocker_question_panel.py` |
| Confirmed derived feature shape | `interns/reports/derived_feature_reviews/json/*.json` |
| PII mask logic | the existing PII masker (referenced by `semantic_contract.json` fields) |
| ExecutionBackend | `core/execution/backend.py: build_execution_backend(cfg)` |
| Strict mode flag | `core/execution/backend.py: _strict_databricks(cfg)` |
| Hotspot profiler | `tools/optimizer_finder.py` |
| MLflow integration | existing per Databricks integration (memory: EVOLUTION.md) |

Reuse these. Adding a parallel mechanism is a review red flag.
