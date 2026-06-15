"""The true Power BI canvas: one conformed model, measure x any dimension, with
global cross-filter across every visual on the page.

Unlike the per-KPI gold pages (frozen cuts), this page aggregates the conformed
star live, so every chart shares ONE fact -- clicking any bar refilters the whole
canvas, and you can slice a measure by any dimension. KPI cards show each measure's
grand total under the active filters.

Chart type per dimension is chosen from cardinality (data-to-viz): month -> line,
few categories -> donut, more -> bar, many -> ranked top-N bar.
"""
from __future__ import annotations

from typing import Any

import polars as pl
from dash import dcc, html

from core.dashboard.model.aggregate import apply_filters, resolve_column
from core.dashboard.model.conformed import ConformedModel, aggregate_measure
from core.dashboard.ui import widgets
from core.dashboard.ui.chart_map import map_chart_type
from core.dashboard.ui.governance import WorkspaceRedaction
from core.dashboard.ui.theme import chart_colors

_WIDTHS = [6, 6, 4, 4, 4, 6, 6]
_DATE_ISH = ("date", "_at", "_dt")


def _humanize(name: str) -> str:
    return str(name).replace("_", " ").replace(".", " ").strip().title()


def primary_measure(model: ConformedModel) -> str:
    for name, m in model.measures.items():
        if m.fmt == "currency":
            return name
    return next(iter(model.measures), "")


def _display_dimensions(model: ConformedModel) -> list[str]:
    """Dimensions worth charting: skip raw date columns (keep derived 'month'),
    skip near-unique and constant columns."""
    out = []
    n = model.frame.height or 1
    for d in model.dimensions:
        dl = d.lower()
        if dl != "month" and any(t in dl for t in _DATE_ISH):
            continue
        distinct = model.frame.get_column(d).n_unique()
        if distinct <= 1 or distinct > max(60, n * 0.5):
            continue
        out.append(d)
    return out


def _chart_for(distinct: int, dim: str) -> dict[str, Any]:
    if dim.lower() == "month":
        return {"chart_type": "line"}
    if distinct <= 6:
        return {"chart_type": "donut"}
    if distinct <= 12:
        return {"chart_type": "bar"}
    return {"chart_type": "ranked_bar", "orientation": "h", "limit": 12}


def conformed_kpi_cards(model: ConformedModel, filters: dict[str, Any]) -> list:
    cards = []
    span = max(3, 12 // max(1, len(model.measures)))
    for name, m in model.measures.items():
        try:
            val = aggregate_measure(model, name, group_by=[], filters=filters)
            total = val.get_column(name).to_list()[0] if val.height else None
        except Exception:
            total = None
        cards.append(html.Div(
            widgets.kpi_card(name, total, fmt=m.fmt or None),
            style={"gridColumn": f"span {span}"},
        ))
    return cards


def _panel_tile(model, measure, dim, span, theme, click_filters, slicer_filters):
    colors = chart_colors(theme)
    distinct = model.frame.get_column(dim).n_unique()
    panel = {**_chart_for(distinct, dim), "x": dim, "y": measure,
             "title": f"{measure} by {_humanize(dim)}"}

    eff = {**(slicer_filters or {}), **(click_filters or {})}
    highlight = None
    if click_filters:
        for k, v in click_filters.items():
            if resolve_column(k, model.frame.columns) == resolve_column(dim, model.frame.columns):
                highlight = v
                eff.pop(k, None)
                break
    try:
        frame = aggregate_measure(model, measure, group_by=[dim], filters=eff)
        if panel.get("limit"):
            frame = frame.sort(measure, descending=True, nulls_last=True).head(panel["limit"])
    except Exception:
        frame = model.frame.head(0)
    if highlight is not None:
        panel["_highlight"] = highlight
    renderer = map_chart_type(panel["chart_type"])
    fig = renderer(frame, panel, measure, colors)
    gid = {"kind": "panel-graph", "kpi": "__conformed__", "x": dim,
           "orient": "pie" if panel["chart_type"] == "donut"
                     else ("h" if panel.get("orientation") == "h" else "v")}
    style = {"gridColumn": f"span {span}", "minHeight": "360px"}
    head = [html.Div([html.P(panel["title"], className="tile-title")], className="tile-head")]
    return html.Div(head + [widgets.graph(fig, id=gid)], className="tile", style=style)


def conformed_canvas(model: ConformedModel, theme="claude", redaction=None,
                     slicer_filters=None, click_filters=None) -> list:
    measure = primary_measure(model)
    tiles = conformed_kpi_cards(model, {**(slicer_filters or {}), **(click_filters or {})})
    dims = _display_dimensions(model)
    if redaction is not None:
        dims = [d for d in dims if not redaction.is_redacted(d)]
    for i, dim in enumerate(dims):
        span = _WIDTHS[i] if i < len(_WIDTHS) else 6
        tiles.append(_panel_tile(model, measure, dim, span, theme,
                                 click_filters, slicer_filters))
    return tiles


def conformed_slicers(model: ConformedModel, redaction=None) -> list:
    slicers = []
    for dim in _display_dimensions(model):
        if redaction is not None and redaction.is_redacted(dim):
            continue
        distinct = model.frame.get_column(dim).n_unique()
        if distinct > 40:
            continue  # too many for a dropdown; use the chart click instead
        vals = sorted(v for v in model.frame.get_column(dim).cast(pl.Utf8).unique().to_list()
                      if v is not None)
        slicers.append(html.Div([
            html.Label(_humanize(dim), className="slicer-label"),
            dcc.Dropdown(id={"kind": "slicer", "col": dim},
                         options=[{"label": str(v), "value": v} for v in vals],
                         multi=True, placeholder=f"All {_humanize(dim)}"),
        ], className="slicer"))
    return slicers


__all__ = ["conformed_canvas", "conformed_kpi_cards", "conformed_slicers",
           "primary_measure"]
