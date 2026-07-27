"""Platform readiness: is Databricks/dbt/Airflow actually usable right now.

Read-only, no side effects, no new auth mechanism -- reuses exactly the
primitives every real execution path already goes through:
`resolve_databricks_config()` + `DatabricksClient.health_check()` (the same
call `build_execution_backend()` makes before falling back to DuckDB), and
`cosmos_dag.cosmos_available()` (the same check `airflow_dag.build_dag()`
uses to decide Cosmos vs. a plain BashOperator).

dbt/Airflow are reported, never treated as blockers -- they're only
required for the dbt-project-generation/Cosmos-orchestration path, not for
the default local-DuckDB KPI flow every workspace can use with nothing
installed beyond the base project.
"""
from __future__ import annotations
from core.observability.cost_ledger import anchored

import argparse
import importlib.util
import json
from pathlib import Path
from dataclasses import asdict, dataclass, field
from typing import Any


def _check_databricks(cfg: Any) -> dict[str, Any]:
    from core.execution.databricks_client import DatabricksClient

    if not cfg.enabled:
        return {
            "status": "not_configured",
            "detail": "databricks.enabled is false in config/lock.toml (or no enterprise override) "
            "-- fine for local-only workspaces, required for databricks_source workspaces.",
        }
    client = DatabricksClient(cfg)
    ok, detail = client.health_check()
    return {
        "status": "ready" if ok else "blocked",
        "detail": detail,
        "catalog": cfg.catalog,
        "execution_mode": cfg.execution,
    }


def _check_dbt() -> dict[str, Any]:
    if importlib.util.find_spec("dbt") is None:
        return {
            "status": "not_installed",
            "detail": "dbt-core not installed -- only needed for a databricks_source workspace's "
            "generate-dbt-project step. Install: pip install -e '.[dbt]'",
        }
    try:
        from dbt.version import __version__ as dbt_version
    except Exception as exc:
        return {"status": "broken", "detail": f"dbt-core installed but failed to import: {exc}"}
    try:
        has_databricks_adapter = importlib.util.find_spec("dbt.adapters.databricks") is not None
    except ModuleNotFoundError:
        has_databricks_adapter = False
    if not has_databricks_adapter:
        return {
            "status": "partial",
            "detail": f"dbt-core {dbt_version} installed, but dbt-databricks adapter is missing.",
        }
    return {"status": "ready", "detail": f"dbt-core {dbt_version} with dbt-databricks adapter."}


def _check_airflow() -> dict[str, Any]:
    from core.orchestration.cosmos_dag import cosmos_available

    if importlib.util.find_spec("airflow") is None:
        return {
            "status": "not_installed",
            "detail": "apache-airflow not installed -- optional; only needed for scheduled "
            "orchestration. `uv run pipeline-run` / Dagster work without it.",
        }
    if not cosmos_available():
        return {
            "status": "partial",
            "detail": "Airflow installed, astronomer-cosmos missing -- the dbt_build stage falls "
            "back to a plain subprocess BashOperator instead of Cosmos's DBT_RUNNER task.",
        }
    return {"status": "ready", "detail": "Airflow + astronomer-cosmos installed."}


def _check_cost_telemetry(cfg: Any) -> dict[str, Any]:
    """Whether the system schemas Phase 3 reads back are actually queryable.

    `system.billing` and `system.query` are NOT enabled by default and an admin
    must turn them on per metastore -- so the honest failure mode is a warehouse
    that works perfectly and a cost query that returns nothing. Checked with the
    same read-only client every other path uses; never enables anything.
    """
    from core.onboarding.databricks.deploy_gates import check_remote_approval

    enable_hint = (
        "An account admin must enable them once per metastore: "
        "`databricks system-schemas enable <metastore-id> billing` and "
        "`... query`."
    )
    if not cfg.enabled:
        return {"status": "not_configured",
                "detail": "databricks is not enabled; there is no warehouse spend to read."}
    gate = check_remote_approval()
    if not gate.ok:
        return {"status": "unknown", "detail": f"{gate.blocking_reason}; cannot probe. {enable_hint}"}
    from core.execution.databricks_client import DatabricksClient

    client = DatabricksClient(cfg)
    missing: list[str] = []
    for table in ("system.billing.usage", "system.billing.list_prices", "system.query.history"):
        try:
            client.execute_query(f"SELECT 1 FROM {table} LIMIT 1")
        except Exception as exc:  # not enabled / not granted / does not exist
            missing.append(f"{table} ({type(exc).__name__})")
    if missing:
        return {"status": "blocked",
                "detail": f"unreadable: {', '.join(missing)}. {enable_hint}"}
    return {"status": "ready",
            "detail": "system.billing + system.query readable; "
                      "`reconcile-warehouse-cost` can attribute run cost."}


@dataclass
class ReadinessReport:
    databricks: dict[str, Any]
    dbt: dict[str, Any]
    airflow: dict[str, Any]
    cost_telemetry: dict[str, Any] = field(default_factory=dict)

    def summary(self) -> dict[str, Any]:
        return asdict(self)

    def blockers(self) -> list[str]:
        """Only Databricks `blocked` (configured but unreachable/unauthenticated)
        is a real blocker -- dbt/Airflow being absent is a capability gap for
        the cloud-native path, not a broken state, since the local-DuckDB path
        works without either."""
        return [f"databricks: {self.databricks['detail']}"] if self.databricks["status"] == "blocked" else []


def check(enterprise_id: str = "") -> ReadinessReport:
    # Resolved ONCE and handed to both Databricks checks: two calls would
    # double every per-enterprise config read for no gain.
    from core.config import resolve_databricks_config

    cfg = resolve_databricks_config(enterprise_id)
    return ReadinessReport(
        databricks=_check_databricks(cfg),
        dbt=_check_dbt(),
        airflow=_check_airflow(),
        cost_telemetry=_check_cost_telemetry(cfg),
    )


@anchored("check-platform-readiness")
def main(argv: list[str] | None = None) -> int:
    from core.paths import PROJECT_ROOT
    from core.storage.workspace_layout import WorkspaceLayout

    parser = argparse.ArgumentParser(
        description="Read-only check: is Databricks/dbt/Airflow usable right now."
    )
    parser.add_argument(
        "--workspace", default="",
        help="Resolve the enterprise_id from this workspace's databricks_source declaration "
        "(same resolution as onboarding/execution). Omit when no workspace is selected yet.",
    )
    parser.add_argument("--repo-root", default=str(PROJECT_ROOT))
    parser.add_argument(
        "--enterprise-id", default="",
        help="Explicit override -- resolve config/enterprises/<id>/lock.toml directly instead of "
        "deriving it from --workspace.",
    )
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)

    enterprise_id = args.enterprise_id
    if not enterprise_id and args.workspace:
        layout = WorkspaceLayout(project_root=Path(args.repo_root).resolve() / args.workspace)
        enterprise_id = layout.enterprise_id()

    report = check(enterprise_id)
    if args.json:
        print(json.dumps(report.summary(), indent=2))
    else:
        for name, result in report.summary().items():
            print(f"{name}: {result['status']} -- {result['detail']}")
        if report.blockers():
            print(f"\nBlockers: {len(report.blockers())}")
    return 1 if report.blockers() else 0


if __name__ == "__main__":
    raise SystemExit(main())
