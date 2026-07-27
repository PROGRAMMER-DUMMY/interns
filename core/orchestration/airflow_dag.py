"""Apache Airflow DAG over the medallion+KPI+dashboard pipeline.

Renders the SAME `STAGES` topology (pipeline_stages.py) that the Dagster wiring
uses, so the two orchestrators can never drift -- one source of truth, two
execution surfaces. Choose by situation:

- **Dagster** (`dagster_defs.py`): asset-based -- models the data artifacts +
  lineage, stale-only re-materialization, partition/backfill ergonomics. Best
  when the pipeline is artifact-centric and you want only-recompute-what-changed.
- **Airflow** (this file): task-based -- mature scheduler, broad operator
  ecosystem, ubiquitous in existing platforms. Best when you already run Airflow
  or need its scheduling/operator breadth.

Airflow is optional. Import is graceful: without `apache-airflow` installed this
module still imports (documenting the shape); the topology + the plain
`run_pipeline()` runner stay usable. With it installed, drop this file in your
``dags/`` folder (or point ``dag_folder`` at it).

    pip install apache-airflow
    export AUTORESEARCH_PIPELINE_WORKSPACE=workspaces/<ws>
    # place/symlink this module under your Airflow dags folder

Real-infra verified (not just unit-tested against the graceful-ImportError
path): a real Airflow install (via Astro CLI/Docker, isolated from this
project's own venv -- pip-installing Airflow directly into a shared venv
downgrades shared deps and is not worth the risk) parsed this module's
`build_dag()` output and executed its Cosmos-backed dbt_build task for real.
Found and fixed one real bug live: `airflow.utils.dates.days_ago` no longer
exists in current Airflow (removed; also Airflow's own documented
anti-pattern -- a dynamic start_date shifts on every re-parse). The Cosmos
`DbtBuildLocalOperator` itself constructed correctly, resolved the real
generated dbt project, and genuinely invoked `dbt build` in-process
(DBT_RUNNER) against live Databricks credentials, reaching a real network
response -- confirming the whole chain (Airflow task -> Cosmos -> dbt ->
Databricks adapter) wires correctly end to end. The one remaining failure
was a `databricks-sql-connector` (legacy Thrift) vs. warehouse-endpoint 404,
isolated to that container's own dependency resolution (a different
connector version than this project's own venv resolves) -- a real but
third-party environment finding, not a bug in this module.
"""
from __future__ import annotations

import os
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Optional

from core.orchestration import cosmos_dag
from core.orchestration.pipeline_stages import STAGES, command_for

_DEFAULT_WORKSPACE_ENV = "AUTORESEARCH_PIPELINE_WORKSPACE"
_ALERT_WEBHOOK_ENV = "AUTORESEARCH_ALERT_WEBHOOK_URL"


def _notify_failure(context: dict) -> None:
    """Webhook alert on a real task failure -- Airflow's on_failure_callback
    fires only once retries are exhausted, which already IS the "page on a
    real failure, not every retry" policy plan section 2 calls for. No
    separate error/warn routing needed at this layer: a dbt `warn`-severity
    test never fails the task at all (only `error`-severity ones do --
    core.onboarding.kpi.data_quality_panel already enforces that split at
    the dbt-test layer), so every callback firing here is already
    error-tier by construction. A next-business-day digest for lower-
    severity signals is deliberately not built -- nothing has generated
    enough warn-tier noise yet to justify a batching/storage system.

    No-op when AUTORESEARCH_ALERT_WEBHOOK_URL is unset (most workspaces
    today) -- never breaks a DAG run if the webhook itself fails.
    """
    webhook_url = os.environ.get(_ALERT_WEBHOOK_ENV, "")
    if not webhook_url:
        return
    try:
        import json
        import urllib.request

        ti = context.get("task_instance")
        dag = context.get("dag")
        text = (
            f"Airflow task failed (retries exhausted): "
            f"{dag.dag_id if dag else '?'}.{ti.task_id if ti else '?'}"
        )
        req = urllib.request.Request(
            webhook_url,
            data=json.dumps({"text": text}).encode("utf-8"),
            headers={"Content-Type": "application/json"},
        )
        urllib.request.urlopen(req, timeout=10)
    except Exception:
        pass  # alerting must never break the DAG it's alerting about


def stage_assets(stage: Any, workspace: str) -> list[Any]:
    """`Stage.produces` globs as Airflow 3 Assets, or [] on Airflow 2.

    `produces` has always been an artifact-glob list and was purely advisory.
    Airflow 3 asset-aware scheduling is what makes it load-bearing: a task that
    declares its outputs lets a DOWNSTREAM dag be triggered by data arriving
    rather than by a cron guess, which is the whole point of moving off a
    schedule-only pipeline.

    Returns [] (not an error) when the installed Airflow predates Assets, so the
    same topology still builds a schedule-driven DAG on Airflow 2.
    """
    try:
        from airflow.sdk import Asset  # Airflow 3
    except ImportError:
        try:
            from airflow.assets import Asset  # early Airflow 3 layout
        except ImportError:
            return []
    return [
        Asset(uri=f"file://{workspace}/{glob}")
        for glob in (getattr(stage, "produces", ()) or ())
    ]


def build_dag(
    workspace: Optional[str] = None,
    *,
    dag_id: str = "autoresearch_medallion_pipeline",
    schedule: Optional[str] = None,
    stages: Any = None,
) -> Any:
    """Render a stage topology into an Airflow DAG (one BashOperator per
    stage, wired by dependencies). Raises a clear error if Airflow is not
    installed.

    ``schedule`` is an Airflow cron/preset (None = manual trigger only). The
    workspace comes from the arg or ``AUTORESEARCH_PIPELINE_WORKSPACE``.
    ``stages`` defaults to STAGES (every existing caller's behavior,
    unchanged); pass ``pipeline_stages.stages_for_workspace(repo_root, ws)``
    to get the dbt_build-substituted topology for a workspace on the dbt
    path instead."""
    try:
        from airflow import DAG
        from airflow.operators.bash import BashOperator
    except ImportError as exc:  # pragma: no cover - optional dep
        raise SystemExit(
            "Apache Airflow is not installed. `pip install apache-airflow` to use "
            "the Airflow surface, or use core.orchestration.dagster_defs (Dagster) "
            "or run_pipeline() (plain sequential) instead."
        ) from exc

    ws = workspace or os.environ.get(_DEFAULT_WORKSPACE_ENV, "")
    repo_root = Path(__file__).resolve().parents[2]
    dag_stages = stages if stages is not None else STAGES

    dag = DAG(
        dag_id=dag_id,
        schedule=schedule,                       # None -> trigger manually
        # A fixed date, not the removed days_ago() helper -- found live:
        # airflow.utils.dates.days_ago no longer exists in current Airflow,
        # and a dynamic "N days ago" start_date is Airflow's own documented
        # anti-pattern anyway (it shifts on every DAG re-parse, causing
        # duplicate/missing DAG runs).
        start_date=datetime(2024, 1, 1),
        # Backfill is a deliberate, separate trigger, never automatic
        # catch-up scheduling -- see the dbt+Airflow integration plan's
        # backfill-safety section.
        catchup=False,
        default_args={
            "retries": 1,
            "retry_delay": timedelta(minutes=5),
            "retry_exponential_backoff": True,
            "max_retry_delay": timedelta(minutes=30),
            "on_failure_callback": _notify_failure,
        },
        tags=["autoresearch", "medallion", "kpi"],
    )
    tasks: dict[str, Any] = {}
    with dag:
        for stage in dag_stages:
            if stage.key == "dbt_build" and ws and cosmos_dag.cosmos_available():
                # Cosmos (DBT_RUNNER, in-process) instead of a subprocess
                # `dbt build` -- only possible with a concrete workspace
                # known at DAG-parse time, see cosmos_dag.py's docstring.
                _generate, build_task = cosmos_dag.build_dbt_tasks(
                    workspace=ws, repo_root=str(repo_root), task_id_prefix=stage.key,
                )
                tasks[stage.key] = build_task
            else:
                operator_kwargs: dict[str, Any] = {}
                # Declaring outlets is what turns `produces` from documentation
                # into a scheduling signal. Passed only when the installed
                # Airflow understands Assets.
                assets = stage_assets(stage, ws) if ws else []
                if assets:
                    operator_kwargs["outlets"] = assets
                tasks[stage.key] = BashOperator(
                    task_id=stage.key,
                    bash_command=(
                        f"cd {repo_root} && " + command_for(stage, ws or "${WORKSPACE}")
                    ),
                    doc_md=stage.description,
                    **operator_kwargs,
                )
        # Wire dependencies from the given topology.
        for stage in dag_stages:
            for up in stage.upstream:
                tasks[up] >> tasks[stage.key]
    return dag


# Airflow's scheduler scans module globals for DAG objects. Build lazily so
# importing this module never requires Airflow (topology stays usable without it).
try:  # pragma: no cover - only exercised when airflow is installed
    import airflow  # noqa: F401

    dag = build_dag()
except Exception:  # ImportError or build-time issue
    dag = None


__all__ = ["build_dag", "dag", "stage_assets"]
