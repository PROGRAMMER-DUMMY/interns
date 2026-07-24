"""Cosmos wiring for the `dbt_build` pipeline stage.

`airflow_dag.py` renders every stage as a `BashOperator` shelling out to the
same governed CLI command `pipeline-run`/Dagster already use -- correct, but
for `dbt_build` that means `dbt build` runs as a subprocess with no
structured result, one Airflow log blob for the whole build, and none of
Cosmos's dbt-aware retry/observability integration.

Per the dbt+Airflow integration plan's own prior decision (the Fortune-100
plan, Priority 3): start with a single `dbt build` step using
`InvocationMode.DBT_RUNNER` (dbt-core called in-process, not a subprocess),
not Cosmos's per-model task graph (`DbtTaskGroup`/`DbtDag`) -- that
per-model granularity earns its complexity once there's a second
workspace's dbt project to actually differentiate it from a plain
`BashOperator` running `dbt build`. This module is that single step, done
with Cosmos's `DbtBuildLocalOperator` rather than a hand-rolled subprocess
call.

Cosmos is optional, same pattern as Airflow itself in `airflow_dag.py`:
`cosmos_available()` lets a caller check before committing to it;
`build_dbt_tasks()` raises a clear `SystemExit` if called without it.

Constraint worth stating plainly: unlike the BashOperator path (which can
defer the workspace to a `${WORKSPACE}` shell env var, resolved at task-run
time), a Cosmos operator's `project_dir`/`profile_name` are plain Python
values fixed at DAG-parse time -- it needs a concrete, already-generated
`<workspace>/dbt/` project on disk when the DAG module is imported. Cosmos
wiring therefore only applies when `build_dag()` is called with a concrete
`workspace` (or the `AUTORESEARCH_PIPELINE_WORKSPACE` env var already set at
import time), never the deferred-workspace placeholder mode.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any, Tuple


def cosmos_available() -> bool:
    """Cheap presence check -- lets a caller (airflow_dag.py) decide whether
    to use the Cosmos path or fall back to the plain BashOperator, without
    exception-driven control flow."""
    try:
        import cosmos  # noqa: F401
    except ImportError:
        return False
    return True


def _read_profile_name(dbt_dir: Path) -> str:
    """dbt_project.yml's `profile:` key -- generate-dbt-project derives it
    per-workspace (never a hardcoded name, see `_dbt_project_name()` in
    dbt_project_generator.py), so this must read it rather than assume one.
    """
    import yaml

    project_yml = dbt_dir / "dbt_project.yml"
    if not project_yml.exists():
        raise FileNotFoundError(
            f"{project_yml} not found -- run `generate-dbt-project --workspace ...` "
            "before wiring the Cosmos dbt_build task."
        )
    data = yaml.safe_load(project_yml.read_text(encoding="utf-8")) or {}
    profile = str(data.get("profile") or "").strip()
    if not profile:
        raise ValueError(f"{project_yml} has no `profile:` key")
    return profile


def build_dbt_tasks(
    *,
    workspace: str,
    repo_root: str,
    task_id_prefix: str = "dbt_build",
) -> Tuple[Any, Any]:
    """Two chained Airflow tasks fulfilling the `dbt_build` stage:

    1. ``<prefix>_generate`` -- a plain BashOperator running
       `generate-dbt-project --workspace <ws>` (project generation stays a
       governed CLI step, same as every other stage; Cosmos only owns
       *running* an already-generated project, not producing one).
    2. ``<prefix>`` -- a Cosmos `DbtBuildLocalOperator` (DBT_RUNNER
       invocation mode) running `dbt build` against that project.

    Returns `(generate_task, build_task)`, already wired
    `generate_task >> build_task`. Must be called with an Airflow DAG
    context active on the call stack (i.e. from inside `with dag:`), same
    requirement as any other operator construction.
    """
    try:
        from airflow.operators.bash import BashOperator
        from cosmos.config import ProfileConfig
        from cosmos.constants import InvocationMode
        from cosmos.operators.local import DbtBuildLocalOperator
    except ImportError as exc:  # pragma: no cover - optional dep
        raise SystemExit(
            "astronomer-cosmos is not installed. `pip install astronomer-cosmos` "
            "to use the Cosmos-backed dbt_build task, or use the plain "
            "BashOperator airflow_dag.build_dag() falls back to for the "
            "dbt_build stage when Cosmos is unavailable."
        ) from exc

    if not workspace:
        raise ValueError(
            "Cosmos wiring requires a concrete workspace known at DAG-parse "
            "time -- it cannot use the ${WORKSPACE}-deferred placeholder "
            "the BashOperator path supports. Pass build_dag(workspace=...) "
            "explicitly or set AUTORESEARCH_PIPELINE_WORKSPACE before import."
        )

    dbt_dir = Path(repo_root) / workspace / "dbt"

    generate_task = BashOperator(
        task_id=f"{task_id_prefix}_generate",
        bash_command=f"cd {repo_root} && uv run generate-dbt-project --workspace {workspace}",
        doc_md="Generate the dbt project (staging/intermediate/marts) from confirmed KPI contracts.",
    )

    profile_config = ProfileConfig(
        profile_name=_read_profile_name(dbt_dir),
        target_name="prod",
        profiles_yml_filepath=str(dbt_dir / "profiles.yml"),
    )
    build_task = DbtBuildLocalOperator(
        task_id=task_id_prefix,
        project_dir=str(dbt_dir),
        profile_config=profile_config,
        invocation_mode=InvocationMode.DBT_RUNNER,
        install_deps=True,   # packages.yml declares dbt-expectations (D2)
        append_env=True,
        doc_md="Run `dbt build` (DBT_RUNNER, in-process) against the generated project.",
    )
    generate_task >> build_task
    return generate_task, build_task


__all__ = ["cosmos_available", "build_dbt_tasks"]
