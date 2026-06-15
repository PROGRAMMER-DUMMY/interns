"""Read materialized medallion gold/silver Delta tables as Polars frames.

The dashboard reads the VALIDATED gold layer directly instead of re-running each
KPI's solution SQL. This is faster (no recompute) and cannot drift from the KPI
result packet, because gold *is* the packet's data.

DuckDB's `delta_scan` is the reader (the same engine the KPI SQL already uses);
the optional `deltalake` package is not required. Every read returns an empty /
None result on failure -- never raises -- to match the defensive style of
`core.dashboard.profile.execute_result_view`.
"""
from __future__ import annotations

from pathlib import Path

import polars as pl

from core.storage.workspace_layout import WorkspaceLayout

_GOLD_SUFFIX = "_results"
_SILVER_SUFFIX = "_features"


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


def list_gold_kpis(layout: WorkspaceLayout) -> list[str]:
    """KPI ids that have a materialized gold result table, sorted."""
    gold = layout.gold_dir
    if not gold.exists():
        return []
    out: list[str] = []
    for child in sorted(gold.iterdir()):
        if child.is_dir() and child.name.endswith(_GOLD_SUFFIX):
            out.append(child.name[: -len(_GOLD_SUFFIX)])
    return out


def read_gold(layout: WorkspaceLayout, kpi_id: str) -> pl.DataFrame | None:
    """The validated KPI result rows (filter + cuts applied). None if absent."""
    return _read_delta(layout.gold_dir / f"{kpi_id}{_GOLD_SUFFIX}")


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


__all__ = [
    "list_bronze_tables", "list_gold_kpis", "read_bronze", "read_gold", "read_silver",
]
