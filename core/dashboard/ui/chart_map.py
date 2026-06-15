"""Map interns chart_type names -> a widget renderer.

The interns recommendation engine (`core.dashboard.profile` / `chart_knowledge`)
emits these chart_types. Every one must map to a renderer so the UI never raises
on a recommended chart. Where MinusAnalyst has no native equivalent, we render a
faithful Plotly version (treemap, lollipop, histogram, bubble_map) or document
the nearest fallback. ``ranked_bar`` is a bar whose top-N ordering is already
applied upstream by `panel_data`.
"""
from __future__ import annotations

from core.dashboard.ui import widgets

# chart_type -> (renderer, note). big_number is handled separately (KPI card).
CHART_RENDERERS = {
    "bar": (widgets.render_bar, "vertical bar"),
    "ranked_bar": (widgets.render_hbar, "horizontal bar, top-N pre-applied upstream"),
    "grouped_bar": (widgets.render_grouped_bar, "grouped bar (color = series)"),
    "stacked_bar_percent": (widgets.render_stacked_bar_percent, "100% stacked bar"),
    "line": (widgets.render_line, "line w/ markers"),
    "stacked_area": (widgets.render_area, "stacked area"),
    "donut": (widgets.render_donut, "donut w/ center total"),
    "pie": (widgets.render_pie, "pie"),
    "treemap": (widgets.render_treemap, "native plotly treemap"),
    "heatmap": (widgets.render_heatmap, "heatmap (two categoricals) -> bar if no color"),
    "lollipop": (widgets.render_lollipop, "native stems+dots"),
    "scatter": (widgets.render_scatter, "scatter"),
    "histogram": (widgets.render_histogram, "native histogram of the measure"),
    "bubble_map": (widgets.render_bubble_map, "geo bubble if lat/lon else bar fallback"),
}

# Charts handled outside the figure renderers.
_NON_FIGURE = {"big_number"}


def map_chart_type(chart_type: str):
    """Return the renderer fn for a chart_type, defaulting to bar.

    big_number returns None (the caller renders a KPI card instead).
    """
    ct = (chart_type or "bar").lower()
    if ct in _NON_FIGURE:
        return None
    entry = CHART_RENDERERS.get(ct)
    if entry is None:
        return widgets.render_bar  # safe default; never raise on an unknown type
    return entry[0]


def is_big_number(chart_type: str) -> bool:
    return (chart_type or "").lower() == "big_number"


__all__ = ["CHART_RENDERERS", "is_big_number", "map_chart_type"]
