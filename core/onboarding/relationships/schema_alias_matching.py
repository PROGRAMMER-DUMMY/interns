"""Schema-alias matching for KPI feature resolution.

Column aliases come from two sources:

1. **Structural** patterns that hold across any domain (suffix variants like
   ``id``/``code``/``date``/``type``/``status``, and the ``id``/``identifier``
   linguistic pair). These are computed from column names alone.
2. **Workspace-derived** aliases from the [[WorkspaceLexicon]] — entries are
   harvested from authored KPI registry cells, data-dictionary descriptions,
   accepted user definitions, and prior resolved features. Each workspace
   produces its own alias set.

The previous module shipped a healthcare-RCM-specific ``BUSINESS_COLUMN_ALIASES``
dictionary. That curated vocabulary has been removed; alias sourcing is now
``derive don't curate``.
"""
from __future__ import annotations

import json
from collections import defaultdict
from pathlib import Path
from typing import TYPE_CHECKING, Any

from core.onboarding.features.derived_evidence import (
    column_profile_summary,
    semantic_meaning_sources,
    value_profile,
)
from core.onboarding.features.blockers import normalize, risk_class

if TYPE_CHECKING:
    from core.onboarding.lexicon import WorkspaceLexicon


STRUCTURAL_ALIAS_SUFFIXES = ("id", "code", "date", "type", "status")


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


def alias_index(
    schema_index: dict[str, list[dict[str, Any]]],
    lexicon: "WorkspaceLexicon | None" = None,
) -> dict[str, list[dict[str, Any]]]:
    """Build an alias index from a schema index.

    Aliases come from two sources: universal structural patterns (suffix
    variants, id/identifier) computed from each column name, plus
    workspace-derived aliases supplied via ``lexicon`` when one is loaded.
    Without a lexicon, only structural aliases fire.
    """
    aliases: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for entries in schema_index.values():
        for entry in entries:
            column = entry["column"]
            normalized = normalize(column)
            for alias in aliases_for_column(column, lexicon=lexicon):
                if alias != normalized:
                    aliases[alias].append(
                        {**entry, "reason": "schema_alias"}
                    )
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


def aliases_for_column(
    column: str,
    *,
    lexicon: "WorkspaceLexicon | None" = None,
) -> set[str]:
    """Aliases for ``column`` from structural patterns and the workspace lexicon.

    Structural patterns are universal: suffix variants (``id``/``code``/
    ``date``/``type``/``status``) and the ``id``/``identifier`` pair.
    Curated word-substitution rewrites that previously shipped here
    (``dept``↔``department``, ``claim``↔``claims``, healthcare business
    aliases) have been removed. Workspace-specific aliases are sourced from
    ``lexicon.column_aliases`` when a lexicon is provided.
    """
    normalized = normalize(column)
    aliases = {normalized}
    compact = normalized
    for suffix in STRUCTURAL_ALIAS_SUFFIXES:
        if compact.endswith(suffix) and not compact.endswith(f"{suffix}{suffix}"):
            aliases.add(compact[: -len(suffix)] + suffix)
    if normalized.endswith("id"):
        base = normalized[:-2]
        aliases.add(base + "id")
        aliases.add(base + "identifier")
    if lexicon is not None:
        try:
            aliases.update(lexicon.aliases_for_column(column))
        except Exception:
            pass
    return {alias for alias in aliases if alias}


def safe_structural_alias(
    feature: str,
    candidates: list[dict[str, Any]],
    *,
    lexicon: "WorkspaceLexicon | None" = None,
) -> bool:
    feature_norm = normalize(feature)
    if risk_class(feature) != "structural":
        return False
    return any(
        feature_norm in aliases_for_column(candidate["column"], lexicon=lexicon)
        for candidate in candidates
    )


def candidate_labels(candidates: list[dict[str, Any]]) -> list[str]:
    return [f"{candidate['column']} ({candidate['dataset']})" for candidate in candidates[:5]]
