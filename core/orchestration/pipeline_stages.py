"""The medallion+KPI+dashboard pipeline as a dependency-ordered stage graph.

This is the pure-Python topology (no Dagster dependency) that
``dagster_defs.py`` renders into a software-defined asset graph. Keeping the
graph here means the DAG shape -- stages, their order, and their governed
commands -- is unit-testable without installing Dagster, and the same definition
drives both the orchestrator and any plain sequential runner.

Each stage wraps an EXISTING governed CLI command (the same ones an operator runs
today). Dependencies encode the real data flow: design -> build -> kpi -> dash.
"""
from __future__ import annotations

from dataclasses import dataclass, field, replace


@dataclass(frozen=True)
class Stage:
    """One pipeline stage = a governed command + its upstream dependencies."""
    key: str
    command: str                          # `{ws}` is replaced with the workspace path
    upstream: tuple[str, ...] = ()
    description: str = ""
    produces: tuple[str, ...] = field(default_factory=tuple)  # artifact globs (advisory)


# The canonical pipeline DAG. Order is encoded by `upstream`, not list position.
STAGES: tuple[Stage, ...] = (
    Stage(
        key="onboard",
        command="uv run onboard-workspace --workspace {ws}",
        description="Profile datasets, build domain model + semantic + relationship contracts.",
        produces=("interns/generated/contracts/*.json",
                  "interns/generated/profiles/*.json"),
    ),
    Stage(
        key="resolve_features",
        command="uv run resolve-kpi-features --workspace {ws}",
        upstream=("onboard",),
        description="Bind KPI metric/cuts tokens to real columns (kpi_feature_mapping).",
        produces=("interns/generated/contracts/kpi_feature_mapping.json",),
    ),
    Stage(
        key="medallion_design",
        command="uv run medallion design --workspace {ws}",
        upstream=("onboard",),
        description="Emit bronze/silver/gold code + contracts + SLA + storage strategy.",
        produces=("interns/generated/medallion/*.json",
                  "interns/generated/medallion/{bronze,silver,gold}/*"),
    ),
    Stage(
        key="medallion_build",
        command="uv run medallion build --workspace {ws}",
        upstream=("medallion_design",),
        description="Execute bronze->silver(MERGE)->gold(OBT); DQ assertions; storage report.",
        produces=("interns/state/medallion/workspace.duckdb",
                  "interns/reports/medallion_storage/storage_report.json"),
    ),
    Stage(
        key="kpi_results",
        command="uv run run-kpi-execution-harness --workspace {ws}",
        upstream=("resolve_features", "medallion_build"),
        description="Generate + execute each KPI result view; record pass/fail evidence.",
        produces=("interns/generated/evidence/kpi_execution_harness.json",),
    ),
    Stage(
        key="kpi_intent_coverage",
        command="uv run validate-kpi-intent-coverage --workspace {ws}",
        upstream=("kpi_results",),
        description=(
            "Check generated SQL realizes each KPI's declared intent (temporal anchor, "
            "grain, cuts). Error-severity findings fail the stage."
        ),
        produces=("interns/reports/kpi_intent_coverage.md",),
    ),
    Stage(
        key="dashboard",
        command="uv run workspace-dashboard --workspace {ws} --screen",
        upstream=("kpi_intent_coverage",),
        description="Build live dashboard from gold; structure-aware screener gate.",
        produces=("interns/reports/dashboard_screener/current.json",),
    ),
)


# NOT a member of STAGES: unlike every stage above, this one is only correct
# for a workspace on the dbt path (WorkspaceLayout.databricks_source_mode()
# in {"additive", "exclusive"}) -- generate-dbt-project requires a declared
# databricks_source, so this stage would simply fail for a local-only
# workspace. stages_for_workspace() below splices it in (replacing
# medallion_build+kpi_results, repointing dashboard's upstream) only for a
# workspace that actually declared one; STAGES itself -- what every existing
# caller (airflow_dag.py, dagster_defs.py, plain pipeline-run) already
# renders unconditionally -- stays exactly as it was, byte-identical, for
# every workspace that doesn't opt in.
#
# `check-remote-execution-gate` (thin CLI over deploy_gates.check_remote_approval,
# the same G5 check the medallion deploy path uses) runs between project
# generation and the actual `dbt build`/`dbt retry` -- this is the real
# production-execution surface shared by plain `pipeline-run`, Dagster, and the
# Airflow BashOperator fallback (Cosmos's own path gates separately, see
# cosmos_dag.py); without it this stage ran `dbt build` against a workspace's
# configured target (often prod) with zero human authorization.
DBT_BUILD_STAGE = Stage(
    key="dbt_build",
    command=(
        "uv run generate-dbt-project --workspace {ws} && "
        "uv run check-remote-execution-gate && "
        "(dbt retry --project-dir {ws}/dbt --profiles-dir {ws}/dbt "
        "|| dbt build --project-dir {ws}/dbt --profiles-dir {ws}/dbt)"
    ),
    # `dbt retry` (1.6+) resumes from target/run_results.json instead of
    # rebuilding every model from scratch -- this only works because this
    # BashOperator path runs `dbt build` in the SAME stable {ws}/dbt
    # directory every time (generate-dbt-project never touches target/), so
    # a prior failed run's state survives an Airflow task retry. `|| dbt
    # build` covers the first-ever run (no run_results.json to retry yet)
    # and any retry that itself needs a full rebuild. NOT wired into the
    # Cosmos-backed path (cosmos_dag.py's DbtBuildLocalOperator) -- found
    # live that Cosmos clones the project to a fresh temp directory on
    # every invocation, so there is no persistent target/ for `dbt retry`
    # to resume from without deeper Cosmos configuration this session
    # hasn't verified; that stays a plain re-run there for now.
    upstream=("resolve_features",),
    description=(
        "Generate + build the dbt project (staging/intermediate/marts, medallion-"
        "layered bronze/silver/gold) for a workspace on the dbt path. Supersedes "
        "medallion_build+kpi_results on that path. Retries resume via `dbt retry`."
    ),
    produces=("{ws}/dbt/target/manifest.json", "{ws}/dbt/models/**/*.sql"),
)


def stages_for_workspace(repo_root: str, workspace: str) -> tuple[Stage, ...]:
    """The real stage set for one specific workspace.

    Byte-identical to STAGES for every existing local-only workspace (the
    overwhelming majority, no databricks_source declared at all) -- this
    function is purely additive. For a workspace on the dbt path
    (databricks_source_mode() in {"additive", "exclusive"}), dbt_build
    replaces medallion_build+kpi_results and dashboard's upstream repoints
    to it, so the DAG a workspace actually gets reflects which backend it
    declared, not a hardcoded assumption either way.
    """
    from pathlib import Path

    from core.storage.workspace_layout import WorkspaceLayout

    layout = WorkspaceLayout(project_root=(Path(repo_root) / workspace).resolve())
    if layout.databricks_source_mode() == "local_files":
        return STAGES

    kept = [s for s in STAGES if s.key not in {"medallion_build", "kpi_results"}]
    result: list[Stage] = []
    for stage in kept:
        if stage.key == "kpi_intent_coverage":
            # dbt_build supersedes kpi_results on this path, so the intent check
            # repoints to it -- it still runs AFTER SQL generation and BEFORE the
            # dashboard, which is the invariant that matters.
            result.append(DBT_BUILD_STAGE)
            result.append(replace(stage, upstream=("dbt_build",)))
        else:
            result.append(stage)
    return tuple(result)


def stage_map(stages: tuple[Stage, ...] = STAGES) -> dict[str, Stage]:
    return {s.key: s for s in stages}


def topological_order(stages: tuple[Stage, ...] = STAGES) -> list[str]:
    """Stage keys in a valid dependency order (raises on a cycle/unknown dep)."""
    smap = stage_map(stages)
    visited: dict[str, int] = {}  # 0 = visiting, 1 = done
    order: list[str] = []

    def visit(key: str, path: tuple[str, ...]) -> None:
        state = visited.get(key)
        if state == 1:
            return
        if state == 0:
            raise ValueError(f"cycle in pipeline stages: {' -> '.join(path + (key,))}")
        if key not in smap:
            raise ValueError(f"unknown stage dependency: {key}")
        visited[key] = 0
        for up in smap[key].upstream:
            visit(up, path + (key,))
        visited[key] = 1
        order.append(key)

    for s in stages:
        visit(s.key, ())
    return order


def command_for(stage: Stage, workspace: str) -> str:
    return stage.command.replace("{ws}", workspace)


def recommend_orchestrator(
    *,
    scheduled: bool = False,
    existing_airflow: bool = False,
    want_lineage_ui: bool = False,
    want_backfills: bool = False,
) -> dict[str, str]:
    """Recommend an execution surface from the situation. Three surfaces share
    the one `STAGES` topology, so this is purely about operational fit:

    - plain `pipeline-run` (no deps): a one-shot / CI / local end-to-end run.
    - Dagster: artifact-centric -- lineage UI, stale-only re-materialization,
      partition-aware backfills.
    - Airflow: task-centric -- when you already run Airflow or need its scheduler
      / operator breadth.
    """
    if not scheduled and not existing_airflow and not want_lineage_ui and not want_backfills:
        choice, why = ("pipeline-run", "One-shot/CI/local run with no scheduling "
                       "needs -> the dependency-free sequential runner.")
    elif existing_airflow and not (want_lineage_ui or want_backfills):
        choice, why = ("airflow", "An Airflow platform already exists and the "
                       "needs are scheduling/operators -> reuse it.")
    elif want_lineage_ui or want_backfills:
        choice, why = ("dagster", "Artifact lineage UI and/or stale-only "
                       "backfills -> Dagster's asset model fits best.")
    else:
        choice, why = ("dagster", "Scheduled run with no existing Airflow -> "
                       "Dagster (asset-based) is the platform default.")
    return {"recommended": choice, "rationale": why}


__all__ = [
    "Stage", "STAGES", "DBT_BUILD_STAGE", "stages_for_workspace",
    "stage_map", "topological_order", "command_for",
    "recommend_orchestrator",
]
