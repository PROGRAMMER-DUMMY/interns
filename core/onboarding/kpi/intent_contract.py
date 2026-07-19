"""KPI Intent Contract builder.

Produces an explicit, per-facet contract for every KPI in the registry.
This is Phase 3 of the intent-contract design (docs/design/kpi_intent_contract.md).

The canonical facets (Section 3 of the design doc) are:
  metric, grain, filters, denominator_scope, grain_bucketing, temporal_anchor,
  output_shape, null_zero_handling

Each facet record carries:
  { facet, value, confidence: high|medium|low|none,
    source: human|default|derived, evidence: [...], alternatives: [...] }

Design constraints (same independence rationale as intent_coverage.py):
  - Facet extraction is INDEPENDENT of result_view_builder.parse_kpi.
    A generator that drops a facet must not pass a check that reused its
    own parser.
  - Feature source_columns are reused (data, not logic) for column
    resolution.
  - All rules are deterministic; no LLM calls.
  - Workspace-agnostic: zero domain vocabulary baked in.

Confidence rules (documented here, referenced in tests):
  metric:
    high  — an agg fn + non-empty column parse cleanly from `metric`
            (COUNT(*) is also high)
    low   — metric text is non-empty but no agg fn could be parsed
    none  — metric field is empty

  grain:
    high  — every declared cut token resolves to an emitted/known column
            (or is a recognized time-bucket / age / days-since token)
    medium — at least one cut resolves; at least one does NOT resolve to
             an emitted column (the cut is present but the column is
             unknown)
    low   — cuts text is non-empty but no token resolves to an emitted
             column
    none  — no cuts declared (scalar KPI; grain is vacuously not required)

  filters:
    high  — at least one quoted literal filter detected in cuts
    medium — at least one prose-filter pattern detected in name (less
             certain than a literal)
    low   — name or cuts contain filter-like prose but pattern is weak
    none  — no filter tokens detected (fine for many KPIs)

  denominator_scope:
    low   — metric is a "<agg> / <same agg> for <group>" share (or
            percent/share pattern) AND no explicit scope is recorded
            in pipeline_decisions.json → genuinely ambiguous
    high  — scope is explicitly recorded in pipeline_decisions.json
    none  — KPI is NOT a percentage-share metric (facet not applicable)

  grain_bucketing:
    none  — not a share metric, OR no raw continuous cut (facet not applicable)
    high  — a bucketing decision is recorded in pipeline_decisions.json
    low   — share metric cut by a raw continuous dimension (exact age /
            days-since) AND no bucketing decision recorded -> genuinely
            ambiguous (band into ranges vs keep exact-value grain)

  temporal_anchor:
    high  — an explicit time-grain source column exists in cuts
            (e.g. Month(ServiceDate)) AND no age/days-since arithmetic
            is present (anchor is unambiguous)
    medium — age/days-since arithmetic is present AND an event-date
             grain column also exists (anchor is determinable but
             requires the event-date column to be resolved)
    low   — age/days-since arithmetic exists but NO event-date grain
             column is found (anchor is ambiguous: event vs today)
    none  — no date/age arithmetic at all (facet not applicable)

  output_shape:
    high  — a clear top-N pattern ("top N") appears in the name
    high  — a share/percent pattern is present in metric (share)
    high  — a trend pattern (time-bucket cut) with no top-N (trend/flat)
    medium — none of the above, but cuts and metric are present
    low   — no metric AND no cuts (cannot determine shape)

  null_zero_handling:
    medium — always; this facet is never explicitly modeled in the
             current registry schema (NULLIF / coalesce are not
             declared). Recorded as source=default so the integrator
             can surface it in gate-provenance.

Standalone module + CLI. NOT wired into flow.py or the blocker panel.
That orchestration is a deferred follow-up for the integrator.
"""
from __future__ import annotations
from core.observability.cost_ledger import anchored

import argparse
import json
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


# ---------------------------------------------------------------------------
# Internal regex patterns (independent of result_view_builder)
# ---------------------------------------------------------------------------

_AGG_FN_RE = re.compile(
    r"\b(sum|avg|count|min|max)\s*\(\s*(distinct\s+)?([^()]+?)\s*\)",
    re.IGNORECASE,
)
_COUNT_STAR_RE = re.compile(r"\bcount\s*\(\s*\*\s*\)", re.IGNORECASE)

# Share / percent patterns
_SHARE_PATTERN_RE = re.compile(
    r"\b(?:percent(?:age)?|share)\b", re.IGNORECASE,
)
_MISMATCHED_GRAIN_RE = re.compile(
    r"\b(?:percent(?:age)?|share)\s+of\s+\w+.*?/.*?\bfor\b", re.IGNORECASE
)
_FOR_GROUP_RE = re.compile(
    r"\bpercentage\b.*/.*\bfor\s+([A-Za-z_][A-Za-z0-9_]*)\b", re.IGNORECASE
)
# Simpler: the design doc signature for mismatched-grain percentage is
# "percentage of <agg> / <agg> for <group>"
_MISMATCH_GRAIN_SIGNATURE_RE = re.compile(
    r"\bpercentage\b.*?/.*?\bfor\s+\w+", re.IGNORECASE
)

# Time-grain source column pattern: Month(col), Year(col), etc.
_TIME_GRAIN_SOURCE_RE = re.compile(
    r"\b(?:year|quarter|month|week|day)\s*\(\s*([A-Za-z_][A-Za-z0-9_]*)\s*\)",
    re.IGNORECASE,
)
# Any time-bucket mention (with or without source column)
_TIME_BUCKET_RE = re.compile(
    r"\b(?:year|quarter|month|week|day)\b", re.IGNORECASE,
)

# Age / days-since arithmetic
_AGE_RE = re.compile(
    r"\bage\s*\(\s*([A-Za-z_][A-Za-z0-9_]*)\s*\)"
    r"|\bage\s+(?:from|of)\s+([A-Za-z_][A-Za-z0-9_]*)"
    r"|\bage\b",
    re.IGNORECASE,
)
_DAYS_SINCE_RE = re.compile(r"\bdays?\s+since\b", re.IGNORECASE)
# A RAW continuous cut yields an exact integer grain (exact age, exact day count).
# These same generic regexes drive the grain-bucketing facet: grouping a share by
# such a value fragments the denominator. A comparison token (age > 50) is a
# filter, not a grouping dimension, and is handled separately.
_RAW_CONTINUOUS_CUT_RE = re.compile(
    r"\bage\b|\bdays?\s+since\b", re.IGNORECASE,
)
_COMPARISON_OP_RE = re.compile(r"[<>]=?|!?=")

# Top-N ranking in name
_TOP_N_RE = re.compile(r"\btop\s+(\d+)\b", re.IGNORECASE)

# Quoted literal filter in cuts
_QUOTED_FILTER_RE = re.compile(r"['\"]([^'\"]{1,80})['\"]")

# Prose filter: "for <Capitalized> <lob-suffix>"
_LOB_SUFFIX_WORDS = frozenset({
    "lob", "line", "plan", "program", "type", "segment",
    "category", "class", "group", "channel", "tier",
})
_PROSE_LOB_RE = re.compile(
    r"\bfor\s+([A-Z][A-Za-z0-9_-]*)\s+(?:" + "|".join(_LOB_SUFFIX_WORDS) + r")\b",
    re.IGNORECASE,
)

# Comparison operator in cut token → it's a filter, not a grain dimension
_COMPARISON_RE = re.compile(r"[<>]=?|!?=")

# Cut splitter (mirrors intent_coverage._split_cuts)
_CUT_SPLIT_RE = re.compile(r"[,;]| and ", re.IGNORECASE)


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _split_cuts(cuts: str) -> list[str]:
    return [p.strip() for p in _CUT_SPLIT_RE.split(cuts) if p.strip()]


def _norm(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "", str(text).lower())


def _feature_column_lookup(kpi: dict[str, Any]) -> dict[str, str]:
    """Map feature label (lowercased) -> first source column.

    Mirrors the same data extraction used by both result_view_builder and
    intent_coverage — this is data, not logic.
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
            if isinstance(source, dict) and source.get("column"):
                resolved = str(source["column"])
                break
        out[label.lower()] = resolved
    return out


def _emitted_columns(lookup: dict[str, str]) -> set[str]:
    return {col for col in lookup.values() if col}


def _resolve_cut_column(token: str, lookup: dict[str, str]) -> str:
    """Resolve a cut token to an underlying column: exact, normalized, word-subset."""
    key = token.lower().strip()
    if key in lookup:
        return lookup[key]
    norm_k = _norm(key)
    for label, column in lookup.items():
        if _norm(label) == norm_k:
            return column
    cut_words = set(re.findall(r"[a-z0-9]+", key))
    best: tuple[int, str] | None = None
    for label, column in lookup.items():
        label_words = set(re.findall(r"[a-z0-9]+", label))
        if label_words and label_words <= cut_words:
            if best is None or len(label_words) > best[0]:
                best = (len(label_words), column)
    return best[1] if best else token


def _load_pipeline_decisions(workspace_root: Path) -> dict[str, Any]:
    """Load pipeline_decisions.json if present, else return empty dict."""
    candidates = [
        workspace_root / "interns" / "state" / "pipeline_decisions.json",
        workspace_root / "interns" / "generated" / "contracts" / "pipeline_decisions.json",
        workspace_root / "pipeline_decisions.json",
    ]
    for path in candidates:
        if path.exists():
            try:
                return json.loads(path.read_text(encoding="utf-8"))
            except (json.JSONDecodeError, OSError):
                pass
    return {}


def _denominator_scope_from_decisions(
    kpi_id: str, decisions: dict[str, Any]
) -> str | None:
    """Look up recorded denominator-scope decision for kpi_id.

    Returns the value string if found, else None.
    """
    # Common schema: decisions[kpi_id]["percentage_denominator_scopes"] or
    # decisions["percentage_denominator_scopes"][kpi_id]
    by_kpi = decisions.get(kpi_id) or {}
    if isinstance(by_kpi, dict):
        scope = by_kpi.get("percentage_denominator_scope") or by_kpi.get("denominator_scope")
        if scope:
            return str(scope)
    top_level = decisions.get("percentage_denominator_scopes") or {}
    if isinstance(top_level, dict):
        scope = top_level.get(kpi_id)
        if scope:
            return str(scope)
    return None


def _grain_bucketing_from_decisions(
    kpi_id: str, decisions: dict[str, Any]
) -> str | None:
    """Look up a recorded grain-bucketing decision for kpi_id. None if absent."""
    by_kpi = decisions.get(kpi_id) or {}
    if isinstance(by_kpi, dict):
        decision = by_kpi.get("grain_bucketing")
        if decision:
            return str(decision)
    top_level = decisions.get("grain_bucketing_decisions") or {}
    if isinstance(top_level, dict):
        decision = top_level.get(kpi_id)
        if decision:
            return str(decision)
    return None


def _raw_continuous_cuts(cuts_text: str) -> list[str]:
    """Cut tokens that GROUP BY a raw exact continuous value (age / days-since).

    Comparison tokens (``age > 50``) are filters, not grouping dimensions, and
    are excluded. Generic: age/days-since are temporal concepts, not domain
    vocabulary.
    """
    out: list[str] = []
    for token in _split_cuts(cuts_text):
        if _COMPARISON_OP_RE.search(token):
            continue
        if _RAW_CONTINUOUS_CUT_RE.search(token):
            out.append(token)
    return out


# ---------------------------------------------------------------------------
# Per-facet extractors
# ---------------------------------------------------------------------------

def _facet_metric(kpi: dict[str, Any]) -> dict[str, Any]:
    """Extract the metric facet.

    Confidence:
      high — COUNT(*) or <agg_fn>(<column>) parsed cleanly
      low  — metric text non-empty but no agg fn found
      none — metric field is empty
    """
    metric_text = str(kpi.get("metric") or "").strip()
    lookup = _feature_column_lookup(kpi)

    if not metric_text:
        return {
            "facet": "metric",
            "value": None,
            "confidence": "none",
            "source": "default",
            "evidence": ["metric field is empty"],
            "alternatives": [],
        }

    # COUNT(*)
    if _COUNT_STAR_RE.search(metric_text):
        return {
            "facet": "metric",
            "value": "count(*)",
            "confidence": "high",
            "source": "derived",
            "evidence": [f"metric text: {metric_text!r}"],
            "alternatives": [],
        }

    # Try to parse agg fn + column
    match = _AGG_FN_RE.search(metric_text)
    if match:
        fn = match.group(1).lower()
        distinct = bool(match.group(2))
        inner = match.group(3).strip()
        # strip predicate if any
        if "=" in inner:
            inner = inner.split("=", 1)[0].strip()
        col = _resolve_cut_column(inner, lookup) if lookup else inner
        value = f"{'count_distinct' if (fn == 'count' and distinct) else fn}({col})"
        return {
            "facet": "metric",
            "value": value,
            "confidence": "high",
            "source": "derived",
            "evidence": [f"metric text: {metric_text!r}", f"parsed: fn={fn}, column={col}"],
            "alternatives": [],
        }

    # Ratio with / — might be parseable partially
    if "/" in metric_text:
        halves = metric_text.split("/", 1)
        left_match = _AGG_FN_RE.search(halves[0])
        right_match = _AGG_FN_RE.search(halves[1]) if len(halves) > 1 else None
        if left_match and right_match:
            l_fn = left_match.group(1).lower()
            r_fn = right_match.group(1).lower()
            l_col = _resolve_cut_column(left_match.group(3).strip(), lookup) if lookup else left_match.group(3).strip()
            r_col = _resolve_cut_column(right_match.group(3).strip(), lookup) if lookup else right_match.group(3).strip()
            return {
                "facet": "metric",
                "value": f"{l_fn}({l_col}) / {r_fn}({r_col})",
                "confidence": "high",
                "source": "derived",
                "evidence": [f"metric text: {metric_text!r}", "ratio pattern matched"],
                "alternatives": [],
            }

    # Metric text exists but could not be parsed
    return {
        "facet": "metric",
        "value": metric_text,
        "confidence": "low",
        "source": "derived",
        "evidence": [f"metric text: {metric_text!r}", "no recognizable agg fn found"],
        "alternatives": [],
    }


def _facet_grain(kpi: dict[str, Any]) -> dict[str, Any]:
    """Extract the grain facet.

    Confidence:
      none   — no cuts text
      high   — all declared cut tokens resolve to emitted/known columns
               (or are recognized time-bucket / age / days-since tokens)
      medium — at least one resolves, at least one does not resolve to
               an emitted column
      low    — cuts text non-empty but no token resolves to an emitted column
    """
    cuts_text = str(kpi.get("cuts") or "").strip()
    if not cuts_text:
        return {
            "facet": "grain",
            "value": [],
            "confidence": "none",
            "source": "default",
            "evidence": ["no cuts declared"],
            "alternatives": [],
        }

    lookup = _feature_column_lookup(kpi)
    emitted = _emitted_columns(lookup)
    tokens = _split_cuts(cuts_text)

    resolved: list[str] = []
    unresolved: list[str] = []
    all_cuts: list[str] = []

    for token in tokens:
        # Comparison ops → filter, skip from grain
        if _COMPARISON_RE.search(token):
            continue
        low = token.lower()

        # Time-bucket token (year/month/etc.)
        if _TIME_BUCKET_RE.search(low):
            all_cuts.append(token)
            resolved.append(token)
            continue

        # Age / days-since
        if _AGE_RE.search(low) or _DAYS_SINCE_RE.search(low):
            all_cuts.append(token)
            resolved.append(token)
            continue

        # Dimensional cut: strip parenthetical, resolve column
        clean = re.sub(r"\(.*?\)", "", token).strip()
        if not clean:
            continue
        col = _resolve_cut_column(clean, lookup)
        all_cuts.append(token)
        if col in emitted or col == clean:
            # col == clean means lookup had no entry but at least the token
            # itself is valid; we consider it resolved if there is no lookup
            # at all (zero features) — the verifier catches real mismatches
            if emitted or not lookup:
                if col in emitted:
                    resolved.append(token)
                else:
                    unresolved.append(token)
            else:
                unresolved.append(token)
        else:
            unresolved.append(token)

    if not all_cuts:
        return {
            "facet": "grain",
            "value": [],
            "confidence": "none",
            "source": "default",
            "evidence": ["cuts text present but contained only filter tokens"],
            "alternatives": [],
        }

    value = all_cuts
    evidence: list[str] = [f"cuts text: {cuts_text!r}"]
    if resolved:
        evidence.append(f"resolved cuts: {resolved}")
    if unresolved:
        evidence.append(f"unresolved cuts (column not in emitted set): {unresolved}")

    if not unresolved:
        confidence = "high"
    elif resolved:
        confidence = "medium"
    else:
        confidence = "low"

    return {
        "facet": "grain",
        "value": value,
        "confidence": confidence,
        "source": "derived",
        "evidence": evidence,
        "alternatives": [],
    }


def _facet_filters(kpi: dict[str, Any]) -> dict[str, Any]:
    """Extract the filters facet.

    Confidence:
      high   — at least one quoted literal in cuts
      medium — prose filter detected in name
      none   — no filters detected
    """
    cuts_text = str(kpi.get("cuts") or "").strip()
    name_text = str(kpi.get("name") or kpi.get("description") or "").strip()

    literal_filters: list[str] = []
    for match in _QUOTED_FILTER_RE.finditer(cuts_text):
        val = match.group(1).strip()
        if val and len(val.split()) <= 2 and len(val) <= 40:
            literal_filters.append(val)

    prose_filters: list[str] = []
    for match in _PROSE_LOB_RE.finditer(name_text):
        val = match.group(1)
        if val:
            prose_filters.append(val)

    # Comparison-operator tokens in cuts are also filter evidence
    comparison_filters: list[str] = []
    for token in _split_cuts(cuts_text):
        if _COMPARISON_RE.search(token):
            comparison_filters.append(token)

    all_filters = literal_filters + comparison_filters + prose_filters
    evidence: list[str] = []
    if literal_filters:
        evidence.append(f"literal filters in cuts: {literal_filters}")
    if comparison_filters:
        evidence.append(f"comparison filters in cuts: {comparison_filters}")
    if prose_filters:
        evidence.append(f"prose filters in name: {prose_filters}")

    if literal_filters or comparison_filters:
        confidence = "high"
        source = "derived"
    elif prose_filters:
        confidence = "medium"
        source = "derived"
    else:
        confidence = "none"
        source = "default"
        evidence = ["no filter tokens detected in cuts or name"]

    return {
        "facet": "filters",
        "value": all_filters if all_filters else [],
        "confidence": confidence,
        "source": source,
        "evidence": evidence,
        "alternatives": [],
    }


def _facet_denominator_scope(
    kpi: dict[str, Any],
    decisions: dict[str, Any],
) -> dict[str, Any]:
    """Extract the denominator_scope facet.

    Confidence:
      none   — KPI is not a percentage-share metric (facet not applicable)
      high   — an explicit denominator scope is recorded in pipeline_decisions
      low    — KPI IS a share/percentage metric but no scope is recorded
               (genuinely ambiguous: within-group vs population-total)
    """
    metric_text = str(kpi.get("metric") or "").strip()
    kpi_id = str(kpi.get("kpi_id") or "")

    # Detect whether this is a percentage / share KPI
    is_share = False
    for_group_token: str = ""

    # Most specific: "percentage ... / ... for <group>"
    mismatch = _MISMATCH_GRAIN_SIGNATURE_RE.search(metric_text)
    if mismatch:
        is_share = True
        fg = _FOR_GROUP_RE.search(metric_text)
        if fg:
            for_group_token = fg.group(1)
    elif _SHARE_PATTERN_RE.search(metric_text):
        is_share = True

    if not is_share:
        return {
            "facet": "denominator_scope",
            "value": None,
            "confidence": "none",
            "source": "default",
            "evidence": ["KPI metric is not a percentage-share; facet not applicable"],
            "alternatives": [],
        }

    # It IS a share — check pipeline_decisions for a recorded scope
    recorded_scope = _denominator_scope_from_decisions(kpi_id, decisions)
    if recorded_scope:
        alt = "grand_total" if recorded_scope not in {"grand_total", "global_total"} else (
            f"within_{for_group_token}" if for_group_token else "within_<group>"
        )
        return {
            "facet": "denominator_scope",
            "value": recorded_scope,
            "confidence": "high",
            "source": "human",
            "evidence": [
                f"metric text: {metric_text!r}",
                f"pipeline_decisions recorded scope: {recorded_scope!r}",
            ],
            "alternatives": [alt],
        }

    # Share but no scope recorded → genuinely ambiguous
    alt_within = f"within_{for_group_token}" if for_group_token else "within_<group>"
    return {
        "facet": "denominator_scope",
        "value": "grand_total",  # current implicit default
        "confidence": "low",
        "source": "default",
        "evidence": [
            f"metric text: {metric_text!r}",
            "share/percentage pattern detected but no denominator scope recorded",
            "default is grand_total (OVER ()) but within-group may be intended",
        ],
        "alternatives": [alt_within],
    }


def _facet_grain_bucketing(
    kpi: dict[str, Any],
    decisions: dict[str, Any],
) -> dict[str, Any]:
    """Extract the grain_bucketing facet.

    A share/percentage metric cut by a RAW continuous dimension (exact age /
    days-since) explodes the result into one tiny row per value, making the
    share denominator meaningless. This facet forces a banding decision.

    Confidence:
      none   — not a share metric, OR no raw continuous cut (facet not applicable)
      high   — a bucketing decision is recorded in pipeline_decisions
      low    — share metric + raw continuous cut + NO decision recorded
               (genuinely ambiguous: band into ranges vs keep exact-value grain)
    """
    metric_text = str(kpi.get("metric") or "").strip()
    cuts_text = str(kpi.get("cuts") or "").strip()
    kpi_id = str(kpi.get("kpi_id") or "")

    is_share = bool(_SHARE_PATTERN_RE.search(metric_text))
    raw_cuts = _raw_continuous_cuts(cuts_text)

    if not is_share or not raw_cuts:
        return {
            "facet": "grain_bucketing",
            "value": None,
            "confidence": "none",
            "source": "default",
            "evidence": [
                "not a share metric cut by a raw continuous dimension; "
                "facet not applicable"
            ],
            "alternatives": [],
        }

    recorded = _grain_bucketing_from_decisions(kpi_id, decisions)
    if recorded:
        return {
            "facet": "grain_bucketing",
            "value": recorded,
            "confidence": "high",
            "source": "human",
            "evidence": [
                f"metric text: {metric_text!r}",
                f"raw continuous cuts: {raw_cuts}",
                f"pipeline_decisions recorded bucketing: {recorded!r}",
            ],
            "alternatives": ["exact_value_grain"],
        }

    # Share + raw continuous cut + no decision → hard block (low confidence).
    return {
        "facet": "grain_bucketing",
        "value": "band_continuous_cuts",  # recommended default
        "confidence": "low",
        "source": "default",
        "evidence": [
            f"metric text: {metric_text!r}",
            f"raw continuous cuts: {raw_cuts}",
            "grouping a share by an exact continuous value fragments the "
            "denominator into one tiny row per value; band into ranges instead",
        ],
        "alternatives": ["exact_value_grain"],
    }


def _facet_temporal_anchor(kpi: dict[str, Any]) -> dict[str, Any]:
    """Extract the temporal_anchor facet.

    Confidence:
      none   — no date/age arithmetic at all (facet not applicable)
      high   — time-grain source column exists in cuts AND no age/days-since
               (anchor is the event-date column; unambiguous)
      medium — age/days-since exists AND an event-date grain column exists
               (anchor is determinable but requires event-date resolution)
      low    — age/days-since exists but NO event-date grain column found
               (ambiguous: event date vs CURRENT_DATE)
    """
    cuts_text = str(kpi.get("cuts") or "").strip()

    has_age = bool(_AGE_RE.search(cuts_text) or _DAYS_SINCE_RE.search(cuts_text))
    event_date_match = _TIME_GRAIN_SOURCE_RE.search(cuts_text)
    has_event_date_grain = bool(event_date_match)
    event_col = event_date_match.group(1) if event_date_match else ""

    # Also check for plain time-bucket tokens without an explicit source column
    has_time_bucket = bool(_TIME_BUCKET_RE.search(cuts_text))

    if not has_age and not has_time_bucket and not has_event_date_grain:
        return {
            "facet": "temporal_anchor",
            "value": None,
            "confidence": "none",
            "source": "default",
            "evidence": ["no date/age arithmetic or time-bucket in cuts"],
            "alternatives": [],
        }

    if not has_age:
        # Only time-bucket, no age arithmetic — anchor is clear
        anchor_val = f"event_date:{event_col}" if event_col else "time_bucket"
        return {
            "facet": "temporal_anchor",
            "value": anchor_val,
            "confidence": "high",
            "source": "derived",
            "evidence": [
                f"cuts text: {cuts_text!r}",
                f"time-grain source column: {event_col!r}" if event_col else "time-bucket token present",
            ],
            "alternatives": [],
        }

    # has_age is True
    if has_event_date_grain:
        return {
            "facet": "temporal_anchor",
            "value": f"event_date:{event_col}",
            "confidence": "medium",
            "source": "derived",
            "evidence": [
                f"cuts text: {cuts_text!r}",
                "age/date arithmetic detected",
                f"event-date grain column found: {event_col!r}",
                "age should be computed as-of the event date, not CURRENT_DATE (BUG-005 pattern)",
            ],
            "alternatives": ["current_date"],
        }

    # has_age but NO event-date grain column
    return {
        "facet": "temporal_anchor",
        "value": "current_date",  # the implicit dangerous default
        "confidence": "low",
        "source": "default",
        "evidence": [
            f"cuts text: {cuts_text!r}",
            "age/days-since arithmetic detected but no event-date grain column found",
            "age arithmetic will default to CURRENT_DATE (BUG-005 risk)",
        ],
        "alternatives": ["event_date:<unknown>"],
    }


def _facet_output_shape(kpi: dict[str, Any]) -> dict[str, Any]:
    """Extract the output_shape facet.

    Confidence:
      high   — top-N in name, OR share in metric, OR time-bucket (trend)
      medium — cuts and metric are present but no clear shape signal
      low    — no metric and no cuts
    """
    name_text = str(kpi.get("name") or kpi.get("description") or "").strip()
    metric_text = str(kpi.get("metric") or "").strip()
    cuts_text = str(kpi.get("cuts") or "").strip()

    top_n_match = _TOP_N_RE.search(name_text)
    if top_n_match:
        n = top_n_match.group(1)
        return {
            "facet": "output_shape",
            "value": f"ranking(top_{n})",
            "confidence": "high",
            "source": "derived",
            "evidence": [f"name contains top-{n} pattern: {name_text!r}"],
            "alternatives": ["flat"],
        }

    # Share/percentage in metric
    if _SHARE_PATTERN_RE.search(metric_text):
        return {
            "facet": "output_shape",
            "value": "share",
            "confidence": "high",
            "source": "derived",
            "evidence": [f"metric contains share/percentage pattern: {metric_text!r}"],
            "alternatives": ["flat"],
        }

    # Trend: time-bucket cut present
    if _TIME_BUCKET_RE.search(cuts_text):
        return {
            "facet": "output_shape",
            "value": "trend",
            "confidence": "high",
            "source": "derived",
            "evidence": [f"time-bucket cut present in: {cuts_text!r}"],
            "alternatives": ["flat"],
        }

    # Cuts and metric present but no shape signal
    if metric_text and cuts_text:
        return {
            "facet": "output_shape",
            "value": "flat",
            "confidence": "medium",
            "source": "default",
            "evidence": [
                f"metric: {metric_text!r}",
                f"cuts: {cuts_text!r}",
                "no top-N, share, or time-bucket pattern detected; assuming flat aggregation",
            ],
            "alternatives": [],
        }

    # Nothing to go on
    return {
        "facet": "output_shape",
        "value": None,
        "confidence": "low",
        "source": "default",
        "evidence": ["no metric and no cuts — cannot determine output shape"],
        "alternatives": [],
    }


def _facet_null_zero_handling(kpi: dict[str, Any]) -> dict[str, Any]:
    """Extract the null_zero_handling facet.

    This facet is never explicitly modeled in the current registry schema
    (NULLIF / coalesce / zero-fill policies are not declared).  Always recorded
    as source=default, confidence=medium, so it appears in gate-provenance
    for the integrator to surface.
    """
    metric_text = str(kpi.get("metric") or "").strip()
    evidence: list[str] = ["null_zero_handling is not explicitly modeled in the KPI registry"]
    # Share KPIs have a concrete divide-by-zero risk
    if _SHARE_PATTERN_RE.search(metric_text):
        evidence.append("share/percentage metric — divide-by-zero risk if denominator is 0")
    return {
        "facet": "null_zero_handling",
        "value": "unspecified",
        "confidence": "medium",
        "source": "default",
        "evidence": evidence,
        "alternatives": ["nullif_zero", "coalesce_zero", "drop_nulls"],
    }


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def build_intent_contract(
    kpi: dict[str, Any],
    decisions: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Build a per-facet intent contract for a single KPI registry entry.

    Parameters
    ----------
    kpi:
        A single KPI dict from kpi_registry.json (possibly merged with feature
        mapping so `features` is populated).
    decisions:
        Loaded pipeline_decisions.json dict (or empty dict). Used for
        denominator_scope resolution. Pass None to skip (treated as empty).

    Returns
    -------
    dict with keys:
      kpi_id, name, facets (list of facet dicts), overall_confidence,
      low_confidence_count, built_at
    """
    if decisions is None:
        decisions = {}

    facets = [
        _facet_metric(kpi),
        _facet_grain(kpi),
        _facet_filters(kpi),
        _facet_denominator_scope(kpi, decisions),
        _facet_grain_bucketing(kpi, decisions),
        _facet_temporal_anchor(kpi),
        _facet_output_shape(kpi),
        _facet_null_zero_handling(kpi),
    ]

    low_count = sum(
        1 for f in facets
        if f["confidence"] in {"low"}
        # "none" on optional facets is not a blocker; "none" on metric is
        # caught separately
        or (f["facet"] == "metric" and f["confidence"] == "none")
    )

    # Overall confidence: lowest non-none confidence across required facets
    # Required facets: metric, grain (unless scalar), output_shape
    required_confidences: list[str] = []
    for f in facets:
        if f["facet"] in {"metric", "grain", "output_shape"}:
            if f["confidence"] != "none":
                required_confidences.append(f["confidence"])

    order = {"high": 0, "medium": 1, "low": 2, "none": 3}
    overall = (
        max(required_confidences, key=lambda c: order.get(c, 3))
        if required_confidences
        else "none"
    )

    return {
        "kpi_id": str(kpi.get("kpi_id") or ""),
        "name": str(kpi.get("name") or kpi.get("description") or ""),
        "facets": facets,
        "overall_confidence": overall,
        "low_confidence_count": low_count,
        "built_at": datetime.now(timezone.utc).isoformat(),
    }


def low_confidence_facets(contract: dict[str, Any]) -> list[dict[str, Any]]:
    """Return facets with confidence in {low} or metric=none that should become
    blocker questions.  For each, produce a panel-ready question dict naming
    the interpretations (e.g. denominator: within-group vs population-total).

    Returns the data only — does NOT wire into the blocker panel.
    """
    questions: list[dict[str, Any]] = []
    for facet in contract.get("facets") or []:
        confidence = facet.get("confidence", "high")
        fname = facet.get("facet", "")
        # Low confidence is always a blocker candidate
        is_low = confidence == "low"
        # metric=none is a hard blocker (cannot generate SQL without a metric)
        is_metric_none = fname == "metric" and confidence == "none"
        if not (is_low or is_metric_none):
            continue

        value = facet.get("value")
        alts = facet.get("alternatives") or []
        evidence = facet.get("evidence") or []

        # Build interpretations list
        interpretations: list[dict[str, Any]] = []
        if value is not None:
            interpretations.append({"label": str(value), "description": "current default / derived value"})
        for alt in alts:
            interpretations.append({"label": str(alt), "description": "alternative interpretation"})
        if not interpretations:
            interpretations.append({"label": "unknown", "description": "no interpretation available"})

        questions.append({
            "facet": fname,
            "kpi_id": contract.get("kpi_id", ""),
            "question": _panel_question_text(fname, value, alts, evidence),
            "confidence": confidence,
            "interpretations": interpretations,
            "evidence": evidence,
            "answer_type": "select_one",
        })

    return questions


def _panel_question_text(
    facet: str,
    value: Any,
    alternatives: list[str],
    evidence: list[str],
) -> str:
    """Produce a human-readable panel question for a low-confidence facet."""
    if facet == "denominator_scope":
        alts_str = " OR ".join(alternatives) if alternatives else "within_<group>"
        return (
            f"denominator_scope is ambiguous: should the denominator be "
            f"`{value}` (current default) or `{alts_str}` (within-group)? "
            f"Evidence: {'; '.join(evidence[:2])}"
        )
    if facet == "grain_bucketing":
        alts_str = " OR ".join(alternatives) if alternatives else "exact_value_grain"
        return (
            f"grain_bucketing: this share/percentage metric is cut by a raw "
            f"continuous dimension. Grouping a share by exact values fragments "
            f"the denominator into one tiny row per value. Band the cut into "
            f"ranges (`{value}`) or keep the exact-value grain (`{alts_str}`)? "
            f"Evidence: {'; '.join(evidence[:2])}"
        )
    if facet == "temporal_anchor":
        return (
            f"temporal_anchor: age/date arithmetic is present but no event-date "
            f"grain column was found. Should age be computed as-of "
            f"`event_date` (when the event occurred) or `current_date` "
            f"(as of today)? Evidence: {'; '.join(evidence[:2])}"
        )
    if facet == "metric":
        return (
            f"metric is missing or unrecognizable. "
            f"Please declare an aggregation function and measure column. "
            f"Evidence: {'; '.join(evidence[:2])}"
        )
    if facet == "grain":
        return (
            f"grain: one or more declared cuts could not be resolved to known columns. "
            f"Please confirm the cut-to-column mapping. "
            f"Evidence: {'; '.join(evidence[:2])}"
        )
    if facet == "output_shape":
        return (
            f"output_shape: cannot determine the output shape (no metric and no cuts). "
            f"Please declare whether this is flat/trend/ranking/share. "
            f"Evidence: {'; '.join(evidence[:2])}"
        )
    # Generic fallback
    return (
        f"{facet}: low confidence ({value!r}). "
        f"Alternatives: {alternatives}. Evidence: {'; '.join(evidence[:2])}"
    )


# ---------------------------------------------------------------------------
# Intent-answer store + blocker-panel routing (Phase 3 wiring)
# ---------------------------------------------------------------------------

INTENT_ANSWERS_FILENAME = "kpi_intent_answers.json"

# Facets that, when low-confidence (or metric=none), should become blocker
# questions in the panel. null_zero_handling is intentionally excluded: it is
# always medium/default and would otherwise fire on every KPI.
_ROUTED_FACETS = frozenset({
    "metric", "grain", "denominator_scope", "grain_bucketing",
    "temporal_anchor", "output_shape", "filters",
})


def _rel(path: Path, root: Path) -> str:
    try:
        return str(path.resolve().relative_to(root.resolve())).replace("\\", "/")
    except ValueError:
        return str(path)


def answer_key(kpi_id: str, facet: str) -> str:
    return f"{kpi_id}::{facet}"


def load_intent_answers(layout: Any) -> dict[str, dict[str, Any]]:
    """Return recorded intent-facet answers keyed by ``<kpi_id>::<facet>``.

    Reads ``interns/generated/contracts/kpi_intent_answers.json``. Missing or
    unreadable -> empty dict.
    """
    path = layout.contracts_dir / INTENT_ANSWERS_FILENAME
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return {}
    answers = data.get("answers") if isinstance(data, dict) else None
    return answers if isinstance(answers, dict) else {}


def record_intent_answer(
    repo_root: str | Path,
    workspace: str | Path,
    *,
    kpi_id: str,
    facet: str,
    value: str,
    source: str = "human",
    confirmed_by: str = "",
    reason: str = "",
) -> dict[str, Any]:
    """Persist a human/agent answer for a low-confidence intent facet.

    Writes to ``kpi_intent_answers.json``. For ``denominator_scope`` the answer
    is ALSO mirrored into ``pipeline_decisions.json`` (via PipelineDecisionRecorder)
    so the SQL generator + execution harness actually apply the chosen scope --
    otherwise the answer would be cosmetic.
    """
    from core.storage.workspace_layout import WorkspaceLayout

    root = Path(repo_root).resolve()
    workspace_path = (root / workspace).resolve()
    layout = WorkspaceLayout(project_root=workspace_path)
    path = layout.contracts_dir / INTENT_ANSWERS_FILENAME
    path.parent.mkdir(parents=True, exist_ok=True)

    data: dict[str, Any] = {}
    if path.exists():
        try:
            loaded = json.loads(path.read_text(encoding="utf-8"))
            if isinstance(loaded, dict):
                data = loaded
        except (json.JSONDecodeError, OSError):
            data = {}

    data.setdefault("artifact_type", INTENT_ANSWERS_FILENAME)
    data.setdefault("version", 1)
    data.setdefault("generated_by", "apply-kpi-panel-answer")
    data["workspace"] = _rel(workspace_path, root)
    answers: dict[str, Any] = data.setdefault("answers", {})
    record = {
        "kpi_id": kpi_id,
        "facet": facet,
        "value": value,
        "source": source,
        "confirmed_by": confirmed_by,
        "reason": reason,
    }
    answers[answer_key(kpi_id, facet)] = record
    path.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")

    if facet == "denominator_scope" and value:
        from core.onboarding.pipeline_plan import PipelineDecisionRecorder

        PipelineDecisionRecorder(root, _rel(workspace_path, root)).record_denominator_scope(
            kpi_id,
            str(value),
            reason=reason or f"intent-contract answer ({source})",
        )
    if facet == "grain_bucketing" and value:
        from core.onboarding.pipeline_plan import PipelineDecisionRecorder

        PipelineDecisionRecorder(root, _rel(workspace_path, root)).record_grain_bucketing(
            kpi_id,
            str(value),
            reason=reason or f"intent-contract answer ({source})",
        )
    if facet == "base_source" and value:
        from core.onboarding.pipeline_plan import PipelineDecisionRecorder

        PipelineDecisionRecorder(root, _rel(workspace_path, root)).record_base_source(
            kpi_id,
            str(value),
            reason=reason or f"intent-contract answer ({source})",
        )
    return record


def _intent_facet_to_panel_question(
    kpi_id: str,
    kpi_name: str,
    facet_q: dict[str, Any],
    workspace_rel: str,
) -> dict[str, Any]:
    """Map a low_confidence_facets() entry into a blocker-panel question dict.

    Conforms to the blocker-question-panel contract (validated by
    WorkspaceArtifactValidator when this question becomes the current panel):
    every option carries option_id/label/business_summary/json_backed, and the
    concrete options are json_backed=False (they carry a chosen value, not
    column-level derivation/physical evidence) so they pass the evidence gate and
    are appliable as the recommended option.
    """
    facet = str(facet_q.get("facet") or "")
    interpretations = facet_q.get("interpretations") or []
    evidence = facet_q.get("evidence") or []
    letters = "abcdefghij"
    options: list[dict[str, Any]] = []
    recommended = ""
    for idx, interp in enumerate(interpretations):
        oid = f"option_{letters[idx]}" if idx < len(letters) else f"option_{idx}"
        label = str(interp.get("label") or "")
        options.append({
            "option_id": oid,
            "label": label,
            "description": str(interp.get("description") or ""),
            "business_summary": (
                f"Resolve `{facet}` for {kpi_id} as `{label}` "
                f"({interp.get('description') or 'interpretation'})."
            ),
            "json_backed": False,
            "intent_facet": {"kpi_id": kpi_id, "facet": facet, "value": label},
        })
        if idx == 0:
            recommended = oid
    options.append({
        "option_id": "custom",
        "label": "Custom value / business rule",
        "business_summary": f"Provide an explicit value or business rule for `{facet}`.",
        "expected_answer_shape": f"explicit value for {facet}",
        "json_backed": False,
        "intent_facet": {"kpi_id": kpi_id, "facet": facet, "value": ""},
    })
    default_value = str((interpretations[0] if interpretations else {}).get("label") or "")
    return {
        "artifact_type": "blocker_question_panel/current.json",
        "version": 1,
        "generated_by": "blocker-question-panel",
        "routed_by": "intent-contract-routing",
        "workspace": workspace_rel,
        "question_id": f"intent_{kpi_id}_{facet}",
        "feature": f"intent::{kpi_id}::{facet}",
        "kpi_id": kpi_id,
        "kpi_name": kpi_name,
        "applies_to_kpis": [kpi_id] if kpi_id else [],
        "reuse_scope": "kpi_specific",
        "stage": "intent_facet_clarification",
        "status": "needs_user_answer",
        "blocker": (
            f"KPI `{kpi_id}` intent facet `{facet}` is "
            f"{facet_q.get('confidence')}-confidence — the interpretation is ambiguous."
        ),
        "question": str(facet_q.get("question") or ""),
        "answer_type": "intent_facet_clarification",
        "facet": facet,
        "confidence": str(facet_q.get("confidence") or ""),
        "evidence": evidence,
        "interpretations": interpretations,
        "interaction_contract": {
            "display_mode": "project_blocker_panel",
            "generic_answer_picker_allowed": False,
            "instruction": (
                "Show current.md verbatim or render options from current.json. "
                "Apply the answer with apply-kpi-panel-answer."
            ),
        },
        "recommended_option_id": recommended,
        "recommended_answer": default_value,
        "why": (
            "This facet was derived with low confidence; the generated SQL uses a "
            "default interpretation that may not match business intent. Confirm it."
        ),
        "options": options,
    }


def intent_facet_panel_questions(
    repo_root: str | Path,
    workspace: str | Path,
) -> list[dict[str, Any]]:
    """Build blocker-panel questions for every unanswered low-confidence facet.

    Reads kpi_registry.json (+ feature mapping), pipeline_decisions.json, and the
    kpi_intent_answers.json store. Facets already answered are skipped so the
    panel converges. Returns panel-shaped question dicts ready to append to the
    blocker panel's question list. Never raises on missing artifacts -> [].
    """
    from core.storage.workspace_layout import WorkspaceLayout

    root = Path(repo_root).resolve()
    workspace_path = (root / workspace).resolve()
    layout = WorkspaceLayout(project_root=workspace_path)

    registry = _load_registry_with_features(workspace_path)
    if not registry:
        return []
    decisions = _load_pipeline_decisions(workspace_path)
    answered = load_intent_answers(layout)
    workspace_rel = _rel(workspace_path, root)

    questions: list[dict[str, Any]] = []
    for kpi in registry:
        contract = build_intent_contract(kpi, decisions)
        kpi_id = str(contract.get("kpi_id") or "")
        kpi_name = str(contract.get("name") or "")
        for facet_q in low_confidence_facets(contract):
            facet = str(facet_q.get("facet") or "")
            if facet not in _ROUTED_FACETS:
                continue
            if answer_key(kpi_id, facet) in answered:
                continue
            questions.append(
                _intent_facet_to_panel_question(kpi_id, kpi_name, facet_q, workspace_rel)
            )
    return questions


# ---------------------------------------------------------------------------
# Markdown report renderer
# ---------------------------------------------------------------------------

_CONFIDENCE_MARKER = {"high": "[ok]", "medium": "[~]", "low": "[x]", "none": "[-]"}


def _render_markdown(contracts: list[dict[str, Any]]) -> str:
    lines: list[str] = [
        "# KPI Intent Contracts",
        "",
        f"Generated: {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')}",
        f"KPIs: {len(contracts)}",
        "",
    ]
    for contract in contracts:
        kpi_id = contract.get("kpi_id", "")
        name = contract.get("name", "")
        overall = contract.get("overall_confidence", "")
        low_count = contract.get("low_confidence_count", 0)
        marker = _CONFIDENCE_MARKER.get(overall, "[-]")
        lines += [
            f"## {kpi_id}: {name}",
            "",
            f"Overall confidence: {marker} **{overall}** | Low-confidence facets: {low_count}",
            "",
            "| Facet | Value | Confidence | Source | Evidence (summary) |",
            "| --- | --- | --- | --- | --- |",
        ]
        for facet in contract.get("facets") or []:
            fname = facet.get("facet", "")
            fval = str(facet.get("value") or "")
            fconf = facet.get("confidence", "")
            fsrc = facet.get("source", "")
            fevid = facet.get("evidence") or []
            evid_summary = fevid[0][:60] if fevid else ""
            conf_marker = _CONFIDENCE_MARKER.get(fconf, "[-]")
            lines.append(
                f"| {fname} | {fval[:40]} | {conf_marker} {fconf} | {fsrc} | {evid_summary} |"
            )
        lines += [""]

        # Low-confidence questions
        low_qs = low_confidence_facets(contract)
        if low_qs:
            lines += ["**Blocker questions (low-confidence facets):**", ""]
            for q in low_qs:
                lines.append(f"- [{q['facet']}] {q['question']}")
            lines += [""]

    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Registry loader + feature-map merger
# ---------------------------------------------------------------------------

def _load_registry_with_features(workspace_root: Path) -> list[dict[str, Any]]:
    """Load kpi_registry.json and, if available, merge feature_mapping features."""
    from core.storage.workspace_layout import WorkspaceLayout

    layout = WorkspaceLayout(project_root=workspace_root)
    reg_path = layout.kpi_registry_path
    if not reg_path.exists():
        return []
    try:
        loaded = json.loads(reg_path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return []

    # Accept both shapes: a bare list of KPI dicts (test fixtures / some
    # exporters) AND the onboarding shape {"kpis": [...]} (the real registry).
    if isinstance(loaded, dict):
        registry = loaded.get("kpis") or loaded.get("rows") or []
    elif isinstance(loaded, list):
        registry = loaded
    else:
        registry = []
    registry = [kpi for kpi in registry if isinstance(kpi, dict)]

    # Backfill the positional kpi_id that the rest of the system uses (execution
    # harness, feature resolver, evidence graph all key on
    # ``kpi.get("kpi_id") or f"kpi_{idx:03d}"``). The on-disk registry rows carry
    # no explicit kpi_id, so without this every intent-contract question/answer
    # keyed to "" -> question_id "intent__<facet>", feature "intent::::<facet>",
    # and decisions mirrored to pipeline_decisions[""] which the generator never
    # reads (follow_ups #1). Enumerate order matches the harness's scheme.
    for idx, kpi in enumerate(registry, start=1):
        if not str(kpi.get("kpi_id") or "").strip():
            kpi["kpi_id"] = f"kpi_{idx:03d}"

    # Try to merge feature mapping (source_columns resolution)
    feat_path = layout.kpi_feature_mapping_path
    if feat_path.exists():
        try:
            feature_map: dict[str, Any] = json.loads(feat_path.read_text(encoding="utf-8"))
            # Common schema: feature_map is a dict keyed by kpi_id or a list
            if isinstance(feature_map, dict):
                by_id: dict[str, Any] = {
                    k: v for k, v in feature_map.items()
                    if isinstance(v, dict) and "features" in v
                }
            else:
                by_id = {}

            for kpi in registry:
                kid = str(kpi.get("kpi_id") or "")
                if kid in by_id and not kpi.get("features"):
                    kpi["features"] = by_id[kid].get("features") or []
        except (json.JSONDecodeError, OSError):
            pass

    return registry


def write_intent_contract(
    repo_root: Path,
    workspace: str,
) -> dict[str, str]:
    """Build intent contracts for every KPI in kpi_registry.json and write
    the JSON + Markdown artifacts.

    Parameters
    ----------
    repo_root:
        Absolute path to the repository root.
    workspace:
        Workspace path relative to repo root (e.g. ``workspaces/MyProject``).

    Returns
    -------
    dict with keys: json_path, md_path, kpi_count
    """
    workspace_root = (repo_root / workspace).resolve()
    from core.storage.workspace_layout import WorkspaceLayout

    layout = WorkspaceLayout(project_root=workspace_root)

    registry = _load_registry_with_features(workspace_root)
    decisions = _load_pipeline_decisions(workspace_root)

    contracts: list[dict[str, Any]] = []
    for kpi in registry:
        contract = build_intent_contract(kpi, decisions)
        contracts.append(contract)

    # Artifact: interns/generated/contracts/kpi_intent_contract.json
    contracts_dir = layout.contracts_dir
    contracts_dir.mkdir(parents=True, exist_ok=True)
    json_path = contracts_dir / "kpi_intent_contract.json"
    json_path.write_text(
        json.dumps({"contracts": contracts, "kpi_count": len(contracts)}, indent=2),
        encoding="utf-8",
    )

    # Artifact: interns/reports/kpi_intent_contract.md
    reports_dir = layout.reports_dir
    reports_dir.mkdir(parents=True, exist_ok=True)
    md_path = reports_dir / "kpi_intent_contract.md"
    md_path.write_text(_render_markdown(contracts), encoding="utf-8")

    return {
        "json_path": str(json_path),
        "md_path": str(md_path),
        "kpi_count": str(len(contracts)),
    }


# ---------------------------------------------------------------------------
# CLI entry point
# ---------------------------------------------------------------------------


@anchored("build-intent-contract")
def main(argv: list[str] | None = None) -> None:
    """Console entry point: build-intent-contract --workspace <ws>"""
    parser = argparse.ArgumentParser(
        prog="build-intent-contract",
        description="Build per-facet KPI intent contracts for a workspace.",
    )
    parser.add_argument(
        "--workspace",
        required=True,
        help="Workspace path relative to repo root (e.g. workspaces/MyProject)",
    )
    parser.add_argument(
        "--repo-root",
        default=None,
        help="Absolute path to the repository root. Defaults to the directory "
             "containing this file's package root.",
    )
    args = parser.parse_args(argv)

    if args.repo_root:
        repo_root = Path(args.repo_root).resolve()
    else:
        # Walk up from this file to find the repo root (contains pyproject.toml)
        here = Path(__file__).resolve()
        candidate = here.parent
        while candidate != candidate.parent:
            if (candidate / "pyproject.toml").exists():
                repo_root = candidate
                break
            candidate = candidate.parent
        else:
            repo_root = Path.cwd()

    try:
        result = write_intent_contract(repo_root, args.workspace)
    except Exception as exc:
        print(f"[x] build-intent-contract failed: {exc}")
        raise SystemExit(1) from exc

    print("[ok] build-intent-contract complete")
    print(f"     KPIs processed : {result['kpi_count']}")
    print(f"     JSON artifact  : {result['json_path']}")
    print(f"     MD artifact    : {result['md_path']}")


__all__ = [
    "build_intent_contract",
    "low_confidence_facets",
    "write_intent_contract",
    "intent_facet_panel_questions",
    "record_intent_answer",
    "load_intent_answers",
    "answer_key",
    "main",
]
