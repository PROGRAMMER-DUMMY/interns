"""Build the live MinusAnalyst-style Dash app for a workspace.

Sidebar (brand + KPI nav) + wide main canvas (topbar + slicer bar + 12-col grid).
One page per KPI plus an Overview.

Two callbacks:
1. _update_click_filters: a chart click toggles a click-filter on that chart's
   dimension; a chip's x removes it; Reset clears all. Stored in `click-filters`.
2. _render: URL -> active page + sidebar; slicer values + click-filters ->
   refiltered grid + chips. Reads the validated gold layer via the Phase 1/2 model.

Binds 127.0.0.1 by design. Non-loopback binds require a token (Phase 4).
"""
from __future__ import annotations

from typing import Any

from dash import ALL, Dash, Input, Output, State, callback_context, html

from core.dashboard.model.crossfilter import CanvasModel, load_canvas
from core.dashboard.ui import layout as L
from core.dashboard.ui.governance import WorkspaceRedaction
from core.dashboard.ui.theme import build_theme_css, index_string
from core.storage.workspace_layout import WorkspaceLayout


def build_live_dashboard(layout: WorkspaceLayout, *, theme: str = "claude") -> Dash:
    """Construct (do not run) the live dashboard Dash app for a workspace."""
    canvas = load_canvas(layout)
    redaction = WorkspaceRedaction(layout.project_root)
    app = Dash(__name__, title=f"{layout.project_root.name} - dashboard",
               suppress_callback_exceptions=True)
    app.index_string = index_string(build_theme_css(theme))
    app.layout = L.app_shell(layout.project_root.name)
    _register_callbacks(app, canvas, theme, redaction)
    return app


def _active_id_from_path(pathname: str, canvas: CanvasModel) -> str:
    slug = (pathname or "/").strip("/").lower()
    return slug if slug in canvas.kpis else L.OVERVIEW_ID


def _click_value(click_data: dict, orient: str):
    pts = (click_data or {}).get("points") or []
    if not pts:
        return None
    pt = pts[0]
    if orient == "pie":
        return pt.get("label")
    if orient == "h":
        return pt.get("y")
    return pt.get("x")


def _register_callbacks(app: Dash, canvas: CanvasModel, theme: str,
                        redaction: WorkspaceRedaction) -> None:

    @app.callback(
        Output("click-filters", "data"),
        Input({"kind": "panel-graph", "kpi": ALL, "x": ALL, "orient": ALL}, "clickData"),
        Input({"kind": "chip-remove", "col": ALL}, "n_clicks"),
        Input("reset-filters", "n_clicks"),
        State({"kind": "panel-graph", "kpi": ALL, "x": ALL, "orient": ALL}, "id"),
        State("click-filters", "data"),
        prevent_initial_call=True,
    )
    def _update_click_filters(click_datas, chip_clicks, reset, ids, current):  # pragma: no cover - Dash runtime
        current = dict(current or {})
        trig = callback_context.triggered_id
        if trig == "reset-filters":
            return {}
        if isinstance(trig, dict) and trig.get("kind") == "chip-remove":
            current.pop(trig.get("col"), None)
            return current
        if isinstance(trig, dict) and trig.get("kind") == "panel-graph":
            for cd, ident in zip(click_datas or [], ids or []):
                if ident == trig and cd:
                    col = ident.get("x")
                    val = _click_value(cd, ident.get("orient"))
                    if col and val is not None:
                        if current.get(col) == val:
                            current.pop(col, None)   # toggle off
                        else:
                            current[col] = val
                    break
        return current

    @app.callback(
        Output("sidebar", "children"),
        Output("slicer-controls", "children"),
        Output("crossfilter-chips", "children"),
        Output("page-header", "children"),
        Output("widgets", "children"),
        Input("url", "pathname"),
        Input({"kind": "slicer", "col": ALL}, "value"),
        Input({"kind": "slicer", "col": ALL}, "id"),
        Input("click-filters", "data"),
    )
    def _render(pathname, slicer_values, slicer_ids, click_filters):  # pragma: no cover - Dash runtime
        active = _active_id_from_path(pathname, canvas)
        slicer_filters: dict[str, Any] = {}
        for val, ident in zip(slicer_values or [], slicer_ids or []):
            if val:
                slicer_filters[ident["col"]] = val
        click_filters = dict(click_filters or {})

        sidebar = L.build_sidebar(canvas, active)
        slicers = L.slicer_bar(canvas, redaction)
        chips = L.crossfilter_chips(click_filters)

        if active == L.OVERVIEW_ID:
            header = L.page_header(
                canvas.layout.project_root.name,
                "Live KPI canvas - click any bar or slice to cross-filter the whole canvas.",
            )
            grid = L.overview_page(canvas, theme, redaction, slicer_filters, click_filters)
        else:
            model = canvas.kpis[active]
            header = L.page_header(model.title,
                                   f"Measure: {model.measure}" if model.measure else "")
            grid = L.kpi_page(model, canvas.gold[active], theme, redaction,
                              slicer_filters, click_filters)
        return sidebar, slicers, chips, header, grid


__all__ = ["build_live_dashboard"]
