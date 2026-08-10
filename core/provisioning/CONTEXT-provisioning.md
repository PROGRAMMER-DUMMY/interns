# Core Provisioning Architecture Context: `core/provisioning`

This document provides an exhaustive reference for all components in [`core/provisioning`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/provisioning).

---

## Executive Overview & Architectural Model

The `core/provisioning` package owns the additive half of the cloud-first spine: planning Unity Catalog objects offline (`plan-provisioning`), applying them behind the one human confirmation (`apply-provisioning`), generating ingestion code per discovered table (`generate-ingestion`), running that code against the warehouse (`run-ingestion`), and shipping generated code to the Databricks workspace (`sync-workspace-code`).

Every mutating command shares one refusal ladder, in order: **no confirmed blueprint -> dry run**, **`AUTORESEARCH_ALLOW_REMOTE_EXECUTION=0` -> refuse**, **remote unreachable -> structured failure** pointing at `check-platform-readiness`. Nothing is ever a traceback, and nothing destructive is executed -- `blocked_destructive` steps exist only to say "a human must decide".

```
plan-provisioning ──► provision_plan.json ──► apply-provisioning ──► Unity Catalog
                                                     │
generate-ingestion ──► ingestion/*.sql + jobs_manifest.json ──► run-ingestion ──► bronze
                                                     │
                                              sync-workspace-code ──► /Workspace/Shared/<ws>/
```

---

## File Details

### 1. [`apply.py`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/provisioning/apply.py)

- **Exact Purpose**: Executes a provision plan against Unity Catalog additively and idempotently. Every step checks existence first, so a re-run after a partial failure resumes rather than duplicating.
- **Key Functions / Classes**:
  - [`ProvisioningApi`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/provisioning/apply.py#L56-L71): Protocol seam so tests substitute a fake without a Databricks account. Implemented by [`SdkUnityCatalogApi`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/databricks/uc_intake.py#L99).
  - [`ApplyResult`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/provisioning/apply.py#L74-L93): Result dataclass carrying per-step records and `created`/`existing`/`blocked`/`failed` counts; `summary()` attaches the `provisioning` stage roster.
  - [`apply_provision_plan(repo_root, workspace, *, dry_run=None, api=None)`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/provisioning/apply.py#L135-L233): Main entry point. `dry_run` defaults OFF only when the confirmed blueprint is present.
  - [`covering_external_location(url, locations)`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/provisioning/apply.py#L243-L261): Name of an existing external location whose URL overlaps `url`, else `""`. **Unity Catalog identifies external locations by URL prefix, not by name**, and forbids overlap in either direction, so a parent and a child both count. Compares on path-segment boundaries, so `s3://bkt/pfx2/` does not cover `s3://bkt/pfx/`. (F14)
  - [`confirmed_blueprint_path(layout)`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/provisioning/apply.py#L95-L97): Path of the single recorded human confirmation.
  - Status constants: `STATUS_APPLIED`, `STATUS_DRY_RUN`, `STATUS_REFUSED_NO_CONFIRMATION`, `STATUS_REFUSED_KILL_SWITCH`, `STATUS_REFUSED_UNAVAILABLE`, `STATUS_FAILED`.
- **Inputs & Outputs**:
  - *Inputs*: `provision_plan.json`, the confirmed-blueprint marker, `AUTORESEARCH_ALLOW_REMOTE_EXECUTION`.
  - *Outputs*: `ApplyResult` and `interns/generated/evidence/provisioning/apply_log.json`.
- **Failure Modes & Edge Cases**:
  - A name miss on an external location does **not** mean the path is free; the overlap check reuses the covering location because no create could ever succeed there.
  - A failing step stops the loop -- later objects depend on earlier ones, so pressing on only yields cascading secondary errors.

### 2. [`cli.py`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/provisioning/cli.py)

- **Exact Purpose**: Governed CLI entry points, each wrapped in `run_workspace_command` for locking, telemetry, trajectory and idempotency.
- **Key Functions / Classes**:
  - [`plan_provisioning_main`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/provisioning/cli.py#L16-L60) -- `plan-provisioning`. Flags: `--catalog`, `--env`, `--schema` (repeatable), `--grant-principal` (repeatable), `--storage-root`.
  - [`apply_provisioning_main`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/provisioning/cli.py#L63-L115) -- `apply-provisioning`. `--dry-run` / `--no-dry-run`, `--allow-replay`.
  - [`generate_ingestion_main`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/provisioning/cli.py#L118-L135) -- `generate-ingestion`.
  - [`run_ingestion_main`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/provisioning/cli.py#L138-L177) -- `run-ingestion`. Same dry-run semantics as apply.
  - [`sync_workspace_code_main`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/provisioning/cli.py#L180-L230) -- `sync-workspace-code`.
- **Inputs & Outputs**:
  - *Inputs*: `--workspace`, `--repo-root`, per-command flags.
  - *Outputs*: JSON envelope on stdout; exit 0 on success, 1 on a structured refusal, 2 on workspace-lock timeout.
- **Failure Modes & Edge Cases**:
  - `--storage-root` is documented as **never derived** from the source location: where managed data physically lives is a residency decision.

### 3. [`ingestion.py`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/provisioning/ingestion.py)

- **Exact Purpose**: Generates Databricks-native ingestion code per discovered table -- `COPY INTO` for batch, Auto Loader for streaming -- into `workspaces/<project>/ingestion/` (git-tracked). It generates; it runs nothing.
- **Key Functions / Classes**:
  - [`generate_ingestion(repo_root, workspace)`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/provisioning/ingestion.py): Emits one `.sql` per table plus `jobs_manifest.json`.
- **Inputs & Outputs**:
  - *Inputs*: `discovery.json`, `provision_plan.json`, the source declaration.
  - *Outputs*: `ingestion/ingest_<table>.sql`, `ingestion/jobs_manifest.json` (`job_count`, `blocked_count`, per-job `method`/`trigger`/`idempotency`).
- **Failure Modes & Edge Cases**:
  - Emitted `COPY INTO` files open with a bare `CREATE TABLE IF NOT EXISTS <t>;` -- the documented Databricks pattern for schema inference under `mergeSchema`, not a missing schema.
  - `FORMAT_OPTIONS` is **format-aware**, not a fixed string: delimited text (`_TEXT_DELIMITED_FORMATS`, currently `CSV`) also gets `'header' = 'true', 'inferSchema' = 'true'`. COPY INTO defaults CSV `header` to false, which lands the header line as a data row and names every column `_c0, _c1, ...` -- silently wrong bronze rather than a loud failure (F23). Self-describing formats (parquet/avro/orc) carry their own names and must NOT receive these options. Delimited text also gets `'rescuedDataColumn' = '_rescued_data'`, and the Auto Loader path sets `cloudFiles.rescuedDataColumn` alongside `cloudFiles.schemaEvolutionMode=addNewColumns`. The two cover different drift: `addNewColumns` absorbs an ADDED column, while a value whose TYPE changed underneath parses to NULL -- indistinguishable from a real null, and silently wrong by the time it reaches a KPI. Rescued, it stays visible and queryable (`WHERE _rescued_data IS NOT NULL`), which is what gives a bronze failure something to inspect. `rescuedDataColumn` is independent of the evolution mode, so both apply.
  - `FROM '<path>'` is `DiscoveredTable.path` verbatim, so discovery must record a location that actually exists -- see `core/intake/CONTEXT-intake.md` and F22.
  - **Idempotency covers the consumed artifact, not just the flags.** `apply-provisioning` folds `fingerprint_paths(provision_plan.json)` into `op_args` as `plan_fingerprint`, and `run-ingestion` folds `jobs_manifest.json` in as `manifest_fingerprint`. Without this, three runs against three materially different plans shared one `op_id` and the envelope reported "this exact call was already applied" about a call whose plan had changed (F16). Any new apply-style command that READS a generated artifact must do the same.
  - Auto Loader must not set `cloudFiles.useNotifications=true` where the bucket denies `s3:GetBucketNotification`; directory-listing mode is used instead. (F2)

### 4. [`ingestion_run.py`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/provisioning/ingestion_run.py)

- **Exact Purpose**: Executes the generated ingestion jobs against the SQL warehouse, in manifest order, behind the same refusal ladder as `apply.py`. Closes the gap where the platform emitted runnable code with no governed way to run it -- which forced operators to the raw vendor CLI and around the kill switch. (F17)
- **Key Functions / Classes**:
  - [`SqlRunner`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/provisioning/ingestion_run.py#L54-L59): Protocol (`execute(sql)`) so tests record statements and never reach a warehouse. `execute` returns result rows when the runner can read them back, else `None` — and `None` means UNVERIFIED, never verified-empty.
  - **Landed-row verification.** After a job's statements run, `_landed_rows()` issues `SELECT COUNT(*)` against the target and the job FAILS when it holds 0 rows while `_discovered_size()` shows discovery measured that source as non-empty. `COPY INTO` succeeds when it matches no file — that is its file bookkeeping working as designed — so without this an empty bronze table reads as a green run all the way to a KPI that silently returns nothing (the handbook's "zero-row detection"). Three deliberate boundaries: the count is a separate statement rather than parsing `COPY INTO`'s own output, because a legitimate re-run copies zero NEW files into an already-full table; `discovery.json` carries no `row_estimate` (the scan reads sizes, not contents) so `size_bytes` is the only available evidence of "the source was not empty"; and a runner that cannot report counts must not have a verdict fabricated for it.
  - [`IngestionRunResult`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/provisioning/ingestion_run.py#L62-L82): Counts `executed`/`failed`/`not_attempted`/`blocked`; `summary()` attaches the `ingestion_generation` roster.
  - [`sql_statements(text)`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/provisioning/ingestion_run.py#L85-L99): Executable statements from a generated file, `--` comments dropped. The SQL Statement Execution API takes one statement per call.
  - [`load_jobs_manifest(layout)`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/provisioning/ingestion_run.py#L102-L109): Reads `ingestion/jobs_manifest.json`; raises `FileNotFoundError` naming `generate-ingestion`.
  - [`run_ingestion_jobs(repo_root, workspace, *, dry_run=None, runner=None)`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/provisioning/ingestion_run.py#L124-L214): Main entry point.
  - [`_sdk_runner(layout)`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/provisioning/ingestion_run.py#L112-L121): Warehouse-backed runner via [`DatabricksClient`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/execution/databricks_client.py#L19) with config from `resolve_databricks_config(layout.enterprise_id())`.
- **Inputs & Outputs**:
  - *Inputs*: `ingestion/jobs_manifest.json` and the `.sql` files it names.
  - *Outputs*: `interns/generated/evidence/ingestion/run_log.json` with per-job outcomes.
- **Failure Modes & Edge Cases**:
  - Stops at the first failing job and marks the rest `not_attempted`; a warehouse that rejected one statement usually rejects the rest, and a wall of secondary errors buries the real one.
  - Re-running is **not** refused: `COPY INTO` skips files it already ingested, so a second run is a legitimate no-op.
  - `execute_query` polls past its wait timeout, so a cold-starting warehouse does not read as a failure.

### 5. [`plan.py`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/provisioning/plan.py)

- **Exact Purpose**: Builds the ordered, additive provision plan offline. The planner never queries Unity Catalog; `apply.py` re-checks existence per step.
- **Key Functions / Classes**:
  - Step kinds: `KIND_CATALOG`, `KIND_SCHEMA`, `KIND_EXTERNAL_LOCATION`, `KIND_VOLUME`, `KIND_GRANT`, `KIND_BLOCKED`.
  - [`build_steps(..., storage_root="")`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/provisioning/plan.py#L139-L240): Ordered steps. A definition mismatch on an existing object, and a grant against a pre-existing catalog, are the only `blocked_destructive` cases.
  - [`build_provision_plan(repo_root, workspace, *, catalog, env, schemas, grant_principals, storage_root="")`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/provisioning/plan.py#L243-L335): Writes `interns/generated/contracts/provision_plan.json`.
- **Inputs & Outputs**:
  - *Inputs*: source declaration, `discovery.json` (`existing_objects`), catalog/env naming, optional `storage_root`.
  - *Outputs*: `provision_plan.json` and a `ProvisionPlan` summary.
- **Failure Modes & Edge Cases**:
  - `storage_root` becomes the catalog's `MANAGED LOCATION`. It is **required** on a metastore whose own `storage_root` is unset (Default Storage accounts), which otherwise rejects a rootless `CREATE CATALOG`; omitted, the metastore root is inherited exactly as before. (F15)

### 6. [`sync_code.py`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/provisioning/sync_code.py)

- **Exact Purpose**: Ships generated code to the Databricks workspace with `databricks sync` (CLI -- incremental and extension-preserving; never `workspace import-dir`, which strips `.py`/`.sql`).
- **Key Functions / Classes**:
  - [`CODE_DIRS`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/provisioning/sync_code.py#L45): `("ingestion", "dbt", "context")` -- **the authority on which directories the platform writes into a workspace**. [`tools/list_workspace_files.py`](file:///C:/Users/shubh/OneDrive/Desktop/interns/tools/list_workspace_files.py) keeps a pinned copy so it never reads these back as source data. (F18)
  - [`EXCLUDES`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/provisioning/sync_code.py#L47): `target/`, `dbt_packages/`, `logs/`, `.venv/`, `__pycache__/`.
  - [`sync_workspace_code(repo_root, workspace, ...)`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/provisioning/sync_code.py): Publishes curated artifacts into `<workspace>/context/`, then pushes `ingestion/` and `dbt/` to the remote root.
- **Inputs & Outputs**:
  - *Inputs*: generated `ingestion/` and `dbt/`, `--remote-root`, confirmation state.
  - *Outputs*: `sync_log.json` with a per-directory outcome.
- **Failure Modes & Edge Cases**:
  - Refuses with "no generated code to ship" when neither `generate-ingestion` nor `generate-dbt-project` has run. Never echoes profile or host values.

---

## 🧹 Code Hygiene & Integrity Audit

- 💀 **Dead Code**: None.
- 🔌 **Unwired Components**: None. All five commands are registered in [`pyproject.toml`](file:///C:/Users/shubh/OneDrive/Desktop/interns/pyproject.toml) and indexed in `.agents/tools.json`.
- 👯 **Logic & Code Duplication**: The refusal ladder (confirmation -> kill switch -> unreachable) is implemented separately in `apply.py` and `ingestion_run.py`. Deliberate for now -- the step/job records differ in shape -- but it is the obvious extraction if a third mutating command appears.
- ⚠️ **Broken References & Mismatches**: None. `ProvisioningApi` and `UnityCatalogApi` are kept in sync by `tests/test_provision_apply.py` and `tests/regressions/test_uc_intake.py`.
