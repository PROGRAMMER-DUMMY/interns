"""
core/medallion/spark_lineage_parser.py — AST walker for generated *.spark.py files (P5).

Covers the three shapes our P2 emitter generates:
  - .withColumn("name", <expr>) calls
  - .select(...) calls
  - DeltaTable.merge(...) calls

Does NOT try to be a general PySpark linter.
"""
from __future__ import annotations

import ast
import re
from typing import Optional

from core.medallion.lineage import LineageEdge


def extract_edges_from_spark_py(
    source: str,
    *,
    target_table: str,
) -> list[LineageEdge]:
    """Walk a generated *.spark.py file and return column-level edges."""
    try:
        tree = ast.parse(source)
    except SyntaxError as exc:
        return [LineageEdge(
            from_node="<unknown>", from_columns=[],
            to_node=target_table, to_columns=[],
            transform_type="unparsed", reasoning=f"syntax error: {exc}",
        )]

    source_table = _infer_source_table(source)
    edges: list[LineageEdge] = []

    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            edge = _handle_call(node, target_table, source_table)
            if edge:
                edges.append(edge)

    return edges


# ── AST call handlers ──────────────────────────────────────────────────────────

def _handle_call(
    node: ast.Call,
    target_table: str,
    source_table: str,
) -> Optional[LineageEdge]:
    name = _call_name(node)
    if name == "withColumn":
        return _handle_with_column(node, target_table, source_table)
    if name == "select":
        return _handle_select(node, target_table, source_table)
    if name in ("merge", "whenMatchedUpdateAll", "whenNotMatchedInsertAll"):
        return _handle_merge(node, target_table, source_table)
    return None


def _handle_with_column(
    node: ast.Call, target_table: str, source_table: str
) -> Optional[LineageEdge]:
    if len(node.args) < 2:
        return None
    col_name = _const_string(node.args[0])
    if col_name is None:
        return None
    source_cols = _extract_col_refs(node.args[1])
    return LineageEdge(
        from_node=source_table,
        from_columns=source_cols,
        to_node=target_table,
        to_columns=[col_name],
        transform_type="computed:withColumn",
        reasoning=f"withColumn in {target_table}",
    )


def _handle_select(
    node: ast.Call, target_table: str, source_table: str
) -> Optional[LineageEdge]:
    cols: list[str] = []
    for arg in node.args:
        s = _const_string(arg)
        if s:
            cols.append(s)
    if not cols:
        return None
    return LineageEdge(
        from_node=source_table,
        from_columns=cols,
        to_node=target_table,
        to_columns=cols,
        transform_type="passthrough:select",
        reasoning=f"select in {target_table}",
    )


def _handle_merge(
    node: ast.Call, target_table: str, source_table: str
) -> Optional[LineageEdge]:
    return LineageEdge(
        from_node=source_table,
        from_columns=[],
        to_node=target_table,
        to_columns=[],
        transform_type="merge:delta",
        reasoning=f"DeltaTable.merge in {target_table}",
    )


# ── Helpers ────────────────────────────────────────────────────────────────────

def _call_name(node: ast.Call) -> str:
    if isinstance(node.func, ast.Attribute):
        return node.func.attr
    if isinstance(node.func, ast.Name):
        return node.func.id
    return ""


def _const_string(node: ast.expr) -> Optional[str]:
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    return None


def _extract_col_refs(node: ast.expr) -> list[str]:
    """Extract string column references from an F.col(...) call tree."""
    cols: list[str] = []
    for child in ast.walk(node):
        if isinstance(child, ast.Call):
            fname = _call_name(child)
            if fname == "col" and child.args:
                s = _const_string(child.args[0])
                if s:
                    cols.append(s)
    return cols


def _infer_source_table(source: str) -> str:
    """Try to extract the SOURCE table name from the script."""
    m = re.search(r'spark\.table\(["\']([^"\']+)["\']\)', source)
    if m:
        return m.group(1)
    m = re.search(r'SOURCES\s*=\s*\[\s*\(["\']([^"\']+)', source)
    if m:
        return m.group(1)
    return "<source_unknown>"
