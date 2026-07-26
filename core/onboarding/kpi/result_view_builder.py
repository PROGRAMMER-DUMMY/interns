"""Generic KPI result-view SQL builder.

Reads the KPI's structured fields (`metric`, `cuts`, business question,
`features`) and composes a workspace-agnostic aggregated SELECT.
Replaces the prior pattern of hardcoded KPI-name SQL templates.

Generic across all workspaces. Zero domain vocabulary baked in.

Inputs (every KPI registry already carries these):
- `metric`  — `sum(col)` / `avg(col)` / `count(*)` / `count(distinct col)` /
              `min(col)` / `max(col)` / ratio of two of the above
- `cuts`    — comma-separated dimensions; tokens like "Month", "Quarter",
              "Year" get bucketed; tokens with `=`/`>`/`<` become filters
- `name`    — used only to detect "top N" ranking hints
- `features` — for column-name resolution via source_columns

Output: a complete CREATE OR REPLACE VIEW <kpi_id>_results AS SELECT ...
statement. For metrics too complex to parse (e.g., "percentage of X / Y for Z"
mismatched-grain ratios), emits a clearly-marked fallback with a TODO
comment instead of silently producing wrong SQL.
"""
from __future__ import annotations

import os
import re
from dataclasses import dataclass, field, replace
from datetime import date, datetime, timezone
from difflib import SequenceMatcher
from typing import Any


def _as_of_date() -> str:
    """The as-of date pinned into generated date arithmetic (ISO `YYYY-MM-DD`).

    Overridable via `AUTORESEARCH_AS_OF_DATE` so a backfill or a reproduction run
    can regenerate the exact SQL a previous run emitted. Defaults to today (UTC).
    """
    override = (os.environ.get("AUTORESEARCH_AS_OF_DATE") or "").strip()
    if override:
        try:
            return date.fromisoformat(override).isoformat()
        except ValueError:
            pass  # malformed override: fall through to today rather than emit junk
    return datetime.now(timezone.utc).date().isoformat()


_TIME_BUCKET_PATTERNS: list[tuple[str, str]] = [
    ("year", "year"),
    ("quarter", "quarter"),
    ("month", "month"),
    ("week", "week"),
    ("day", "day"),
]
_AGG_FN_PATTERN = re.compile(
    r"\b(sum|avg|mean|count|min|max|median|stddev|std|variance|var)\s*"
    r"\(\s*(distinct\s+|disitnct\s+)?([^()]+?)\s*\)",
    re.IGNORECASE,
)
# Canonicalise aggregate-function aliases to one name per dialect renderer.
_AGG_FN_ALIASES = {"mean": "avg", "std": "stddev", "var": "variance"}
_PREDICATE_IN_COUNT = re.compile(r"^\s*([A-Za-z_][A-Za-z0-9_]*)\s*=\s*['\"]([^'\"]+)['\"]\s*$")
_TOP_N_IN_NAME = re.compile(r"\btop\s+(\d+)\b", re.IGNORECASE)
# Ranking intent without an explicit "top N": "which X had the MOST Y",
# "the HIGHEST / LARGEST ...". Ranks the measure DESC and caps the result so a
# leaderboard question returns the leaders, not every row. DESC-only words on
# purpose (fewest/lowest would need ASC, handled separately if needed).
_RANK_HINT = re.compile(r"\b(?:most|highest|largest|greatest|maximum)\b", re.IGNORECASE)
_DEFAULT_RANK_LIMIT = 20
# Temporal-grain intent stated only in the QUESTION (not in cuts): "each quarter
# over time", "per month", "monthly trend". Generalises kpi_008 -- the cut was
# empty but the question asked for a per-period breakdown. Captures the period.
_PROSE_TEMPORAL = re.compile(
    r"\b(?:each|per|every|by)\s+(year|quarter|month|week|day)\b"
    r"|\b(year|quarter|month|week|dai)ly\b"
    r"|\b(year|quarter|month|week|day)\s+over\s+(?:the\s+)?(?:year|time)\b",
    re.IGNORECASE,
)
# Date-ish column-name hints (generic; no domain words). Used only to pick a
# date column that is ALREADY a resolved feature -- never to invent one.
_DATE_COL_HINT = ("date", "start", "stop", "_at", "timestamp", "time")


def _prose_temporal_unit(name_text: str) -> str | None:
    """The period a question asks to break out by ('each quarter over time' ->
    'quarter'), or None. 'daily' normalises to 'day'."""
    m = _PROSE_TEMPORAL.search(name_text or "")
    if not m:
        return None
    unit = next((g for g in m.groups() if g), "")
    return "day" if unit == "dai" else (unit or None)


def _date_column_from_lookup(lookup: dict[str, str]) -> str | None:
    """A date-typed column already in the resolved feature set, by name hint.
    Prefers an event/start column. Returns the physical column or None -- never
    fabricates a column that is not a resolved feature."""
    cols = [c for c in lookup.values() if c]
    for pref in ("start", "service", "event", "order", "date"):
        for c in cols:
            if pref in c.lower():
                return c
    for c in cols:
        if any(h in c.lower() for h in _DATE_COL_HINT):
            return c
    return None
_TIME_BUCKET_HINT = re.compile(
    r"\b(year|quarter|month|week|day)\b(?:\s*\(\s*([A-Za-z_][A-Za-z0-9_]*)\s*\))?",
    re.IGNORECASE,
)
_COMPARISON_FILTER = re.compile(
    r"([A-Za-z_][A-Za-z0-9_]*)\s*([=<>!]+)\s*(?:['\"]([^'\"]+)['\"]|([A-Za-z0-9_.]+))"
)
# NOTE: both alternatives need a leading word boundary. Without `\b` on the
# second one, "age of/from <word>" matches INSIDE other words — e.g.
# "percent`age of` total" or "aver`age of` order_value" — and wrongly treats the
# trailing noun ("total", "order_value") as an age/date column. That produced
# `date_diff('year', CAST("total" AS DATE)) AS age` on a percentage KPI.
_AGE_PATTERN = re.compile(
    r"\bage\s*\(\s*([A-Za-z_][A-Za-z0-9_]*)\s*\)|\bage\s+(?:from|of)\s+([A-Za-z_][A-Za-z0-9_]*)",
    re.IGNORECASE,
)
_DAYS_SINCE_PATTERN = re.compile(
    r"\bdays?\s+since\s+([A-Za-z_][A-Za-z0-9_]*)", re.IGNORECASE,
)
_HAVING_PATTERN = re.compile(
    r"\b(?:with|having)\s+(?:at\s+least|more\s+than|over|above|>\s*=?|>)\s*(\d+)\s+([A-Za-z_][A-Za-z0-9_ ]+?)(?:\b|$)",
    re.IGNORECASE,
)
_PCT_OF_TOTAL_PATTERN = re.compile(
    r"\b(?:percent(?:age)?|share)\s+of\s+total\b", re.IGNORECASE,
)
_SHARE_OF_GROUP_PATTERN = re.compile(
    r"\b(?:percent(?:age)?|share)\s+of\s+([A-Za-z_][A-Za-z0-9_]*)\b", re.IGNORECASE,
)
_RUNNING_TOTAL_PATTERN = re.compile(
    r"\b(?:running|cumulative)\s+(?:total|sum|count)\b", re.IGNORECASE,
)
_MOVING_AVG_PATTERN = re.compile(
    r"\b(?:moving|rolling)\s+(?:avg|average|mean)\s+(\d+)\b", re.IGNORECASE,
)
_RANK_WITHIN_PATTERN = re.compile(
    r"\brank(?:ed)?\s+(?:\w+\s+)?(?:within|per)\s+([A-Za-z_][A-Za-z0-9_]*)\b",
    re.IGNORECASE,
)
# Generic "share / percentage metric" signal. A metric whose result is a share or
# percentage makes the per-row denominator load-bearing: if the grain explodes,
# every cell becomes a tiny meaningless fraction. Derived from metric text alone
# (e.g. "share of orders", "percentage of revenue"); domain-agnostic.
_SHARE_METRIC_PATTERN = re.compile(
    r"\b(?:percent(?:age)?|share)\b", re.IGNORECASE,
)
# Default proposed bucket width for a raw continuous cut, in the cut's own unit
# (years for an age cut, days for a days-since cut). A width, not a domain value:
# the same 10-unit banding applies to "age" (10-year bands) or "days since
# signup" (10-day bands). The user can override via the grain-bucketing answer.
_DEFAULT_BUCKET_WIDTH = 10


@dataclass(frozen=True)
class WindowSpec:
    """Window OVER clause for an aggregation.

    partition_by: column expressions for PARTITION BY (empty → no partition)
    order_by:     column expressions for ORDER BY (empty → no order)
    frame:        optional frame clause, e.g. "ROWS BETWEEN 2 PRECEDING AND CURRENT ROW"
    """
    partition_by: tuple[str, ...] = ()
    order_by: tuple[str, ...] = ()
    frame: str = ""


@dataclass(frozen=True)
class Aggregation:
    fn: str
    column: str
    alias: str
    distinct: bool = False
    predicate: str | None = None
    window: WindowSpec | None = None
    # When False, the aggregation is used internally (e.g. it still drives
    # windowed-only/SELECT DISTINCT detection and is inlined into a composite
    # expression) but is NOT projected as its own output column. Used so a
    # percentage-share metric emits only cuts + the single share column rather
    # than leaking its numerator/denominator scaffolding.
    project: bool = True


@dataclass(frozen=True)
class Dimension:
    """A GROUP BY column.

    - `expression` is the SQL fragment used in GROUP BY / ORDER BY / PARTITION BY
      (e.g. `date_trunc('month', "order_date")` or just `"channel"`). It is also
      the SELECT projection unless `display_expression` overrides it.
    - `display_expression` (optional) is what appears in SELECT instead of
      `expression`. It must be functionally determined by `expression` (built from
      the same grouped sub-expressions) so GROUP BY stays valid. Used for banded
      continuous cuts: GROUP BY the numeric band lower bound (sorts correctly),
      but display a readable `20-29` range. `None` -> SELECT uses `expression`.
    - `alias` is the AS name in SELECT
    """
    expression: str
    alias: str
    display_expression: str | None = None


@dataclass(frozen=True)
class FilterClause:
    column: str
    op: str
    value: str
    is_literal: bool = True


@dataclass
class ParsedKPI:
    aggregations: list[Aggregation] = field(default_factory=list)
    dimensions: list[Dimension] = field(default_factory=list)
    filters: list[FilterClause] = field(default_factory=list)
    having: list[str] = field(default_factory=list)
    extra_select_exprs: list[tuple[str, str]] = field(default_factory=list)  # (expression, alias)
    limit: int | None = None
    ratio: tuple[Aggregation, Aggregation] | None = None
    fallback_reason: str = ""
    # Denominator scope for percentage-share KPIs. Values: "grand_total" (default,
    # OVER ()) or "per_<group>" (OVER PARTITION BY <group>). Recorded so the choice
    # is auditable and not silently implied. None = not a percentage-share KPI.
    denominator_scope: str | None = None
    # Alternative denominator scope not chosen; recorded for audit.
    denominator_scope_alternative: str | None = None
    # Age-fallback note: set when age/date arithmetic falls back to CURRENT_DATE
    # because no event-date grain column was found.
    age_as_of_assumption: str | None = None
    # Grain-bucketing block: set (with fallback_reason) when a share/percentage
    # metric is cut by a RAW continuous dimension (exact integer age / days-since)
    # and no bucketing decision is recorded. Grouping a share by an exact
    # continuous value fragments the denominator into one tiny row per value
    # (e.g. ~7k rows each ~0.2%). The block proposes banding the cut into ranges
    # instead of emitting the exploded GROUP BY. None = no bucketing block.
    grain_bucketing_block: dict[str, Any] | None = None
    # Single-attribution share: set for distinct-entity share metrics so each
    # entity is counted in exactly ONE grain cell (shares sum to ~100% instead
    # of >100% when entities appear in multiple cells). Keys: entity_column,
    # order_terms (ROW_NUMBER ordering), mode ("most_recent_event" or
    # "deterministic_cell_order"). None = no attribution CTE.
    share_attribution: dict[str, Any] | None = None

    @property
    def can_compose(self) -> bool:
        return not self.fallback_reason


def _norm_alias(text: str) -> str:
    cleaned = re.sub(r"[^A-Za-z0-9_]+", "_", text.strip().lower()).strip("_")
    return cleaned or "value"


def _dimension_alias(column: str, cut_label: str) -> str:
    """Output alias for a plain dimension column.

    Normally the physical column name (``Name`` -> ``name``). But when that name
    is a generic person-identifier word that DISPLAY redaction blanks
    (``^name$``, ``^address$``, ...) yet the cut clearly names a non-PII business
    dimension (``Department Name`` -> ``department_name``), alias by the cut
    label so the dimension stays readable on rendered tables. The SQL still reads
    the real column — only the OUTPUT alias changes. A cut that simply IS the
    identifier (``SSN`` -> ``ssn``, ``DOB`` -> ``dob``) has no more-descriptive
    label, so its alias is unchanged and it stays redacted. Genuinely sensitive
    columns are independently SHA-256 masked upstream; this only governs the
    label, never whether a value is protected. Domain-agnostic.
    """
    from core.onboarding.kpi.pii_redaction import is_pii_column

    col_alias = _norm_alias(column)
    label_alias = _norm_alias(cut_label)
    if is_pii_column(col_alias) and label_alias != col_alias and not is_pii_column(label_alias):
        return label_alias
    return col_alias


def _quote(value: str, dialect: str = "duckdb") -> str:
    if not value:
        return value
    if dialect == "databricks":
        return "`" + value.replace("`", "``") + "`"
    return '"' + value.replace('"', '""') + '"'


def _column_lookup(kpi: dict[str, Any]) -> dict[str, str]:
    """Map feature label → underlying column name via source_columns.

    If a feature has `source_columns`, the first column wins. Otherwise
    the feature label itself is the resolved column.
    """
    out: dict[str, str] = {}
    for feature in kpi.get("features") or []:
        if not isinstance(feature, dict):
            continue
        label = str(feature.get("feature") or feature.get("name") or "")
        if not label:
            continue
        # A derived-formula feature is MATERIALIZED in the features view under
        # its own name; its source_columns list the formula INPUTS (e.g.
        # START/STOP for over_24_hour). Resolving the label to an input column
        # made the result view group by the input instead of the derived
        # column. The label resolves to itself.
        if str(feature.get("resolution_type") or "") == "derived_formula":
            out[label.lower()] = label
            continue
        resolved = label
        for source in feature.get("source_columns") or []:
            if isinstance(source, dict):
                col = str(source.get("column") or "")
                if col:
                    resolved = col
                    break
        out[label.lower()] = resolved
    return out


def _features_view_column_lookup(kpi: dict[str, Any]) -> dict[str, str]:
    """Map feature label -> the column name the SQL FEATURES VIEW exposes it
    under, i.e. the feature label itself, always.

    `_column_lookup` above answers a different question -- the feature's
    PHYSICAL source column -- needed by Polars/PySpark (which read raw
    dataframes directly, no intermediate view) and by raw-table analysis
    inside this module. This function is for the one case that's genuinely
    different: SQL result-view generation (`parse_kpi`/build_result_view_sql)
    queries FROM the features view exclusively, never the raw catalog views,
    and that view's SELECT list aliases every column to its feature label
    unconditionally (sql_generator.py's `<expr> AS "<feature_label>"`, run
    for every resolution_type, not just derived_formula). Resolving to the
    physical column name there referenced a column the features view never
    exposes under that name, producing a binder error whenever a feature's
    label differs from its bound column (e.g. a feature named `cargo_claims`
    bound to physical column `Id`). Deliberately a separate function, not a
    change to `_column_lookup` itself: that function's physical-column
    answer is correct for its other callers and must not change to fix a
    problem specific to the SQL result-view context.
    """
    out: dict[str, str] = {}
    for feature in kpi.get("features") or []:
        if not isinstance(feature, dict):
            continue
        label = str(feature.get("feature") or feature.get("name") or "")
        if not label:
            continue
        out[label.lower()] = label
    return out


def _resolve_column(name: str, lookup: dict[str, str]) -> str:
    """Resolve a KPI cut/term to an underlying column.

    Beyond an exact feature-label match, handles the common case where a cut is
    phrased more verbosely than the feature label (e.g. cut `Department Name`
    vs feature `Department`). Resolution order:
      1. exact label (lowercased)
      2. normalized label (ignoring spaces/punctuation)
      3. word-subset: the feature label's words are all contained in the cut's
         words — the most specific (longest) such label wins.
    Falls back to the original name when nothing matches, so the verifier still
    blocks if it does not exist as a real column.
    """
    if not name:
        return name
    key = name.lower()
    if key in lookup:
        return lookup[key]
    norm = re.sub(r"[^a-z0-9]+", "", key)
    for label, column in lookup.items():
        if re.sub(r"[^a-z0-9]+", "", label) == norm:
            return column
    cut_words = set(re.findall(r"[a-z0-9]+", key))
    best: tuple[int, str] | None = None
    for label, column in lookup.items():
        label_words = set(re.findall(r"[a-z0-9]+", label))
        if label_words and label_words <= cut_words:
            if best is None or len(label_words) > best[0]:
                best = (len(label_words), column)
    return best[1] if best else name


def _norm(text: str) -> str:
    """Lowercase, strip everything but alphanumerics."""
    return re.sub(r"[^a-z0-9]+", "", str(text).lower())


def _emitted_columns(lookup: dict[str, str]) -> set[str]:
    """The set of physical columns the features view actually emits."""
    return {col for col in lookup.values() if col}


def _measure_input_columns(metric_text: str, lookup: dict[str, str]) -> set[str]:
    """Columns consumed as an aggregate's ARGUMENT in the metric, lowercased.

    Such a column is what is being MEASURED, never a grain to group by. Emitting
    it as a dimension collapses the result to one row per entity and, for a
    share, spreads the total across every individual -- which also turns an
    identifier column into exported row-level data. Observed 2026-07-26: a
    `count(distinct PatientID) ... for patients` metric fuzzy-resolved its own
    "for <group>" token back onto `PatientID`, giving one row per patient at
    0.023% each, and the identifiers reached an exported slide.

    Reuses `_AGG_FN_PATTERN` (the same parser the metric itself is read with) so
    a new aggregate function is recognised here for free. Non-column arguments
    (`*`, a `col = 'x'` predicate, a nested expression) are skipped.
    """
    out: set[str] = set()
    for match in _AGG_FN_PATTERN.finditer(str(metric_text or "")):
        inner = match.group(3).strip().strip('"`')
        if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_ ]*", inner):
            continue
        resolved = _resolve_column(inner, lookup)
        if resolved:
            out.add(resolved.lower())
    return out


def _drop_measure_inputs(
    dimensions: list["Dimension"], measure_inputs: set[str]
) -> list["Dimension"]:
    """Dimensions minus any that is a bare reference to a measure-input column.

    Only bare column references are dropped: a derived expression over the same
    column (``date_trunc('month', "OrderDate")`` while the metric is
    ``min(OrderDate)``) is a legitimate grain and does not match.
    """
    return [
        d for d in dimensions
        if d.expression.strip('"`').lower() not in measure_inputs
    ]


def _dataset_token_index(kpi: dict[str, Any]) -> list[tuple[str, str]]:
    """Map each feature's source-dataset stem to the column it resolves to.

    A group token (e.g. ``departement``) often names the *dimension table* the
    column lives in (``departments.csv`` → ``Name``) rather than the column
    itself. This index lets us recover the emitted column from the dataset name
    when no feature label matches the token. Workspace-agnostic: it reads only
    the dataset path stem already present in ``source_columns``.
    """
    index: list[tuple[str, str]] = []
    for feature in kpi.get("features") or []:
        if not isinstance(feature, dict):
            continue
        for source in feature.get("source_columns") or []:
            if not isinstance(source, dict):
                continue
            column = str(source.get("column") or "")
            dataset = str(source.get("dataset") or "")
            if not column or not dataset:
                continue
            stem = re.split(r"[\\/]", dataset)[-1]
            stem = re.sub(r"\.[A-Za-z0-9]+$", "", stem)  # drop extension
            if stem:
                index.append((stem, column))
    return index


def _fuzzy_ratio(a: str, b: str) -> float:
    if not a or not b:
        return 0.0
    return SequenceMatcher(None, a, b).ratio()


def _resolve_group_column(
    token: str,
    lookup: dict[str, str],
    kpi: dict[str, Any],
    fallback_columns: tuple[str, ...] = (),
) -> str:
    """Resolve a "for <group>" partition token to a real emitted column.

    Unlike a plain cut, a phantom partition column makes the generated SQL
    non-executable (BUG-011), so this never returns an unresolved token.
    Resolution order:
      1. ``_resolve_column`` (exact / normalized / word-subset on feature labels)
         — accepted only if it lands on a column the features view emits.
      2. Fuzzy match of the token against feature labels and against source
         dataset stems (e.g. ``departement`` ~ ``departments`` → ``Name``);
         the closest emitted column above a similarity threshold wins. This
         absorbs misspellings/aliases of a dimension whose redundant feature was
         dropped (BUG-001).
      3. Fall back to the closest already-built cut dimension column.
      4. Empty string — caller must then drop the per-group denominator rather
         than emit a PARTITION BY on a column that does not exist.
    """
    emitted = _emitted_columns(lookup)

    direct = _resolve_column(token, lookup)
    if direct in emitted:
        return direct

    token_norm = _norm(token)
    if not token_norm:
        return ""

    candidates: list[tuple[float, str]] = []
    # Feature labels → emitted column.
    for label, column in lookup.items():
        if column not in emitted:
            continue
        score = _fuzzy_ratio(token_norm, _norm(label))
        candidates.append((score, column))
    # Source dataset stems → emitted column (handles dimension-table aliases).
    for stem, column in _dataset_token_index(kpi):
        if column not in emitted:
            continue
        stem_norm = _norm(stem)
        score = _fuzzy_ratio(token_norm, stem_norm)
        # A containment relationship (departement in departments or vice-versa)
        # is a strong signal even when the edit ratio is middling.
        if token_norm in stem_norm or stem_norm in token_norm:
            score = max(score, 0.9)
        candidates.append((score, column))

    if candidates:
        score, column = max(candidates, key=lambda c: c[0])
        if score >= 0.6:
            return column

    # Last resort: reuse the closest existing cut dimension's underlying column
    # so the denominator still partitions by a real emitted column rather than a
    # phantom token.
    best_dim: tuple[float, str] | None = None
    for bare in fallback_columns:
        if bare not in emitted:
            continue
        score = _fuzzy_ratio(token_norm, _norm(bare))
        if best_dim is None or score > best_dim[0]:
            best_dim = (score, bare)
    if best_dim and best_dim[0] >= 0.6:
        return best_dim[1]

    return ""


def _denom_is_within_scope(denominator_scope: str | None) -> bool:
    """True when an explicit within-group denominator scope was chosen."""
    return denominator_scope is not None and denominator_scope not in {
        "grand_total",
        "global_total",
    }


def _detect_time_bucket(token: str) -> tuple[str, str, str] | None:
    """Return (bucket_unit, source_column, alias) when token is a time bucket hint.

    Examples:
      "Month"                  → ("month", "", "month")
      "Month (ServiceDate)"    → ("month", "ServiceDate", "month")
      "Year(DOB)"              → ("year", "DOB", "year")
    Returns None when no time bucket hint is present.
    """
    match = _TIME_BUCKET_HINT.search(token)
    if not match:
        return None
    bucket = match.group(1).lower()
    source = (match.group(2) or "").strip()
    return bucket, source, bucket


# A KPI question often asks for TWO measures ("...the top 10 procedures AND the
# average base cost for each", "...AND the number of times they were performed"),
# but the registry's single `metric` field keeps only one. These patterns recover
# the dropped second measure from the question prose. Generic; no domain words.
_SECONDARY_AVG = re.compile(
    r"\b(?:and|,|with)\s+(?:the\s+)?(?:average|avg|mean)\s+([a-z][a-z0-9_ ]+?)"
    r"(?:\s+(?:for|per|of|each|by|and)\b|[.?]|$)", re.IGNORECASE)
_SECONDARY_SUM = re.compile(
    r"\b(?:and|,|with)\s+(?:the\s+)?total\s+([a-z][a-z0-9_ ]+?)"
    r"(?:\s+(?:for|per|of|each|by|and)\b|[.?]|$)", re.IGNORECASE)
_SECONDARY_COUNT = re.compile(
    r"\b(?:and|,|with)\s+(?:the\s+)?(?:number of times|number of them|"
    r"how many times|count of how many|times they were)\b", re.IGNORECASE)


def _detect_secondary_measure(
    name_text: str, lookup: dict[str, str], primary_fn: str
) -> Aggregation | None:
    """A second measure asked for in the question prose but missing from the
    single-valued registry `metric`. Returns one extra Aggregation or None.
    Never duplicates the primary aggregation's function."""
    real = {_norm(c) for c in _emitted_columns(lookup)}
    # The features view emits `<column> AS <feature-label>` (e.g. BASE_COST AS
    # "cost"), so the result view must reference the EMITTED name (the label),
    # not the raw column. Build column -> emitted-label so the secondary measure
    # references what the staging view actually projects.
    col_to_label = {col: label for label, col in lookup.items() if col}

    def resolved(token: str) -> str | None:
        col = _resolve_column(token.strip(), lookup)
        # Only accept a column that actually exists in the KPI's resolved feature
        # set -- otherwise the second measure would reference a phantom column
        # (e.g. "base cost") and emit invalid SQL.
        if not col or _norm(col) not in real:
            return None
        return col_to_label.get(col, col)

    if _SECONDARY_COUNT.search(name_text) and primary_fn != "count":
        return Aggregation(fn="count", column="*", alias="row_count")
    m = _SECONDARY_AVG.search(name_text)
    if m and primary_fn != "avg":
        col = resolved(m.group(1))
        if col:
            return Aggregation(fn="avg", column=col, alias=_norm_alias(f"avg_{col}"))
    m = _SECONDARY_SUM.search(name_text)
    if m and primary_fn != "sum":
        col = resolved(m.group(1))
        if col:
            return Aggregation(fn="sum", column=col, alias=_norm_alias(f"sum_{col}"))
    return None


def _parse_aggregation(
    text: str, lookup: dict[str, str], dialect: str = "duckdb"
) -> Aggregation | None:
    text = text.strip()
    if not text:
        return None
    if re.match(r"\bcount\s*\(\s*\*\s*\)", text, re.IGNORECASE):
        return Aggregation(fn="count", column="*", alias="row_count")
    match = _AGG_FN_PATTERN.search(text)
    if not match:
        return None
    fn = match.group(1).lower()
    fn = _AGG_FN_ALIASES.get(fn, fn)
    distinct = bool(match.group(2))
    inner_raw = match.group(3).strip()
    predicate_match = _PREDICATE_IN_COUNT.match(inner_raw)
    if predicate_match:
        col = _resolve_column(predicate_match.group(1), lookup)
        literal = predicate_match.group(2)
        return Aggregation(
            fn="count",
            column=col,
            alias=_norm_alias(f"{col}_{literal}_count"),
            # Baked in at parse time (dialect-aware) -- _agg_expr_no_alias
            # inlines agg.predicate verbatim, so an un-dialected quote here
            # would silently compare a STRING LITERAL to a string literal in
            # Spark SQL (double-quoted = string literal, not identifier),
            # producing a constant (always-true/false) predicate.
            predicate=f"{_quote(col, dialect)} = '{literal}'",
        )
    column = _resolve_column(inner_raw, lookup)
    alias_base = f"{('distinct_' if distinct else '')}{column}" if column else fn
    return Aggregation(
        fn=fn,
        column=column,
        alias=_norm_alias(f"{fn}_{alias_base}"),
        distinct=distinct,
    )


def _split_cuts(cuts_text: str) -> list[str]:
    if not cuts_text:
        return []
    parts = re.split(r"[,;]| and ", cuts_text, flags=re.IGNORECASE)
    return [p.strip() for p in parts if p.strip()]


def _parse_filter(token: str, lookup: dict[str, str]) -> FilterClause | None:
    match = _COMPARISON_FILTER.search(token)
    if not match:
        return None
    column = _resolve_column(match.group(1), lookup)
    op = match.group(2)
    literal = match.group(3)
    numeric = match.group(4)
    if literal is not None:
        return FilterClause(column=column, op=op, value=f"'{literal}'", is_literal=True)
    if numeric is not None:
        return FilterClause(column=column, op=op, value=numeric, is_literal=False)
    return None


def _detect_window_intent(metric_text: str, name_text: str) -> dict[str, Any]:
    """Detect window-function intent from KPI text. Returns a dict describing
    what window pattern was matched, empty dict if none.

    Order matters: most specific patterns first, since "share of X" would
    otherwise match "percentage of sum" in a mismatched-grain ratio.
    """
    haystack = (metric_text + " " + name_text).lower()
    # 1. Mismatched-grain percentage MUST be checked before share-of-group
    #    because it contains "percentage of <agg>" that would otherwise match.
    if "percentage" in metric_text.lower() and "/" in metric_text and " for " in metric_text.lower():
        for_match = re.search(r"\bfor\s+([A-Za-z_][A-Za-z0-9_]*)\b", metric_text, re.IGNORECASE)
        partition = for_match.group(1) if for_match else ""
        return {"kind": "mismatched_grain_percentage", "partition": partition}
    if _PCT_OF_TOTAL_PATTERN.search(haystack):
        return {"kind": "percent_of_total"}
    rank = _RANK_WITHIN_PATTERN.search(haystack)
    if rank:
        return {"kind": "rank_within", "partition": rank.group(1)}
    if _RUNNING_TOTAL_PATTERN.search(haystack):
        return {"kind": "running_total"}
    mavg = _MOVING_AVG_PATTERN.search(haystack)
    if mavg:
        return {"kind": "moving_average", "window_size": int(mavg.group(1))}
    # Share-of-group LAST among these — least specific match.
    share_match = _SHARE_OF_GROUP_PATTERN.search(haystack)
    if share_match:
        candidate = share_match.group(1)
        # Reject if the match is followed by "(" — that means it's an aggregation, not a group column.
        post = haystack[share_match.end():share_match.end() + 1]
        if post not in {"(", " "} or candidate in {"sum", "count", "avg", "min", "max", "distinct"}:
            return {}
        return {"kind": "percent_of_group", "group": candidate}
    return {}


def raw_date_input_columns(kpi: dict[str, Any]) -> set[str]:
    """Source columns the result view consumes as RAW dates, lowercased.

    These columns feed date arithmetic — age / days-since cuts
    (``date_diff(..., CAST(col AS DATE), ...)``) and the event-date anchor of a
    time-bucket cut (``Month(ServiceDate)``). In every case the RAW value is a
    transformation INPUT and is never projected to the result output (only the
    derived band / age / bucket is). The features view must therefore carry
    these columns UNMASKED: a SHA-256 mask would make the downstream
    ``CAST(<hash> AS DATE)`` fail, and masking serves no privacy purpose because
    the raw value is never emitted. Domain-agnostic — derived purely from the
    KPI's own cuts text, no column or domain vocabulary is hard-coded.

    The SQL generator consults this so a sensitive date column (e.g. ``DOB``,
    a HIPAA identifier) is still masked wherever it WOULD be output, but is left
    raw when it is consumed only to derive a non-identifying band such as an age
    bucket (which HIPAA Safe Harbor explicitly permits).
    """
    cuts_text = str(kpi.get("cuts") or "").strip()
    if not cuts_text:
        return set()
    lookup = _column_lookup(kpi)
    cols: set[str] = set()
    for match in _AGE_PATTERN.finditer(cuts_text):
        source = match.group(1) or match.group(2)
        if source:
            cols.add(_resolve_column(source, lookup))
    for match in _DAYS_SINCE_PATTERN.finditer(cuts_text):
        source = match.group(1)
        if source:
            cols.add(_resolve_column(source, lookup))
    event = _detect_event_date_column(cuts_text, lookup)
    if event:
        cols.add(event)
    return {c.lower() for c in cols if c}


def _detect_event_date_column(cuts_text: str, lookup: dict[str, str]) -> str:
    """Discover the KPI's event/service date column, generically.

    The event date is the column the KPI uses as its time grain — i.e. the
    source column of a time-bucket cut such as ``Month (ServiceDate)`` /
    ``Year(order_date)``. Returns the resolved physical column name, or an
    empty string if the KPI has no explicit time-grain source.

    This is the reference date for as-of-event age arithmetic (BUG-005): a
    row's age must be computed as of when the event happened, not as of today.
    Detection is purely structural (the time-bucket's source column) so it
    carries no domain vocabulary.
    """
    for token in _split_cuts(cuts_text):
        bucket = _detect_time_bucket(token)
        if not bucket:
            continue
        _unit, source, _alias = bucket
        if source:
            return _resolve_column(source, lookup)
    return ""


def _band_expr(base: str, band_width: int) -> str:
    """Band a continuous integer expression into fixed-width ranges.

    Returns the band's lower bound: ``CAST(FLOOR(base / width) AS BIGINT) *
    width``. Grouping by the lower bound keeps the share denominator meaningful
    (one row per band, not per exact value) and sorts numerically; the BIGINT cast
    drops the float ``.0`` so labels read ``30-39`` not ``30.0-39.0``.
    Domain-agnostic — the same form bands years (age) or days (days-since), and
    ``CAST AS BIGINT`` is valid in both DuckDB and Spark/Databricks.
    """
    return f"(CAST(FLOOR(({base}) / {band_width}) AS BIGINT) * {band_width})"


def _band_label_expr(base: str, band_width: int) -> str:
    """Readable ``lo-hi`` range label for a banded continuous value.

    Built from the same lower-bound expression as :func:`_band_expr`, so it is
    functionally determined by the GROUP BY key (display-only; the numeric lower
    bound still drives GROUP BY / ORDER BY for correct numeric sort). ``CONCAT``
    coerces the numeric bounds to text in both DuckDB and Spark/Databricks, so no
    dialect-specific string cast is needed.
    """
    lo = _band_expr(base, band_width)
    hi = f"({lo} + {band_width} - 1)"
    return f"CONCAT({lo}, '-', {hi})"


def _detect_date_arithmetic(
    cuts_text: str,
    lookup: dict[str, str],
    as_of_expr: str = "CURRENT_DATE",
    band_width: int | None = None,
    dialect: str = "duckdb",
) -> list[tuple[str, str, str | None]]:
    """Detect age/date-arithmetic expressions in cuts text. Returns
    [(group_expression, alias, display_expression), ...] to add to SELECT.

    ``group_expression`` is used for GROUP BY / ORDER BY / PARTITION BY;
    ``display_expression`` (or ``None``) is the SELECT projection override.

    ``as_of_expr`` is the reference point the arithmetic is measured against.
    For a historical/trend KPI the caller passes the event-date expression
    (e.g. ``CAST("ServiceDate" AS DATE)``) so age is computed as-of the event,
    not as-of today (BUG-005). Defaults to ``CURRENT_DATE`` when the KPI has no
    event date available, preserving the original behavior.

    ``band_width`` (when set) bands the raw continuous value into fixed-width
    ranges instead of emitting the exact integer grain. This is how a recorded
    ``band_continuous_cuts`` grain decision actually changes the SQL: a share
    metric cut by age then groups by 10-year bands (numeric lower bound), shown as
    a readable ``20-29`` label, not one row per exact age. ``None`` keeps the
    exact-value grain (the pre-existing behavior) with no display override.
    """
    out: list[tuple[str, str, str | None]] = []
    for match in _AGE_PATTERN.finditer(cuts_text):
        source = match.group(1) or match.group(2)
        if not source:
            continue
        col = _resolve_column(source, lookup)
        unit_yr = "year" if dialect == "databricks" else "'year'"
        base = f"date_diff({unit_yr}, CAST({_quote(col, dialect)} AS DATE), {as_of_expr})"
        if band_width:
            out.append((_band_expr(base, band_width), "age_band",
                        _band_label_expr(base, band_width)))
        else:
            out.append((base, "age", None))
    for match in _DAYS_SINCE_PATTERN.finditer(cuts_text):
        source = match.group(1)
        col = _resolve_column(source, lookup)
        unit_dy = "day" if dialect == "databricks" else "'day'"
        base = f"date_diff({unit_dy}, CAST({_quote(col, dialect)} AS DATE), {as_of_expr})"
        alias = f"days_since_{_norm_alias(col)}"
        if band_width:
            out.append((_band_expr(base, band_width), f"{alias}_band",
                        _band_label_expr(base, band_width)))
        else:
            out.append((base, alias, None))
    return out


def _is_share_metric(metric_text: str, window_intent: dict[str, Any]) -> bool:
    """True when the metric's result is a share/percentage.

    A share/percentage metric makes the per-cell denominator load-bearing, so an
    exploded grain renders every cell a meaningless fraction. Detection is
    generic: either the metric text carries a share/percent word, or the parsed
    window-intent kind is one of the share-producing kinds. No domain words.
    """
    if _SHARE_METRIC_PATTERN.search(metric_text or ""):
        return True
    return window_intent.get("kind") in {
        "mismatched_grain_percentage",
        "percent_of_total",
        "percent_of_group",
    }


def _detect_raw_continuous_cuts(
    cuts_text: str, lookup: dict[str, str]
) -> list[dict[str, Any]]:
    """Find cut tokens that GROUP BY a raw exact continuous value.

    Reuses the existing generic date-arithmetic regexes (age / days-since): each
    yields an exact integer grain (a person's exact age, the exact day count
    since an event), so grouping by it produces one row per distinct value. A
    comparison token (``age > 50``) is a FILTER, not a grouping dimension, and is
    skipped. Returns one descriptor per raw continuous cut with the resolved
    source column, the cut unit, and a proposed band width — domain-agnostic.
    """
    out: list[dict[str, Any]] = []
    for token in _split_cuts(cuts_text):
        if any(op in token for op in ("=", "<", ">")):
            continue  # a threshold filter, not a grouping dimension
        age_match = _AGE_PATTERN.search(token)
        days_match = _DAYS_SINCE_PATTERN.search(token)
        if age_match:
            source = age_match.group(1) or age_match.group(2) or ""
            unit = "year"
        elif days_match:
            source = days_match.group(1) or ""
            unit = "day"
        else:
            continue
        col = _resolve_column(source, lookup) if source else source
        out.append({
            "cut": token.strip(),
            "source_column": col,
            "unit": unit,
            "proposed_band_width": _DEFAULT_BUCKET_WIDTH,
        })
    return out


def _build_grain_bucketing_block(
    raw_cuts: list[dict[str, Any]], metric_text: str
) -> dict[str, Any]:
    """Build the structured hard-block payload proposing age/range bands.

    Mirrors the denominator_scope facet shape: a derived recommendation plus the
    evidence the reviewer needs. The proposal is to band each raw continuous cut
    into fixed-width ranges (default width) so the share denominator stays
    meaningful, instead of emitting a per-exact-value GROUP BY.
    """
    proposals = [
        {
            "cut": rc["cut"],
            "source_column": rc["source_column"],
            "unit": rc["unit"],
            "proposed_bucket": (
                f"band {rc['source_column'] or rc['cut']} into "
                f"{rc['proposed_band_width']}-{rc['unit']} ranges"
            ),
            "proposed_band_width": rc["proposed_band_width"],
        }
        for rc in raw_cuts
    ]
    cut_labels = ", ".join(rc["cut"] for rc in raw_cuts)
    return {
        "reason": (
            f"share/percentage metric `{metric_text}` is cut by a raw continuous "
            f"dimension ({cut_labels}); grouping a share by exact values "
            f"fragments the denominator into one tiny row per value. Choose a "
            f"band width (or confirm exact-value grain) before generating."
        ),
        "metric": metric_text,
        "raw_continuous_cuts": [rc["cut"] for rc in raw_cuts],
        "proposals": proposals,
        "recommended": "band_continuous_cuts",
        "alternatives": ["exact_value_grain"],
    }


def _band_width_from_decision(grain_bucketing: str | None) -> int | None:
    """Resolve the band width (in the cut's own unit) from a grain decision.

    This is what turns a recorded grain-bucketing answer into actual banded SQL:

    - ``None`` / ``"exact_value_grain"`` -> ``None`` (keep the exact-value grain;
      the reviewer explicitly accepted one row per exact value).
    - ``"band_continuous_cuts"`` -> the default width (``_DEFAULT_BUCKET_WIDTH``).
    - An explicit width may be encoded as ``"band_continuous_cuts:15"`` or a bare
      integer string to override the default.

    Width-agnostic across units (10 years for an age cut, 10 days for a
    days-since cut) so it stays domain-agnostic.
    """
    if not grain_bucketing:
        return None
    text = str(grain_bucketing).strip().lower()
    if text in {"exact_value_grain", "exact", "exact_value"}:
        return None
    match = re.search(r"\d+", text)
    if match:
        width = int(match.group())
        if width > 0:
            return width
    return _DEFAULT_BUCKET_WIDTH


def _detect_having(text: str, aggregations: list[Aggregation]) -> list[str]:
    """Detect HAVING clauses from KPI text. Returns a list of SQL fragments
    that go AFTER the HAVING keyword.
    """
    out: list[str] = []
    for match in _HAVING_PATTERN.finditer(text):
        threshold = int(match.group(1))
        # Use the first aggregation's alias as the LHS of the HAVING comparison
        if aggregations:
            out.append(f"{aggregations[0].alias} > {threshold}")
    return out


def parse_kpi(
    kpi: dict[str, Any],
    denominator_scope: str | None = None,
    grain_bucketing: str | None = None,
    dialect: str = "duckdb",
) -> ParsedKPI:
    """Parse a KPI, then drop any dimension that is the metric's own input.

    The branch bodies live in `_parse_kpi_branches`; every one of them can
    append a Dimension, and each has its own `return`. Normalising once at this
    single exit is what keeps a future branch from reintroducing the
    measure-input-as-dimension defect (see `_measure_input_columns`). The
    mismatched-grain branch additionally filters its own `grain_dimensions`
    before deriving window PARTITION BY terms from them -- a projection-only
    filter here would leave that window at the finer grain.
    """
    parsed = _parse_kpi_branches(kpi, denominator_scope, grain_bucketing, dialect)
    measure_inputs = _measure_input_columns(
        str(kpi.get("metric") or ""), _features_view_column_lookup(kpi)
    )
    if measure_inputs:
        parsed.dimensions = _drop_measure_inputs(parsed.dimensions, measure_inputs)
    return parsed


def _parse_kpi_branches(
    kpi: dict[str, Any],
    denominator_scope: str | None = None,
    grain_bucketing: str | None = None,
    dialect: str = "duckdb",
) -> ParsedKPI:
    """Parse a KPI registry entry into structured aggregations/dimensions/filters.

    Parameters
    ----------
    denominator_scope:
        Optional denominator-scope decision for percentage-share KPIs.  When the
        value is a within-group scope (``"within_<group>"`` or any value that is
        not ``None`` / ``"grand_total"`` / ``"global_total"``), the denominator
        window in the mismatched-grain percentage branch is emitted as
        ``OVER (PARTITION BY <partition_col>)`` instead of the default
        grand-total ``OVER ()``.  The resolved partition column is the same one
        already computed for the numerator's group.  See design/kpi_intent_contract.md
        §3 (denominator_scope facet).
    grain_bucketing:
        Optional bucketing decision for a share/percentage metric that is cut by
        a RAW continuous dimension (exact integer age / days-since). When such a
        cut is present and NO decision is recorded (``grain_bucketing`` is
        ``None``), ``parse_kpi`` sets a hard block (``fallback_reason`` +
        ``grain_bucketing_block``) proposing fixed-width bands instead of
        emitting the exploded GROUP BY. Any non-None value (e.g.
        ``"band_continuous_cuts"`` or ``"exact_value_grain"``) records that the
        grain was confirmed, so the generator proceeds. Mirrors the
        denominator_scope facet pattern.
    dialect:
        Identifier-quoting dialect for every column reference baked into a
        Dimension/Aggregation/filter expression here (age/date-arithmetic
        grain, GROUP BY dimensions, COUNT(...) predicates, window PARTITION
        BY columns). Defaults to "duckdb" (double-quoted identifiers,
        unchanged pre-existing behavior). "databricks" uses backtick
        identifiers -- required because Spark SQL treats a double-quoted
        string as a STRING LITERAL, not an identifier, by default: an
        un-dialected quote here would silently bake a constant/always-equal
        predicate or GROUP BY a literal string instead of the real column.
    """
    parsed = ParsedKPI()
    metric_text = str(kpi.get("metric") or "").strip()
    cuts_text = str(kpi.get("cuts") or "").strip()
    name_text = str(kpi.get("name") or kpi.get("business_question") or "").strip()
    lookup = _features_view_column_lookup(kpi)
    window_intent = _detect_window_intent(metric_text, name_text)

    # Grain-bucketing hard block: a share/percentage metric cut by a RAW
    # continuous dimension (exact integer age / days-since) fragments the
    # denominator into one tiny row per value (e.g. ~7k rows each ~0.2%). Block
    # generation and PROPOSE bands until a bucketing decision is recorded. A
    # recorded decision (any non-None grain_bucketing) confirms the grain and
    # lets generation proceed unchanged. Comparison tokens (age > 50) are
    # filters, not grouping dimensions, and never trigger this block.
    if grain_bucketing is None:
        raw_continuous_cuts = _detect_raw_continuous_cuts(cuts_text, lookup)
        if raw_continuous_cuts and _is_share_metric(metric_text, window_intent):
            block = _build_grain_bucketing_block(raw_continuous_cuts, metric_text)
            parsed.grain_bucketing_block = block
            parsed.fallback_reason = block["reason"]
            return parsed

    # A recorded band decision turns the raw continuous cut into fixed-width
    # bands when the date-arithmetic dimensions are emitted below; exact_value_grain
    # (and None) leave band_width None so the exact grain is preserved.
    band_width = _band_width_from_decision(grain_bucketing)

    # BUG-005: age (and other date arithmetic) must be measured as-of the
    # event/service date when the KPI has one, not as-of today. The event date
    # is the KPI's time-grain source column (e.g. Month(ServiceDate)); when none
    # exists we fall back to CURRENT_DATE.
    event_date_col = _detect_event_date_column(cuts_text, lookup)
    if event_date_col:
        as_of_expr = f"CAST({_quote(event_date_col, dialect)} AS DATE)"
    else:
        # No event-date anchor exists in the KPI grain. Semantically "as of now" is
        # the right reading, but emitting the literal CURRENT_DATE defers the anchor
        # to EXECUTION time, so the view stops being a function of the data alone:
        # re-running the same SQL later silently reshapes its own age bands and a
        # historical number cannot be reproduced. Pin the anchor to a literal date
        # captured at GENERATION time instead -- same semantics for this run,
        # reproducible forever after. Regeneration is an explicit act that records a
        # new date.
        as_of_date = _as_of_date()
        as_of_expr = f"DATE '{as_of_date}'"
        parsed.age_as_of_assumption = (
            f"date arithmetic anchored to a pinned as-of date {as_of_date} (no "
            "event-date column in the KPI grain); regenerate to advance it"
        )

    # Mismatched-grain percentage is handled via window functions.
    # The metric reads "<agg> / <same agg> for <group>": a share-of-total by
    # <group>. The numerator is the aggregate within each group and the
    # denominator is the grand total of the same aggregate, so each row is one
    # group and its percentage of the whole (e.g. a department's distinct lives
    # as a fraction of all distinct lives). NOTE: shares sum to ~100% across
    # groups ONLY when each entity maps to exactly one group. When the same
    # entity appears in multiple grain cells (e.g. one patient with visits in
    # several departments), per-cell DISTINCT counts overlap and the shares
    # total >100%. The post-execution share-sum check in
    # flow._write_result_preview measures and surfaces this in the packet.
    if window_intent.get("kind") == "mismatched_grain_percentage":
        partition = window_intent.get("partition", "")
        inner = _AGG_FN_PATTERN.search(metric_text)
        if inner:
            fn = inner.group(1).lower()
            distinct = bool(inner.group(2))
            column = _resolve_column(inner.group(3).strip(), lookup)

            # BUG-024: the descriptive cuts DO subdivide the share. The KPI asks
            # for "percentage share ... by <cuts>", so every declared cut is part
            # of the result grain. Previously only the "for <group>" column was
            # emitted and the descriptive cuts (gender/age/visit type) were
            # silently dropped, producing a group-only result that ignored the
            # stated cuts and forced a manual SQL edit. Numerator now counts
            # within each full-grain cell; the denominator stays the grand total
            # (OVER ()), so each row is one cell and its percentage of the whole
            # population. Shares sum to ~100% across cells only when each entity
            # belongs to one cell; overlapping membership makes the total exceed
            # 100% (measured and flagged post-execution). Denominator-scope
            # semantics (per-group vs grand-total) are intentionally left as-is.
            emitted = _emitted_columns(lookup)
            grain_dimensions: list[Dimension] = []
            grain_seen: set[str] = set()

            def _add_grain(
                expression: str, alias: str, display: str | None = None
            ) -> None:
                if expression in grain_seen:
                    return
                grain_seen.add(expression)
                grain_dimensions.append(
                    Dimension(
                        expression=expression, alias=alias,
                        display_expression=display,
                    )
                )

            cut_columns: list[str] = []
            for token in _split_cuts(cuts_text):
                bucket = _detect_time_bucket(token)
                if bucket:
                    unit, source, alias = bucket
                    source_col = _resolve_column(source or alias, lookup) or alias
                    expr = f"date_trunc('{unit}', CAST({_quote(source_col, dialect)} AS DATE))"
                    _add_grain(expr, _norm_alias(alias))
                    continue
                if _AGE_PATTERN.search(token) or _DAYS_SINCE_PATTERN.search(token):
                    for expr, alias, display in _detect_date_arithmetic(
                        token, lookup, as_of_expr, band_width, dialect=dialect
                    ):
                        _add_grain(expr, alias, display)
                    continue
                clean = re.sub(r"\(.*?\)", "", token).strip()
                if not clean:
                    continue
                col = _resolve_column(clean, lookup)
                if col in emitted:
                    cut_columns.append(col)
                    _add_grain(_quote(col, dialect), _dimension_alias(col, clean))

            # Resolve the "for <group>" token to a REAL emitted column (graceful
            # fallback to the cut columns) and make sure it is part of the grain.
            # A raw/aliased token must never reach PARTITION BY or the view is
            # non-executable (BUG-011).
            # A bare TIME-GRAIN word as the group ("for year") is not a column:
            # it names the time-bucket dimension already in the grain, and the
            # share is within-period (each year's groups sum to ~100%). Letting
            # it fall through to column resolution bound it to an unrelated
            # emitted column (the metric's own id input), polluting the grain
            # with one row per entity.
            partition_time_expr = ""
            partition_low = str(partition or "").strip().lower()
            if partition_low in {"year", "quarter", "month", "week", "day"}:
                partition_time_expr = next(
                    (d.expression for d in grain_dimensions if d.alias == partition_low),
                    "",
                )
            partition_col = ""
            if partition and not partition_time_expr:
                partition_col = _resolve_group_column(
                    partition, lookup, kpi, tuple(cut_columns)
                )
            if partition_col and partition_col in emitted:
                _add_grain(_quote(partition_col, dialect), _norm_alias(partition_col))

            # An empty grain is fine: the numerator then partitions by nothing
            # (OVER ()), matching the denominator — degenerate but executable. We
            # never emit a PARTITION BY on an UNRESOLVED token (BUG-011); that is
            # already prevented above by only adding emitted columns / a resolved
            # group column.
            # The metric's own aggregate argument is not a grain. Filtered HERE,
            # not only at parse_kpi's exit, because grain_terms below become the
            # numerator's window PARTITION BY and the attribution ORDER BY: a
            # projection-only filter would leave those at the finer grain and the
            # share would still be computed per entity.
            grain_dimensions = _drop_measure_inputs(
                grain_dimensions,
                _measure_input_columns(metric_text, lookup),
            )
            parsed.dimensions.extend(grain_dimensions)
            grain_terms = tuple(d.expression for d in grain_dimensions)

            # Single attribution for DISTINCT-entity shares: an entity that
            # appears in multiple grain cells (one patient in several
            # departments, one customer in several channels...) used to be
            # counted in EVERY cell, so the shares totalled >100% (measured
            # 229% on real data). Each entity is now attributed to exactly one
            # cell — its most recent event when the grain has an event date,
            # else a deterministic cell order — and the result is a plain
            # GROUP BY whose shares sum to ~100% by construction. Row-based
            # shares (sum / non-distinct count) already partition rows across
            # cells and are left on the window path.
            if distinct and column and column != "*" and grain_terms:
                order_terms: list[str] = []
                if event_date_col:
                    order_terms.append(f"CAST({_quote(event_date_col, dialect)} AS DATE) DESC")
                    attribution_mode = "most_recent_event"
                else:
                    attribution_mode = "deterministic_cell_order"
                # Grain expressions as (tie)breakers make the pick fully
                # deterministic across engines and runs.
                order_terms.extend(grain_terms)
                parsed.share_attribution = {
                    "entity_column": column,
                    "order_terms": order_terms,
                    "mode": attribution_mode,
                }
                if _denom_is_within_scope(denominator_scope):
                    _within_col = _resolve_group_column(
                        partition, lookup, kpi, tuple(cut_columns)
                    ) if partition else ""
                    if _within_col and _within_col in emitted:
                        denom_expr = (
                            f"SUM(COUNT(*)) OVER (PARTITION BY {_quote(_within_col, dialect)})"
                        )
                        parsed.denominator_scope = denominator_scope
                        parsed.denominator_scope_alternative = "grand_total"
                    else:
                        denom_expr = "SUM(COUNT(*)) OVER ()"
                        parsed.denominator_scope = "grand_total"
                else:
                    denom_expr = "SUM(COUNT(*)) OVER ()"
                    parsed.denominator_scope = denominator_scope or "grand_total"
                    parsed.denominator_scope_alternative = None
                # Plain aggregation (project=False) keeps the composer in
                # GROUP BY mode; the share is the only emitted measure.
                parsed.aggregations.append(
                    Aggregation(
                        fn="count", column="*",
                        alias=_norm_alias(f"attributed_{column}_count"),
                        project=False,
                    )
                )
                parsed.extra_select_exprs.append(
                    (
                        f"CAST(COUNT(*) AS DOUBLE) / NULLIF({denom_expr}, 0) * 100",
                        "percentage_share",
                    )
                )
                return parsed

            # Numerator = lives within each full-grain cell (PARTITION BY grain).
            # project=False: it is inlined into percentage_share, NOT emitted as
            # its own output column (the metric is a single share column; the
            # numerator/denominator are internal scaffolding the cuts never named).
            group_agg = Aggregation(
                fn=fn, column=column,
                alias=_norm_alias(f"{fn}_{column}_per_group"),
                distinct=distinct,
                window=WindowSpec(partition_by=grain_terms),
                project=False,
            )
            # Denominator scope: grand_total (default, OVER ()) or within-group
            # (OVER (PARTITION BY <partition_col>)).  The denominator_scope facet
            # is recorded as a SQL comment so the choice is auditable and not
            # silently implied.  DEFAULT is grand_total — no change to existing
            # behaviour unless an explicit within-group scope is passed.
            _denom_is_within = (
                denominator_scope is not None
                and denominator_scope not in {"grand_total", "global_total"}
            )
            if partition_time_expr:
                # "for <time-grain>" in the metric is an explicit within-period
                # share: each period's groups sum to ~100%.
                _denom_window = WindowSpec(partition_by=(partition_time_expr,))
                _scope_label = f"within_{partition_low}"
            elif _denom_is_within and partition_col and partition_col in emitted:
                _denom_window = WindowSpec(partition_by=(_quote(partition_col, dialect),))
                _scope_label = denominator_scope
            else:
                _denom_window = WindowSpec()
                _scope_label = denominator_scope or "grand_total"
            total_agg = Aggregation(
                fn=fn, column=column,
                alias=_norm_alias(f"{fn}_{column}_total"),
                distinct=distinct,
                window=_denom_window,
                project=False,
            )
            # Kept in aggregations so windowed-only/SELECT DISTINCT detection still
            # fires, but project=False so neither is emitted as an output column.
            parsed.aggregations.extend([group_agg, total_agg])
            # Record the denominator scope as a ParsedKPI field for downstream
            # audit and enforcement (intent_coverage denominator_scope_findings).
            parsed.denominator_scope = _scope_label
            parsed.denominator_scope_alternative = (
                "grand_total" if _denom_is_within else
                (f"within_{partition_col}" if partition_col else None)
            )
            # Inline the numerator/denominator window expressions directly so the
            # view emits ONLY cuts + the single percentage_share column (no leaked
            # sum_*_per_group / sum_*_total scaffolding columns).
            parsed.extra_select_exprs.append(
                (
                    f"CAST({_agg_expr_no_alias(group_agg, dialect)} AS DOUBLE) "
                    f"/ NULLIF({_agg_expr_no_alias(total_agg, dialect)}, 0) * 100",
                    "percentage_share",
                )
            )
            # Window aggregations don't GROUP BY; they OVER. Dimensions still
            # appear in SELECT, deduped to one row per cell via SELECT DISTINCT
            # (BUG-012).
            return parsed

    if "/" in metric_text and any(
        fn in metric_text.lower() for fn in ("sum(", "count(", "avg(", "count (")
    ):
        halves = [h.strip() for h in metric_text.split("/", 1)]
        numerator = _parse_aggregation(halves[0], lookup, dialect)
        denominator = _parse_aggregation(halves[1], lookup, dialect) if len(halves) > 1 else None
        if numerator and denominator:
            parsed.ratio = (numerator, denominator)
            parsed.aggregations.extend([numerator, denominator])
        elif numerator:
            parsed.aggregations.append(numerator)
        elif denominator:
            parsed.aggregations.append(denominator)
        else:
            parsed.fallback_reason = "ratio metric could not be parsed"
    else:
        agg = _parse_aggregation(metric_text, lookup, dialect)
        if agg:
            # Apply window-function intent (other than mismatched-grain percentage,
            # which short-circuits earlier).
            kind = window_intent.get("kind")
            if kind == "percent_of_total" and agg.fn in {"sum", "count", "avg"}:
                # A percentage is one logical column. The base aggregate and the
                # grand-total denominator are internal scaffolding: mark both
                # project=False and inline them so the view emits only
                # cuts + percent_of_total (consistent with the share-of-group path).
                # (Filters are parsed AFTER this point; the filtered-numerator
                # rewrite happens at the end of parse_kpi.)
                base_agg = replace(agg, project=False)
                parsed.aggregations.append(base_agg)
                parsed.extra_select_exprs.append(
                    (
                        f"CAST({_agg_expr_no_alias(base_agg, dialect)} AS DOUBLE) "
                        f"/ NULLIF(SUM({_agg_expr_no_alias(base_agg, dialect)}) OVER (), 0) * 100",
                        "percent_of_total",
                    )
                )
            elif kind == "percent_of_group":
                group_col = _resolve_column(window_intent["group"], lookup)
                if group_col.lower() not in lookup:
                    # Fallback to percent_of_total if the matched group name is not a valid column in lookup
                    base_agg = replace(agg, project=False)
                    parsed.aggregations.append(base_agg)
                    parsed.extra_select_exprs.append(
                        (
                            f"CAST({_agg_expr_no_alias(base_agg, dialect)} AS DOUBLE) "
                            f"/ NULLIF(SUM({_agg_expr_no_alias(base_agg, dialect)}) OVER (), 0) * 100",
                            "percent_of_total",
                        )
                    )
                else:
                    # Same single-metric-column rule: inline base + per-group
                    # denominator, emit only cuts + percent_of_<group>.
                    base_agg = replace(agg, project=False)
                    parsed.aggregations.append(base_agg)
                    parsed.extra_select_exprs.append(
                        (
                            f"CAST({_agg_expr_no_alias(base_agg, dialect)} AS DOUBLE) "
                            f"/ NULLIF(SUM({_agg_expr_no_alias(base_agg, dialect)}) OVER (PARTITION BY {_quote(group_col, dialect)}), 0) * 100",
                            f"percent_of_{_norm_alias(group_col)}",
                        )
                    )
            elif kind == "running_total":
                running = Aggregation(
                    fn=agg.fn, column=agg.column,
                    alias=_norm_alias(f"running_{agg.alias}"),
                    distinct=agg.distinct,
                    window=WindowSpec(order_by=("__time_order__",)),
                )
                parsed.aggregations.extend([agg, running])
            elif kind == "moving_average":
                size = int(window_intent.get("window_size") or 3)
                rolling = Aggregation(
                    fn="avg", column=agg.column,
                    alias=_norm_alias(f"moving_avg_{size}_{agg.column}"),
                    window=WindowSpec(
                        order_by=("__time_order__",),
                        frame=f"ROWS BETWEEN {size - 1} PRECEDING AND CURRENT ROW",
                    ),
                )
                parsed.aggregations.extend([agg, rolling])
            elif kind == "rank_within":
                partition_col = _resolve_column(window_intent["partition"], lookup)
                rank_agg = Aggregation(
                    fn="row_number", column="",
                    alias=_norm_alias(f"rank_within_{partition_col}"),
                    window=WindowSpec(
                        partition_by=(_quote(partition_col, dialect),),
                        order_by=(f"{agg.alias} DESC",),
                    ),
                )
                parsed.aggregations.append(agg)
                parsed.aggregations.append(rank_agg)
            else:
                parsed.aggregations.append(agg)
                # Recover a second measure the single-valued `metric` dropped
                # ("...and the average base cost", "...and the number of times").
                # Simple metrics only -- the special window/share branches above
                # are never reached here.
                secondary = _detect_secondary_measure(name_text, lookup, agg.fn)
                if secondary is not None and not any(
                    a.alias == secondary.alias for a in parsed.aggregations
                ):
                    parsed.aggregations.append(secondary)
        elif metric_text:
            parsed.fallback_reason = f"unrecognized metric shape: `{metric_text}`"

    for token in _split_cuts(cuts_text):
        if _parse_filter(token, lookup):
            parsed.filters.append(_parse_filter(token, lookup))
            continue
        bucket = _detect_time_bucket(token)
        if bucket:
            unit, source, alias = bucket
            source_col = _resolve_column(source or alias, lookup) or alias
            expr = f"date_trunc('{unit}', CAST({_quote(source_col, dialect)} AS DATE))"
            parsed.dimensions.append(Dimension(expression=expr, alias=_norm_alias(alias)))
            continue
        # Skip tokens that will be handled by _detect_date_arithmetic (age/days-since)
        if _AGE_PATTERN.search(token) or _DAYS_SINCE_PATTERN.search(token):
            continue
        clean_token = re.sub(r"\(.*?\)", "", token).strip()
        if not clean_token:
            continue
        column = _resolve_column(clean_token, lookup)
        parsed.dimensions.append(
            Dimension(expression=_quote(column, dialect), alias=_norm_alias(column))
        )

    # Prose temporal grain: the question asks for a per-period breakdown ("each
    # quarter over time") but `cuts` carried no time bucket. Synthesize the
    # bucket ONLY when a date column is already a resolved feature (no
    # fabrication) and no temporal dimension exists yet. Generalises kpi_008.
    _temporal_aliases = {"year", "quarter", "month", "week", "day"}
    if not any(d.alias in _temporal_aliases for d in parsed.dimensions):
        unit = _prose_temporal_unit(name_text)
        if unit:
            date_col = _date_column_from_lookup(lookup)
            if date_col:
                expr = f"date_trunc('{unit}', CAST({_quote(date_col, dialect)} AS DATE))"
                parsed.dimensions.insert(
                    0, Dimension(expression=expr, alias=_norm_alias(unit)))

    # Prose categorical filter: "for <value> <lob_col>" pattern.
    # Strategy: find any dimension whose source column name appears AFTER "for <value>"
    # in the name text. Limits match to single capitalised or quoted words to
    # avoid grabbing multi-word phrases.
    # e.g. "for Medicare LOB" → col=LineOfBusiness, val=Medicare
    #      "for Commercial segment" → col=Segment, val=Commercial
    for dim in parsed.dimensions:
        if dim.alias in {"month", "quarter", "year", "week", "day", "age", "age_band"}:
            continue
        col = dim.expression.strip('"').strip('`')
        col_label = col.lower().replace("_", "").replace(" ", "")
        # Match: "for <SingleWord> <col_label>" where SingleWord starts with uppercase
        prose_match = re.search(
            rf"\bfor\s+([A-Z][a-z]+)\s+{re.escape(col_label)}\b",
            name_text, re.IGNORECASE,
        )
        if prose_match:
            val = prose_match.group(1).strip()
            if val and not any(f.value.strip("'").lower() == val.lower() for f in parsed.filters):
                parsed.filters.append(FilterClause(column=col, op="=", value=f"'{val}'"))

    # Prose age threshold: "above 50", "over 50 years", "patients above 50"
    age_threshold_match = re.search(
        r"\b(?:above|over|older\s+than|greater\s+than|>\s*=?)\s*(\d+)\s*(?:years?(?:\s+of\s+age)?)?",
        name_text, re.IGNORECASE,
    )
    if age_threshold_match:
        threshold = age_threshold_match.group(1)
        # Find the age expression already added as a dimension
        age_dim = next(
            (d for d in parsed.dimensions if d.alias == "age" and "date_diff" in d.expression),
            None,
        )
        if age_dim:
            parsed.filters.append(
                FilterClause(column=age_dim.expression, op=">", value=threshold, is_literal=False)
            )

    for source_text in (cuts_text, name_text):
        for match in re.finditer(r"['\"]([^'\"]{1,80})['\"]", source_text):
            literal = match.group(1).strip()
            if not literal:
                continue
            # Reject prose fragments: a real categorical value is short and not a
            # multi-word phrase scraped out of the KPI text (guards the
            # `= 'amount paid for medicare LOB across'` class of bug).
            if len(literal.split()) > 2 or len(literal) > 40:
                continue
            anchor = ""
            for dim in parsed.dimensions:
                if dim.alias.lower() in source_text.lower():
                    anchor = dim.expression.strip('"')
                    break
            for filt in parsed.filters:
                if literal.lower() == filt.value.strip("'").lower():
                    break
            else:
                if anchor:
                    parsed.filters.append(
                        FilterClause(column=anchor, op="=", value=f"'{literal}'")
                    )

    top_match = _TOP_N_IN_NAME.search(name_text)
    if top_match:
        try:
            parsed.limit = int(top_match.group(1))
        except (TypeError, ValueError):
            parsed.limit = None
    elif _RANK_HINT.search(name_text):
        # Leaderboard question ("which patients had the MOST readmissions") with
        # no explicit "top N": rank by the measure DESC and cap. Only meaningful
        # when there is a measure to rank by, which the emit step verifies.
        parsed.limit = _DEFAULT_RANK_LIMIT

    # Date arithmetic (age, days-since) — must run BEFORE prose filter detection
    # so the date_diff dimension exists when we look for it.
    for expr, alias, display in _detect_date_arithmetic(
        cuts_text + " " + name_text, lookup, as_of_expr, band_width, dialect=dialect
    ):
        parsed.extra_select_exprs.append((expr, alias))
        parsed.dimensions.append(
            Dimension(expression=expr, alias=alias, display_expression=display)
        )

    # Prose categorical filter: "for <Value> <col_ref>" where col_ref matches the
    # column name, its alias, OR a first-letter abbreviation (e.g. LOB → LineOfBusiness).
    for dim in parsed.dimensions:
        if dim.alias in {"month", "quarter", "year", "week", "day", "age", "age_band"}:
            continue
        col = dim.expression.strip('"').strip('`')
        # Build reference names: column name, alias, and first-letter abbreviation
        ref_words = {col.lower(), dim.alias.lower()}
        # Split on underscore/space AND camelCase boundaries for abbreviation
        camel_words = re.sub(r"([A-Z])", r"_\1", col).strip("_").split("_")
        abbrev = "".join(w[0] for w in camel_words if w).lower()
        if len(abbrev) > 1:
            ref_words.add(abbrev)  # e.g. LineOfBusiness → "lob"
        for ref in ref_words:
            prose_match = re.search(
                rf"\b(?:for|in)\s+([A-Za-z]\w*)\s+{re.escape(ref)}\b",
                name_text, re.IGNORECASE,
            )
            if prose_match:
                val = prose_match.group(1).strip().title()
                if val and not any(f.value.strip("'").lower() == val.lower() for f in parsed.filters):
                    parsed.filters.append(FilterClause(column=col, op="=", value=f"'{val}'"))
                break

    # Prose age threshold: "above 50", "over 50 years", "patients above 50"
    age_threshold_match = re.search(
        r"\b(?:above|over|older\s+than|greater\s+than|>\s*=?)\s*(\d+)\s*(?:years?(?:\s+of\s+age)?)?",
        name_text, re.IGNORECASE,
    )
    if age_threshold_match:
        threshold = age_threshold_match.group(1)
        age_dim = next(
            (d for d in parsed.dimensions if d.alias == "age" and "date_diff" in d.expression),
            None,
        )
        if age_dim:
            parsed.filters.append(
                FilterClause(column=age_dim.expression, op=">", value=threshold, is_literal=False)
            )

    # HAVING — aggregate filters parsed from KPI text.
    parsed.having.extend(_detect_having(metric_text + " " + name_text, parsed.aggregations))

    # Filtered percent-of-total rewrite (runs AFTER filter parsing): filters
    # define the NUMERATOR subset ("how many X had <condition>, and what
    # percentage of all X") — a WHERE clause would filter the denominator too
    # and the percentage would always read 100%. Rewrite to a FILTER (WHERE
    # ...) numerator over an unfiltered plain denominator, drop the WHERE, and
    # also project the filtered count itself (the question's other half). Both
    # sides become plain aggregates (the windowed denominator bound-errored
    # without a GROUP BY anyway). Only the no-dimensions shape is rewritten.
    if parsed.filters and not parsed.dimensions:
        pct_idx = next(
            (i for i, (_, alias) in enumerate(parsed.extra_select_exprs)
             if alias == "percent_of_total"),
            None,
        )
        windowed_total = next(
            (a for a in parsed.aggregations
             if a.window is not None and a.alias.startswith("total_")),
            None,
        )
        base = next(
            (a for a in parsed.aggregations
             if a.window is None and not a.project),
            None,
        )
        if pct_idx is not None and windowed_total is not None and base is not None:
            filters_sql = " AND ".join(_filter_sql(f, dialect) for f in parsed.filters)
            filtered_expr = f"{_agg_expr_no_alias(base, dialect)} FILTER (WHERE {filters_sql})"
            total_expr = _agg_expr_no_alias(base, dialect)
            parsed.aggregations = [a for a in parsed.aggregations if a is not windowed_total]
            parsed.extra_select_exprs[pct_idx] = (
                f"CAST({filtered_expr} AS DOUBLE) / NULLIF({total_expr}, 0) * 100",
                "percent_of_total",
            )
            parsed.extra_select_exprs.insert(pct_idx, (filtered_expr, base.alias))
            parsed.filters = []

    return parsed


def _window_sql(window: WindowSpec) -> str:
    parts: list[str] = []
    if window.partition_by:
        parts.append("PARTITION BY " + ", ".join(window.partition_by))
    if window.order_by:
        parts.append("ORDER BY " + ", ".join(window.order_by))
    if window.frame:
        parts.append(window.frame)
    return "OVER (" + " ".join(parts) + ")"


_ROUND_FNS = {"sum", "avg", "median", "stddev", "variance"}

def _agg_expr_no_alias(agg: Aggregation, dialect: str = "duckdb") -> str:
    """Render an aggregation's SQL expression WITHOUT the trailing ``AS alias``.

    Used both by _agg_sql (which appends the alias) and to inline an internal
    (non-projected) window aggregation into a composite expression such as a
    percentage-share numerator/denominator."""
    quoted_col = _quote(agg.column, dialect) if agg.column else ""
    if agg.fn == "row_number":
        body = "ROW_NUMBER()"
    elif agg.column == "*":
        body = "COUNT(*)"
    elif agg.predicate:
        body = f"SUM(CASE WHEN {agg.predicate} THEN 1 ELSE 0 END)"
    elif agg.distinct and agg.fn in {"count", "sum"}:
        body = f"COUNT(DISTINCT {quoted_col})"
    else:
        raw = f"{agg.fn.upper()}({quoted_col})"
        body = f"ROUND({raw}, 2)" if agg.fn in _ROUND_FNS and agg.window is None else raw
    if agg.window is not None:
        return f"{body} {_window_sql(agg.window)}"
    return body


def _agg_sql(agg: Aggregation, dialect: str = "duckdb") -> str:
    return f"{_agg_expr_no_alias(agg, dialect)} AS {agg.alias}"


def _filter_sql(filt: FilterClause, dialect: str = "duckdb") -> str:
    # If column is an expression (contains parens/spaces) use it as-is; otherwise quote it
    col = filt.column if ("(" in filt.column or " " in filt.column) else _quote(filt.column, dialect)
    return f"{col} {filt.op} {filt.value}"


def build_result_view_sql(
    kpi: dict[str, Any],
    *,
    kpi_id: str,
    feature_view: str,
    result_view: str,
    dialect: str = "duckdb",
    denominator_scope: str | None = None,
    grain_bucketing: str | None = None,
) -> str:
    """Compose the result-view SQL for a KPI. Always returns a valid CREATE VIEW.

    For KPIs whose metric/cuts shape is too complex for the generic builder,
    returns a clearly-commented fallback (`SELECT * FROM features`) so the
    pipeline still produces a valid view but the reviewer sees the gap.

    Parameters
    ----------
    denominator_scope:
        Resolved denominator-scope facet for percentage-share KPIs.
        ``None`` or ``"grand_total"`` / ``"global_total"`` → OVER () (default,
        grand total, no change to existing behaviour).
        ``"within_<group>"`` or any other within-group value → OVER (PARTITION BY
        <partition_col>) — the denominator sums only within the resolved group,
        not across the whole population.  The choice is recorded as an auditable
        SQL comment in the generated view.
    grain_bucketing:
        Resolved grain-bucketing decision for a share/percentage metric cut by a
        raw continuous dimension. ``None`` (no decision) → the view is BLOCKED
        and emits a clearly-marked grain-bucketing proposal instead of the
        exploded GROUP BY. ``"band_continuous_cuts"`` (optionally
        ``"band_continuous_cuts:<width>"``) → the continuous cut is emitted as
        fixed-width bands (``FLOOR(value / width) * width``, default width 10) so
        the share denominator stays meaningful. ``"exact_value_grain"`` confirms
        the exact-value grain and generates one row per value unchanged.
    """
    parsed = parse_kpi(
        kpi,
        denominator_scope=denominator_scope,
        grain_bucketing=grain_bucketing,
        dialect=dialect,
    )
    if parsed.grain_bucketing_block is not None:
        block = parsed.grain_bucketing_block
        lines = [
            "-- BLOCKED: grain-bucketing decision required (no exploded GROUP BY emitted).",
            f"-- reason: {block['reason']}",
            f"-- recommended: {block['recommended']} | alternatives: {block['alternatives']}",
        ]
        for proposal in block["proposals"]:
            lines.append(f"--   propose: {proposal['proposed_bucket']}")
        lines += [
            f"-- KPI metric: {kpi.get('metric', '')!r}",
            f"-- KPI cuts:   {kpi.get('cuts', '')!r}",
            f"CREATE OR REPLACE VIEW {result_view} AS",
            f"SELECT * FROM {feature_view};",
        ]
        return "\n".join(lines)
    if not parsed.can_compose or (not parsed.aggregations and not parsed.dimensions):
        reason = parsed.fallback_reason or (
            "KPI metric and cuts produced no parseable aggregation; "
            "falling back to a generic projection"
        )
        return "\n".join(
            [
                f"-- Generic builder fallback: {reason}",
                f"-- KPI metric: {kpi.get('metric', '')!r}",
                f"-- KPI cuts:   {kpi.get('cuts', '')!r}",
                f"CREATE OR REPLACE VIEW {result_view} AS",
                f"SELECT * FROM {feature_view};",
            ]
        )

    # Detect "window-only" mode: every aggregation has an OVER clause (or there
    # are no plain aggregations at all). In that mode we do NOT emit GROUP BY —
    # window functions don't aggregate rows away.
    plain_aggs = [a for a in parsed.aggregations if a.window is None]
    has_window_aggs = any(a.window is not None for a in parsed.aggregations)
    windowed_only = has_window_aggs and not plain_aggs

    select_terms: list[str] = []
    group_by_terms: list[str] = []
    for dim in parsed.dimensions:
        select_terms.append(f"{dim.display_expression or dim.expression} AS {dim.alias}")
        if not windowed_only:
            # GROUP BY the EXPRESSION, not the output alias. Alias-grouping is legal in
            # DuckDB/Databricks but resolves to a same-named SOURCE column when one
            # exists, silently regrouping the result. (Reverted an agy-session edit.)
            group_by_terms.append(dim.expression)
    for agg in parsed.aggregations:
        if not agg.project:
            continue  # internal scaffolding (e.g. share numerator/denominator); inlined elsewhere
        select_terms.append(_agg_sql(agg, dialect))
    if parsed.ratio:
        numerator, denominator = parsed.ratio
        select_terms.append(
            f"CAST({numerator.alias} AS DOUBLE) / NULLIF({denominator.alias}, 0) AS ratio"
        )
    # T9: dedupe extra-selects by their PROJECTED ALIAS (exact), not by substring.
    # `any(alias in term ...)` dropped a legitimately-distinct expr whenever a
    # short alias (e.g. `age`) was a substring of another term's alias
    # (`age_band`). Parse each term's trailing `AS <alias>` and compare exactly.
    # Ref: core-audit ob-kpi-d.md.
    def _emitted_alias(term: str) -> str:
        m = re.search(r'\bAS\s+("?[\w]+"?)\s*$', term, re.IGNORECASE)
        return m.group(1).strip('"') if m else ""

    emitted_aliases = {a for a in (_emitted_alias(t) for t in select_terms) if a}
    for expr, alias in parsed.extra_select_exprs:
        if str(alias).strip('"') not in emitted_aliases:
            select_terms.append(f"{expr} AS {alias}")
            emitted_aliases.add(str(alias).strip('"'))

    where_clause = ""
    if parsed.filters:
        where_clause = "WHERE " + " AND ".join(_filter_sql(f, dialect) for f in parsed.filters)

    group_by_clause = ""
    if group_by_terms and plain_aggs:
        group_by_clause = "GROUP BY " + ", ".join(group_by_terms)

    having_clause = ""
    if parsed.having:
        having_clause = "HAVING " + " AND ".join(parsed.having)

    order_by_clause = ""
    if parsed.limit and plain_aggs:
        order_alias = plain_aggs[0].alias
        order_by_clause = f"ORDER BY {order_alias} DESC"
    elif group_by_terms and plain_aggs:
        order_by_clause = "ORDER BY " + ", ".join(group_by_terms)

    limit_clause = f"LIMIT {parsed.limit}" if parsed.limit and plain_aggs else ""

    # In windowed-only mode there is no GROUP BY, so the projection returns one
    # row per source record (every row at the same grain carries identical
    # window values). Dedupe to one row per grain with SELECT DISTINCT. Valid in
    # both duckdb and databricks; safe because windowed-only mode has no
    # ORDER BY/LIMIT and every same-grain row is byte-identical. [ok]
    select_keyword = "SELECT DISTINCT" if windowed_only else "SELECT"
    lines: list[str] = []
    # Auditable denominator-scope comment: always emitted for percentage-share
    # KPIs so the chosen scope is visible in the generated SQL and not silently
    # implied (design/kpi_intent_contract.md §2 "Reported" + §5).
    if parsed.denominator_scope is not None:
        lines.append(f"-- denominator_scope: {parsed.denominator_scope}")
    # Auditable as-of comment: emitted when date arithmetic fell back to
    # CURRENT_DATE so the temporal anchor is visible in the generated SQL.
    if parsed.age_as_of_assumption:
        lines.append(f"-- as_of_assumption: {parsed.age_as_of_assumption}")
    attribution = parsed.share_attribution
    if attribution:
        # Single-attribution share: ROW_NUMBER picks ONE row (= one grain cell)
        # per entity, the outer query aggregates only those rows, so shares sum
        # to ~100% by construction. The mode comment keeps the choice auditable.
        lines.append(
            f"-- share_attribution: single ({attribution['mode']}); "
            "each entity counted in exactly one grain cell"
        )
        entity_q = _quote(attribution["entity_column"], dialect)
        order_sql = ", ".join(attribution["order_terms"])
        lines += [
            f"CREATE OR REPLACE VIEW {result_view} AS",
            "WITH __attributed AS (",
            "  SELECT *,",
            f"    ROW_NUMBER() OVER (PARTITION BY {entity_q} ORDER BY {order_sql})"
            " AS __attribution_rn",
            f"  FROM {feature_view}",
            ")",
            select_keyword,
            "  " + ",\n  ".join(select_terms),
            "FROM __attributed",
        ]
        rn_filter = "__attribution_rn = 1"
        where_clause = (
            where_clause + f" AND {rn_filter}" if where_clause else f"WHERE {rn_filter}"
        )
    else:
        lines += [
            f"CREATE OR REPLACE VIEW {result_view} AS",
            select_keyword,
            "  " + ",\n  ".join(select_terms),
            f"FROM {feature_view}",
        ]
    if where_clause:
        lines.append(where_clause)
    if group_by_clause:
        lines.append(group_by_clause)
    if having_clause:
        lines.append(having_clause)
    if order_by_clause:
        lines.append(order_by_clause)
    if limit_clause:
        lines.append(limit_clause)
    return "\n".join(lines) + ";"


__all__ = [
    "Aggregation",
    "Dimension",
    "FilterClause",
    "ParsedKPI",
    "build_result_view_sql",
    "parse_kpi",
    "raw_date_input_columns",
]
