"""Builders for the MinusAnalyst-style app shell + per-page content.

Orientation (matches MinusAnalyst): a left sidebar (brand + nav) and a wide main
canvas (topbar + global slicer bar + 12-col widget grid). Each KPI is a "page" in
the sidebar nav, plus an Overview page that shows every KPI's headline + lead
panel. The shell is built once; the page header, sidebar active state, and grid
are (re)built by the routing/cross-filter callback.

The chart for each panel is taken verbatim from the interns recommendation; this
module only places and renders. Display-redacted columns are dropped from panels
and slicers (governance parity).
"""
from __future__ import annotations

from typing import Any

import polars as pl
from dash import dcc, html

from core.dashboard.model.aggregate import resolve_column
from core.dashboard.model.crossfilter import panel_data
from core.dashboard.model.cuts import KpiModel
from core.dashboard.ui import widgets
from core.dashboard.ui.chart_map import is_big_number, map_chart_type
from core.dashboard.ui.governance import WorkspaceRedaction
from core.dashboard.ui.theme import chart_colors

OVERVIEW_ID = "overview"

# Tile widths (out of 12) by panel position; first panel is wider for emphasis.
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
            html.Div(id="sidebar", className="sidebar"),
            html.Div(
                className="main",
                children=[
                    html.Div(id="page-header"),
                    html.Div(id="slicer-bar", className="slicer-bar"),
                    html.Div(id="widgets", className="grid"),
                ],
            ),
        ],
    )


def build_sidebar(canvas, active_id: str) -> list:
    name = canvas.layout.project_root.name
    mark = (name or "M")[0].upper()
    items = [
        dcc.Link(
            className="nav-item active" if active_id == OVERVIEW_ID else "nav-item",
            href="/", children=[html.Span("Overview")],
        )
    ]
    for kid, model in canvas.kpis.items():
        items.append(dcc.Link(
            className="nav-item active" if kid == active_id else "nav-item",
            href=f"/{kid}",
            children=[html.Span(_nav_title(model.title))],
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
    return t if len(t) <= limit else t[: limit - 1].rstrip() + "…"


def page_header(title: str, sub: str = "") -> html.Div:
    bits = [html.H1(title, className="page-title")]
    if sub:
        bits.append(html.P(sub, className="page-sub"))
    return html.Div(className="topbar", children=[
        html.Div(className="topbar-left", children=[html.Div(bits)]),
    ])


# ---------------------------------------------------------------------------
# Slicer bar (global cross-filter)
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
    """Global slicers from the canvas's shared dimensions (>1 KPI). Redacted
    dimensions are never offered."""
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


# ---------------------------------------------------------------------------
# Tiles + pages
# ---------------------------------------------------------------------------


def _tile(body, *, span: int, title: str = "") -> html.Div:
    style = {"gridColumn": f"span {span}", "minHeight": "360px"}
    head = [html.Div([html.P(title, className="tile-title")], className="tile-head")] if title else []
    return html.Div(head + [body], className="tile", style=style)


def _headline(model: KpiModel, gold: pl.DataFrame, filters=None) -> float | None:
    mcol = resolve_column(model.measure, gold.columns)
    if mcol is None:
        return None
    frame = panel_data(model, gold, {"x": None, "y": model.measure}, filters=filters) \
        if filters else gold
    try:
        from core.dashboard.model.aggregate import apply_filters
        f = apply_filters(gold, filters) if filters else gold
        return float(f.get_column(mcol).sum())
    except Exception:
        return float(gold.get_column(mcol).sum())


def _panel_tile(model, gold, panel, span, theme, filters=None) -> html.Div:
    colors = chart_colors(theme)
    title = panel.get("title") or ""
    chart_type = str(panel.get("chart_type") or "bar")
    try:
        frame = panel_data(model, gold, panel, filters=filters)
    except Exception:
        frame = gold.head(0)
    renderer = map_chart_type(chart_type)
    fig = renderer(frame, panel, panel.get("y") or model.measure, colors)
    return _tile(widgets.graph(fig), span=span, title=title)


def _safe_panels(model, redaction):
    panels = [p for p in model.panels if p.get("x") and not is_big_number(p.get("chart_type"))]
    if redaction is not None:
        panels = [p for p in panels if redaction.panel_is_safe(p)]
    return panels


def kpi_page(model, gold, theme="claude", redaction=None, filters=None) -> list:
    """All tiles for one KPI: headline card (span 12) + its recommended panels."""
    tiles = [html.Div(
        widgets.kpi_card(model.title, _headline(model, gold, filters),
                         fmt=model.y_format or None),
        style={"gridColumn": "span 12"},
    )]
    for i, panel in enumerate(_safe_panels(model, redaction)):
        span = _WIDTHS[i] if i < len(_WIDTHS) else 6
        tiles.append(_panel_tile(model, gold, panel, span, theme, filters))
    return tiles


def overview_page(canvas, theme="claude", redaction=None, filters=None) -> list:
    """One headline card per KPI + that KPI's single lead panel."""
    tiles = []
    for kid, model in canvas.kpis.items():
        gold = canvas.gold[kid]
        tiles.append(html.Div(
            widgets.kpi_card(model.title, _headline(model, gold, filters),
                             fmt=model.y_format or None),
            style={"gridColumn": "span 4"},
        ))
    for kid, model in canvas.kpis.items():
        gold = canvas.gold[kid]
        panels = _safe_panels(model, redaction)
        if panels:
            tiles.append(_panel_tile(model, gold, panels[0], 6, theme, filters))
    if not tiles:
        return [html.Div("No gold KPI results found for this workspace.", className="empty")]
    return tiles


__all__ = [
    "OVERVIEW_ID", "app_shell", "build_sidebar", "kpi_page", "overview_page",
    "page_header", "slicer_bar",
]
