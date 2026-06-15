"""Build the 12-column tile grid for a workspace from the live model.

One section per KPI; each KPI's recommended panels become tiles. The chart for
each panel is taken verbatim from the interns recommendation -- this module only
places and renders, it never re-picks a chart.
"""
from __future__ import annotations

from typing import Any

import polars as pl
from dash import dcc, html

from core.dashboard.model.crossfilter import panel_data
from core.dashboard.model.cuts import KpiModel
from core.dashboard.ui import widgets
from core.dashboard.ui.chart_map import is_big_number, map_chart_type
from core.dashboard.ui.theme import chart_colors

# Tile widths (out of 12) by panel position; first panel is wider.
_WIDTHS = [8, 4, 6, 6, 4]


def _tile(body, *, span: int, title: str = "") -> html.Div:
    style = {"gridColumn": f"span {span}", "minHeight": "340px"}
    head = [html.Div([html.P(title, className="tile-title")], className="tile-head")] if title else []
    return html.Div(head + [body], className="tile", style=style)


def _panel_tile(model: KpiModel, gold: pl.DataFrame, panel: dict[str, Any],
                span: int, theme: str) -> html.Div:
    colors = chart_colors(theme)
    title = panel.get("title") or ""
    chart_type = str(panel.get("chart_type") or "bar")
    try:
        frame = panel_data(model, gold, panel)
    except Exception:
        frame = gold.head(0)

    if is_big_number(chart_type) or not panel.get("x"):
        measure = panel.get("y") or model.measure
        from core.dashboard.model.aggregate import resolve_column
        mcol = resolve_column(measure, gold.columns)
        total = float(gold.get_column(mcol).sum()) if mcol else None
        return widgets.kpi_card(title or model.title, total, fmt=panel.get("y_format"))

    renderer = map_chart_type(chart_type)
    fig = renderer(frame, panel, panel.get("y") or model.measure, colors)
    return _tile(widgets.graph(fig), span=span, title=title)


def kpi_section(model: KpiModel, gold: pl.DataFrame, theme: str = "claude") -> html.Div:
    """A titled section: KPI headline card + its recommended panel tiles."""
    from core.dashboard.model.aggregate import resolve_column
    mcol = resolve_column(model.measure, gold.columns)
    headline = float(gold.get_column(mcol).sum()) if mcol else None

    tiles = [
        html.Div(
            widgets.kpi_card(model.title, headline, fmt=model.y_format or None),
            style={"gridColumn": "span 12"},
        )
    ]
    panels = [p for p in model.panels if p.get("x")]
    for i, panel in enumerate(panels):
        span = _WIDTHS[i] if i < len(_WIDTHS) else 6
        tiles.append(_panel_tile(model, gold, panel, span, theme))

    return html.Div([
        html.H2(model.title, className="page-title"),
        html.Div(tiles, className="grid"),
    ], className="kpi-section", id={"kind": "kpi-section", "kpi": model.kpi_id})


def _slicer(label: str, col: str, options: list[str]) -> html.Div:
    return html.Div([
        html.Label(label, className="slicer-label"),
        dcc.Dropdown(
            id={"kind": "slicer", "col": col},
            options=[{"label": str(o), "value": o} for o in options],
            multi=True, placeholder=f"All {label}", className="slicer-dd",
        ),
    ], className="slicer")


def slicer_bar(canvas) -> html.Div:
    """Page slicers from the canvas's shared dimensions (cross-filter the canvas).

    Only dimensions present in >1 KPI become global slicers; their distinct values
    are pooled across the KPIs that expose them.
    """
    shared = canvas.shared_dimensions()
    slicers = []
    for dim_lower, kpi_ids in sorted(shared.items()):
        if len(kpi_ids) < 2:
            continue  # single-KPI dims are sliced within their section, not globally
        # pool distinct values across KPIs exposing this dim
        values: set = set()
        col_name = dim_lower
        for kid in kpi_ids:
            gold = canvas.gold[kid]
            from core.dashboard.model.aggregate import resolve_column
            c = resolve_column(dim_lower, gold.columns)
            if c is None:
                continue
            col_name = c
            values.update(gold.get_column(c).cast(pl.Utf8).unique().to_list())
        if values:
            label = col_name.replace("_", " ").title()
            slicers.append(_slicer(label, col_name, sorted(v for v in values if v is not None)))
    return html.Div(slicers, className="slicer-bar")


__all__ = ["kpi_section", "slicer_bar"]
