"""Dash renderer: live SQL re-execution + Plotly chart per spec.

Workspace-agnostic. Takes a `WorkspaceLayout`, reads every KPI's spec,
re-executes its generated SQL via DuckDB against the workspace datasets,
and renders a Plotly figure per spec. Blocked KPIs render a blocker card
with the recovery commands from `compute_workflow_diff`.
"""
from __future__ import annotations

import json
import os
import re
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from datetime import date, datetime

import dash
import dash_bootstrap_components as dbc
import plotly.express as px
import plotly.graph_objs as go
from dash import ALL, Input, Output, dcc, html, no_update

from core.dashboard.design_md import DesignTokens
from core.dashboard.spec import DashboardSpec, load_kpi_spec
from core.onboarding.kpi.registry_loader import load_kpi_definitions
from core.onboarding.workspace.flow import compute_workflow_diff
from core.storage.workspace_layout import WorkspaceLayout


@contextmanager
def _at_repo_root(repo_root: Path):
    """DuckDB read_csv_auto paths in generated SQL are relative to repo root."""
    old = Path.cwd()
    try:
        os.chdir(repo_root)
        yield
    finally:
        os.chdir(old)


def _execute_sql_view(repo_root: Path, sql_path: Path, view_name: str, limit: int = 5000):
    """Return a list of dicts (rows). Empty on failure — never raises."""
    if not sql_path.exists():
        return []
    try:
        import duckdb
    except Exception:
        return []
    try:
        sql_text = sql_path.read_text(encoding="utf-8")
    except OSError:
        return []
    try:
        with _at_repo_root(repo_root):
            conn = duckdb.connect(":memory:")
            try:
                conn.execute(sql_text)
                cursor = conn.execute(f'SELECT * FROM "{view_name}" LIMIT {int(limit)}')
                columns = [d[0] for d in cursor.description]
                rows = cursor.fetchall()
                return [dict(zip(columns, row)) for row in rows]
            finally:
                conn.close()
    except Exception:
        return []


_DATE_NAME_RE = re.compile(
    r"(date|month|year|week|day|timestamp|created|service|invoice|filed|posted)",
    re.IGNORECASE,
)


def _spec_date_column(spec: DashboardSpec) -> str:
    x_col = str(spec.config.get("x") or "")
    if x_col and _DATE_NAME_RE.search(x_col):
        return x_col
    return ""


def _coerce_date(value: Any) -> date | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    text = str(value).strip()
    if not text:
        return None
    for fmt in ("%Y-%m-%d", "%Y-%m-%dT%H:%M:%S", "%Y/%m/%d"):
        try:
            return datetime.strptime(text[: len(fmt) + 4], fmt).date()
        except ValueError:
            continue
    try:
        return datetime.fromisoformat(text[:19]).date()
    except ValueError:
        return None


def _filter_rows_by_date(
    rows: list[dict[str, Any]],
    column: str,
    start: date | None,
    end: date | None,
) -> list[dict[str, Any]]:
    if not column or (start is None and end is None):
        return rows
    out: list[dict[str, Any]] = []
    for row in rows:
        value = _coerce_date(row.get(column))
        if value is None:
            continue
        if start is not None and value < start:
            continue
        if end is not None and value > end:
            continue
        out.append(row)
    return out


# Neutral hairline grid/axis (shared across themes; the rest comes from DESIGN.md).
_GRID_COLOR = "#e3ddcf"
_AXIS_COLOR = "#cfc8b8"

# Design language is data-driven from a swappable DESIGN.md (Phase 1d). The active
# tokens supply the accent (single-series), the colorblind-safe categorical ramp
# (multi-series — separation enforced by the verify gate), fonts, and text color.
# `set_active_design()` is called by the export/refresh path before rendering so a
# workspace's DESIGN.md changes the look with no code change. Defaults are editorial.
_ACTIVE: DesignTokens = DesignTokens()


def set_active_design(tokens: DesignTokens) -> None:
    """Set the DESIGN.md tokens used by subsequent figure renders (process-wide)."""
    global _ACTIVE
    _ACTIVE = tokens


def _apply_corporate_theme(fig: go.Figure, *, percent_axis: str | None = None) -> go.Figure:
    """Apply the shared clean-corporate-BI look to any figure (single styling seam)."""
    fig.update_layout(
        template="plotly_white",
        colorway=list(_ACTIVE.categorical),
        font={"family": _ACTIVE.sans, "size": 12, "color": _ACTIVE.ink},
        title={"font": {"size": 15, "color": _ACTIVE.ink}, "x": 0.01, "xanchor": "left"},
        paper_bgcolor="rgba(0,0,0,0)",
        plot_bgcolor="rgba(0,0,0,0)",
        margin={"l": 52, "r": 18, "t": 44, "b": 44},
        legend={
            "orientation": "h",
            "yanchor": "bottom",
            "y": 1.02,
            "xanchor": "right",
            "x": 1,
            "title": {"text": ""},
        },
        hoverlabel={"font": {"family": _ACTIVE.sans, "size": 12}},
        autosize=True,
    )
    axis_style = {
        "gridcolor": _GRID_COLOR,
        "linecolor": _AXIS_COLOR,
        "zerolinecolor": _GRID_COLOR,
        "showline": True,
        "ticks": "outside",
        "tickcolor": _AXIS_COLOR,
        "automargin": True,  # V5: labels/titles never clip the card edge
    }
    fig.update_xaxes(**axis_style)
    fig.update_yaxes(**axis_style)
    # Thousands separators on numeric axes by default; percent axes opt in.
    fig.update_layout(separators=".,")
    if percent_axis == "x":
        fig.update_xaxes(tickformat=".0%")
    elif percent_axis == "y":
        fig.update_yaxes(tickformat=".0%")
    return fig


def _aggregate_rows(
    rows: list[dict[str, Any]], x_col: str, color_col: str | None, y_col: str
) -> list[dict[str, Any]]:
    """Sum the measure over the group keys (x[, color]). Charts must show one value
    per category/series — plotting raw granular rows produces dot-stripes (line) or
    a broken >100% stack (share). Order of first appearance is preserved."""
    keys = [c for c in (x_col, color_col) if c]
    if not keys:
        return rows
    agg: dict[tuple, dict[str, Any]] = {}
    order: list[tuple] = []
    for r in rows:
        k = tuple(r.get(c) for c in keys)
        if k not in agg:
            agg[k] = {c: r.get(c) for c in keys}
            agg[k][y_col] = 0
            order.append(k)
        val = r.get(y_col)
        if isinstance(val, (int, float)):
            agg[k][y_col] += val
    return [agg[k] for k in order]


def _normalize_percent(data: list[dict[str, Any]], y_col: str) -> list[dict[str, Any]]:
    """Re-express an aggregated measure as % of its total (0-100), so a share
    metric broken down by one dimension reads as a composition that sums to 100%
    instead of a meaningless sum of pre-computed shares (e.g. 6000%)."""
    total = sum(r.get(y_col) for r in data if isinstance(r.get(y_col), (int, float)))
    if not total:
        return data
    out = []
    for r in data:
        nr = dict(r)
        v = r.get(y_col)
        if isinstance(v, (int, float)):
            nr[y_col] = v / total * 100.0
        out.append(nr)
    return out


def _first_non_constant_categorical(
    rows: list[dict[str, Any]], y_col: str, preferred: str = ""
) -> str:
    """The categorical column to rank/group by: prefer `preferred` if it actually
    varies; otherwise the first non-measure column with >1 distinct value. This is
    what stops a top-N chart from ranking by a filter-pinned constant (one bar)."""
    if not rows:
        return preferred
    def varies(col: str) -> bool:
        return col in rows[0] and len({str(r.get(col)) for r in rows}) > 1
    if preferred and varies(preferred):
        return preferred
    for col, val in rows[0].items():
        if col == y_col or isinstance(val, (int, float)):
            continue
        if varies(col):
            return col
    return preferred or next(iter(rows[0]), "")


def _cap_categories(
    rows: list[dict[str, Any]], x_col: str, color_col: str | None, y_col: str,
    *, max_x: int = 12, max_series: int = 6,
) -> list[dict[str, Any]]:
    """Cap a dense categorical chart: keep the top `max_x` x-categories by total
    measure (rest -> 'Other'), and the top `max_series` color series (rest ->
    'Other'). Keeps stacked/share charts readable instead of hair-thin slivers."""
    def _totals(col: str) -> list[str]:
        tot: dict[str, float] = {}
        for r in rows:
            k = str(r.get(col))
            v = r.get(y_col)
            tot[k] = tot.get(k, 0) + (v if isinstance(v, (int, float)) else 0)
        return [k for k, _ in sorted(tot.items(), key=lambda kv: kv[1], reverse=True)]
    keep_x = set(_totals(x_col)[:max_x]) if x_col else set()
    keep_s = set(_totals(color_col)[:max_series]) if color_col else set()
    out: list[dict[str, Any]] = []
    for r in rows:
        nr = dict(r)
        if x_col and str(r.get(x_col)) not in keep_x:
            nr[x_col] = "Other"
        if color_col and str(r.get(color_col)) not in keep_s:
            nr[color_col] = "Other"
        out.append(nr)
    # Re-aggregate after relabelling so the 'Other' buckets merge.
    return _aggregate_rows(out, x_col, color_col, y_col)


def _figure_from_spec(
    spec: DashboardSpec, rows: list[dict[str, Any]], *, show_title: bool = True
) -> go.Figure:
    """Build a Plotly figure from a merged spec and live rows. Always returns a figure."""
    config = spec.config
    chart_type = str(config.get("chart_type") or "bar")
    # V1: the card/page header already shows the KPI title — suppress the in-figure
    # title for embedded views so it is not duplicated.
    title = str(config.get("title") or spec.kpi_id) if show_title else ""
    color = str(config.get("color") or "")

    if not rows:
        fig = go.Figure()
        fig.add_annotation(text="(no rows returned by KPI SQL)", showarrow=False, x=0.5, y=0.5)
        fig.update_layout(title=title)
        return _apply_corporate_theme(fig)

    if chart_type == "big_number":
        first_row = rows[0] if rows else {}
        value = next(iter(first_row.values()), "—") if first_row else "—"
        fig = go.Figure(
            go.Indicator(
                mode="number",
                value=value if isinstance(value, (int, float)) else 0,
                title={"text": title},
            )
        )
        return _apply_corporate_theme(fig)

    x_col = str(config.get("x") or "")
    y_col = str(config.get("y") or "")
    if x_col not in (rows[0] if rows else {}):
        x_col = next(iter(rows[0].keys())) if rows else x_col
    candidate_y = [
        col for col, val in (rows[0] if rows else {}).items()
        if isinstance(val, (int, float))
    ]
    if y_col not in (rows[0] if rows else {}):
        y_col = candidate_y[0] if candidate_y else (list(rows[0].keys())[1] if len(rows[0]) > 1 else x_col)
    color_col = color if color and color in (rows[0] if rows else {}) else None

    y_is_percent = str(config.get("y_format") or "").lower() == "percent"
    # Palette by series count (decision 4): single-series -> editorial accent;
    # multi-series (a color split, or always-multi stacked-percent) -> the
    # colorblind-safe categorical ramp so series are genuinely separable. Forced
    # onto px traces via color_discrete_sequence (layout.colorway alone does not
    # color single-series bars — px would fall back to its default blue).
    is_multi = bool(color_col) or chart_type == "stacked_bar_percent"
    seq = list(_ACTIVE.categorical) if is_multi else [_ACTIVE.accent]

    try:
        if chart_type == "line":
            # V2: aggregate the measure by period (and optional series) so a trend
            # is one value per x, not a scatter of every raw row.
            data = _aggregate_rows(rows, x_col, color_col, y_col)
            fig = px.line(
                data, x=x_col, y=y_col, color=color_col, title=title, markers=True,
                color_discrete_sequence=seq,
            )
        elif chart_type == "ranked_bar":
            # V4: rank by a NON-constant categorical (a filter-pinned constant like a
            # single LOB would collapse the chart to one bar). Aggregate the measure
            # per entity, sort desc, limit to N. Plotly draws the first category at the
            # bottom, so reverse to put the largest bar on top.
            rank_col = _first_non_constant_categorical(rows, y_col, preferred=x_col)
            data = _aggregate_rows(rows, rank_col, None, y_col)
            if y_is_percent:
                data = _normalize_percent(data, y_col)
            limit = int(config.get("limit") or 10)
            ordered = sorted(data, key=lambda r: r.get(y_col) or 0, reverse=True)[:limit]
            ordered = list(reversed(ordered))
            fig = px.bar(
                ordered, x=y_col, y=rank_col, orientation="h", title=title,
                color_discrete_sequence=seq,
            )
        elif chart_type == "stacked_bar_percent":
            # V3: aggregate by (x, color) so each stack is a true 0-100%, and cap dense
            # categoricals (top-N x + top series, rest -> 'Other') so bars are readable.
            data = _cap_categories(rows, x_col, color_col, y_col)
            fig = px.bar(
                data, x=x_col, y=y_col, color=color_col, title=title,
                color_discrete_sequence=seq,
            )
            fig.update_layout(barmode="stack", barnorm="percent")
        elif chart_type == "grouped_bar":
            data = _aggregate_rows(rows, x_col, color_col, y_col)
            fig = px.bar(
                data, x=x_col, y=y_col, color=color_col, barmode="group", title=title,
                color_discrete_sequence=seq,
            )
        else:
            data = _aggregate_rows(rows, x_col, color_col, y_col)
            if y_is_percent and not color_col:
                data = _normalize_percent(data, y_col)
            fig = px.bar(data, x=x_col, y=y_col, color=color_col, title=title,
                         color_discrete_sequence=seq)
    except Exception as exc:
        fig = go.Figure()
        fig.add_annotation(text=f"(chart render failed: {exc})", showarrow=False, x=0.5, y=0.5)
        fig.update_layout(title=title)
        return _apply_corporate_theme(fig)

    # Percent axes: our percent values are in PERCENT UNITS (0-100), so a Plotly
    # ".0%" tickformat (which expects 0-1 fractions) would x100 again -> 5000%.
    # Always use a plain "%" suffix on the measure axis instead.
    if y_is_percent or chart_type == "stacked_bar_percent":
        measure_axis = "x" if chart_type == "ranked_bar" else "y"
        if measure_axis == "y":
            fig.update_yaxes(ticksuffix="%")
        else:
            fig.update_xaxes(ticksuffix="%")
        if chart_type == "stacked_bar_percent":
            fig.update_yaxes(range=[0, 100])
    # Adaptive log scale (data-derived in profile.decide_panels): apply to the
    # MEASURE axis — x for horizontal ranked_bar, y otherwise. Never combined with
    # a percent axis (a 0-100 share is not log).
    if config.get("log_scale") and not (y_is_percent or chart_type == "stacked_bar_percent"):
        if chart_type == "ranked_bar":
            fig.update_xaxes(type="log")
        else:
            fig.update_yaxes(type="log")
    # V5: rotate long categorical x-tick labels so they do not clip the card
    # (vertical charts only; ranked_bar puts categories on the y-axis).
    if chart_type in ("bar", "grouped_bar", "stacked_bar_percent"):
        fig.update_xaxes(tickangle=-35, automargin=True)
        fig.update_yaxes(automargin=True)
    return _apply_corporate_theme(fig)


def _detect_artifact_dialect(repo_root: Path, layout: WorkspaceLayout, kpi_id: str) -> str:
    """Return 'sql' if a .sql artifact exists, 'polars'/'pyspark' if a .py marker file
    exists, or 'unknown' otherwise.

    Generic detection — checks the workspace's solutions_dir directly. Falls back to
    `workspace_settings.json` `output_dialect` when artifact-on-disk is ambiguous.
    """
    solutions = layout.solutions_dir
    if (solutions / f"{kpi_id}.sql").exists():
        return "sql"
    polars_file = solutions / f"{kpi_id}.polars.py"
    pyspark_file = solutions / f"{kpi_id}.pyspark.py"
    if polars_file.exists():
        return "polars"
    if pyspark_file.exists():
        return "pyspark"
    settings = {}
    try:
        settings = layout.load_settings() or {}
    except Exception:
        settings = {}
    declared = str(settings.get("output_dialect") or "").lower().strip()
    if declared in {"sql", "polars", "pyspark"}:
        return declared
    return "unknown"


def _non_sql_dialect_card(kpi_id: str, dialect: str, source_path: str, spec: DashboardSpec) -> dbc.Card:
    return dbc.Card(
        [
            dbc.CardHeader(
                [
                    html.H5(spec.config.get("title") or kpi_id, className="mb-1"),
                    dbc.Badge(
                        f"{dialect} runtime",
                        color="secondary",
                        className="ms-1",
                    ),
                ]
            ),
            dbc.CardBody(
                [
                    html.P(
                        f"This KPI is generated as {dialect.upper()} and the live dashboard "
                        "renderer currently only embeds DuckDB SQL results.",
                        className="text-muted",
                    ),
                    html.P(
                        [
                            "Run the source artifact in its native runtime to see results: ",
                            html.Code(source_path or "(no source)"),
                        ],
                        className="small",
                    ),
                    html.P(
                        "Set workspace_settings.output_dialect = sql, regenerate, and refresh "
                        "the dashboard to render the chart here.",
                        className="small text-muted",
                    ),
                ]
            ),
        ],
        className="mb-3 shadow-sm border-secondary",
    )


def _build_kpi_figure(
    repo_root: Path,
    layout: WorkspaceLayout,
    spec: DashboardSpec,
    *,
    start: date | None = None,
    end: date | None = None,
) -> go.Figure:
    sql_path_rel = str(spec.config.get("sql_path") or "")
    sql_path = (repo_root / sql_path_rel).resolve() if sql_path_rel else None
    view_name = f"{spec.kpi_id}_results"
    rows = _execute_sql_view(repo_root, sql_path, view_name) if sql_path else []
    date_col = _spec_date_column(spec)
    if date_col:
        rows = _filter_rows_by_date(rows, date_col, start, end)
    return _figure_from_spec(spec, rows)


def _kpi_chart_card(repo_root: Path, layout: WorkspaceLayout, spec: DashboardSpec) -> dbc.Card:
    dialect = _detect_artifact_dialect(repo_root, layout, spec.kpi_id)
    if dialect not in {"sql", "unknown"}:
        source_relative = ""
        candidate = layout.solutions_dir / f"{spec.kpi_id}.{dialect}.py"
        if candidate.exists():
            try:
                source_relative = candidate.relative_to(repo_root).as_posix()
            except ValueError:
                source_relative = candidate.as_posix()
        return _non_sql_dialect_card(spec.kpi_id, dialect, source_relative, spec)
    fig = _build_kpi_figure(repo_root, layout, spec)
    definition = spec.config.get("definition") or {}
    has_date_axis = bool(_spec_date_column(spec))
    return dbc.Card(
        [
            dbc.CardHeader(
                [
                    html.H5(spec.config.get("title") or spec.kpi_id, className="mb-1"),
                    html.Small(definition.get("metric") or "", className="text-muted"),
                ]
            ),
            dbc.CardBody(
                [
                    dcc.Graph(
                        id={"role": "kpi-chart", "kpi_id": spec.kpi_id},
                        figure=fig,
                        config={"displaylogo": False},
                    ),
                    html.Div(
                        [
                            html.Span("Cuts: ", className="text-muted"),
                            html.Code(str(definition.get("cuts") or "—")),
                            html.Span(
                                " · Listens to global date filter"
                                if has_date_axis
                                else " · No date axis (global filter does not apply)",
                                className="ms-2 text-muted small",
                            ),
                        ],
                        className="mt-2 small",
                    ),
                ]
            ),
            dbc.CardFooter(
                html.Small(
                    f"spec: {spec.spec_path}",
                    className="text-muted",
                )
            ),
        ],
        className="mb-3 shadow-sm",
    )


def _kpi_blocker_card(kpi_id: str, gap: dict[str, Any]) -> dbc.Card:
    blockers = gap.get("blockers") or []
    recovery = gap.get("recovery_commands") or []
    blocker_summary = ", ".join(
        str(b.get("code", b) if isinstance(b, dict) else b) for b in blockers
    ) or "blocked"
    recovery_items = [
        html.Li(
            [
                html.Strong(cmd.get("label") or cmd.get("why") or "Action"),
                html.Pre(
                    cmd.get("command") or "",
                    className="bg-light p-2 mt-1 small mb-0",
                    style={"whiteSpace": "pre-wrap"},
                ),
            ]
        )
        for cmd in recovery
    ] or [html.Li("No recovery commands surfaced. Run `workspace-flow status --diff` to refresh.")]
    return dbc.Card(
        [
            dbc.CardHeader(
                [
                    html.H5(kpi_id, className="mb-1"),
                    dbc.Badge(blocker_summary, color="warning", className="ms-1"),
                ]
            ),
            dbc.CardBody(
                [
                    html.Div("This KPI has no executable SQL yet.", className="text-muted mb-2"),
                    html.Strong("Recovery commands:"),
                    html.Ul(recovery_items, className="mt-1"),
                ]
            ),
        ],
        className="mb-3 border-warning shadow-sm",
    )


def _index_card_grid(cards: list[Any]) -> dbc.Container:
    rows = []
    for i in range(0, len(cards), 2):
        rows.append(
            dbc.Row(
                [dbc.Col(card, md=6) for card in cards[i:i + 2]],
                className="mb-1",
            )
        )
    return dbc.Container(rows, fluid=True)


def build_dash_app(repo_root: Path, workspace_rel: str) -> dash.Dash:
    """Build the per-workspace BI Dash app.

    Caller is responsible for `app.run(...)`. Static export uses the same
    component tree via `export_static_html`.
    """
    repo_root = Path(repo_root).resolve()
    workspace = (repo_root / workspace_rel).resolve()
    layout = WorkspaceLayout(project_root=workspace)

    app = dash.Dash(
        __name__,
        external_stylesheets=[dbc.themes.BOOTSTRAP],
        title=f"Dashboard — {workspace.name}",
    )

    definitions = load_kpi_definitions(layout)
    diff = compute_workflow_diff(repo_root, workspace_rel)
    gaps_by_id = {str(g.get("kpi_id")): g for g in (diff.get("kpi_gaps") or [])}

    cards = []
    date_aware_specs: dict[str, DashboardSpec] = {}
    for kpi_id, definition in sorted(definitions.items()):
        spec = load_kpi_spec(layout, kpi_id)
        gap = gaps_by_id.get(kpi_id) or {}
        is_blocked = str(gap.get("status")) == "blocked" or not spec
        if is_blocked or not (spec and spec.config.get("sql_path")):
            cards.append(_kpi_blocker_card(kpi_id, gap or {"blockers": ["spec_missing"], "recovery_commands": []}))
        else:
            cards.append(_kpi_chart_card(repo_root, layout, spec))
            if _spec_date_column(spec):
                date_aware_specs[kpi_id] = spec

    has_date_filter = bool(date_aware_specs)

    header_children = [
        html.H2(f"Workspace Dashboard — {workspace.name}", className="mt-3"),
        html.P(
            "Live KPI charts. Each card re-executes its KPI SQL against the "
            "workspace datasets on every render. Spec lives in `dashboard/<kpi_id>.json`; "
            "edit the `user_overrides` section to customize.",
            className="text-muted",
        ),
    ]
    if has_date_filter:
        header_children.append(
            dbc.Card(
                dbc.CardBody(
                    [
                        html.Strong("Global date range filter"),
                        html.Div(
                            "Applies to every KPI whose x-axis is a date column. "
                            "KPIs without a date axis are unaffected.",
                            className="small text-muted mb-2",
                        ),
                        dcc.DatePickerRange(
                            id="global-date-range",
                            display_format="YYYY-MM-DD",
                            clearable=True,
                            persistence=True,
                            persistence_type="session",
                        ),
                    ]
                ),
                className="mb-3",
            )
        )
    header = dbc.Container(header_children, fluid=True)

    if not cards:
        cards = [
            dbc.Alert(
                "No KPIs registered in this workspace yet. Run "
                "`uv run workspace-flow start --workspace <ws>` first.",
                color="info",
            )
        ]

    app.layout = dbc.Container(
        [header, _index_card_grid(cards)],
        fluid=True,
        className="pb-5",
    )

    if has_date_filter:
        @app.callback(
            Output({"role": "kpi-chart", "kpi_id": ALL}, "figure"),
            Input("global-date-range", "start_date"),
            Input("global-date-range", "end_date"),
            Input({"role": "kpi-chart", "kpi_id": ALL}, "id"),
        )
        def _apply_global_date_range(start_date_str, end_date_str, chart_ids):
            start = _coerce_date(start_date_str) if start_date_str else None
            end = _coerce_date(end_date_str) if end_date_str else None
            figures = []
            for chart_id in chart_ids:
                kpi_id = chart_id.get("kpi_id") if isinstance(chart_id, dict) else None
                spec = date_aware_specs.get(kpi_id) if kpi_id else None
                if not spec:
                    figures.append(no_update)
                    continue
                figures.append(_build_kpi_figure(repo_root, layout, spec, start=start, end=end))
            return figures

    return app


def render_kpi_html(repo_root: Path, layout: WorkspaceLayout, spec: DashboardSpec) -> str:
    """Render a single KPI's chart as a standalone HTML fragment."""
    sql_path_rel = str(spec.config.get("sql_path") or "")
    sql_path = (repo_root / sql_path_rel).resolve() if sql_path_rel else None
    view_name = f"{spec.kpi_id}_results"
    rows = _execute_sql_view(repo_root, sql_path, view_name) if sql_path else []
    fig = _figure_from_spec(spec, rows, show_title=False)
    return fig.to_html(
        include_plotlyjs="cdn",
        full_html=False,
        div_id=f"chart_{spec.kpi_id}",
        config={"responsive": True, "displayModeBar": False},
        default_width="100%",
    )


def _format_measure(value: Any, y_col: str, *, percent: bool) -> str:
    """Format a single measure value for a headline: percent / currency / count.

    Currency/percent are inferred from the measure COLUMN NAME (generic business
    terms only), never from a workspace domain.
    """
    if not isinstance(value, (int, float)):
        return str(value)
    name = (y_col or "").lower()
    if percent or "percent" in name or "share" in name or "rate" in name or "ratio" in name:
        return f"{value:.1f}%" if value > 1 else f"{value * 100:.1f}%"
    is_currency = any(t in name for t in ("amount", "paid", "revenue", "cost", "price", "spend", "sales"))
    prefix = "$" if is_currency else ""
    if abs(value) >= 1_000_000:
        return f"{prefix}{value / 1_000_000:.1f}M"
    if abs(value) >= 1_000:
        return f"{prefix}{value / 1_000:.1f}K"
    return f"{prefix}{value:,.0f}" if value == int(value) else f"{prefix}{value:,.2f}"


def _kpi_headline(rows: list[dict[str, Any]], spec: DashboardSpec) -> str:
    """One representative number/label for a KPI card (generic across chart types)."""
    if not rows:
        return "—"
    config = spec.config
    chart_type = str(config.get("chart_type") or "bar")
    y_col = str(config.get("y") or "")
    if y_col not in rows[0]:
        numeric = [c for c, v in rows[0].items() if isinstance(v, (int, float))]
        y_col = numeric[0] if numeric else ""
    percent = str(config.get("y_format") or "").lower() == "percent"
    is_share = percent or any(t in y_col.lower() for t in ("percent", "share", "ratio", "rate"))
    vals = [r.get(y_col) for r in rows if isinstance(r.get(y_col), (int, float))]
    if chart_type == "big_number":
        return _format_measure(next(iter(rows[0].values()), "—"), y_col, percent=percent)
    if chart_type == "ranked_bar" and vals:
        # Match V4: rank by the same non-constant categorical the chart uses, then
        # aggregate so the headline reflects the true top entity (not a filter constant).
        rank_col = _first_non_constant_categorical(rows, y_col, preferred=str(config.get("x") or ""))
        agg = _aggregate_rows(rows, rank_col, None, y_col)
        top = max(agg, key=lambda r: r.get(y_col) or 0)
        label = str(top.get(rank_col, "")) if rank_col in top else ""
        return f"{label}: {_format_measure(top.get(y_col), y_col, percent=percent)}".strip(": ")
    if is_share:
        # Summing percentages is meaningless, and the fraction-vs-percent-unit of a
        # share column can't be inferred reliably — so headline the breadth instead.
        x_col = str(config.get("x") or "")
        if x_col not in rows[0]:
            # Same fallback the figure uses: first non-measure (categorical) column.
            non_measure = [c for c, v in rows[0].items() if not isinstance(v, (int, float))]
            x_col = non_measure[0] if non_measure else (next(iter(rows[0]), ""))
        segments = len({str(r.get(x_col)) for r in rows if x_col in r}) if x_col else len(rows)
        return f"{segments} segments"
    if vals:
        return _format_measure(sum(vals), y_col, percent=percent)
    return "—"


def _panel_spec(spec: DashboardSpec, panel: dict[str, Any]) -> DashboardSpec:
    """A throwaway single-chart spec for one data-derived panel."""
    config = dict(panel)
    config.setdefault("definition", spec.config.get("definition") or {})
    return DashboardSpec(
        kpi_id=spec.kpi_id, config=config, machine_defaults=config,
        user_overrides={}, spec_path=spec.spec_path,
    )


def _panel_html(spec: DashboardSpec, panel: dict[str, Any], rows: list[dict[str, Any]],
                idx: int, height: int) -> dict[str, Any]:
    fig = _figure_from_spec(_panel_spec(spec, panel), rows, show_title=False)
    fig.update_xaxes(title_text="")
    fig.update_yaxes(title_text="")
    fig.update_layout(height=height)
    return {
        "chart_html": fig.to_html(
            include_plotlyjs="cdn", full_html=False,
            div_id=f"chart_{spec.kpi_id}_{idx}",
            config={"responsive": True, "displayModeBar": False},
            default_width="100%",
        ),
        "sub_title": str(panel.get("title") or ""),
        "chart_type": str(panel.get("chart_type") or ""),
    }


def render_kpi_inline(
    repo_root: Path, layout: WorkspaceLayout, spec: DashboardSpec, *, height: int = 300
) -> dict[str, Any]:
    """Render a KPI as an inline grid-card payload: one or MORE panels + headline.

    When the spec carries data-derived `panels` (one chart per informative
    dimension), each is rendered; otherwise a single chart is rendered (legacy
    path). One SQL execution feeds every panel and the headline.
    """
    sql_path_rel = str(spec.config.get("sql_path") or "")
    sql_path = (repo_root / sql_path_rel).resolve() if sql_path_rel else None
    view_name = f"{spec.kpi_id}_results"
    rows = _execute_sql_view(repo_root, sql_path, view_name) if sql_path else []
    definition = spec.config.get("definition") or {}

    panels = spec.config.get("panels") or []
    if panels:
        # Smaller per-panel height when there are multiple, so the card stays compact.
        per_h = height if len(panels) == 1 else max(220, int(height * 0.8))
        rendered = [_panel_html(spec, p, rows, i, per_h) for i, p in enumerate(panels)]
    else:
        fig = _figure_from_spec(spec, rows, show_title=False)
        fig.update_xaxes(title_text="")
        fig.update_yaxes(title_text="")
        fig.update_layout(height=height)
        rendered = [{
            "chart_html": fig.to_html(
                include_plotlyjs="cdn", full_html=False, div_id=f"chart_{spec.kpi_id}",
                config={"responsive": True, "displayModeBar": False}, default_width="100%",
            ),
            "sub_title": "",
            "chart_type": str(spec.config.get("chart_type") or ""),
        }]

    return {
        "panels": rendered,
        "chart_html": rendered[0]["chart_html"],  # back-compat (first panel)
        "headline": _kpi_headline(rows, spec),
        "metric": str(definition.get("metric") or ""),
        "title": str(spec.config.get("title") or spec.kpi_id),
        "chart_type": str(spec.config.get("chart_type") or ""),
    }


__all__ = [
    "render_kpi_inline",
    "set_active_design",
    "build_dash_app",
    "render_kpi_html",
]
