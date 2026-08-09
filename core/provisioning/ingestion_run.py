"""Execute the ingestion jobs `generate-ingestion` emitted -- gated, in order.

`generate-ingestion` writes runnable COPY INTO / Auto Loader SQL per discovered
table and deliberately runs nothing. Until this module existed there was no
governed way to run it either, so landing data meant reaching for the vendor CLI
directly -- which routes around ``AUTORESEARCH_ALLOW_REMOTE_EXECUTION``, the one
human-only kill switch. (F17)

The refusal ladder is the same one ``apply-provisioning`` already proves:

1. **No confirmed blueprint -> dry run.** The confirmed blueprint is the single
   recorded human approval of the cloud-first flow; without it this command
   reports what it would run and runs nothing.
2. **Kill-switch -> refuse.** ``AUTORESEARCH_ALLOW_REMOTE_EXECUTION=0`` stops a
   confirmed workspace anyway.
3. **Warehouse unreachable -> structured failure**, pointing at
   ``check-platform-readiness``. Never a traceback.

Execution stops at the first failing job: bronze tables are independent, but a
warehouse that rejected one statement usually rejects the rest, and a wall of
identical secondary errors buries the real one. Re-running is safe -- COPY INTO
skips files it has already ingested, which is why a second run is a legitimate
no-op rather than something to refuse.
"""
from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Protocol

from core.observability.log_redaction import redact
from core.onboarding.databricks.deploy_gates import REMOTE_ENV
from core.onboarding.panel_contract import attach_stage_routing
from core.provisioning.apply import confirmed_blueprint_path
from core.storage.workspace_layout import WorkspaceLayout

RUN_LOG_VERSION = 1
ROUTING_STAGE = "ingestion_generation"

STATUS_EXECUTED = "executed"
STATUS_DRY_RUN = "dry_run"
STATUS_REFUSED_NO_CONFIRMATION = "refused_no_confirmation"
STATUS_REFUSED_KILL_SWITCH = "refused_remote_execution_kill_switch"
STATUS_REFUSED_UNAVAILABLE = "refused_warehouse_unavailable"
STATUS_FAILED = "failed"

READINESS_COMMAND = "uv run check-platform-readiness"


class SqlRunner(Protocol):
    """One statement at a time, so tests substitute a recorder and never reach
    a warehouse."""

    def execute(self, sql: str) -> None: ...


@dataclass
class IngestionRunResult:
    status: str
    ok: bool
    dry_run: bool
    confirmed_blueprint: bool
    run_log_path: str = ""
    manifest_path: str = ""
    catalog: str = ""
    executed: int = 0
    failed: int = 0
    not_attempted: int = 0
    blocked: int = 0
    jobs: list[dict[str, Any]] = field(default_factory=list)
    detail: str = ""
    next_command: str = ""

    def summary(self) -> dict[str, Any]:
        return attach_stage_routing(self.__dict__.copy(), ROUTING_STAGE)


def sql_statements(text: str) -> list[str]:
    """Executable statements from a generated .sql file, comments removed.

    The SQL Statement Execution API takes one statement per call, and the
    generated files carry a CREATE TABLE plus a COPY INTO.

    # ponytail: splits on ';' after dropping '--' lines, which is sound for
    # generated files (their only literals are s3:// URIs). Swap in a real
    # tokenizer if a generator ever emits a semicolon inside a string.
    """
    lines = [
        line for line in (text or "").splitlines()
        if not line.lstrip().startswith("--")
    ]
    return [part.strip() for part in "\n".join(lines).split(";") if part.strip()]


def load_jobs_manifest(layout: WorkspaceLayout) -> dict[str, Any]:
    path = layout.project_root / "ingestion" / "jobs_manifest.json"
    if not path.is_file():
        raise FileNotFoundError(
            f"no ingestion manifest at {path}; run `uv run generate-ingestion` first"
        )
    return json.loads(path.read_text(encoding="utf-8"))


def _sdk_runner(catalog: str) -> SqlRunner:
    """Warehouse-backed runner. Imported lazily so the module stays importable
    (and testable) on a machine with no Databricks SDK configured.

    ``execute_query`` polls past its wait timeout and raises on a non-SUCCEEDED
    terminal state, which is what a COPY INTO on a cold warehouse needs -- the
    first statement pays the start-up and would otherwise come back RUNNING."""
    from core.execution.databricks_client import DatabricksExecutionClient

    client = DatabricksExecutionClient()
    if not client.is_configured():
        raise ConnectionError(
            "no Databricks SQL warehouse configured for this workspace"
        )

    class _WarehouseRunner:
        def execute(self, sql: str) -> None:
            client.execute_query(sql)

    return _WarehouseRunner()


def run_ingestion_jobs(
    repo_root: str | Path,
    workspace: str | Path,
    *,
    dry_run: bool | None = None,
    runner: SqlRunner | None = None,
) -> IngestionRunResult:
    repo_root = Path(repo_root).resolve()
    layout = WorkspaceLayout(project_root=(repo_root / workspace).resolve())

    manifest = load_jobs_manifest(layout)
    jobs = list(manifest.get("jobs") or [])
    catalog = str(manifest.get("catalog") or "")
    manifest_path = _rel(layout.project_root / "ingestion" / "jobs_manifest.json", repo_root)

    confirmed = confirmed_blueprint_path(layout).exists()
    effective_dry_run = (not confirmed) if dry_run is None else bool(dry_run)

    job_records: list[dict[str, Any]] = []

    def _finish(status: str, ok: bool, detail: str = "", next_command: str = "") -> IngestionRunResult:
        result = IngestionRunResult(
            status=status, ok=ok, dry_run=effective_dry_run,
            confirmed_blueprint=confirmed, manifest_path=manifest_path,
            catalog=catalog, jobs=job_records, detail=detail,
            next_command=next_command,
        )
        result.executed = sum(1 for j in job_records if j["status"] == STATUS_EXECUTED)
        result.failed = sum(1 for j in job_records if j["status"] == "failed")
        result.not_attempted = sum(1 for j in job_records if j["status"] == "not_attempted")
        result.blocked = sum(1 for j in job_records if j["status"] == "blocked")
        result.run_log_path = _write_log(layout, repo_root, manifest, result)
        return result

    if not confirmed and not effective_dry_run:
        job_records = [_record(j, "blocked", "no confirmed blueprint") for j in jobs]
        return _finish(
            STATUS_REFUSED_NO_CONFIRMATION, False,
            "the solution blueprint has not been confirmed; ingestion writes rows "
            "into bronze and needs the one recorded human confirmation "
            f"({_rel(confirmed_blueprint_path(layout), repo_root)}).",
            'uv run apply-blueprint-answer --workspace <ws> --confirmed-by "<name>"',
        )

    if effective_dry_run:
        job_records = [_record(j, "would_execute") for j in jobs]
        detail = "" if confirmed else (
            "no confirmed blueprint yet, so this is a dry run. Nothing was ingested."
        )
        return _finish(
            STATUS_REFUSED_NO_CONFIRMATION if not confirmed else STATUS_DRY_RUN,
            confirmed, detail,
            "" if confirmed else
            'uv run apply-blueprint-answer --workspace <ws> --confirmed-by "<name>"',
        )

    if os.environ.get(REMOTE_ENV, "") == "0":
        job_records = [_record(j, "blocked", "remote execution kill-switch set") for j in jobs]
        return _finish(
            STATUS_REFUSED_KILL_SWITCH, False,
            f"{REMOTE_ENV}=0 is set in this shell; remote execution is disabled by "
            "a human override. Nothing was ingested.",
        )

    if runner is None:
        try:
            runner = _sdk_runner(catalog)
        except Exception as exc:
            job_records = [_record(j, "blocked", "warehouse unavailable") for j in jobs]
            return _finish(
                STATUS_REFUSED_UNAVAILABLE, False,
                f"the SQL warehouse is not reachable: {redact(str(exc))}. "
                "Nothing was ingested.",
                READINESS_COMMAND,
            )

    stopped = False
    for job in jobs:
        if stopped:
            job_records.append(_record(job, "not_attempted", "stopped after an earlier failure"))
            continue
        try:
            count = _run_one(runner, layout, job)
        except Exception as exc:
            job_records.append(
                _record(job, "failed", f"{type(exc).__name__}: {redact(str(exc))}")
            )
            stopped = True
            continue
        job_records.append(
            _record(job, STATUS_EXECUTED, f"[ok] ran {count} statement(s)")
        )

    failed = any(record["status"] == "failed" for record in job_records)
    return _finish(STATUS_FAILED if failed else STATUS_EXECUTED, not failed)


def _run_one(runner: SqlRunner, layout: WorkspaceLayout, job: dict[str, Any]) -> int:
    rel = str(job.get("file") or "")
    path = layout.project_root / rel
    if not path.is_file():
        raise FileNotFoundError(f"generated job file missing: {rel}")
    statements = sql_statements(path.read_text(encoding="utf-8"))
    for statement in statements:
        runner.execute(statement)
    return len(statements)


def _record(job: dict[str, Any], status: str, detail: str = "") -> dict[str, Any]:
    return {
        "job_name": job.get("job_name", ""),
        "target_table": job.get("target_table", ""),
        "method": job.get("method", ""),
        "status": status,
        "detail": detail,
        "at": datetime.now(timezone.utc).isoformat(),
    }


def _write_log(
    layout: WorkspaceLayout,
    repo_root: Path,
    manifest: dict[str, Any],
    result: IngestionRunResult,
) -> str:
    log_dir = layout.project_root / "interns" / "generated" / "evidence" / "ingestion"
    log_dir.mkdir(parents=True, exist_ok=True)
    payload = {
        "artifact_type": "evidence/ingestion/run_log.json",
        "version": RUN_LOG_VERSION,
        "generated_by": "run-ingestion",
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "workspace": layout.project_root.name,
        "catalog": result.catalog,
        "connector": manifest.get("connector", ""),
        "additive_only": True,
        "status": result.status,
        "dry_run": result.dry_run,
        "confirmed_blueprint": result.confirmed_blueprint,
        "counts": {
            "executed": result.executed,
            "failed": result.failed,
            "not_attempted": result.not_attempted,
            "blocked": result.blocked,
        },
        "jobs": result.jobs,
    }
    log_file = log_dir / "run_log.json"
    log_file.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    return _rel(log_file, repo_root)


def _rel(path: Path, root: Path) -> str:
    try:
        return str(path.resolve().relative_to(root)).replace("\\", "/")
    except ValueError:
        return path.as_posix()


__all__ = [
    "IngestionRunResult",
    "SqlRunner",
    "STATUS_DRY_RUN",
    "STATUS_EXECUTED",
    "STATUS_FAILED",
    "STATUS_REFUSED_KILL_SWITCH",
    "STATUS_REFUSED_NO_CONFIRMATION",
    "STATUS_REFUSED_UNAVAILABLE",
    "load_jobs_manifest",
    "run_ingestion_jobs",
    "sql_statements",
]
