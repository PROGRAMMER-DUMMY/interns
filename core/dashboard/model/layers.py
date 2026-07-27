"""Read materialized medallion gold/silver tables as Polars frames.

The dashboard reads the VALIDATED gold layer directly instead of re-running each
KPI's solution SQL. This is faster (no recompute) and cannot drift from the KPI
result packet, because gold *is* the packet's data.

Two sources, selected local-first (never a hardcoded branch in a caller):

- **Local** (every existing workspace, and any "additive"-mode workspace
  whose gold was actually built by `medallion build`): Delta tables under
  `interns/state/medallion/`. Read via DuckDB's `delta_scan` (the same
  engine the KPI SQL already uses); the optional `deltalake` package is not
  required.
- **Databricks dbt mart** (only when local has nothing -- typically an
  "exclusive"-mode workspace, where local dataset discovery was skipped
  entirely so there is no local gold to have): the
  `<catalog>.<gold_schema>.fct_<kpi_id>` table `generate-dbt-project` +
  `dbt build` produced (core.onboarding.kpi.dbt_project_generator). Read via
  `DatabricksClient`, gated by the SAME `AUTORESEARCH_ALLOW_REMOTE_EXECUTION`
  approval every other Databricks execution path in this platform requires
  (core.execution.backend.build_execution_backend) -- the dashboard gets no
  side door around it. PHI/sensitive-column masking is already applied at
  mart-build time (dbt_project_generator's `_feature_select_items()`), so a
  read here needs no additional governance check of its own.

`WorkspaceLayout.databricks_source_mode()` alone does NOT decide the source:
"additive" mode means local files AND Databricks are both profiled for
contract discovery, not that gold results live in Databricks -- an existing
workspace whose gold was built the ordinary local way must keep reading it
unchanged. Local-first is what actually implements "selected by
databricks_source_mode(), not hardcoded" without silently regressing every
current real workspace.

Every read returns an empty / None result on failure -- never raises -- to
match the defensive style of `core.dashboard.profile.execute_result_view`.
That is a RENDERING decision, not a reporting one: a REMOTE read failure is
logged loudly to stderr before returning None, because a swallowed one made a
permission denial indistinguishable from "no KPI built yet" and shipped an
empty dashboard and a chartless deck in silence (TRAP 4).
Silver/bronze reads stay local-only for now (deep-drill sources, not the
KPI-facing gold layer this phase's verify bar covers); a Databricks read path
for them would follow this same pattern if a workspace needs it.
"""
from __future__ import annotations

import os
import sys
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import polars as pl

from core.storage.workspace_layout import WorkspaceLayout

_GOLD_SUFFIX = "_results"
_SILVER_SUFFIX = "_features"
_MART_PREFIX = "fct_"


def _delta_conn():
    """A DuckDB connection with the delta extension loaded.

    Tries LOAD first (offline, extension already cached) and only falls back to
    INSTALL when the extension is genuinely absent.
    """
    import duckdb

    con = duckdb.connect(":memory:")
    try:
        con.execute("LOAD delta;")
    except Exception:
        con.execute("INSTALL delta; LOAD delta;")
    return con


def _read_delta(path: Path) -> pl.DataFrame | None:
    if not path.exists():
        return None
    try:
        con = _delta_conn()
    except Exception:
        return None
    try:
        # DuckDB wants a forward-slash absolute path even on Windows.
        p = path.resolve().as_posix()
        return con.execute(f"SELECT * FROM delta_scan('{p}')").pl()
    except Exception:
        return None
    finally:
        try:
            con.close()
        except Exception:
            pass


def _list_local_gold_kpis(layout: WorkspaceLayout) -> list[str]:
    gold = layout.gold_dir
    if not gold.exists():
        return []
    out: list[str] = []
    for child in sorted(gold.iterdir()):
        if child.is_dir() and child.name.endswith(_GOLD_SUFFIX):
            out.append(child.name[: -len(_GOLD_SUFFIX)])
    return out


def list_gold_kpis(layout: WorkspaceLayout) -> list[str]:
    """KPI ids that have a materialized gold result table, sorted.

    Local-first: "additive" mode means local files AND Databricks are BOTH
    profiled for contract discovery -- it says nothing about which backend
    actually produced the currently-materialized gold data. An existing
    workspace whose gold was built by `medallion build` (the overwhelming
    majority) must keep reading it unchanged; only when local has nothing
    (typically "exclusive" mode, where local dataset discovery was skipped
    entirely) does this fall through to the dbt-built Databricks mart.
    """
    local = _list_local_gold_kpis(layout)
    if local or layout.databricks_source_mode() == "local_files":
        return local
    return _list_databricks_gold_kpis(layout)


def read_gold(layout: WorkspaceLayout, kpi_id: str) -> pl.DataFrame | None:
    """The validated KPI result rows (filter + cuts applied). None if absent.

    Local-first, same rationale as list_gold_kpis()."""
    local = _read_delta(layout.gold_dir / f"{kpi_id}{_GOLD_SUFFIX}")
    if local is not None or layout.databricks_source_mode() == "local_files":
        return local
    return _read_databricks_gold(layout, kpi_id)


def read_silver(layout: WorkspaceLayout, kpi_id: str) -> pl.DataFrame | None:
    """Row-grain joined+derived features for a KPI. None if absent.

    Silver is best-effort: for some KPIs it does not carry every gold cut column
    (a derived/joined dimension may only exist in gold), and it may carry PII the
    gold layer does not. Callers must treat it as a deep-drill source, gated by
    column coverage and redaction.
    """
    return _read_delta(layout.silver_dir / f"{kpi_id}{_SILVER_SUFFIX}")


def list_bronze_tables(layout: WorkspaceLayout) -> list[str]:
    """Names of the raw bronze entity tables (transactions/patients/...)."""
    bronze = layout.bronze_dir
    if not bronze.exists():
        return []
    return sorted(c.name for c in bronze.iterdir() if c.is_dir())


def read_bronze(layout: WorkspaceLayout, table: str) -> pl.DataFrame | None:
    """A raw bronze entity table as a Polars frame. None if absent.

    Bronze is the only conformed (shared) layer in this medallion; the conformed
    semantic model cleans + joins these into a star. Returns the raw rows -- the
    caller applies silver-grade cleaning.
    """
    return _read_delta(layout.bronze_dir / table)


def _dbt_dir(layout: WorkspaceLayout) -> Path:
    return layout.project_root / "dbt"


def _read_dbt_gold_schema(layout: WorkspaceLayout) -> str | None:
    """The marts `+schema:` dbt_project_generator.py actually wrote, read
    from the generated dbt_project.yml rather than assumed -- `--gold-schema`
    is a CLI override (default "gold"), so a workspace may have customized it.
    """
    project_yml = _dbt_dir(layout) / "dbt_project.yml"
    if not project_yml.exists():
        return None
    try:
        import yaml

        data = yaml.safe_load(project_yml.read_text(encoding="utf-8")) or {}
        models = data.get("models") or {}
        for project_cfg in models.values():
            if not isinstance(project_cfg, dict):
                continue
            marts = project_cfg.get("marts")
            if isinstance(marts, dict) and marts.get("+schema"):
                return str(marts["+schema"])
    except Exception:
        return None
    return None


def _databricks_read_context(layout: WorkspaceLayout) -> tuple[str, str, Any] | None:
    """(catalog, gold_schema, DatabricksClient) if a Databricks-sourced gold
    read is currently possible, else None. Gated by the same
    AUTORESEARCH_ALLOW_REMOTE_EXECUTION approval every other Databricks
    execution path requires (core.execution.backend.build_execution_backend)
    -- never a side door around it, even for a read-only dashboard query.
    """
    if os.environ.get("AUTORESEARCH_ALLOW_REMOTE_EXECUTION") != "1":
        return None
    settings = layout.load_settings()
    source = settings.get("databricks_source")
    catalog = str((source or {}).get("catalog") or "").strip()
    if not catalog:
        return None
    gold_schema = _read_dbt_gold_schema(layout)
    if not gold_schema:
        return None
    try:
        from core.config import resolve_databricks_config
        from core.execution.databricks_client import DatabricksClient

        cfg = resolve_databricks_config(layout.enterprise_id())
        if not cfg.is_active():
            return None
        client = DatabricksClient(cfg)
    except Exception:
        return None
    return catalog, gold_schema, client


def _list_databricks_gold_kpis(layout: WorkspaceLayout) -> list[str]:
    ctx = _databricks_read_context(layout)
    if ctx is None:
        return []
    catalog, gold_schema, client = ctx
    try:
        cols, rows = client.execute_query(f"SHOW TABLES IN `{catalog}`.`{gold_schema}`")
    except Exception:
        return []
    name_idx = cols.index("tableName") if "tableName" in cols else 1
    out = sorted(
        str(row[name_idx])[len(_MART_PREFIX):]
        for row in rows
        if str(row[name_idx]).startswith(_MART_PREFIX)
    )
    return out


def _read_databricks_gold(layout: WorkspaceLayout, kpi_id: str) -> pl.DataFrame | None:
    ctx = _databricks_read_context(layout)
    if ctx is None:
        return None
    catalog, gold_schema, client = ctx
    try:
        cols, rows = client.execute_query(
            f"SELECT * FROM `{catalog}`.`{gold_schema}`.`{_MART_PREFIX}{kpi_id}`"
        )
        return pl.DataFrame([list(r) for r in rows], schema=cols, orient="row")
    except Exception as exc:
        # Loud, not silent. A swallowed exception here made a permission denial
        # indistinguishable from "no KPI built yet": the audited run shipped
        # empty dashboards and a chartless deck and said nothing (TRAP 4). The
        # return stays None because five callers type this as Optional and the
        # aggregate-pushdown work (Phase 8) will rewrite the signature anyway --
        # but a failure a human never sees is the actual defect.
        from core.observability.log_redaction import redact

        print(
            f"[dashboard] gold read FAILED for {kpi_id} from "
            f"`{catalog}`.`{gold_schema}`: {type(exc).__name__}: {redact(str(exc))}. "
            "Rendering will treat this KPI as absent -- it is not.",
            file=sys.stderr,
            flush=True,
        )
        return None


@dataclass(frozen=True)
class GoldSourceStatus:
    """The dashboard's staleness/availability signal -- a blocked or failed
    pipeline must be visibly distinguishable from fresh data, never silently
    indistinguishable from "no KPIs built yet"."""
    mode: str                       # "local_files" | "additive" | "exclusive"
    source: str                     # "local_delta" | "databricks_dbt_mart" | "unavailable"
    remote_read_approved: bool
    as_of: str | None               # ISO-8601 UTC timestamp, None if unknown

    def summary(self) -> dict[str, Any]:
        return self.__dict__.copy()


def _local_gold_as_of(layout: WorkspaceLayout) -> str | None:
    gold = layout.gold_dir
    if not gold.exists():
        return None
    latest: float | None = None
    for child in gold.iterdir():
        log_dir = child / "_delta_log"
        if not log_dir.exists():
            continue
        for f in log_dir.glob("*.json"):
            mtime = f.stat().st_mtime
            if latest is None or mtime > latest:
                latest = mtime
    if latest is None:
        return None
    return datetime.fromtimestamp(latest, tz=timezone.utc).isoformat()


def gold_source_status(layout: WorkspaceLayout) -> GoldSourceStatus:
    """Where the dashboard's gold data currently comes from, and how fresh
    it is -- for the dashboard's staleness banner (dbt+Airflow plan section
    D4). Local-first, same rationale as list_gold_kpis()/read_gold()."""
    mode = layout.databricks_source_mode()
    local_as_of = _local_gold_as_of(layout)
    if local_as_of or mode == "local_files":
        return GoldSourceStatus(mode, "local_delta" if local_as_of else "unavailable", True, local_as_of)

    approved = os.environ.get("AUTORESEARCH_ALLOW_REMOTE_EXECUTION") == "1"
    ctx = _databricks_read_context(layout)
    if ctx is None:
        return GoldSourceStatus(mode, "unavailable", approved, None)
    catalog, gold_schema, client = ctx
    kpi_ids = _list_databricks_gold_kpis(layout)
    as_of = None
    if kpi_ids:
        try:
            hist_cols, rows = client.execute_query(
                f"DESCRIBE HISTORY `{catalog}`.`{gold_schema}`.`{_MART_PREFIX}{kpi_ids[0]}` LIMIT 1"
            )
            # DESCRIBE HISTORY's column order is `version, timestamp, ...` --
            # found live: assuming position 0 silently returned the version
            # number ("1") as a fake "as_of" timestamp. Look up by name.
            ts_idx = hist_cols.index("timestamp") if "timestamp" in hist_cols else 1
            if rows:
                as_of = str(rows[0][ts_idx])
        except Exception:
            as_of = None
    return GoldSourceStatus(mode, "databricks_dbt_mart", approved, as_of)


__all__ = [
    "GoldSourceStatus", "gold_source_status",
    "list_bronze_tables", "list_gold_kpis", "read_bronze", "read_gold", "read_silver",
]
