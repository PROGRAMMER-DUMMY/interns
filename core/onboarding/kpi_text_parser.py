from __future__ import annotations

import re
from typing import Any


KPI_CUTS_HEADERS = [
    "cuts / grain hints",
    "cuts",
    "cuts with drg",
    "cuts with drg consolidated",
    "cuts with drg(consolidated)",
    "drg consolidated",
    "grain",
    "dimensions",
]


def is_template_kpi_row(name: str) -> bool:
    normalized = re.sub(r"[^a-z0-9]+", " ", name.lower()).strip()
    template_markers = (
        "which key business question is the kpi metric feature meant to answer",
        "key business question",
        "short description of the kpi metric feature",
    )
    return any(marker in normalized for marker in template_markers)


def infer_metric_and_cuts(name: str, description: str = "") -> tuple[str, str]:
    text = f"{name} {description}".lower()
    metric = ""
    cuts: list[str] = []
    if "amount paid" in text or "paid amount" in text:
        metric = "amount paid"
    elif "percentage share of lives" in text or "share of lives" in text:
        metric = "percentage share of lives"
    elif "lives count" in text:
        metric = "lives count"

    if "lob" in text or "line of business" in text:
        if "medicare" in text:
            cuts.append("LOB = Medicare")
        elif "commercial" in text:
            cuts.append("LOB = Commercial")
        else:
            cuts.append("LOB")
    if "gender" in text:
        cuts.append("Gender")
    if "payer" in text or "payor" in text:
        cuts.append("Payer")
    if "above 50" in text or "over 50" in text:
        cuts.append("Age > 50")
    elif re.search(r"\bage\b", text):
        cuts.append("Age")
    if "visit type" in text:
        cuts.append("VisitType")
    if "department" in text:
        cuts.append("Department")
    if "top 10" in text and "Payer" not in cuts:
        cuts.append("Payer top 10")
    return metric, ", ".join(dict.fromkeys(cuts))


def first_existing(lowered: dict[str, str], candidates: list[str]) -> str | None:
    for candidate in candidates:
        if candidate in lowered:
            return lowered[candidate]
    return None


def first_index(index: dict[str, int], candidates: list[str]) -> int | None:
    for candidate in candidates:
        if candidate in index:
            return index[candidate]
    return None


def cell_at(row: list[str], idx: int | None) -> str:
    if idx is None or idx >= len(row):
        return ""
    return row[idx].strip()


def clean_cell(value: Any) -> str:
    if value is None:
        return ""
    return str(value).strip()


def extract_kpis_from_sql(text: str, source: str) -> list[dict[str, str]]:
    kpis = []
    current_objective = ""
    current_question = []

    def save_question():
        nonlocal current_question
        if current_question:
            question_text = " ".join(current_question)
            question_text = re.sub(r"^[a-z0-9]+\.\s+", "", question_text, flags=re.IGNORECASE).strip()
            if question_text:
                metric, cuts = infer_metric_and_cuts(question_text, current_objective)
                kpis.append({
                    "name": question_text,
                    "description": current_objective.strip() or f"Extracted from {source}",
                    "cuts": cuts,
                    "metric": metric,
                    "source": source
                })
            current_question = []

    for line in text.splitlines():
        line = line.strip()
        if not line.startswith("--"):
            save_question()
            continue

        comment = line.lstrip("- ").strip()
        if not comment:
            save_question()
            continue

        if comment.isupper() and ("OBJECTIVE" in comment or "GOAL" in comment):
            current_objective = comment
            save_question()
            continue

        is_question_start = (
            comment.endswith("?") or
            re.match(r"^[a-z0-9]+\.\s+", comment, re.IGNORECASE) or
            re.match(r"^(what|how|which|where|when|why|is|are|do|does)\b", comment, re.IGNORECASE)
        )

        if current_question:
            current_question.append(comment)
        elif is_question_start:
            current_question.append(comment)
        else:
            if "query" in comment.lower() or "kpi" in comment.lower():
                current_question.append(comment)

    save_question()
    return kpis
