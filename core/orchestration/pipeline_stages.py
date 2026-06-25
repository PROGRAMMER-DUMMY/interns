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

from dataclasses import dataclass, field


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
        key="dashboard",
        command="uv run workspace-dashboard --workspace {ws} --screen",
        upstream=("kpi_results",),
        description="Build live dashboard from gold; structure-aware screener gate.",
        produces=("interns/reports/dashboard_screener/current.json",),
    ),
)


def stage_map() -> dict[str, Stage]:
    return {s.key: s for s in STAGES}


def topological_order() -> list[str]:
    """Stage keys in a valid dependency order (raises on a cycle/unknown dep)."""
    smap = stage_map()
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

    for s in STAGES:
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
    "Stage", "STAGES", "stage_map", "topological_order", "command_for",
    "recommend_orchestrator",
]
