from __future__ import annotations

import re
from collections import Counter, defaultdict
from typing import Any


JOIN_KEY_SUFFIXES = ("id", "code")


def prioritize_blockers(mapping: dict[str, Any]) -> list[dict[str, Any]]:
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
        risk = risk_class(feature)
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


def risk_class(feature: str) -> str:
    value = normalize(feature)
    financial_terms = (
        "amount",
        "paid",
        "payor",
        "payer",
        "denied",
        "allowed",
        "balance",
        "cost",
        "revenue",
        "margin",
        "refund",
        "charge",
    )
    temporal_terms = ("date", "day", "month", "age", "aging", "admit", "discharge")
    if any(term in value for term in financial_terms):
        return "financial_correctness"
    if any(term in value for term in temporal_terms):
        return "temporal_correctness"
    if value.endswith(JOIN_KEY_SUFFIXES) or any(
        term in value for term in ("department", "dept", "provider", "patient", "claim", "encounter")
    ):
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
