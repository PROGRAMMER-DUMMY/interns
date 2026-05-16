# 10 — Operations Runbook

Day-to-day operations: how to run the agent, what to do when something breaks, and how to recover from common failure modes.

## 1. Standard workflow

### First run on a new workspace

```bash
# 1. Onboarding (must already have been done)
uv run onboard-workspace --workspace workspaces/<ws>

# 2. Resolve KPI features and blockers
uv run resolve-kpi-features --workspace workspaces/<ws> --domain healthcare --include-candidates
uv run prepare-kpi-blocker-panel --workspace workspaces/<ws> --domain healthcare
# Resolve any items in interns/reports/blocker_question_panel/current.md

# 3. (P3+) Initialize the PII salt for this workspace
uv run python -c "from core.medallion.salt_store import materialize_salt_if_missing; materialize_salt_if_missing('<ws>')"

# 4. Design the medallion pipeline
uv run design-medallion --workspace workspaces/<ws>

# 5. Review the design panel
cat workspaces/<ws>/interns/reports/medallion_design_panel/current.md
# Resolve any unconfirmed_decisions in the panel

# 6. Re-run design (cache hit if nothing changed; otherwise produces ratified manifest)
uv run design-medallion --workspace workspaces/<ws>

# 7. Validate
uv run validate-workspace-artifacts --workspace workspaces/<ws>

# 8. Build (P1+)
uv run build-medallion --workspace workspaces/<ws>

# 9. Trace a column (P5)
uv run medallion-lineage trace --workspace workspaces/<ws> --column gold.fact_claim.claim_amount
```

### Iterating (after a source data change)

```bash
# Onboarding picks up new datasets
uv run onboard-workspace --workspace workspaces/<ws>

# Design notices the new inputs_hash and regenerates
uv run design-medallion --workspace workspaces/<ws>

# Validate + build
uv run validate-workspace-artifacts --workspace workspaces/<ws>
uv run build-medallion --workspace workspaces/<ws>
```

### Running cheaply during local development

```bash
uv run design-medallion --workspace workspaces/<ws> --cheap --dry-run
# No LLM call, no file writes — confirms the cache will hit or what would change
```

## 2. Required Databricks grants (for `--target delta`)

The build identity needs:

```sql
-- Bronze schema (write only by build; read by audit only)
CREATE SCHEMA IF NOT EXISTS <ws>_medallion_bronze;
GRANT CREATE TABLE, MODIFY ON SCHEMA <ws>_medallion_bronze TO `<build-service-principal>`;
GRANT USAGE ON SCHEMA <ws>_medallion_bronze TO `<build-service-principal>`;
GRANT SELECT ON SCHEMA <ws>_medallion_bronze TO `audit-role`;
-- explicitly no grants for consumer roles

-- Silver schema (write by build; read by consumers)
CREATE SCHEMA IF NOT EXISTS <ws>_medallion_silver;
GRANT CREATE TABLE, MODIFY ON SCHEMA <ws>_medallion_silver TO `<build-service-principal>`;
GRANT USAGE ON SCHEMA <ws>_medallion_silver TO `<build-service-principal>`, `analytics-consumers`;
GRANT SELECT ON SCHEMA <ws>_medallion_silver TO `analytics-consumers`;

-- Gold schema (write by build; read by all KPI consumers)
CREATE SCHEMA IF NOT EXISTS <ws>_medallion_gold;
GRANT CREATE TABLE, MODIFY ON SCHEMA <ws>_medallion_gold TO `<build-service-principal>`;
GRANT USAGE ON SCHEMA <ws>_medallion_gold TO PUBLIC;  -- or appropriate role
GRANT SELECT ON SCHEMA <ws>_medallion_gold TO PUBLIC;
```

Replace `<ws>` with the workspace's normalized name (lowercase, `-` → `_`).

Verify grants:

```sql
SHOW GRANTS ON SCHEMA <ws>_medallion_silver;
```

## 3. Troubleshooting

### Exit code: `RUN_ONBOARD_FIRST`

**Symptom**: `[design-medallion] RUN_ONBOARD_FIRST: Missing onboarding artifacts ...`

**Cause**: `domain_model.json` or `profile_index.json` is absent.

**Fix**:
```bash
uv run onboard-workspace --workspace workspaces/<ws>
```

If onboarding fails: ensure `workspaces/<ws>/datasets/` has CSV/parquet files. Inspect with `uv run list-workspace-files --workspace workspaces/<ws>`.

### Exit code: `NO_KPIS_DEFINED`

**Symptom**: KPI registry is missing or empty.

**Fix**:
```bash
uv run resolve-kpi-features --workspace workspaces/<ws> --domain <domain> --include-candidates
```

### Exit code: `KPI_BLOCKERS_UNRESOLVED`

**Symptom**: KPI blocker panel has unresolved entries.

**Fix**:
```bash
uv run prepare-kpi-blocker-panel --workspace workspaces/<ws> --domain <domain>
cat workspaces/<ws>/interns/reports/blocker_question_panel/current.md
# answer each blocker:
uv run apply-kpi-panel-answer --workspace workspaces/<ws> --domain <domain> --answer <option_id>
```

### Exit code: `WORKSPACE_BUSY`

**Symptom**: Another `build-medallion` is in progress, or a stale lockfile from a crashed run.

**Diagnose**:
```bash
cat workspaces/<ws>/interns/state/medallion/.lock  # shows pid + timestamp
ps -p <pid>  # or Get-Process <pid> on Windows
```

**Fix**: If the holder process is alive, wait. If dead, remove the lockfile:

```bash
rm workspaces/<ws>/interns/state/medallion/.lock
```

### Exit code: `SQL_LINT_FAIL`

**Symptom**: Lint rules rejected an emitted SQL file.

**Diagnose**:
```bash
cat workspaces/<ws>/interns/reports/medallion_design_panel/current.md
# scroll to the lint findings section
```

**Fix**: Usually the underlying issue is bad schema mapping or a misnamed column. Resolve the panel entry; re-run `design-medallion`. If the lint rule itself is wrong, the fix is in `core/medallion/sql_lint.py` — not the SQL.

### Exit code: `BUDGET_EXCEEDED`

**Symptom**: LLM USD spend hit the `max_usd_per_run` cap mid-run.

**Diagnose**:
```bash
cat workspaces/<ws>/interns/state/medallion/runs/<run_id>/budget.json
```

**Fix**:
- Raise the cap temporarily: edit `manifest.yaml: budget.max_usd_per_run` and re-run with `--resume <run_id>`.
- Or: run with `--cheap` to use the deterministic seed (no LLM cost), accept lower quality, then iterate.
- Or: investigate why cost spiked — usually a model picking a heavy tier when a medium-tier minimum task was retried.

### Exit code: `MODEL_DISCOVERY_FAILED`

**Symptom**: `/model` returned empty; agent doesn't know what's available.

**Diagnose**: Run the CLI's `/model` command manually:
```bash
claude /model       # for claude-code
gemini /models      # for gemini-cli
codex /model        # for codex
```

**Fix**:
- If the CLI itself errors: fix CLI auth.
- If the CLI works but the agent doesn't see output: open `core/medallion/model_discovery.py:_parse_discovery_output` and adapt the parser to the actual format.
- Workaround: pin with `--engine <engine> --model <id>` to skip discovery.

### Exit code: `MODEL_SEARCH_FAILED`

**Symptom**: WebSearch couldn't classify an unknown model and the cache had nothing.

**Fix**:
- Re-run with network access available.
- Or: classify manually by adding to `core/agents/state/model_classification_cache.json`:
  ```json
  {
    "claude-code:custom-internal-model": {
      "engine": "claude-code",
      "model_id": "custom-internal-model",
      "parameter_count": 70.0,
      "context_window": 128000,
      "vision_capable": false,
      "benchmark_composite": 0.6,
      "classified_at": "2026-05-15T00:00:00Z",
      "evidence": ["manual classification"],
      "confidence": 0.5
    }
  }
  ```
- Or: pass `--no-search --model <known-id>` to force a specific model.

### Exit code: `INSUFFICIENT_MODEL_CAPABILITY`

**Symptom**: All discovered models are light-tier; a task requires medium.

**Fix**:
- Enable a higher-tier model in the active CLI session.
- Or: lower the minimum (NOT recommended for `star_schema_design` — quality cliff is real).

### Validator reports `bronze.<table>.natural_key is empty`

**Symptom**: Warning, not error. The agent didn't detect a primary key in this Bronze table.

**Diagnose**: Inspect the schema:
```bash
cat workspaces/<ws>/interns/generated/profiles/datasets_<file>.profile.json | jq '.schema'
```

**Fix**: Add a hint in `workspace_feature_definitions.json`:

```json
{
  "bronze_natural_key_overrides": {
    "department__hospital_a": ["DeptID"]
  }
}
```

Re-run `design-medallion --force`.

### Validator reports `PII column X is not hashed in any Silver table`

**Symptom**: P3 validator check 6 fired.

**Cause**: `semantic_contract.json` flagged the column as PII but the agent's Silver design doesn't include it in `pii_hash_columns`.

**Fix**: Resolve the design panel entry asking about this column. If the column should not appear in Silver at all, add to `workspace_feature_definitions.json`:

```json
{
  "silver_column_exclusions": ["<table>.<column>"]
}
```

### Build: `SILVER_ASSERTION_FAILED`

**Symptom**: A Silver post-load assertion violated.

**Diagnose**:
```bash
cat workspaces/<ws>/interns/state/medallion/runs/<run_id>/run.json | jq '.per_table_status'
```

The `assertions` block shows which assertion failed. Then:

```bash
# Run the assertion SQL manually to see violating rows
duckdb workspaces/<ws>/interns/state/medallion/local.duckdb < \
  workspaces/<ws>/interns/generated/medallion/silver/_<table>_assertions.sql
```

**Fix**: Usually means upstream data has the issue (nulls in a not-null PK; duplicate rows from a misconfigured source). Either correct the source data or relax the assertion in `silver_contract.json` (NOT recommended without judgment — the assertion exists for a reason).

### Build: degraded_run after compute failure

**Symptom**: Run succeeded but `run.json` shows `degraded_run: true` and `target_actual: "duckdb"` despite declared target `delta`.

**Diagnose**:
```bash
cat workspaces/<ws>/interns/state/medallion/runs/<run_id>/run.json | jq '.compromise_history'
```

Common causes: cluster won't start, network outage, Spark OOM.

**Fix**:
- If transient: re-run; if successful, `degraded_run: false`.
- If persistent: investigate Databricks side. For strict environments, set `AUTORESEARCH_DATABRICKS_STRICT=1` so the next run halts loudly instead of silently demoting.
- Mark KPI results from this run as untrusted (degraded data path).

## 4. Salt rotation (P3+)

Rotation is a clean cut — old hashes are invalidated.

```bash
# 1. Generate a new salt
NEW_SALT=$(python -c "import secrets; print(secrets.token_hex(32))")

# 2. Store it (Databricks secret scope path shown; adapt for OS env / secrets.toml)
databricks secrets put-secret autoresearch medallion_salt__<ws> --string-value $NEW_SALT

# 3. Rebuild Silver (mandatory — old hashes are now invalid)
uv run build-medallion --workspace workspaces/<ws> --rebuild-silver

# 4. Communicate to consumers: Gold IDs have changed
```

After rotation, joins between Silver/Gold and any externally-stored dataset that referenced the old hashed values will break. This is the cost of rotation — accept it consciously.

## 5. Rollback procedures

### Rollback a bad design (manifest)

The design is regenerable from `inputs_hash`. If a design pass produced bad output:

```bash
# revert generated/medallion/ to a known-good state via git
git checkout HEAD~1 -- workspaces/<ws>/interns/generated/medallion/

# force-regenerate using whatever inputs are now in place
uv run design-medallion --workspace workspaces/<ws> --force
```

### Rollback a bad build

DuckDB target: drop and re-run.

```bash
rm workspaces/<ws>/interns/state/medallion/local.duckdb
uv run build-medallion --workspace workspaces/<ws>
```

Delta target: requires careful Delta time-travel.

```sql
-- Inspect history
DESCRIBE HISTORY <ws>_medallion_silver.patient;

-- Restore to a prior version
RESTORE TABLE <ws>_medallion_silver.patient TO VERSION AS OF <n>;
```

After restore, run `build-medallion --only-layer gold` to rebuild downstream Gold.

## 6. Monitoring

### Local

Per-run state files:

```bash
ls -lt workspaces/<ws>/interns/state/medallion/runs/ | head -5
cat workspaces/<ws>/interns/state/medallion/runs/<latest>/run.json | jq .
```

### Databricks

MLflow experiments under `/medallion/<workspace>`:

- Filter by `tags.degraded_run = "True"` to find degraded runs.
- Trend `metrics.row_count_delta.silver_<table>` over time.
- Track `metrics.assertions_pass_rate.<table>` for data quality drift.

Suggested alerts (Databricks SQL or external):

- `degraded_run == True` for any run in the last 24h.
- `assertions_pass_rate.* < 1.0` for any run.
- `row_count_delta.silver_<table> == 0` for N consecutive runs (input source stale?).

## 7. Common operator FAQs

**Q: design-medallion exited with `cache_hit: true` but I want to regenerate.**
A: `--force`.

**Q: I edited the manifest by hand. Now what?**
A: Don't. Regenerate. If you must keep your edit, copy your intent into `workspace_feature_definitions.json` so the next design pass produces the same effect.

**Q: Can I run two workspaces in parallel?**
A: Yes. Lockfiles are per-workspace.

**Q: How do I disable the LLM entirely?**
A: `--cheap` on every `design-medallion` invocation. Quality is lower; suitable for offline iteration.

**Q: Can I add a new dataset without re-onboarding?**
A: No. Re-run `onboard-workspace` first. The agent reads `domain_model.json` which is onboarding's output.

**Q: How do I see what changed between two runs?**
A:
```bash
git diff <prev-commit> -- workspaces/<ws>/interns/generated/medallion/
diff workspaces/<ws>/interns/state/medallion/runs/<old>/run.json workspaces/<ws>/interns/state/medallion/runs/<new>/run.json
```

**Q: A KPI value changed unexpectedly between runs.**
A: This should have surfaced as a `KPI_ROW_EQUALITY_FAIL` blocker. If it didn't, the row-equality check is broken or was bypassed — file a bug. To investigate:
```bash
diff <(duckdb local.duckdb "SELECT * FROM gold.kpi_001_result ORDER BY 1") \
     <(duckdb local.duckdb.bak "SELECT * FROM gold.kpi_001_result ORDER BY 1")
```

**Q: Can I exclude a specific Bronze table from the build?**
A: `build-medallion --only-layer silver --only-table <name>` skips Bronze for that table. If you need to permanently exclude a source, remove it from the workspace and re-onboard.

## 8. Glossary

| Term | Meaning |
|---|---|
| **Bronze** | Raw source data loaded as-is, plus `_source_system` / `_source_file` / `_load_ts` metadata columns |
| **Silver** | Cleaned, type-cast, deduplicated, PII-hashed data unified across sources |
| **Gold** | Star-schema fact and dimension tables; full-refresh on every build |
| **Grain** | The "one row per X" declaration for a fact table |
| **Conformed dimension** | A dimension shared by multiple facts (e.g., `dim_patient` used by both `fact_claim` and `fact_encounter`) |
| **SCD Type 1** | Slowly-changing dimension that overwrites on change (no history) |
| **SCD Type 2** | Slowly-changing dimension that preserves history via effective-date rows |
| **Watermarked append** | Bronze load strategy using `_load_ts` + natural_key for idempotent re-runs |
| **MERGE-on-PK** | Silver load strategy: insert new rows, update existing on primary-key match |
| **Full refresh** | Gold load strategy: drop and rebuild deterministically |
| **`inputs_hash`** | Idempotency key for the design pass |
| **Manifest hash** | The `inputs_hash` recorded in `manifest.yaml` |
| **Degraded run** | A build that fell back from declared target (e.g., delta) to DuckDB |
| **Strict mode** | `cfg.databricks.fallback == "fail"` or `AUTORESEARCH_DATABRICKS_STRICT=1`; disables silent fallback |
| **Salt** | Workspace-scoped secret string concatenated to PII before SHA-256 hashing |
| **Design panel** | `medallion_design_panel/current.{json,md}`; surface for ratifying agent proposals |
| **Tier** | heavy / medium / light; assigned dynamically from discovered model ranking |
| **Minimum tier** | Per-task-class floor; agent picks cheapest available tier ≥ minimum |
