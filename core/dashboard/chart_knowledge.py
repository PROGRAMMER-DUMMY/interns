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
                f"{int(LOLLIPOP_SPREAD * 100)}% of each other read better as "
                "lollipops — near-equal bars become an undifferentiated wall "
                "(data-to-viz: lollipop for many similar-height bars)",
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
            "similar-height categories: lollipops emphasize the value mark "
            "over redundant bar ink (data-to-viz recommendation)",
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
    "choose_trend_chart",
    "choose_two_categorical_chart",
    "is_ordinal_categories",
    "value_spread",
]
