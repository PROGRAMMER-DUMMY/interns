"""Plotly/Dash widget renderers (ported + adapted from MinusAnalyst).

Each renderer takes a panel dict (the interns chart recommendation -- chart_type,
x, color, y, agg, limit, orientation, y_format, title) and an aggregated Polars
frame, and returns a Plotly Figure. The figure-building is self-contained: no
MinusAnalyst Project/Widget/Measure models, no interns renderer import.

A KPI big-number card and a conditional-format HTML table are also provided as
Dash components.
"""
from __future__ import annotations

from typing import Any

import plotly.express as px
import plotly.graph_objects as go
import polars as pl
from dash import dcc, html

from core.dashboard.model.aggregate import resolve_column
from core.dashboard.ui.format import format_value
from core.dashboard.ui.theme import PALETTE, chart_colors

_SEQ = {"color_discrete_sequence": PALETTE}


def _humanize(name: str) -> str:
    return str(name).replace("_", " ").replace(".", " ").strip().title()


def _xy(frame: pl.DataFrame, panel: dict[str, Any], measure: str):
    x = resolve_column(panel.get("x"), frame.columns)
    color = resolve_column(panel.get("color"), frame.columns)
    y = resolve_column(measure, frame.columns) or measure
    return x, y, color


def _style(fig, colors, *, is_pie=False, horizontal=False):
    margin = dict(l=8, r=66 if horizontal else 10, t=24, b=8)
    fig.update_layout(
        colorway=PALETTE,
        paper_bgcolor="rgba(0,0,0,0)",
        plot_bgcolor="rgba(0,0,0,0)",
        font=dict(family="Hanken Grotesk, ui-sans-serif, Segoe UI, sans-serif",
                  color=colors["muted"], size=12),
        margin=margin,
        showlegend=is_pie or bool(fig.data and len(fig.data) > 1),
        legend=dict(orientation="h", yanchor="bottom", y=-0.22, x=0, title_text=""),
    )
    fig.update_xaxes(showgrid=False, zeroline=False, automargin=True, title_text="",
                     tickfont=dict(size=11, color=colors["muted"]))
    fig.update_yaxes(showgrid=True, gridcolor=colors["grid"], zeroline=False,
                     automargin=True, title_text="",
                     tickfont=dict(size=11, color=colors["muted"]))


# ---------------------------------------------------------------------------
# Chart renderers (signature: (frame, panel, measure, colors) -> go.Figure)
# ---------------------------------------------------------------------------


def render_bar(frame, panel, measure, colors, *, horizontal=False):
    x, y, color = _xy(frame, panel, measure)
    horizontal = horizontal or panel.get("orientation") == "h"
    if horizontal:
        fig = px.bar(frame, y=x, x=y, color=color, orientation="h",
                     barmode="group", **_SEQ)
    else:
        fig = px.bar(frame, x=x, y=y, color=color, barmode="group", **_SEQ)
    if y and not color and frame.height:
        fmt = panel.get("y_format")
        texts = [format_value(v, fmt) for v in frame.get_column(y).to_list()]
        fig.update_traces(text=texts, textposition="outside", cliponaxis=False,
                          textfont=dict(size=10.5, color=colors["muted"]))
        if horizontal:
            fig.update_yaxes(autorange="reversed")
    _style(fig, colors, horizontal=horizontal)
    return fig


def render_hbar(frame, panel, measure, colors):
    return render_bar(frame, panel, measure, colors, horizontal=True)


def render_grouped_bar(frame, panel, measure, colors):
    return render_bar(frame, panel, measure, colors)


def render_stacked_bar_percent(frame, panel, measure, colors):
    x, y, color = _xy(frame, panel, measure)
    fig = px.bar(frame, x=x, y=y, color=color, barmode="stack", **_SEQ)
    fig.update_layout(barnorm="percent")
    _style(fig, colors)
    return fig


def render_line(frame, panel, measure, colors, *, area=False):
    x, y, color = _xy(frame, panel, measure)
    d = frame.sort(x) if x else frame
    fn = px.area if area else px.line
    fig = fn(d, x=x, y=y, color=color, markers=not area, **_SEQ)
    if panel.get("log_scale"):
        fig.update_yaxes(type="log")
    _style(fig, colors)
    return fig


def render_area(frame, panel, measure, colors):
    return render_line(frame, panel, measure, colors, area=True)


def render_donut(frame, panel, measure, colors, *, hole=0.55):
    x, y, _ = _xy(frame, panel, measure)
    fig = px.pie(frame, values=y, names=x, hole=hole, **_SEQ)
    fig.update_traces(textposition="inside", textinfo="percent")
    _style(fig, colors, is_pie=True)
    fig.update_layout(showlegend=True, legend=dict(
        orientation="h", y=-0.04, x=0.5, xanchor="center", yanchor="top",
        font=dict(size=10, color=colors["muted"]), title_text=""))
    if hole and y and frame.height:
        total = float(frame.get_column(y).sum())
        fig.add_annotation(
            x=0.5, y=0.56, xref="paper", yref="paper", showarrow=False, align="center",
            text=(f"<b>{format_value(total, panel.get('y_format'))}</b>"
                  f"<br><span style='font-size:9px'>{_humanize(y)}</span>"),
            font=dict(size=15, color=colors["ink"], family="Fraunces, Georgia, serif"))
    return fig


def render_pie(frame, panel, measure, colors):
    return render_donut(frame, panel, measure, colors, hole=0.0)


def render_treemap(frame, panel, measure, colors):
    x, y, _ = _xy(frame, panel, measure)
    fig = px.treemap(frame, path=[x], values=y, **_SEQ)
    _style(fig, colors)
    return fig


def render_scatter(frame, panel, measure, colors):
    x, y, color = _xy(frame, panel, measure)
    fig = px.scatter(frame, x=x, y=y, color=color, **_SEQ)
    _style(fig, colors)
    return fig


def render_histogram(frame, panel, measure, colors):
    _, y, _ = _xy(frame, panel, measure)
    fig = px.histogram(frame, x=y, **_SEQ)
    _style(fig, colors)
    return fig


def render_lollipop(frame, panel, measure, colors):
    """Stems + dots; the data-to-viz alternative to a long bar list."""
    x, y, _ = _xy(frame, panel, measure)
    cats = frame.get_column(x).cast(pl.Utf8).to_list() if x else []
    vals = frame.get_column(y).to_list() if y else []
    fig = go.Figure()
    for c, v in zip(cats, vals):
        fig.add_trace(go.Scatter(x=[0, v], y=[c, c], mode="lines",
                                 line=dict(color=colors["dim"], width=2),
                                 showlegend=False, hoverinfo="skip"))
    fig.add_trace(go.Scatter(x=vals, y=cats, mode="markers",
                             marker=dict(color=colors["accent"], size=10),
                             showlegend=False))
    _style(fig, colors, horizontal=True)
    fig.update_yaxes(autorange="reversed")
    return fig


def render_heatmap(frame, panel, measure, colors):
    x, y, color = _xy(frame, panel, measure)
    if color:
        pivot = frame.pivot(values=y, index=x, on=color, aggregate_function="sum")
        ycats = pivot.get_column(x).cast(pl.Utf8).to_list()
        xcats = [c for c in pivot.columns if c != x]
        fig = go.Figure(go.Heatmap(
            z=pivot.drop(x).to_numpy(),
            x=[str(c) for c in xcats], y=[str(i) for i in ycats],
            colorscale=[[0, colors["surface"]], [1, colors["accent"]]]))
        _style(fig, colors)
        return fig
    return render_bar(frame, panel, measure, colors)


def render_bubble_map(frame, panel, measure, colors):
    """Geo scatter when lat/lon present; else fall back to a bar."""
    lat = resolve_column(panel.get("lat"), frame.columns)
    lon = resolve_column(panel.get("lon"), frame.columns)
    _, y, _ = _xy(frame, panel, measure)
    if lat and lon:
        fig = px.scatter_geo(frame, lat=lat, lon=lon, size=y,
                             color_discrete_sequence=[colors["accent"]])
        fig.update_layout(paper_bgcolor="rgba(0,0,0,0)", margin=dict(l=0, r=0, t=8, b=0))
        return fig
    return render_bar(frame, panel, measure, colors)


# ---------------------------------------------------------------------------
# KPI card + table (Dash components, not figures)
# ---------------------------------------------------------------------------


def kpi_card(label: str, value, fmt: str | None = None,
             delta: float | None = None, delta_label: str = "") -> html.Div:
    children = [
        html.Div(label, className="kpi-label"),
        html.Div(format_value(value, fmt), className="kpi-value"),
    ]
    if delta is not None:
        up = delta >= 0
        children.append(html.Div(
            className="kpi-delta " + ("up" if up else "down"),
            children=[
                html.Span(f"{'^' if up else 'v'} {abs(delta):.1f}%", className="kpi-delta-val"),
                html.Span(delta_label or "", className="kpi-delta-label"),
            ]))
    return html.Div(children, className="tile kpi kpi-accent")


def _hex_rgb(h: str):
    h = h.lstrip("#")
    return int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16)


def _rgba(hex_color: str, alpha: float) -> str:
    r, g, b = _hex_rgb(hex_color)
    return f"rgba({r},{g},{b},{round(alpha, 3)})"


def data_table(frame: pl.DataFrame, *, measure: str | None = None,
               fmt: str | None = None, conditional: dict[str, str] | None = None,
               colors: dict | None = None) -> html.Div:
    """HTML table with optional conditional formatting.

    conditional: {column_name: "data_bar" | "color_scale" | "icon"}.
    """
    colors = colors or chart_colors()
    conditional = conditional or {}
    cols = frame.columns
    measure_c = resolve_column(measure, cols) if measure else None

    stats = {}
    for col in conditional:
        c = resolve_column(col, cols)
        if c is not None:
            s = frame.get_column(c).cast(pl.Float64, strict=False)
            try:
                stats[c] = (float(s.min()), float(s.max()))
            except Exception:
                pass

    thead = html.Tr([
        html.Th(_humanize(c), className="num" if c == measure_c else "") for c in cols
    ])
    rows = []
    for row in frame.iter_rows(named=True):
        cells = []
        for c in cols:
            is_num = c == measure_c
            text = format_value(row[c], fmt) if is_num else str(row[c])
            style, children = {}, text
            rule = conditional.get(c) or conditional.get(c.lower())
            if rule and c in stats:
                lo, hi = stats[c]
                raw = row[c]
                raw = float(raw) if raw is not None else float("nan")
                span = (hi - lo) or 1.0
                pct = max(0.0, min(1.0, (raw - lo) / span)) if raw == raw else 0.0
                bar = colors["accent"]
                if rule == "data_bar":
                    p = round(pct * 100)
                    style["background"] = (f"linear-gradient(90deg, {_rgba(bar, 0.22)} "
                                           f"{p}%, transparent {p}%)")
                elif rule == "color_scale":
                    style["background"] = _rgba(bar, 0.08 + 0.40 * pct)
                elif rule == "icon" and raw == raw:
                    up = raw >= 0
                    children = [html.Span("^ " if up else "v ",
                                          style={"color": "#3F8C6E" if up else "#C0563F"}), text]
            cells.append(html.Td(children, className="num" if is_num else "", style=style))
        rows.append(html.Tr(cells))
    return html.Div(html.Table([html.Thead(thead), html.Tbody(rows)], className="tbl"),
                    className="tbl-wrap", style={"height": "100%"})


def graph(fig, **kwargs) -> dcc.Graph:
    return dcc.Graph(figure=fig, config={"displayModeBar": False},
                     style={"height": "100%"}, **kwargs)


__all__ = [
    "render_bar", "render_hbar", "render_grouped_bar", "render_stacked_bar_percent",
    "render_line", "render_area", "render_donut", "render_pie", "render_treemap",
    "render_scatter", "render_histogram", "render_lollipop", "render_heatmap",
    "render_bubble_map", "kpi_card", "data_table", "graph",
]
