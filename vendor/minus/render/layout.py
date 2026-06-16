"""Builders for the app shell and per-page content.

The static layout (created once) holds routing + stores + named containers.
Sidebar, header, slicer bar, and the widget grid are (re)built by callbacks so
they track the current URL, active filters, and hot-reloaded config.
"""

from __future__ import annotations

from typing import Any

import polars as pl
from dash import dcc, html

from minus.config.models import Dashboard, Filter
from minus.data.model import split_field
from minus.render.interactions import drill_state
from minus.render.widgets import render_widget
from minus.render.widgets.render import drill_crumb as build_drill_crumb


# ---------------------------------------------------------------------------
# Static outer layout
# ---------------------------------------------------------------------------


def app_shell(state) -> html.Div:
    chat_on = state.project.agent.enabled
    return html.Div(
        id="app-root",
        className="app chat-on" if chat_on else "app",
        children=[
            dcc.Location(id="url", refresh=False),
            # Safety-net poll; ConfigWatcher reloads eagerly between ticks.
            dcc.Interval(id="cfg-poll", interval=3000, n_intervals=0),
            # Optional scheduled data refresh (re-reads sources every N seconds).
            dcc.Interval(id="data-refresh",
                         interval=(state.project.refresh_seconds or 3600) * 1000,
                         disabled=not state.project.refresh_seconds),
            dcc.Store(id="config-version", data=state.version),
            dcc.Store(id="crossfilter", data={}),
            # Per-widget in-place drill-down position {page: {wid: {level, trail}}}.
            dcc.Store(id="drill", data={}),
            # Remembers the active widget tab so a cross-filter re-render keeps it.
            dcc.Store(id="active-tab", data=None),
            # Panel visibility persists in the browser session.
            dcc.Store(id="ui-state", storage_type="session",
                      data={"sidebar": True, "chat": chat_on}),
            html.Div(id="sidebar", className="sidebar"),
            html.Div(
                className="main",
                children=[
                    html.Div(id="page-header"),
                    html.Div(className="slicer-bar", children=[
                        html.Div(id="slicer-controls", className="slicer-group"),
                        html.Div(id="crossfilter-chips", className="slicer-group"),
                    ]),
                    html.Div(id="widgets"),
                ],
            ),
            build_chat(state) if chat_on else html.Div(),
        ],
    )


# ---------------------------------------------------------------------------
# Sidebar / header
# ---------------------------------------------------------------------------


def build_sidebar(state, active_id: str) -> list:
    items = [
        dcc.Link(
            className="nav-item active" if p.id == active_id else "nav-item",
            href=f"/{p.id}",
            # Icon is optional; when absent the active accent bar + weight carry it.
            children=([html.Span(p.icon, className="nav-icon")] if p.icon else [])
            + [html.Span(p.title)],
        )
        for p in state.pages
    ]
    mark = (state.project.name or "M")[0].upper()
    return [
        html.Div(className="brand", children=[
            html.Div(mark, className="brand-mark"),
            html.Span(state.project.name, className="brand-name"),
        ]),
        html.Div("Pages", className="nav-label"),
        *items,
        html.Div("MinusAnalyst · config-driven", className="sidebar-foot"),
    ]


def build_header(page: Dashboard, chat_on: bool = True) -> html.Div:
    sub = [html.P(page.description, className="page-sub")] if page.description else []
    actions = []
    if chat_on:
        actions.append(html.Button(id="toggle-chat", className="icon-btn icon-btn--chat",
                                   n_clicks=0, title="Show/hide the agent panel"))
    actions.append(html.Button("Reset filters", id="reset-filters", className="btn btn-ghost"))
    return html.Div(className="topbar", children=[
        html.Div(className="topbar-left", children=[
            html.Button(id="toggle-sidebar", className="icon-btn", n_clicks=0,
                        title="Show/hide the navigation"),
            html.Div([html.H1(page.title, className="page-title"), *sub]),
        ]),
        html.Div(className="topbar-actions", children=actions),
    ])


# ---------------------------------------------------------------------------
# Slicer bar
# ---------------------------------------------------------------------------


def build_slicer_controls(page: Dashboard, state) -> list:
    return [_slicer(f, state) for f in page.filters]


def build_crossfilter_chips(crossfilter: dict[str, Any]) -> list:
    return [
        html.Div(className="crossfilter-chip", children=[
            html.Span(f"{split_field(field)[1]}: {value}"),
            html.Span("✕", className="x",
                      id={"kind": "cf-remove", "field": field}),
        ])
        for field, value in (crossfilter or {}).items()
    ]


def _slicer(f: Filter, state) -> html.Div:
    sid = {"kind": "slicer", "field": f.field}
    table, col = split_field(f.field)
    label = f.label or col or f.field        # bare 'column' has no table part
    if f.type == "date_range":
        # DatePickerRange exposes start_date/end_date (not `value`), so it uses a
        # distinct id kind that the widgets callback reads separately.
        s = state.model.assemble(table, [f.field]).get_column(f.field)
        if not s.dtype.is_temporal():
            s = s.str.to_datetime(strict=False)
        s = s.drop_nulls()
        lo = s.min().date() if len(s) else None
        hi = s.max().date() if len(s) else None
        ctrl = dcc.DatePickerRange(
            id={"kind": "slicer-date", "field": f.field},
            min_date_allowed=lo, max_date_allowed=hi, display_format="YYYY-MM-DD",
        )
    elif f.type == "range":
        s = state.model.assemble(table, [f.field]).get_column(f.field) \
            .cast(pl.Float64, strict=False).drop_nulls()
        lo, hi = (float(s.min()), float(s.max())) if len(s) else (0, 1)
        ctrl = dcc.RangeSlider(id=sid, min=lo, max=hi, value=[lo, hi])
    else:
        opts = _distinct(state, f.field)
        ctrl = dcc.Dropdown(
            id=sid, options=[{"label": str(o), "value": o} for o in opts],
            multi=(f.type == "multi"), placeholder=f"All {label.lower()}",
            value=f.default,
        )
    return html.Div(className="slicer", children=[
        html.Label(label, className="slicer-label"), ctrl])


def _distinct(state, field: str, limit: int = 500) -> list:
    if "." in field:
        table = split_field(field)[0]
        s = state.model.assemble(table, [field]).get_column(field).drop_nulls()
        return sorted(s.unique().to_list(), key=lambda v: str(v))[:limit]
    # Logical / global slicer (bare column): union the column's distinct values
    # across every table that has it, so one control spans several tables.
    vals: set = set()
    for t in state.project.tables:
        try:
            cols = state.model.columns(t.name)
        except Exception:
            continue
        col = next((c for c in cols if c.lower() == field.lower()), None)
        if col:
            vals |= set(state.model.table_df(t.name).get_column(col).drop_nulls().unique().to_list())
    return sorted(vals, key=lambda v: str(v))[:limit]


# ---------------------------------------------------------------------------
# Widget grid
# ---------------------------------------------------------------------------


def _render_tile(w, state, page, filters, crossfilter, drill=None):
    try:
        wf = dict(filters)
        wf.update(crossfilter)
        highlight = None
        if w.dimension and w.dimension in crossfilter:
            wf.pop(w.dimension, None)          # don't self-filter the clicked chart
            highlight = crossfilter[w.dimension]
        # Scope the cross-filter: a click on one KPI's chart must not error the
        # sibling tiles of an unrelated-table scorecard -- they just ignore it.
        wf = state.engine.applicable_filters(w, wf)

        # In-place drill-down: override the chart's dimension to the current
        # drill level and filter to the drilled-into categories, with a crumb.
        eff, crumb = w, None
        path = list(getattr(w, "drill_path", []) or [])
        ds = drill_state(drill, page.id, w.id) if (drill and path) else None
        if ds and ds.get("level", 0) > 0 and w.dimension:
            table = split_field(w.dimension)[0]
            for dim_name, label in ds.get("trail", []):
                wf[f"{table}.{dim_name}"] = label
            level = min(ds["level"], len(path) - 1)
            # Drop the now-stale "... by <top dim>" from the title; the breadcrumb
            # shows the live drill dimension, so the title shouldn't contradict it.
            base_title = (w.title or "").split(" by ")[0].strip()
            eff = w.model_copy(update={"dimension": f"{table}.{path[level]}",
                                       "title": base_title or w.title})
            crumb = build_drill_crumb(page.id, w.id, ds.get("trail", []), path[level])

        result = state.engine.run(eff, wf)
        return render_widget(eff, result, state.project, page.id, highlight, crumb)
    except Exception as exc:  # surface per-tile errors instead of crashing the page
        return html.Div(
            className="tile", style={"gridColumn": f"span {w.width}"},
            children=[html.P(w.title or w.id, className="tile-title"),
                      html.Div(f"⚠ {exc}", className="empty")])


def build_widgets(page: Dashboard, state, filters: dict[str, Any],
                  crossfilter: dict[str, Any] | None = None,
                  active_tab: str | None = None,
                  drill: dict[str, Any] | None = None) -> list:
    """Render the page's tiles.

    ``filters`` are page slicers; ``crossfilter`` are click selections. A click
    on a chart filters every *other* visual + the KPIs, but the clicked chart
    keeps all its categories and highlights the selection (Power BI-style).

    When widgets declare a ``tab``, the tiles are grouped into ``dcc.Tabs`` so
    each view fits the screen without scrolling; switching tabs is client-side,
    and ``active_tab`` restores the selected tab across cross-filter re-renders.
    """
    crossfilter = crossfilter or {}
    if not page.widgets:
        return [html.Div("This page has no widgets yet.", className="empty")]

    # Group preserving first-seen order; widgets without a tab go to a default.
    groups: dict[str, list] = {}
    for w in page.widgets:
        groups.setdefault(getattr(w, "tab", None) or "", []).append(w)

    def grid_for(ws):
        return html.Div([_render_tile(w, state, page, filters, crossfilter, drill)
                         for w in ws], className="grid")

    # No tabs declared -> a single grid (original behavior).
    if len(groups) == 1 and "" in groups:
        return [grid_for(groups[""])]

    blocks: list = []
    # Untabbed widgets render as a PERSISTENT grid above the tabs (e.g. a KPI
    # scorecard that stays visible while you switch the per-KPI chart tabs),
    # rather than being shoved into an "Other" tab.
    if "" in groups:
        blocks.append(grid_for(groups.pop("")))

    if groups:
        labels = list(groups.keys())
        value = active_tab if active_tab in labels else labels[0]
        blocks.append(dcc.Tabs(
            id="widget-tabs", value=value, className="widget-tabs",
            children=[dcc.Tab(label=lbl or "Other", value=lbl,
                              className="widget-tab", selected_className="widget-tab--sel",
                              children=[grid_for(ws)]) for lbl, ws in groups.items()],
        ))
    return blocks


# ---------------------------------------------------------------------------
# Chat panel
# ---------------------------------------------------------------------------


def build_chat(state) -> html.Div:
    cmd = " ".join(state.project.agent.command).replace("{prompt}", "…")
    return html.Div(id="chat", className="chat", children=[
        html.Div(className="chat-head", children=[
            html.Div([
                html.H3("Edit with your agent"),
                html.Div(f"via  {cmd}", className="sub"),
            ]),
        ]),
        html.Div(id="chat-log", className="chat-log", children=[
            html.Div("Ask for changes in plain English — e.g. "
                     "\"add a pie chart of total amount by visit type\". "
                     "Your CLI agent edits the YAML and the dashboard reloads.",
                     className="bubble agent"),
        ]),
        html.Div(className="chat-compose", children=[
            dcc.Textarea(id="chat-input", placeholder="Describe a change…"),
            html.Div(className="topbar-actions", children=[
                html.Button("Send to agent", id="chat-send", className="btn btn-accent"),
                dcc.Checklist(id="chat-dryrun", options=[{"label": " dry run", "value": "dry"}],
                              value=[], style={"fontSize": "12px", "color": "#6B6862"}),
            ]),
            html.Div("No API key — runs your installed CLI with its own auth.",
                     className="chat-hint"),
            dcc.Loading(html.Div(id="chat-status")),
        ]),
    ])
