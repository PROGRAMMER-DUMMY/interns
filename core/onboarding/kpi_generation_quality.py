from __future__ import annotations

import re
from pathlib import Path
from typing import Any


def score_kpis(kpis: list[Any], listing: dict[str, Any], context_files: list[str]) -> dict[str, Any]:
    data_files = [
        file
        for file in listing.get("files", [])
        if "/datasets/" in file and Path(file).suffix.lower() in {".csv", ".parquet", ".json"}
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
        kpi_scores.append(
            {
                "kpi_id": f"kpi_{idx:03d}",
                "business_question": kpi.name,
                "implementation_readiness": min(100, implementation),
                "business_quality": min(100, business),
                "missing_discussion_points": missing,
                "recommendation": "revise_before_implementation" if missing else "ready_for_mapping",
            }
        )
    if not kpi_scores:
        implementation_avg = 0
        business_avg = 0
    else:
        implementation_avg = round(
            sum(item["implementation_readiness"] for item in kpi_scores) / len(kpi_scores)
        )
        business_avg = round(sum(item["business_quality"] for item in kpi_scores) / len(kpi_scores))
    overall = round((implementation_avg + business_avg) / 2)
    return {
        "implementation_readiness": implementation_avg,
        "business_quality": business_avg,
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
    if "paid" in files or "amount" in files:
        return {
            "name": "What is paid amount trend by key business dimensions?",
            "description": "Seed KPI suggested from available amount-like data.",
            "cuts": "time period, payer or line of business when available",
            "metric": "sum(PaidAmount)",
            "refinement_required": "Confirm owner, temporal anchor, filters, and acceptance tests.",
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
    common_aliases = {
        "paid": {"paid", "amount", "payment"},
        "payer": {"payer", "payor"},
        "lob": {"line", "business"},
        "claim": {"claim", "claims"},
    }
    expanded = set(tokens)
    for token in tokens:
        expanded.update(common_aliases.get(token, set()))
    return bool(expanded.intersection(file_tokens))


def unique_sorted(values: Any) -> list[str]:
    return sorted({str(value) for value in values if str(value)})
