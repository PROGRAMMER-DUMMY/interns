"""
core/databricks_client.py — Databricks WorkspaceClient builder and health check.

Reads credentials from DatabricksConfig (sourced from lock.toml + env vars).
All SDK imports are lazy so the module loads fine when Databricks is disabled.
"""
from __future__ import annotations

import json
import shlex
from typing import TYPE_CHECKING, Tuple

from core.observability.log_redaction import redact

if TYPE_CHECKING:
    from core.config import DatabricksConfig

# execute_query's bounds. Generous rather than tight: this client is the
# metadata/sample read path (profiling samples, information_schema listings),
# not a bulk export, so the caps exist to make an unbounded read FAIL rather
# than to tune throughput. Any caller that legitimately needs more says so.
_POLL_INTERVAL_SECONDS = 2
_POLL_DEADLINE_SECONDS = 1800.0        # 30 min: a cold warehouse plus a slow query
_POLL_MAX_CONSECUTIVE_ERRORS = 4       # ~5 control-plane failures in a row before giving up
_MAX_RESULT_ROWS = 1_000_000


class DatabricksClient:
    """Thin wrapper around databricks-sdk WorkspaceClient."""

    def __init__(self, cfg: "DatabricksConfig"):
        self.cfg = cfg
        self._client = None

    # ── Public ────────────────────────────────────────────────────────────────

    def is_configured(self) -> bool:
        return self.cfg.is_active()

    def get_client(self):
        """Return (and lazily initialise) the WorkspaceClient.

        Prefers explicit host/token (env-var-sourced -- the right path for CI,
        Airflow workers, and other contexts with no ~/.databrickscfg). Falls
        back to the Databricks CLI's own profile-based auth when those aren't
        set, so a workstation already authenticated via `databricks auth
        login` / `databricks configure` doesn't need the token duplicated
        into .env as well.
        """
        if self._client is None:
            from databricks.sdk import WorkspaceClient
            if self.cfg.host and self.cfg.token:
                self._client = WorkspaceClient(host=self.cfg.host, token=self.cfg.token)
            else:
                self._client = WorkspaceClient(profile=self.cfg.profile or "DEFAULT")
        return self._client

    def health_check(self) -> Tuple[bool, str]:
        """
        Verify the connection is valid.
        Returns (True, "OK") or (False, error_message).
        """
        if not self.is_configured():
            return False, (
                "Databricks not configured: neither DATABRICKS_HOST/DATABRICKS_TOKEN "
                f"nor a working ~/.databrickscfg profile ({self.cfg.profile!r}) were found"
            )
        try:
            me = self.get_client().current_user.me()
            return True, f"Connected as {me.user_name}"
        except Exception as exc:
            msg = redact(str(exc))
            if "401" in msg or "Unauthorized" in msg:
                return False, (
                    "Authentication failed (HTTP 401). "
                    "DATABRICKS_TOKEN may be expired or invalid. "
                    "Azure AAD tokens expire after 1 hour — use a PAT or OAuth M2M service principal."
                )
            return False, f"Connection error: {msg}"

    def create_mlflow_experiment(self, name: str) -> str:
        """
        Create or retrieve an MLflow experiment under the current user's home folder.
        Falls back to a flat name if the workspace path fails.
        Returns experiment_id.
        """
        import mlflow
        mlflow.set_tracking_uri("databricks")

        # Must be a single level under /Users/<email>/ — parent always exists
        try:
            me = self.get_client().current_user.me()
            user_email = me.user_name or "autoresearch"
        except Exception:
            user_email = "autoresearch"

        experiment_path = f"/Users/{user_email}/autoresearch"
        exp = mlflow.set_experiment(experiment_path)
        return exp.experiment_id

    def ensure_delta_schema(self, catalog: str, schema: str) -> None:
        """
        Ensure schema exists under catalog.
        Does NOT try to create the catalog — assumes it exists (e.g. 'main').
        Creating a catalog requires a managed storage location, which free trial
        workspaces don't have configured by default.
        """
        client = self.get_client()
        try:
            client.schemas.get(f"{catalog}.{schema}")
        except Exception:
            client.schemas.create(schema, catalog_name=catalog)

    def discover_capabilities(self) -> dict:
        """Best-effort Databricks capability discovery for setup/readiness reports."""
        capabilities = {
            "configured": self.is_configured(),
            "current_user": None,
            "sql_warehouses": "unknown",
            "unity_catalog": "unknown",
            "catalogs": [],
            "can_create_schema": "unknown",
            "jobs": "unknown",
            "mlflow": "unknown",
            "http_path_configured": bool(self.cfg.http_path),
        }
        if not self.is_configured():
            return capabilities
        client = self.get_client()
        try:
            me = client.current_user.me()
            capabilities["current_user"] = me.user_name
        except Exception as exc:
            capabilities["current_user_error"] = redact(str(exc))
        try:
            catalogs = list(client.catalogs.list())
            capabilities["unity_catalog"] = "available"
            capabilities["catalogs"] = [getattr(item, "name", "") for item in catalogs if getattr(item, "name", "")]
        except Exception as exc:
            capabilities["unity_catalog"] = "unavailable"
            capabilities["catalog_error"] = redact(str(exc))
        try:
            list(client.jobs.list(limit=1))
            capabilities["jobs"] = "available"
        except Exception as exc:
            capabilities["jobs"] = "unavailable"
            capabilities["jobs_error"] = redact(str(exc))
        try:
            if hasattr(client, "warehouses"):
                list(client.warehouses.list())
                capabilities["sql_warehouses"] = "available"
        except Exception as exc:
            capabilities["sql_warehouses"] = "unavailable"
            capabilities["sql_warehouses_error"] = redact(str(exc))
        try:
            import mlflow
            mlflow.set_tracking_uri("databricks")
            capabilities["mlflow"] = "available"
        except Exception as exc:
            capabilities["mlflow"] = "unavailable"
            capabilities["mlflow_error"] = redact(str(exc))
        return capabilities

    def write_delta(self, catalog: str, schema: str, table: str, records: list[dict]) -> None:
        """
        Append records to a Delta table via SQL statement execution.
        Fails silently — caller marks run as telemetry_partial on exception.
        """
        if not records:
            return
        # T4 (P3): the records carry untrusted free text (user names, error
        # messages, KPI text). Pass the JSON as a BOUND PARAMETER instead of
        # f-string interpolation so a single quote / SQL metacharacter cannot
        # break or inject into the statement. Validate the table parts as
        # identifiers and backtick-quote them (they are config-derived, but the
        # gate is cheap and closes the surface). Ref: core-audit execution.md.
        from databricks.sdk.service.sql import StatementParameterListItem

        from core.sql_safety import assert_safe_identifier, quote_ident_backtick

        target = ".".join(
            quote_ident_backtick(assert_safe_identifier(part, context="delta table part"))
            for part in (catalog, schema, table)
        )
        rows_json = json.dumps(records)
        statement = (
            f"INSERT INTO {target}\n"
            "SELECT * FROM (\n"
            "  SELECT explode(from_json(:rows, 'array<map<string,string>>'))\n"
            ")"
        )
        client = self.get_client()
        client.statement_execution.execute_statement(
            warehouse_id=self._extract_warehouse_id(),
            statement=statement,
            parameters=[StatementParameterListItem(name="rows", value=rows_json)],
            wait_timeout="30s",
        )

    def execute_query(
        self,
        sql: str,
        *,
        wait_timeout: str = "50s",
        timeout: float = _POLL_DEADLINE_SECONDS,
        max_rows: int = _MAX_RESULT_ROWS,
    ) -> Tuple[list, list]:
        """
        Execute SQL against the configured SQL warehouse and return
        (column_names, rows) -- rows as lists of string values (the SQL
        Statement Execution API's default JSON_ARRAY result format).

        This is the missing read path this platform didn't have: write_delta()
        above executes SQL but never reads results back, and every other
        execute_statement call site (core/execution/backend.py) only reads
        status/row-count, never resp.result.data_array. Building this properly
        here (not duplicated per caller) so profiling, cost-tag verification,
        and anything else that needs to read the warehouse can reuse it.

        Reads EVERY chunk (P1). The Statement Execution API paginates: the
        first response carries `next_chunk_index` when more data exists, and
        returning only `data_array` silently yields a PREFIX that looks like a
        complete, successful result. `max_rows` bounds memory, but passing it
        RAISES rather than truncating -- a cap that quietly returns a prefix
        would reintroduce the exact bug it guards.

        Polling is bounded (P2). `timeout` caps total wait so a hung statement
        cannot pin a caller (an Airflow worker slot, in practice) forever, and
        a transient `get_statement` failure is retried rather than abandoning a
        statement that is running fine server-side.
        """
        import time as _time
        from databricks.sdk.service.sql import StatementState

        client = self.get_client()
        resp = client.statement_execution.execute_statement(
            warehouse_id=self._extract_warehouse_id(),
            statement=sql,
            wait_timeout=wait_timeout,
        )
        # execute_statement blocks synchronously up to wait_timeout; if the
        # warehouse is still cold-starting or the query is still running past
        # that, it returns PENDING/RUNNING instead of raising -- poll until a
        # terminal state, the deadline, or exhausted retries.
        deadline = _time.monotonic() + max(float(timeout), 0.0)
        consecutive_errors = 0
        while resp.status.state in (StatementState.PENDING, StatementState.RUNNING):
            if _time.monotonic() >= deadline:
                raise TimeoutError(
                    f"Databricks SQL statement {resp.statement_id} is still "
                    f"{resp.status.state} after {timeout}s; giving up the wait "
                    "(the statement may still be running server-side)"
                )
            _time.sleep(_POLL_INTERVAL_SECONDS)
            try:
                resp = client.statement_execution.get_statement(resp.statement_id)
                consecutive_errors = 0
            except Exception as exc:
                # A blip against the control plane says nothing about the
                # statement, which is very likely still running. Retry a
                # bounded number of times before surfacing it.
                consecutive_errors += 1
                if consecutive_errors > _POLL_MAX_CONSECUTIVE_ERRORS:
                    raise RuntimeError(
                        f"Databricks SQL statement {resp.statement_id}: "
                        f"{_POLL_MAX_CONSECUTIVE_ERRORS + 1} consecutive polling "
                        f"failures; last error: {redact(str(exc))}"
                    ) from exc

        if resp.status.state != StatementState.SUCCEEDED:
            error = resp.status.error
            message = redact(error.message) if error and error.message else str(resp.status.state)
            raise RuntimeError(f"Databricks SQL statement failed ({resp.status.state}): {message}")

        columns = [col.name for col in (resp.manifest.schema.columns or [])]
        rows: list = []
        chunk = resp.result if resp.result else None
        while chunk is not None:
            rows.extend(getattr(chunk, "data_array", None) or [])
            if len(rows) > max_rows:
                raise RuntimeError(
                    f"Databricks SQL result exceeded max_rows={max_rows}. Refusing to "
                    "return a truncated prefix -- narrow the query (LIMIT/aggregate/"
                    "filter) or raise max_rows deliberately."
                )
            next_index = getattr(chunk, "next_chunk_index", None)
            chunk = (
                client.statement_execution.get_statement_result_chunk_n(
                    resp.statement_id, next_index
                )
                if next_index is not None
                else None
            )
        return columns, rows

    def submit_job_run(self, task: dict, time_budget: int) -> int:
        """Submit experiment_cmd as a one-time Databricks job run. Returns run_id."""
        from databricks.sdk.service.jobs import RunTask, SparkPythonTask
        cmd = task.get("experiment_cmd", [])
        if isinstance(cmd, str):
            cmd = shlex.split(cmd)
        if not cmd:
            raise ValueError("task has no experiment_cmd")
        script_idx = next((idx for idx, part in enumerate(cmd) if str(part).endswith(".py")), len(cmd) - 1)
        python_file = task.get("databricks_python_file") or cmd[script_idx]
        if not _is_remote_databricks_path(str(python_file)):
            raise ValueError(
                "Databricks Jobs require a remote python file path. "
                "Set task['databricks_python_file'] to a /Workspace, /Volumes, or dbfs: path."
            )
        params = cmd[script_idx + 1:]
        client = self.get_client()

        run = client.jobs.submit(
            run_name=f"autoresearch_{task.get('id', 'run')}",
            tasks=[RunTask(
                task_key="experiment",
                spark_python_task=SparkPythonTask(
                    python_file=python_file,
                    parameters=params,
                ),
                timeout_seconds=time_budget,
            )],
        ).result()
        return run.run_id

    def poll_job_run(self, run_id: int, hard_timeout: int) -> Tuple[str, str]:
        """
        Poll a job run until terminal state or hard_timeout.
        Returns (state: SUCCESS|FAILED|TIMEDOUT, output_log: str).
        """
        import time
        from databricks.sdk.service.jobs import RunLifeCycleState

        client = self.get_client()
        start = time.time()
        while True:
            run = client.jobs.get_run(run_id)
            state = run.state
            if state.life_cycle_state in (
                RunLifeCycleState.TERMINATED,
                RunLifeCycleState.INTERNAL_ERROR,
                RunLifeCycleState.SKIPPED,
            ):
                output = client.jobs.get_run_output(run_id)
                log = output.logs or ""
                # Return the BARE enum name ("SUCCESS"), not str(enum) which
                # renders "RunResultState.SUCCESS" and never == "SUCCESS" at the
                # call sites, recording successful jobs as exit_code=1. Ref:
                # core-audit execution.md (T9-class).
                rs = state.result_state
                if rs is None:
                    result_state = "FAILED"
                else:
                    result_state = getattr(rs, "name", None) or str(rs).split(".")[-1]
                return result_state, log

            if time.time() - start > hard_timeout:
                try:
                    client.jobs.cancel_run(run_id)
                except Exception:
                    pass
                return "TIMEDOUT", ""

            time.sleep(3)

    # ── Private ───────────────────────────────────────────────────────────────

    def _extract_warehouse_id(self) -> str:
        """Extract warehouse ID from DATABRICKS_HTTP_PATH."""
        path = self.cfg.http_path
        parts = path.rstrip("/").split("/")
        return parts[-1] if parts else ""


def _is_remote_databricks_path(path: str) -> bool:
    return path.startswith(("/Workspace/", "/Volumes/", "dbfs:/"))
