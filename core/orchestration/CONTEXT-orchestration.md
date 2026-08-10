# Core Orchestration Architecture Context: `core/orchestration`

This document provides an exhaustive reference for all components in [`core/orchestration`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/orchestration).

---

## Executive Overview & Architectural Model

The `core/orchestration` package manages loop iteration loops, stage execution, dbt compilation, Airflow/Cosmos DAG generation, Dagster asset definitions, dbt backfilling, dbt run-state publication, Airflow REST health checks, and execution governance.

```
┌──────────────────┐        ┌──────────────────┐        ┌──────────────────┐
│     loop.py      ├───────►│ pipeline_stages  ├───────►│   governor.py    │
└────────┬─────────┘        └──────────────────┘        └──────────────────┘
         │
         ├───► ┌──────────────────┐
         │     │  airflow_dag.py  │
         │     └──────────────────┘
         │
         ├───► ┌──────────────────┐
         │     │  cosmos_dag.py   │
         │     └──────────────────┘
         │
         ├───► ┌──────────────────┐
         │     │  dbt_backfill.py │
         │     └──────────────────┘
         │
         └───► ┌──────────────────┐
               │   dbt_state.py   │
               └──────────────────┘
```

---

## File Details

### 1. [`airflow_dag.py`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/orchestration/airflow_dag.py)

- **Exact Purpose**: Generates Apache Airflow DAG Python files for orchestrating workspace dbt and ETL jobs.
- **Key Functions / Classes**:
  - [`generate_airflow_dag(workspace_dir)`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/orchestration/airflow_dag.py#L30-L110): Renders Airflow DAG code using DAG templates.
- **Inputs & Outputs**:
  - *Inputs*: Pipeline plan and schedule settings.
  - *Outputs*: Generated `.py` Airflow DAG file.
- **Failure Modes & Edge Cases**:
  - Handles missing task dependencies by inserting prerequisite check tasks.
  - `build_dag()`'s `dbt_build` stage, when wired (concrete workspace + Cosmos installed), now also chains a `publish_dbt_state` task AFTER the WAP `publish_gold` task (`build_task >> publish_task >> state_task`), via [`cosmos_dag.build_publish_dbt_state_task`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/orchestration/cosmos_dag.py). The DAG's `tails["dbt_build"]` (what downstream stages like the anomaly check hang off) now points at this state task, not `publish_task`.
  - `build_dag()`'s `dbt_backfill` leaf task is built via `cosmos_dag.build_backfill_task(..., pool="backfill")` -- a dedicated Airflow pool so a large replay cannot starve the task slots the nightly scheduled run needs. The pool must exist in the deployment before that matters; the module's own docstring documents the one-time `setup_pools` bootstrap command (`airflow pools set backfill 2 "bounded replay capacity"`) that creates it -- Airflow has no DAG-time API to create a pool, and silently falls back to the unbounded `default_pool` if it's missing rather than erroring, so this is a deploy-checklist item, not something a missing-pool error will surface on its own.

### 1a. [`airflow_health.py`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/orchestration/airflow_health.py)

- **Exact Purpose**: Airflow operability check over REST (`/api/v2/`), never the CLI -- the CLI needs co-location with the deployment, REST does not (`docs/reference/airflow_cli_reference.md` section 7), so this is what a workflow-guard health poll against a remote/managed Airflow (Astro, MWAA, Composer) has to use.
- **Key Functions / Classes**:
  - `check_airflow_health(base_url, token, dag_ids, *, http=urllib.request.urlopen, timeout=10) -> dict`: `GET {base_url}/api/v2/monitor/health` for the scheduler heartbeat status, then `GET {base_url}/api/v2/dags/{dag_id}` per `dag_id` for `is_paused`. Returns `{"ok": bool, "paused_dags": [...], "scheduler": "healthy"|"unhealthy"|"unreachable"}` (plus a redacted `"detail"` on an unreachable scheduler). `http` is injectable so tests never open a real socket. A paused DAG makes `ok: False` even with a healthy scheduler -- a paused pipeline fails silently and forever (`airflow_cli_reference.md` section 8 item 4). A connection failure on the health call makes `scheduler: "unreachable"`, `ok: False`, and never raises (read-only probe). A per-DAG lookup failure after a reachable scheduler folds that dag_id into `paused_dags` rather than silently reading as healthy.
  - `main(argv=None) -> int`: `check-airflow-health --base-url <url> --token <jwt> [--dag-id <id> ...]` console script (`AIRFLOW_API_BASE_URL`/`AIRFLOW_API_TOKEN` env fallbacks); prints the result as JSON, exit 1 when `ok` is false or when base-url/token are both missing.
- **Inputs & Outputs**:
  - *Inputs*: Airflow deployment `base_url`, a bearer JWT, the DAG id(s) to check.
  - *Outputs*: the health/pause-state dict above; no files written.
- **Failure Modes & Edge Cases**:
  - The JWT is carried ONLY in the `Authorization: Bearer` request header -- never present in the returned dict; any exception text surfaced in `"detail"` is passed through `core.observability.log_redaction.redact` first (a connection error can echo the host it failed to reach).
  - Never raises: every network failure resolves to a structured `{"ok": False, ...}` result.

### 2. [`cosmos_dag.py`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/orchestration/cosmos_dag.py)

- **Exact Purpose**: Astronomer Cosmos integration for compiling dbt projects into Airflow DAGs, plus the emitted command builders/BashOperator factories for backfill, WAP publish-gold, dbt-state publication, table maintenance, and ghost-table reconcile that `airflow_dag.build_dag()` wires downstream of the `dbt_build` stage.
- **Key Functions / Classes**:
  - `build_dbt_tasks(*, workspace, repo_root, task_id_prefix="dbt_build")`: two chained tasks -- `generate-dbt-project` (BashOperator) then `dbt build` via Cosmos's `DbtBuildLocalOperator` (DBT_RUNNER, in-process). Raises `SystemExit` without Cosmos/Airflow installed or without the remote-execution approval gate; raises `ValueError` on an empty `workspace`.
  - `publish_gold_command(*, workspace, repo_root)` / `build_publish_gold_task(...)`: the WAP swap (`dbt run-operation publish_gold`) that promotes staged marts to live gold, run only after `dbt build`'s tests pass.
  - `publish_dbt_state_command(*, workspace, repo_root)` / [`build_publish_dbt_state_task(*, workspace, repo_root, task_id="publish_dbt_state")`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/orchestration/cosmos_dag.py): shells to the governed `publish-dbt-state` CLI (`core.orchestration.dbt_state`), publishing `target/manifest.json` + `target/run_results.json` to a UC volume. Must be wired downstream of `build_publish_gold_task`'s task (see `airflow_dag.py`).
  - `backfill_command(*, workspace, repo_root)` / [`build_backfill_task(*, workspace, repo_root, task_id="dbt_backfill", pool="")`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/orchestration/cosmos_dag.py): params-driven bounded replay via `run-dbt-backfill`. When no model declares `event_time` the command REFUSES (guard first, `exit 1`, before the invocation) rather than printing a `DEGRADED` warning and running anyway -- without an event time the span flags bind nothing and the run silently becomes a full refresh of every selected model, which at multi-TB differs from a bounded replay by orders of magnitude in runtime and spend. The refusal names both exits: declare `event_time`, or re-trigger with DAG config `{"allow_full_refresh": "yes"}` so the expensive path is asked for by name and recorded in the run conf. `airflow_dag.build_dag()` must therefore declare `allow_full_refresh` in `params={...}` -- a templated-but-undeclared Airflow param raises at render time, which would break the task instead of gating it. `pool` (empty here -- `airflow_dag.build_dag()` passes `"backfill"`) sets the BashOperator's `pool=` kwarg only when non-empty, so a large replay queues on a dedicated slot count instead of the nightly run's.
  - `maintenance_command(*, workspace, repo_root, layer, weekday)` / `build_maintenance_task(...)`: weekly `OPTIMIZE`+`VACUUM` per generated silver/gold table, hash-offset across a fleet, retention read from the workspace's SLA contract (never `RETAIN 0 HOURS`).
  - `ghost_reconcile_command(*, workspace, repo_root, day_of_month, schema="gold")` / `build_ghost_reconcile_task(...)`: monthly report-only reconcile against `dbt_project_generator.reconcile_ghost_tables`.
  - [`_run_ghost_reconcile(*, workspace, repo_root, schema)`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/orchestration/cosmos_dag.py#L375-L415): the subprocess-side body. Lists the schema's live tables from `information_schema` and hands them to `reconcile_ghost_tables` as **fully-qualified `catalog.schema.table`** names, matching what that function now diffs against (dbt's `relation_name`). Passing the bare `table_name` matches nothing and reports every live table as an orphan. (F20) Both `catalog` and `schema` go through `core.sql_safety.assert_safe_identifier` before being interpolated into the `information_schema` query — neither is user input today, but that is a property of today's callers, not of this function. The catalog comes from `dbt_project_generator.resolve_catalog_and_base`, NOT from `workspace_settings.databricks_source.catalog` — that key holds the declared base, and provisioning creates `<base>_<env>`, so querying the base's `information_schema` hits a nonexistent catalog, returns nothing, and reports every model as an orphan. (F21)
  - `main(argv=None)`: `python -m core.orchestration.cosmos_dag {maintenance|ghost-reconcile}` -- the subprocess entrypoint the emitted `maintenance_command`/`ghost_reconcile_command` bash strings shell out to (executes real DDL/queries against the configured Databricks warehouse, gated behind the same remote-execution approval as `build_dbt_tasks`).
- **Inputs & Outputs**:
  - `_dbt_target_name(repo_root, workspace)`: the profiles.yml target to build against, read from `provision_plan.json`'s `env`. **Never a literal.** `profiles.yml` declares `dev` and `prod` on `<base>_dev`/`<base>_prod`, but `plan-provisioning --env` defaults to `dev`, so hardcoding `prod` pointed `dbt build` at a catalog that was never created — and since sources follow `{{ target.database }}`, the DAG read an absent catalog instead of the landed bronze (F26). Falls back to `prod` when there is no plan (externally-managed catalog), which is the pre-fix behaviour where it was correct.
  - *Inputs*: `dbt_project.yml` (profile name, `vars.catalog`/`vars.gold_schema`), `interns/generated/contracts/provision_plan.json` (`env` -> dbt target), generated `models/{staging,intermediate,marts}/*.sql`, the workspace's `sla_contract.json`.
  - *Deployment requirement*: the Cosmos operator runs with `append_env=True` and the emitted `profiles.yml` uses `env_var()`, so the Airflow scheduler/worker environment must carry `DATABRICKS_HOST`, `DATABRICKS_HTTP_PATH` and `DATABRICKS_TOKEN`. dbt raises on a missing `env_var`, so this fails loudly rather than silently.
  - *Outputs*: Airflow task objects (when Cosmos/Airflow installed) or plain bash command strings (always, regardless of install state -- these are pure string builders tested without Airflow present).
- **Failure Modes & Edge Cases**:
  - `build_dbt_tasks` refuses (SystemExit) without the remote-execution approval gate, checked before the optional-dependency import.
  - `maintenance_command`/`ghost_reconcile_command` degrade to a harmless `echo` when no dbt project has been generated yet for the workspace.
  - `_retention_hours` never returns a value that could render `RETAIN 0 HOURS`, even on a malformed SLA contract.

### 3. [`dag_verify.py`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/orchestration/dag_verify.py)

- **Exact Purpose**: Static syntax and lint checker for generated Airflow/Cosmos DAG files.
- **Key Functions / Classes**:
  - [`verify_dag_structure(dag_path)`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/orchestration/dag_verify.py#L20-L70): Performs AST parsing and import safety checks on DAG files.
- **Inputs & Outputs**:
  - *Inputs*: Path to DAG file.
  - *Outputs*: Verification report indicating syntax or import issues.
- **Failure Modes & Edge Cases**:
  - Rejects DAG files with unparseable syntax or forbidden network imports.

### 4. [`dagster_defs.py`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/orchestration/dagster_defs.py)

- **Exact Purpose**: Dagster asset definition builder for multi-platform data orchestration.
- **Key Functions / Classes**:
  - [`generate_dagster_defs(workspace_dir)`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/orchestration/dagster_defs.py#L20-L75): Renders Dagster software-defined assets.
- **Inputs & Outputs**:
  - *Inputs*: Pipeline plan.
  - *Outputs*: Dagster Definitions dictionary object.
- **Failure Modes & Edge Cases**:
  - Gracefully falls back if Dagster package is not installed.

### 5. [`dbt_backfill.py`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/orchestration/dbt_backfill.py)

- **Exact Purpose**: Manages historical data backfilling runs for dbt models.
- **Key Functions / Classes**:
  - [`run_dbt_backfill(workspace_dir, start_date, end_date)`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/orchestration/dbt_backfill.py#L25-L85): Generates backfill partition execution commands.
- **Inputs & Outputs**:
  - *Inputs*: Date range parameters, workspace directory.
  - *Outputs*: Execution status report per partition interval.
- **Failure Modes & Edge Cases**:
  - Aborts on single partition failure to prevent bad state cascade.

### 6. [`dbt_index.py`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/orchestration/dbt_index.py)

- **Exact Purpose**: Indexes dbt project models, macros, sources, and manifest metadata.
- **Key Functions / Classes**:
  - [`index_dbt_project(dbt_project_dir)`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/orchestration/dbt_index.py#L20-L70): Reads `manifest.json` or parses `.sql` files into structured index.
- **Inputs & Outputs**:
  - *Inputs*: Path to dbt project folder.
  - *Outputs*: dbt model index dictionary.
- **Failure Modes & Edge Cases**:
  - Handles projects without compiled `manifest.json` by scanning raw SQL models.

### 7. [`dbt_state.py`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/orchestration/dbt_state.py)

- **Exact Purpose**: Publishes a dbt run's state (`target/manifest.json` + `target/run_results.json`) to a Unity Catalog volume after a build, unlocking slim CI (`dbt build --state ... --select state:modified+`), `dbt retry`, and `dbt clone` -- none of which have anywhere to read prior state from otherwise. Wired into the Airflow/Cosmos DAG downstream of `cosmos_dag.publish_gold_command`/`build_publish_gold_task` (state should reflect a build actually promoted to live gold, not a WAP-staged one a failed test might still roll back).
- **Key Functions / Classes**:
  - `publish_state(project_dir, workspace, *, runner=subprocess.run) -> dict`: two `databricks fs cp --overwrite` calls per artifact present in `target/` (one to a UTC-timestamped folder, one to `latest/`), under `/Volumes/<catalog>/_state/dbt/<workspace-basename>/`. No `target/` (or an empty one) makes zero `databricks` calls and returns `{"ok": False, "reason": "no target/ artifacts"}`. Stops at the first failed call (structured `{"ok": False, "reason": "databricks fs cp failed", ...}`, stderr tail redacted via `core.observability.log_redaction.redact`). `runner` is injectable so tests never touch the network, same shape as `core.provisioning.sync_code.sync_workspace_code`.
  - `state_download_command(workspace, *, repo_root=PROJECT_ROOT) -> list[str]`: the `databricks fs cp --recursive <remote>/latest target` argv CI (or a human running slim CI locally) uses to pull the last published state down before a `--state`-scoped `dbt build`.
  - `state_remote_root(project_dir, workspace) -> str`: `/Volumes/<catalog>/_state/dbt/<workspace-basename>`, where `catalog` is read from `dbt_project.yml`'s `vars.catalog` (falls back to `main` when absent, e.g. an offline/local-only workspace with no generated dbt project). `vars.catalog` is the CONCRETE provisioned catalog since F21, so this volume path agrees with provisioning's `checkpoint_root`; before that it was the declared base and pointed at a catalog that was never created.
  - `main(argv=None) -> int`: `publish-dbt-state --workspace <ws> [--repo-root <root>]` console script; prints the `publish_state` result as JSON, exit 1 on failure.
- **Inputs & Outputs**:
  - *Inputs*: `<workspace>/dbt/target/{manifest.json,run_results.json}`, `<workspace>/dbt/dbt_project.yml` (for `vars.catalog`).
  - *Outputs*: Files pushed to a UC volume via `databricks fs cp`; the function-level return dict (no evidence-report file is written by this module).
- **Failure Modes & Edge Cases**:
  - Missing `databricks` CLI on PATH is caught and reported (`FileNotFoundError` in the `detail` field), never raised.
  - Never echoes profile/host/token values -- `redact()` scrubs the stderr tail before it reaches the returned dict.

### 8. [`dbt_verify.py`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/orchestration/dbt_verify.py)

- **Exact Purpose**: Validates dbt model compilation, SQL syntax, and column references.
- **Key Functions / Classes**:
  - [`verify_dbt_project(dbt_project_dir)`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/orchestration/dbt_verify.py#L25-L80): Runs `dbt compile` or offline AST validation.
- **Inputs & Outputs**:
  - *Inputs*: Path to dbt project.
  - *Outputs*: Verification status and compilation error logs.
- **Failure Modes & Edge Cases**:
  - Traps dbt syntax errors and reports specific line numbers.

### 9. [`governor.py`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/orchestration/governor.py)

- **Exact Purpose**: Loop orchestration governor enforcing iteration limits, budget caps, and stop conditions.
- **Key Functions / Classes**:
  - [`OrchestrationGovernor`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/orchestration/governor.py#L15-L45): Checks iteration limits before launching subsequent steps.
- **Inputs & Outputs**:
  - *Inputs*: Current run state, step count, resource usage.
  - *Outputs*: Boolean allow/halt decision.
- **Failure Modes & Edge Cases**:
  - Prevents runaway recursive loops by enforcing hard max iteration limits.

### 10. [`loop.py`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/orchestration/loop.py)

- **Exact Purpose**: Primary optimization loop runner orchestrating onboarding, profiling, feature resolution, and execution verification.
- **Key Functions / Classes**:
  - [`run_orchestration_loop(workspace_dir, intent)`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/orchestration/loop.py#L40-L160): Main entry point executing the full lifecycle loop.
- **Inputs & Outputs**:
  - *Inputs*: Workspace directory, user intent.
  - *Outputs*: Run summary status and generated artifacts.
- **Failure Modes & Edge Cases**:
  - Halts execution cleanly when encountering unresolved business definition blockers.

### 11. [`pipeline_stages.py`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/orchestration/pipeline_stages.py)

- **Exact Purpose**: Defines discrete execution stage functions in the orchestration pipeline (e.g. `stage_onboard`, `stage_profile`, `stage_kpi_sql`).
- **Key Functions / Classes**:
  - [`execute_stage(stage_name, context)`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/orchestration/pipeline_stages.py#L20-L75): Runs named stage handler.
- **Inputs & Outputs**:
  - *Inputs*: Stage name string, workspace context.
  - *Outputs*: Stage result dictionary.
- **Failure Modes & Edge Cases**:
  - Returns explicit error context if stage dependencies are unsatisfied.
