"""
core/databricks_client.py — Databricks WorkspaceClient builder and health check.

Reads credentials from DatabricksConfig (sourced from lock.toml + env vars).
All SDK imports are lazy so the module loads fine when Databricks is disabled.
"""
from __future__ import annotations

import json
import shlex
from typing import TYPE_CHECKING, Tuple

if TYPE_CHECKING:
    from core.config import DatabricksConfig


class DatabricksClient:
    """Thin wrapper around databricks-sdk WorkspaceClient."""

    def __init__(self, cfg: "DatabricksConfig"):
        self.cfg = cfg
        self._client = None

    # ── Public ────────────────────────────────────────────────────────────────

    def is_configured(self) -> bool:
        return self.cfg.is_active()

    def get_client(self):
        """Return (and lazily initialise) the WorkspaceClient."""
        if self._client is None:
            from databricks.sdk import WorkspaceClient
            self._client = WorkspaceClient(
                host=self.cfg.host,
                token=self.cfg.token,
            )
        return self._client

    def health_check(self) -> Tuple[bool, str]:
        """
        Verify the connection is valid.
        Returns (True, "OK") or (False, error_message).
        """
        if not self.is_configured():
            return False, "Databricks not configured (DATABRICKS_HOST / DATABRICKS_TOKEN missing)"
        try:
            me = self.get_client().current_user.me()
            return True, f"Connected as {me.user_name}"
        except Exception as exc:
            msg = str(exc)
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
            capabilities["current_user_error"] = str(exc)
        try:
            catalogs = list(client.catalogs.list())
            capabilities["unity_catalog"] = "available"
            capabilities["catalogs"] = [getattr(item, "name", "") for item in catalogs if getattr(item, "name", "")]
        except Exception as exc:
            capabilities["unity_catalog"] = "unavailable"
            capabilities["catalog_error"] = str(exc)
        try:
            list(client.jobs.list(limit=1))
            capabilities["jobs"] = "available"
        except Exception as exc:
            capabilities["jobs"] = "unavailable"
            capabilities["jobs_error"] = str(exc)
        try:
            if hasattr(client, "warehouses"):
                list(client.warehouses.list())
                capabilities["sql_warehouses"] = "available"
        except Exception as exc:
            capabilities["sql_warehouses"] = "unavailable"
            capabilities["sql_warehouses_error"] = str(exc)
        try:
            import mlflow
            mlflow.set_tracking_uri("databricks")
            capabilities["mlflow"] = "available"
        except Exception as exc:
            capabilities["mlflow"] = "unavailable"
            capabilities["mlflow_error"] = str(exc)
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
                result_state = str(state.result_state) if state.result_state else "FAILED"
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
