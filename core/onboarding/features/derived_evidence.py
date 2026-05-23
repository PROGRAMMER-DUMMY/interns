from __future__ import annotations

import re
from pathlib import Path
from typing import Any

from core.onboarding.features.derivation_search import PATTERNS_PATH
from core.onboarding.features.blockers import normalize


def derived_feature_options(
    feature: str,
    candidate_patterns: list[dict[str, Any]],
    schema_index: dict[str, list[dict[str, Any]]],
    kpi: dict[str, Any],
    expression_context: str,
) -> list[dict[str, Any]]:
    options = []
    for pattern in candidate_patterns:
        bindings = {
            input_name: list(columns)
            for input_name, columns in (pattern.get("candidate_bindings") or {}).items()
            if columns
        }
        selected_bindings = {
            input_name: columns[0]
            for input_name, columns in bindings.items()
            if columns
        }
        required_inputs = list(pattern.get("required_inputs", []))
        missing_inputs = [name for name in required_inputs if name not in selected_bindings]
        input_columns = [
            derived_input_column(input_name, column, schema_index)
            for input_name, column in selected_bindings.items()
        ]
        formula_templates = {
            engine: fill_formula_template(template, selected_bindings)
            for engine, template in (pattern.get("templates") or {}).items()
        }
        formula = formula_templates.get("duckdb_sql") or next(iter(formula_templates.values()), "")
        example = derived_feature_example(feature, pattern, selected_bindings, input_columns, formula)
        evidence_sources = derived_evidence_sources(
            feature,
            pattern,
            input_columns,
            kpi,
            expression_context,
        )
        options.append(
            {
                "derived_column_name": feature,
                "business_meaning": pattern.get("description", ""),
                "formula": formula,
                "formula_templates": formula_templates,
                "input_columns": input_columns,
                "example": example,
                "evidence_sources": evidence_sources,
                "derivation_reasoning": derivation_reasoning(
                    feature,
                    pattern,
                    missing_inputs,
                    input_columns,
                ),
                "evidence_state": "candidate_derivation_not_ground_truth",
                "confidence": "medium" if not missing_inputs and input_columns else "low",
                "needs_user_confirmation": True,
                "missing_inputs": missing_inputs,
                "requires": list(pattern.get("requires", [])),
                "source_pattern_id": pattern.get("pattern_id"),
                "clarification_prompt": pattern.get("clarification_prompt", ""),
            }
        )
    return options


def derived_input_column(
    input_name: str,
    column: str,
    schema_index: dict[str, list[dict[str, Any]]],
) -> dict[str, Any]:
    evidences = schema_index.get(normalize(column), [])
    evidence = evidences[0] if evidences else {}
    return {
        "input_name": input_name,
        "column": column,
        "dataset": evidence.get("dataset"),
        "dtype": evidence.get("dtype"),
        "role": "formula_input",
        "profile_path": evidence.get("profile_path"),
        "row_count": evidence.get("row_count"),
        "observed_values": evidence.get("sample_values") or [],
        "value_profile": value_profile(evidence),
        "semantic_meaning_sources": semantic_meaning_sources(input_name, column, evidence),
        "reason": input_column_reason(input_name, column, evidence),
        "example_value": example_value(input_name, evidence),
    }


def semantic_meaning_sources(
    input_name: str,
    column: str,
    evidence: dict[str, Any],
) -> list[dict[str, Any]]:
    meanings: list[dict[str, Any]] = []
    dataset = evidence.get("dataset")
    profile_path = evidence.get("profile_path")
    if profile_path or dataset:
        meanings.append(
            {
                "file": profile_path or dataset,
                "field": column,
                "meaning": (
                    f"Column `{column}` is bound to required derivation input "
                    f"`{input_name}` by schema/profile name matching."
                ),
                "evidence_state": "schema_profile_inferred",
            }
        )
    return meanings


def input_column_reason(input_name: str, column: str, evidence: dict[str, Any]) -> str:
    dataset = evidence.get("dataset")
    if dataset:
        return (
            f"Used for `{input_name}` because `{column}` was the closest available profiled "
            f"column in `{dataset}`. This is candidate evidence, not a business definition."
        )
    return (
        f"Used for `{input_name}` because it matched the derivation pattern input name. "
        "No profile-backed dataset evidence was found."
    )


def derivation_reasoning(
    feature: str,
    pattern: dict[str, Any],
    missing_inputs: list[str],
    input_columns: list[dict[str, Any]],
) -> dict[str, str]:
    pattern_id = pattern.get("pattern_id") or "unknown_pattern"
    why_this = (
        f"`{feature}` is not proven as a direct physical column, and derivation pattern "
        f"`{pattern_id}` matched the feature term plus available profiled columns."
    )
    if missing_inputs:
        risk = f"Missing required inputs: {', '.join(missing_inputs)}."
    elif not input_columns:
        risk = "No input columns were bound from profile evidence."
    else:
        risk = "Input columns are profile-backed, but the business meaning still needs confirmation."
    return {
        "why_this_formula": why_this,
        "why_not_ground_truth": "No source artifact explicitly defines this derived column with this formula.",
        "remaining_risk": risk,
    }


def derived_evidence_sources(
    feature: str,
    pattern: dict[str, Any],
    input_columns: list[dict[str, Any]],
    kpi: dict[str, Any],
    expression_context: str,
) -> list[dict[str, Any]]:
    sources = [
        {
            "file": repo_style_path(PATTERNS_PATH),
            "evidence_type": "derivation_pattern_library",
            "evidence": f"Pattern `{pattern.get('pattern_id')}` proposes a reusable candidate formula.",
            "evidence_state": "candidate_not_ground_truth",
        }
    ]
    if kpi.get("source"):
        sources.append(
            {
                "file": kpi.get("source"),
                "evidence_type": "kpi_registry",
                "evidence": f"KPI expression references `{feature}` in `{expression_context}`.",
                "evidence_state": "source_mentions_feature",
            }
        )
    for column in input_columns:
        sources.append(
            {
                "file": column.get("profile_path") or "interns/generated/profiles/profile_index.json",
                "dataset": column.get("dataset"),
                "column": column.get("column"),
                "evidence_type": "profile_schema",
                "evidence": f"Input column `{column.get('column')}` is present in generated profile evidence.",
                "evidence_state": "schema_presence_only",
            }
        )
    return sources


def derived_feature_example(
    feature: str,
    pattern: dict[str, Any],
    selected_bindings: dict[str, str],
    input_columns: list[dict[str, Any]],
    formula: str,
) -> dict[str, Any]:
    pattern_id = str(pattern.get("pattern_id") or "")
    by_input = {column["input_name"]: column.get("example_value") for column in input_columns}
    example_inputs = dict(by_input)
    example_inputs.update(pattern_example_inputs(pattern_id, selected_bindings))
    output_value = pattern_example_output(feature, pattern_id, example_inputs)
    return {
        "example_type": "synthetic_formula_example",
        "input": {
            column_name: example_inputs.get(input_name)
            for input_name, column_name in selected_bindings.items()
        },
        "output": {feature: output_value},
        "substituted_formula": substitute_formula_values(formula, selected_bindings, example_inputs),
        "warning": "Example demonstrates formula mechanics only; it is not workspace ground truth.",
    }


def pattern_example_inputs(pattern_id: str, selected_bindings: dict[str, str]) -> dict[str, Any]:
    if pattern_id == "age_years":
        return {"birth_date": "1980-01-01", "anchor_date": "2024-01-01"}
    if pattern_id == "age_band":
        return {"age_years": 42}
    if pattern_id == "net_paid_amount":
        values = {"paid_amount": 100.0, "refund_amount": 10.0, "denied_amount": 0.0}
        return {name: value for name, value in values.items() if name in selected_bindings}
    if pattern_id == "days_in_ar":
        return {"ar_start_date": "2024-01-01", "as_of_date": "2024-01-31"}
    if pattern_id == "cpt_family":
        return {"procedure_code": "99213", "cpt_reference": "Evaluation and Management"}
    if pattern_id == "provider_specialty":
        return {"provider_id": "P001", "specialization": "Cardiology"}
    return {}


def pattern_example_output(feature: str, pattern_id: str, example_inputs: dict[str, Any]) -> Any:
    if pattern_id == "age_years":
        return 44
    if pattern_id == "age_band":
        return "35-49"
    if pattern_id == "net_paid_amount":
        paid = float(example_inputs.get("paid_amount", 100.0) or 0)
        refund = float(example_inputs.get("refund_amount", 0.0) or 0)
        return paid - refund
    if pattern_id == "days_in_ar":
        return 30
    if pattern_id == "cpt_family":
        return "Evaluation and Management"
    if pattern_id == "provider_specialty":
        return example_inputs.get("specialization", "Cardiology")
    return f"example_{feature}"


def substitute_formula_values(
    formula: str,
    selected_bindings: dict[str, str],
    example_inputs: dict[str, Any],
) -> str:
    substituted = formula
    for input_name, column in selected_bindings.items():
        value = example_inputs.get(input_name)
        rendered = f"'{value}'" if isinstance(value, str) else str(value)
        substituted = re.sub(rf"\b{re.escape(column)}\b", rendered, substituted)
    return substituted


def fill_formula_template(template: str, selected_bindings: dict[str, str]) -> str:
    filled = template
    for input_name, column in selected_bindings.items():
        filled = filled.replace("{" + input_name + "}", column)
    return filled


def example_value(input_name: str, evidence: dict[str, Any]) -> Any:
    sample_values = evidence.get("sample_values") or []
    if sample_values:
        return sample_values[0]
    for key in ("sample_min", "exact_min", "metadata_min", "sample_max", "exact_max", "metadata_max"):
        value = evidence.get(key)
        if value is not None:
            return value
    if "date" in input_name:
        return "2024-01-01"
    dtype = str(evidence.get("dtype") or "").lower()
    if any(token in dtype for token in ("int", "float", "double", "decimal")):
        return 1
    return f"example_{input_name}"


def column_profile_summary(profile: dict[str, Any], column: str) -> dict[str, Any]:
    for item in profile.get("columns") or []:
        if item.get("name") == column:
            return {
                "sample_min": item.get("sample_min"),
                "sample_max": item.get("sample_max"),
                "exact_min": item.get("exact_min"),
                "exact_max": item.get("exact_max"),
                "metadata_min": item.get("metadata_min"),
                "metadata_max": item.get("metadata_max"),
                "null_count": item.get("null_count"),
                "sample_values": item.get("sample_values") or [],
                "profile_source": item.get("source"),
            }
    return {}


def value_profile(evidence: dict[str, Any]) -> dict[str, Any]:
    return {
        "sample_values": evidence.get("sample_values") or [],
        "sample_min": evidence.get("sample_min"),
        "sample_max": evidence.get("sample_max"),
        "exact_min": evidence.get("exact_min"),
        "exact_max": evidence.get("exact_max"),
        "metadata_min": evidence.get("metadata_min"),
        "metadata_max": evidence.get("metadata_max"),
        "null_count": evidence.get("null_count"),
        "profile_source": evidence.get("profile_source"),
        "note": "Values come from bounded profile evidence, not a full raw-data dump.",
    }


def repo_style_path(path: Path) -> str:
    return path.as_posix()
