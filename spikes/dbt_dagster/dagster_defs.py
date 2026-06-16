"""Dagster definitions wrapping the dbt project as software-defined assets.

This is the orchestration shape the spike evaluates. ``@dbt_assets`` turns every
dbt model into a Dagster asset by reading the dbt manifest, so the dbt lineage
graph IS the Dagster asset graph -- scheduling, retries, partitions, backfills,
and run history all come for free. That is exactly the gap the bespoke CLI
pipeline has today: a static, deterministic chain with no runtime, no retries,
and no scheduler.

NOT installed in this repo -- this file documents the shape. To run it:
    pip install dagster dagster-dbt dbt-duckdb
    cd spikes/dbt_dagster && dbt parse          # produces target/manifest.json
    dagster dev -f dagster_defs.py
"""
from __future__ import annotations

from pathlib import Path

try:
    from dagster import AssetExecutionContext, Definitions
    from dagster_dbt import DbtCliResource, dbt_assets
except ImportError as exc:  # pragma: no cover - spike shape; deps optional
    raise SystemExit(
        "Dagster/dbt not installed. This file documents the orchestration "
        "shape; install `dagster dagster-dbt dbt-duckdb` to run it."
    ) from exc

PROJECT_DIR = Path(__file__).parent
MANIFEST = PROJECT_DIR / "target" / "manifest.json"


@dbt_assets(manifest=MANIFEST)
def hospital_a_dbt_assets(context: AssetExecutionContext, dbt: DbtCliResource):
    # `dbt build` runs models AND their tests in dependency order. In production
    # this is `state:modified+` for Slim CI, and incremental marts mean only new
    # partitions recompute -- closing the full-recompute gap.
    yield from dbt.cli(["build"], context=context).stream()


defs = Definitions(
    assets=[hospital_a_dbt_assets],
    resources={"dbt": DbtCliResource(project_dir=str(PROJECT_DIR))},
)
