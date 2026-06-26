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


# A measure is currency only when its metric/column names a monetary quantity.
# Reuse the project's single canonical financial vocabulary instead of a private
# (and slightly insurance-leaning) duplicate -- 'amount'/'cost' already cover the
# real cases (paid_amount, claim_cost). Generic; no domain words.
from core.onboarding.workspace.research import GENERIC_FINANCIAL_SEED as _MONEY_TOKENS
_AGG_FUNCS = ("count", "avg", "mean", "median", "stddev", "variance", "sum", "min", "max")


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


# Tokens whose metric is "better when LOWER" -- a down-trend is GOOD, so a ▼
# colors green. Generic business vocabulary (cost/aging/error/wait/denial), no
# workspace words. Everything else defaults to higher-is-better.
_LOWER_BETTER_TOKENS = (
    "cost", "expense", "spend", "denial", "denied", "reject", "error", "defect",
    "aging", "days_in", "days in", "wait", "delay", "lag", "overdue", "outstanding",
    "churn", "loss", "leakage", "backlog", "downtime", "readmission", "readmit",
    "mortality", "complication", "infection", "fraud", "risk",
)


def metric_goal(metric: str, measure: str, title: str = "") -> str:
    """'higher' or 'lower' -- whether a bigger value is better, for semantic delta
    /target coloring. Lower-is-better for cost/denial/aging/error-style metrics;
    higher-is-better otherwise. The KPI title is included so a metric whose
    bad-direction word lives only in the question ("how many were READMITTED")
    is still classified. Generic; no domain assumptions."""
    text = f"{metric} {measure} {title}".lower()
    return "lower" if any(t in text for t in _LOWER_BETTER_TOKENS) else "higher"


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


# Business-label extraction: turn a KPI's QUESTION into a short noun-phrase label
# (a stakeholder reads "Total Encounters", not the measure column "ID"/"row_count").
# Pure heuristic, domain-free: strip question stems + trailing clauses, drop filler,
# prefix the aggregation verb (Avg / % / Top). Generic across workspaces.
_LABEL_STEMS = (
    r"^for each [\w\s]+?,\s*", r"^how many\s+", r"^how much\s+",
    r"^what (?:is|are|was|were)\s+(?:the\s+)?(?:trend (?:for|of|in)\s+)?",
    r"^what percentage of\s+", r"^what proportion of\s+",
    r"^which\s+", r"^what\s+", r"^trend (?:for|of|in)\s+",
)
# When the "%" prefix is added for a share, drop a redundant "percentage share/
# share/percentage" already inside the core (avoids "% Percentage Share Lives").
_LABEL_SHARE_NOISE = r"\b(percentage share|percentage|proportion|share)\b"
_LABEL_TRAIL = (
    r"\s+occurred\b.*$", r"\s+belonged\b.*$", r"\s+performed\b.*$",
    r"\s+broken down by\b.*$", r"\s+for each\b.*$", r"\s+over time\b.*$",
    r"\s+of a previous\b.*$", r"\s+were admitted\b.*$", r"\s+with the\b.*$",
    r"\s+and the\b.*$", r"\s+versus\b.*$", r"\s+within\b.*$", r",.*$",
)
_LABEL_NOISE = (
    r"\b(the|a|an|of|all|were|was|had|is|are|each|most|total number|number"
    r"|times|they|this|represent|does)\b"
)
_LABEL_RANK_NOISE = r"\b(top\s*\d*|highest|lowest|frequent|average|avg|with)\b"
_LABEL_TRAIL_PREP = r"\s+(for|by|in|with|over|to|from|versus|under|and|each)$"


def business_label(metric: str, measure: str, fmt: str, title: str,
                   *, max_words: int = 4) -> str:
    """A short, stakeholder-facing card label derived from the KPI QUESTION --
    'How many total encounters occurred each year?' -> 'Total Encounters';
    'What is the average total claim cost...' -> 'Avg Total Claim Cost'. Falls
    back to the cleaned measure name when the title yields nothing. Generic."""
    raw = title or ""
    t = raw.strip().rstrip("?.").lower()
    if not t:
        return clean_measure_name(metric, measure, fmt)
    is_rank = bool(re.search(r"\btop\s*\d+|\bmost\b|\bhighest\b|\bwhich\b", t))
    for s in _LABEL_STEMS:
        t = re.sub(s, "", t, flags=re.IGNORECASE)
    for tr in _LABEL_TRAIL:
        t = re.sub(tr, "", t, flags=re.IGNORECASE)
    mfn = re.match(r"\s*(\w+)", metric or "")
    fn = mfn.group(1).lower() if mfn else "count"
    is_share = (fmt or "").lower() == "percent" \
        or "percent" in (metric + raw).lower() or "proportion" in t
    t = re.sub(_LABEL_NOISE, " ", t)
    if is_rank or fn in ("avg", "mean"):
        t = re.sub(_LABEL_RANK_NOISE, " ", t)
    if is_share:
        t = re.sub(_LABEL_SHARE_NOISE, " ", t)   # avoid "% Percentage Share ..."
    t = re.sub(r"\s+", " ", t).strip()
    core = " ".join(t.split()[:max_words])
    for _ in range(3):
        core = re.sub(_LABEL_TRAIL_PREP, "", core, flags=re.IGNORECASE).strip()
    core = core.title()
    if is_share:
        return f"% {core}".strip() if core else "Share"
    if is_rank:
        return f"Top {core}".strip() if core else "Top"
    if fn in ("avg", "mean"):
        return f"Avg {core}".strip() if core else "Average"
    return core or clean_measure_name(metric, measure, fmt)


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
    title = str(hints.get("title") or kpi_id)
    # Card label = a business noun-phrase from the QUESTION ("Total Encounters"),
    # not the raw measure column ("ID"/"row_count"). Falls back to the cleaned
    # measure name when the title is just the kpi_id (no real question).
    fmt = measure_fmt(metric, measure, y_format)
    label = business_label(metric, measure, fmt, title) if title != kpi_id \
        else clean_measure_name(metric, measure, y_format)
    return KpiModel(
        kpi_id=kpi_id,
        title=title,
        gold_columns=columns,
        measure=measure,
        cuts=cuts,
        kind=kind,
        additive=additive,
        y_format=y_format,
        panels=panels,
        card_label=label,
        metric=metric,
    )


__all__ = [
    "KpiModel", "build_kpi_model", "classify_measure", "clean_measure_name",
    "business_label", "measure_func", "measure_fmt", "headline_agg", "metric_goal",
]
