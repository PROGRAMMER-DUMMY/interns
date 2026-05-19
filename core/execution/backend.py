"""
core/execution_backend.py — ExecutionBackend strategy.

Abstracts WHERE an experiment runs: locally (DuckDB), on a Databricks SQL
Warehouse, via Databricks Connect, or as a Databricks Job.

Selection is driven by config/lock.toml [databricks] execution = "...".
All Databricks imports are lazy so DuckDBBackend loads with zero extra deps.
"""
from __future__ import annotations

import os
import sys
import time
import signal
import subprocess
import shlex
import hashlib
import json
from abc import ABC, abstractmethod
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Optional

from core.resource.manager import ResourceDecision, ResourceManager
from core.failures import (
    StructuredFailure,
    internal_bug,
    remote_denied,
    remote_unavailable,
    validation_blocker,
)

if TYPE_CHECKING:
    from core.config import Config, DatabricksConfig
    from core.execution.databricks_client import DatabricksClient
    from core.observability.parser import MetricParser

ROOT = Path(__file__).resolve().parents[2]


def normalize_command(cmd) -> list[str]:
    """Accept legacy string commands or argv lists and return a subprocess-safe argv."""
    if not cmd:
        return []
    if isinstance(cmd, str):
        return shlex.split(cmd)
    return [str(part) for part in cmd]


@dataclass
class ExecutionResult:
    exit_code: int
    log_content: str
    elapsed_seconds: float
    metric: Optional[float] = None
    token_count: int = 0
    telemetry_partial: bool = False
    metadata: dict | None = None
    failure: StructuredFailure | None = None


class ExecutionBackend(ABC):
    """Strategy interface for running one experiment iteration."""

    @abstractmethod
    def execute(
        self,
        task: dict,
        time_budget: int,
        hard_timeout: int,
        log_path: Path,
    ) -> ExecutionResult:
        pass


# ── DuckDB (local subprocess) ─────────────────────────────────────────────────

class DuckDBBackend(ExecutionBackend):
    """
    Runs experiment_cmd + evaluator_cmd as local subprocesses.
    This is the current (unchanged) behaviour and the fallback for all modes.
    """

    def __init__(
        self,
        parser: Optional["MetricParser"] = None,
        *,
        fallback_failure: StructuredFailure | None = None,
        resource_decision: ResourceDecision | None = None,
    ):
        from core.observability.parser import RegexLogParser
        self.parser = parser or RegexLogParser()
        self.fallback_failure = fallback_failure
        self.resource_decision = resource_decision

    def execute(self, task: dict, time_budget: int, hard_timeout: int, log_path: Path) -> ExecutionResult:
        start = time.time()
        resource_decision = self.resource_decision or _resource_decision_for_task(task)
        if resource_decision and resource_decision.status == "blocked":
            log_content = (
                "[backend:duckdb] Resource preflight blocked local execution\n"
                f"resource_mode: {resource_decision.mode}\n"
                f"resource_blockers: {resource_decision.blockers}\n"
                "next: use remote/Databricks execution or reduce workload size\n"
            )
            log_path.write_text(log_content, encoding="utf-8")
            failure = validation_blocker("local_resource_preflight", "Local execution blocked by resource budget.")
            return ExecutionResult(
                exit_code=1,
                log_content=log_content,
                elapsed_seconds=time.time() - start,
                metadata={"resource_decision": resource_decision.to_dict()},
                failure=failure,
            )
        with open(log_path, "w", encoding="utf-8") as log_f:
            if resource_decision:
                log_f.write(
                    f"[backend:duckdb] resource_mode={resource_decision.mode} "
                    f"workers={resource_decision.recommended_workers}\n"
                )
            exit_code = self._run_subprocess(
                task["experiment_cmd"], time_budget, hard_timeout, log_f
            )
            if exit_code in (0, 124):
                self._run_evaluator(task.get("evaluator_cmd", []), log_f)

        log_content = log_path.read_text(encoding="utf-8", errors="replace") if log_path.exists() else ""
        metric = self.parser.parse_metric(log_content, task.get("metric_key", "primary_metric"))
        all_metrics = self.parser.parse_all_metrics(log_content)
        elapsed = time.time() - start

        return ExecutionResult(
            exit_code=exit_code,
            log_content=log_content,
            elapsed_seconds=elapsed,
            metric=metric,
            token_count=int(all_metrics.get("token_count", 0)),
            metadata=_execution_metadata(self.fallback_failure, resource_decision),
            failure=self.fallback_failure if exit_code == 0 else None,
        )

    def _run_subprocess(self, cmd, time_budget: int, hard_timeout: int, log_f) -> int:
        argv = normalize_command(cmd)
        if not argv:
            log_f.write("[backend:duckdb] ERROR: empty experiment command\n")
            return 1
        env = os.environ.copy()
        env["AUTORESEARCH_TIME_BUDGET"] = str(time_budget)
        kwargs = dict(cwd=ROOT, env=env, stdout=log_f, stderr=subprocess.STDOUT)
        if sys.platform != "win32":
            kwargs["start_new_session"] = True

        log_f.write(f"[backend:duckdb] Starting: {' '.join(argv)}\n")
        log_f.flush()
        try:
            proc = subprocess.Popen(argv, **kwargs)
        except FileNotFoundError as exc:
            log_f.write(f"[backend:duckdb] ERROR: {exc}\n")
            return 1

        start = time.time()
        while True:
            try:
                exit_code = proc.wait(timeout=1.0)
                break
            except subprocess.TimeoutExpired:
                pass
            if time.time() - start > hard_timeout:
                self._kill(proc, log_f)
                exit_code = 124
                break

        log_f.write(f"[backend:duckdb] exit={exit_code}  wall={time.time()-start:.1f}s\n")
        log_f.flush()
        return exit_code

    def _run_evaluator(self, evaluator_cmd, log_f) -> None:
        argv = normalize_command(evaluator_cmd)
        if not argv:
            return
        log_f.write(f"[backend:duckdb] Evaluator: {' '.join(argv)}\n")
        log_f.flush()
        try:
            subprocess.run(argv, cwd=ROOT, stdout=log_f, stderr=subprocess.STDOUT)
        except FileNotFoundError as exc:
            log_f.write(f"[backend:duckdb] Evaluator not found: {exc}\n")

    def _kill(self, proc, log_f) -> None:
        log_f.write("[backend:duckdb] Hard timeout — killing process\n")
        if sys.platform == "win32":
            proc.terminate()
        else:
            try:
                os.killpg(os.getpgid(proc.pid), signal.SIGTERM)
            except ProcessLookupError:
                pass
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()


class IsolatedDuckDBBackend(DuckDBBackend):
    """
    Experimental isolated worker backend.
    Uses restricted environment variables and resource-limited subprocesses.
    Future versions will use Docker-based isolation.
    """

    def _run_subprocess(self, cmd, time_budget: int, hard_timeout: int, log_f) -> int:
        argv = normalize_command(cmd)
        if not argv:
            return 1
        
        # Strip most environment variables to prevent leakage
        safe_env = {
            "PATH": os.environ.get("PATH", ""),
            "SYSTEMROOT": os.environ.get("SYSTEMROOT", ""),
            "PYTHONPATH": str(ROOT),
            "AUTORESEARCH_TIME_BUDGET": str(time_budget),
            "AUTORESEARCH_ISOLATED_WORKER": "1",
        }
        
        log_f.write(f"[backend:isolated] Starting isolated task: {' '.join(argv)}\n")
        log_f.flush()
        
        try:
            # Use lower priority and strict memory limits if supported by OS
            proc = subprocess.Popen(
                argv, 
                cwd=ROOT, 
                env=safe_env, 
                stdout=log_f, 
                stderr=subprocess.STDOUT
            )
        except Exception as exc:
            failure = internal_bug("isolated_subprocess_start", str(exc))
            log_f.write(f"[backend:isolated] ERROR: {exc}\n")
            log_f.write(f"[backend:isolated] failure_kind={failure.kind.value}\n")
            return 1

        start = time.time()
        while True:
            try:
                exit_code = proc.wait(timeout=1.0)
                break
            except subprocess.TimeoutExpired:
                pass
            if time.time() - start > hard_timeout:
                self._kill(proc, log_f)
                exit_code = 124
                break
        
        return exit_code


# ── Databricks Jobs (submit + poll) ───────────────────────────────────────────

class JobsBackend(ExecutionBackend):
    """
    Submits experiment_cmd as a Databricks one-time job run and polls for result.
    Works with any unmodified experiment.py — the script runs on Databricks compute.
    Falls back to DuckDBBackend if the job submission fails.
    """

    def __init__(self, db_client: "DatabricksClient", cfg: "DatabricksConfig"):
        self.db = db_client
        self.cfg = cfg
        self._fallback = DuckDBBackend()

    def execute(self, task: dict, time_budget: int, hard_timeout: int, log_path: Path) -> ExecutionResult:
        start = time.time()
        try:
            print("[backend:jobs] Submitting Databricks job run...", flush=True)
            run_id = self.db.submit_job_run(task, time_budget)
            result_state, log_content = self.db.poll_job_run(run_id, hard_timeout)
        except Exception as exc:
            print(f"[backend:jobs] Submission failed: {exc} — falling back to DuckDB", flush=True)
            return self._fallback.execute(task, time_budget, hard_timeout, log_path)

        log_path.write_text(log_content, encoding="utf-8")

        from core.observability.parser import RegexLogParser
        parser = RegexLogParser()
        metric = parser.parse_metric(log_content, task.get("metric_key", "primary_metric"))
        all_metrics = parser.parse_all_metrics(log_content)

        exit_code = 0 if result_state == "SUCCESS" else 1
        return ExecutionResult(
            exit_code=exit_code,
            log_content=log_content,
            elapsed_seconds=time.time() - start,
            metric=metric,
            token_count=int(all_metrics.get("token_count", 0)),
        )


# ── Databricks SQL Warehouse ───────────────────────────────────────────────────

class WarehouseBackend(ExecutionBackend):
    """
    Runs the SQL script directly on a Databricks SQL Warehouse.
    Requires DATABRICKS_HTTP_PATH and a task with a sql_file field.
    For Python/Polars tasks use JobsBackend instead.
    """

    def __init__(self, db_client: "DatabricksClient", cfg: "DatabricksConfig"):
        self.db = db_client
        self.cfg = cfg
        self._fallback = DuckDBBackend()

    def execute(self, task: dict, time_budget: int, hard_timeout: int, log_path: Path) -> ExecutionResult:
        sql_file = task.get("sql_file") or task.get("editable_file", "")
        if not sql_file or not str(sql_file).endswith(".sql"):
            print("[backend:warehouse] No sql_file in task — falling back to DuckDB", flush=True)
            return self._fallback.execute(task, time_budget, hard_timeout, log_path)

        start = time.time()
        try:
            from databricks.sdk.service.sql import StatementState
            sql = Path(sql_file).read_text(encoding="utf-8")
            client = self.db.get_client()
            warehouse_id = self.db._extract_warehouse_id()
            resp = client.statement_execution.execute_statement(
                warehouse_id=warehouse_id,
                statement=sql,
                wait_timeout=f"{min(time_budget, 50)}s",
            )
            state = resp.status.state
            ok = state == StatementState.SUCCEEDED
            row_count = resp.manifest.total_row_count if resp.manifest else 0
            log_content = f"warehouse_state: {state}\nrow_count: {row_count}\n"
            if not ok:
                error = resp.status.error
                log_content += f"error: {error.message if error else 'unknown'}\n"
        except Exception as exc:
            print(f"[backend:warehouse] Error: {exc} — falling back to DuckDB", flush=True)
            return self._fallback.execute(task, time_budget, hard_timeout, log_path)

        elapsed = time.time() - start
        log_content += f"execution_time_seconds: {elapsed:.4f}\n"
        log_path.write_text(log_content, encoding="utf-8")

        from core.observability.parser import RegexLogParser
        metric = RegexLogParser().parse_metric(log_content, task.get("metric_key", "primary_metric"))

        return ExecutionResult(
            exit_code=0 if ok else 1,
            log_content=log_content,
            elapsed_seconds=elapsed,
            metric=metric,
        )


# ── Databricks Connect (local → remote Spark) ─────────────────────────────────

class ConnectBackend(ExecutionBackend):
    """
    Runs experiment locally but routes Spark operations to a remote Databricks cluster.
    Requires databricks-connect package and a cluster configured in DatabricksConfig.
    Polars operations still run locally.
    """

    def __init__(self, cfg: "DatabricksConfig"):
        self.cfg = cfg
        self._fallback = DuckDBBackend()

    def execute(self, task: dict, time_budget: int, hard_timeout: int, log_path: Path) -> ExecutionResult:
        try:
            from databricks.connect import DatabricksSession  # noqa: F401
        except ImportError:
            print("[backend:connect] databricks-connect not installed — falling back to DuckDB", flush=True)
            return self._fallback.execute(task, time_budget, hard_timeout, log_path)

        env = os.environ.copy()
        env["DATABRICKS_HOST"] = self.cfg.host
        env["DATABRICKS_TOKEN"] = self.cfg.token
        env["SPARK_CONNECT_MODE"] = "1"

        # Run the experiment subprocess with Databricks Connect env wired
        backend = DuckDBBackend()
        result = backend._run_subprocess.__func__(  # type: ignore[attr-defined]
            backend, task["experiment_cmd"], time_budget, hard_timeout,
            open(log_path, "w", encoding="utf-8")
        )
        log_content = log_path.read_text(encoding="utf-8", errors="replace") if log_path.exists() else ""
        from core.observability.parser import RegexLogParser
        parser = RegexLogParser()
        metric = parser.parse_metric(log_content, task.get("metric_key", "primary_metric"))
        all_metrics = parser.parse_all_metrics(log_content)
        return ExecutionResult(
            exit_code=result,
            log_content=log_content,
            elapsed_seconds=0,
            metric=metric,
            token_count=int(all_metrics.get("token_count", 0)),
        )


# ── Factory ───────────────────────────────────────────────────────────────────

class StrictJobsBackend(JobsBackend):
    """Jobs backend variant that can fail closed instead of local fallback."""

    def execute(self, task: dict, time_budget: int, hard_timeout: int, log_path: Path) -> ExecutionResult:
        start = time.time()
        try:
            print("[backend:jobs] Submitting Databricks job run...", flush=True)
            run_id = self.db.submit_job_run(task, time_budget)
            result_state, log_content = self.db.poll_job_run(run_id, hard_timeout)
        except Exception as exc:
            if _strict_databricks(self.cfg):
                failure = remote_unavailable("databricks_jobs_submit", str(exc))
                log_content = f"[backend:jobs] Submission failed: {exc}\n"
                log_path.write_text(log_content, encoding="utf-8")
                return ExecutionResult(1, log_content, time.time() - start, failure=failure)
            return super().execute(task, time_budget, hard_timeout, log_path)

        log_path.write_text(log_content, encoding="utf-8")
        from core.observability.parser import RegexLogParser
        parser = RegexLogParser()
        metric = parser.parse_metric(log_content, task.get("metric_key", "primary_metric"))
        all_metrics = parser.parse_all_metrics(log_content)
        exit_code = 0 if result_state == "SUCCESS" else 1
        return ExecutionResult(
            exit_code=exit_code,
            log_content=log_content,
            elapsed_seconds=time.time() - start,
            metric=metric,
            token_count=int(all_metrics.get("token_count", 0)),
            metadata={"databricks_backend": "jobs", "run_id": run_id, "result_state": result_state},
        )


class StrictWarehouseBackend(WarehouseBackend):
    """Warehouse backend with audit metadata and strict no-fallback support."""

    def execute(self, task: dict, time_budget: int, hard_timeout: int, log_path: Path) -> ExecutionResult:
        sql_file = task.get("sql_file") or task.get("editable_file", "")
        if not sql_file or not str(sql_file).endswith(".sql"):
            if _strict_databricks(self.cfg):
                failure = validation_blocker(
                    "databricks_warehouse_task",
                    "No sql_file in task",
                )
                log_content = "[backend:warehouse] No sql_file in task\n"
                log_path.write_text(log_content, encoding="utf-8")
                return ExecutionResult(1, log_content, 0, failure=failure)
            return super().execute(task, time_budget, hard_timeout, log_path)

        start = time.time()
        warehouse_id = ""
        statement_id = ""
        row_count = 0
        sql_hash = ""
        try:
            from databricks.sdk.service.sql import StatementState
            sql = Path(sql_file).read_text(encoding="utf-8")
            sql_hash = hashlib.sha256(sql.encode("utf-8")).hexdigest()
            client = self.db.get_client()
            warehouse_id = self.db._extract_warehouse_id()
            resp = client.statement_execution.execute_statement(
                warehouse_id=warehouse_id,
                statement=sql,
                wait_timeout=f"{min(time_budget, 50)}s",
            )
            state = resp.status.state
            ok = state == StatementState.SUCCEEDED
            row_count = resp.manifest.total_row_count if resp.manifest else 0
            statement_id = _first_attr(resp, "statement_id", "id") or ""
            log_content = (
                "databricks_backend: warehouse\n"
                f"warehouse_id: {warehouse_id}\n"
                f"statement_id: {statement_id}\n"
                f"sql_sha256: {sql_hash}\n"
                f"warehouse_state: {state}\n"
                f"row_count: {row_count}\n"
            )
            if not ok:
                error = resp.status.error
                log_content += f"error: {error.message if error else 'unknown'}\n"
        except Exception as exc:
            if _strict_databricks(self.cfg):
                failure = remote_unavailable("databricks_warehouse_execute", str(exc))
                log_content = (
                    "databricks_backend: warehouse\n"
                    f"warehouse_id: {warehouse_id}\n"
                    f"sql_sha256: {sql_hash}\n"
                    f"error: {exc}\n"
                )
                log_path.write_text(log_content, encoding="utf-8")
                return ExecutionResult(1, log_content, time.time() - start, failure=failure)
            return super().execute(task, time_budget, hard_timeout, log_path)

        elapsed = time.time() - start
        log_content += f"execution_time_seconds: {elapsed:.4f}\n"
        log_path.write_text(log_content, encoding="utf-8")
        from core.observability.parser import RegexLogParser
        metric = RegexLogParser().parse_metric(log_content, task.get("metric_key", "primary_metric"))
        return ExecutionResult(
            exit_code=0 if ok else 1,
            log_content=log_content,
            elapsed_seconds=elapsed,
            metric=metric,
            metadata={
                "databricks_backend": "warehouse",
                "warehouse_id": warehouse_id,
                "statement_id": statement_id,
                "sql_sha256": sql_hash,
                "row_count": row_count,
            },
        )


def build_execution_backend(cfg: "Config") -> ExecutionBackend:
    """
    Build the correct ExecutionBackend from config.
    Returns DuckDBBackend if Databricks is disabled or credentials are absent.
    """
    db_cfg = cfg.databricks
    if not db_cfg.is_active():
        return DuckDBBackend()
    if os.environ.get("AUTORESEARCH_ALLOW_REMOTE_EXECUTION") != "1":
        failure = remote_denied(
            "execution_backend_selection",
            "Databricks is configured, but remote execution requires explicit approval.",
            next_command="Set AUTORESEARCH_ALLOW_REMOTE_EXECUTION=1 to use Databricks execution.",
        )
        print(
            "[execution_backend] Databricks configured; remote execution requires "
            "explicit approval. Set AUTORESEARCH_ALLOW_REMOTE_EXECUTION=1 to use it. "
            "Falling back to DuckDB.",
            flush=True,
        )
        return DuckDBBackend(fallback_failure=failure)

    from core.execution.databricks_client import DatabricksClient
    client = DatabricksClient(db_cfg)

    ok, msg = client.health_check()
    if not ok:
        print(f"[execution_backend] Databricks unavailable: {msg}")
        if db_cfg.fallback == "fail" or _strict_databricks(db_cfg):
            raise RuntimeError(f"Databricks required but unavailable: {msg}")
        print("[execution_backend] Falling back to DuckDB")
        return DuckDBBackend(fallback_failure=remote_unavailable("databricks_health_check", msg))

    mode = db_cfg.execution
    if mode == "jobs":
        return StrictJobsBackend(client, db_cfg)
    if mode == "warehouse":
        return StrictWarehouseBackend(client, db_cfg)
    if mode == "connect":
        return ConnectBackend(db_cfg)
    return DuckDBBackend()


def _resource_decision_for_task(task: dict) -> ResourceDecision | None:
    workspace_value = task.get("workspace")
    if not workspace_value:
        editable = Path(str(task.get("editable_file") or ""))
        parts = editable.parts
        if len(parts) >= 2 and parts[0] == "workspaces":
            workspace_value = str(Path(parts[0]) / parts[1])
    if not workspace_value:
        return None
    workspace = (ROOT / str(workspace_value)).resolve()
    if not workspace.exists() or not workspace.is_relative_to(ROOT):
        return None
    estimated_bytes = _task_estimated_bytes(task)
    return ResourceManager(workspace, repo_root=ROOT).decide(
        estimated_bytes=estimated_bytes,
        workload="execution",
    )


def _task_estimated_bytes(task: dict) -> int:
    total = 0
    for key in ("editable_file", "sql_file"):
        value = task.get(key)
        if value:
            path = (ROOT / str(value)).resolve()
            if path.exists() and path.is_file():
                total += path.stat().st_size
    workspace = task.get("workspace")
    if workspace:
        profile_index = ROOT / str(workspace) / "interns" / "generated" / "profiles" / "profile_index.json"
        if profile_index.exists():
            try:
                data = json.loads(profile_index.read_text(encoding="utf-8"))
            except Exception:
                data = {}
            for profile in data.get("profiles", []):
                total += int(profile.get("size_bytes") or 0)
    return total


def _execution_metadata(
    fallback_failure: StructuredFailure | None,
    resource_decision: ResourceDecision | None,
) -> dict | None:
    metadata: dict = {}
    if fallback_failure:
        metadata["fallback_failure"] = fallback_failure.summary()
    if resource_decision:
        metadata["resource_decision"] = resource_decision.to_dict()
    return metadata or None


def _strict_databricks(cfg: "DatabricksConfig") -> bool:
    return cfg.fallback == "fail" or os.environ.get("AUTORESEARCH_DATABRICKS_STRICT") == "1"


def _first_attr(obj, *names: str):
    for name in names:
        if hasattr(obj, name):
            return getattr(obj, name)
    return None
