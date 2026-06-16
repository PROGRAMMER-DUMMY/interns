"""All Dash callbacks. Registered once against dynamically-created components
via pattern-matching IDs.

Data flow:
  url / config-version  -> sidebar, header, slicer controls
  slicers + crossfilter -> widget grid (re-rendered server-side)
  graph clicks          -> crossfilter store (+ drill-through navigation)
  cfg-poll Interval     -> detects YAML changes on disk -> bumps config-version
  chat send             -> runs the CLI agent -> edits YAML -> reload
"""

from __future__ import annotations

from dash import ALL, MATCH, Input, Output, State, ctx, dcc, html, no_update
from dash.exceptions import PreventUpdate

from minus.agent.bridge import build_prompt, run_agent
from minus.render import layout as L
from minus.render.interactions import NO_NAV, apply_interaction


def _page_id(pathname: str | None, state):
    pid = (pathname or "").strip("/") or None
    page = state.page_by_id(pid)
    return page


def register_callbacks(app, state) -> None:

    # ----- hot reload: poll config mtime ---------------------------------
    @app.callback(Output("config-version", "data"),
                  Input("cfg-poll", "n_intervals"),
                  prevent_initial_call=True)
    def _poll(_n):
        if state.maybe_reload():
            return state.version
        return no_update

    # ----- scheduled data refresh ----------------------------------------
    @app.callback(Output("config-version", "data", allow_duplicate=True),
                  Input("data-refresh", "n_intervals"),
                  prevent_initial_call=True)
    def _data_refresh(n):
        state.model.clear_cache()   # re-read source data; widgets re-render
        return f"refresh-{n}"

    # ----- routing: sidebar / header / slicers ---------------------------
    @app.callback(
        Output("sidebar", "children"),
        Output("page-header", "children"),
        Output("slicer-controls", "children"),
        Input("url", "pathname"),
        Input("config-version", "data"),
    )
    def _route(pathname, _ver):
        if state.error:
            return (L.build_sidebar(state, "") if state.pages else [],
                    html.Div(f"⚠ Config error:\n{state.error}", className="empty"), [])
        page = _page_id(pathname, state)
        if page is None:
            return [], html.Div("No dashboards found. Run `minus new --from-data <folder>`.",
                                className="empty"), []
        return (L.build_sidebar(state, page.id),
                L.build_header(page, state.project.agent.enabled),
                L.build_slicer_controls(page, state))

    # ----- collapsible panels (sidebar + chat) ---------------------------
    chat_on = state.project.agent.enabled
    toggle_inputs = [Input("toggle-sidebar", "n_clicks")]
    if chat_on:
        toggle_inputs.append(Input("toggle-chat", "n_clicks"))

    @app.callback(Output("ui-state", "data"), *toggle_inputs,
                  State("ui-state", "data"), prevent_initial_call=True)
    def _toggle_panels(*args):
        # The toggle buttons live in the per-page header, which is injected after
        # load; ignore the mount event (n_clicks 0/None) so the panels don't
        # spuriously collapse on first render — only act on a real click.
        if not ctx.triggered or not ctx.triggered[0]["value"]:
            raise PreventUpdate
        ui = dict(args[-1] or {"sidebar": True, "chat": chat_on})
        trig = ctx.triggered_id
        if trig == "toggle-sidebar":
            ui["sidebar"] = not ui.get("sidebar", True)
        elif trig == "toggle-chat":
            ui["chat"] = not ui.get("chat", True)
        return ui

    @app.callback(Output("app-root", "className"), Input("ui-state", "data"))
    def _apply_panels(ui):
        ui = ui or {"sidebar": True, "chat": chat_on}
        cls = "app"
        if chat_on and ui.get("chat", True):
            cls += " chat-on"
        if not ui.get("sidebar", True):
            cls += " sidebar-off"
        return cls

    # ----- crossfilter chips ---------------------------------------------
    @app.callback(
        Output("crossfilter-chips", "children"),
        Input("crossfilter", "data"),
        Input("url", "pathname"),
    )
    def _chips(cf, pathname):
        page = _page_id(pathname, state)
        if page is None:
            return []
        return L.build_crossfilter_chips((cf or {}).get(page.id, {}))

    # ----- widget grid ---------------------------------------------------
    @app.callback(
        Output("widgets", "children"),
        Input({"kind": "slicer", "field": ALL}, "value"),
        Input({"kind": "slicer-date", "field": ALL}, "start_date"),
        Input({"kind": "slicer-date", "field": ALL}, "end_date"),
        Input("crossfilter", "data"),
        Input("config-version", "data"),
        State("url", "pathname"),
        State("active-tab", "data"),
    )
    def _widgets(_vals, _starts, _ends, cf, _ver, pathname, active_tab):
        page = _page_id(pathname, state)
        if page is None:
            raise PreventUpdate
        # filter types declared on the page, so we know how to read each control
        ftypes = {f.field: f.type for f in page.filters}
        filters: dict = {}

        # value-based slicers (dropdown / multi / range)
        for entry in (ctx.inputs_list[0] or []):
            field = entry["id"]["field"]
            val = entry.get("value")
            if val in (None, [], ""):
                continue
            if ftypes.get(field) == "range" and isinstance(val, (list, tuple)) and len(val) == 2:
                filters[field] = {"from": val[0], "to": val[1]}
            else:
                filters[field] = val

        # date-range slicers: combine start_date + end_date into bounds
        starts = {e["id"]["field"]: e.get("value") for e in (ctx.inputs_list[1] or [])}
        ends = {e["id"]["field"]: e.get("value") for e in (ctx.inputs_list[2] or [])}
        for field in set(starts) | set(ends):
            lo, hi = starts.get(field), ends.get(field)
            if lo or hi:
                filters[field] = {"from": lo, "to": hi}

        # Keep slicers and click selections separate so the clicked chart can
        # highlight (not self-filter). Slicers still filter every widget.
        cross = (cf or {}).get(page.id, {})
        return L.build_widgets(page, state, filters, cross, active_tab)

    # ----- remember the active widget tab (so cross-filter keeps it) ----
    @app.callback(
        Output("active-tab", "data"),
        Input("widget-tabs", "value"),
        prevent_initial_call=True,
    )
    def _remember_tab(value):
        return value

    # ----- CSV export ----------------------------------------------------
    @app.callback(
        Output({"kind": "download", "page": MATCH, "wid": MATCH}, "data"),
        Input({"kind": "export", "page": MATCH, "wid": MATCH}, "n_clicks"),
        State({"kind": "slicer", "field": ALL}, "value"),
        State("crossfilter", "data"),
        prevent_initial_call=True,
    )
    def _export(n, slicer_vals, cf):
        if not n:
            raise PreventUpdate
        tid = ctx.triggered_id
        page = state.page_by_id(tid["page"])
        widget = next((w for w in page.widgets if w.id == tid["wid"]), None) if page else None
        if widget is None:
            raise PreventUpdate
        filters = {}
        for entry in (ctx.states_list[0] or []):
            v = entry.get("value")
            if v not in (None, [], ""):
                filters[entry["id"]["field"]] = v
        filters.update((cf or {}).get(page.id, {}))
        result = state.engine.run(widget, filters)
        return dcc.send_string(result.frame.write_csv(), f"{page.id}_{widget.id}.csv")

    # ----- crossfilter / drill-through / reset ---------------------------
    @app.callback(
        Output("crossfilter", "data"),
        Output("url", "pathname"),
        Input({"kind": "graph", "page": ALL, "wid": ALL, "dim": ALL, "drill": ALL}, "clickData"),
        Input({"kind": "cf-remove", "field": ALL}, "n_clicks"),
        Input("reset-filters", "n_clicks"),
        State("crossfilter", "data"),
        State("url", "pathname"),
        prevent_initial_call=True,
    )
    def _crossfilter(_clicks, _removes, _reset, cf, pathname):
        page = _page_id(pathname, state)
        page_id = page.id if page else ""
        value = ctx.triggered[0]["value"] if ctx.triggered else None
        new_cf, nav = apply_interaction(ctx.triggered_id, value, cf or {}, page_id)
        if new_cf is None:
            raise PreventUpdate
        return new_cf, (no_update if nav is NO_NAV else nav)

    # ----- chat -> agent -------------------------------------------------
    if state.project.agent.enabled:
        @app.callback(
            Output("chat-log", "children"),
            Output("chat-status", "children"),
            Output("config-version", "data", allow_duplicate=True),
            Output("chat-input", "value"),
            Input("chat-send", "n_clicks"),
            State("chat-input", "value"),
            State("chat-dryrun", "value"),
            State("chat-log", "children"),
            State("url", "pathname"),
            prevent_initial_call=True,
        )
        def _chat(_n, text, dryrun, log, pathname):
            if not text or not text.strip():
                raise PreventUpdate
            page = _page_id(pathname, state)
            prompt = build_prompt(state.root, state.project, text.strip(),
                                  page_id=page.id if page else None, model=state.model)
            result = run_agent(state.project, state.root, prompt, dry_run=bool(dryrun))

            log = list(log or [])
            log.append(html.Div(text.strip(), className="bubble user"))
            log.append(html.Div(
                ("✓ " if result.ok else "✕ ") + (result.message or ""),
                className="bubble agent"))

            version = no_update
            if result.ok and not dryrun:
                state.maybe_reload()
                version = state.version
            status = "" if result.ok else "Agent reported a problem (see above)."
            return log, status, version, ""
