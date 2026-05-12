"""
Classify artifact diffs into optimization patterns.

The classifier is intentionally rule-based. Enterprise learning needs stable,
auditable labels before it needs model-driven guesses.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field


@dataclass(frozen=True)
class ChangeClassification:
    primary_type: str
    change_types: list[str] = field(default_factory=list)
    confidence: str = "low"
    evidence: list[str] = field(default_factory=list)


_PATTERNS: list[tuple[str, re.Pattern[str], str]] = [
    (
        "predicate_pushdown",
        re.compile(r"^\+(?!\+\+).*\b(where|having|qualify)\b", re.IGNORECASE | re.MULTILINE),
        "Added filter predicate.",
    ),
    (
        "join_rewrite",
        re.compile(r"^[+-](?![+-]).*\b(join|on)\b", re.IGNORECASE | re.MULTILINE),
        "Changed join logic.",
    ),
    (
        "cte_rewrite",
        re.compile(r"^[+-](?![+-]).*\b(with|as\s*\(|create\s+(temp|temporary)?\s*table)\b", re.IGNORECASE | re.MULTILINE),
        "Changed CTE or materialization structure.",
    ),
    (
        "aggregation_rewrite",
        re.compile(r"^[+-](?![+-]).*\b(group\s+by|sum\(|avg\(|count\(|min\(|max\()\b", re.IGNORECASE | re.MULTILINE),
        "Changed aggregation logic.",
    ),
    (
        "case_simplification",
        re.compile(r"^[+-](?![+-]).*\b(case\s+when|coalesce\(|nullif\()\b", re.IGNORECASE | re.MULTILINE),
        "Changed conditional/null-handling logic.",
    ),
    (
        "column_pruning",
        re.compile(r"^[+-](?![+-]).*\b(select|exclude|drop\s+column)\b", re.IGNORECASE | re.MULTILINE),
        "Changed selected/projected columns.",
    ),
]


def classify_diff(diff_text: str, artifact_path: str = "") -> ChangeClassification:
    """Return stable optimization labels for a git diff."""
    if not diff_text.strip():
        return ChangeClassification(
            primary_type="no_code_change",
            change_types=["no_code_change"],
            confidence="high",
            evidence=["No diff was present for the artifact."],
        )

    labels: list[str] = []
    evidence: list[str] = []
    for label, pattern, reason in _PATTERNS:
        if pattern.search(diff_text):
            labels.append(label)
            evidence.append(reason)

    if not labels:
        labels = ["structural_edit"]
        evidence = ["Diff changed the artifact but did not match a known optimization pattern."]

    confidence = "high" if len(labels) == 1 else "medium"
    if artifact_path.endswith(".sql") and labels[0] != "structural_edit":
        confidence = "high"

    return ChangeClassification(
        primary_type=labels[0],
        change_types=labels,
        confidence=confidence,
        evidence=evidence,
    )


def expected_reason(change_type: str) -> str:
    """Return a concise hypothesis for why a change type should help."""
    reasons = {
        "predicate_pushdown": "Reduce rows earlier so downstream joins and aggregations process less data.",
        "join_rewrite": "Reduce intermediate join size or avoid inefficient join order/conditions.",
        "cte_rewrite": "Avoid repeated scans or materialize a costly intermediate once.",
        "aggregation_rewrite": "Reduce grouping cost or simplify aggregate computation.",
        "case_simplification": "Reduce per-row expression cost while preserving null and business rules.",
        "column_pruning": "Read and materialize fewer columns.",
        "structural_edit": "Improve the artifact through a change that needs empirical validation.",
        "no_code_change": "Establish or re-check the current baseline.",
    }
    return reasons.get(change_type, "Improve the score through an empirically measured change.")
