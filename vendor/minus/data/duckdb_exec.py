"""DuckDB pushdown executor for the common grouped/scalar aggregate case.

The pandas path in :mod:`minus.query.engine` loads whole tables and does the
join + group-by + filtering in pandas. For large fact tables that is wasteful.
This module pushes that work down to DuckDB: it registers each needed table's
pandas DataFrame as a DuckDB view, builds a single SQL statement that LEFT-joins
the dimension tables onto the base (fact) table along the project relationships,
applies the active filters as a WHERE clause, groups by the widget dimensions,
and computes the aggregate measures with SQL aggregate functions.

The result is a pandas DataFrame shaped *exactly* like what
``QueryEngine._grouped_result`` / ``_scalar_result`` produce: dimension columns
named ``"table.column"`` and measure columns named after the measure.

Only the "common case" is handled here — plain aggregate measures over plain
columns with the filter forms the pandas path supports. Anything else raises
:class:`PushdownUnsupported`, and the engine falls back to pandas. Any DuckDB
error also propagates so the engine can fall back.
"""

from __future__ import annotations

import os
import threading
from contextlib import contextmanager
from typing import Any, Optional

import polars as pl

from minus.config.models import Measure, Relationship
from minus.data.model import SemanticModel, split_field

# Aggregations we can express as plain SQL. Mirrors the pandas semantics in
# minus.query.measures.aggregate. ``count`` / ``count_distinct`` are special.
_SQL_AGG = {
    "sum": "SUM",
    "avg": "AVG",
    "min": "MIN",
    "max": "MAX",
    "median": "MEDIAN",
}
_SUPPORTED_AGGS = set(_SQL_AGG) | {"count", "count_distinct"}


class PushdownUnsupported(Exception):
    """Raised when a widget/query cannot be expressed by the DuckDB path."""


def _query_timeout() -> float:
    """Seconds before a runaway pushdown query is interrupted so it can't hold
    the shared connection lock forever. 0 (or unset to the default) disables it;
    override with the ``MINUS_QUERY_TIMEOUT`` environment variable."""
    try:
        return float(os.environ.get("MINUS_QUERY_TIMEOUT", "30"))
    except ValueError:
        return 30.0


@contextmanager
def _interrupt_after(con, seconds: float):
    """Interrupt ``con``'s in-flight query after ``seconds`` (a watchdog timer).
    DuckDB raises on interrupt, so the engine falls back to pandas and, crucially,
    the connection lock is released. A non-positive timeout is a no-op."""
    if not seconds or seconds <= 0:
        yield
        return
    timer = threading.Timer(seconds, con.interrupt)
    timer.daemon = True
    timer.start()
    try:
        yield
    finally:
        timer.cancel()


def _quote_ident(name: str) -> str:
    """Quote a SQL identifier (table or alias) — double-quote, escape quotes."""
    return '"' + name.replace('"', '""') + '"'


def _col_ref(field: str) -> str:
    """'claims.ClaimAmount' -> "claims"."ClaimAmount" (qualified, quoted)."""
    table, col = split_field(field)
    return f"{_quote_ident(table)}.{_quote_ident(col)}"


def _alias(field: str) -> str:
    """The output column name for a dimension: kept as 'table.column'."""
    return _quote_ident(field)


def _sql_literal(value: Any) -> str:
    """Render a Python scalar as a SQL literal."""
    if value is None:
        return "NULL"
    if isinstance(value, bool):
        return "TRUE" if value else "FALSE"
    if isinstance(value, (int, float)):
        return repr(value)
    # Dates / timestamps / everything else -> string literal (single-quote escaped).
    s = str(value)
    return "'" + s.replace("'", "''") + "'"


def supported(
    model: SemanticModel,
    measures: list[Measure],
    agg_fields: dict[str, Optional[str]],
    dims: list[str],
    filters: dict[str, Any],
) -> bool:
    """Return True if this query is expressible by the DuckDB pushdown path.

    The engine still wraps the actual execution in try/except, so this is a
    fast pre-check, not a correctness guarantee.
    """
    # Derived/expression measures stay on pandas.
    for m in measures:
        if m.kind is not None:
            return False
    # Every leaf aggregate must be a plain SQL aggregate.
    for name in agg_fields:
        m = model.project.measure(name)
        if m.agg not in _SUPPORTED_AGGS:
            return False
        # count_distinct / sum / avg / ... (non-count) need a concrete field.
        if m.agg != "count" and not m.field:
            return False
    # Dimensions and filter keys must be plain 'table.column' refs.
    for ref in list(dims) + list(filters.keys()):
        if "." not in ref:
            return False
    return True


def _measure_sql(model: SemanticModel, name: str, field: Optional[str]) -> str:
    """SQL aggregate expression for a leaf measure, aliased to its name."""
    m = model.project.measure(name)
    alias = _quote_ident(name)
    if m.agg == "count":
        return f"COUNT(*) AS {alias}"
    if m.agg == "count_distinct":
        return f"COUNT(DISTINCT {_col_ref(field)}) AS {alias}"
    fn = _SQL_AGG[m.agg]
    return f"{fn}({_col_ref(field)}) AS {alias}"


def _filter_sql(field: str, val: Any) -> Optional[str]:
    """Build a WHERE clause fragment for one filter, mirroring _apply_filters.

    Returns None when the filter is inactive (empty / None), matching the
    pandas path which skips such filters.
    """
    if val is None or val == [] or val == "":
        return None
    col = _col_ref(field)
    if isinstance(val, dict) and any(k in val for k in
                                     ("from", "to", "ge", "le", "gt", "lt")):
        parts = []
        for key, op in (("from", ">="), ("ge", ">="), ("to", "<="), ("le", "<="),
                        ("gt", ">"), ("lt", "<")):
            if val.get(key) is not None:
                parts.append(f"{col} {op} {_sql_literal(val[key])}")
        if not parts:
            return None
        return " AND ".join(parts)
    if isinstance(val, (list, tuple)):
        items = ", ".join(_sql_literal(v) for v in val)
        return f"{col} IN ({items})"
    return f"{col} = {_sql_literal(val)}"


def _join_plan(
    model: SemanticModel, base: str, needed: set[str]
) -> list[tuple[str, Relationship]]:
    """Ordered list of (table_to_join, relationship) hops, base first excluded.

    Walks the same shortest-path graph SemanticModel.assemble uses, so the join
    topology matches the pandas path exactly.
    """
    included = {base}
    plan: list[tuple[str, Relationship]] = []
    for target in needed:
        if target in included:
            continue
        for src, dst, rel in model._path(base, target):
            if dst in included:
                continue
            plan.append((dst, rel))
            included.add(dst)
    return plan


def _join_on(rel: Relationship) -> str:
    left = _col_ref(f"{rel.from_table}.{rel.from_column}")
    right = _col_ref(f"{rel.to_table}.{rel.to_column}")
    return f"{left} = {right}"


def run_pushdown(
    model: SemanticModel,
    base: str,
    measures: list[Measure],
    agg_fields: dict[str, Optional[str]],
    dims: list[str],
    filters: dict[str, Any],
) -> pl.DataFrame:
    """Execute the join+group-by+filter in DuckDB; return a tidy DataFrame.

    Columns: one per dimension named 'table.column', plus one per leaf measure
    named after the measure. For the scalar (no-dims) case the frame has exactly
    one row. Raises on anything it cannot handle (caller falls back to pandas).
    """
    if not supported(model, measures, agg_fields, dims, filters):
        raise PushdownUnsupported("query not expressible as DuckDB pushdown")

    # Tables we must bring into the join: base + any referenced by dims, filters,
    # and measure fields.
    needed: set[str] = {base}
    for ref in list(dims) + list(filters.keys()):
        needed.add(split_field(ref)[0])
    for fld in agg_fields.values():
        if fld:
            needed.add(split_field(fld)[0])

    plan = _join_plan(model, base, needed)
    join_tables = [base] + [dst for dst, _ in plan]

    # Reuse the model's persistent connection (no per-query reconnect). The lock
    # serializes access -- a DuckDB connection is not safe for concurrent use --
    # and the timeout watchdog guarantees one query can't hold it indefinitely.
    con = model.duckdb()
    with model.duckdb_lock:
        # Bring each needed table into DuckDB under its logical name (original,
        # unprefixed columns). Prefer scanning the file IN PLACE (read_parquet/
        # read_csv_auto) so the join+group-by+filter pushes down to DuckDB
        # without materializing the whole table in Python -- the scalable path.
        # Fall back to registering the in-memory frame when no file scan exists.
        for tbl in join_tables:
            scan = model.scan_source(tbl)
            if scan:
                con.execute(f"CREATE OR REPLACE VIEW {_quote_ident(tbl)} AS "
                            f"SELECT * FROM {scan}")
            else:
                con.register(tbl, model.table_df(tbl))

        # SELECT list: dims (aliased to 'table.column') + measures.
        select_parts: list[str] = []
        for d in dims:
            select_parts.append(f"{_col_ref(d)} AS {_alias(d)}")
        for name, fld in agg_fields.items():
            select_parts.append(_measure_sql(model, name, fld))

        sql = f"SELECT {', '.join(select_parts)}\nFROM {_quote_ident(base)}"
        for dst, rel in plan:
            sql += f"\nLEFT JOIN {_quote_ident(dst)} ON {_join_on(rel)}"

        where_parts: list[str] = []
        for field, val in filters.items():
            frag = _filter_sql(field, val)
            if frag:
                where_parts.append(frag)
        if where_parts:
            sql += "\nWHERE " + " AND ".join(where_parts)

        if dims:
            group_terms = ", ".join(_col_ref(d) for d in dims)
            sql += f"\nGROUP BY {group_terms}"

        with _interrupt_after(con, _query_timeout()):
            return con.execute(sql).pl()
