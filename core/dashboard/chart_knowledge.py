"""Chart-selection knowledge base derived from data-to-viz.com.

The panel decider used to pick chart types with hardcoded cardinality
if-chains. This module replaces that with the explicit decision framework
from https://www.data-to-viz.com/ (input data shape -> ranked chart
candidates, plus caveat rules), so chart selection is principled, citable,
and extensible instead of bounded by whatever branches someone wrote.

Every recommendation carries the rule that fired and its source URL; the
panel spec records them as ``selection_reason`` / ``selection_source`` so a
reviewer can trace WHY a chart was chosen. To refresh the knowledge against
the live sites, re-run the exploration documented in
``docs/reference/chart_selection_guide.md`` and update the rules here —
selection itself never fetches the network (determinism, offline, tests).

Scope note: KPI result views are pre-aggregated (GROUP BY), so the
distribution family (violin / boxplot / histogram / ridgeline) can never
apply — there is no row-level distribution left to draw. The families that
CAN occur are encoded below.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

DATA_TO_VIZ = "https://www.data-to-viz.com/"
DATAVIZ_PROJECT = "https://datavizproject.com/"

# Above this distinct-count a categorical breakdown becomes a ranked top-N.
RANKED_CARDINALITY = 15
# At or below this distinct-count a share split reads as a composition.
DONUT_CARDINALITY = 5
# Part-to-whole with more categories than a donut can hold -> treemap.
TREEMAP_CARDINALITY = 15
# data-to-viz: "many bars of similar height" read better as lollipops.
# Relative spread = (max - min) / max across the plotted values.
LOLLIPOP_SPREAD = 0.35
# data-to-viz spaghetti caveat: more series than this must not be overlaid.
MAX_LINE_SERIES = 6
# Heatmap needs both axes readable: cap each side's cardinality.
HEATMAP_MAX_CARDINALITY = 20


@dataclass(frozen=True)
class ChartChoice:
    chart_type: str
    reason: str
    source: str = DATA_TO_VIZ
    modifiers: dict[str, Any] = field(default_factory=dict)

    def spec_fields(self) -> dict[str, Any]:
        return {
            "chart_type": self.chart_type,
            "selection_reason": self.reason,
            "selection_source": self.source,
            **self.modifiers,
        }


def choose_trend_chart(*, series_count: int, is_share: bool) -> ChartChoice:
    """Ordered-numeric / time-series family (data-to-viz family 10)."""
    if series_count > MAX_LINE_SERIES:
        return ChartChoice(
            "line",
            f"time series with {series_count} series exceeds the overlay cap "
            f"({MAX_LINE_SERIES}); rendered without a color split to avoid a "
            "spaghetti chart (data-to-viz caveat)",
            modifiers={"drop_color": True},
        )
    if is_share and series_count > 1:
        return ChartChoice(
            "stacked_area",
            "share evolving over time across few series reads as a stacked "
            "composition (data-to-viz: evolution + part-to-whole)",
        )
    return ChartChoice(
        "line",
        "one value per period (with at most a handful of series) is the "
        "canonical evolution chart (data-to-viz: time series family)",
    )


def choose_categorical_chart(
    *,
    distinct: int,
    is_share: bool,
    value_spread: float | None = None,
    is_ordinal: bool = False,
) -> ChartChoice:
    """One-categorical + one-numeric family (data-to-viz families 5 and 7).

    ``value_spread`` is (max-min)/max over the aggregated values when known;
    None means unknown (falls back to bar shapes). ``is_ordinal`` marks
    naturally-ordered categories (age bands, numeric buckets): their ORDER is
    information, so unordered shapes (donut, treemap, value-ranked anything)
    are never chosen for them.
    """
    if is_ordinal:
        return ChartChoice(
            "bar",
            "ordinal categories (bands/buckets) carry order as information; "
            "an ordered bar preserves it where donuts/treemaps would not "
            "(data-to-viz: ordering principle)",
        )
    if distinct > RANKED_CARDINALITY:
        if value_spread is not None and value_spread < LOLLIPOP_SPREAD:
            return ChartChoice(
                "lollipop",
                "top-N entities whose values are within "
                f"{int(LOLLIPOP_SPREAD * 100)}% of each other render as a "
                "Cleveland dot plot on a value-zoomed axis — position (not "
                "bar length) encodes, so near-equal values stay separable "
                "(data-to-viz: lollipop/dot for many similar-height bars)",
                modifiers={"limit": 10, "orientation": "h"},
            )
        return ChartChoice(
            "ranked_bar",
            f"{distinct} distinct entities exceed the readable bar count; "
            "rank and keep the top N (data-to-viz: ordered barplot)",
            modifiers={"limit": 10, "orientation": "h"},
        )
    if is_share and distinct <= DONUT_CARDINALITY:
        return ChartChoice(
            "donut",
            "a share split across a handful of categories is a composition; "
            "slices are annotated directly and sum to 100% (data-to-viz pie "
            "caveats honored: few slices, direct labels, no legend)",
        )
    if is_share and distinct <= TREEMAP_CARDINALITY:
        return ChartChoice(
            "treemap",
            "part-to-whole across more categories than a donut can hold "
            "legibly (data-to-viz: treemap for one-categorical proportions)",
        )
    if value_spread is not None and value_spread < LOLLIPOP_SPREAD and distinct >= 6:
        return ChartChoice(
            "lollipop",
            "similar-height categories render as a Cleveland dot plot on a "
            "value-zoomed axis: position encodes the value, keeping near-equal "
            "categories separable (data-to-viz recommendation)",
        )
    return ChartChoice(
        "bar",
        "few categories with distinct magnitudes: the ordered barplot is the "
        "default comparison chart (data-to-viz: barplot family)",
    )


def choose_two_categorical_chart(
    *, distinct_a: int, distinct_b: int
) -> ChartChoice | None:
    """Two-categoricals + measure family (data-to-viz family 6/8).

    Returns None when either side is too wide for a readable heatmap.
    """
    if 2 <= distinct_a <= HEATMAP_MAX_CARDINALITY and 2 <= distinct_b <= HEATMAP_MAX_CARDINALITY:
        return ChartChoice(
            "heatmap",
            "two categorical dimensions against one measure: a heatmap shows "
            "the interaction no single-dimension panel can (data-to-viz: "
            "heatmap for two categorical variables)",
        )
    return None


_LEADING_NUM = __import__("re").compile(r"^\s*(\d+(?:\.\d+)?)")
_GEO_LAT_RE = __import__("re").compile(r"^(lat|latitude)$", 2)  # re.IGNORECASE
_GEO_LON_RE = __import__("re").compile(r"^(lon|lng|long|longitude)$", 2)


def detect_geo_columns(
    column_names: list[str], sample_values: dict[str, list[Any]]
) -> tuple[str, str] | None:
    """(lat_column, lon_column) when the data carries plottable coordinates.

    Evidence-based per the repo's derive-don't-curate rule: the NAME must look
    geographic AND the VALUES must sit in valid coordinate ranges — a column
    merely named "lat" full of row ids must not unlock the map family.
    """
    def in_range(col: str, lo: float, hi: float) -> bool:
        vals = [v for v in sample_values.get(col, []) if isinstance(v, (int, float))]
        return bool(vals) and all(lo <= v <= hi for v in vals)

    lat = next((c for c in column_names if _GEO_LAT_RE.match(c) and in_range(c, -90, 90)), "")
    lon = next((c for c in column_names if _GEO_LON_RE.match(c) and in_range(c, -180, 180)), "")
    return (lat, lon) if lat and lon else None


def choose_geo_chart(*, point_count: int) -> ChartChoice:
    """Geographic family (data-to-viz family 13): coordinates + a value."""
    return ChartChoice(
        "bubble_map",
        "rows carry coordinate columns: a value at locations is the bubble-map "
        "case (data-to-viz: map family, coordinate-based with value); "
        f"{point_count} points",
    )


def choose_two_numeric_chart(*, row_count: int) -> ChartChoice:
    """Two-numerics family (data-to-viz family 2): relationship between
    measures."""
    return ChartChoice(
        "scatter",
        "two numeric measures per row: the relationship question is a scatter "
        "plot (data-to-viz: two numeric variables family)",
    )


def choose_distribution_chart(*, row_count: int) -> ChartChoice:
    """One-numeric distribution family (data-to-viz family 1): row-level
    values, not an aggregate."""
    return ChartChoice(
        "histogram",
        "result rows are row-level readings (no grouping dimension collapses "
        "them): the value's distribution is the chart (data-to-viz: one "
        "numeric variable family; try different bin sizes caveat applies)",
    )


def is_ordinal_categories(values: list[Any]) -> bool:
    """True when every distinct category starts with a number ("0-9", "10-19",
    "60") — banded/bucketed categories whose order is information."""
    uniq = {str(v) for v in values if v is not None}
    if len(uniq) < 2:
        return False
    return all(_LEADING_NUM.match(v) for v in uniq)


def value_spread(values: list[float]) -> float | None:
    """Relative spread (max-min)/max of positive aggregated values."""
    nums = [v for v in values if isinstance(v, (int, float))]
    if len(nums) < 2:
        return None
    top = max(nums)
    if top <= 0:
        return None
    return (top - min(nums)) / top


__all__ = [
    "ChartChoice",
    "DATA_TO_VIZ",
    "DATAVIZ_PROJECT",
    "DONUT_CARDINALITY",
    "HEATMAP_MAX_CARDINALITY",
    "LOLLIPOP_SPREAD",
    "MAX_LINE_SERIES",
    "RANKED_CARDINALITY",
    "TREEMAP_CARDINALITY",
    "choose_categorical_chart",
    "choose_distribution_chart",
    "choose_geo_chart",
    "choose_trend_chart",
    "choose_two_categorical_chart",
    "choose_two_numeric_chart",
    "detect_geo_columns",
    "is_ordinal_categories",
    "value_spread",
]
