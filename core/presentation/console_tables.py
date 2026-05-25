from __future__ import annotations

from collections.abc import Iterable, Sequence
from typing import Any


def render_markdown_table(columns: Sequence[str], rows: Iterable[Sequence[Any]]) -> str:
    """Render query results as a compact Markdown table."""
    headers = [str(column) for column in columns]
    body = [[_format_cell(value) for value in row] for row in rows]
    widths = [
        max(len(headers[idx]), *(len(row[idx]) for row in body)) if body else len(headers[idx])
        for idx in range(len(headers))
    ]
    header_line = "| " + " | ".join(_pad(headers[idx], widths[idx]) for idx in range(len(headers))) + " |"
    separator = "| " + " | ".join("-" * widths[idx] for idx in range(len(headers))) + " |"
    data_lines = [
        "| " + " | ".join(_pad(row[idx], widths[idx]) for idx in range(len(headers))) + " |"
        for row in body
    ]
    return "\n".join([header_line, separator, *data_lines])


def render_query_result_table(cursor, *, limit: int | None = None) -> str:
    """Render a DB-API cursor result as a Markdown table."""
    columns = [str(description[0]) for description in cursor.description or []]
    rows = cursor.fetchmany(limit) if limit else cursor.fetchall()
    if not columns:
        return "(query returned no tabular result)"
    if not rows:
        return render_markdown_table(columns, []) + "\n\n(no rows)"
    suffix = ""
    if limit and len(rows) == limit:
        suffix = f"\n\n(showing first {limit} rows)"
    return render_markdown_table(columns, rows) + suffix


def _format_cell(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, float):
        return f"{value:.2f}".rstrip("0").rstrip(".")
    return str(value).replace("\r", " ").replace("\n", " ")


def _pad(value: str, width: int) -> str:
    return value + (" " * max(0, width - len(value)))
