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

import hashlib
import json
import os
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Optional

from core.orchestration import cosmos_dag
from core.orchestration.pipeline_stages import STAGES, command_for

_DEFAULT_WORKSPACE_ENV = "AUTORESEARCH_PIPELINE_WORKSPACE"
_ALERT_WEBHOOK_ENV = "AUTORESEARCH_ALERT_WEBHOOK_URL"
_UNASSIGNED_OWNER = "unassigned"

# Pass as `schedule=` to get a hash-spread daily cron instead of a literal one.
HASHED_SCHEDULE = "hashed"
# The nightly window hashed schedules land in (01:00-05:59 local scheduler
# time). Wide enough to spread a fleet, still inside the usual overnight batch
# window a business expects its numbers to be refreshed in.
_HASH_WINDOW_FIRST_HOUR = 1
_HASH_WINDOW_HOURS = 5


def hashed_schedule(workspace_id: str) -> str:
    """A daily cron whose minute AND hour are derived from a stable hash of the
    workspace id, rather than a fixed default time.

    Every generated DAG defaulting to the same `0 2 * * *` is a self-inflicted
    thundering herd -- a hundred workspaces then hit the warehouse on the same
    minute and queue behind each other (Shopify's Airflow-at-scale writeup names
    hashing the DAG identity into the offset as the fix). Deterministic:
    the same workspace always gets the same slot, so a redeploy never silently
    moves a pipeline's schedule.

    md5, deliberately, not the builtin `hash()`: PYTHONHASHSEED randomizes
    `hash()` per process, which would hand the same DAG a different schedule on
    every scheduler restart.
    """
    digest = int(hashlib.md5(workspace_id.encode("utf-8")).hexdigest()[:8], 16)
    minute = digest % 60
    hour = _HASH_WINDOW_FIRST_HOUR + (digest // 60) % _HASH_WINDOW_HOURS
    return f"{minute} {hour} * * *"


def _fleet_offset(workspace_id: str, modulus: int) -> int:
    """A deterministic offset in [0, modulus) for a workspace, reusing
    `hashed_schedule`'s own minute/hour digest rather than re-hashing --
    one hash, three uses (daily cron, weekly maintenance day, monthly
    reconcile day), so a fleet's maintenance/reconcile schedules spread out
    exactly like its daily pipeline schedule already does (no re-stampede on
    a shared Monday/1st-of-month)."""
    minute, hour = (int(x) for x in hashed_schedule(workspace_id or "").split()[:2])
    return (minute * 60 + hour) % modulus


def _read_owner(repo_root: Path, workspace: str) -> str:
    """Intake's `owner_and_operations` free-text answer (on-call/owner name),
    defensively read from `intake_answers.json` -- absent file, absent key,
    or blank text all default to `"unassigned"` rather than raising: this
    feeds the DAG's `owner` and the failure alert payload, and a missing
    answer must still produce a DAG, just an honestly unowned one (see
    `_alerting_banner`)."""
    if not workspace:
        return _UNASSIGNED_OWNER
    path = repo_root / workspace / "interns" / "generated" / "intake" / "intake_answers.json"
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return _UNASSIGNED_OWNER
    if not isinstance(data, dict):
        return _UNASSIGNED_OWNER
    answer = str((data.get("owner_and_operations") or {}).get("answer") or "").strip()
    return answer or _UNASSIGNED_OWNER


def _alerting_banner(*, owner: str, webhook_configured: bool) -> str:
    """A loud, hard-to-miss DAG header when a real failure would page nobody:
    no webhook AND no named on-call owner (audit A8 -- "default deployment
    pages nobody"). Empty string (no banner) the moment either is true."""
    if webhook_configured or owner != _UNASSIGNED_OWNER:
        return ""
    return "[x] ALERTING NOT CONFIGURED - failures page nobody"


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
        import urllib.request

        ti = context.get("task_instance")
        dag = context.get("dag")
        task_id = ti.task_id if ti else "?"
        text = (
            f"Airflow task failed (retries exhausted): "
            f"{dag.dag_id if dag else '?'}.{task_id}"
        )
        # default_args is a real dict on an actual DAG; guard the type so a
        # test double (or a DAG built without default_args) can't blow this up.
        default_args = getattr(dag, "default_args", None)
        owner = default_args.get("owner") if isinstance(default_args, dict) else None
        payload = {
            "text": text,
            "owner": str(owner or _UNASSIGNED_OWNER),
            "severity": "error",
            "workspace": os.environ.get(_DEFAULT_WORKSPACE_ENV, ""),
            "task": task_id,
        }
        req = urllib.request.Request(
            webhook_url,
            data=json.dumps(payload).encode("utf-8"),
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

    ``schedule`` is an Airflow cron/preset (None = manual trigger only), or
    ``HASHED_SCHEDULE`` to get a nightly cron whose minute/hour are derived
    from a hash of the workspace id (see `hashed_schedule`) -- the right
    choice for a generated fleet, and never a fixed default time. The
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

    if schedule == HASHED_SCHEDULE:
        schedule = hashed_schedule(ws or dag_id)

    # On-call wiring (audit A8): intake's owner_and_operations answer becomes
    # the DAG's owner (default_args, so every task shows it) and rides along
    # in every failure alert payload -- a default deployment must not page
    # nobody silently.
    owner = _read_owner(repo_root, ws)

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
            "owner": owner,
            "retries": 1,
            "retry_delay": timedelta(minutes=5),
            "retry_exponential_backoff": True,
            "max_retry_delay": timedelta(minutes=30),
            "on_failure_callback": _notify_failure,
        },
        # The backfill window, as DAG-run params -- what makes backfill an
        # emitted, re-triggerable seam (spec section 8) instead of a hand-run
        # command. Empty by default: a scheduled run backfills nothing; a
        # deliberate "Trigger DAG w/ config" run supplies the window and the
        # dbt_backfill task replays exactly it.
        params={"event_time_start": "", "event_time_end": ""},
        tags=["autoresearch", "medallion", "kpi"],
    )
    banner = _alerting_banner(
        owner=owner, webhook_configured=bool(os.environ.get(_ALERT_WEBHOOK_ENV, ""))
    )
    if banner:
        dag.doc_md = banner + ("\n\n" + dag.doc_md if dag.doc_md else "")
    tasks: dict[str, Any] = {}
    # Where a stage's DOWNSTREAM edge attaches, when the stage expands into
    # more than one task (dbt_build -> build, then the WAP publish swap).
    tails: dict[str, Any] = {}
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
            # WAP: marts build into <gold>__staging; they become live gold
            # only in this task, and only once the build's own tests passed.
            # Emitted for BOTH dbt_build paths (Cosmos and the plain
            # BashOperator fallback) -- a project whose marts were built but
            # never published serves nothing. Every downstream stage
            # (dashboard, results) therefore hangs off the publish.
            if stage.key == "dbt_build" and ws:
                publish_task = cosmos_dag.build_publish_gold_task(
                    workspace=ws, repo_root=str(repo_root),
                )
                tasks[stage.key] >> publish_task
                tails[stage.key] = publish_task
        # The params-driven backfill entrypoint: a leaf task, deliberately not
        # on the scheduled chain. A scheduled run leaves the window params
        # empty; a "trigger with config" run replays exactly the given window.
        if ws:
            cosmos_dag.build_backfill_task(workspace=ws, repo_root=str(repo_root))
            # Maintenance + ghost-reconcile (audit A11/A4, missing #6): leaf
            # tasks, same as backfill -- they run every scheduled DAG run but
            # self-gate on this workspace's own hash-derived weekly/monthly
            # day (spread across a fleet instead of stacking on everyone's
            # Monday/1st-of-month; see _fleet_offset).
            maint_weekday = _fleet_offset(ws, 7)
            reconcile_day = _fleet_offset(ws, 28) + 1
            cosmos_dag.build_maintenance_task(
                workspace=ws, repo_root=str(repo_root), layer="silver", weekday=maint_weekday,
            )
            cosmos_dag.build_maintenance_task(
                workspace=ws, repo_root=str(repo_root), layer="gold", weekday=maint_weekday,
            )
            cosmos_dag.build_ghost_reconcile_task(
                workspace=ws, repo_root=str(repo_root), day_of_month=reconcile_day,
            )
            # Post-results KPI headline anomaly check (audit A8 missing half:
            # threshold alerting) -- hangs off whichever stage actually
            # produced KPI results: kpi_results on the plain medallion path,
            # the published dbt_build on the dbt path.
            results_key = "dbt_build" if "dbt_build" in tasks else "kpi_results"
            if results_key in tasks:
                anomaly_task = BashOperator(
                    task_id="kpi_anomaly_check",
                    bash_command=(
                        f"cd {repo_root} && uv run python -m "
                        f"core.observability.kpi_anomaly_check --workspace {ws}"
                    ),
                    doc_md=(
                        "Post-results KPI headline anomaly check (median absolute "
                        "deviation vs trailing runs)."
                    ),
                )
                tails.get(results_key, tasks[results_key]) >> anomaly_task
        # Wire dependencies from the given topology.
        for stage in dag_stages:
            for up in stage.upstream:
                tails.get(up, tasks[up]) >> tasks[stage.key]
    return dag


# Airflow's scheduler scans module globals for DAG objects. Build lazily so
# importing this module never requires Airflow (topology stays usable without it).
try:  # pragma: no cover - only exercised when airflow is installed
    import airflow  # noqa: F401

    dag = build_dag()
except Exception:  # ImportError or build-time issue
    dag = None


__all__ = [
    "build_dag", "dag", "stage_assets", "hashed_schedule", "HASHED_SCHEDULE",
]
