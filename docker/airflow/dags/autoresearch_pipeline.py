"""Airflow entry point for the generated medallion pipeline.

`core.orchestration.airflow_dag.build_dag()` is a FACTORY -- it returns a DAG
object rather than writing a file -- so Airflow needs this stub to import it.
Keeping the stub thin is deliberate: the topology lives in `pipeline_stages`
where the rest of the platform can reason about it, and this file only decides
which workspace and schedule to bind.

AUTORESEARCH_PIPELINE_WORKSPACE selects the workspace; AUTORESEARCH_PIPELINE_
SCHEDULE sets the cron (unset = manual trigger only). Backfill is deliberately
NOT catchup -- `catchup=False` is set in build_dag() and a backfill is a
separate, human-confirmed `run-dbt-backfill` invocation.
"""
from __future__ import annotations

import os

from core.orchestration.airflow_dag import build_dag

workspace = os.environ.get("AUTORESEARCH_PIPELINE_WORKSPACE", "")
schedule = os.environ.get("AUTORESEARCH_PIPELINE_SCHEDULE") or None

if workspace:
    dag = build_dag(workspace=workspace, schedule=schedule)
