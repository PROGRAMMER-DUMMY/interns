"""Engine-neutral structured KPI intent.

The SQL path composes SQL text directly (``result_view_builder.parse_kpi``). The
imperative engines (Polars, PySpark) need the *same* understanding of a KPI but
expressed as structured data they can render in their own API — not SQL strings.

This module is that shared structured layer. It reuses the exact regexes and
column-resolution helpers the SQL builder uses, so all three engines agree on
what a metric, a cut, a time bucket, an age derivation, and a filter mean. This
is what keeps the engines from drifting (the root cause of the Polars/PySpark
`Month`/`Age`/missing-filter bugs).

Everything is derived from the KPI text + feature mapping. No domain vocabulary
is hardcoded.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

from core.onboarding.kpi.result_view_builder import (
    _AGE_PATTERN,
    _AGG_FN_PATTERN,
    _DAYS_SINCE_PATTERN,
    _TOP_N_IN_NAME,
    _column_lookup,
    _detect_time_bucket,
    _detect_window_intent,
    _norm_alias,
    _resolve_column,
    _split_cuts,
)

# Window kinds the imperative engines cannot yet render faithfully; generators
# must fail loud rather than emit a silently-different (simpler) query.
_UNSUPPORTED_WINDOW_KINDS = {"running_total", "moving_average", "rank_within"}


@dataclass(frozen=True)
class MetricIntent:
    fn: str                 # sum | avg | count | min | max
    column: str             # "" means count(*)
    distinct: bool
    alias: str


@dataclass(frozen=True)
class DimIntent:
    kind: str               # column | time | age | days_since
    column: str             # resolved source column ("" for count-style)
    alias: str
    unit: str = ""          # time bucket unit: year/quarter/month/week/day


@dataclass(frozen=True)
class FilterIntent:
    target: str             # column name, or "__age__" for an age-derived threshold
    op: str                 # = < > <= >= !=
    value: str
    is_literal: bool


@dataclass(frozen=True)
class ShareIntent:
    """A percentage-share computation: base / scope * 100.

    kind:
      percent_of_total            — base agg divided by the grand total
      percent_of_group            — base agg divided by the per-`partition` total
      mismatched_grain_percentage — per-`partition` distinct count over the grand total
    """
    kind: str
    metric: MetricIntent
    partition: str = ""
    alias: str = "percentage_share"


@dataclass(frozen=True)
class KPIIntent:
    metric: MetricIntent | None = None
    dims: list[DimIntent] = field(default_factory=list)
    filters: list[FilterIntent] = field(default_factory=list)
    top_n: int | None = None
    ratio: tuple[MetricIntent, MetricIntent] | None = None
    share: ShareIntent | None = None
    unsupported_window: str = ""   # non-empty => imperative engines must fail loud


_FILTER_TOKEN = re.compile(
    r"^\s*([A-Za-z_][\w ]*?)\s*(<=|>=|!=|=|<|>)\s*'?([^']+?)'?\s*$"
)
_NAME_AGE_THRESHOLD = re.compile(
    r"\b(?:above|over|older\s+than|greater\s+than|>\s*=?)\s*(\d+)\s*(?:years?(?:\s+of\s+age)?)?",
    re.IGNORECASE,
)


def parse_metric(metric_text: str, lookup: dict[str, str]) -> MetricIntent | None:
    text = (metric_text or "").strip()
    if not text:
        return None
    if re.match(r"\bcount\s*\(\s*\*\s*\)", text, re.IGNORECASE):
        return MetricIntent(fn="count", column="", distinct=False, alias="row_count")
    match = _AGG_FN_PATTERN.search(text)
    if not match:
        return None
    fn = match.group(1).lower()
    distinct = bool(match.group(2))
    column = _resolve_column(match.group(3).strip(), lookup)
    alias = _norm_alias(f"{fn}_{('distinct_' if distinct else '')}{column}")
    return MetricIntent(fn=fn, column=column, distinct=distinct, alias=alias)


def parse_intent(kpi: dict[str, Any]) -> KPIIntent:
    lookup = _column_lookup(kpi)
    metric_text = str(kpi.get("metric") or "")
    cuts_text = str(kpi.get("cuts") or "")
    name_text = str(kpi.get("name") or kpi.get("business_question") or "")

    metric = parse_metric(metric_text, lookup)
    ratio, share, unsupported = _parse_window_or_ratio(metric_text, name_text, metric, lookup)
    dims: list[DimIntent] = []
    filters: list[FilterIntent] = []

    for token in _split_cuts(cuts_text):
        filt = _filter_from_token(token, lookup)
        if filt:
            filters.append(filt)
            continue
        bucket = _detect_time_bucket(token)
        if bucket:
            unit, source, alias = bucket
            col = _resolve_column(source or alias, lookup)
            # Use the alias _detect_time_bucket returns (the unit, e.g. "month")
            # so every engine names the bucket column exactly like the SQL path
            # (`date_trunc('month', ...) AS month`). A divergent "<unit>_bucket"
            # alias here broke cross-engine row parity.
            dims.append(DimIntent(kind="time", column=col, alias=alias, unit=unit))
            continue
        if _AGE_PATTERN.search(token):
            source = _age_source(token)
            col = _resolve_column(source, lookup) if source else ""
            dims.append(DimIntent(kind="age", column=col, alias="age"))
            continue
        days = _DAYS_SINCE_PATTERN.search(token)
        if days:
            col = _resolve_column(days.group(1), lookup)
            dims.append(DimIntent(kind="days_since", column=col, alias=f"days_since_{_norm_alias(col)}"))
            continue
        clean = re.sub(r"\(.*?\)", "", token).strip()
        if clean:
            col = _resolve_column(clean, lookup)
            dims.append(DimIntent(kind="column", column=col, alias=_norm_alias(col)))

    # Name-derived age threshold (e.g. "patients above 50 years") → filter on age.
    age_match = _NAME_AGE_THRESHOLD.search(name_text)
    if age_match and any(d.kind == "age" for d in dims):
        filters.append(FilterIntent(target="__age__", op=">", value=age_match.group(1), is_literal=False))

    # Name-derived categorical filter: "for <Value> <colref>" where colref matches a
    # column dimension's name, alias, or first-letter abbreviation (e.g. LOB).
    for dim in [d for d in dims if d.kind == "column"]:
        for ref in _reference_names(dim):
            prose = re.search(
                rf"\b(?:for|in)\s+([A-Za-z]\w*)\s+{re.escape(ref)}\b",
                name_text, re.IGNORECASE,
            )
            if prose:
                value = prose.group(1).strip().title()
                if value and not any(
                    f.target == dim.column and f.value.lower() == value.lower() for f in filters
                ):
                    filters.append(FilterIntent(target=dim.column, op="=", value=value, is_literal=True))
                break

    top_n = None
    top_match = _TOP_N_IN_NAME.search(name_text)
    if top_match:
        try:
            top_n = int(top_match.group(1))
        except (TypeError, ValueError):
            top_n = None

    return KPIIntent(
        metric=metric,
        dims=dims,
        filters=filters,
        top_n=top_n,
        ratio=ratio,
        share=share,
        unsupported_window=unsupported,
    )


def _parse_window_or_ratio(
    metric_text: str,
    name_text: str,
    metric: MetricIntent | None,
    lookup: dict[str, str],
) -> tuple[tuple[MetricIntent, MetricIntent] | None, ShareIntent | None, str]:
    # Nested aggregation, e.g. avg(sum(x)) or count(distinct sum(...)) — a KPI built
    # on top of another aggregation. Not yet renderable in the imperative engines;
    # fail loud rather than emit a single-level (wrong) aggregation.
    if re.search(
        r"\b(?:sum|avg|count|min|max)\s*\(\s*[^)]*\b(?:sum|avg|count|min|max)\s*\(",
        metric_text, re.IGNORECASE,
    ):
        return None, None, "nested_aggregation"

    intent = _detect_window_intent(metric_text, name_text)
    kind = intent.get("kind", "")
    if kind in _UNSUPPORTED_WINDOW_KINDS:
        return None, None, kind
    if kind == "mismatched_grain_percentage":
        inner = _AGG_FN_PATTERN.search(metric_text)
        partition = _resolve_column(intent.get("partition", ""), lookup)
        if inner and partition:
            base = MetricIntent(
                fn=inner.group(1).lower(),
                column=_resolve_column(inner.group(3).strip(), lookup),
                distinct=bool(inner.group(2)),
                alias="share_value",
            )
            return None, ShareIntent(kind=kind, metric=base, partition=partition), ""
        return None, None, ""
    if kind == "percent_of_total" and metric:
        return None, ShareIntent(kind=kind, metric=metric, alias="percent_of_total"), ""
    if kind == "percent_of_group" and metric:
        partition = _resolve_column(intent.get("group", ""), lookup)
        return None, ShareIntent(
            kind=kind, metric=metric, partition=partition,
            alias=f"percent_of_{_norm_alias(partition)}",
        ), ""
    if "/" in metric_text:
        halves = [h.strip() for h in metric_text.split("/", 1)]
        num = parse_metric(halves[0], lookup)
        den = parse_metric(halves[1], lookup) if len(halves) > 1 else None
        if num and den:
            return (num, den), None, ""
    return None, None, ""


def _filter_from_token(token: str, lookup: dict[str, str]) -> FilterIntent | None:
    if not any(op in token for op in ("=", "<", ">")):
        return None
    match = _FILTER_TOKEN.match(token)
    if not match:
        return None
    column = _resolve_column(match.group(1).strip(), lookup)
    value = match.group(3).strip()
    is_literal = not re.fullmatch(r"-?\d+(?:\.\d+)?", value)
    return FilterIntent(target=column, op=match.group(2), value=value, is_literal=is_literal)


def _age_source(token: str) -> str:
    match = _AGE_PATTERN.search(token)
    if not match:
        return ""
    return (match.group(1) or match.group(2) or "").strip()


def _reference_names(dim: DimIntent) -> set[str]:
    col = dim.column
    refs = {col.lower(), dim.alias.lower()}
    camel_words = re.sub(r"([A-Z])", r"_\1", col).strip("_").split("_")
    abbrev = "".join(w[0] for w in camel_words if w).lower()
    if len(abbrev) > 1:
        refs.add(abbrev)
    return {r for r in refs if r}


__all__ = ["MetricIntent", "DimIntent", "FilterIntent", "KPIIntent", "parse_intent", "parse_metric"]
