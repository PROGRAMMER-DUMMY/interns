"""KPI intent-coverage checks.

Independently re-derive a KPI's *declared* intent — grain dimensions, the
metric aggregation, and explicit filters — from the raw registry fields
(``metric`` / ``cuts`` / ``name``) and assert the generated result-view SQL
realizes each one.

This is deliberately INDEPENDENT of the generator's own parser
(``result_view_builder.parse_kpi``). If the generator silently drops a declared
cut (BUG-024) or a hand-edit removes one, a check that reused the same parser
would share the blind spot and pass anyway. By re-deriving intent with a
separate, simpler tokenizer, the generator and this verifier must AGREE — a
divergence is a finding.

Workspace-agnostic: no domain vocabulary. Column resolution uses only the
feature ``source_columns`` already present in the KPI entry.

Two additional hard gates:

JOIN-CORRECTNESS — every JOIN in a generated result/feature view must correspond
to a proven relationship loaded from ``relationship_contracts.json``.  A join on
an unproven or unknown pair of columns is a ``join_not_proven`` error.

FILTER-REALIZATION (prose) — high-confidence prose filters declared in the KPI
name are checked for presence in the result-view SQL.  Only two conservative
patterns are flagged to avoid false positives on descriptive titles:

  * ``for <Value> <lob-ish-word>`` (e.g. "for Medicare Plan", "for Commercial LOB")
  * ``above/over N year`` age threshold (e.g. "above 65 years")
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any


_TIME_UNITS = ("year", "quarter", "month", "week", "day")
_AGG_RE = re.compile(
    r"\b(sum|avg|count|min|max)\s*\(\s*(distinct\s+|disitnct\s+)?([^()]+?)\s*\)",
    re.IGNORECASE,
)
_COUNT_STAR_RE = re.compile(r"count\s*\(\s*\*\s*\)", re.IGNORECASE)
_AGE_RE = re.compile(r"\bage\b", re.IGNORECASE)
_DAYS_SINCE_RE = re.compile(r"\bdays?\s+since\b", re.IGNORECASE)
_CUT_SPLIT_RE = re.compile(r"[,;]| and ", re.IGNORECASE)
_COMPARISON_RE = re.compile(r"[<>]=?|!?=")

# Conservative LOB-suffix words that signal a "for <Value> <word>" filter.
# Deliberately kept short to reduce false positives; only unambiguous category
# suffixes qualify.  Workspace-agnostic — derived from domain-neutral lexicon.
_LOB_SUFFIX_WORDS = frozenset(
    {
        "lob",
        "line",
        "plan",
        "program",
        "type",
        "segment",
        "category",
        "class",
        "group",
        "channel",
        "tier",
    }
)

# "for <CamelCaseOrAllCaps> <lob-suffix>" pattern — captures the filter value.
# The value must start with an uppercase letter (i.e. is a proper noun / enum
# value, not a generic description word).
_PROSE_LOB_RE = re.compile(
    r"\bfor\s+([A-Z][A-Za-z0-9_-]*)\s+(?:" + "|".join(_LOB_SUFFIX_WORDS) + r")\b",
    re.IGNORECASE,
)

# "above/over N year(s)" age-threshold pattern.
_PROSE_AGE_THRESHOLD_RE = re.compile(
    r"\b(?:above|over)\s+(\d+)\s+years?\b",
    re.IGNORECASE,
)

# SQL JOIN clause extractor: captures the ON clause column pair.
# Handles: JOIN tbl [AS alias] ON [alias.]"ColA" = [alias.]"ColB"
# Also handles unquoted identifiers and backtick quoting.
_JOIN_ON_RE = re.compile(
    r"\bJOIN\b[^O]*?\bON\b\s+"
    r'(?:[A-Za-z_][A-Za-z0-9_]*\.)?[`"]?([A-Za-z_][A-Za-z0-9_]*)[`"]?'
    r"\s*=\s*"
    r'(?:[A-Za-z_][A-Za-z0-9_]*\.)?[`"]?([A-Za-z_][A-Za-z0-9_]*)[`"]?',
    re.IGNORECASE | re.DOTALL,
)

# Proven states — matches EXECUTABLE_RELATIONSHIP_STATES in contracts.py.
_PROVEN_STATES = frozenset({"proven_data_model", "user_confirmed"})
_APPROVED_APPROVAL_STATES = frozenset({"approved_for_execution", "approved"})


@dataclass(frozen=True)
class CoverageFinding:
    severity: str  # "error" | "warning"
    code: str  # grain_not_realized | metric_not_realized | filter_not_realized
    kind: str  # grain | metric | filter
    expected: str
    message: str


def _norm(text: Any) -> str:
    """Lowercase, strip everything but alphanumerics."""
    return re.sub(r"[^a-z0-9]+", "", str(text or "").lower())


def _compact(text: str) -> str:
    """Lowercase and strip whitespace so `count( distinct x )` matches `count(distinct`."""
    return re.sub(r"\s+", "", text.lower())


def _token_present(token: str, block_lower: str) -> bool:
    """True when `token` appears as a whole identifier/word in the lowercased SQL.

    Word-boundary matching (NOT substring on alnum-normalized text): `age` must
    match `AS age` / `PARTITION BY ... age` but must NOT spuriously match inside
    `percentage_share`. Quotes and parens around SQL identifiers act as
    boundaries, so `"VisitType"` and `AS visittype` both match `visittype`.
    """
    cleaned = token.lower().strip()
    if not cleaned:
        return False
    return re.search(rf"\b{re.escape(cleaned)}\b", block_lower) is not None


def _feature_column_lookup(kpi: dict[str, Any]) -> dict[str, str]:
    """Map feature label (lowercased) -> first source column. Data, not logic —
    safe to share with the generator; the *coverage logic* stays independent."""
    out: dict[str, str] = {}
    for feature in kpi.get("features") or []:
        if not isinstance(feature, dict):
            continue
        label = str(feature.get("feature") or feature.get("name") or "")
        if not label:
            continue
        # A derived-formula feature is materialized under its OWN name in the
        # features view; its source_columns are the formula INPUTS. The label
        # resolves to itself (mirrors the generator's _column_lookup, so the
        # checker expects the same column the generator emits).
        if str(feature.get("resolution_type") or "") == "derived_formula":
            out[label.lower()] = label
            continue
        resolved = label
        for source in feature.get("source_columns") or []:
            if isinstance(source, dict) and source.get("column"):
                resolved = str(source["column"])
                break
        out[label.lower()] = resolved
    return out


def _resolve_cut_column(token: str, lookup: dict[str, str]) -> str:
    """Resolve a cut token to an underlying column: exact label, normalized
    label, then word-subset (feature label words all contained in the token).
    Falls back to the token itself so the verifier still flags it if absent."""
    key = token.lower().strip()
    if key in lookup:
        return lookup[key]
    norm = _norm(key)
    for label, column in lookup.items():
        if _norm(label) == norm:
            return column
    cut_words = set(re.findall(r"[a-z0-9]+", key))
    best: tuple[int, str] | None = None
    for label, column in lookup.items():
        label_words = set(re.findall(r"[a-z0-9]+", label))
        if label_words and label_words <= cut_words:
            if best is None or len(label_words) > best[0]:
                best = (len(label_words), column)
    return best[1] if best else token


def _split_cuts(cuts: str) -> list[str]:
    return [part.strip() for part in _CUT_SPLIT_RE.split(cuts) if part.strip()]


def result_view_block(sql: str, kpi_id: str) -> str:
    """Return the SQL text from the `CREATE ... VIEW <kpi_id>_results AS` clause
    onward. Coverage must be scoped to the RESULT view: a dimension present only
    in an upstream features/staging view (but dropped from the result grain)
    must NOT count as realized. Returns "" when the result view is not defined."""
    pattern = (
        r"create\s+or\s+replace\s+(?:temp\w*\s+)?view\s+"
        r'[`"]?' + re.escape(f"{kpi_id}_results") + r'[`"]?\s+as'
    )
    match = re.search(pattern, sql, flags=re.IGNORECASE)
    if not match:
        return ""
    return sql[match.start():]


def declared_grain(kpi: dict[str, Any]) -> list[tuple[str, list[str]]]:
    """Independently extract declared grain dimensions from raw `cuts`.

    Returns [(raw_token, [expected_tokens])]. A token carrying a comparison
    operator is a filter, not a grain dimension, and is excluded.
    """
    lookup = _feature_column_lookup(kpi)
    out: list[tuple[str, list[str]]] = []
    for token in _split_cuts(str(kpi.get("cuts") or "")):
        if _COMPARISON_RE.search(token):
            continue  # this is a filter clause, handled separately
        low = token.lower()
        unit = next((u for u in _TIME_UNITS if re.search(rf"\b{u}\b", low)), "")
        if unit:
            out.append((token, [unit]))
            continue
        if _AGE_RE.search(low):
            # An age cut is realized either as the exact `age` or, when a
            # band_continuous_cuts grain decision is in effect, as `age_band`.
            # Accept either so the banded form is not flagged as a dropped cut.
            # Stays independent of the generator's parser (BUG-024 rationale);
            # it just knows the banding alias convention.
            out.append((token, ["age", "age_band"]))
            continue
        if _DAYS_SINCE_RE.search(low):
            # `date_diff` covers both the exact days-since and the banded
            # `days_since_<col>_band` form (the band wraps the same date_diff).
            out.append((token, ["days_since", "date_diff"]))
            continue
        clean = re.sub(r"\(.*?\)", "", token).strip()
        if not clean:
            continue
        out.append((token, [_resolve_cut_column(clean, lookup)]))
    return out


def declared_metric_aggs(kpi: dict[str, Any]) -> list[tuple[str, str, bool]]:
    """Extract (fn, input_column, distinct) tuples from the raw `metric`."""
    metric = str(kpi.get("metric") or "")
    out: list[tuple[str, str, bool]] = []
    if _COUNT_STAR_RE.search(metric):
        out.append(("count", "*", False))
    for match in _AGG_RE.finditer(metric):
        fn = match.group(1).lower()
        distinct = bool(match.group(2))
        inner = match.group(3).strip()
        if "=" in inner:  # predicate count, e.g. count(status = 'open')
            inner = inner.split("=", 1)[0].strip()
        out.append((fn, inner, distinct))
    return out


def declared_explicit_filters(kpi: dict[str, Any]) -> list[str]:
    """Quoted literal filter values present in `cuts` (high confidence). Prose
    filters in the name are intentionally NOT treated as hard-gate filters to
    avoid false blocks on workspaces with descriptive titles."""
    out: list[str] = []
    seen: set[str] = set()
    for match in re.finditer(r"['\"]([^'\"]{1,80})['\"]", str(kpi.get("cuts") or "")):
        value = match.group(1).strip()
        if value and len(value.split()) <= 2 and len(value) <= 40:
            key = value.lower()
            if key not in seen:
                seen.add(key)
                out.append(value)
    return out


def grain_coverage_findings(kpi: dict[str, Any], sql: str) -> list[CoverageFinding]:
    """The enforced check: every declared grain dimension must appear in the
    generated result-view SQL. This is the BUG-024 detector."""
    kpi_id = str(kpi.get("kpi_id") or "")
    block = result_view_block(sql, kpi_id) if kpi_id else sql
    if not block:
        # No result view to inspect; the execution harness reports the missing
        # view separately — don't double-report here.
        return []
    block_lower = block.lower()
    findings: list[CoverageFinding] = []
    for raw_token, expected in declared_grain(kpi):
        if any(_token_present(token, block_lower) for token in expected):
            continue
        findings.append(
            CoverageFinding(
                severity="error",
                code="grain_not_realized",
                kind="grain",
                expected=raw_token,
                message=(
                    f"declared cut/grain dimension `{raw_token}` "
                    f"(expected {expected}) is absent from the generated "
                    f"result view for `{kpi_id or 'kpi'}`"
                ),
            )
        )
    return findings


def evaluate_intent_coverage(kpi: dict[str, Any], sql: str) -> list[CoverageFinding]:
    """Full intent coverage: grain + metric aggregation + explicit filters.
    Used by the named intent-coverage harness/report. Enforcement of the grain
    subset also runs inside the execution harness via grain_coverage_findings."""
    kpi_id = str(kpi.get("kpi_id") or "")
    block = result_view_block(sql, kpi_id) if kpi_id else sql
    if not block:
        return [
            CoverageFinding(
                severity="error",
                code="result_view_missing",
                kind="grain",
                expected=f"{kpi_id}_results",
                message=f"generated SQL does not define result view `{kpi_id}_results`",
            )
        ]
    findings = grain_coverage_findings(kpi, sql)

    compact = _compact(block)
    block_lower = block.lower()
    for fn, column, distinct in declared_metric_aggs(kpi):
        # sum/count over DISTINCT is rendered as COUNT(DISTINCT ...) by the
        # generator (summing distinct values is meaningless), so accept that.
        fn_present = f"{fn}(" in compact
        if distinct:
            fn_present = fn_present or "count(distinct" in compact or f"{fn}(distinct" in compact
        col_present = column == "*" or _token_present(column, block_lower)
        if not (fn_present and col_present):
            findings.append(
                CoverageFinding(
                    severity="error",
                    code="metric_not_realized",
                    kind="metric",
                    expected=f"{fn}({'distinct ' if distinct else ''}{column})",
                    message=(
                        f"metric aggregation `{fn}({'distinct ' if distinct else ''}{column})` "
                        f"is not realized in the result view for `{kpi_id or 'kpi'}`"
                    ),
                )
            )

    for literal in declared_explicit_filters(kpi):
        if _token_present(literal, block_lower):
            continue
        findings.append(
            CoverageFinding(
                severity="error",
                code="filter_not_realized",
                kind="filter",
                expected=literal,
                message=(
                    f"declared filter literal `{literal}` is absent from the "
                    f"result view for `{kpi_id or 'kpi'}`"
                ),
            )
        )

    # Prose-filter realization (conservative patterns only).
    findings.extend(prose_filter_findings(kpi, sql))

    return findings


# ---------------------------------------------------------------------------
# DENOMINATOR-SCOPE check (design/kpi_intent_contract.md §5)
# ---------------------------------------------------------------------------

# Pattern: a bare OVER () denominator — matches the common
# ``COUNT(DISTINCT ...) OVER ()`` or ``SUM(...) OVER ()`` form.
_OVER_GRAND_TOTAL_RE = re.compile(r"\bOVER\s*\(\s*\)", re.IGNORECASE)

# Pattern: OVER (PARTITION BY ...) with something inside — the denominator
# is scoped to a group, not the whole population.
_OVER_PARTITION_BY_RE = re.compile(r"\bOVER\s*\(\s*PARTITION\s+BY\b", re.IGNORECASE)


def _is_within_group_scope(scope: str) -> bool:
    """Return True when *scope* is a within-group denominator-scope value.

    Grand-total / absent values (``None``, ``"grand_total"``,
    ``"global_total"``) return False.  Anything else (e.g.
    ``"within_department"``, ``"within_region"``) returns True.
    This mirrors the decision logic in ``result_view_builder.build_result_view_sql``.
    """
    return bool(scope) and scope not in {"grand_total", "global_total"}


def denominator_scope_findings(
    kpi: dict[str, Any],
    sql: str,
    scope: str | None,
) -> list[CoverageFinding]:
    """Check that the denominator window in the result-view SQL matches *scope*.

    Independently re-checks the generated SQL without reusing the generator's
    parser (same independence rationale as grain_coverage_findings): if the
    generator silently drops the scope, a check that called the generator
    would share the blind spot.

    Rules
    -----
    * ``scope`` is ``None`` / ``"grand_total"`` / ``"global_total"`` (default):
      no check — the grand-total window (OVER ()) is always acceptable and we
      do not flag an absent scope.
    * ``scope`` is a within-group value (anything else):
      - The result view must NOT contain any bare ``OVER ()`` window, because a
        within-group scope means ALL windows (including the denominator) must
        be partitioned.  A bare ``OVER ()`` means the denominator was not
        updated — the recorded decision was silently ignored by the generator.
      - If a bare ``OVER ()`` is present under a within-group scope, that is
        ``denominator_scope_not_realized``.

    Parameters
    ----------
    kpi:
        KPI registry / feature-mapping entry (used only for ``kpi_id``).
    sql:
        Full generated SQL for the KPI (may include both feature and result
        views).  Coverage is scoped to the result view.
    scope:
        The resolved denominator-scope facet value from
        ``pipeline_decisions.json`` (or ``None`` when absent).
    """
    if not _is_within_group_scope(scope or ""):
        return []

    kpi_id = str(kpi.get("kpi_id") or "")
    block = result_view_block(sql, kpi_id) if kpi_id else sql
    if not block:
        # No result view to inspect — the missing-view error is reported elsewhere.
        return []

    # Under a within-group scope, ANY bare OVER () window in the result view is
    # evidence that the denominator was NOT scoped to the group.  The correctly
    # realized SQL has no bare OVER () — every window is partitioned.
    has_grand_total = bool(_OVER_GRAND_TOTAL_RE.search(block))

    if has_grand_total:
        return [
            CoverageFinding(
                severity="error",
                code="denominator_scope_not_realized",
                kind="denominator_scope",
                expected=str(scope),
                message=(
                    f"denominator_scope `{scope}` was recorded for `{kpi_id or 'kpi'}` "
                    f"but the result-view SQL contains a grand-total OVER () window "
                    f"instead of OVER (PARTITION BY <group>) for the denominator. "
                    f"The recorded decision was not applied by the generator."
                ),
            )
        ]

    # No bare OVER () found — denominator is correctly scoped (or the KPI
    # does not use a window denominator at all, so the check is not applicable).
    return []


# ---------------------------------------------------------------------------
# TEMPORAL-ANCHOR check (design/kpi_intent_contract.md §5, BUG-005 latent risk)
# ---------------------------------------------------------------------------

# Detects date-arithmetic that uses CURRENT_DATE as the reference point.
_CURRENT_DATE_RE = re.compile(r"\bCURRENT_DATE\b", re.IGNORECASE)

# Detects date-arithmetic that uses a column cast to DATE (event-date anchor).
# Matches: CAST("SomeColumn" AS DATE) or CAST(`SomeColumn` AS DATE)
_CAST_COL_AS_DATE_RE = re.compile(
    r"\bCAST\s*\([`\"]?[A-Za-z_][A-Za-z0-9_]*[`\"]?\s+AS\s+DATE\s*\)",
    re.IGNORECASE,
)

# Time-grain column presence in cuts: "Month (col)", "Year(col)", etc.
_TIME_GRAIN_SOURCE_RE = re.compile(
    r"\b(?:year|quarter|month|week|day)\s*\(\s*([A-Za-z_][A-Za-z0-9_]*)\s*\)",
    re.IGNORECASE,
)

# Age/days-since arithmetic in cuts.
_AGE_CUT_RE = re.compile(r"\bage\b", re.IGNORECASE)
_DAYS_SINCE_CUT_RE = re.compile(r"\bdays?\s+since\b", re.IGNORECASE)


def temporal_anchor_findings(kpi: dict[str, Any], sql: str) -> list[CoverageFinding]:
    """Conservative check: when a KPI has both age/date arithmetic in its cuts
    AND an explicit time-grain source column (e.g. ``Month(ServiceDate)``), the
    generated SQL must NOT use ``CURRENT_DATE`` as the age reference — it should
    use the event-date column (BUG-005 pattern).

    Conservative: only fires when ALL three conditions hold:
    1. Cuts declare age or days-since arithmetic.
    2. Cuts also carry an explicit time-grain source column (event date present).
    3. The result-view SQL uses ``CURRENT_DATE`` in date arithmetic.

    Returns an empty list when any condition is absent, so false-positive rate
    for KPIs without age arithmetic or without an event-date grain is zero.
    """
    kpi_id = str(kpi.get("kpi_id") or "")
    cuts = str(kpi.get("cuts") or "")

    has_age_cut = bool(_AGE_CUT_RE.search(cuts) or _DAYS_SINCE_CUT_RE.search(cuts))
    if not has_age_cut:
        return []

    has_event_date_grain = bool(_TIME_GRAIN_SOURCE_RE.search(cuts))
    if not has_event_date_grain:
        return []

    block = result_view_block(sql, kpi_id) if kpi_id else sql
    if not block:
        return []

    if not _CURRENT_DATE_RE.search(block):
        return []

    # Both conditions met and CURRENT_DATE found — flag as temporal-anchor error.
    event_col_match = _TIME_GRAIN_SOURCE_RE.search(cuts)
    event_col = event_col_match.group(1) if event_col_match else "<event_col>"
    return [
        CoverageFinding(
            severity="error",
            code="temporal_anchor_not_realized",
            kind="temporal_anchor",
            expected=f"event_date:{event_col}",
            message=(
                f"KPI `{kpi_id or 'kpi'}` has age/date arithmetic and an event-date "
                f"grain column `{event_col}`, but the result-view SQL uses CURRENT_DATE "
                f"as the reference instead of the event date (BUG-005 pattern). "
                f"Age should be computed as-of the event, not as-of today."
            ),
        )
    ]


# ---------------------------------------------------------------------------
# OUTPUT-SHAPE check (design/kpi_intent_contract.md §5)
# ---------------------------------------------------------------------------

_TOP_N_NAME_RE = re.compile(r"\btop\s+(\d+)\b", re.IGNORECASE)
_LIMIT_RE = re.compile(r"\bLIMIT\s+(\d+)\b", re.IGNORECASE)


def output_shape_findings(kpi: dict[str, Any], sql: str) -> list[CoverageFinding]:
    """Conservative check: a ``top N`` KPI name must emit ``LIMIT N`` in the
    result-view SQL.

    Conservative: only fires when the KPI name contains a ``top N`` token AND
    the result view has no matching ``LIMIT N`` clause.

    Returns an empty list when the KPI name does not mention ``top N``.
    """
    kpi_id = str(kpi.get("kpi_id") or "")
    name = str(kpi.get("name") or kpi.get("description") or "")

    top_match = _TOP_N_NAME_RE.search(name)
    if not top_match:
        return []
    n = top_match.group(1)

    block = result_view_block(sql, kpi_id) if kpi_id else sql
    if not block:
        return []

    limit_match = _LIMIT_RE.search(block)
    if limit_match and limit_match.group(1) == n:
        return []

    return [
        CoverageFinding(
            severity="error",
            code="output_shape_not_realized",
            kind="output_shape",
            expected=f"LIMIT {n}",
            message=(
                f"KPI `{kpi_id or 'kpi'}` name declares a top-{n} ranking "
                f"but the result-view SQL does not contain LIMIT {n}."
            ),
        )
    ]


# ---------------------------------------------------------------------------
# JOIN-CORRECTNESS check
# ---------------------------------------------------------------------------

def _load_proven_join_pairs(relationship_contracts_path: str | Path) -> set[frozenset[str]]:
    """Load proven column pairs from a relationship_contracts.json artifact.

    Returns a set of frozensets, each containing two normalized column-name
    strings ``{col_a_lower, col_b_lower}``.  Either join direction satisfies
    the check.  Only relationships whose ``state`` is in ``_PROVEN_STATES`` OR
    whose ``approval.state`` is in ``_APPROVED_APPROVAL_STATES`` are included.

    Raises no exceptions — returns an empty set when the file is missing or
    unparseable so that callers can decide whether to block or warn.
    """
    path = Path(relationship_contracts_path)
    if not path.exists():
        return set()
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return set()

    pairs: set[frozenset[str]] = set()
    for rel in data.get("relationships") or []:
        if not isinstance(rel, dict):
            continue
        state = str(rel.get("state") or "")
        approval_state = str((rel.get("approval") or {}).get("state") or "")
        if state not in _PROVEN_STATES and approval_state not in _APPROVED_APPROVAL_STATES:
            continue
        left_col = str(rel.get("left_column") or "").strip()
        right_col = str(rel.get("right_column") or "").strip()
        if left_col and right_col:
            pairs.add(frozenset({left_col.lower(), right_col.lower()}))
    return pairs


def _extract_join_pairs(sql: str) -> list[tuple[str, str]]:
    """Parse JOIN ... ON a.col_x = b.col_y clauses from SQL.

    Returns a list of (col_left, col_right) tuples of bare column names
    (unquoted, un-aliased).  Independent of the generator's own SQL parser.
    """
    pairs: list[tuple[str, str]] = []
    for match in _JOIN_ON_RE.finditer(sql):
        col_a = match.group(1).strip()
        col_b = match.group(2).strip()
        if col_a and col_b:
            pairs.append((col_a, col_b))
    return pairs


def join_correctness_findings(
    sql: str,
    proven_pairs: set[frozenset[str]],
    *,
    kpi_id: str = "",
) -> list[CoverageFinding]:
    """Check every JOIN ON clause in *sql* against the set of proven column
    pairs.  Returns error-severity ``CoverageFinding`` items for each
    unproven join.

    Parameters
    ----------
    sql:
        Full generated SQL (may contain feature views and the result view).
    proven_pairs:
        Set of ``frozenset({col_a_lower, col_b_lower})`` loaded from
        ``relationship_contracts.json`` via ``_load_proven_join_pairs``.
    kpi_id:
        KPI identifier for error messages (cosmetic only).
    """
    findings: list[CoverageFinding] = []
    for col_a, col_b in _extract_join_pairs(sql):
        pair = frozenset({col_a.lower(), col_b.lower()})
        if pair not in proven_pairs:
            findings.append(
                CoverageFinding(
                    severity="error",
                    code="join_not_proven",
                    kind="join",
                    expected=f"{col_a} = {col_b}",
                    message=(
                        f"JOIN ON `{col_a}` = `{col_b}` in the generated SQL for "
                        f"`{kpi_id or 'kpi'}` does not correspond to a proven "
                        f"relationship (state must be proven_data_model, "
                        f"user_confirmed, or approved_for_execution)"
                    ),
                )
            )
    return findings


# ---------------------------------------------------------------------------
# FILTER-REALIZATION (prose) check
# ---------------------------------------------------------------------------

def declared_prose_filters(kpi: dict[str, Any]) -> list[tuple[str, str]]:
    """Extract high-confidence prose filter declarations from the KPI name.

    Returns a list of ``(filter_type, value)`` tuples.  Two conservative
    patterns are recognized:

    * ``"lob_value"`` — "for <CamelOrAllCaps> <lob-suffix-word>" in the name.
      The captured value is the first (capitalized) token; the suffix is
      discarded.
    * ``"age_threshold"`` — "above/over N year[s]" in the name.  The captured
      value is the integer string ``str(N)``.

    Deliberately conservative to avoid false positives on descriptive titles.
    Only flags when confident the filter was *declared*, not merely descriptive.
    """
    name = str(kpi.get("name") or kpi.get("description") or "")
    out: list[tuple[str, str]] = []
    seen: set[str] = set()

    for match in _PROSE_LOB_RE.finditer(name):
        value = match.group(1)
        # Reject plain common adjectives / prepositions that start with uppercase
        # only because they follow a word boundary (e.g. "for All Patients").
        # Accept only values that look like an enum / proper noun: all-caps or
        # CamelCase (mixed case with at least one uppercase after position 0).
        if len(value) < 2:
            continue
        # All-caps (like "MEDICARE", "LOB") or proper CamelCase — both are fine.
        key = ("lob_value", value.lower())
        if key not in seen:
            seen.add(key)
            out.append(("lob_value", value))

    for match in _PROSE_AGE_THRESHOLD_RE.finditer(name):
        threshold = match.group(1)
        key = ("age_threshold", threshold)
        if key not in seen:
            seen.add(key)
            out.append(("age_threshold", threshold))

    return out


def _age_threshold_present(value: str, sql_lower: str) -> bool:
    """True when the age-threshold integer appears in a genuine numeric/
    comparison context in the SQL, not merely as a bare substring anywhere in
    the text -- a raw `value in sql_lower` check spuriously matches a `LIMIT
    5`/`TOP 5`, a year (`2025`), or any other unrelated occurrence of the same
    digits. Accepts the shapes the docstring below promises: the bare integer
    next to a comparison operator, or inside a DATEDIFF/date_diff call.
    """
    n = re.escape(value)
    try:
        n_plus_1 = re.escape(str(int(value) + 1))
    except ValueError:
        n_plus_1 = n
    pattern = (
        rf"[<>]=?\s*{n}\b"                 # "> N" / ">= N" / "< N" (operator-first)
        rf"|\b{n}\s*[<>]="                  # "N <=" (operand-first)
        rf"|<\s*{n_plus_1}\b"               # "< N+1" (exclusive upper bound)
        rf"|date_?diff[\s\S]{{0,80}}?\b{n}\b"  # DATEDIFF(...) ... N
    )
    return re.search(pattern, sql_lower) is not None


def prose_filter_findings(kpi: dict[str, Any], sql: str) -> list[CoverageFinding]:
    """Check that prose-declared filters in the KPI name appear in the SQL.

    Scoped to the *full* generated SQL (both feature and result views), not
    only the result view, because a WHERE clause in a CTE/feature view is a
    valid implementation of a declared filter restriction.

    Returns error-severity findings for each missing filter.
    """
    kpi_id = str(kpi.get("kpi_id") or "")
    sql_lower = sql.lower()
    findings: list[CoverageFinding] = []

    for filter_type, value in declared_prose_filters(kpi):
        if filter_type == "lob_value":
            # The generated SQL may quote or alias the value; check for the
            # bare lowercase string anywhere in the SQL.
            if value.lower() in sql_lower:
                continue
            findings.append(
                CoverageFinding(
                    severity="error",
                    code="filter_not_realized",
                    kind="filter",
                    expected=value,
                    message=(
                        f"KPI name declares a filter for `{value}` but the "
                        f"value is absent from the generated SQL for "
                        f"`{kpi_id or 'kpi'}`"
                    ),
                )
            )
        elif filter_type == "age_threshold":
            # Accept any of: the bare integer, "> N", ">= N", "< N+1", or
            # "DATEDIFF/date_diff ... N" patterns in the SQL.
            if _age_threshold_present(value, sql_lower):
                continue
            findings.append(
                CoverageFinding(
                    severity="error",
                    code="filter_not_realized",
                    kind="filter",
                    expected=f"age threshold {value}",
                    message=(
                        f"KPI name declares an age threshold of {value} "
                        f"but the value is absent from the generated SQL for "
                        f"`{kpi_id or 'kpi'}`"
                    ),
                )
            )
    return findings


__all__ = [
    "CoverageFinding",
    "declared_explicit_filters",
    "declared_grain",
    "declared_metric_aggs",
    "declared_prose_filters",
    "denominator_scope_findings",
    "evaluate_intent_coverage",
    "grain_coverage_findings",
    "join_correctness_findings",
    "output_shape_findings",
    "prose_filter_findings",
    "result_view_block",
    "temporal_anchor_findings",
    "_extract_join_pairs",
    "_is_within_group_scope",
    "_load_proven_join_pairs",
]
