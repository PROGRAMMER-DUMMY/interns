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

import json
import shlex
from pathlib import Path
from typing import Any, Tuple

from core.onboarding.databricks.deploy_gates import check_remote_approval
from core.sql_safety import assert_safe_identifier


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
    # Checked first, before the optional-dependency import below: the
    # cheapest precondition first, and it makes this gate verifiable by tests
    # regardless of whether astronomer-cosmos happens to be installed. Same
    # G5 gate the medallion deploy path and dbt_backfill.py use. DAG modules
    # are parsed repeatedly by a persistent Airflow scheduler process, so
    # this is necessarily a coarser authorization boundary than a one-shot
    # CLI's "set it in your own shell": the scheduler's OWN process
    # environment must carry AUTORESEARCH_ALLOW_REMOTE_EXECUTION=1 for this
    # DAG to be schedulable against `target=prod` at all. That is a deliberate
    # deployment-time decision an operator makes once, not a per-run check --
    # still never settable by this codebase itself (verify-only, same as G5).
    remote_gate = check_remote_approval()
    if not remote_gate.ok:
        raise SystemExit(
            f"Cosmos dbt_build wiring refused: {remote_gate.blocking_reason} "
            "Set AUTORESEARCH_ALLOW_REMOTE_EXECUTION=1 in the Airflow scheduler's "
            "own environment to authorize this DAG to run dbt build against prod."
        )

    if not workspace:
        raise ValueError(
            "Cosmos wiring requires a concrete workspace known at DAG-parse "
            "time -- it cannot use the ${WORKSPACE}-deferred placeholder "
            "the BashOperator path supports. Pass build_dag(workspace=...) "
            "explicitly or set AUTORESEARCH_PIPELINE_WORKSPACE before import."
        )

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


def backfill_command(*, workspace: str, repo_root: str) -> str:
    """The emitted backfill command: a params-driven, bounded replay.

    Backfill is a first-class EMITTED seam, not something an operator
    hand-runs on a laptop -- the window comes from the DAG run's own params
    (`event_time_start` / `event_time_end`), which is what makes it
    re-triggerable, auditable and identical every time. It shells out to the
    governed `run-dbt-backfill` CLI (span bound + human-gate + remote-approval
    gate + report artifact) rather than calling `dbt` directly, so the DAG
    path and the CLI path cannot diverge in safety.

    Honesty about degradation (design spec section 8): a bounded replay needs
    a declared `event_time`. Without one, dbt's `--event-time-start/-end` match
    no batch and the invocation is a full rebuild of the selected models -- the
    command SAYS so in the task log instead of implying a partition-aligned
    backfill it cannot deliver.
    """
    dbt_dir = Path(repo_root) / workspace / "dbt"
    command = (
        f"cd {repo_root} && uv run run-dbt-backfill --workspace {workspace} "
        "--event-time-start {{ params.event_time_start }} "
        "--event-time-end {{ params.event_time_end }}"
    )
    if dbt_dir.exists() and not _declares_event_time(dbt_dir):
        return (
            "echo 'DEGRADED: no model in this dbt project declares event_time, so this "
            "backfill is a FULL REFRESH of the selected models, not a bounded "
            "partition-aligned replay. Declare event_time on the incremental models to "
            "get a bounded backfill.' && " + command
        )
    return command


def publish_gold_command(*, workspace: str, repo_root: str) -> str:
    """The WAP publish step, run only AFTER `dbt build` (models + tests) passes.

    Marts materialize into `<gold>__staging`; this operation is what promotes
    them to the live gold schema the dashboard reads. A failed test fails the
    build task, this task never runs, and the previous live tables keep
    serving -- which is the whole point of write-audit-publish on Delta, where
    there is no Iceberg-style branch to fast-forward.
    """
    dbt_dir = Path(repo_root) / workspace / "dbt"
    return (
        f"cd {repo_root} && dbt run-operation publish_gold "
        f"--project-dir {dbt_dir} --profiles-dir {dbt_dir}"
    )


def publish_dbt_state_command(*, workspace: str, repo_root: str) -> str:
    """The BashOperator command for `publish_dbt_state`: ships this run's
    `target/manifest.json` + `target/run_results.json` to a UC volume via
    the governed `publish-dbt-state` CLI (`core.orchestration.dbt_state`) --
    never an inline `databricks fs cp` here, same "one command, two callers
    can't diverge" reasoning as `publish_gold_command`.
    """
    return f"cd {repo_root} && uv run publish-dbt-state --workspace {workspace}"


def _declares_event_time(dbt_dir: Path) -> bool:
    # Imported lazily: this module is imported at Airflow DAG-parse time, and
    # the generator pulls in the whole KPI SQL-generation stack.
    from core.onboarding.kpi.dbt_project_generator import project_declares_event_time

    return project_declares_event_time(dbt_dir)


def build_backfill_task(
    *, workspace: str, repo_root: str, task_id: str = "dbt_backfill", pool: str = "",
) -> Any:
    """`backfill_command()` as a BashOperator. Not wired into the scheduled
    path: it runs when a DAG run supplies the window params (see
    `airflow_dag.build_dag`'s `params`).

    `pool` (empty by default here; `airflow_dag.build_dag()` passes
    `"backfill"`) is what stops a large replay from starving the task slots
    the nightly scheduled run needs -- the pool itself must already exist in
    the deployment (`setup_pools` bootstrap, see `airflow_dag.py`'s module
    docstring); Airflow queues the task rather than erroring if it doesn't."""
    from airflow.operators.bash import BashOperator

    kwargs: dict[str, Any] = {"pool": pool} if pool else {}
    return BashOperator(
        task_id=task_id,
        bash_command=backfill_command(workspace=workspace, repo_root=repo_root),
        doc_md=(
            "Bounded dbt backfill over the run's `event_time_start`/`event_time_end` "
            "params, via the governed run-dbt-backfill CLI (span bound + human gate)."
        ),
        **kwargs,
    )


def build_publish_gold_task(*, workspace: str, repo_root: str, task_id: str = "publish_gold") -> Any:
    """`publish_gold_command()` as a BashOperator -- the final WAP swap task."""
    from airflow.operators.bash import BashOperator

    return BashOperator(
        task_id=task_id,
        bash_command=publish_gold_command(workspace=workspace, repo_root=repo_root),
        doc_md=(
            "Write-Audit-Publish swap: promote the built+tested marts from the staging "
            "gold schema to live gold. Never runs when the build/tests fail."
        ),
    )


def build_publish_dbt_state_task(
    *, workspace: str, repo_root: str, task_id: str = "publish_dbt_state",
) -> Any:
    """`publish_dbt_state_command()` as a BashOperator. Must be wired
    downstream of `build_publish_gold_task`'s task -- state should reflect a
    build that was actually promoted to live gold, not a WAP-staged one a
    failed test might still roll back."""
    from airflow.operators.bash import BashOperator

    return BashOperator(
        task_id=task_id,
        bash_command=publish_dbt_state_command(workspace=workspace, repo_root=repo_root),
        doc_md=(
            "Publish target/manifest.json + target/run_results.json to a UC volume "
            "(timestamped + latest) -- unlocks slim CI, `dbt retry`, `dbt clone`. "
            "Runs after publish_gold."
        ),
    )


def _project_layer_schema_tables(dbt_dir: Path, layer: str) -> Tuple[str, str, list]:
    """(catalog, schema, table_stems) for one generated layer ("silver" or
    "gold") of a dbt project, read straight from its own dbt_project.yml +
    models folder layout -- offline, DAG-parse-time only, same pattern as
    `_read_profile_name`/`_declares_event_time` above. Empty when the
    project hasn't been generated yet (first-ever run): maintenance for a
    workspace with no dbt project yet is a no-op, not an error.
    """
    import yaml

    project_yml = dbt_dir / "dbt_project.yml"
    if not project_yml.exists():
        return "", "", []
    data = yaml.safe_load(project_yml.read_text(encoding="utf-8")) or {}
    catalog = str((data.get("vars") or {}).get("catalog") or "")
    models_cfg = data.get("models") or {}
    project_cfg = next(iter(models_cfg.values()), {}) if models_cfg else {}
    if layer == "gold":
        # The LIVE/published gold schema (vars.gold_schema), never the WAP
        # staging schema -- maintenance belongs on what the dashboard reads.
        schema = str((data.get("vars") or {}).get("gold_schema") or "gold")
        tables = sorted(p.stem for p in (dbt_dir / "models" / "marts").glob("*.sql"))
    else:
        schema = str((project_cfg.get("staging") or {}).get("+schema") or "silver")
        tables = sorted(
            p.stem
            for folder in ("staging", "intermediate")
            for p in (dbt_dir / "models" / folder).glob("*.sql")
        )
    return catalog, schema, tables


_DEFAULT_RETENTION_DAYS = {"silver": 365, "gold": 730}


def _retention_hours(repo_root: Path, workspace: str, layer: str) -> int:
    """VACUUM retention in hours, from the workspace's declared SLA contract
    (medallion design's `retention_policy.<layer>_days`), defaulting to the
    documented platform default (audit A4: silver 365d / gold 730d) when the
    contract is absent or the field doesn't resolve to a positive number.
    Never returns an hours value that could render `RETAIN 0 HOURS` -- that
    would delete every file version immediately.
    """
    default_days = _DEFAULT_RETENTION_DAYS.get(layer, 365)
    sla_path = (
        Path(repo_root) / workspace / "interns" / "generated" / "medallion" / "sla_contract.json"
    )
    days: Any = default_days
    try:
        data = json.loads(sla_path.read_text(encoding="utf-8"))
        declared = (data.get("retention_policy") or {}).get(f"{layer}_days")
        if isinstance(declared, (int, float)) and declared > 0:
            days = declared
    except (OSError, json.JSONDecodeError, ValueError, TypeError):
        pass
    return max(1, int(days)) * 24


def maintenance_command(*, workspace: str, repo_root: str, layer: str, weekday: int) -> str:
    """Weekly OPTIMIZE + VACUUM for every generated table in one schema
    (silver or gold), gated to this workspace's own hash-derived weekday
    (`airflow_dag._fleet_offset`, built on the same `hashed_schedule` digest
    the daily cron uses) so a daily-scheduled DAG only actually compacts
    once a week, and a fleet's maintenance spreads across the week instead
    of stacking on everyone's Monday. `weekday` is 0=Monday..6=Sunday
    (`date +%u` is 1=Monday..7=Sunday, hence the +1 below). VACUUM retention
    always comes from `_retention_hours`, so `RETAIN 0 HOURS` can never be
    emitted here.
    """
    repo_root_p = Path(repo_root)
    dbt_dir = repo_root_p / workspace / "dbt"
    catalog, schema, tables = _project_layer_schema_tables(dbt_dir, layer)
    if not tables:
        payload = f"echo 'no generated {layer} tables to maintain yet for {workspace}'"
    else:
        retain_hours = _retention_hours(repo_root_p, workspace, layer)
        stmts: list[str] = []
        for table in tables:
            fq = f"`{catalog}`.`{schema}`.`{table}`" if catalog else f"`{schema}`.`{table}`"
            stmts.append(f"OPTIMIZE {fq}")
            stmts.append(f"VACUUM {fq} RETAIN {retain_hours} HOURS")
        stmt_args = " ".join(f"--stmt {shlex.quote(s)}" for s in stmts)
        payload = f"uv run python -m core.orchestration.cosmos_dag maintenance {stmt_args}"
    return (
        f"cd {repo_root} && if [ $(date +%u) -eq {weekday + 1} ]; then {payload}; "
        f"else echo 'not the weekly maintenance day for {workspace} ({layer})'; fi"
    )


def ghost_reconcile_command(
    *, workspace: str, repo_root: str, day_of_month: int, schema: str = "gold",
) -> str:
    """Monthly ghost-table reconcile (audit A11 / missing #6): report-only,
    gated to this workspace's own hash-derived day of month
    (`airflow_dag._fleet_offset`) so a fleet's reconciles don't all fire on
    the 1st. Runs the EXISTING `reconcile_ghost_tables` (dbt_project_
    generator.py, imported, never edited here) against a live listing of the
    schema.
    """
    return (
        f"cd {repo_root} && if [ $(date +%d) -eq {day_of_month:02d} ]; then "
        f"uv run python -m core.orchestration.cosmos_dag ghost-reconcile "
        f"--workspace {workspace} --repo-root {repo_root} --schema {schema}; "
        f"else echo 'not the monthly ghost-reconcile day for {workspace}'; fi"
    )


def _run_maintenance_statements(statements: list, *, enterprise_id: str = "") -> str:
    """Execute OPTIMIZE/VACUUM statements built by `maintenance_command()`
    against the configured Databricks warehouse. Gated behind the same
    remote-execution approval `build_dbt_tasks()` requires (G5) -- this runs
    real DDL against a real warehouse."""
    remote_gate = check_remote_approval()
    if not remote_gate.ok:
        return f"maintenance refused: {remote_gate.blocking_reason}"
    if not statements:
        return "no statements to run"
    from core.config import resolve_databricks_config
    from core.execution.databricks_client import DatabricksClient

    db_cfg = resolve_databricks_config(enterprise_id)
    if not db_cfg.is_active():
        return "Databricks not configured; nothing executed"
    client = DatabricksClient(db_cfg)
    for stmt in statements:
        client.execute_query(stmt)
    return f"executed {len(statements)} statement(s)"


def _run_ghost_reconcile(*, workspace: str, repo_root: str, schema: str) -> str:
    """Live-list the schema's tables (when Databricks is configured) and
    hand them to the existing report-only reconcile in
    dbt_project_generator.py -- imported, never edited. Defensive when
    Databricks isn't reachable: an empty live listing just means every
    project model reports as an orphan-free run, never a crash."""
    remote_gate = check_remote_approval()
    if not remote_gate.ok:
        return f"ghost-reconcile refused: {remote_gate.blocking_reason}"
    from core.onboarding.kpi.dbt_project_generator import (
        reconcile_ghost_tables,
        resolve_catalog_and_base,
    )
    from core.storage.workspace_layout import WorkspaceLayout

    repo_root_p = Path(repo_root)
    layout = WorkspaceLayout(project_root=repo_root_p / workspace)
    dbt_dir = layout.project_root / "dbt"
    declared = layout.load_settings().get("databricks_source")
    declared = declared if isinstance(declared, dict) else {}
    # The CONCRETE catalog, not workspace_settings' declared base: provisioning
    # creates `<base>_<env>`, so querying the base's information_schema hits a
    # catalog that does not exist and every project model reads as an orphan.
    # (F21 -- same conflation F20 fixed one layer up.)
    catalog, _base = resolve_catalog_and_base(layout)
    enterprise_id = str(declared.get("enterprise_id") or "")
    tables: list[str] = []
    if catalog:
        from core.config import resolve_databricks_config
        from core.execution.databricks_client import DatabricksClient

        db_cfg = resolve_databricks_config(enterprise_id)
        if db_cfg.is_active():
            client = DatabricksClient(db_cfg)
            try:
                # Both halves are interpolated into SQL text, so both are
                # validated as identifiers first. Neither is user input today
                # (catalog comes from provision_plan.json, schema from the DAG
                # definition), but "not currently reachable from a user" is a
                # property of today's callers, not of this function.
                safe_catalog = assert_safe_identifier(catalog, context="catalog")
                safe_schema = assert_safe_identifier(schema, context="schema")
                _, rows = client.execute_query(
                    f"SELECT table_name FROM `{safe_catalog}`.information_schema.tables "
                    f"WHERE table_schema = '{safe_schema}'"
                )
                # Fully qualified, because reconcile_ghost_tables diffs against
                # dbt's `relation_name` (catalog.schema.table). Passing the bare
                # table_name matches nothing there and reports every live table
                # as an orphan. The query is already scoped to this catalog and
                # schema, so both are known here. (F20)
                tables = [
                    f"{catalog}.{schema}.{str(r[0])}" for r in rows if r and r[0]
                ]
            except Exception:
                tables = []
    return reconcile_ghost_tables(dbt_dir, tables, layout=layout, schema=schema)


def build_maintenance_task(
    *, workspace: str, repo_root: str, layer: str, weekday: int, task_id: str = "",
) -> Any:
    """`maintenance_command()` as a BashOperator -- a leaf task, same as
    backfill: runs every scheduled DAG run and self-gates on its weekday."""
    from airflow.operators.bash import BashOperator

    return BashOperator(
        task_id=task_id or f"maintenance_{layer}",
        bash_command=maintenance_command(
            workspace=workspace, repo_root=repo_root, layer=layer, weekday=weekday,
        ),
        doc_md=(
            f"Weekly OPTIMIZE + VACUUM for every generated {layer} table "
            "(retention-aligned, offset across the fleet by workspace hash)."
        ),
    )


def build_ghost_reconcile_task(
    *, workspace: str, repo_root: str, day_of_month: int, schema: str = "gold",
    task_id: str = "ghost_reconcile",
) -> Any:
    """`ghost_reconcile_command()` as a BashOperator -- a leaf task, same as
    backfill: runs every scheduled DAG run and self-gates on its day of
    month."""
    from airflow.operators.bash import BashOperator

    return BashOperator(
        task_id=task_id,
        bash_command=ghost_reconcile_command(
            workspace=workspace, repo_root=repo_root, day_of_month=day_of_month, schema=schema,
        ),
        doc_md=(
            "Monthly ghost-table reconcile (report-only, audit A11) -- offset across "
            "the fleet by workspace hash."
        ),
    )


def main(argv: list = None) -> int:
    """CLI surface for the two commands `maintenance_command()`/
    `ghost_reconcile_command()` shell out to. Not a pyproject.toml console
    script -- the orchestrator wires that; invoked in the meantime as
    `python -m core.orchestration.cosmos_dag <subcommand>` from the DAG's
    own bash_command."""
    import argparse

    parser = argparse.ArgumentParser(description="cosmos_dag maintenance CLI")
    sub = parser.add_subparsers(dest="command", required=True)

    maint = sub.add_parser("maintenance")
    maint.add_argument("--stmt", action="append", default=[])
    maint.add_argument("--enterprise-id", default="")

    recon = sub.add_parser("ghost-reconcile")
    recon.add_argument("--workspace", required=True)
    recon.add_argument("--repo-root", default=".")
    recon.add_argument("--schema", default="gold")

    args = parser.parse_args(argv)
    if args.command == "maintenance":
        print(_run_maintenance_statements(args.stmt, enterprise_id=args.enterprise_id))
    else:
        print(_run_ghost_reconcile(
            workspace=args.workspace, repo_root=args.repo_root, schema=args.schema,
        ))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())


__all__ = [
    "cosmos_available",
    "build_dbt_tasks",
    "backfill_command",
    "publish_gold_command",
    "publish_dbt_state_command",
    "build_backfill_task",
    "build_publish_gold_task",
    "build_publish_dbt_state_task",
    "maintenance_command",
    "ghost_reconcile_command",
    "build_maintenance_task",
    "build_ghost_reconcile_task",
    "main",
]
