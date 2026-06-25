"""Auto-generate a starter project from a folder of CSVs.

This makes the framework "generic": point it at any CSV folder and it infers a
star schema (tables, primary keys, relationships), a handful of measures, and a
first dashboard. The agent (or you) then refines the YAML.
"""

from __future__ import annotations

import re
from pathlib import Path

import polars as pl
import yaml

NUMERIC_HINT = re.compile(r"amount|paid|cost|charge|price|total|qty|quantity|count|deduct|copay|coins",
                          re.I)
ID_HINT = re.compile(r"id$", re.I)


def _slug(name: str) -> str:
    s = re.sub(r"[^0-9a-zA-Z]+", "_", name).strip("_").lower()
    return s or "col"


def _detect_pk(columns: list[str], table: str) -> str | None:
    # prefer a leading column that looks like an id
    for c in columns[:2]:
        if ID_HINT.search(c):
            return c
    # else a column whose slug matches the table name
    for c in columns:
        if _slug(c).startswith(_slug(table)):
            return c
    return columns[0] if columns else None


def scaffold_from_folder(folder: Path | str, root: Path | str,
                         name: str | None = None) -> tuple[dict, dict]:
    """Return (project_dict, dashboard_dict) inferred from CSVs in ``folder``."""
    folder = Path(folder)
    root = Path(root)
    csvs = sorted(folder.glob("*.csv"))
    if not csvs:
        raise FileNotFoundError(f"no .csv files in {folder}")

    rel_path = _relpath(folder, root)
    ds_name = _slug(folder.name) or "main"

    tables, cols_by_table, dtypes_by_table = [], {}, {}
    for fp in csvs:
        tname = _slug(fp.stem)
        head = pl.read_csv(fp, n_rows=200, infer_schema_length=200)
        head = head.rename({c: str(c).strip() for c in head.columns})
        cols = list(head.columns)
        cols_by_table[tname] = cols
        dtypes_by_table[tname] = head.schema
        pk = _detect_pk(cols, tname)
        tables.append({"name": tname, "source": ds_name, "file": fp.name, "primary_key": pk})

    pk_index = {t["primary_key"]: t["name"] for t in tables if t["primary_key"]}

    # relationships: a column equal to another table's PK -> many_to_one
    relationships = []
    for t in tables:
        for col in cols_by_table[t["name"]]:
            if col == t["primary_key"]:
                continue
            target = pk_index.get(col)
            if target and target != t["name"]:
                relationships.append({
                    "from_table": t["name"], "from_column": col,
                    "to_table": target, "to_column": col, "kind": "many_to_one",
                })

    # fact table = most foreign keys
    fk_count = {t["name"]: 0 for t in tables}
    for r in relationships:
        fk_count[r["from_table"]] += 1
    fact = max(fk_count, key=fk_count.get) if any(fk_count.values()) else tables[0]["name"]

    # measures from numeric columns on the fact table
    measures, measure_names = [], []
    fact_dtypes = dtypes_by_table[fact]
    for col in cols_by_table[fact]:
        if ID_HINT.search(col):
            continue
        if fact_dtypes[col].is_numeric() and NUMERIC_HINT.search(col):
            mname = f"total_{_slug(col)}"
            # Currency only for a column that names a monetary quantity -- a flat
            # `currency` default mislabels plain numeric sums (counts, quantities)
            # as money. Generic; no domain assumptions.
            money = any(t in col.lower() for t in (
                "amount", "cost", "price", "revenue", "spend", "sales", "paid",
                "charge", "claim", "fee", "balance", "payment", "income"))
            measures.append({"name": mname, "label": f"Total {col}",
                             "agg": "sum", "field": f"{fact}.{col}",
                             "fmt": "currency" if money else "int"})
            measure_names.append(mname)
    # always provide a row count + a distinct-entity count
    measures.append({"name": f"{fact}_count", "label": f"# {fact.title()}",
                     "agg": "count", "table": fact, "fmt": "int"})
    measure_names.insert(0, f"{fact}_count")

    project = {
        "name": (name or folder.name).replace("_", " ").title(),
        "datasources": [{"name": ds_name, "type": "csv", "path": rel_path}],
        "tables": tables,
        "relationships": relationships,
        "measures": measures,
        "theme": {"name": "claude"},
        "dashboards_dir": "config/dashboards",
    }

    dashboard = _build_dashboard(fact, tables, relationships, cols_by_table,
                                 dtypes_by_table, measure_names)
    return project, dashboard


def _build_dashboard(fact, tables, relationships, cols, dtypes, measure_names) -> dict:
    # candidate categorical dimensions: low-cardinality text cols on joined dims
    dims = []
    dim_tables = {r["to_table"] for r in relationships if r["from_table"] == fact}
    for t in tables:
        if t["name"] not in dim_tables and t["name"] != fact:
            continue
        for c in cols[t["name"]]:
            if ID_HINT.search(c) or _slug(c) in ("ssn", "phonenumber", "address"):
                continue
            if not dtypes[t["name"]][c].is_numeric():
                dims.append(f"{t['name']}.{c}")

    primary_measure = next((m for m in measure_names if m != f"{fact}_count"), measure_names[0])

    widgets = []
    # KPI row
    for i, mname in enumerate(measure_names[:4]):
        widgets.append({"id": f"kpi_{i}", "type": "kpi", "measure": mname, "width": 3, "height": 130})
    # a couple of bar charts on the first dimensions
    for i, dim in enumerate(dims[:2]):
        widgets.append({
            "id": f"bar_{i}", "type": "bar", "title": f"{primary_measure} by {dim.split('.')[-1]}",
            "measure": primary_measure, "dimension": dim, "limit": 12, "width": 6, "height": 340,
        })
    # a pie if a third dimension exists
    if len(dims) > 2:
        widgets.append({
            "id": "pie_0", "type": "donut", "title": f"Share by {dims[2].split('.')[-1]}",
            "measure": primary_measure, "dimension": dims[2], "limit": 8, "width": 6, "height": 340,
        })
    # a table of detail
    table_dim = dims[0] if dims else None
    if table_dim:
        widgets.append({
            "id": "table_0", "type": "table", "title": "Detail",
            "dimension": table_dim, "measures": measure_names[:3],
            "limit": 25, "width": 6, "height": 340,
        })

    return {
        "id": "overview", "title": "Overview", "order": 10,
        "description": "Auto-generated from your data. Edit this YAML to customize.",
        "filters": [{"id": "f_" + d.split(".")[0], "field": d, "type": "multi"} for d in dims[:2]],
        "widgets": widgets,
    }


def write_scaffold(folder: Path | str, root: Path | str, *, force: bool = False) -> Path:
    """Generate and write project.yaml + dashboards/overview.yaml under ``root``."""
    root = Path(root)
    project, dashboard = scaffold_from_folder(folder, root)
    proj_path = root / "project.yaml"
    dash_dir = root / project["dashboards_dir"]
    dash_path = dash_dir / "overview.yaml"
    if proj_path.exists() and not force:
        raise FileExistsError(f"{proj_path} exists; pass force=True to overwrite")
    dash_dir.mkdir(parents=True, exist_ok=True)
    proj_path.write_text(_dump(project), encoding="utf-8")
    dash_path.write_text(_dump(dashboard), encoding="utf-8")
    return proj_path


def _relpath(path: Path, root: Path) -> str:
    try:
        return path.resolve().relative_to(root.resolve()).as_posix()
    except ValueError:
        return path.as_posix()


def _dump(data: dict) -> str:
    return yaml.safe_dump(data, sort_keys=False, allow_unicode=True, width=100)
