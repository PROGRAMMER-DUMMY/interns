"""Dash renderer: live SQL re-execution + Plotly chart per spec.

Workspace-agnostic. Takes a `WorkspaceLayout`, reads every KPI's spec,
re-executes its generated SQL via DuckDB against the workspace datasets,
and renders a Plotly figure per spec. Blocked KPIs render a blocker card
with the recovery commands from `compute_workflow_diff`.
"""
from __future__ import annotations

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
from dash import ALL, Input, Output, State, ctx, dcc, html, no_update

from core.dashboard.design_md import DesignTokens, load_design_tokens
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

# Safety cap on rows fetched from a KPI result view into the browser. KPI views are
# pre-aggregated (GROUP BY) so they're small; this guards against a pathological
# high-cardinality view and keeps raw/large data server-side (2e scale guardrail).
_SAMPLE_CAP = 5000
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


_LEADING_NUM_RE = re.compile(r"^\s*(-?\d+(?:\.\d+)?)")


def _categorical_order(values: list[str]) -> tuple[str, list[str] | None]:
    """Decide the display order for a categorical axis, generically:

    * ORDINAL / banded (every category starts with a number — "0-9","10-19","60")
      -> sort by that leading number ('array' order, ascending). Fixes age bands /
      ranges / numeric buckets rendering in arbitrary first-appearance order.
    * NOMINAL (labels with no leading number — departments, gender, visit type)
      -> 'total descending' so the biggest bar is first (readability).

    Returns (categoryorder, categoryarray|None) for Plotly axis config.
    """
    uniq = [v for v in dict.fromkeys(str(x) for x in values)]
    if not uniq:
        return "trace", None
    nums = [_LEADING_NUM_RE.match(v) for v in uniq]
    if all(nums):
        ordered = sorted(uniq, key=lambda v: float(_LEADING_NUM_RE.match(v).group(1)))
        return "array", ordered
    return "total descending", None


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
    is_multi = bool(color_col) or chart_type in ("stacked_bar_percent", "donut")
    seq = list(_ACTIVE.categorical) if is_multi else [_ACTIVE.accent]
    # Per-category coloring (donuts, low-cardinality bars) always uses the
    # colorblind-safe ramp regardless of series count.
    ramp = list(_ACTIVE.categorical)

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
            # Rank entities can be numeric (age, year). Stringify + force a
            # category axis, or Plotly places bars on a CONTINUOUS numeric
            # axis — thin barcode lines at y=53, y=55, ... instead of ranked
            # category bars.
            ordered = [{**r, rank_col: str(r.get(rank_col))} for r in ordered]
            fig = px.bar(
                ordered, x=y_col, y=rank_col, orientation="h", title=title,
                color_discrete_sequence=seq,
            )
            fig.update_yaxes(type="category")
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
        elif chart_type == "scatter":
            # Two numeric measures per row: relationship view.
            fig = px.scatter(
                rows, x=x_col, y=y_col, title=title,
                color_discrete_sequence=[_ACTIVE.accent], opacity=0.55,
            )
        elif chart_type == "histogram":
            # Row-level values: the distribution is the story.
            fig = px.histogram(
                rows, x=x_col, title=title,
                color_discrete_sequence=[_ACTIVE.accent], nbins=30,
            )
        elif chart_type == "bubble_map":
            lat_col = str(config.get("lat") or "lat")
            lon_col = str(config.get("lon") or "lon")
            # Bubble size must be non-negative (data-to-viz: map value to AREA);
            # negative measures (e.g. temperatures) render as plain points.
            sizable = all(
                isinstance(r.get(y_col), (int, float)) and r.get(y_col) >= 0
                for r in rows[:64]
            ) and bool(rows)
            fig = px.scatter_geo(
                rows, lat=lat_col, lon=lon_col,
                size=y_col if sizable else None,
                title=title, color_discrete_sequence=[_ACTIVE.accent],
            )
            fig.update_geos(fitbounds="locations", bgcolor="rgba(0,0,0,0)")
        elif chart_type == "lollipop":
            # data-to-viz: many bars of SIMILAR height read better as
            # lollipops — the dot marks the value without redundant bar ink.
            rank_col = _first_non_constant_categorical(rows, y_col, preferred=x_col)
            data = _aggregate_rows(rows, rank_col, None, y_col)
            if y_is_percent:
                data = _normalize_percent(data, y_col)
            limit = int(config.get("limit") or 10)
            ordered = sorted(data, key=lambda r: r.get(y_col) or 0, reverse=True)[:limit]
            ordered = list(reversed(ordered))
            cats = [str(r.get(rank_col)) for r in ordered]
            vals = [r.get(y_col) or 0 for r in ordered]
            fig = go.Figure()
            for cat, val in zip(cats, vals):
                fig.add_trace(go.Scatter(
                    x=[0, val], y=[cat, cat], mode="lines",
                    line={"color": _GRID_COLOR, "width": 2}, showlegend=False,
                ))
            fig.add_trace(go.Scatter(
                x=vals, y=cats, mode="markers",
                marker={"color": _ACTIVE.accent, "size": 11}, showlegend=False,
            ))
            fig.update_layout(title=title)
            fig.update_yaxes(type="category")
        elif chart_type == "treemap":
            # Part-to-whole across more categories than a donut holds legibly.
            data = _aggregate_rows(rows, x_col, None, y_col)
            data = [r for r in data if isinstance(r.get(y_col), (int, float)) and r[y_col] > 0]
            fig = px.treemap(
                data, path=[x_col], values=y_col, title=title,
                color_discrete_sequence=ramp,
            )
            fig.update_traces(textinfo="label+percent root")
        elif chart_type == "heatmap":
            # Two categorical dimensions x one measure: the interaction view.
            data = _aggregate_rows(rows, x_col, color_col, y_col)
            xs = sorted({str(r.get(x_col)) for r in data})
            ys = sorted({str(r.get(color_col)) for r in data})
            lookup = {
                (str(r.get(x_col)), str(r.get(color_col))): r.get(y_col)
                for r in data
            }
            z = [[lookup.get((x, y)) for x in xs] for y in ys]
            fig = go.Figure(go.Heatmap(
                z=z, x=xs, y=ys,
                colorscale=[[0, "#f6f1e7"], [1, _ACTIVE.accent]],
                colorbar={"thickness": 12, "outlinewidth": 0},
            ))
            fig.update_layout(title=title)
        elif chart_type == "stacked_area":
            # Few-series share evolution: composition over time.
            data = _aggregate_rows(rows, x_col, color_col, y_col)
            fig = px.area(
                data, x=x_col, y=y_col, color=color_col, title=title,
                color_discrete_sequence=list(_ACTIVE.categorical),
            )
        elif chart_type == "donut":
            # Part-of-whole for a LOW-cardinality breakdown of a share metric:
            # a donut reads composition at a glance and gives each slice its
            # own ramp color. The label+percent is on the slice, so the legend
            # is redundant noise.
            data = _aggregate_rows(rows, x_col, None, y_col)
            fig = px.pie(
                data, names=x_col, values=y_col, title=title, hole=0.45,
                color_discrete_sequence=seq,
            )
            fig.update_traces(textinfo="label+percent", textposition="inside")
            fig.update_layout(showlegend=False)
        else:
            data = _aggregate_rows(rows, x_col, color_col, y_col)
            if y_is_percent and not color_col:
                data = _normalize_percent(data, y_col)
            if not color_col and chart_type == "bar" and len(data) <= 8:
                # Low-cardinality single-series bars: one ramp color per
                # category (the axis already names them — no legend), so
                # side-by-side panels aren't a wall of one accent color.
                fig = px.bar(data, x=x_col, y=y_col, color=x_col, title=title,
                             color_discrete_sequence=ramp)
                fig.update_layout(showlegend=False)
            else:
                fig = px.bar(data, x=x_col, y=y_col, color=color_col, title=title,
                             color_discrete_sequence=seq)
    except Exception as exc:
        fig = go.Figure()
        fig.add_annotation(text=f"(chart render failed: {exc})", showarrow=False, x=0.5, y=0.5)
        fig.update_layout(title=title)
        return _apply_corporate_theme(fig)

    # Percent axes: our percent values are in PERCENT UNITS (0-100), so a Plotly
    # ".0%" tickformat (which expects 0-1 fractions) would x100 again -> 5000%.
    # Always use a plain "%" suffix on the MEASURE axis instead — and only on
    # charts whose measure IS an axis: a heatmap's measure is the color, a
    # donut/treemap's is the slice/tile, so suffixing their (category) axes
    # mislabels the categories ("80-89" -> "80-89%").
    _NO_AXIS_MEASURE = ("heatmap", "donut", "treemap", "bubble_map")
    if (y_is_percent or chart_type == "stacked_bar_percent") and chart_type not in _NO_AXIS_MEASURE:
        measure_axis = "x" if chart_type in ("ranked_bar", "lollipop") else "y"
        if measure_axis == "y":
            fig.update_yaxes(ticksuffix="%")
        else:
            fig.update_xaxes(ticksuffix="%")
        if chart_type == "stacked_bar_percent":
            fig.update_yaxes(range=[0, 100])
    # Adaptive log scale (data-derived in profile.decide_panels): LINE charts
    # only. A log axis on any BAR chart makes bar length non-proportional to
    # value (and the bar base lands at the axis minimum, exaggerating spreads),
    # so bars always stay linear-from-zero regardless of the spec flag.
    if (
        config.get("log_scale")
        and chart_type == "line"
        and not (y_is_percent or chart_type == "stacked_bar_percent")
    ):
        fig.update_yaxes(type="log")
    # Category ORDER (correctness): vertical-bar x-axis. Ordinal/banded categories
    # (age bands, numeric buckets) sort by their natural numeric value; plain nominal
    # categories sort by measure descending. ranked_bar already sorts by value; line
    # over time stays chronological.
    if chart_type in ("bar", "grouped_bar", "stacked_bar_percent"):
        try:
            cats = [r.get(x_col) for r in data if x_col in r]
            order, arr = _categorical_order([c for c in cats if c is not None])
            if order == "array" and arr:
                fig.update_xaxes(categoryorder="array", categoryarray=arr)
            elif order == "total descending" and chart_type != "stacked_bar_percent":
                fig.update_xaxes(categoryorder="total descending")
        except Exception:
            pass
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


def _kpi_render_data(repo_root: Path, layout: WorkspaceLayout, spec: DashboardSpec) -> dict[str, Any]:
    """Execute the KPI's SQL ONCE and build its data-derived panel figures +
    headline. Returns figures (not HTML) for the live Dash app. Each panel is
    (title, figure); falls back to a single chart when the spec has no `panels`.
    """
    sql_path_rel = str(spec.config.get("sql_path") or "")
    sql_path = (repo_root / sql_path_rel).resolve() if sql_path_rel else None
    rows = _execute_sql_view(repo_root, sql_path, f"{spec.kpi_id}_results") if sql_path else []
    definition = spec.config.get("definition") or {}
    panels: list[tuple[str, go.Figure]] = []
    panels_cfg = spec.config.get("panels") or []
    if panels_cfg:
        for i, p in enumerate(panels_cfg):
            fig = _figure_from_spec(_panel_spec(spec, p), rows, show_title=False)
            panels.append((str(p.get("title") or f"View {i + 1}"), fig))
    else:
        panels.append((str(spec.config.get("title") or spec.kpi_id),
                       _figure_from_spec(spec, rows, show_title=False)))
    return {
        "kpi_id": spec.kpi_id,
        "title": str(spec.config.get("title") or spec.kpi_id),
        "metric": str(definition.get("metric") or ""),
        "cuts": str(definition.get("cuts") or ""),
        "headline": _kpi_headline(rows, spec),
        "panels": panels,
    }


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


def _dash_index_string(t: DesignTokens) -> str:
    """Editorial CSS shell for the live app, generated from DESIGN.md tokens.
    Overview strip + drill detail are sized to fit the viewport (no page scroll;
    the detail grid is the only scroll region when a KPI has many panels)."""
    fams = "&".join(f"family={f}" for f in t.font_families)
    fonts = (f'<link href="https://fonts.googleapis.com/css2?{fams}&display=swap" rel="stylesheet">'
             if fams else "")
    css = (
        ":root{"
        f"--paper:{t.paper};--card:{t.card};--ink:{t.ink};--ink-soft:{t.ink_soft};"
        f"--rule:{t.rule};--rule-soft:{t.rule_soft};--accent:{t.accent};--accent-deep:{t.accent_deep};"
        f"--serif:{t.serif};--sans:{t.sans};--mono:{t.mono};"
        "}"
        "*{box-sizing:border-box;}"
        "html,body{margin:0;height:100%;font-family:var(--sans);color:var(--ink);background:var(--paper);}"
        "#app{display:flex;flex-direction:column;height:100vh;overflow:hidden;}"
        ".mast{border-bottom:2px solid var(--ink);padding:.7rem 1.2rem;}"
        ".mast .ey{font-family:var(--mono);font-size:.62rem;letter-spacing:.26em;text-transform:uppercase;color:var(--accent);margin:0;}"
        ".mast h1{font-family:var(--serif);font-weight:900;font-size:1.5rem;margin:.1rem 0 0;letter-spacing:-.02em;}"
        ".strip{display:flex;gap:.6rem;overflow-x:auto;padding:.7rem 1.2rem;border-bottom:1px solid var(--rule);flex:0 0 auto;}"
        ".tile{flex:0 0 auto;min-width:150px;background:var(--card);border:1px solid var(--rule);border-radius:8px;padding:.5rem .7rem;cursor:pointer;transition:border-color .2s,transform .2s;}"
        ".tile:hover{border-color:var(--ink);transform:translateY(-2px);}"
        ".tile.sel{border-color:var(--accent);box-shadow:-3px 4px 0 rgba(180,68,28,.12);}"
        ".tile .t{font-family:var(--mono);font-size:.6rem;text-transform:uppercase;letter-spacing:.1em;color:var(--ink-soft);}"
        ".tile .h{font-family:var(--serif);font-weight:900;font-size:1.15rem;color:var(--accent);}"
        ".tile .n{font-size:.7rem;color:var(--ink-soft);max-width:200px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;}"
        ".gsep{flex:0 0 auto;align-self:center;writing-mode:vertical-rl;transform:rotate(180deg);font-family:var(--mono);font-size:.58rem;letter-spacing:.18em;color:var(--accent-deep);padding:.2rem 0;border-left:2px solid var(--accent-deep);margin-left:.3rem;}"
        ".ctl{display:flex;gap:1rem;align-items:center;padding:.5rem 1.2rem;border-bottom:1px solid var(--rule-soft);flex:0 0 auto;flex-wrap:wrap;}"
        ".ctl .lab{font-family:var(--mono);font-size:.66rem;text-transform:uppercase;letter-spacing:.12em;color:var(--ink-soft);}"
        ".detail{flex:1 1 auto;overflow:auto;padding:1rem 1.2rem;}"
        ".dgrid{display:grid;grid-template-columns:repeat(auto-fit,minmax(360px,1fr));gap:1rem;}"
        ".pane{background:var(--card);border:1px solid var(--rule);border-radius:8px;padding:.5rem .7rem;min-width:0;overflow:hidden;}"
        ".pane .pt{font-family:var(--mono);font-size:.66rem;text-transform:uppercase;letter-spacing:.12em;color:var(--accent-deep);margin:.1rem 0 .2rem;}"
        ".dh{font-family:var(--serif);font-size:1.1rem;margin:0 0 .2rem;}"
        ".dm{font-family:var(--mono);font-size:.68rem;color:var(--ink-soft);margin-bottom:.6rem;}"
        ".js-plotly-plot,.plot-container{width:100%!important;}"
    )
    return (
        "<!DOCTYPE html><html><head>{%metas%}<title>{%title%}</title>"
        + fonts
        + f"<style>{css}</style>{{%favicon%}}{{%css%}}</head>"
        + "<body><div id=\"app\">{%app_entry%}</div><footer>{%config%}{%scripts%}{%renderer%}</footer></body></html>"
    )


def build_dash_app(repo_root: Path, workspace_rel: str) -> dash.Dash:
    """Build the per-workspace BI Dash app: overview tiles (fit one viewport) + a
    drill-down detail showing the selected KPI's data-derived panels. Charts use
    the DESIGN.md theme. Caller runs `app.run(...)`.
    """
    repo_root = Path(repo_root).resolve()
    workspace = (repo_root / workspace_rel).resolve()
    layout = WorkspaceLayout(project_root=workspace)

    tokens = load_design_tokens(workspace)
    set_active_design(tokens)

    app = dash.Dash(__name__, title=f"Dashboard — {workspace.name}")
    app.index_string = _dash_index_string(tokens)

    definitions = load_kpi_definitions(layout)
    diff = compute_workflow_diff(repo_root, workspace_rel)
    gaps_by_id = {str(g.get("kpi_id")): g for g in (diff.get("kpi_gaps") or [])}

    # LAZY (2e): query each KPI's result view ONCE for its headline + panel titles,
    # but DON'T build the heavy Plotly figures up front — those are built on drill and
    # cached. KPI result views are pre-aggregated (GROUP BY) and the fetch is capped at
    # _SAMPLE_CAP, so raw (multi-TB) data never reaches the browser: DuckDB/Delta
    # aggregates server-side, we sample the small result, and figures build per drilled
    # KPI rather than all-at-once.
    meta: dict[str, dict[str, Any]] = {}
    blocked: dict[str, dict[str, Any]] = {}
    for kpi_id, definition in sorted(definitions.items()):
        spec = load_kpi_spec(layout, kpi_id)
        gap = gaps_by_id.get(kpi_id) or {}
        if str(gap.get("status")) == "blocked" or not (spec and spec.config.get("sql_path")):
            blocked[kpi_id] = gap or {"blockers": ["spec_missing"], "recovery_commands": []}
            continue
        sql_path = (repo_root / str(spec.config.get("sql_path"))).resolve()
        rows = _execute_sql_view(repo_root, sql_path, f"{kpi_id}_results", limit=_SAMPLE_CAP)
        panels_cfg = spec.config.get("panels") or [dict(spec.config)]
        defn = spec.config.get("definition") or {}
        meta[kpi_id] = {
            "spec": spec,
            "rows": rows,
            "headline": _kpi_headline(rows, spec),
            "title": str(spec.config.get("title") or kpi_id),
            "metric": str(defn.get("metric") or ""),
            "cuts": str(defn.get("cuts") or ""),
            # 2d — nested KPIs: an optional generic `group` field on the KPI
            # definition organizes the overview into sections. Absent -> flat.
            "group": str(defn.get("group") or definition.get("group") or ""),
            "panels_cfg": panels_cfg,
            "panel_titles": [str(p.get("title") or f"View {i + 1}") for i, p in enumerate(panels_cfg)],
        }

    ready_ids = sorted(meta, key=lambda k: (meta[k]["group"], k))  # group together
    first = ready_ids[0] if ready_ids else ""

    _fig_cache: dict[tuple[str, int], go.Figure] = {}

    def _panel_figure(kpi_id: str, idx: int) -> go.Figure:
        key = (kpi_id, idx)
        if key not in _fig_cache:
            m = meta[kpi_id]
            _fig_cache[key] = _figure_from_spec(
                _panel_spec(m["spec"], m["panels_cfg"][idx]), m["rows"], show_title=False
            )
        return _fig_cache[key]

    # Overview strip: clickable tiles, grouped by `group` with inline separators so
    # nested KPIs stay organized while the strip remains one fit-to-viewport row.
    strip_children: list[Any] = []
    last_group = None
    for kid in ready_ids:
        g = meta[kid]["group"]
        if g and g != last_group:
            strip_children.append(html.Div(g.upper(), className="gsep"))
        last_group = g
        strip_children.append(html.Div(
            [html.Div(kid.replace("_", " ").upper(), className="t"),
             html.Div(meta[kid]["headline"], className="h"),
             html.Div(meta[kid]["title"], className="n", title=meta[kid]["title"])],
            id={"role": "kpi-tile", "kpi_id": kid},
            className="tile" + (" sel" if kid == first else ""),
            n_clicks=0,
        ))
    for kid in blocked:
        strip_children.append(html.Div(
            [html.Div(kid.replace("_", " ").upper(), className="t"),
             html.Div("blocked", className="h", style={"color": "var(--ink-soft)"}),
             html.Div("no executable SQL", className="n")],
            className="tile",
        ))

    controls = html.Div([
        html.Span("KPI", className="lab"),
        dcc.Dropdown(id="kpi-pick", options=[{"label": meta[k]["title"], "value": k} for k in ready_ids],
                     value=first, clearable=False, style={"minWidth": "320px"}),
        html.Span("Views", className="lab"),
        dcc.Checklist(id="panel-pick", inline=True, style={"display": "flex", "gap": ".5rem", "flexWrap": "wrap"}),
    ], className="ctl") if ready_ids else html.Div()

    app.layout = html.Div([
        html.Header([html.P("Workspace Intelligence", className="ey"),
                     html.H1(workspace.name.replace("-", " "))], className="mast"),
        html.Div(strip_children or [html.Div("No KPIs registered yet.", className="n")], className="strip"),
        controls,
        html.Div(id="kpi-detail", className="detail"),
    ], id="app")

    def _detail_children(kpi_id: str, visible_idxs: list[int] | None) -> Any:
        m = meta.get(kpi_id)
        if not m:
            return html.Div("Select a KPI above.", className="dm")
        n = len(m["panel_titles"])
        idxs = visible_idxs if visible_idxs is not None else list(range(n))
        cells = []
        for i in idxs:
            if i < 0 or i >= n:
                continue
            cells.append(html.Div([
                html.Div(m["panel_titles"][i], className="pt"),
                dcc.Graph(figure=_panel_figure(kpi_id, i),  # built lazily + cached
                          config={"displaylogo": False, "responsive": True},
                          style={"height": "300px"}),
            ], className="pane"))
        return [
            html.H2(m["title"], className="dh"),
            html.Div((m["metric"] + ("  ·  cuts: " + m["cuts"] if m["cuts"] else "")) or "", className="dm"),
            html.Div(cells, className="dgrid"),
        ]

    if ready_ids:
        @app.callback(
            Output("kpi-pick", "value"),
            Input({"role": "kpi-tile", "kpi_id": ALL}, "n_clicks"),
            prevent_initial_call=True,
        )
        def _tile_to_pick(_clicks):
            trig = ctx.triggered_id
            if isinstance(trig, dict) and trig.get("role") == "kpi-tile":
                return trig.get("kpi_id")
            return no_update

        @app.callback(
            Output("panel-pick", "options"),
            Output("panel-pick", "value"),
            Input("kpi-pick", "value"),
        )
        def _panels_for_kpi(kpi_id):
            m = meta.get(kpi_id)
            if not m:
                return [], []
            opts = [{"label": t, "value": i} for i, t in enumerate(m["panel_titles"])]
            return opts, [o["value"] for o in opts]  # all visible by default

        @app.callback(
            Output("kpi-detail", "children"),
            Input("kpi-pick", "value"),
            Input("panel-pick", "value"),
        )
        def _render_detail(kpi_id, visible):
            return _detail_children(kpi_id, list(visible) if visible is not None else None)

        @app.callback(
            Output({"role": "kpi-tile", "kpi_id": ALL}, "className"),
            Input("kpi-pick", "value"),
            State({"role": "kpi-tile", "kpi_id": ALL}, "id"),
        )
        def _highlight_tile(kpi_id, ids):
            return ["tile sel" if (i or {}).get("kpi_id") == kpi_id else "tile" for i in ids]

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
