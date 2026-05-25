from __future__ import annotations

import json
from collections import defaultdict
from pathlib import Path
from typing import Any

from core.onboarding.features.derived_evidence import (
    column_profile_summary,
    semantic_meaning_sources,
    value_profile,
)
from core.onboarding.features.blockers import normalize, risk_class


STRUCTURAL_ALIAS_SUFFIXES = ("id", "code", "date", "type", "status")
BUSINESS_COLUMN_ALIASES = {
    "paidamount": {"paid", "amountpaid", "amount"},
    "payorid": {"payer", "payor"},
    "payerid": {"payer", "payor"},
    "lineofbusiness": {"lob", "lineofbusiness", "businessline"},
    "visittype": {"visit", "visittype"},
    "deptid": {"department", "departmentid"},
    "departmentid": {"department", "dept", "deptid"},
    "patientid": {"lives", "life", "member", "patient"},
}


def load_schema_index(profile_index_path: Path) -> dict[str, list[dict[str, Any]]]:
    if not profile_index_path.exists():
        return {}
    data = json.loads(profile_index_path.read_text(encoding="utf-8"))
    return schema_index_from_profiles(data.get("profiles", []))


def schema_index_from_profiles(profiles: list[dict[str, Any]]) -> dict[str, list[dict[str, Any]]]:
    index: dict[str, list[dict[str, Any]]] = {}
    for profile in profiles:
        dataset = profile.get("path", "")
        row_count = profile.get("row_count")
        for column, dtype in (profile.get("schema") or {}).items():
            index.setdefault(normalize(column), []).append(
                {
                    "dataset": dataset,
                    "column": column,
                    "dtype": str(dtype),
                    "row_count": row_count,
                    "profile_path": profile.get("profile_path"),
                    **column_profile_summary(profile, column),
                }
            )
    return index


def alias_index(schema_index: dict[str, list[dict[str, Any]]]) -> dict[str, list[dict[str, Any]]]:
    aliases: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for entries in schema_index.values():
        for entry in entries:
            column = entry["column"]
            normalized = normalize(column)
            for alias in aliases_for_column(column):
                if alias != normalized:
                    aliases[alias].append({**entry, "reason": "normalized_structural_alias"})
    return dict(aliases)


def source_columns(evidences: list[dict[str, Any]]) -> list[dict[str, Any]]:
    columns = []
    for evidence in evidences:
        meanings = semantic_meaning_sources(
            "physical_column_candidate",
            evidence["column"],
            evidence,
        )
        if evidence.get("dictionary_description"):
            meanings.append(
                {
                    "file": evidence.get("dictionary_path"),
                    "table": evidence.get("dictionary_table"),
                    "field": evidence.get("dictionary_field") or evidence["column"],
                    "meaning": evidence.get("dictionary_description"),
                    "evidence_state": "data_dictionary",
                }
            )
        mapping_proof = {
            "proof_state": (
                "dictionary_and_profile_backed"
                if evidence.get("dictionary_description") and evidence.get("profile_path")
                else "profile_backed"
            ),
            "claim": (
                f"`{evidence['column']}` is a candidate physical mapping for the KPI feature."
            ),
            "match_reason": evidence.get("reason")
            or (
                "Column is present in generated profile evidence and matched the KPI term "
                "directly or through a configured alias."
            ),
            "score": evidence.get("score"),
            "source_files": [
                file
                for file in [
                    evidence.get("profile_path"),
                    evidence.get("dictionary_path"),
                ]
                if file
            ],
            "dictionary_evidence": {
                "table": evidence.get("dictionary_table"),
                "field": evidence.get("dictionary_field") or evidence["column"],
                "description": evidence.get("dictionary_description"),
                "file": evidence.get("dictionary_path"),
            }
            if evidence.get("dictionary_description")
            else None,
            "profile_evidence": {
                "dataset": evidence.get("dataset"),
                "column": evidence.get("column"),
                "dtype": evidence.get("dtype"),
                "row_count": evidence.get("row_count"),
                "profile_path": evidence.get("profile_path"),
                "value_profile": value_profile(evidence),
            },
            "sample_query": sample_query(evidence),
            "sample_output": sample_output(evidence),
            "remaining_risk": (
                "Physical column, dictionary, and profile evidence support the mapping, "
                "but business owners can still override if the intended grain differs."
            ),
        }
        columns.append(
            {
                "dataset": evidence["dataset"],
                "column": evidence["column"],
                "dtype": evidence.get("dtype"),
                "row_count": evidence.get("row_count"),
                "score": evidence.get("score"),
                "profile_path": evidence.get("profile_path"),
                "observed_values": evidence.get("sample_values") or [],
                "value_profile": value_profile(evidence),
                "semantic_meaning_sources": meanings,
                "mapping_proof": mapping_proof,
                "reason": mapping_proof["match_reason"],
            }
        )
    return columns


def sample_query(evidence: dict[str, Any]) -> str:
    dataset = Path(str(evidence.get("dataset") or "")).stem or "source_table"
    column = str(evidence.get("column") or "column")
    alias = normalize(column) or "sample_value"
    return f'SELECT "{column}" AS "{alias}" FROM "{dataset}" LIMIT 5;'


def sample_output(evidence: dict[str, Any]) -> list[dict[str, Any]]:
    column = str(evidence.get("column") or "column")
    alias = normalize(column) or "sample_value"
    values = list(evidence.get("sample_values") or [])
    if not values:
        for key in ("sample_min", "exact_min", "metadata_min", "sample_max", "exact_max", "metadata_max"):
            value = evidence.get(key)
            if value is not None:
                values.append(value)
                break
    return [{alias: value} for value in values[:5]]


def aliases_for_column(column: str) -> set[str]:
    normalized = normalize(column)
    aliases = {normalized}
    aliases.update(BUSINESS_COLUMN_ALIASES.get(normalized, set()))
    compact = normalized
    for suffix in STRUCTURAL_ALIAS_SUFFIXES:
        if compact.endswith(suffix) and not compact.endswith(f"{suffix}{suffix}"):
            aliases.add(compact[: -len(suffix)] + suffix)
    if normalized.endswith("id"):
        base = normalized[:-2]
        aliases.add(base + "id")
        aliases.add(base + "identifier")
    if "department" in normalized:
        aliases.add(normalized.replace("department", "dept"))
    if "dept" in normalized:
        aliases.add(normalized.replace("dept", "department"))
    if "claims" in normalized:
        aliases.add(normalized.replace("claims", "claim"))
    if "claim" in normalized:
        aliases.add(normalized.replace("claim", "claims"))
    return {alias for alias in aliases if alias}


def safe_structural_alias(feature: str, candidates: list[dict[str, Any]]) -> bool:
    feature_norm = normalize(feature)
    if risk_class(feature) != "structural":
        return False
    return any(feature_norm in aliases_for_column(candidate["column"]) for candidate in candidates)


def candidate_labels(candidates: list[dict[str, Any]]) -> list[str]:
    return [f"{candidate['column']} ({candidate['dataset']})" for candidate in candidates[:5]]
