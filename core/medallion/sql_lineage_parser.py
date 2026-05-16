"""
core/medallion/sql_lineage_parser.py — sqlglot-based column-level lineage (P5).

Parses emitted *.duckdb.sql files to extract (target_column <- source_columns)
edges. Covers four projection cases: passthrough, function call, CASE, subquery.
On parse failure, emits a placeholder edge with transform_type="unparsed".
"""
from __future__ import annotations


from core.medallion.lineage import LineageEdge


def extract_edges_from_sql(
    sql: str,
    *,
    target_table: str,
    dialect: str = "duckdb",
) -> list[LineageEdge]:
    """Parse `sql` and return column-level LineageEdge list."""
    try:
        import sqlglot
        from sqlglot import expressions as exp
    except ImportError:
        return [_unparsed_edge(target_table, reason="sqlglot not installed")]

    try:
        tree = sqlglot.parse_one(sql, read=dialect)
    except Exception as exc:
        return [_unparsed_edge(target_table, reason=str(exc)[:200])]

    if tree is None:
        return []

    select = tree.find(exp.Select)
    if select is None:
        return []

    source_tables = _collect_source_tables(select, exp)
    edges: list[LineageEdge] = []

    for projection in select.expressions:
        try:
            target_col, source_refs, transform_type = _resolve_projection(
                projection, source_tables, exp
            )
            for src_table, src_cols in source_refs.items():
                if src_cols:
                    edges.append(LineageEdge(
                        from_node=src_table,
                        from_columns=src_cols,
                        to_node=target_table,
                        to_columns=[target_col] if target_col else [],
                        transform_type=transform_type,
                        reasoning=f"derived in {target_table} SELECT projection",
                    ))
        except Exception:
            continue

    return edges


# ── Internal helpers ───────────────────────────────────────────────────────────

def _collect_source_tables(select, exp) -> dict[str, str]:
    """Return {alias_or_name: qualified_name} for all tables in scope."""
    result: dict[str, str] = {}
    for table in select.find_all(exp.Table):
        alias = table.alias if table.alias else table.name
        qualified = f"{table.db}.{table.name}" if table.db else table.name
        if alias:
            result[alias] = qualified
    return result


def _resolve_projection(
    expr, source_tables: dict[str, str], exp
) -> tuple[str, dict[str, list[str]], str]:
    """
    Return (target_column_name, {source_table: [columns]}, transform_type).
    """
    if isinstance(expr, exp.Alias):
        target_name = expr.alias
        body = expr.this
    elif isinstance(expr, exp.Column):
        target_name = expr.name
        body = expr
    elif isinstance(expr, exp.Star):
        return ("*", {}, "passthrough")
    else:
        target_name = expr.sql()[:32]
        body = expr

    columns = list(body.find_all(exp.Column))
    refs: dict[str, list[str]] = {}
    for c in columns:
        table_alias = c.table or "<unaliased>"
        actual = source_tables.get(table_alias, table_alias)
        refs.setdefault(actual, []).append(c.name)

    if isinstance(body, exp.Column):
        return target_name, refs, "passthrough"
    if body.find(exp.Case):
        return target_name, refs, "conditional"
    if body.find(exp.Subquery):
        return target_name, refs, "subquery"
    funcs = list(body.find_all(exp.Func))
    if funcs:
        fn_name = funcs[0].key if hasattr(funcs[0], "key") else type(funcs[0]).__name__
        return target_name, refs, f"computed:{fn_name}"
    return target_name, refs, "computed"


def _unparsed_edge(target_table: str, *, reason: str) -> LineageEdge:
    return LineageEdge(
        from_node="<unknown>",
        from_columns=[],
        to_node=target_table,
        to_columns=[],
        transform_type="unparsed",
        reasoning=f"parse failed: {reason}",
    )
