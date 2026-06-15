"""Builders for the MinusAnalyst-style app shell + per-page content.

Orientation (matches MinusAnalyst): a left sidebar (brand + nav) and a wide main
canvas (topbar + global slicer bar + 12-col widget grid). Each KPI is a "page" in
the sidebar nav, plus an Overview page. The shell is built once; the page header,
sidebar, slicers, chips, and grid are (re)built by the routing/cross-filter
callback.

Interactivity (Power BI-style):
- dropdown slicers cross-filter the whole canvas;
- CLICKING a bar/slice adds a click-filter on that chart's dimension (global,
  applied to every KPI exposing the column) and shows a removable chip;
- the clicked chart cross-highlights (keeps all marks, dims the non-selected)
  rather than collapsing to one value.

Chart choice is taken verbatim from the interns recommendation. Display-redacted
columns are dropped from panels and slicers (governance parity).
"""
from __future__ import annotations

from typing import Any

import polars as pl
from dash import dcc, html

from core.dashboard.model.aggregate import apply_filters, resolve_column
from core.dashboard.model.crossfilter import panel_data
from core.dashboard.model.cuts import KpiModel
from core.dashboard.ui import widgets
from core.dashboard.ui.chart_map import is_big_number, map_chart_type
from core.dashboard.ui.governance import WorkspaceRedaction
from core.dashboard.ui.theme import chart_colors

OVERVIEW_ID = "overview"
_WIDTHS = [8, 4, 6, 6, 4]


# ---------------------------------------------------------------------------
# Static outer shell
# ---------------------------------------------------------------------------


def app_shell(workspace_name: str) -> html.Div:
    return html.Div(
        id="app-root",
        className="app",
        children=[
            dcc.Location(id="url", refresh=False),
            dcc.Store(id="click-filters", data={}),
            html.Div(id="sidebar", className="sidebar"),
            html.Div(
                className="main",
                children=[
                    html.Div(id="page-header"),
                    html.Div(className="slicer-bar", children=[
                        html.Div(id="slicer-controls", className="slicer-group"),
                        html.Div(id="crossfilter-chips", className="slicer-group"),
                    ]),
                    html.Div(id="widgets", className="grid"),
                ],
            ),
        ],
    )


def build_sidebar(canvas, active_id: str) -> list:
    name = canvas.layout.project_root.name
    mark = (name or "M")[0].upper()
    items = [dcc.Link(
        className="nav-item active" if active_id == OVERVIEW_ID else "nav-item",
        href="/", children=[html.Span("Overview")],
    )]
    for kid, model in canvas.kpis.items():
        items.append(dcc.Link(
            className="nav-item active" if kid == active_id else "nav-item",
            href=f"/{kid}", children=[html.Span(_nav_title(model.title))],
        ))
    return [
        html.Div(className="brand", children=[
            html.Div(mark, className="brand-mark"),
            html.Span(name, className="brand-name"),
        ]),
        html.Div("KPIs", className="nav-label"),
        *items,
        html.Div("Live - validated gold layer", className="sidebar-foot"),
    ]


def _nav_title(title: str, limit: int = 46) -> str:
    t = (title or "").strip()
    return t if len(t) <= limit else t[: limit - 1].rstrip() + "..."


def page_header(title: str, sub: str = "") -> html.Div:
    bits = [html.H1(title, className="page-title")]
    if sub:
        bits.append(html.P(sub, className="page-sub"))
    return html.Div(className="topbar", children=[
        html.Div(className="topbar-left", children=[html.Div(bits)]),
        html.Div(className="topbar-actions", children=[
            html.Button("Reset filters", id="reset-filters", className="btn btn-ghost",
                        n_clicks=0),
        ]),
    ])


# ---------------------------------------------------------------------------
# Slicer bar + cross-filter chips
# ---------------------------------------------------------------------------


def _slicer(label: str, col: str, options: list[str]) -> html.Div:
    return html.Div([
        html.Label(label, className="slicer-label"),
        dcc.Dropdown(
            id={"kind": "slicer", "col": col},
            options=[{"label": str(o), "value": o} for o in options],
            multi=True, placeholder=f"All {label}",
        ),
    ], className="slicer")


def slicer_bar(canvas, redaction: WorkspaceRedaction | None = None) -> list:
    shared = canvas.shared_dimensions()
    slicers = []
    for dim_lower, kpi_ids in sorted(shared.items()):
        if len(kpi_ids) < 2:
            continue
        if redaction is not None and redaction.is_redacted(dim_lower):
            continue
        values: set = set()
        col_name = dim_lower
        for kid in kpi_ids:
            gold = canvas.gold[kid]
            c = resolve_column(dim_lower, gold.columns)
            if c is None:
                continue
            col_name = c
            values.update(gold.get_column(c).cast(pl.Utf8).unique().to_list())
        if values:
            label = col_name.replace("_", " ").title()
            slicers.append(_slicer(label, col_name,
                                   sorted(v for v in values if v is not None)))
    return slicers


def crossfilter_chips(click_filters: dict[str, Any]) -> list:
    chips = []
    for col, val in (click_filters or {}).items():
        label = col.replace("_", " ").title()
        chips.append(html.Div(className="crossfilter-chip", children=[
            html.Span(f"{label}: {val}"),
            html.Span("x", className="x", id={"kind": "chip-remove", "col": col}),
        ]))
    return chips


# ---------------------------------------------------------------------------
# Tiles + pages
# ---------------------------------------------------------------------------


def _tile(body, *, span: int, title: str = "") -> html.Div:
    style = {"gridColumn": f"span {span}", "minHeight": "360px"}
    head = [html.Div([html.P(title, className="tile-title")], className="tile-head")] if title else []
    return html.Div(head + [body], className="tile", style=style)


def _orient(chart_type: str) -> str:
    ct = (chart_type or "").lower()
    if ct in ("donut", "pie"):
        return "pie"
    if ct == "ranked_bar" or ct == "hbar":
        return "h"
    return "v"


def _combined(slicer_filters, click_filters) -> dict:
    eff = dict(slicer_filters or {})
    eff.update(click_filters or {})
    return eff


def _headline(model, gold, slicer_filters, click_filters) -> float | None:
    mcol = resolve_column(model.measure, gold.columns)
    if mcol is None:
        return None
    try:
        f = apply_filters(gold, _combined(slicer_filters, click_filters))
        return float(f.get_column(mcol).sum())
    except Exception:
        return float(gold.get_column(mcol).sum())


def _panel_tile(model, gold, panel, span, theme, kpi_id,
                slicer_filters, click_filters) -> html.Div:
    colors = chart_colors(theme)
    title = panel.get("title") or ""
    chart_type = str(panel.get("chart_type") or "bar")
    xcol = panel.get("x")
    xres = resolve_column(xcol, gold.columns) if xcol else None

    # If this panel's own dimension carries a click-filter, don't self-filter it:
    # show all categories and HIGHLIGHT the selection instead.
    eff = _combined(slicer_filters, click_filters)
    highlight = None
    if xres and click_filters:
        for k, v in click_filters.items():
            if resolve_column(k, gold.columns) == xres:
                highlight = v
                eff.pop(k, None)
                break

    p = dict(panel)
    if highlight is not None:
        p["_highlight"] = highlight
    try:
        frame = panel_data(model, gold, p, filters=eff)
    except Exception:
        frame = gold.head(0)
    renderer = map_chart_type(chart_type)
    fig = renderer(frame, p, p.get("y") or model.measure, colors)
    gid = {"kind": "panel-graph", "kpi": kpi_id, "x": xres or "",
           "orient": _orient(chart_type)}
    return _tile(widgets.graph(fig, id=gid), span=span, title=title)


def _safe_panels(model, redaction):
    panels = [p for p in model.panels if p.get("x") and not is_big_number(p.get("chart_type"))]
    if redaction is not None:
        panels = [p for p in panels if redaction.panel_is_safe(p)]
    return panels


def kpi_page(model, gold, theme="claude", redaction=None,
             slicer_filters=None, click_filters=None) -> list:
    tiles = [html.Div(
        widgets.kpi_card(model.title, _headline(model, gold, slicer_filters, click_filters),
                         fmt=model.y_format or None),
        style={"gridColumn": "span 12"},
    )]
    for i, panel in enumerate(_safe_panels(model, redaction)):
        span = _WIDTHS[i] if i < len(_WIDTHS) else 6
        tiles.append(_panel_tile(model, gold, panel, span, theme, model.kpi_id,
                                 slicer_filters, click_filters))
    return tiles


def overview_page(canvas, theme="claude", redaction=None,
                  slicer_filters=None, click_filters=None) -> list:
    tiles = []
    for kid, model in canvas.kpis.items():
        gold = canvas.gold[kid]
        tiles.append(html.Div(
            widgets.kpi_card(model.title,
                             _headline(model, gold, slicer_filters, click_filters),
                             fmt=model.y_format or None),
            style={"gridColumn": "span 4"},
        ))
    for kid, model in canvas.kpis.items():
        panels = _safe_panels(model, redaction)
        if panels:
            tiles.append(_panel_tile(model, canvas.gold[kid], panels[0], 6, theme, kid,
                                     slicer_filters, click_filters))
    if not tiles:
        return [html.Div("No gold KPI results found for this workspace.", className="empty")]
    return tiles


__all__ = [
    "OVERVIEW_ID", "app_shell", "build_sidebar", "crossfilter_chips", "kpi_page",
    "overview_page", "page_header", "slicer_bar",
]
