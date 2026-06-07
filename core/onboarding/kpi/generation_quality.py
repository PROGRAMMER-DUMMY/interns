from __future__ import annotations

import re
from pathlib import Path
from typing import Any


def score_kpis(kpis: list[Any], listing: dict[str, Any], context_files: list[str]) -> dict[str, Any]:
    data_files = [
        file
        for file in listing.get("files", [])
        if "/interns/" not in file
        and "/docs/" not in file
        and Path(file).suffix.lower() in {".csv", ".parquet", ".json", ".jsonl"}
    ]
    has_data_model = bool(listing.get("possible_data_model_files"))
    has_context = bool(context_files)
    kpi_scores = []
    for idx, kpi in enumerate(kpis, start=1):
        text = " ".join([kpi.name, kpi.description, kpi.cuts, kpi.metric]).lower()
        implementation = 0
        implementation += 25 if kpi.metric else 0
        implementation += 15 if kpi.cuts else 0
        implementation += 20 if data_files else 0
        implementation += 15 if has_data_model else 0
        implementation += 15 if column_like_token_overlap(text, data_files) else 0
        implementation += 10 if kpi.refinement_required else 5
        business = 0
        business += 25 if looks_like_business_question(kpi.name) else 10 if kpi.name else 0
        business += 15 if kpi.description else 0
        business += 15 if kpi.cuts else 0
        business += 10 if kpi.refinement_required else 0
        business += 10 if has_context else 0
        business += 10 if any(token in text for token in ("trend", "top", "share", "rate", "ratio")) else 0
        business += 15 if any(token in text for token in ("why", "which", "how", "what")) else 0
        missing = missing_discussion_points(kpi, has_context)
        understanding = understanding_score(
            kpi,
            has_data_model=has_data_model,
            has_context=has_context,
            data_files=data_files,
        )
        kpi_scores.append(
            {
                "kpi_id": f"kpi_{idx:03d}",
                "business_question": kpi.name,
                "implementation_readiness": min(100, implementation),
                "business_quality": min(100, business),
                "understanding_score": understanding["score"],
                "understanding": understanding,
                "missing_discussion_points": missing,
                "recommendation": "revise_before_implementation" if missing else "ready_for_mapping",
            }
        )
    if not kpi_scores:
        implementation_avg = 0
        business_avg = 0
        understanding_avg = 0
    else:
        implementation_avg = round(
            sum(item["implementation_readiness"] for item in kpi_scores) / len(kpi_scores)
        )
        business_avg = round(sum(item["business_quality"] for item in kpi_scores) / len(kpi_scores))
        understanding_avg = round(
            sum(item["understanding_score"] for item in kpi_scores) / len(kpi_scores)
        )
    overall = round((implementation_avg + business_avg) / 2)
    return {
        "implementation_readiness": implementation_avg,
        "business_quality": business_avg,
        "understanding_score": understanding_avg,
        "overall_score": overall,
        "kpi_count": len(kpis),
        "coverage": {
            "kpi_file_present": bool(listing.get("possible_kpi_files")),
            "data_model_present": has_data_model,
            "dataset_file_count": len(data_files),
            "optional_context_present": has_context,
        },
        "kpis": kpi_scores,
        "missing_discussion_points": unique_sorted(
            point for item in kpi_scores for point in item.get("missing_discussion_points", [])
        ),
    }


# Per-dimension understanding model. Each dimension maps to a 0-100 confidence
# derived only from the KPI text + structural workspace evidence (no domain
# vocabulary). The session understanding score is the mean of these dimensions.
UNDERSTANDING_DIMENSIONS = (
    "business_question",
    "metric",
    "cuts_dimensions",
    "grain",
    "filters",
    "source_proof",
)

# Status icons surfaced in the panel markdown breakdown.
_CONFIDENCE_STRONG = 75
_CONFIDENCE_PARTIAL = 40


def understanding_score(
    kpi: Any,
    *,
    has_data_model: bool,
    has_context: bool,
    data_files: list[str],
) -> dict[str, Any]:
    """Compute a 0-100 understanding/confidence score for a single KPI.

    Returns the mean per-dimension confidence, a per-dimension breakdown with a
    status icon (strong/partial/missing), and the single lowest-confidence
    dimension named as the next question to ask. Everything is derived from the
    KPI text and structural evidence so the score stays workspace-agnostic.
    """
    name = str(getattr(kpi, "name", "") or "")
    description = str(getattr(kpi, "description", "") or "")
    cuts = str(getattr(kpi, "cuts", "") or "")
    metric = str(getattr(kpi, "metric", "") or "")
    refinement = str(getattr(kpi, "refinement_required", "") or "")
    text = " ".join([name, description, cuts, metric, refinement]).lower()

    confidences: dict[str, int] = {}

    # business_question: is it phrased as an answerable business question?
    bq = 0
    bq += 60 if looks_like_business_question(name) else 25 if name.strip() else 0
    bq += 25 if description.strip() else 0
    bq += 15 if any(token in text for token in ("decision", "action", "use to", "why")) else 0
    confidences["business_question"] = min(100, bq)

    # metric: is the measure/aggregation expressed and not a placeholder?
    metric_value = metric.strip().lower()
    placeholder = metric_value in {"", "confirm metric"} or "<measure>" in metric_value
    mt = 0
    if not placeholder:
        mt += 60
        mt += 25 if re.search(r"\b(sum|count|avg|average|min|max|ratio|rate|median)\b", text) else 0
        mt += 15 if re.search(r"[()]", metric) else 0
    else:
        mt += 20 if any(token in text for token in ("rate", "ratio", "share", "count", "total")) else 0
    confidences["metric"] = min(100, mt)

    # cuts_dimensions: are slicing dimensions named?
    ct = 0
    if cuts.strip():
        ct += 55
        tokens = [tok for tok in re.split(r"[,/;]| and | by ", cuts) if tok.strip()]
        ct += 30 if len(tokens) >= 2 else 15
        ct += 15 if not re.search(r"\bconfirm\b", cuts.lower()) else 0
    confidences["cuts_dimensions"] = min(100, ct)

    # grain: is a temporal/aggregation grain decided?
    gr = 0
    gr += 60 if any(
        token in text for token in ("daily", "monthly", "weekly", "yearly", "per ", "grain")
    ) else 0
    gr += 25 if any(token in text for token in ("date", "month", "year", "period", "time")) else 0
    gr += 15 if cuts.strip() else 0
    gr = 35 if gr == 0 and cuts.strip() else gr
    confidences["grain"] = min(100, gr)

    # filters: are inclusions/exclusions decided?
    fl = 0
    fl += 70 if any(token in text for token in ("exclude", "include", "filter", "where", "only")) else 0
    fl += 30 if "denominator" in text or "numerator" in text else 0
    fl = 30 if fl == 0 and description.strip() else fl
    confidences["filters"] = min(100, fl)

    # source_proof: do we have data/model/context evidence + column overlap?
    sp = 0
    sp += 30 if data_files else 0
    sp += 25 if has_data_model else 0
    sp += 25 if column_like_token_overlap(text, data_files) else 0
    sp += 20 if has_context else 0
    confidences["source_proof"] = min(100, sp)

    score = round(sum(confidences[dim] for dim in UNDERSTANDING_DIMENSIONS) / len(UNDERSTANDING_DIMENSIONS))
    breakdown = [
        {
            "dimension": dim,
            "confidence": confidences[dim],
            "status": _confidence_status(confidences[dim]),
            "icon": _confidence_icon(confidences[dim]),
        }
        for dim in UNDERSTANDING_DIMENSIONS
    ]
    lowest = min(breakdown, key=lambda item: item["confidence"])
    return {
        "score": score,
        "label": _understanding_label(score),
        "dimensions": breakdown,
        "lowest_dimension": lowest["dimension"],
        "next_question": _next_question_for(lowest["dimension"]),
    }


def _confidence_status(value: int) -> str:
    if value >= _CONFIDENCE_STRONG:
        return "strong"
    if value >= _CONFIDENCE_PARTIAL:
        return "partial"
    return "missing"


def _confidence_icon(value: int) -> str:
    return {"strong": "[ok]", "partial": "[~]", "missing": "[x]"}[_confidence_status(value)]


def _understanding_label(score: int) -> str:
    if score >= _CONFIDENCE_STRONG:
        return "well_understood"
    if score >= _CONFIDENCE_PARTIAL:
        return "partially_understood"
    return "needs_clarification"


def _next_question_for(dimension: str) -> str:
    prompts = {
        "business_question": "State the decision this KPI supports and phrase it as a business question.",
        "metric": "Define the exact measure and aggregation (e.g. sum/count/ratio of which value?).",
        "cuts_dimensions": "Name the dimensions to slice by (which categories/segments?).",
        "grain": "Decide the reporting grain and temporal anchor (per day/month/period?).",
        "filters": "Specify the inclusion/exclusion rules and any denominator scope.",
        "source_proof": "Identify the source dataset/columns that prove this KPI is computable.",
    }
    return prompts.get(dimension, "Clarify the lowest-confidence dimension before finalizing.")


def missing_discussion_points(kpi: Any, has_context: bool) -> list[str]:
    missing = []
    text = " ".join([kpi.name, kpi.description, kpi.cuts, kpi.metric, kpi.refinement_required]).lower()
    if "owner" not in text:
        missing.append("owner")
    if not any(token in text for token in ("decision", "action", "use to", "why")):
        missing.append("decision_supported")
    if not kpi.cuts:
        missing.append("grain_or_dimensions")
    if not any(token in text for token in ("date", "month", "year", "trend", "period", "service")):
        missing.append("temporal_anchor")
    if not any(token in text for token in ("exclude", "include", "filter", "where")):
        missing.append("inclusions_exclusions")
    if any(token in text for token in ("percentage", "share", "rate", "ratio")) and "denominator" not in text:
        missing.append("denominator")
    if "test" not in text and "acceptance" not in text:
        missing.append("acceptance_tests")
    if not has_context:
        missing.append("stakeholder_context")
    return unique_sorted(missing)


def advisor_notes(quality: dict[str, Any]) -> list[str]:
    notes = []
    for point in quality.get("missing_discussion_points", []):
        notes.append(f"Discuss `{point}` before production approval.")
    return notes or ["No high-risk missing discussion point detected in the current evidence."]


def merge_refinement(existing: str, missing: list[str]) -> str:
    parts = [existing] if existing else []
    if missing:
        parts.append("Discuss: " + ", ".join(missing))
    return "; ".join(parts)


def suggest_seed_kpi(session: dict[str, Any]) -> dict[str, Any]:
    files = " ".join(session.get("detected_files", {}).get("files", [])).lower()
    from core.onboarding.lexicon.vocabulary import GENERIC_FINANCIAL_SEED
    if any(seed in files for seed in GENERIC_FINANCIAL_SEED):
        return {
            "name": "What is the trend of the workspace's primary monetary measure across business dimensions?",
            "description": "Seed KPI suggested from amount-shaped columns detected in the workspace.",
            "cuts": "time period and a categorical dimension; confirm specifics during refinement",
            "metric": "sum(<measure>)",
            "refinement_required": "Confirm the specific measure column, owner, temporal anchor, filters, and acceptance tests.",
            "source": "generated_from_workspace_data",
        }
    return {
        "name": "What operational KPI should this dataset support?",
        "description": "Seed KPI requiring product/business clarification.",
        "cuts": "confirm grain and dimensions",
        "metric": "confirm metric",
        "refinement_required": "Confirm business question, owner, metric, grain, and tests.",
        "source": "generated_from_workspace_data",
    }


def looks_like_business_question(value: str) -> bool:
    return bool(re.match(r"^\s*(what|how|which|where|when|why|is|are|do|does)\b", value, re.I))


def column_like_token_overlap(text: str, data_files: list[str]) -> bool:
    tokens = {token for token in re.split(r"[^a-z0-9]+", text.lower()) if len(token) > 3}
    if not tokens:
        return False
    file_tokens = {
        token
        for file in data_files
        for token in re.split(r"[^a-z0-9]+", Path(file).stem.lower())
        if len(token) > 3
    }
    # Generic alias rules: plural<->singular only. Domain-specific synonyms
    # (e.g. payer<->payor) used to live here and got removed; they now come
    # from `workspace_feature_definitions.json` accepted aliases.
    expanded = set(tokens)
    for token in tokens:
        if token.endswith("s"):
            expanded.add(token[:-1])
        else:
            expanded.add(token + "s")
    return bool(expanded.intersection(file_tokens))


def unique_sorted(values: Any) -> list[str]:
    return sorted({str(value) for value in values if str(value)})


# ---------------------------------------------------------------------------
# KPI result-format (output layout) candidates
# ---------------------------------------------------------------------------
# Layout candidates are derived generically from a KPI's metric + cuts. Each
# candidate carries a concrete EXAMPLE markdown table with illustrative sample
# values so the user sees exactly what their KPI output will look like before
# finalizing. No domain vocabulary is hardcoded; column headers come from the
# KPI's own cuts string (or generic structural fallbacks).


def _metric_label(kpi: dict[str, Any]) -> str:
    metric = str(kpi.get("metric") or "").strip()
    if metric and metric.lower() not in {"confirm metric"}:
        return metric
    name = str(kpi.get("business_question") or kpi.get("name") or "").strip()
    return name or "metric value"


def _dimension_tokens(kpi: dict[str, Any]) -> list[str]:
    cuts = str(kpi.get("cuts") or "")
    tokens = [tok.strip() for tok in re.split(r"[,/;]| and | by ", cuts) if tok.strip()]
    cleaned = [tok for tok in tokens if not re.search(r"\bconfirm\b", tok.lower())]
    return cleaned or tokens


def _time_dimension(tokens: list[str]) -> str | None:
    for tok in tokens:
        if any(word in tok.lower() for word in ("date", "month", "year", "period", "time", "day", "week", "quarter")):
            return tok
    return None


def _format_table(headers: list[str], rows: list[list[str]]) -> str:
    head = "| " + " | ".join(headers) + " |"
    sep = "| " + " | ".join(["---"] * len(headers)) + " |"
    body = ["| " + " | ".join(row) + " |" for row in rows]
    return "\n".join([head, sep, *body])


def result_format_candidates(kpi: dict[str, Any]) -> list[dict[str, Any]]:
    """Return 2-3 candidate result-table layouts for a KPI, each with a concrete
    example markdown table built from illustrative sample values.

    Layouts: time-series, ranked/top-N, grouped breakdown. Headers and grouping
    derive from the KPI's own metric + cuts so the example stays workspace
    agnostic.
    """
    metric_label = _metric_label(kpi)
    tokens = _dimension_tokens(kpi)
    time_dim = _time_dimension(tokens)
    category_dims = [tok for tok in tokens if tok != time_dim]
    primary_category = category_dims[0] if category_dims else "dimension"

    candidates: list[dict[str, Any]] = []

    # Time-series layout (only when a temporal dimension is present/likely).
    ts_period = time_dim or "period"
    candidates.append(
        {
            "option_id": "option_a",
            "layout_id": "time_series",
            "label": "Time-series",
            "description": f"One row per {ts_period}; tracks `{metric_label}` over time.",
            "columns": [ts_period, metric_label],
            "example_markdown": _format_table(
                [ts_period, metric_label],
                [
                    ["2026-01", "1,240"],
                    ["2026-02", "1,388"],
                    ["2026-03", "1,502"],
                ],
            ),
        }
    )

    # Ranked / top-N layout.
    candidates.append(
        {
            "option_id": "option_b",
            "layout_id": "ranked_top_n",
            "label": "Ranked / top-N",
            "description": f"Rows ranked by `{metric_label}`, highest first, sliced by `{primary_category}`.",
            "columns": ["rank", primary_category, metric_label],
            "example_markdown": _format_table(
                ["rank", primary_category, metric_label],
                [
                    ["1", "Segment A", "9,820"],
                    ["2", "Segment B", "7,415"],
                    ["3", "Segment C", "6,073"],
                ],
            ),
        }
    )

    # Grouped breakdown layout (two cuts when available, else category + share).
    second_category = category_dims[1] if len(category_dims) > 1 else "share_of_total"
    grouped_headers = [primary_category, second_category, metric_label]
    candidates.append(
        {
            "option_id": "option_c",
            "layout_id": "grouped_breakdown",
            "label": "Grouped breakdown",
            "description": f"`{metric_label}` broken down across `{primary_category}` and `{second_category}`.",
            "columns": grouped_headers,
            "example_markdown": _format_table(
                grouped_headers,
                [
                    ["Group 1", "Bucket X", "3,210"],
                    ["Group 1", "Bucket Y", "2,884"],
                    ["Group 2", "Bucket X", "4,109"],
                ],
            ),
        }
    )
    return candidates
