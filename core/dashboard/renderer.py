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
    """DuckDB read_csv_auto paths in generated SQL are relative to repo root.

    Delegates to the shared thread-safe ``pushd`` so concurrent Dash callbacks
    can't race the process-global cwd. Ref: core-audit SUMMARY.md T1.
    """
    from core.storage.atomic_io import pushd

    with pushd(repo_root):
        yield


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


def _as_year(value: Any) -> int | None:
    """Return a plausible 4-digit year (1900-2100) from an int/float/str, else None.

    Lets a date-named column holding bare years ("2021", 2021) read as a timeline
    axis. Generic — no workspace assumptions, just a sane calendar-year window."""
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        n = int(value)
    else:
        text = str(value).strip()
        if not text.lstrip("-").isdigit():
            return None
        n = int(text)
    return n if 1900 <= n <= 2100 else None


def _classify_columns(
    rows: list[dict[str, Any]],
) -> tuple[list[str], list[str], list[str]]:
    """Split a KPI result's columns into (dimensions, measures, date_cols) by dtype.

    Workspace-agnostic: classification is purely structural, derived from the
    VALUES in the live result rows plus a date-name hint, never from any
    workspace's domain vocabulary.

    * date_cols  — values coerce to a date, OR the column name is date-like and
      every present value coerces to a date. These are timeline X candidates.
    * measures   — values are numeric (int/float, non-bool) and the column is not
      a date column. These are Y / measure candidates.
    * dimensions — everything else (categorical / text / mixed). These are the
      X / breakdown candidates.

    A date column is NOT also listed as a measure (a year stored as an int still
    reads as a timeline, not a quantity to sum). The returned lists preserve the
    column order of the first row so dropdowns are stable.
    """
    if not rows:
        return [], [], []
    columns = list(rows[0].keys())
    dimensions: list[str] = []
    measures: list[str] = []
    date_cols: list[str] = []
    for col in columns:
        present = [r.get(col) for r in rows if r.get(col) is not None]
        if not present:
            # All-null column: treat as a (weak) dimension so it stays selectable.
            dimensions.append(col)
            continue
        name_is_date = bool(_DATE_NAME_RE.search(col))
        # Numeric? booleans are ints in Python but read as categories, not measures.
        numeric = [
            v for v in present if isinstance(v, (int, float)) and not isinstance(v, bool)
        ]
        all_numeric = len(numeric) == len(present)
        # Date? a value-level coercion test (handles string/date/datetime values).
        coerces_date = sum(1 for v in present if _coerce_date(v) is not None)
        looks_date = coerces_date == len(present) and (
            name_is_date or not all_numeric
        )
        # A date-NAMED column of bare year values ("2021", 2021) is a timeline
        # axis, not a quantity to sum — _coerce_date can't parse a lone year, so
        # detect it here (every value is an int-like year in a plausible range).
        if not looks_date and name_is_date:
            years = [_as_year(v) for v in present]
            if all(y is not None for y in years):
                looks_date = True
        if looks_date:
            date_cols.append(col)
        elif all_numeric:
            measures.append(col)
        else:
            dimensions.append(col)
    return dimensions, measures, date_cols


# --- Drill-down model (Phase 2) -------------------------------------------
# Clicking a category bar (e.g. an age band) re-renders the detail to show the
# WITHIN-category breakdown by the OTHER dimensions, filtered to the clicked
# value. The drill hierarchy is workspace-agnostic: the `by` axis is whatever
# categorical the clicked panel charts, and the `into` dimensions are derived
# from the result columns' dtypes (the other non-measure, non-date categoricals).
# A spec MAY pin an explicit `drill: {"by": <col>, "into": [<col>, ...]}` to
# override the default derivation; absent/invalid fields fall back to derivation.


def _drill_into(rows: list[dict[str, Any]], by: str) -> list[str]:
    """The OTHER categorical dimensions to break a drilled category down by.

    Generic: every non-measure, non-date dimension except `by`, in result-column
    order, that actually varies WITHIN the clicked slice's natural scope (kept
    here as all dims; the caller filters rows then renders, so a degenerate
    single-value dim simply renders one slice). No domain assumptions.
    """
    if not rows or not by:
        return []
    dims, _measures, _dates = _classify_columns(rows)
    return [d for d in dims if d != by]


def _drill_model(
    spec: DashboardSpec, rows: list[dict[str, Any]], by: str = ""
) -> dict[str, Any]:
    """Resolve the drill hierarchy for a click on the `by` category.

    Honors an optional `spec.config['drill'] = {"by": col, "into": [cols]}`:
    a pinned `by` (when the caller doesn't pass one) and/or a pinned `into`
    list are used when their columns exist in the live rows; otherwise both are
    derived from the data shape. Returns ``{"by": str, "into": list[str]}`` with
    `into` excluding `by` and any column absent from the rows.
    """
    present = set(rows[0].keys()) if rows else set()
    cfg = spec.config.get("drill") if isinstance(spec.config, dict) else None
    cfg = cfg if isinstance(cfg, dict) else {}
    if not by:
        cfg_by = str(cfg.get("by") or "")
        by = cfg_by if cfg_by in present else ""
        if not by:
            dims, _m, _d = _classify_columns(rows)
            by = dims[0] if dims else ""
    into_cfg = [c for c in (cfg.get("into") or []) if c in present and c != by]
    into = into_cfg if into_cfg else _drill_into(rows, by)
    return {"by": by, "into": into}


def _filter_rows_by_value(
    rows: list[dict[str, Any]], column: str, value: Any
) -> list[dict[str, Any]]:
    """Rows whose `column` equals the clicked `value` (string-compared so a
    numeric/category click matches regardless of dtype). Empty column or value
    returns the rows unchanged."""
    if not column:
        return rows
    target = str(value)
    return [r for r in rows if str(r.get(column)) == target]


_DRILL_DONUT_MAX = 8


def _drill_panels(
    by: str, value: Any, into: list[str], rows: list[dict[str, Any]] | None = None
) -> list[dict[str, Any]]:
    """Panel specs for a drilled category: one breakdown per `into` dimension,
    of the measure within the clicked `value`.

    Chart type is data-driven by the dimension's cardinality WITHIN the drilled
    slice: a low-cardinality breakdown is a donut (the composition reads at a
    glance and sums to 100% over the filtered rows), while a high-cardinality one
    is a ranked bar (a donut of 30 slivers is unreadable — data-to-viz). When
    `rows` is omitted the default is a donut. `y`/`y_format` are filled by the
    caller from the KPI's own spec."""
    panels: list[dict[str, Any]] = []
    for dim in into:
        chart = "donut"
        if rows is not None:
            distinct = len({str(r.get(dim)) for r in rows if dim in r})
            if distinct > _DRILL_DONUT_MAX:
                chart = "ranked_bar"
        panel: dict[str, Any] = {
            "chart_type": chart,
            "x": dim,
            "title": f"By {dim.replace('_', ' ').title()}",
        }
        if chart == "ranked_bar":
            panel["orientation"] = "h"
            panel["limit"] = 12
        panels.append(panel)
    return panels


# Chart types the Explore pane can build from a transient spec. Each maps to a
# branch in `_figure_from_spec`. Kept generic (no domain assumptions) and ordered
# from most-common to least so the dropdown reads sensibly.
_EXPLORE_CHART_TYPES: tuple[tuple[str, str], ...] = (
    ("Bar", "bar"),
    ("Grouped bar", "grouped_bar"),
    ("Stacked 100%", "stacked_bar_percent"),
    ("Line / timeline", "line"),
    ("Ranked bar (top-N)", "ranked_bar"),
    ("Donut", "donut"),
    ("Treemap", "treemap"),
    ("Heatmap", "heatmap"),
    ("Stacked area", "stacked_area"),
)


def _explore_chart_options(
    *, recommended: str = "", fit: dict[str, str] | None = None
) -> list[dict[str, Any]]:
    """Dropdown options for the Explore chart-type picker.

    Soft guidance (not enforcement): the recommended chart is marked, and charts
    that are a poor fit for the current X/measure/series shape are badged ``(not
    ideal)`` — every option stays selectable so the user can still force any chart.
    """
    fit = fit or {}
    opts: list[dict[str, Any]] = []
    for label, value in _EXPLORE_CHART_TYPES:
        verdict = fit.get(value, "ok")
        if value == recommended:
            label = f"★ {label}"
        elif verdict == "poor":
            label = f"{label} (not ideal)"
        opts.append({"label": label, "value": value})
    return opts


def _pretty_label(col: str) -> str:
    """Human axis/label for a result column: drop the agg prefix, de-snake, Title
    Case, and render a share/percent measure as ``… (%)``. Generic — no workspace
    vocabulary (``sum_paidamount`` -> ``Paidamount``; ``percentage_share`` ->
    ``Percentage Share (%)``; ``department_name`` -> ``Department Name``)."""
    if not col:
        return ""
    name = str(col)
    low = name.lower()
    for pre in ("sum_", "avg_", "mean_", "count_", "distinct_", "total_"):
        if low.startswith(pre):
            name = name[len(pre):]
            low = name.lower()
            break
    pretty = name.replace("_", " ").strip().title()
    if any(t in low for t in ("percent", "share", "ratio", "rate", "pct")):
        if "%" not in pretty:
            pretty = f"{pretty} (%)"
    return pretty


def _distinct_count(rows: list[dict[str, Any]], col: str) -> int:
    """Distinct non-null values of ``col`` across rows."""
    if not col:
        return 0
    return len({str(r.get(col)) for r in rows if r.get(col) is not None})


def _explore_recommendation(
    rows: list[dict[str, Any]], x: str, series: str | None, measure: str
) -> tuple[str, str, dict[str, str]]:
    """Recommend a chart for the chosen X/Breakdown/Measure using the data-to-viz
    chart-knowledge brain, and rate every chart's FIT for that shape.

    Returns ``(recommended_chart_type, reason, fit)`` where ``fit`` maps each
    chart_type -> ``"best" | "ok" | "poor"``. This is what makes Explore not
    "shallow": the same principled selection the KPI panels use now drives the
    ad-hoc chart picker, so a line-over-departments or a 10-slice donut is flagged
    instead of silently rendered.
    """
    from core.dashboard import chart_knowledge as ck

    if not rows or not x:
        return "bar", "", {}
    _dims, measures, date_cols = _classify_columns(rows)
    is_date = x in (date_cols or [])
    is_share = any(t in (measure or "").lower() for t in ("percent", "share", "ratio", "rate", "pct"))
    has_series = bool(series and series != x and series in (rows[0] if rows else {}))
    distinct_x = _distinct_count(rows, x)
    x_vals = [r.get(x) for r in rows if r.get(x) is not None]
    is_ordinal = ck.is_ordinal_categories(x_vals)

    # 1) Pick the recommended chart from the data shape (data-to-viz families).
    if is_date:
        choice = ck.choose_trend_chart(
            series_count=_distinct_count(rows, series) if has_series else 1,
            is_share=is_share,
        )
    elif has_series:
        choice = ck.choose_two_categorical_chart(
            distinct_a=distinct_x, distinct_b=_distinct_count(rows, series)
        ) or ck.ChartChoice(
            "grouped_bar",
            "two categorical dimensions with a wide axis read better side-by-side "
            "than as a heatmap (data-to-viz: grouped bar)",
        )
    else:
        choice = ck.choose_categorical_chart(
            distinct=distinct_x, is_share=is_share, is_ordinal=is_ordinal
        )
    recommended = choice.chart_type
    reason = choice.reason

    # 2) Rate every selectable chart's fit for this shape (best / ok / poor).
    fit: dict[str, str] = {}
    for _label, ct in _EXPLORE_CHART_TYPES:
        if ct == recommended:
            fit[ct] = "best"
            continue
        verdict = "ok"
        time_only = ct in ("line", "stacked_area")
        if time_only and not is_date:
            verdict = "poor"                                  # a trend chart needs time
        elif ct == "donut" and (is_date or has_series or is_ordinal or distinct_x > ck.DONUT_CARDINALITY):
            verdict = "poor"                                  # pie caveat: few non-ordinal slices
        elif ct == "treemap" and (is_date or has_series or distinct_x > ck.TREEMAP_CARDINALITY):
            verdict = "poor"
        elif ct == "heatmap" and not has_series:
            verdict = "poor"                                  # heatmap needs two categoricals
        elif ct in ("grouped_bar", "stacked_bar_percent") and not has_series:
            verdict = "poor"                                  # nothing to split without a breakdown
        elif ct == "ranked_bar" and is_date:
            verdict = "poor"
        elif ct == "bar" and is_date:
            verdict = "poor"                                  # over time -> line
        fit[ct] = verdict
    return recommended, reason, fit


def _explore_default_chart_type(x: str, date_cols: list[str], series: str | None) -> str:
    """Pick a sensible default chart for an X/series choice.

    A date X -> line (timeline). Otherwise a breakdown -> grouped bar; a plain
    single categorical -> bar.
    """
    if x and x in (date_cols or []):
        return "line"
    if series:
        return "grouped_bar"
    return "bar"


def _explore_figure(
    spec: DashboardSpec,
    rows: list[dict[str, Any]],
    *,
    x: str,
    series: str | None,
    measure: str,
    chart_type: str,
) -> go.Figure:
    """Build an Explore figure by assembling a TRANSIENT spec from the dropdown
    choices and reusing `_figure_from_spec` (the single chart-quality seam).

    Guards invalid/empty combos: an unknown X/measure (not in the live rows)
    falls back to the first real dimension / measure so the pane never renders a
    blank or crashes. A date X with a non-line chart still defaults sensibly via
    the caller; here we only ensure the columns exist and the share/percent flag
    is derived from the measure name so percent charts read 0-100%.
    """
    if not rows:
        return _figure_from_spec(
            _panel_spec(spec, {"chart_type": chart_type or "bar", "title": ""}), rows,
            show_title=False,
        )
    dims, measures, date_cols = _classify_columns(rows)
    present = set(rows[0].keys())
    # Resolve X: keep the choice if it's a real column, else first dimension/date.
    if x not in present:
        x = (dims or date_cols or list(present))[0]
    # Resolve measure: keep if real & numeric, else first measure, else any column.
    if measure not in present or measure not in measures:
        measure = measures[0] if measures else (list(present)[-1])
    # Series is optional; drop it if it doesn't exist or duplicates X.
    color = series if (series and series in present and series != x) else ""
    if not chart_type:
        chart_type = _explore_default_chart_type(x, date_cols, color or None)
    # Percent intent is derived from the measure name (generic share vocabulary),
    # so a share measure renders as a true 0-100% composition.
    name = (measure or "").lower()
    y_format = "percent" if any(
        t in name for t in ("percent", "share", "ratio", "rate", "pct")
    ) else ""
    panel = {
        "chart_type": chart_type,
        "x": x,
        "y": measure,
        "color": color,
        "title": "",
    }
    if y_format:
        panel["y_format"] = y_format
    return _figure_from_spec(_panel_spec(spec, panel), rows, show_title=False)


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
            # data-to-viz: many SIMILAR-height values read better as a
            # Cleveland dot plot than bars. POSITION (not length) encodes the
            # value, so the axis zooms to the data range — on a zero axis,
            # near-equal values collapse into one vertical line of dots and
            # the chart says nothing. Each dot is value-labeled; a full-width
            # pale guide ties the dot to its category label.
            rank_col = _first_non_constant_categorical(rows, y_col, preferred=x_col)
            data = _aggregate_rows(rows, rank_col, None, y_col)
            if y_is_percent:
                data = _normalize_percent(data, y_col)
            limit = int(config.get("limit") or 10)
            ordered = sorted(data, key=lambda r: r.get(y_col) or 0, reverse=True)[:limit]
            ordered = list(reversed(ordered))
            cats = [str(r.get(rank_col)) for r in ordered]
            vals = [float(r.get(y_col) or 0) for r in ordered]
            lo, hi = min(vals), max(vals)
            pad = max((hi - lo) * 0.18, hi * 0.005) or 1.0
            fig = go.Figure()
            for cat in cats:
                fig.add_trace(go.Scatter(
                    x=[lo - pad, hi + pad], y=[cat, cat], mode="lines",
                    line={"color": _GRID_COLOR, "width": 1}, showlegend=False,
                    hoverinfo="skip",
                ))
            labels = [f"{v:,.1f}%" if y_is_percent else f"{v:,.0f}" for v in vals]
            fig.add_trace(go.Scatter(
                x=vals, y=cats, mode="markers+text", text=labels,
                textposition="middle right",
                textfont={"size": 11, "color": _ACTIVE.ink_soft},
                marker={"color": _ACTIVE.accent, "size": 11}, showlegend=False,
            ))
            fig.update_layout(title=title)
            fig.update_yaxes(type="category")
            fig.update_xaxes(range=[lo - pad, hi + pad * 2.2])
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
            colorbar = {"thickness": 12, "outlinewidth": 0}
            if y_is_percent:
                colorbar["ticksuffix"] = "%"
            fig = go.Figure(go.Heatmap(
                z=z, x=xs, y=ys,
                colorscale=[[0, "#f6f1e7"], [1, _ACTIVE.accent]],
                colorbar=colorbar,
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
    # Human axis titles: raw result columns (`sum_paidamount`) read as code on an
    # executive chart. Prettify for axis-based families (the measure/category live
    # on an axis); heatmap/donut/treemap/maps encode via color/slice, not an axis.
    if chart_type not in ("heatmap", "donut", "treemap", "bubble_map", "big_number"):
        if chart_type in ("ranked_bar", "lollipop"):
            fig.update_xaxes(title_text=_pretty_label(y_col))
            fig.update_yaxes(title_text=_pretty_label(x_col))
        else:
            fig.update_xaxes(title_text=_pretty_label(x_col))
            fig.update_yaxes(title_text=_pretty_label(y_col))
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


# A panel's information value, by chart family: charts that encode MORE of the
# KPI at once (two categorical dims, or a trend with a series) carry more value
# than a single-dimension slice. Used to pick the 1-2 default panels per KPI so a
# dashboard of 20 KPIs is not a wall of 100 charts — the Explore pane gives the
# user every other comparison on demand. Workspace-agnostic (reads only the spec).
_PANEL_VALUE_RANK: dict[str, int] = {
    "heatmap": 6,            # two categorical dims x measure — the richest single view
    "stacked_bar_percent": 5,
    "stacked_area": 5,
    "grouped_bar": 5,
    "line": 4,              # trend over time
    "bubble_map": 4,
    "ranked_bar": 4,        # ranks the primary entity dimension
    "scatter": 3,
    "lollipop": 3,
    "treemap": 3,
    "bar": 2,
    "donut": 2,             # single-dimension composition
    "histogram": 2,
    "big_number": 1,
}


def _panel_value_score(panel: dict[str, Any]) -> int:
    """Higher = shows more of the KPI at once. A second encoded dimension
    (color/series) adds one point so a split chart beats its flat sibling."""
    score = _PANEL_VALUE_RANK.get(str(panel.get("chart_type") or "bar"), 2)
    if panel.get("color"):
        score += 1
    return score


def _default_panel_idxs(panels_cfg: list[dict[str, Any]], *, cap: int = 2) -> list[int]:
    """Indices of the top-``cap`` highest-value panels, in original display order.

    The KPI shows its 1-2 best (most information-dense) views by default instead
    of every derived panel; ties keep the earlier panel (the data-to-viz priority
    order the inference step already chose). Returns at least one index when any
    panel exists."""
    n = len(panels_cfg)
    if n <= cap:
        return list(range(n))
    ranked = sorted(range(n), key=lambda i: (_panel_value_score(panels_cfg[i]), -i), reverse=True)
    return sorted(ranked[:cap])


def _dash_index_string(t: DesignTokens) -> str:
    """Claude-style CSS shell for the live app, generated from DESIGN.md tokens.
    A slim header, a clickable KPI rail, a prominent Explore card, then the KPI's
    1-2 highest-value panels. Colors come from the workspace tokens (the same
    tokens drive the Plotly palette, so chart colors are untouched)."""
    fams = "&".join(f"family={f}" for f in t.font_families)
    fonts = (f'<link href="https://fonts.googleapis.com/css2?{fams}&display=swap" rel="stylesheet">'
             if fams else "")
    css = (
        ":root{"
        f"--paper:{t.paper};--card:{t.card};--ink:{t.ink};--ink-soft:{t.ink_soft};"
        f"--rule:{t.rule};--rule-soft:{t.rule_soft};--accent:{t.accent};--accent-deep:{t.accent_deep};"
        f"--serif:{t.serif};--sans:{t.sans};--mono:{t.mono};"
        "--shadow:0 1px 2px rgba(40,33,28,.04),0 6px 20px rgba(40,33,28,.06);"
        "--radius:14px;}"
        "*{box-sizing:border-box;}"
        "html,body{margin:0;height:100%;font-family:var(--sans);color:var(--ink);"
        "background:var(--paper);-webkit-font-smoothing:antialiased;}"
        "#app{display:flex;flex-direction:column;height:100vh;overflow:hidden;}"
        # Slim, calm header — no heavy rule, just a soft hairline.
        ".mast{padding:1rem 1.6rem .9rem;border-bottom:1px solid var(--rule-soft);}"
        ".mast .ey{font-family:var(--mono);font-size:.62rem;letter-spacing:.24em;text-transform:uppercase;color:var(--accent);margin:0;}"
        ".mast h1{font-family:var(--serif);font-weight:600;font-size:1.5rem;margin:.15rem 0 0;letter-spacing:-.015em;color:var(--ink);}"
        # KPI rail — soft rounded pills.
        ".strip{display:flex;gap:.7rem;overflow-x:auto;padding:1rem 1.6rem;flex:0 0 auto;}"
        ".tile{flex:0 0 auto;min-width:160px;background:var(--card);border:1px solid var(--rule-soft);"
        "border-radius:var(--radius);padding:.7rem .9rem;cursor:pointer;box-shadow:var(--shadow);"
        "transition:transform .18s ease,border-color .18s ease,box-shadow .18s ease;}"
        ".tile:hover{transform:translateY(-2px);border-color:var(--accent);}"
        ".tile.sel{border-color:var(--accent);box-shadow:0 0 0 1px var(--accent),var(--shadow);}"
        ".tile .t{font-family:var(--mono);font-size:.58rem;text-transform:uppercase;letter-spacing:.1em;color:var(--ink-soft);}"
        ".tile .h{font-family:var(--serif);font-weight:700;font-size:1.25rem;color:var(--accent);margin-top:.1rem;}"
        ".tile .n{font-size:.72rem;color:var(--ink-soft);max-width:210px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;margin-top:.15rem;}"
        ".tile .st{font-family:var(--mono);font-size:.56rem;letter-spacing:.12em;text-transform:uppercase;color:var(--accent-deep);margin-top:.3rem;}"
        ".gsep{flex:0 0 auto;align-self:center;writing-mode:vertical-rl;transform:rotate(180deg);font-family:var(--mono);font-size:.56rem;letter-spacing:.18em;color:var(--accent-deep);}"
        # Scroll region holds Explore card + the value panels.
        ".scroll{flex:1 1 auto;overflow:auto;padding:.2rem 1.6rem 1.6rem;}"
        # Explore — the promoted customization card, sits at the top.
        ".explore{background:var(--card);border:1px solid var(--rule-soft);border-radius:var(--radius);"
        "box-shadow:var(--shadow);padding:1rem 1.1rem;margin-bottom:1.2rem;}"
        ".explore .xh{display:flex;align-items:baseline;gap:.6rem;margin:0 0 .7rem;}"
        ".explore .xh b{font-family:var(--serif);font-weight:600;font-size:1.02rem;color:var(--ink);}"
        ".explore .xh span{font-family:var(--mono);font-size:.62rem;letter-spacing:.1em;text-transform:uppercase;color:var(--ink-soft);}"
        ".ctl{display:flex;gap:.6rem 1.1rem;align-items:center;flex-wrap:wrap;margin-bottom:.5rem;}"
        # Each label+dropdown reads as one grouped control that wraps as a unit.
        ".cgroup{display:flex;align-items:center;gap:.45rem;}"
        ".ctl .lab{font-family:var(--mono);font-size:.58rem;text-transform:uppercase;letter-spacing:.12em;"
        "color:var(--ink-soft);white-space:nowrap;}"
        # The data-to-viz 'why this chart' note under the Explore controls.
        ".why{font-family:var(--sans);font-size:.74rem;color:var(--ink-soft);line-height:1.4;"
        "margin:.1rem 0 .6rem;padding:.4rem .6rem;background:rgba(180,68,28,.05);"
        "border-left:2px solid var(--accent);border-radius:0 6px 6px 0;}"
        ".why .why-k{font-family:var(--mono);font-size:.62rem;text-transform:uppercase;letter-spacing:.1em;color:var(--accent-deep);}"
        # KPI detail header + value panels.
        ".dhwrap{margin:.2rem 0 .8rem;display:flex;align-items:flex-start;justify-content:space-between;gap:1rem;}"
        # Card toolbar — small pill buttons top-right (Table / Export).
        ".toolbar{display:flex;gap:.5rem;flex:0 0 auto;}"
        ".tbtn{font-family:var(--mono);font-size:.62rem;text-transform:uppercase;letter-spacing:.1em;"
        "cursor:pointer;background:var(--card);color:var(--accent-deep);border:1px solid var(--rule);"
        "border-radius:8px;padding:.35rem .7rem;transition:border-color .18s,background .18s;}"
        ".tbtn:hover{border-color:var(--accent);}"
        ".tbtn.active{background:var(--accent);color:#fff;border-color:var(--accent);}"
        ".datawrap{background:var(--card);border:1px solid var(--rule-soft);border-radius:var(--radius);"
        "box-shadow:var(--shadow);padding:.8rem 1rem;margin-top:1rem;}"
        ".dh{font-family:var(--serif);font-weight:600;font-size:1.2rem;margin:0;letter-spacing:-.01em;}"
        ".dm{font-family:var(--mono);font-size:.66rem;color:var(--ink-soft);margin-top:.25rem;}"
        ".dgrid{display:grid;grid-template-columns:repeat(auto-fit,minmax(380px,1fr));gap:1.2rem;}"
        ".pane{background:var(--card);border:1px solid var(--rule-soft);border-radius:var(--radius);"
        "box-shadow:var(--shadow);padding:.8rem 1rem;min-width:0;overflow:hidden;}"
        ".pane .pt{font-family:var(--mono);font-size:.62rem;text-transform:uppercase;letter-spacing:.12em;color:var(--accent-deep);margin:0 0 .3rem;}"
        ".js-plotly-plot,.plot-container{width:100%!important;}"
        ".drill-bar{display:flex;align-items:center;gap:.7rem;margin:0 0 .7rem;}"
        ".drill-back{font-family:var(--mono);font-size:.64rem;text-transform:uppercase;letter-spacing:.1em;cursor:pointer;background:var(--card);color:var(--accent-deep);border:1px solid var(--rule);border-radius:8px;padding:.3rem .7rem;}"
        ".drill-back:hover{border-color:var(--accent);}"
        ".drill-crumb{font-family:var(--mono);font-size:.68rem;letter-spacing:.05em;color:var(--ink-soft);}"
        ".hidden{display:none!important;}"
    )
    return (
        "<!DOCTYPE html><html><head>{%metas%}<title>{%title%}</title>"
        + fonts
        + f"<style>{css}</style>{{%favicon%}}{{%css%}}</head>"
        + "<body><div id=\"app\">{%app_entry%}</div><footer>{%config%}{%scripts%}{%renderer%}</footer></body></html>"
    )


def _slice_rows(rows: list[dict[str, Any]], col: str, values: list[Any]) -> list[dict[str, Any]]:
    """Filter ``rows`` to those whose ``col`` is one of ``values`` (Power-BI-style
    slice). Empty col/values -> unchanged. Compared as strings so the dropdown's
    string values match numeric/categorical cells alike."""
    if not col or not values:
        return rows
    keep = {str(v) for v in values}
    return [r for r in rows if str(r.get(col)) in keep]


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
    # The detail toolbar (Table/Export) lives inside the dynamically-rendered
    # kpi-detail, so its callbacks reference ids not present in the initial layout.
    app.config.suppress_callback_exceptions = True
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
            # Phase 2 drill: each panel's x column tells a click which dimension
            # it is drilling BY (the clicked bar's category lives on that axis).
            "panel_x": [str(p.get("x") or "") for p in panels_cfg],
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
    tile_badges = load_kpi_statuses(workspace)
    strip_children: list[Any] = []
    last_group = None
    for kid in ready_ids:
        g = meta[kid]["group"]
        if g and g != last_group:
            strip_children.append(html.Div(g.upper(), className="gsep"))
        last_group = g
        tile_children = [
            html.Div(kid.replace("_", " ").upper(), className="t"),
            html.Div(meta[kid]["headline"], className="h"),
            html.Div(meta[kid]["title"], className="n", title=meta[kid]["title"]),
        ]
        badge = tile_badges.get(kid, "")
        if badge:
            tile_children.append(html.Div(badge, className="st"))
        strip_children.append(html.Div(
            tile_children,
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

    # The KPI selector is now driven entirely by clicking a tile in the rail, so
    # the old "KPI | <title> | Views" control row is gone (it read as noise). The
    # dropdown is KEPT but HIDDEN — it is the single source of "current KPI" that
    # the detail/explore/drill callbacks all read, and tile clicks write to it.
    kpi_state = dcc.Dropdown(
        id="kpi-pick",
        options=[{"label": meta[k]["title"], "value": k} for k in ready_ids],
        value=first, clearable=False, className="hidden",
    ) if ready_ids else dcc.Dropdown(id="kpi-pick", className="hidden")

    # Explore — promoted to a prominent card at the TOP of the scroll region (it is
    # the comparison/customization surface the board asked for). The four dropdowns
    # populate from the SELECTED KPI's own result columns (classified by dtype) and
    # a callback rebuilds the figure via a transient spec -> `_figure_from_spec`.
    _dd = {"minWidth": "170px", "fontSize": ".8rem"}
    explore = html.Div([
        html.Div([
            html.B("Explore"),
            html.Span("compare any columns for this KPI", className=""),
        ], className="xh"),
        # Each label+dropdown is one .cgroup so a label never wraps away from its
        # control. The first row is the comparison; the second is the slice.
        html.Div([
            html.Div([html.Span("X", className="lab"),
                      dcc.Dropdown(id="explore-x", clearable=False, style=_dd)], className="cgroup"),
            html.Div([html.Span("Breakdown", className="lab"),
                      dcc.Dropdown(id="explore-series", clearable=True, placeholder="(none)", style=_dd)], className="cgroup"),
            html.Div([html.Span("Measure", className="lab"),
                      dcc.Dropdown(id="explore-measure", clearable=False, style=_dd)], className="cgroup"),
            html.Div([html.Span("Chart", className="lab"),
                      dcc.Dropdown(id="explore-chart", options=_explore_chart_options(),
                                   clearable=False, style=_dd)], className="cgroup"),
        ], className="ctl"),
        # Slice (Power-BI-style filter): pick a dimension + the value(s) to keep;
        # the chart re-renders to only those rows. This is the slice-and-dice the
        # chart does on the data itself — no separate table.
        html.Div([
            html.Div([html.Span("Slice by", className="lab"),
                      dcc.Dropdown(id="explore-filter-col", clearable=True,
                                   placeholder="(no slice)", style=_dd)], className="cgroup"),
            html.Div([html.Span("Values", className="lab"),
                      dcc.Dropdown(id="explore-filter-vals", multi=True, placeholder="(all)",
                                   style={"minWidth": "260px", "fontSize": ".8rem"})], className="cgroup"),
        ], className="ctl"),
        # Why this chart — the data-to-viz reason for the recommendation.
        html.Div(id="explore-reason", className="why"),
        dcc.Graph(id="explore-graph",
                  config={"displaylogo": False, "responsive": True},
                  style={"height": "360px"}),
    ], id="explore-pane", className="explore") if ready_ids else html.Div()

    app.layout = html.Div([
        html.Header([html.P("Workspace Intelligence", className="ey"),
                     html.H1(workspace.name.replace("-", " "))], className="mast"),
        html.Div(strip_children or [html.Div("No KPIs registered yet.", className="n")], className="strip"),
        kpi_state,
        # Phase 2 drill state: {kpi_id, by, value} when a category is drilled,
        # else None. A click on a panel bar sets it; "← back" clears it.
        dcc.Store(id="drill-store"),
        dcc.Download(id="detail-download"),
        html.Div([
            explore,
            html.Div(id="kpi-detail"),
        ], className="scroll"),
    ], id="app")

    def _drill_detail_children(kpi_id: str, by: str, value: Any) -> Any:
        """Phase 2 sub-panel view: the WITHIN-category breakdown for a clicked
        bar. Reuses `_figure_from_spec` on rows filtered to the clicked value,
        one donut per OTHER dimension. "← back" clears the drill state."""
        m = meta[kpi_id]
        spec = m["spec"]
        model = _drill_model(spec, m["rows"], by=by)
        into = model["into"]
        filtered = _filter_rows_by_value(m["rows"], by, value)
        y_col = str(spec.config.get("y") or "")
        y_format = str(spec.config.get("y_format") or "")
        cells = []
        for panel in _drill_panels(by, value, into, filtered):
            p = dict(panel)
            if y_col:
                p["y"] = y_col
            if y_format:
                p["y_format"] = y_format
            fig = _figure_from_spec(_panel_spec(spec, p), filtered, show_title=False)
            cells.append(html.Div([
                html.Div(p["title"], className="pt"),
                dcc.Graph(figure=fig,
                          config={"displaylogo": False, "responsive": True},
                          style={"height": "300px"}),
            ], className="pane"))
        if not cells:
            cells = [html.Div("No further breakdown for this category.", className="dm")]
        by_label = by.replace("_", " ").title()
        return [
            html.Div([
                html.Button("← back", id="drill-back", n_clicks=0, className="drill-back"),
                html.Span(f"{by_label}: {value}", className="drill-crumb"),
            ], className="drill-bar"),
            html.H2(m["title"], className="dh"),
            html.Div(f"Within {by_label} = {value}", className="dm"),
            html.Div(cells, className="dgrid"),
            _data_view_component(workspace, kpi_id, filtered),
        ]

    def _detail_children(
        kpi_id: str, visible_idxs: list[int] | None, drill: dict[str, Any] | None = None
    ) -> Any:
        m = meta.get(kpi_id)
        if not m:
            return html.Div("Select a KPI above.", className="dm")
        # Drill active for THIS KPI -> render the within-category sub-panels.
        if drill and drill.get("kpi_id") == kpi_id and drill.get("by"):
            return _drill_detail_children(kpi_id, str(drill["by"]), drill.get("value"))
        n = len(m["panel_titles"])
        # Default = the 1-2 highest-value panels (not every derived view). Explore
        # + drill give the user the rest on demand. `visible_idxs` (if a future
        # caller passes one) still overrides.
        idxs = visible_idxs if visible_idxs is not None else _default_panel_idxs(m["panels_cfg"])
        cells = []
        for i in idxs:
            if i < 0 or i >= n:
                continue
            cells.append(html.Div([
                html.Div(m["panel_titles"][i], className="pt"),
                dcc.Graph(figure=_panel_figure(kpi_id, i),  # built lazily + cached
                          id={"role": "kpi-panel", "kpi_id": kpi_id, "idx": i,
                              "x": m["panel_x"][i]},
                          config={"displaylogo": False, "responsive": True},
                          style={"height": "320px"}),
            ], className="pane"))
        return [
            html.Div([
                html.Div([
                    html.H2(m["title"], className="dh"),
                    html.Div((m["metric"] + ("  ·  cuts: " + m["cuts"] if m["cuts"] else "")) or "", className="dm"),
                ]),
                # Card toolbar (top-right): toggle the data table / export it.
                html.Div([
                    html.Button("Table", id="detail-table-btn", n_clicks=0, className="tbtn"),
                    html.Button("Export CSV", id="detail-export-btn", n_clicks=0, className="tbtn"),
                ], className="toolbar"),
            ], className="dhwrap"),
            html.Div(cells, className="dgrid"),
            html.Div(_data_view_table(workspace, m["rows"]),
                     id="detail-data", className="datawrap hidden"),
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
            Output("kpi-detail", "children"),
            Input("kpi-pick", "value"),
            Input("drill-store", "data"),
        )
        def _render_detail(kpi_id, drill):
            return _detail_children(
                kpi_id,
                None,  # default = the KPI's top-2 value panels
                drill if isinstance(drill, dict) else None,
            )

        # --- Drill-down (Phase 2) -----------------------------------------
        # A click on any rendered panel bar sets the drill state to the clicked
        # category; selecting a different KPI or hitting "← back" clears it.
        @app.callback(
            Output("drill-store", "data"),
            Input({"role": "kpi-panel", "kpi_id": ALL, "idx": ALL, "x": ALL}, "clickData"),
            Input("kpi-pick", "value"),
            prevent_initial_call=True,
        )
        def _set_drill(_clicks, _kpi_id):
            trig = ctx.triggered_id
            # KPI changed (or any non-panel trigger) -> clear drill.
            if not (isinstance(trig, dict) and trig.get("role") == "kpi-panel"):
                return None
            by = str(trig.get("x") or "")
            if not by:
                return no_update
            # Find which panel's clickData fired and read the clicked category.
            point = None
            for entry in (ctx.triggered or []):
                val = entry.get("value")
                if val and isinstance(val, dict) and val.get("points"):
                    point = val["points"][0]
                    break
            if not point:
                return no_update
            # The clicked category is on the axis that holds the `by` dimension.
            # For vertical bars it is x; ranked/horizontal bars put it on y.
            value = point.get("x")
            if value is None or value == "":
                value = point.get("y")
            if value is None or value == "":
                value = point.get("label")  # donut/pie slice
            if value is None:
                return no_update
            return {"kpi_id": str(trig.get("kpi_id") or ""), "by": by, "value": value}

        @app.callback(
            Output("drill-store", "data", allow_duplicate=True),
            Input("drill-back", "n_clicks"),
            prevent_initial_call=True,
        )
        def _clear_drill(n_clicks):
            return None if n_clicks else no_update

        @app.callback(
            Output({"role": "kpi-tile", "kpi_id": ALL}, "className"),
            Input("kpi-pick", "value"),
            State({"role": "kpi-tile", "kpi_id": ALL}, "id"),
        )
        def _highlight_tile(kpi_id, ids):
            return ["tile sel" if (i or {}).get("kpi_id") == kpi_id else "tile" for i in ids]

        # --- Explore pane (Phase 1) ---------------------------------------
        @app.callback(
            Output("explore-x", "options"),
            Output("explore-x", "value"),
            Output("explore-series", "options"),
            Output("explore-series", "value"),
            Output("explore-measure", "options"),
            Output("explore-measure", "value"),
            Output("explore-filter-col", "options"),
            Output("explore-filter-col", "value"),
            Input("kpi-pick", "value"),
        )
        def _explore_options(kpi_id):
            """Refill the Explore X/Breakdown/Measure/Slice dropdowns from the
            selected KPI's columns. The CHART picker is owned by the recommend
            callback (data-to-viz driven), so it is not set here."""
            m = meta.get(kpi_id)
            if not m:
                return [], None, [], None, [], None, [], None
            dims, measures, date_cols = _classify_columns(m["rows"])
            x_cols = date_cols + dims  # date candidates first (timeline-friendly)
            x_opts = [{"label": c, "value": c} for c in x_cols]
            series_opts = [{"label": c, "value": c} for c in dims]
            measure_opts = [{"label": c, "value": c} for c in measures]
            cfg = m["spec"].config
            # Seed X/measure from the spec when those columns are real candidates.
            x_default = cfg.get("x") if cfg.get("x") in x_cols else (x_cols[0] if x_cols else None)
            m_default = cfg.get("y") if cfg.get("y") in measures else (measures[0] if measures else None)
            return (
                x_opts, x_default,
                series_opts, None,
                measure_opts, m_default,
                series_opts, None,   # slice field options (dims), start unset
            )

        @app.callback(
            Output("explore-chart", "options"),
            Output("explore-chart", "value"),
            Output("explore-reason", "children"),
            Input("kpi-pick", "value"),
            Input("explore-x", "value"),
            Input("explore-series", "value"),
            Input("explore-measure", "value"),
            Input("explore-filter-col", "value"),
            Input("explore-filter-vals", "value"),
        )
        def _explore_recommend(kpi_id, x, series, measure, filter_col, filter_vals):
            """Recommend a chart for the chosen X/Breakdown/Measure (data-to-viz),
            badge each chart's fit, and set the picker to the recommendation. The
            chart dropdown is NOT an input, so a manual chart pick is never reset.
            Recomputes after a slice (the kept rows can change the recommendation)."""
            m = meta.get(kpi_id)
            if not m:
                return _explore_chart_options(), "bar", ""
            rows = _slice_rows(m["rows"], str(filter_col or ""), list(filter_vals or []))
            recommended, reason, fit = _explore_recommendation(
                rows, str(x or ""), str(series) if series else None, str(measure or "")
            )
            opts = _explore_chart_options(recommended=recommended, fit=fit)
            why = (
                [html.Span("Recommended: ", className="why-k"),
                 html.Span(f"{recommended.replace('_', ' ')} — {reason}")]
                if reason else ""
            )
            return opts, recommended, why

        @app.callback(
            Output("explore-filter-vals", "options"),
            Output("explore-filter-vals", "value"),
            Input("kpi-pick", "value"),
            Input("explore-filter-col", "value"),
        )
        def _explore_filter_values(kpi_id, col):
            """Populate the slice VALUES with the distinct values of the chosen
            slice column for this KPI. Clearing the column clears the values."""
            m = meta.get(kpi_id)
            if not m or not col:
                return [], []
            seen: list[str] = []
            for r in m["rows"]:
                v = r.get(col)
                if v is None:
                    continue
                s = str(v)
                if s not in seen:
                    seen.append(s)
            seen.sort()
            return [{"label": s, "value": s} for s in seen], []

        @app.callback(
            Output("explore-graph", "figure"),
            Input("kpi-pick", "value"),
            Input("explore-x", "value"),
            Input("explore-series", "value"),
            Input("explore-measure", "value"),
            Input("explore-chart", "value"),
            Input("explore-filter-col", "value"),
            Input("explore-filter-vals", "value"),
        )
        def _explore_render(kpi_id, x, series, measure, chart_type, filter_col, filter_vals):
            m = meta.get(kpi_id)
            if not m:
                return go.Figure()
            rows = _slice_rows(m["rows"], str(filter_col or ""), list(filter_vals or []))
            return _explore_figure(
                m["spec"], rows,
                x=str(x or ""), series=str(series) if series else None,
                measure=str(measure or ""), chart_type=str(chart_type or ""),
            )

        # --- Card toolbar: Data table toggle + CSV export -----------------
        @app.callback(
            Output("detail-data", "className"),
            Output("detail-table-btn", "className"),
            Input("detail-table-btn", "n_clicks"),
            prevent_initial_call=True,
        )
        def _toggle_data_table(n_clicks):
            """Show/hide the KPI's data table from the card toolbar."""
            shown = bool(n_clicks) and (n_clicks % 2 == 1)
            return (
                "datawrap" if shown else "datawrap hidden",
                "tbtn active" if shown else "tbtn",
            )

        @app.callback(
            Output("detail-download", "data"),
            Input("detail-export-btn", "n_clicks"),
            State("kpi-pick", "value"),
            prevent_initial_call=True,
        )
        def _export_data_csv(n_clicks, kpi_id):
            """Export the KPI's redacted result rows as CSV (never raw PII)."""
            m = meta.get(kpi_id)
            if not n_clicks or not m or not m["rows"]:
                return no_update
            import csv as _csv
            import io as _io

            rows = _redacted_display_rows(workspace, m["rows"])
            cols = list(rows[0].keys())
            buf = _io.StringIO()
            writer = _csv.DictWriter(buf, fieldnames=cols, extrasaction="ignore")
            writer.writeheader()
            writer.writerows(rows)
            return dict(content=buf.getvalue(), filename=f"{kpi_id}_data.csv")

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


def load_kpi_statuses(workspace: Path) -> dict[str, str]:
    """Per-KPI status badge text from the execution-harness + parity evidence.

    "[ok] parity" when the harness passed and engines matched, "[ok]" on a
    pass without a parity record, "" when no evidence exists. Never raises —
    badges are decoration, not a gate. Shared by the static export tiles and
    the live app's tile strip.
    """
    import json as _json

    badges: dict[str, str] = {}
    evidence = workspace / "interns" / "generated" / "evidence"
    try:
        harness = _json.loads(
            (evidence / "kpi_execution_harness.json").read_text(encoding="utf-8")
        )
        for record in harness.get("records") or []:
            if record.get("status") == "passed":
                badges[str(record.get("kpi_id") or "")] = "[ok]"
    except (_json.JSONDecodeError, OSError):
        pass
    try:
        parity = _json.loads(
            (evidence / "engine_parity" / "current.json").read_text(encoding="utf-8")
        )
        for kpi_id, entry in (parity.get("kpis") or {}).items():
            verdict = (entry or {}).get("parity") or {}
            if verdict.get("status") == "match" and badges.get(kpi_id):
                badges[kpi_id] = "[ok] parity"
    except (_json.JSONDecodeError, OSError):
        pass
    badges.pop("", None)
    return badges


_DATA_VIEW_ROWS_LIVE = 200


def _redacted_display_rows(workspace: Path, rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """The display-redacted, row-capped view of a KPI's result rows (the only form
    ever shown or exported — PII patterns + data_policy widening + age Safe Harbor)."""
    from core.governance.data_policy import (
        load_workspace_data_policy,
        policy_redaction_patterns,
    )
    from core.onboarding.kpi.pii_redaction import (
        DEFAULT_PII_COLUMN_PATTERNS,
        redact_rows,
        workspace_aggregate_ages,
    )

    patterns = DEFAULT_PII_COLUMN_PATTERNS + tuple(
        policy_redaction_patterns(load_workspace_data_policy(workspace))
    )
    return redact_rows(
        rows[:_DATA_VIEW_ROWS_LIVE],
        patterns=patterns,
        aggregate_ages=workspace_aggregate_ages(workspace),
    )


def _data_view_table(workspace: Path, rows: list[dict[str, Any]]):
    """The Data table itself (no toolbar/disclosure — the card supplies those).

    A rendered surface, so values pass through display redaction first.
    """
    if not rows:
        return html.Div("(no rows)", className="dm")
    shown = _redacted_display_rows(workspace, rows)
    columns = list(shown[0].keys())

    def cell(value: Any) -> str:
        if isinstance(value, float):
            return f"{value:,.2f}"
        return "" if value is None else str(value)

    note = (
        f"{len(shown)} of {len(rows)}{'+' if len(rows) > len(shown) else ''} rows"
        " · PII display-redacted"
    )
    records = [{c: cell(r.get(c)) for c in columns} for r in shown]
    from dash import dash_table

    table = dash_table.DataTable(
        data=records,
        columns=[{"name": _pretty_label(c), "id": c} for c in columns],
        sort_action="native",
        page_size=20,
        style_table={"maxHeight": "360px", "overflowY": "auto"},
        style_cell={
            "fontFamily": "var(--sans)", "fontSize": "0.78rem",
            "textAlign": "left", "padding": "0.3rem 0.6rem",
            "backgroundColor": "transparent",
        },
        style_header={
            "fontFamily": "var(--mono)", "textTransform": "uppercase",
            "fontSize": "0.62rem", "letterSpacing": "0.1em",
            "backgroundColor": "transparent", "fontWeight": "600",
        },
    )
    return html.Div([html.Div(note, className="dm"), table])


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

    all_panels = spec.config.get("panels") or []
    # Show only the 1-2 highest-value panels by default — same cap as the live app
    # (`_default_panel_idxs`) so the static export is a faithful proxy and a 20-KPI
    # board isn't a wall of 100 charts. The Explore preview below covers the rest.
    panels = [all_panels[i] for i in _default_panel_idxs(all_panels)] if all_panels else []
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
    "_classify_columns",
    "_explore_figure",
    "_explore_chart_options",
    "_drill_model",
    "_drill_into",
    "_drill_panels",
    "_filter_rows_by_value",
]
