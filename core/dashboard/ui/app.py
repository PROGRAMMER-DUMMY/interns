"""Build the live Power BI-style Dash app for a workspace.

Lists the gold KPIs, builds models + reads gold (Phase 1), renders the warm
MinusAnalyst theme with one section per KPI (Phase 3), and wires global slicers
to cross-filter the canvas (Phase 2 engine).

Binds 127.0.0.1 by design. Auth + PII redaction are Phase 4; do not bind a
non-loopback host here until that lands.
"""
from __future__ import annotations

from typing import Any

from dash import ALL, Dash, Input, Output, html

from core.dashboard.model.crossfilter import CanvasModel, load_canvas, panel_data
from core.dashboard.ui import layout as L
from core.dashboard.ui.chart_map import is_big_number, map_chart_type
from core.dashboard.ui.governance import WorkspaceRedaction
from core.dashboard.ui.theme import build_theme_css, chart_colors, index_string
from core.dashboard.ui.widgets import graph
from core.storage.workspace_layout import WorkspaceLayout


def _canvas_body(canvas: CanvasModel, theme: str, redaction: WorkspaceRedaction) -> list:
    if not canvas.kpis:
        return [html.Div("No gold KPI results found for this workspace.",
                         className="empty")]
    sections = [L.slicer_bar(canvas, redaction)]
    for kid, model in canvas.kpis.items():
        sections.append(L.kpi_section(model, canvas.gold[kid], theme, redaction))
    return sections


def build_live_dashboard(
    layout: WorkspaceLayout, *, theme: str = "claude"
) -> Dash:
    """Construct (do not run) the live dashboard Dash app for a workspace."""
    canvas = load_canvas(layout)
    redaction = WorkspaceRedaction(layout.project_root)
    app = Dash(__name__, title=f"{layout.project_root.name} - dashboard")
    app.index_string = index_string(build_theme_css(theme))

    app.layout = html.Div(
        className="app sidebar-off",
        children=[
            html.Div(  # main column
                className="main",
                children=[
                    html.Div(
                        html.Div([
                            html.H1(layout.project_root.name, className="page-title"),
                            html.P("Live KPI canvas - click a slicer to cross-filter.",
                                   className="page-sub"),
                        ], className="topbar-left"),
                        className="topbar",
                    ),
                    html.Div(_canvas_body(canvas, theme, redaction), id="canvas"),
                ],
            ),
        ],
    )

    _register_callbacks(app, canvas, theme, redaction)
    return app


def _register_callbacks(app: Dash, canvas: CanvasModel, theme: str,
                        redaction: WorkspaceRedaction) -> None:
    @app.callback(
        Output("canvas", "children"),
        Input({"kind": "slicer", "col": ALL}, "value"),
        Input({"kind": "slicer", "col": ALL}, "id"),
        prevent_initial_call=True,
    )
    def _cross_filter(values, ids):  # pragma: no cover - exercised via Dash runtime
        filters: dict[str, Any] = {}
        for val, ident in zip(values or [], ids or []):
            if val:
                filters[ident["col"]] = val
        body = [L.slicer_bar(canvas, redaction)]
        colors = chart_colors(theme)
        for kid, model in canvas.kpis.items():
            gold = canvas.gold[kid]
            tiles = []
            safe_panels = [p for p in model.panels
                           if p.get("x") and redaction.panel_is_safe(p)]
            for i, panel in enumerate(safe_panels):
                span = L._WIDTHS[i] if i < len(L._WIDTHS) else 6
                try:
                    frame = panel_data(model, gold, panel, filters=filters)
                except Exception:
                    frame = gold.head(0)
                ct = str(panel.get("chart_type") or "bar")
                if is_big_number(ct):
                    continue
                renderer = map_chart_type(ct)
                fig = renderer(frame, panel, panel.get("y") or model.measure, colors)
                tiles.append(L._tile(graph(fig), span=span, title=panel.get("title") or ""))
            body.append(html.Div([
                html.H2(model.title, className="page-title"),
                html.Div(tiles, className="grid"),
            ], className="kpi-section"))
        return body


__all__ = ["build_live_dashboard"]
