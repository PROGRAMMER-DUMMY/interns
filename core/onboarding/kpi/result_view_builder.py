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

import re
from dataclasses import dataclass, field
from difflib import SequenceMatcher
from typing import Any


_TIME_BUCKET_PATTERNS: list[tuple[str, str]] = [
    ("year", "year"),
    ("quarter", "quarter"),
    ("month", "month"),
    ("week", "week"),
    ("day", "day"),
]
_AGG_FN_PATTERN = re.compile(
    r"\b(sum|avg|count|min|max)\s*\(\s*(distinct\s+|disitnct\s+)?([^()]+?)\s*\)",
    re.IGNORECASE,
)
_PREDICATE_IN_COUNT = re.compile(r"^\s*([A-Za-z_][A-Za-z0-9_]*)\s*=\s*['\"]([^'\"]+)['\"]\s*$")
_TOP_N_IN_NAME = re.compile(r"\btop\s+(\d+)\b", re.IGNORECASE)
_TIME_BUCKET_HINT = re.compile(
    r"\b(year|quarter|month|week|day)\b(?:\s*\(\s*([A-Za-z_][A-Za-z0-9_]*)\s*\))?",
    re.IGNORECASE,
)
_COMPARISON_FILTER = re.compile(
    r"([A-Za-z_][A-Za-z0-9_]*)\s*([=<>!]+)\s*(?:['\"]([^'\"]+)['\"]|([A-Za-z0-9_.]+))"
)
_AGE_PATTERN = re.compile(
    r"\bage\s*\(\s*([A-Za-z_][A-Za-z0-9_]*)\s*\)|age\s+(?:from|of)\s+([A-Za-z_][A-Za-z0-9_]*)",
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


@dataclass(frozen=True)
class Dimension:
    """A GROUP BY column.

    - `expression` is the SQL fragment used in both SELECT and GROUP BY
      (e.g. `date_trunc('month', "order_date")` or just `"channel"`)
    - `alias` is the AS name in SELECT
    """
    expression: str
    alias: str


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

    @property
    def can_compose(self) -> bool:
        return not self.fallback_reason


def _norm_alias(text: str) -> str:
    cleaned = re.sub(r"[^A-Za-z0-9_]+", "_", text.strip().lower()).strip("_")
    return cleaned or "value"


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
        resolved = label
        for source in feature.get("source_columns") or []:
            if isinstance(source, dict):
                col = str(source.get("column") or "")
                if col:
                    resolved = col
                    break
        out[label.lower()] = resolved
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


def _parse_aggregation(text: str, lookup: dict[str, str]) -> Aggregation | None:
    text = text.strip()
    if not text:
        return None
    if re.match(r"\bcount\s*\(\s*\*\s*\)", text, re.IGNORECASE):
        return Aggregation(fn="count", column="*", alias="row_count")
    match = _AGG_FN_PATTERN.search(text)
    if not match:
        return None
    fn = match.group(1).lower()
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
            predicate=f"{_quote(col)} = '{literal}'",
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


def _detect_date_arithmetic(
    cuts_text: str,
    lookup: dict[str, str],
    as_of_expr: str = "CURRENT_DATE",
) -> list[tuple[str, str]]:
    """Detect age/date-arithmetic expressions in cuts text. Returns
    [(sql_expression, alias), ...] to add to SELECT.

    ``as_of_expr`` is the reference point the arithmetic is measured against.
    For a historical/trend KPI the caller passes the event-date expression
    (e.g. ``CAST("ServiceDate" AS DATE)``) so age is computed as-of the event,
    not as-of today (BUG-005). Defaults to ``CURRENT_DATE`` when the KPI has no
    event date available, preserving the original behavior.
    """
    out: list[tuple[str, str]] = []
    for match in _AGE_PATTERN.finditer(cuts_text):
        source = match.group(1) or match.group(2)
        if not source:
            continue
        col = _resolve_column(source, lookup)
        out.append(
            (
                f"date_diff('year', CAST({_quote(col)} AS DATE), {as_of_expr})",
                "age",
            )
        )
    for match in _DAYS_SINCE_PATTERN.finditer(cuts_text):
        source = match.group(1)
        col = _resolve_column(source, lookup)
        out.append(
            (
                f"date_diff('day', CAST({_quote(col)} AS DATE), {as_of_expr})",
                f"days_since_{_norm_alias(col)}",
            )
        )
    return out


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


def parse_kpi(kpi: dict[str, Any], denominator_scope: str | None = None) -> ParsedKPI:
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
    """
    parsed = ParsedKPI()
    metric_text = str(kpi.get("metric") or "").strip()
    cuts_text = str(kpi.get("cuts") or "").strip()
    name_text = str(kpi.get("name") or kpi.get("business_question") or "").strip()
    lookup = _column_lookup(kpi)
    window_intent = _detect_window_intent(metric_text, name_text)

    # BUG-005: age (and other date arithmetic) must be measured as-of the
    # event/service date when the KPI has one, not as-of today. The event date
    # is the KPI's time-grain source column (e.g. Month(ServiceDate)); when none
    # exists we fall back to CURRENT_DATE.
    event_date_col = _detect_event_date_column(cuts_text, lookup)
    as_of_expr = (
        f"CAST({_quote(event_date_col)} AS DATE)" if event_date_col else "CURRENT_DATE"
    )

    # Mismatched-grain percentage is handled via window functions.
    # The metric reads "<agg> / <same agg> for <group>": a share-of-total by
    # <group>. The numerator is the aggregate within each group and the
    # denominator is the grand total of the same aggregate, so each row is one
    # group and its percentage of the whole (e.g. a department's distinct lives
    # as a fraction of all distinct lives). Shares sum to ~100% across groups.
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
            # population (shares sum to ~100% across all cells). Denominator-scope
            # semantics (per-group vs grand-total) are intentionally left as-is.
            emitted = _emitted_columns(lookup)
            grain_dimensions: list[Dimension] = []
            grain_seen: set[str] = set()

            def _add_grain(expression: str, alias: str) -> None:
                if expression in grain_seen:
                    return
                grain_seen.add(expression)
                grain_dimensions.append(Dimension(expression=expression, alias=alias))

            cut_columns: list[str] = []
            for token in _split_cuts(cuts_text):
                bucket = _detect_time_bucket(token)
                if bucket:
                    unit, source, alias = bucket
                    source_col = _resolve_column(source or alias, lookup) or alias
                    expr = f"date_trunc('{unit}', CAST({_quote(source_col)} AS DATE))"
                    _add_grain(expr, _norm_alias(alias))
                    continue
                if _AGE_PATTERN.search(token) or _DAYS_SINCE_PATTERN.search(token):
                    for expr, alias in _detect_date_arithmetic(token, lookup, as_of_expr):
                        _add_grain(expr, alias)
                    continue
                clean = re.sub(r"\(.*?\)", "", token).strip()
                if not clean:
                    continue
                col = _resolve_column(clean, lookup)
                if col in emitted:
                    cut_columns.append(col)
                    _add_grain(_quote(col), _norm_alias(col))

            # Resolve the "for <group>" token to a REAL emitted column (graceful
            # fallback to the cut columns) and make sure it is part of the grain.
            # A raw/aliased token must never reach PARTITION BY or the view is
            # non-executable (BUG-011).
            partition_col = _resolve_group_column(
                partition, lookup, kpi, tuple(cut_columns)
            ) if partition else ""
            if partition_col and partition_col in emitted:
                _add_grain(_quote(partition_col), _norm_alias(partition_col))

            # An empty grain is fine: the numerator then partitions by nothing
            # (OVER ()), matching the denominator — degenerate but executable. We
            # never emit a PARTITION BY on an UNRESOLVED token (BUG-011); that is
            # already prevented above by only adding emitted columns / a resolved
            # group column.
            parsed.dimensions.extend(grain_dimensions)
            grain_terms = tuple(d.expression for d in grain_dimensions)

            # Numerator = lives within each full-grain cell (PARTITION BY grain).
            group_agg = Aggregation(
                fn=fn, column=column,
                alias=_norm_alias(f"{fn}_{column}_per_group"),
                distinct=distinct,
                window=WindowSpec(partition_by=grain_terms),
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
            if _denom_is_within and partition_col and partition_col in emitted:
                _denom_window = WindowSpec(partition_by=(_quote(partition_col),))
                _scope_label = denominator_scope
            else:
                _denom_window = WindowSpec()
                _scope_label = denominator_scope or "grand_total"
            total_agg = Aggregation(
                fn=fn, column=column,
                alias=_norm_alias(f"{fn}_{column}_total"),
                distinct=distinct,
                window=_denom_window,
            )
            parsed.aggregations.extend([group_agg, total_agg])
            # Record the denominator scope as a ParsedKPI field for downstream
            # audit and enforcement (intent_coverage denominator_scope_findings).
            parsed.denominator_scope = _scope_label
            parsed.denominator_scope_alternative = (
                "grand_total" if _denom_is_within else
                (f"within_{partition_col}" if partition_col else None)
            )
            parsed.extra_select_exprs.append(
                (
                    f"CAST({group_agg.alias} AS DOUBLE) / NULLIF({total_agg.alias}, 0) * 100",
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
        numerator = _parse_aggregation(halves[0], lookup)
        denominator = _parse_aggregation(halves[1], lookup) if len(halves) > 1 else None
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
        agg = _parse_aggregation(metric_text, lookup)
        if agg:
            # Apply window-function intent (other than mismatched-grain percentage,
            # which short-circuits earlier).
            kind = window_intent.get("kind")
            if kind == "percent_of_total" and agg.fn in {"sum", "count", "avg"}:
                total_agg = Aggregation(
                    fn=agg.fn, column=agg.column,
                    alias=_norm_alias(f"total_{agg.alias}"),
                    distinct=agg.distinct, window=WindowSpec(),
                )
                parsed.aggregations.extend([agg, total_agg])
                parsed.extra_select_exprs.append(
                    (
                        f"CAST({agg.alias} AS DOUBLE) / NULLIF({total_agg.alias}, 0) * 100",
                        "percent_of_total",
                    )
                )
            elif kind == "percent_of_group":
                group_col = _resolve_column(window_intent["group"], lookup)
                group_agg = Aggregation(
                    fn=agg.fn, column=agg.column,
                    alias=_norm_alias(f"{agg.alias}_per_{group_col}"),
                    distinct=agg.distinct,
                    window=WindowSpec(partition_by=(_quote(group_col),)),
                )
                parsed.aggregations.extend([agg, group_agg])
                parsed.extra_select_exprs.append(
                    (
                        f"CAST({agg.alias} AS DOUBLE) / NULLIF({group_agg.alias}, 0) * 100",
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
                        partition_by=(_quote(partition_col),),
                        order_by=(f"{agg.alias} DESC",),
                    ),
                )
                parsed.aggregations.append(agg)
                parsed.aggregations.append(rank_agg)
            else:
                parsed.aggregations.append(agg)
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
            expr = f"date_trunc('{unit}', CAST({_quote(source_col)} AS DATE))"
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
            Dimension(expression=_quote(column), alias=_norm_alias(column))
        )

    # Prose categorical filter: "for <value> <lob_col>" pattern.
    # Strategy: find any dimension whose source column name appears AFTER "for <value>"
    # in the name text. Limits match to single capitalised or quoted words to
    # avoid grabbing multi-word phrases.
    # e.g. "for Medicare LOB" → col=LineOfBusiness, val=Medicare
    #      "for Commercial segment" → col=Segment, val=Commercial
    for dim in parsed.dimensions:
        if dim.alias in {"month", "quarter", "year", "week", "day", "age"}:
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

    # Date arithmetic (age, days-since) — must run BEFORE prose filter detection
    # so the date_diff dimension exists when we look for it.
    for expr, alias in _detect_date_arithmetic(cuts_text + " " + name_text, lookup, as_of_expr):
        parsed.extra_select_exprs.append((expr, alias))
        parsed.dimensions.append(Dimension(expression=expr, alias=alias))

    # Prose categorical filter: "for <Value> <col_ref>" where col_ref matches the
    # column name, its alias, OR a first-letter abbreviation (e.g. LOB → LineOfBusiness).
    for dim in parsed.dimensions:
        if dim.alias in {"month", "quarter", "year", "week", "day", "age"}:
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


_ROUND_FNS = {"sum", "avg"}

def _agg_sql(agg: Aggregation, dialect: str = "duckdb") -> str:
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
        return f"{body} {_window_sql(agg.window)} AS {agg.alias}"
    return f"{body} AS {agg.alias}"


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
    """
    parsed = parse_kpi(kpi, denominator_scope=denominator_scope)
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
        select_terms.append(f"{dim.expression} AS {dim.alias}")
        if not windowed_only:
            group_by_terms.append(dim.expression)
    for agg in parsed.aggregations:
        select_terms.append(_agg_sql(agg, dialect))
    if parsed.ratio:
        numerator, denominator = parsed.ratio
        select_terms.append(
            f"CAST({numerator.alias} AS DOUBLE) / NULLIF({denominator.alias}, 0) AS ratio"
        )
    for expr, alias in parsed.extra_select_exprs:
        # Skip if the expr is already in select_terms (e.g., date arithmetic doubled as dimension).
        if not any(alias in term for term in select_terms):
            select_terms.append(f"{expr} AS {alias}")

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
]
