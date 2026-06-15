"""Build the live MinusAnalyst-style Dash app for a workspace.

Sidebar (brand + KPI nav) + wide main canvas (topbar + global slicer bar +
12-col grid). One page per KPI plus an Overview. A single callback handles both
routing (URL -> active page + sidebar state) and cross-filtering (slicer values
-> refiltered grid), reading the validated gold layer via the Phase 1/2 model.

Binds 127.0.0.1 by design. Non-loopback binds require a token (Phase 4).
"""
from __future__ import annotations

from typing import Any

from dash import ALL, Dash, Input, Output, html

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
    if slug in canvas.kpis:
        return slug
    return L.OVERVIEW_ID


def _register_callbacks(app: Dash, canvas: CanvasModel, theme: str,
                        redaction: WorkspaceRedaction) -> None:

    @app.callback(
        Output("sidebar", "children"),
        Output("slicer-bar", "children"),
        Output("page-header", "children"),
        Output("widgets", "children"),
        Input("url", "pathname"),
        Input({"kind": "slicer", "col": ALL}, "value"),
        Input({"kind": "slicer", "col": ALL}, "id"),
    )
    def _render(pathname, slicer_values, slicer_ids):  # pragma: no cover - Dash runtime
        active = _active_id_from_path(pathname, canvas)
        filters: dict[str, Any] = {}
        for val, ident in zip(slicer_values or [], slicer_ids or []):
            if val:
                filters[ident["col"]] = val

        sidebar = L.build_sidebar(canvas, active)
        slicers = L.slicer_bar(canvas, redaction)

        if active == L.OVERVIEW_ID:
            header = L.page_header(
                canvas.layout.project_root.name,
                "Live KPI canvas - click a slicer to cross-filter the whole canvas.",
            )
            grid = L.overview_page(canvas, theme, redaction, filters)
        else:
            model = canvas.kpis[active]
            header = L.page_header(model.title, model.y_format and f"Measure: {model.measure}" or "")
            grid = L.kpi_page(model, canvas.gold[active], theme, redaction, filters)
        return sidebar, slicers, header, grid


__all__ = ["build_live_dashboard"]
