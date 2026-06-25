"""Per-KPI model: resolve the measure, the cut dimensions, and how the measure
re-aggregates, from the actual gold schema + the dashboard spec.

Gold is the source of truth for which columns exist (the spec's `_result_columns`
can be stale/broken -- observed on kpi_002). The spec is used only for hints:
the measure name (`y`), display format (`y_format`), aggregation (`agg`), the
metric text, the title, and the recommended panels.

Measure kinds and additivity
-----------------------------
- ``sum``   sum / count style measure        -> ADDITIVE (sum the parts)
- ``share`` percentage share of a whole      -> ADDITIVE under single attribution
            (the platform default: one cell per entity, shares sum to 100%, and
            `share_sum_check` enforces it -- so summing a subset of cells gives
            that subset's share correctly).
- ``ratio`` average / rate / non-decomposable ratio -> NON-ADDITIVE (cannot be
            recombined by summing; roll-up must recompute or stay filter-only).
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

import polars as pl

from core.dashboard.model.aggregate import is_numeric_dtype, resolve_column
from core.storage.workspace_layout import WorkspaceLayout

_SHARE_RE = re.compile(r"(percent|share|proportion)", re.IGNORECASE)
_RATIO_RE = re.compile(r"(avg|average|mean|\bratio\b|\brate\b)", re.IGNORECASE)
_MEASURE_NAME_RE = re.compile(
    r"(sum|count|total|amount|paid|revenue|share|percent|rate|ratio|value|"
    r"metric|score|qty|quantity|volume|cost|price|spend|sales)",
    re.IGNORECASE,
)


@dataclass(frozen=True)
class KpiModel:
    """How to read + re-aggregate one KPI's validated result."""

    kpi_id: str
    title: str                         # the full business question (subtitle/tooltip)
    gold_columns: list[str]
    measure: str                       # actual gold column name
    cuts: list[str]                    # actual gold column names (non-measure)
    kind: str                          # "sum" | "share" | "ratio"
    additive: bool
    y_format: str = ""
    panels: list[dict[str, Any]] = field(default_factory=list)
    card_label: str = ""               # short measure name for KPI cards/nav
    metric: str = ""                   # raw metric expression, e.g. count(distinct Id)


def classify_measure(*, agg: str, y_format: str, metric: str) -> tuple[str, bool]:
    """Return (kind, additive) from spec hints.

    Share-with-single-attribution is additive; only avg/rate/ratio is not.
    Order matters: a "percentage share" metric reads as share (additive), not as
    a generic rate (non-additive), even though the word "rate" may appear nearby.
    """
    yf = (y_format or "").lower()
    metric_l = (metric or "").lower()
    if yf == "percent" or _SHARE_RE.search(metric_l):
        return "share", True
    if (agg or "").lower() in {"avg", "mean"} or _RATIO_RE.search(metric_l):
        return "ratio", False
    return "sum", True


# Generic money tokens (no domain words): a measure is currency only when its
# metric/column names a monetary quantity. Mirrors the financial vocabulary used
# elsewhere; kept here so the dashboard layer has ONE source of truth for
# format/aggregation and never defaults a non-money measure to currency.
_MONEY_TOKENS = (
    "amount", "cost", "price", "revenue", "spend", "sales", "paid", "charge",
    "claim", "fee", "balance", "payment", "income", "margin", "dollars", "usd",
)
_AGG_FUNCS = ("count", "avg", "mean", "median", "sum", "min", "max")


def measure_func(metric: str, measure: str = "") -> str:
    """Leading aggregate function of a KPI measure: count|avg|sum|min|max|median|''.

    Reads the metric expression first (``count(distinct Id)`` -> ``count``); if
    that is empty, falls back to the measure COLUMN name (``avg_base_cost`` ->
    ``avg``, ``count_distinct_id`` -> ``count``). Generic; no domain vocabulary.
    """
    m = (metric or "").strip().lower()
    for fn in _AGG_FUNCS:
        if m.startswith(fn + "(") or m.startswith(fn + " ") or m == fn:
            return "avg" if fn == "mean" else fn
    ml = (measure or "").strip().lower()
    for fn in _AGG_FUNCS:
        if ml.startswith(fn + "_") or ml == fn:
            return "avg" if fn == "mean" else fn
    return ""


def _is_money_measure(metric: str, measure: str) -> bool:
    text = f"{metric} {measure}".lower()
    return any(token in text for token in _MONEY_TOKENS)


def measure_fmt(metric: str, measure: str, y_format: str) -> str:
    """Display format derived from the measure's meaning -- never a flat currency
    default. ``percent`` for shares, ``int`` for counts, ``currency`` only for a
    genuine money quantity, ``float`` for a non-money average."""
    if (y_format or "").lower() == "percent" \
            or _SHARE_RE.search((metric or "").lower()) \
            or _SHARE_RE.search((measure or "").lower()):
        return "percent"
    fn = measure_func(metric, measure)
    if fn == "count":
        return "int"
    if _is_money_measure(metric, measure):
        return "currency"
    if fn in ("avg", "median"):
        return "float"
    return "int"


def headline_agg(metric: str, measure: str, y_format: str) -> str:
    """How to collapse pre-aggregated gold rows into ONE headline number.

    Counts/sums partition cleanly -> ``sum``. Averages must NOT be summed
    (sum-of-averages is meaningless) -> ``avg``. Shares -> ``max`` (largest
    segment, since summing shares = 100%). Generic across workspaces.
    """
    if (y_format or "").lower() == "percent" or _SHARE_RE.search((metric or "").lower()):
        return "max"
    fn = measure_func(metric, measure)
    if fn in ("avg", "median"):
        return "avg"
    if fn == "min":
        return "min"
    if fn == "max":
        return "max"
    return "sum"


def _split_camel(token: str) -> str:
    """'PaidAmount' -> 'Paid Amount'; 'paidamount' -> 'Paidamount'."""
    s = re.sub(r"(?<=[a-z0-9])(?=[A-Z])", " ", str(token))
    s = re.sub(r"[_\s]+", " ", s).strip()
    return s.title()


def clean_measure_name(metric: str, measure: str, y_format: str) -> str:
    """A short, human measure label for KPI cards/nav -- NOT the business question.

    'sum(PaidAmount)' -> 'Paid Amount'; percentage/share metrics -> 'Percentage
    Share'; falls back to the humanized measure column.
    """
    metric_l = (metric or "").lower()
    if (y_format or "").lower() == "percent" or _SHARE_RE.search(metric_l) \
            or _SHARE_RE.search((measure or "").lower()):
        return "Percentage Share"
    inner = re.search(r"\(([^)]+)\)", metric or "")
    token = inner.group(1) if inner else (measure or "")
    token = re.sub(r"\bdistinct\b", "", token, flags=re.IGNORECASE).strip()
    token = re.sub(r"^(sum|avg|mean|count|total|min|max)[_ ]?", "", token, flags=re.IGNORECASE)
    # `count(*)` (and similar) leaves a bare `*`/empty token -- never a label.
    # Fall back to the measure column name so a card never reads "*" / blank.
    token = token.strip(" *")
    label = _split_camel(token)
    return label or _split_camel(measure) or "Value"


def _choose_measure(columns: list[str], schema: dict[str, Any], hint: str | None) -> str:
    """Resolve the measure column: spec `y` if it maps to a real gold column,
    else a measure-named numeric, else the last numeric column (generator puts
    the measure last)."""
    resolved = resolve_column(hint, columns)
    if resolved:
        return resolved
    numeric = [c for c in columns if is_numeric_dtype(schema.get(c))]
    if not numeric:
        return columns[-1] if columns else ""
    for name in numeric:
        if _SHARE_RE.search(name):
            return name
    best = ""
    for name in numeric:
        if _MEASURE_NAME_RE.search(name):
            best = name
    return best or numeric[-1]


def _load_spec_hints(layout: WorkspaceLayout, kpi_id: str) -> dict[str, Any]:
    """Merged spec config (machine_defaults + user_overrides) or {} if absent."""
    try:
        from core.dashboard.spec import load_kpi_spec

        spec = load_kpi_spec(layout, kpi_id)
    except Exception:
        spec = None
    return dict(spec.config) if spec else {}


def build_kpi_model(
    layout: WorkspaceLayout, kpi_id: str, gold: pl.DataFrame
) -> KpiModel:
    """Build the model from the gold frame + spec hints. Gold owns the columns."""
    columns = list(gold.columns)
    schema = dict(gold.schema)
    hints = _load_spec_hints(layout, kpi_id)

    measure = _choose_measure(columns, schema, hints.get("y"))
    cuts = [c for c in columns if c != measure]
    kind, additive = classify_measure(
        agg=str(hints.get("agg") or "sum"),
        y_format=str(hints.get("y_format") or ""),
        metric=str(hints.get("metric") or hints.get("title") or ""),
    )
    panels = hints.get("panels") if isinstance(hints.get("panels"), list) else []
    metric = str(hints.get("metric") or "")
    y_format = str(hints.get("y_format") or "")
    return KpiModel(
        kpi_id=kpi_id,
        title=str(hints.get("title") or kpi_id),
        gold_columns=columns,
        measure=measure,
        cuts=cuts,
        kind=kind,
        additive=additive,
        y_format=y_format,
        panels=panels,
        card_label=clean_measure_name(metric, measure, y_format),
        metric=metric,
    )


__all__ = [
    "KpiModel", "build_kpi_model", "classify_measure", "clean_measure_name",
    "measure_func", "measure_fmt", "headline_agg",
]
