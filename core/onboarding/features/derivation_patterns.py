"""Reusable derived-feature patterns the engine cannot express from a single
metric/cuts cell: a DERIVED DURATION bucket (start/stop -> threshold) and a
TEMPORAL SELF-JOIN recurrence (an event within N days of a prior event for the
same entity). These are exactly what the ``feature-derivation-library`` skill is
meant to own.

Each pattern emits a JSON-backed option in the same shape as
``core.onboarding.features.derived_evidence.derived_feature_options`` so the
blocker panel can render it and the user can confirm. Deterministic, evidence
-driven, no LLM, no domain vocabulary: detection rides on generic English
duration/recurrence phrasing plus profiled column evidence (sample-value date
shape, id/fk shape), never a baked keyword ladder.
"""
from __future__ import annotations

import re
from typing import Any

# ISO-ish date / datetime value, used to recognize a temporal column profiled as
# a plain string (the common CSV case). Same shape used by metric_derivation.
_DATE_VALUE_RE = re.compile(
    r"^\s*\d{4}[-/]\d{1,2}[-/]\d{1,2}([T ]\d{1,2}:\d{2}(:\d{2})?(\.\d+)?(Z|[+-]\d{2}:?\d{2})?)?\s*$"
)
_DATE_DTYPE_TOKENS = ("date", "time", "timestamp", "datetime")

# "over / under / more than / at least N hours" -> threshold + unit.
_DURATION_RE = re.compile(
    r"\b(?:over|under|more than|less than|greater than|at least|longer than|"
    r"shorter than|above|below)\s+(\d+)\s+(hour|day|week|minute)s?\b",
    re.IGNORECASE,
)
# "within N days of a previous / prior ..." -> window + unit.
_WINDOW_RE = re.compile(
    r"\bwithin\s+(\d+)\s+(hour|day|week|month)s?\b", re.IGNORECASE
)
_RECURRENCE_HINTS = ("previous", "prior", "again", "repeat", "readmit", "re-", "another", "subsequent")

_START_HINTS = ("start", "begin", "from", "open", "admit", "in")
_STOP_HINTS = ("stop", "end", "to", "close", "discharge", "out", "finish")


def _is_temporal(col: dict[str, Any]) -> bool:
    dtype = str(col.get("dtype") or "").lower()
    name = str(col.get("column") or "").lower()
    if any(tok in dtype for tok in _DATE_DTYPE_TOKENS) or any(tok in name for tok in _DATE_DTYPE_TOKENS):
        return True
    samples = [str(v).strip() for v in (col.get("sample_values") or []) if str(v).strip()]
    if not samples:
        return False
    return sum(1 for s in samples if _DATE_VALUE_RE.match(s)) / len(samples) >= 0.6


def _looks_like_id(col: dict[str, Any]) -> bool:
    name = str(col.get("column") or "").lower()
    return bool(re.search(r"(^|_)id$|(^|_)id_|_key$|^id$|uuid|guid", name)) or name.endswith("id")


def _name_has(col: dict[str, Any], hints: tuple[str, ...]) -> bool:
    name = str(col.get("column") or "").lower()
    return any(h in name for h in hints)


def _input_col(col: dict[str, Any], role: str) -> dict[str, Any]:
    return {
        "input_name": role,
        "column": col.get("column"),
        "dataset": col.get("dataset"),
        "dtype": col.get("dtype"),
        "profile_path": col.get("profile_path"),
        "evidence_state": "profile_inferred",
    }


def _quote(name: Any) -> str:
    return '"' + str(name or "") + '"'


def _option(
    *,
    name: str,
    pattern_id: str,
    business_meaning: str,
    formula: str,
    input_columns: list[dict[str, Any]],
    reasoning: str,
    confidence: str,
) -> dict[str, Any]:
    return {
        "derived_column_name": name,
        "source_pattern_id": pattern_id,
        "business_meaning": business_meaning,
        "formula": formula,
        "input_columns": input_columns,
        "example": {},
        "evidence_sources": [c.get("profile_path") for c in input_columns if c.get("profile_path")],
        "derivation_reasoning": {
            "summary": reasoning,
            "evidence_state": "candidate_derivation_not_ground_truth",
        },
        "evidence_state": "candidate_derivation_not_ground_truth",
        "confidence": confidence,
        "needs_user_confirmation": True,
    }


def _temporal_pair(columns: list[dict[str, Any]]) -> tuple[dict[str, Any], dict[str, Any]] | None:
    """A (start, stop) temporal column pair from the SAME dataset, generically."""
    by_dataset: dict[str, list[dict[str, Any]]] = {}
    for col in columns:
        if _is_temporal(col):
            by_dataset.setdefault(str(col.get("dataset") or ""), []).append(col)
    for temporal in by_dataset.values():
        if len(temporal) < 2:
            continue
        start = next((c for c in temporal if _name_has(c, _START_HINTS)), None)
        stop = next((c for c in temporal if _name_has(c, _STOP_HINTS)), None)
        if start and stop and start is not stop:
            return start, stop
        # Fall back to the first two temporal columns in spec order.
        return temporal[0], temporal[1]
    return None


def detect_duration_bucket(question: str, columns: list[dict[str, Any]]) -> dict[str, Any] | None:
    match = _DURATION_RE.search(question or "")
    if not match:
        return None
    n, unit = int(match.group(1)), match.group(2).lower()
    pair = _temporal_pair(columns)
    if not pair:
        return None
    start, stop = pair
    formula = (
        f"date_diff('{unit}', CAST({_quote(start.get('column'))} AS TIMESTAMP), "
        f"CAST({_quote(stop.get('column'))} AS TIMESTAMP)) >= {n}"
    )
    return _option(
        name=f"over_{n}_{unit}",
        pattern_id="duration_bucket",
        business_meaning=(
            f"Whether the elapsed time between {start.get('column')} and "
            f"{stop.get('column')} is at least {n} {unit}(s)."
        ),
        formula=formula,
        input_columns=[_input_col(start, "start"), _input_col(stop, "stop")],
        reasoning=(
            f"Question implies a {n}-{unit} duration threshold; "
            f"{start.get('column')}/{stop.get('column')} are the profiled "
            "start/stop timestamps of the same table."
        ),
        confidence="medium",
    )


def detect_recurrence_within_window(
    question: str, columns: list[dict[str, Any]]
) -> dict[str, Any] | None:
    match = _WINDOW_RE.search(question or "")
    low = (question or "").lower()
    if not match or not any(h in low for h in _RECURRENCE_HINTS):
        return None
    n, unit = int(match.group(1)), match.group(2).lower()
    entity = next((c for c in columns if _looks_like_id(c)), None)
    event = next((c for c in columns if _is_temporal(c)), None)
    if entity is None or event is None:
        return None
    ec, tc = _quote(entity.get("column")), _quote(event.get("column"))
    formula = (
        f"EXISTS (SELECT 1 FROM <self> p WHERE p.{ec} = {ec} "
        f"AND p.{tc} < {tc} "
        f"AND {tc} <= p.{tc} + INTERVAL {n} {unit.upper()})"
    )
    return _option(
        name=f"recurred_within_{n}_{unit}",
        pattern_id="recurrence_within_window",
        business_meaning=(
            f"Whether the same {entity.get('column')} has a prior event within "
            f"{n} {unit}(s) before this one (a self-join over the event table)."
        ),
        formula=formula,
        input_columns=[_input_col(entity, "entity"), _input_col(event, "event_time")],
        reasoning=(
            f"Question implies recurrence within {n} {unit}(s) of a prior event; "
            f"needs a self-join on {entity.get('column')} ordered by "
            f"{event.get('column')}. Candidate only -- confirm the window semantics."
        ),
        confidence="low",
    )


def detect_derivation_patterns(
    question: str, columns: list[dict[str, Any]]
) -> list[dict[str, Any]]:
    """Return any reusable derived-feature options that match the question +
    profiled columns. Deterministic and generic; empty list when none apply."""
    options: list[dict[str, Any]] = []
    for detector in (detect_duration_bucket, detect_recurrence_within_window):
        try:
            option = detector(question, columns)
        except Exception:  # pragma: no cover - detection is advisory, never fatal
            option = None
        if option:
            options.append(option)
    return options


__all__ = [
    "detect_derivation_patterns",
    "detect_duration_bucket",
    "detect_recurrence_within_window",
]
