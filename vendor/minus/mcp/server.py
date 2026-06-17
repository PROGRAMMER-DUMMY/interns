"""MinusAnalyst MCP server.

Wraps the read-only query + config layers as Model Context Protocol tools, so
any MCP client can list dashboards, inspect the semantic model/schema, enumerate
governed measures, and run a measure through the same :class:`QueryEngine` the
dashboard renders from.

The protocol-facing tools are thin wrappers around the plain ``_*`` functions in
this module; tests call those directly without a live MCP session.
"""

from __future__ import annotations

import math
from pathlib import Path
from typing import Any, Optional

from mcp.server.fastmcp import FastMCP

from minus.agent.bridge import field_catalog
from minus.config.loader import export_schema
from minus.config.models import Widget
from minus.render.app import AppState

# ---------------------------------------------------------------------------
# Cached per-root state
# ---------------------------------------------------------------------------

_STATES: dict[str, AppState] = {}


def get_state(root: Path | str = ".") -> AppState:
    """Return a cached :class:`AppState` for ``root`` (built on first use)."""
    key = str(Path(root).resolve())
    state = _STATES.get(key)
    if state is None:
        state = AppState(root)
        _STATES[key] = state
    return state


def _clean(value: Any) -> Any:
    """Make a value JSON-safe (NaN/inf -> None, numpy scalars -> python)."""
    if isinstance(value, float):
        return None if (math.isnan(value) or math.isinf(value)) else value
    if hasattr(value, "item"):  # numpy scalar
        try:
            return _clean(value.item())
        except (ValueError, TypeError):
            return value
    return value


def _records(frame) -> list[dict]:
    """Convert a DataFrame to a list of JSON-safe dict records."""
    out: list[dict] = []
    for row in frame.to_dicts():
        out.append({k: _clean(v) for k, v in row.items()})
    return out


# ---------------------------------------------------------------------------
# Tool implementations (plain functions — directly unit-testable)
# ---------------------------------------------------------------------------


def _list_dashboards(root: Path | str = ".") -> list[dict]:
    """List the loaded dashboard pages."""
    state = get_state(root)
    return [
        {"id": p.id, "title": p.title, "widget_count": len(p.widgets)}
        for p in state.pages
    ]


def _describe_model(root: Path | str = ".") -> str:
    """Return the field/measure catalog (tables, columns, measures, relationships)."""
    state = get_state(root)
    return field_catalog(state.project, state.model)


def _get_schema(root: Path | str = ".") -> dict:
    """Return the project/dashboard JSON Schema (the config contract)."""
    return export_schema()


def _list_measures(root: Path | str = ".") -> list[dict]:
    """List the governed measures defined on the project."""
    state = get_state(root)
    out: list[dict] = []
    for m in state.project.measures:
        out.append(
            {
                "name": m.name,
                "label": m.label,
                "kind": m.kind or m.agg,
                "fmt": m.fmt,
            }
        )
    return out


def _query_metric(
    measure: str,
    dimension: Optional[str] = None,
    filters: Optional[dict] = None,
    limit: Optional[int] = None,
    root: Path | str = ".",
) -> dict:
    """Run a measure through the QueryEngine via a transient widget.

    Returns ``{"measure", "dimension", "scalar"|"rows", ...}`` on success or
    ``{"error": "..."}`` on failure (never raises, so the server stays up).
    """
    try:
        state = get_state(root)
        widget = Widget(
            id="__mcp_query__",
            type="bar" if dimension else "kpi",
            measure=measure,
            dimension=dimension,
            limit=limit,
        )
        result = state.engine.run(widget, filters or {})
        if dimension:
            return {
                "measure": measure,
                "dimension": dimension,
                "rows": _records(result.frame),
            }
        return {"measure": measure, "scalar": _clean(result.scalar)}
    except Exception as exc:  # keep the server alive on bad input
        return {"error": f"{type(exc).__name__}: {exc}"}


# ---------------------------------------------------------------------------
# Server factory
# ---------------------------------------------------------------------------


def build_server(root: Path | str = ".") -> FastMCP:
    """Build a :class:`FastMCP` server bound to the project at ``root``."""
    # Warm (and validate) the project up front so config errors surface now.
    get_state(root)
    server = FastMCP("MinusAnalyst")

    @server.tool()
    def list_dashboards() -> list[dict]:
        """List the report's dashboard pages (id, title, widget count)."""
        return _list_dashboards(root)

    @server.tool()
    def describe_model() -> str:
        """Describe the semantic model: tables, columns, measures, relationships."""
        return _describe_model(root)

    @server.tool()
    def get_schema() -> dict:
        """Return the JSON Schema for the project/dashboard config."""
        return _get_schema(root)

    @server.tool()
    def list_measures() -> list[dict]:
        """List the governed measures (name, label, kind/agg, fmt)."""
        return _list_measures(root)

    @server.tool()
    def query_metric(
        measure: str,
        dimension: Optional[str] = None,
        filters: Optional[dict] = None,
        limit: Optional[int] = None,
    ) -> dict:
        """Query a governed measure, optionally grouped by a 'table.column' dimension.

        Without a dimension a scalar (KPI) value is returned; with one, the
        result rows are returned. ``filters`` is a ``{field: value}`` mapping.
        """
        return _query_metric(measure, dimension, filters, limit, root)

    return server


__all__ = [
    "build_server",
    "get_state",
    "_list_dashboards",
    "_describe_model",
    "_get_schema",
    "_list_measures",
    "_query_metric",
]
