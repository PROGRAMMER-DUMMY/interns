"""Evidence-driven dashboard panel selection.

Generic across workspaces. Instead of choosing a chart from KPI *text*
(keywords like "top N" / "share" / declared cut labels), this module profiles
the ACTUAL executed result rows — column types, distinct counts, constants —
and derives the visualization from that data shape. This is "derive, don't
curate": the chart emerges from evidence, not a hand-written keyword ladder.

Key idea for multi-dimensional KPIs: rather than cram every dimension into one
overloaded chart, emit ONE PANEL PER DIMENSION (the measure broken down by that
single dimension), each panel's chart-type chosen from that dimension's profile.
A 4-dimension KPI becomes a small set of simple, readable charts.

No imports from `renderer`/`spec` — keep it cycle-free so both can use it.
"""
from __future__ import annotations

import re
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path
from typing import Any


_DATE_NAME_RE = re.compile(
    r"(date|month|year|week|day|quarter|timestamp|created|service|invoice|filed|posted)",
    re.IGNORECASE,
)
_MEASURE_NAME_RE = re.compile(
    r"(sum|count|avg|mean|total|amount|paid|revenue|share|percent|rate|ratio|"
    r"value|metric|kpi|score|qty|quantity|volume|cost|price|spend|sales)",
    re.IGNORECASE,
)
_SHARE_NAME_RE = re.compile(r"(percent|share|ratio|rate|proportion)", re.IGNORECASE)

# Cardinality thresholds for chart selection live in
# core.dashboard.chart_knowledge (the data-to-viz knowledge base).

# Cap how many breakdown panels a single KPI emits (most-informative first).
_MAX_PANELS = 5
# A color split is only legible up to this many series.
_MAX_COLOR_SERIES = 6


@dataclass(frozen=True)
class ColumnProfile:
    name: str
    n: int
    distinct: int
    numeric: bool
    temporal: bool

    @property
    def constant(self) -> bool:
        return self.distinct <= 1


def _looks_temporal(name: str, values: list[Any]) -> bool:
    if _DATE_NAME_RE.search(name or ""):
        return True
    sample = [v for v in values if v is not None][:8]
    if not sample:
        return False
    hits = 0
    for v in sample:
        if isinstance(v, (date, datetime)):
            hits += 1
            continue
        if re.match(r"^\d{4}[-/]\d{2}([-/]\d{2})?", str(v).strip()):
            hits += 1
    return hits >= max(1, len(sample) // 2)


def profile_columns(rows: list[dict[str, Any]]) -> dict[str, ColumnProfile]:
    """Profile each result column from the actual rows."""
    profiles: dict[str, ColumnProfile] = {}
    if not rows:
        return profiles
    cols = list(rows[0].keys())
    n = len(rows)
    for col in cols:
        values = [r.get(col) for r in rows]
        non_null = [v for v in values if v is not None]
        numeric = bool(non_null) and all(isinstance(v, (int, float)) and not isinstance(v, bool) for v in non_null)
        distinct = len({str(v) for v in values})
        temporal = (not numeric) and _looks_temporal(col, values)
        profiles[col] = ColumnProfile(
            name=col, n=n, distinct=distinct, numeric=numeric, temporal=temporal
        )
    return profiles


def choose_measure(profiles: dict[str, ColumnProfile], rows: list[dict[str, Any]]) -> str:
    """Pick the measure column: a share/percent-named numeric first, else the
    last measure-named numeric, else the last numeric column (generator puts the
    measure last)."""
    numeric = [p.name for p in profiles.values() if p.numeric]
    if not numeric:
        return ""
    for name in numeric:
        if _SHARE_NAME_RE.search(name):
            return name
    best = ""
    for name in numeric:
        if _MEASURE_NAME_RE.search(name):
            best = name
    return best or numeric[-1]


def _aggregate(rows: list[dict[str, Any]], dim: str, measure: str) -> dict[str, float]:
    out: dict[str, float] = {}
    for r in rows:
        k = str(r.get(dim))
        v = r.get(measure)
        if isinstance(v, (int, float)):
            out[k] = out.get(k, 0.0) + v
    return out


def _informativeness(rows: list[dict[str, Any]], dim: str, measure: str) -> float:
    """How much the measure varies across a dimension's values (coefficient of
    variation). Higher = the dimension differentiates the measure more = more
    worth charting. Used to order/select panels generically."""
    totals = list(_aggregate(rows, dim, measure).values())
    if len(totals) < 2:
        return 0.0
    mean = sum(totals) / len(totals)
    if mean == 0:
        return 0.0
    var = sum((t - mean) ** 2 for t in totals) / len(totals)
    return (var ** 0.5) / abs(mean)


def _should_log_scale(rows: list[dict[str, Any]], dim: str, measure: str) -> bool:
    """Derive log-vs-linear from the measure's distribution across a dimension.

    Log helps when the aggregated values span orders of magnitude — on a linear
    axis the small categories collapse to invisible slivers next to the large
    ones. Requires strictly positive values (log is undefined at <=0). The
    order-of-magnitude span is a property of the DATA, not a tunable business
    threshold: ratio >= 50 (~1.7 decades) is where linear stops being readable.
    """
    totals = [v for v in _aggregate(rows, dim, measure).values() if v is not None]
    positive = [v for v in totals if v > 0]
    if len(positive) < 2 or len(positive) != len(totals):
        return False  # any non-positive value -> log is invalid
    return (max(positive) / min(positive)) >= 50.0


def _humanize(name: str) -> str:
    return re.sub(r"[_\s]+", " ", str(name)).strip().title()


def decide_panels(
    rows: list[dict[str, Any]], definition: dict[str, Any]
) -> list[dict[str, Any]]:
    """Derive a list of panel specs from the result data shape.

    Each panel is a self-contained chart dict: chart_type, x, y, optional color,
    agg, limit, y_format, title. One panel per informative dimension (the measure
    broken down by that dimension), plus a trend panel when a temporal column is
    present. Constant columns are ignored (a filter-pinned value adds nothing) —
    this generically subsumes the "rank by a non-constant column" rule.

    Returns [] when there are no rows (caller falls back to name-based inference).
    """
    if not rows:
        return []
    profiles = profile_columns(rows)
    measure = choose_measure(profiles, rows)
    metric_text = str(definition.get("metric") or "")
    is_share = bool(measure and _SHARE_NAME_RE.search(measure)) or bool(_SHARE_NAME_RE.search(metric_text))

    # Dimensions = non-measure, non-constant columns.
    dims = [
        p.name
        for p in profiles.values()
        if p.name != measure and not p.constant
    ]
    temporal_dims = [d for d in dims if profiles[d].temporal]
    cat_dims = [d for d in dims if not profiles[d].temporal]

    if not measure:
        # No numeric measure — nothing to chart meaningfully.
        return [{"chart_type": "big_number", "y": next(iter(profiles), ""), "title": _humanize(metric_text or "KPI")}]

    if not dims:
        return [{"chart_type": "big_number", "y": measure, "title": _humanize(metric_text or measure)}]

    panels: list[dict[str, Any]] = []

    from core.dashboard.chart_knowledge import (
        choose_categorical_chart,
        choose_distribution_chart,
        choose_geo_chart,
        choose_trend_chart,
        choose_two_categorical_chart,
        choose_two_numeric_chart,
        detect_geo_columns,
        is_ordinal_categories,
        value_spread,
    )

    # Family detection beyond measure+categorical (sensor/geo/raw workspaces):
    # these shapes existed nowhere in the original KPI corpus but the knowledge
    # base covers their data-to-viz families generically.
    sample_values = {c: [r.get(c) for r in rows[:64]] for c in (rows[0] if rows else {})}
    geo = detect_geo_columns(list(sample_values), sample_values)
    if geo:
        lat, lon = geo
        choice = choose_geo_chart(point_count=len(rows))
        panels.append({
            **choice.spec_fields(),
            "lat": lat, "lon": lon, "y": measure,
            "title": f"{_humanize(measure)} by Location",
        })
        cat_dims = [d for d in cat_dims if d not in (lat, lon)]

    # Second numeric measure -> relationship panel (scatter).
    other_numeric = [
        p.name for p in profiles.values()
        if p.numeric and p.name != measure and not p.constant
        and p.name not in (geo or ())
        and p.distinct > max(8, p.n // 10)  # continuous, not a coded category
    ]
    if other_numeric:
        second = other_numeric[0]
        choice = choose_two_numeric_chart(row_count=len(rows))
        panels.append({
            **choice.spec_fields(),
            "x": second, "y": measure,
            "title": f"{_humanize(measure)} vs {_humanize(second)}",
        })
        cat_dims = [d for d in cat_dims if d != second]

    # Row-level (unaggregated) values: every dimension is near-unique, so the
    # measure's DISTRIBUTION is the story (impossible on GROUP BY results).
    n_rows = len(rows)
    row_level = bool(measure) and n_rows >= 30 and all(
        profiles[d].distinct >= n_rows * 0.9 for d in dims
    )
    if row_level:
        choice = choose_distribution_chart(row_count=n_rows)
        panels.append({
            **choice.spec_fields(),
            "x": measure, "y": measure,
            "title": f"Distribution of {_humanize(measure)}",
        })

    # Trend panel: measure over time, optionally split by the smallest cat dim.
    if temporal_dims:
        t = temporal_dims[0]
        color = ""
        small_cats = sorted(cat_dims, key=lambda d: profiles[d].distinct)
        if small_cats:
            color = small_cats[0]
        series_count = profiles[color].distinct if color else 1
        choice = choose_trend_chart(series_count=series_count, is_share=is_share)
        panel = {
            **choice.spec_fields(),
            "x": t, "y": measure, "agg": "sum",
            "title": f"{_humanize(measure)} over {_humanize(t)}",
        }
        if color and not panel.pop("drop_color", False):
            panel["color"] = color
        if is_share:
            panel["y_format"] = "percent"
        if panel["chart_type"] == "line" and _should_log_scale(rows, t, measure):
            panel["log_scale"] = True
        panels.append(panel)

    # One breakdown panel per categorical dimension, ordered by informativeness.
    # Chart types come from the data-to-viz knowledge base (chart_knowledge),
    # not hardcoded branches: each panel records WHY its chart was chosen.
    ranked_cats = sorted(
        cat_dims, key=lambda d: _informativeness(rows, d, measure), reverse=True
    )
    for d in ranked_cats:
        p = profiles[d]
        spread = value_spread(list(_aggregate(rows, d, measure).values()))
        choice = choose_categorical_chart(
            distinct=p.distinct,
            is_share=is_share,
            value_spread=spread,
            is_ordinal=is_ordinal_categories([r.get(d) for r in rows]),
        )
        panel: dict[str, Any] = {
            **choice.spec_fields(),
            "x": d, "y": measure, "agg": "sum",
            "title": f"{_humanize(measure)} by {_humanize(d)}",
        }
        if is_share:
            panel["y_format"] = "percent"
        panels.append(panel)

    # Interaction panel: two categorical dimensions against the measure
    # (data-to-viz family 6) — shows what no single-dimension panel can.
    if len(ranked_cats) >= 2:
        a, b = ranked_cats[0], ranked_cats[1]
        pair = choose_two_categorical_chart(
            distinct_a=profiles[a].distinct, distinct_b=profiles[b].distinct
        )
        if pair is not None:
            panel = {
                **pair.spec_fields(),
                "x": a, "color": b, "y": measure, "agg": "sum",
                "title": f"{_humanize(measure)}: {_humanize(a)} x {_humanize(b)}",
            }
            if is_share:
                panel["y_format"] = "percent"
            panels.append(panel)

    return panels[:_MAX_PANELS]


@contextmanager
def _at_repo_root(repo_root: Path):
    import os
    old = os.getcwd()
    try:
        os.chdir(repo_root)
        yield
    finally:
        os.chdir(old)


def execute_result_view(
    repo_root: Path, sql_path: Path, kpi_id: str, *, limit: int = 5000
) -> list[dict[str, Any]]:
    """Execute a KPI's SQL and return rows of its ``<kpi_id>_results`` view.

    Cycle-free duplicate of the renderer's executor so spec-build can profile
    data without importing the renderer. Empty on any failure — never raises.
    """
    if not sql_path or not Path(sql_path).exists():
        return []
    try:
        import duckdb
    except Exception:
        return []
    try:
        sql_text = Path(sql_path).read_text(encoding="utf-8")
    except OSError:
        return []
    view = f"{kpi_id}_results"
    try:
        with _at_repo_root(repo_root):
            conn = duckdb.connect(":memory:")
            try:
                conn.execute(sql_text)
                cur = conn.execute(f'SELECT * FROM "{view}" LIMIT {int(limit)}')
                cols = [d[0] for d in cur.description]
                return [dict(zip(cols, row)) for row in cur.fetchall()]
            finally:
                conn.close()
    except Exception:
        return []


__all__ = [
    "ColumnProfile",
    "profile_columns",
    "choose_measure",
    "decide_panels",
    "execute_result_view",
]
