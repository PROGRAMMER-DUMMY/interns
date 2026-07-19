"""Local DuckDB warehouse — mirrors Databricks SQL Warehouse + Unity Catalog locally.

Creates a persistent warehouse.duckdb that registers Bronze Delta tables as
fact_* / dim_* views. Generated KPI SQL then uses clean table names instead of
read_csv_auto() or delta_scan() paths — the same pattern as Databricks catalog refs.

Layout:
  interns/state/warehouse.duckdb      ← the warehouse (open with any DuckDB client)
  interns/state/medallion/bronze/     ← Bronze Delta tables
  interns/state/medallion/silver/     ← Silver Delta tables (KPI feature views)
  interns/state/medallion/gold/       ← Gold Delta tables (KPI result views)

Table naming:
  fact_{stem}   ← base/transaction tables (highest ref-count or name match)
  dim_{stem}    ← dimension tables (patients, departments, providers, etc.)
  kpi_{id}_features  ← Silver view per KPI
  kpi_{id}_results   ← Gold view per KPI
"""
from __future__ import annotations
from core.observability.cost_ledger import anchored

import argparse
import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from core.onboarding.kpi.sql_generator import (
    _choose_base_source,
    _feature_source_refs,
    _safe_name,
    _repo_path,
    _rel,
)
from core.storage.workspace_layout import WorkspaceLayout


# Fact vs dimension is derived from workspace evidence (KPI base sources, else the
# largest table by row count) — never from hardcoded domain table names.


@dataclass
class WarehouseTable:
    warehouse_name: str       # e.g. fact_transactions
    delta_path: str           # absolute path to Delta table
    source_path: str          # original CSV/Parquet path (relative)
    table_type: str           # fact | dim | unknown
    row_count: int = 0
    columns: list[str] = field(default_factory=list)


@dataclass
class WarehouseSetupResult:
    warehouse_path: str
    tables: list[dict[str, Any]]
    status: str

    def summary(self) -> dict[str, Any]:
        return {
            "warehouse_path": self.warehouse_path,
            "table_count": len(self.tables),
            "tables": self.tables,
            "status": self.status,
        }


class LocalWarehouse:
    def __init__(self, repo_root: str | Path, workspace: str | Path) -> None:
        self.repo_root = Path(repo_root).resolve()
        self.workspace = (self.repo_root / workspace).resolve()
        self.layout = WorkspaceLayout(project_root=self.workspace)

    @property
    def warehouse_path(self) -> Path:
        return self.layout.state_dir / "warehouse.duckdb"

    def setup(self) -> WarehouseSetupResult:
        """Ingest CSVs to Bronze Delta, classify as fact/dim, register in warehouse.duckdb."""
        import duckdb

        profile_map = self._profile_map()
        base_sources = self._detect_fact_tables(profile_map)
        if not base_sources and profile_map:
            # No KPI mapping to identify the fact: fall back to the largest table by
            # row count (a structural, workspace-agnostic signal — facts are typically
            # the high-volume transaction tables).
            largest = max(profile_map.items(), key=lambda kv: int(kv[1].get("row_count") or 0))
            base_sources = {largest[0]}

        tables: list[WarehouseTable] = []
        for source_path, profile in profile_map.items():
            stem = _safe_name(Path(source_path).stem)
            bronze_path = self.layout.bronze_dir / stem
            table_type = self._classify(stem, source_path, base_sources)
            prefix = "fact" if table_type == "fact" else "dim"
            warehouse_name = f"{prefix}_{stem}"

            # Ingest to Bronze Delta if not already present
            self._ingest_bronze(source_path, bronze_path)

            schema = profile.get("schema") or {}
            tables.append(WarehouseTable(
                warehouse_name=warehouse_name,
                delta_path=str(bronze_path),
                source_path=source_path,
                table_type=table_type,
                row_count=profile.get("row_count", 0),
                columns=list(schema.keys()),
            ))

        # Build/update the DuckDB warehouse
        self.warehouse_path.parent.mkdir(parents=True, exist_ok=True)
        con = duckdb.connect(str(self.warehouse_path))
        try:
            con.execute("INSTALL delta; LOAD delta;")
            for t in tables:
                delta_path = Path(t.delta_path)
                if not (delta_path / "_delta_log").exists():
                    continue
                con.execute(f'DROP VIEW IF EXISTS "{t.warehouse_name}";')
                con.execute(
                    f'CREATE VIEW "{t.warehouse_name}" AS '
                    f"SELECT * FROM delta_scan('{delta_path.as_posix()}');"
                )

            # Register Silver and Gold views if they exist
            for silver_table in self.layout.silver_dir.glob("*/"):
                if (silver_table / "_delta_log").exists():
                    vname = silver_table.name
                    con.execute(f'DROP VIEW IF EXISTS "{vname}";')
                    con.execute(
                        f'CREATE VIEW "{vname}" AS '
                        f"SELECT * FROM delta_scan('{silver_table.as_posix()}');"
                    )
            for gold_table in self.layout.gold_dir.glob("*/"):
                if (gold_table / "_delta_log").exists():
                    vname = gold_table.name
                    con.execute(f'DROP VIEW IF EXISTS "{vname}";')
                    con.execute(
                        f'CREATE VIEW "{vname}" AS '
                        f"SELECT * FROM delta_scan('{gold_table.as_posix()}');"
                    )
        finally:
            con.close()

        return WarehouseSetupResult(
            warehouse_path=_rel(self.warehouse_path, self.repo_root),
            tables=[
                {
                    "warehouse_name": t.warehouse_name,
                    "type": t.table_type,
                    "rows": t.row_count,
                    "columns": t.columns,
                    "delta_path": t.delta_path,
                }
                for t in tables
            ],
            status="ready",
        )

    def list_tables(self) -> list[dict[str, Any]]:
        """List all registered tables in the warehouse."""
        import duckdb
        if not self.warehouse_path.exists():
            return []
        con = duckdb.connect(str(self.warehouse_path), read_only=True)
        try:
            rows = con.execute(
                "SELECT table_name, table_type FROM information_schema.tables "
                "WHERE table_schema = 'main' ORDER BY table_name"
            ).fetchall()
            return [{"name": r[0], "type": r[1]} for r in rows]
        finally:
            con.close()

    def query(self, sql: str) -> Any:
        """Run a SQL query against the warehouse.
        Opens read-write when SQL contains DDL (CREATE/DROP), read-only for pure SELECT."""
        import duckdb
        has_ddl = bool(re.search(r"\b(CREATE|DROP|INSERT|UPDATE|DELETE|COPY)\b", sql, re.IGNORECASE))
        con = duckdb.connect(str(self.warehouse_path), read_only=not has_ddl)
        try:
            con.execute("LOAD delta;")
            stmts = [s.strip() for s in re.split(r";\s*\n|;\s*$", sql) if s.strip()]
            result = None
            for stmt in stmts:
                # Strip leading comment lines, keep the actual SQL
                clean = "\n".join(
                    ln for ln in stmt.splitlines()
                    if not ln.strip().startswith("--")
                ).strip()
                if not clean:
                    continue
                result = con.execute(clean)
            return result.fetchdf() if result else None
        finally:
            con.close()

    # ── internal helpers ───────────────────────────────────────────────────

    def _detect_fact_tables(self, profile_map: dict[str, dict[str, Any]]) -> set[str]:
        """Use _choose_base_source logic to identify fact tables across all KPIs."""
        mapping_path = self.layout.contracts_dir / "kpi_feature_mapping.json"
        if not mapping_path.exists():
            return set()
        mapping = json.loads(mapping_path.read_text(encoding="utf-8"))
        from core.onboarding.kpi.sql_generator import _grain_dimensions
        from core.onboarding.relationships.base_source_selector import (
            load_pinned_base_sources,
        )
        from core.onboarding.relationships.contracts import load_relationship_contracts

        relationships = load_relationship_contracts(
            self.repo_root, _rel(self.workspace, self.repo_root)
        )
        pinned = load_pinned_base_sources(self.layout.contracts_dir)
        fact_sources: set[str] = set()
        for kpi in mapping.get("kpis", []):
            refs = [
                ref
                for feature in kpi.get("features", [])
                for ref in _feature_source_refs(feature, self.repo_root)
            ]
            base = _choose_base_source(
                refs,
                profile_map,
                relationships,
                grain_dimensions=_grain_dimensions(kpi),
                pinned=pinned.get(str(kpi.get("kpi_id") or "")),
            )
            if base:
                fact_sources.add(base)
        return fact_sources

    def _classify(self, stem: str, source_path: str, fact_sources: set[str]) -> str:
        # Workspace-agnostic: the fact is whatever the KPI mappings (or the largest-table
        # fallback) identified as a base source; everything else is a dimension.
        return "fact" if source_path in fact_sources else "dim"

    def _ingest_bronze(self, source_path: str, bronze_path: Path) -> None:
        if (bronze_path / "_delta_log").exists():
            return  # already ingested
        full_path = self.repo_root / source_path
        if not full_path.exists():
            return
        bronze_path.mkdir(parents=True, exist_ok=True)
        try:
            import deltalake
            import pyarrow.csv as pa_csv
            table = pa_csv.read_csv(str(full_path))
            deltalake.write_deltalake(str(bronze_path), table, mode="overwrite")
        except Exception:
            pass

    def _profile_map(self) -> dict[str, dict[str, Any]]:
        index = self.layout.profiles_dir / "profile_index.json"
        if not index.exists():
            return {}
        data = json.loads(index.read_text(encoding="utf-8"))
        return {
            _repo_path(str(p.get("path") or ""), self.repo_root): p
            for p in data.get("profiles", [])
            if p.get("path")
        }


# ── warehouse-aware SQL generator helper ──────────────────────────────────────

def warehouse_table_name(
    source_path: str,
    fact_sources: set[str],
    stem_override: str = "",
) -> str:
    """Return the warehouse table name for a given source path.

    fact_/dim_ is decided by workspace evidence (the KPI base sources passed in),
    not by hardcoded domain table names.
    """
    stem = stem_override or _safe_name(Path(source_path).stem)
    return f"fact_{stem}" if source_path in fact_sources else f"dim_{stem}"



@anchored("kpi-local-warehouse")
def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Set up local DuckDB warehouse.")
    sub = parser.add_subparsers(dest="command")

    setup_p = sub.add_parser("setup", help="Ingest sources and register tables")
    setup_p.add_argument("--workspace", required=True)
    setup_p.add_argument("--repo-root", default=".")

    list_p = sub.add_parser("list", help="List registered tables")
    list_p.add_argument("--workspace", required=True)
    list_p.add_argument("--repo-root", default=".")

    query_p = sub.add_parser("query", help="Run a SQL query against the warehouse")
    query_p.add_argument("--workspace", required=True)
    query_p.add_argument("--repo-root", default=".")
    query_p.add_argument("--sql", required=True)

    args = parser.parse_args(argv)
    if not args.command:
        parser.print_help()
        return 1

    wh = LocalWarehouse(repo_root=args.repo_root, workspace=args.workspace)

    if args.command == "setup":
        result = wh.setup()
        print(json.dumps(result.summary(), indent=2))

    elif args.command == "list":
        tables = wh.list_tables()
        for t in tables:
            print(f"  {t['type']:4s}  {t['name']}")

    elif args.command == "query":
        df = wh.query(args.sql)
        print(df.to_string())

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
