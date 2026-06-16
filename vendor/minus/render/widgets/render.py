"""Render a widget spec + query result into a Dash component (a styled tile)."""

from __future__ import annotations

import polars as pl
import plotly.express as px
import plotly.graph_objects as go
from dash import dcc, html

from minus.config.models import Project, Widget
from minus.query.engine import QueryResult
from minus.query.measures import format_value
from minus.render.theme import chart_colors

# Warm editorial palette: terracotta lead with muted complements (pine, ochre,
# dusty blue, plum, sage, sand, slate). Cohesive with the Claude theme accent.
PALETTE = ["#C2603C", "#3F6B5E", "#C9952F", "#6E7F94",
           "#9C5A6B", "#7E8B5A", "#D9A877", "#4A453D"]


# ---------------------------------------------------------------------------
# Public entry
# ---------------------------------------------------------------------------


def render_widget(widget: Widget, result: QueryResult, project: Project,
                  page_id: str, highlight=None) -> html.Div:
    style = {"gridColumn": f"span {widget.width}", "height": f"{widget.height}px"}
    if widget.type == "kpi":
        return html.Div(_kpi(widget, result, project), className="tile kpi kpi-accent",
                        style=style)
    colors = chart_colors(project)
    if widget.type == "table":
        body = _table(widget, result, project, colors)
    else:
        body = _chart(widget, result, project, page_id, highlight, colors)
    title = widget.title or ""
    head_bits = [html.P(title, className="tile-title")] if title else [html.Span()]
    extras = []
    if widget.export:
        head_bits.append(html.Button("CSV", n_clicks=0, className="csv-btn",
                                     id={"kind": "export", "page": page_id, "wid": widget.id}))
        extras.append(dcc.Download(id={"kind": "download", "page": page_id, "wid": widget.id}))
    header = [html.Div(head_bits, className="tile-head")] if (title or widget.export) else []
    return html.Div(header + [body] + extras, className="tile", style=style)


# ---------------------------------------------------------------------------
# KPI
# ---------------------------------------------------------------------------


def _kpi(widget, result, project):
    m = project.measure(widget.measure)
    label = widget.title or m.label or m.name
    value = format_value(result.scalar, m.fmt)
    # Target/benchmark: color the value green/red vs target (healthcare-KPI style).
    target = getattr(m, "target", None)
    value_style = {}
    on_target = None
    if target is not None and result.scalar is not None:
        higher_better = getattr(m, "goal", "higher") == "higher"
        on_target = (result.scalar >= target) if higher_better else (result.scalar <= target)
        value_style = {"color": "#3F8C6E" if on_target else "#C0563F"}
    children = [
        html.Div(label, className="kpi-label"),
        html.Div(value, className="kpi-value", style=value_style),
    ]
    # One-line business context (the question the KPI answers).
    sub = getattr(widget, "subtitle", None)
    if sub:
        children.append(html.Div(sub, className="kpi-sub", title=sub))
    if target is not None:
        children.append(html.Div(
            className="kpi-delta " + ("up" if on_target else "down"),
            children=[
                html.Span("● ", style={"color": "#3F8C6E" if on_target else "#C0563F"}),
                html.Span(f"Target {format_value(target, m.fmt)}", className="kpi-delta-label"),
            ]))
    # Period-over-period trend (▲/▼ % vs prev period), when configured.
    if result.delta is not None:
        up = result.delta >= 0
        children.append(html.Div(
            className="kpi-delta " + ("up" if up else "down"),
            children=[
                html.Span(f"{'▲' if up else '▼'} {abs(result.delta):.1f}%",
                          className="kpi-delta-val"),
                html.Span(result.delta_label or "", className="kpi-delta-label"),
            ]))
    return children


# ---------------------------------------------------------------------------
# Charts
# ---------------------------------------------------------------------------


def _chart(widget, result, project, page_id, highlight=None, colors=None):
    colors = colors or chart_colors(project)
    df = result.frame
    dim = result.dimensions[0] if result.dimensions else None
    breakdown = result.dimensions[1] if len(result.dimensions) > 1 else None
    primary = result.measures[0] if result.measures else None

    labels = {c: _short(c) for c in df.columns}
    for mname in result.measures:
        labels[mname] = _measure_label(mname, project)

    seq = {"color_discrete_sequence": PALETTE}
    t = widget.type
    # Multi-measure (e.g. Claimed vs Paid) -> grouped bars, one series per measure.
    multi = len(result.measures) > 1 and not breakdown
    val = result.measures if multi else primary
    if t in ("bar", "hbar"):
        if t == "hbar":
            fig = px.bar(df, y=dim, x=val, color=breakdown, orientation="h",
                         barmode="group", labels=labels, **seq)
        else:
            fig = px.bar(df, x=dim, y=val, color=breakdown, barmode="group",
                         labels=labels, **seq)
    elif t == "stacked_bar":
        # composition: one bar per x, stacked segments per breakdown
        fig = px.bar(df, x=dim, y=primary, color=breakdown, barmode="stack",
                     labels=labels, **seq)
    elif t == "combo":
        # measures[0] as bars (left axis) + measures[1] as a line (right axis),
        # with the line measure's target drawn as a dotted reference -- one chart
        # showing absolute value AND a rate/target together.
        ms = result.measures
        d = df.sort(dim)
        xs = d.get_column(dim).cast(pl.Utf8).to_list()
        fig = go.Figure()
        fig.add_bar(x=xs, y=d.get_column(ms[0]).to_list(),
                    name=_measure_label(ms[0], project), marker_color=PALETTE[0])
        if len(ms) > 1:
            fig.add_scatter(x=xs, y=d.get_column(ms[1]).to_list(), yaxis="y2",
                            name=_measure_label(ms[1], project), mode="lines+markers",
                            line=dict(color=PALETTE[1], width=2.5))
            tgt = getattr(project.measure(ms[1]), "target", None)
            if tgt is not None:
                fig.add_hline(y=tgt, yref="y2", line_dash="dot",
                              line_color=colors["muted"],
                              annotation_text=f"target {tgt:g}",
                              annotation_font_size=9)
    elif t == "small_multiples":
        # the measure faceted by the breakdown dimension (trellis) -- compares a
        # trend/shape across categories without one overloaded chart.
        d = df.sort(dim)
        facet = breakdown or dim
        fig = px.line(d, x=dim, y=primary, facet_col=facet, facet_col_wrap=3,
                      markers=True, labels=labels, **seq)
        fig.for_each_annotation(lambda a: a.update(text=a.text.split("=")[-1]))
        fig.update_yaxes(matches=None, showticklabels=False)
    elif t in ("line", "area"):
        d = df.sort(dim)
        fn = px.area if t == "area" else px.line
        fig = fn(d, x=dim, y=primary, color=breakdown, labels=labels,
                 markers=(t == "line"), **seq)
    elif t in ("pie", "donut"):
        fig = px.pie(df, values=primary, names=dim, hole=0.55 if t == "donut" else 0,
                     labels=labels, **seq)
        fig.update_traces(textposition="inside", textinfo="percent")
    elif t == "scatter":
        ms = result.measures
        x, y = ms[0], (ms[1] if len(ms) > 1 else ms[0])
        fig = px.scatter(df, x=x, y=y, color=dim, labels=labels, **seq)
    elif t == "heatmap":
        if breakdown:
            pivot = df.pivot(values=primary, index=dim, on=breakdown, aggregate_function="sum")
            ycats = pivot.get_column(dim).cast(pl.Utf8).to_list()
            xcats = [c for c in pivot.columns if c != dim]
            fig = go.Figure(go.Heatmap(z=pivot.drop(dim).to_numpy(),
                                       x=[str(c) for c in xcats], y=[str(i) for i in ycats],
                                       colorscale=[[0, colors["surface"]], [1, colors["accent"]]]))
        else:
            fig = px.bar(df, x=dim, y=primary, labels=labels, **seq)
    else:  # pragma: no cover - guarded by schema
        fig = px.bar(df, x=dim, y=primary, labels=labels, **seq)

    # Data labels on single-series bars so values read without hovering.
    if t in ("bar", "hbar") and primary and not breakdown and not multi and len(df):
        mfmt = _measure_fmt(primary, project)
        texts = [format_value(v, mfmt) for v in df.get_column(primary).to_list()]
        fig.update_traces(text=texts, textposition="outside", cliponaxis=False,
                          textfont=dict(size=10.5, color=colors["muted"]))
        if t == "hbar":
            fig.update_yaxes(autorange="reversed")  # highest at the top

    # Highlight the clicked category in its own chart (keep all bars/slices,
    # dim the rest) rather than filtering it down to a single value.
    if highlight is not None and dim and not breakdown and not multi:
        hl = highlight if isinstance(highlight, (list, tuple, set)) else [highlight]
        hlset = {str(x) for x in hl}
        cats = df.get_column(dim).cast(pl.Utf8).to_list()
        marks = [colors["accent"] if c in hlset else colors["dim"] for c in cats]
        if t in ("bar", "hbar"):
            fig.update_traces(marker_color=marks)
        elif t in ("pie", "donut"):
            fig.update_traces(marker=dict(colors=marks))

    _style(fig, widget, colors)

    # Combo: a right-hand secondary axis for the line measure + a legend.
    if t == "combo":
        fig.update_layout(
            showlegend=True,
            yaxis2=dict(overlaying="y", side="right", showgrid=False,
                        tickfont=dict(size=11, color=colors["muted"])),
            legend=dict(orientation="h", yanchor="bottom", y=-0.22, x=0, title_text=""),
        )

    # Donut/pie: show a legend, and a center total for donuts (self-explanatory).
    if t in ("pie", "donut"):
        fig.update_layout(showlegend=True, legend=dict(
            orientation="h", y=-0.04, x=0.5, xanchor="center", yanchor="top",
            font=dict(size=10, color=colors["muted"]), title_text=""))
        if t == "donut" and primary:
            fig.update_traces(domain=dict(x=[0, 1], y=[0.12, 1]))
            total = float(df.get_column(primary).sum())
            fig.add_annotation(
                x=0.5, y=0.56, xref="paper", yref="paper", showarrow=False, align="center",
                text=(f"<b>{format_value(total, _measure_fmt(primary, project))}</b>"
                      f"<br><span style='font-size:9px'>{_measure_label(primary, project)}</span>"),
                font=dict(size=15, color=colors["ink"], family="Fraunces, Georgia, serif"))

    cf_id = {"kind": "graph", "page": page_id, "wid": widget.id,
             "dim": dim or "", "drill": widget.drilldown or ""}
    return dcc.Graph(figure=fig, id=cf_id, config={"displayModeBar": False},
                     style={"height": "100%"})


def _style(fig, widget, colors):
    # leave room for outside data labels: above (vertical bars) / right (hbar)
    margin = dict(l=8, r=10, t=8, b=8)
    if widget.type == "bar":
        margin["t"] = 24
    elif widget.type == "hbar":
        margin["r"] = 66
    is_pie = bool(fig.data) and getattr(fig.data[0], "type", "") == "pie"
    fig.update_layout(
        colorway=PALETTE,
        paper_bgcolor="rgba(0,0,0,0)",
        plot_bgcolor="rgba(0,0,0,0)",
        font=dict(family="Hanken Grotesk, ui-sans-serif, Segoe UI, sans-serif",
                  color=colors["muted"], size=12),
        margin=margin,
        legend=dict(orientation="h", yanchor="bottom", y=-0.22, x=0, title_text=""),
        showlegend=is_pie or bool(fig.data and len(fig.data) > 1),
    )
    # hide data labels that don't fit (prevents overlap on thin bars)
    if widget.type in ("bar", "hbar"):
        fig.update_layout(uniformtext=dict(minsize=8, mode="hide"))
    fig.update_xaxes(showgrid=False, zeroline=False, automargin=True,
                     title_text="", tickfont=dict(size=11, color=colors["muted"]))
    fig.update_yaxes(showgrid=True, gridcolor=colors["grid"], zeroline=False,
                     automargin=True, title_text="", tickfont=dict(size=11, color=colors["muted"]))


# ---------------------------------------------------------------------------
# Table
# ---------------------------------------------------------------------------


def _hex_rgb(h: str) -> tuple[int, int, int]:
    h = h.lstrip("#")
    return int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16)


def _table(widget, result, project, colors=None):
    colors = colors or chart_colors(project)
    df = result.frame
    headers, fmts = [], {}
    for c in df.columns:
        if c in result.measures:
            headers.append((c, _measure_label(c, project), True))
            fmts[c] = project.measure(c).fmt
        else:
            headers.append((c, _short(c), False))

    # precompute per-column min/max for any conditional-format rules
    rules = {r.column: r for r in widget.conditional}
    stats = {}
    for col in rules:
        if col in df.columns:
            s = df.get_column(col).cast(pl.Float64, strict=False)
            stats[col] = (float(s.min()), float(s.max()))

    thead = html.Tr([html.Th(label, className="num" if num else "") for _, label, num in headers])
    rows = []
    for row in df.iter_rows(named=True):
        cells = []
        for col, _, num in headers:
            text = format_value(row[col], fmts.get(col)) if num else str(row[col])
            style, children = {}, text
            rule = rules.get(col)
            if rule and col in stats:
                lo, hi = stats[col]
                raw = row[col]
                raw = float(raw) if raw is not None else float("nan")
                span = (hi - lo) or 1.0
                pct = max(0.0, min(1.0, (raw - lo) / span)) if raw == raw else 0.0
                bar = rule.color or colors["accent"]
                if rule.type == "data_bar":
                    p = round(pct * 100)
                    style["background"] = (f"linear-gradient(90deg, {_rgba(bar, 0.22)} "
                                           f"{p}%, transparent {p}%)")
                elif rule.type == "color_scale":
                    style["background"] = _rgba(bar, 0.08 + 0.40 * pct)
                elif rule.type == "icon" and raw == raw:
                    up = float(raw) >= 0
                    children = [html.Span("▲ " if up else "▼ ",
                                          style={"color": "#3F8C6E" if up else "#C0563F"}), text]
            cells.append(html.Td(children, className="num" if num else "", style=style))
        rows.append(html.Tr(cells))
    return html.Div(html.Table([html.Thead(thead), html.Tbody(rows)], className="tbl"),
                    className="tbl-wrap", style={"height": "100%"})


def _rgba(hex_color: str, alpha: float) -> str:
    r, g, b = _hex_rgb(hex_color)
    return f"rgba({r},{g},{b},{round(alpha, 3)})"


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def _short(col: str) -> str:
    return col.split(".", 1)[-1]


def _measure_label(name: str, project: Project) -> str:
    try:
        m = project.measure(name)
        return m.label or m.name
    except KeyError:
        return name


def _measure_fmt(name: str, project: Project):
    try:
        return project.measure(name).fmt
    except KeyError:
        return None
