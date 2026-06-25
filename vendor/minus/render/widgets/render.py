"""Render a widget spec + query result into a Dash component (a styled tile)."""

from __future__ import annotations

from datetime import date, datetime

import polars as pl
import plotly.express as px
import plotly.graph_objects as go
from dash import dcc, html

from minus.config.models import Project, Widget
from minus.query.engine import QueryResult
from minus.query.measures import format_value
from minus.render.theme import chart_colors

# Warm editorial palette: terracotta lead with distinct, saturated complements
# (teal, gold, indigo, plum, olive, sky, slate) -- cohesive with the Claude
# accent but separable enough to read many series apart at a glance.
PALETTE = ["#C2603C", "#2F7D6B", "#D69A21", "#4E5D94",
           "#9C4F6B", "#7E8B5A", "#5BA3C2", "#4A453D"]

# Sequential scale for heatmaps: cream -> ochre -> terracotta -> deep brown,
# so cell intensity is actually legible (the old surface->accent wash wasn't).
HEAT_SCALE = [[0.0, "#FBF3E7"], [0.25, "#E8C893"], [0.55, "#D9943F"],
              [0.8, "#C2603C"], [1.0, "#7E3A22"]]


# ---------------------------------------------------------------------------
# Public entry
# ---------------------------------------------------------------------------


def render_widget(widget: Widget, result: QueryResult, project: Project,
                  page_id: str, highlight=None, drill_crumb=None) -> html.Div:
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
    # Affordance: tell a first-time user this chart drills. Shown only at the top
    # level (once drilled, the breadcrumb's 'up' control takes over).
    if getattr(widget, "drill_path", None) and drill_crumb is None:
        head_bits.append(html.Span("click a bar to drill in ↧", className="drill-hint"))
    if widget.export:
        head_bits.append(html.Button("CSV", n_clicks=0, className="csv-btn",
                                     id={"kind": "export", "page": page_id, "wid": widget.id}))
    extras = [dcc.Download(id={"kind": "download", "page": page_id, "wid": widget.id})] \
        if widget.export else []
    header = [html.Div(head_bits, className="tile-head")] if (title or widget.export) else []
    if drill_crumb is not None:
        header = [drill_crumb] + header
    return html.Div(header + [body] + extras, className="tile", style=style)


def drill_crumb(page_id: str, wid: str, trail: list, next_dim: str) -> html.Div:
    """Breadcrumb + 'up' control for a drilled-down chart."""
    steps = [html.Span(f"{_short(dim)}: {label}", className="drill-step")
             for dim, label in trail]
    return html.Div(className="drill-crumb", children=[
        html.Button("↑ up", id={"kind": "drill-up", "page": page_id, "wid": wid},
                    n_clicks=0, className="drill-up-btn", title="Drill up one level"),
        *steps,
        html.Span(f"→ by {_short(next_dim)}", className="drill-next"),
    ])


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
    """Dash wrapper: build the themed figure, then mount it as a cross-filterable Graph."""
    colors = colors or chart_colors(project)
    fig = build_chart_figure(widget, result, project, highlight=highlight, colors=colors)
    dim = result.dimensions[0] if result.dimensions else None
    cf_id = {"kind": "graph", "page": page_id, "wid": widget.id,
             "dim": dim or "", "drill": widget.drilldown or ""}
    return dcc.Graph(figure=fig, id=cf_id, config={"displayModeBar": False},
                     style={"height": "100%"})


def build_chart_figure(widget, result, project, *, highlight=None, colors=None) -> go.Figure:
    """Build the themed Plotly figure for a non-KPI/table widget.

    Extracted from the Dash renderer so the static exporters (the MinusDeck PPTX
    and MinusPress PDF vendors) draw the EXACT figure the live app shows --
    identical palette, data labels, axis styling, and theme colours.
    """
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
            # With a second cut, STACK (one bar per category, coloured segments)
            # -- a grouped hbar would explode into a forest of thin bars.
            fig = px.bar(df, y=dim, x=val, color=breakdown, orientation="h",
                         barmode="stack" if breakdown else "group",
                         labels=labels, **seq)
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
        _flag_incomplete_tail(fig, d, dim, project, colors)
    elif t in ("pie", "donut"):
        fig = px.pie(df, values=primary, names=dim, hole=0.55 if t == "donut" else 0,
                     labels=labels, **seq)
        # Show the actual value AND its share on each slice (e.g. "1,355" + "7.9%"),
        # not just the percent -- the number is what the reader is after.
        pmfmt = _measure_fmt(primary, project) if primary else None
        slice_vals = [format_value(v, pmfmt) for v in df.get_column(primary).to_list()]
        fig.update_traces(text=slice_vals, textposition="inside",
                          texttemplate="%{text}<br>%{percent}")
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
                                       colorscale=HEAT_SCALE,
                                       colorbar=dict(thickness=10, len=0.9)))
        else:
            fig = px.bar(df, x=dim, y=primary, labels=labels, **seq)
    else:  # pragma: no cover - guarded by schema
        fig = px.bar(df, x=dim, y=primary, labels=labels, **seq)

    # Data labels: print each bar/segment's value so the chart reads without
    # hovering -- single-series, grouped (breakdown), stacked, and multi-measure
    # alike. A breakdown keeps ONE measure (same fmt across its series); a
    # multi-measure chart maps each trace back to its own measure's fmt.
    if t in ("bar", "hbar", "stacked_bar") and len(df) and (primary or multi):
        label_fmt = {_measure_label(mn, project): _measure_fmt(mn, project)
                     for mn in result.measures} if multi else {}
        default_fmt = _measure_fmt(primary, project) if primary else None
        stacked = (t == "stacked_bar") or (t == "hbar" and breakdown)
        pos = "inside" if stacked else "outside"
        # Per-segment labels (inside for stacks; tiny ones auto-hide via uniformtext).
        for tr in fig.data:
            vals = tr.x if t == "hbar" else tr.y
            if vals is None:
                continue
            mfmt = label_fmt.get(getattr(tr, "name", None), default_fmt)
            tr.update(text=[format_value(v, mfmt) for v in vals],
                      texttemplate="%{text}", textposition=pos, cliponaxis=False,
                      textfont=dict(size=10.5, color=colors["muted"]))
        # Stacked bars: the per-segment numbers are individually tiny, so also
        # annotate each category's TOTAL at the end of its bar -- the COMPLETE
        # share/value of that department/category, read at a glance.
        if stacked and dim and primary and not multi:
            tot = (df.group_by(dim, maintain_order=True)
                     .agg(pl.col(primary).cast(pl.Float64, strict=False).sum().alias("__t")))
            for cat, v in zip(tot.get_column(dim).to_list(),
                              tot.get_column("__t").to_list()):
                if v is None:
                    continue
                txt = format_value(v, default_fmt)
                if t == "hbar":
                    fig.add_annotation(x=v, y=str(cat), text=txt, showarrow=False,
                                       xanchor="left", xshift=5, yanchor="middle",
                                       font=dict(size=10.5, color=colors["ink"]))
                else:
                    fig.add_annotation(x=str(cat), y=v, text=txt, showarrow=False,
                                       yanchor="bottom", yshift=3, xanchor="center",
                                       font=dict(size=10.5, color=colors["ink"]))
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
            legend=dict(orientation="h", yanchor="top", y=-0.18, x=0.5,
                        xanchor="center", title_text=""),
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

    return fig


def _style(fig, widget, colors):
    # leave room for outside data labels: above (vertical bars) / right (hbar)
    margin = dict(l=8, r=10, t=8, b=8)
    if widget.type in ("bar", "stacked_bar"):
        margin["t"] = 24          # room for value / stacked-total labels above bars
    elif widget.type == "hbar":
        margin["r"] = 66          # room for value / stacked-total labels at bar tip
    is_pie = bool(fig.data) and getattr(fig.data[0], "type", "") == "pie"
    legend_shown = is_pie or bool(fig.data and len(fig.data) > 1)
    # Reserve bottom space so the horizontal legend sits BELOW the plot/axis and
    # never overlaps the bars or tick labels (more room when many series wrap).
    if legend_shown:
        if is_pie:
            labels = getattr(fig.data[0], "labels", None)
            n_series = len(labels) if labels is not None else 0
        else:
            n_series = len(fig.data)
        margin["b"] = 58 + (18 if n_series > 4 else 0)
    fig.update_layout(
        colorway=PALETTE,
        paper_bgcolor="rgba(0,0,0,0)",
        plot_bgcolor="rgba(0,0,0,0)",
        font=dict(family="Hanken Grotesk, ui-sans-serif, Segoe UI, sans-serif",
                  color=colors["muted"], size=12),
        margin=margin,
        legend=dict(orientation="h", yanchor="top", y=-0.16, x=0.5, xanchor="center",
                    title_text="", font=dict(size=10.5, color=colors["muted"])),
        showlegend=legend_shown,
    )
    # hide data labels that don't fit (prevents overlap on thin bars)
    if widget.type in ("bar", "hbar", "stacked_bar"):
        fig.update_layout(uniformtext=dict(minsize=8, mode="hide"))
        # Stop a few categories (e.g. after a drill leaves 1-2) from ballooning
        # into giant square blocks: widen the gap so bars stay a sane width.
        try:
            tr = fig.data[0]
            cats = tr.y if widget.type == "hbar" else tr.x
            ncat = len({str(c) for c in cats}) if cats is not None else 0
        except Exception:
            ncat = 0
        if ncat and ncat <= 2:
            fig.update_layout(bargap=0.74)
        elif ncat and ncat <= 5:
            fig.update_layout(bargap=0.5)
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


def _as_date(v):
    if isinstance(v, datetime):
        return v.date()
    if isinstance(v, date):
        return v
    try:
        return date.fromisoformat(str(v)[:10])
    except Exception:
        return None


def _flag_incomplete_tail(fig, d, dim, project, colors):
    """Shade + label the trailing periods beyond ``project.data_through`` so a
    volume dip from an incomplete final period (e.g. a still-loading extract)
    isn't misread as a real decline. No-op unless a cutoff is set and the series
    actually extends past it. Generic: works for any date-bucketed trend."""
    cutoff_raw = getattr(project, "data_through", None)
    if not cutoff_raw or dim not in d.columns or d.height == 0:
        return
    cutoff = _as_date(cutoff_raw)
    xs = [x for x in d.get_column(dim).to_list() if x is not None]
    if cutoff is None or not xs:
        return
    last = _as_date(xs[-1])
    if last is None or last <= cutoff:
        return
    try:
        fig.add_vrect(x0=cutoff, x1=last, fillcolor=colors["muted"], opacity=0.07,
                      line_width=0, layer="below")
        fig.add_vline(x=cutoff, line_dash="dot", line_width=1, line_color=colors["muted"])
        fig.add_annotation(x=last, y=1.0, yref="paper", showarrow=False,
                           text="incomplete", xanchor="right", yanchor="bottom",
                           font=dict(size=9, color=colors["muted"]))
    except Exception:
        pass


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
