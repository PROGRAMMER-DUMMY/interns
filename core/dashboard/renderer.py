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


def _figure_from_spec(spec: DashboardSpec, rows: list[dict[str, Any]]) -> go.Figure:
    """Build a Plotly figure from a merged spec and live rows. Always returns a figure."""
    config = spec.config
    chart_type = str(config.get("chart_type") or "bar")
    title = str(config.get("title") or spec.kpi_id)
    color = str(config.get("color") or "")

    if not rows:
        fig = go.Figure()
        fig.add_annotation(text="(no rows returned by KPI SQL)", showarrow=False, x=0.5, y=0.5)
        fig.update_layout(title=title, template="plotly_white")
        return fig

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
        fig.update_layout(template="plotly_white")
        return fig

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

    try:
        if chart_type == "line":
            fig = px.line(
                rows, x=x_col, y=y_col, color=color_col, title=title, markers=True,
            )
        elif chart_type == "ranked_bar":
            limit = int(config.get("limit") or 10)
            ordered = sorted(rows, key=lambda r: r.get(y_col) or 0, reverse=True)[:limit]
            fig = px.bar(ordered, x=x_col, y=y_col, color=color_col, title=title)
        elif chart_type == "grouped_bar":
            fig = px.bar(
                rows, x=x_col, y=y_col, color=color_col, barmode="group", title=title,
            )
        else:
            fig = px.bar(rows, x=x_col, y=y_col, color=color_col, title=title)
    except Exception as exc:
        fig = go.Figure()
        fig.add_annotation(text=f"(chart render failed: {exc})", showarrow=False, x=0.5, y=0.5)
        fig.update_layout(title=title, template="plotly_white")
        return fig

    fig.update_layout(
        template="plotly_white",
        margin={"l": 40, "r": 20, "t": 50, "b": 40},
    )
    return fig


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
    fig = _figure_from_spec(spec, rows)
    return fig.to_html(include_plotlyjs="cdn", full_html=False, div_id=f"chart_{spec.kpi_id}")


__all__ = [
    "build_dash_app",
    "render_kpi_html",
]
