"""KPI registry text parsing helpers.

Inference is intentionally evidence-driven: the matcher is a
[[WorkspaceLexicon]] derived from the workspace's own KPI registry rows,
profiles, dictionary, and accepted definitions. Without a lexicon this
module returns empty inferences -- there is no pre-baked vocabulary.

The hardcoded healthcare-RCM/e-commerce keyword ladder that previously
lived here was removed; production-grade inference must come from
workspace evidence, not a curated dictionary.
"""
from __future__ import annotations

import re
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from core.onboarding.lexicon import WorkspaceLexicon


# Cold-start baseline used during the FIRST onboarding pass to find the
# cuts column in spreadsheets, before a workspace lexicon exists. These
# are generic header names, not domain vocabulary. The lexicon's
# observed cuts_headers extend this list on subsequent runs.
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


def infer_metric_and_cuts(
    name: str,
    description: str = "",
    *,
    lexicon: "WorkspaceLexicon | None" = None,
) -> tuple[str, str]:
    """Return (metric, cuts) inferred from the workspace lexicon, or empty.

    Without a lexicon the function returns ``("", "")``. Authored KPI
    registry cells are the source of truth; inference only fills gaps when
    workspace-derived phrases support a match.
    """
    if lexicon is None:
        return "", ""
    return lexicon.infer_metric_and_cuts(name, description)


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


def extract_kpis_from_sql(
    text: str,
    source: str,
    *,
    lexicon: "WorkspaceLexicon | None" = None,
) -> list[dict[str, str]]:
    kpis: list[dict[str, str]] = []
    current_objective = ""
    current_question: list[str] = []

    def save_question() -> None:
        nonlocal current_question
        if current_question:
            question_text = " ".join(current_question)
            question_text = re.sub(
                r"^[a-z0-9]+\.\s+", "", question_text, flags=re.IGNORECASE
            ).strip()
            if question_text:
                metric, cuts = infer_metric_and_cuts(
                    question_text, current_objective, lexicon=lexicon
                )
                kpis.append({
                    "name": question_text,
                    "description": current_objective.strip() or f"Extracted from {source}",
                    "cuts": cuts,
                    "metric": metric,
                    "source": source,
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
            comment.endswith("?")
            or re.match(r"^[a-z0-9]+\.\s+", comment, re.IGNORECASE)
            or re.match(
                r"^(what|how|which|where|when|why|is|are|do|does)\b",
                comment,
                re.IGNORECASE,
            )
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
