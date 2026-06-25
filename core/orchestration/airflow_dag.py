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
"""
from __future__ import annotations

import os
from pathlib import Path
from typing import Any, Optional

from core.orchestration.pipeline_stages import STAGES, command_for

_DEFAULT_WORKSPACE_ENV = "AUTORESEARCH_PIPELINE_WORKSPACE"


def build_dag(
    workspace: Optional[str] = None,
    *,
    dag_id: str = "autoresearch_medallion_pipeline",
    schedule: Optional[str] = None,
) -> Any:
    """Render STAGES into an Airflow DAG (one BashOperator per stage, wired by
    dependencies). Raises a clear error if Airflow is not installed.

    ``schedule`` is an Airflow cron/preset (None = manual trigger only). The
    workspace comes from the arg or ``AUTORESEARCH_PIPELINE_WORKSPACE``."""
    try:
        from airflow import DAG
        from airflow.operators.bash import BashOperator
        from airflow.utils.dates import days_ago
    except ImportError as exc:  # pragma: no cover - optional dep
        raise SystemExit(
            "Apache Airflow is not installed. `pip install apache-airflow` to use "
            "the Airflow surface, or use core.orchestration.dagster_defs (Dagster) "
            "or run_pipeline() (plain sequential) instead."
        ) from exc

    ws = workspace or os.environ.get(_DEFAULT_WORKSPACE_ENV, "")
    repo_root = Path(__file__).resolve().parents[2]

    dag = DAG(
        dag_id=dag_id,
        schedule=schedule,                       # None -> trigger manually
        start_date=days_ago(1),
        catchup=False,
        default_args={"retries": 1},
        tags=["autoresearch", "medallion", "kpi"],
    )
    tasks: dict[str, Any] = {}
    with dag:
        for stage in STAGES:
            tasks[stage.key] = BashOperator(
                task_id=stage.key,
                bash_command=(
                    f"cd {repo_root} && " + command_for(stage, ws or "${WORKSPACE}")
                ),
                doc_md=stage.description,
            )
        # Wire dependencies from the shared topology.
        for stage in STAGES:
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


__all__ = ["build_dag", "dag"]
