from __future__ import annotations

import re
from collections import Counter, defaultdict
from typing import Any


JOIN_KEY_SUFFIXES = ("id", "code")


def prioritize_blockers(
    mapping: dict[str, Any],
    *,
    structural_hints: set[str] | None = None,
) -> list[dict[str, Any]]:
    """Cluster blocked features by name and assign risk classes.

    `structural_hints` lets the caller pass workspace evidence (typically
    profiled table names) so domain words classify as `structural` without
    any hardcoded vocabulary.
    """
    counts: Counter[str] = Counter()
    examples: dict[str, list[str]] = defaultdict(list)
    for kpi in mapping.get("kpis", []):
        for feature in kpi.get("features", []):
            state = feature.get("state")
            if str(state).startswith("blocked") or state == "candidate_unconfirmed":
                name = str(feature.get("feature", ""))
                counts[name] += 1
                if len(examples[name]) < 3:
                    examples[name].append(kpi.get("kpi_id", ""))

    clusters = []
    for feature, count in counts.items():
        risk = risk_class(feature, structural_hints=structural_hints)
        clusters.append(
            {
                "feature": feature,
                "count": count,
                "risk": risk,
                "priority_score": risk_score(risk) * 100 + count,
                "example_kpis": examples[feature],
                "recommended_question": question_for_feature(feature, risk),
            }
        )
    clusters.sort(key=lambda item: (-item["priority_score"], item["feature"]))
    return clusters


def infer_join_candidates(features: list[dict[str, Any]]) -> list[dict[str, Any]]:
    by_name: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for feature in features:
        for column in feature.get("source_columns", []):
            normalized = normalize(column.get("column", ""))
            if normalized.endswith(JOIN_KEY_SUFFIXES):
                by_name[normalized].append(column)

    joins = []
    for normalized, columns in by_name.items():
        datasets = sorted({column["dataset"] for column in columns})
        if len(datasets) > 1:
            joins.append(
                {
                    "key": normalized,
                    "state": "candidate_join",
                    "datasets": datasets,
                    "evidence": columns,
                    "requires": ["uniqueness_check", "null_check", "grain_cardinality_check"],
                }
            )
    return joins


# Generic starter terms. NOT workspace-specific (every business has costs,
# dates, etc.). Domain-specific terms (e.g., "encounter", "claim") must come
# in via `structural_hints` derived from the workspace's profiled table names.
GENERIC_FINANCIAL_TERMS = (
    "amount",
    "paid",
    "balance",
    "cost",
    "revenue",
    "margin",
    "refund",
    "charge",
    "price",
    "fee",
    "spend",
    "income",
    "profit",
    "loss",
)
GENERIC_TEMPORAL_TERMS = (
    "date",
    "day",
    "month",
    "year",
    "week",
    "quarter",
    "age",
    "aging",
    "timestamp",
)


def risk_class(feature: str, *, structural_hints: set[str] | None = None) -> str:
    """Classify a feature's risk class.

    Structural classification is evidence-driven: the caller passes
    `structural_hints` derived from the workspace (typically the profiled
    table names). Without hints, structural classification falls back to
    the join-key-suffix check alone. No domain vocabulary is hardcoded.
    """
    value = normalize(feature)
    if any(term in value for term in GENERIC_FINANCIAL_TERMS):
        return "financial_correctness"
    if any(term in value for term in GENERIC_TEMPORAL_TERMS):
        return "temporal_correctness"
    if value.endswith(JOIN_KEY_SUFFIXES):
        return "structural"
    if structural_hints:
        normalized_hints = {normalize(hint) for hint in structural_hints if hint}
        if any(hint and hint in value for hint in normalized_hints):
            return "structural"
    return "business_semantics"


def risk_score(risk: str) -> int:
    return {
        "financial_correctness": 4,
        "temporal_correctness": 3,
        "business_semantics": 2,
        "structural": 1,
    }.get(risk, 1)


def question_for_feature(feature: str, risk: str) -> str:
    if risk == "financial_correctness":
        return f"What authoritative source or accepted rule defines `{feature}` and its grain?"
    if risk == "temporal_correctness":
        return f"What temporal anchor/source defines `{feature}`?"
    return f"What source, formula, or accepted rule should define `{feature}`?"


def normalize(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", "", str(value).lower())
