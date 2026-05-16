"""
core/medallion/databricks_target.py — Downsize-then-fallback policy for Databricks execution.

On a compute failure, tries a smaller compute config before falling back to
DuckDB. Records the full attempt history in run state for audit / debugging.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from core.execution.backend import ExecutionResult
    from core.config import Config


@dataclass
class CompromiseHistory:
    attempts: list[dict[str, Any]] = field(default_factory=list)
    degraded: bool = False
    fallback_reason: str = ""


def execute_with_downsize_then_fallback(
    task: dict,
    time_budget: int,
    hard_timeout: int,
    log_path: Path,
    cfg: "Config",
) -> tuple["ExecutionResult", CompromiseHistory]:
    """
    Execute a task with automatic downsize + DuckDB fallback on failure.

    Returns (result, history). history.degraded == True means the build fell
    back to DuckDB — the caller should set run_state.degraded_run = True.
    """
    from core.execution.backend import build_execution_backend, DuckDBBackend, _strict_databricks

    history = CompromiseHistory()
    backend = build_execution_backend(cfg)
    target_name = cfg.databricks.execution if cfg and cfg.databricks else "duckdb"

    primary = backend.execute(task, time_budget, hard_timeout, log_path)
    history.attempts.append({"target": target_name, "exit_code": primary.exit_code})

    if primary.exit_code == 0:
        return primary, history

    if _strict_databricks(cfg.databricks if cfg else None):
        history.fallback_reason = "strict mode — no retry"
        return primary, history

    # Downsize retry
    downsized_task = {**task, "compute_hint": "small"}
    downsized = backend.execute(downsized_task, time_budget, hard_timeout * 2, log_path)
    history.attempts.append({
        "target": target_name, "compute": "small", "exit_code": downsized.exit_code
    })

    if downsized.exit_code == 0:
        return downsized, history

    # DuckDB fallback
    fallback = DuckDBBackend().execute(task, time_budget, hard_timeout, log_path)
    history.attempts.append({"target": "duckdb", "exit_code": fallback.exit_code})
    history.degraded = True
    exit_codes = [a["exit_code"] for a in history.attempts[:-1]]
    history.fallback_reason = f"Databricks failed twice (exit codes {exit_codes})"
    return fallback, history
