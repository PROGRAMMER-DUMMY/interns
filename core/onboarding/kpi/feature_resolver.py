"""Evidence-backed KPI feature resolution.

The first pass is intentionally conservative: it extracts identifiers from KPI
metric expressions, proves only direct schema matches, and blocks everything
else. A deeper pass can attach reusable derivation candidates, but candidates are
never executable proof.
"""
from __future__ import annotations

import argparse
import csv
import json
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from core.onboarding.features.blockers import (
    infer_join_candidates as infer_feature_join_candidates,
    normalize as normalize_blocker,
    prioritize_blockers as prioritize_feature_blockers,
)
from core.onboarding.features.derived_evidence import (
    derived_feature_options,
)
from core.onboarding.relationships.schema_alias_matching import (
    alias_index as build_alias_index,
    candidate_labels,
    load_schema_index,
    safe_structural_alias,
    source_columns,
)
from core.onboarding.memory.workspace_definitions import (
    READY_STATES,
    apply_workspace_definition as apply_workspace_feature_definition,
    apply_workspace_definitions_to_mapping,
    load_workspace_definitions,
    summarize_mapping,
)
from core.onboarding.memory.user_decisions import apply_user_decision as apply_kpi_user_decision
from core.onboarding.features.expression import (
    ExtractedExpression,
    extract_expression as parse_feature_expression,
)
from core.onboarding.features.derivation_search import (
    DerivationPatternSearcher,
    DerivationSearchInput,
)
from core.onboarding.kpi.blocker_question_panel import BlockerQuestionPanelBuilder
from core.storage.metadata_store import build_metadata_store
from core.storage.workspace_layout import WorkspaceLayout


GENERATOR_VERSION = 2
DERIVED_FEATURE_EVIDENCE_VERSION = 1
PLACEHOLDER_KPI_TERMS = {"confirm", "metric", "grain", "dimension", "dimensions"}
PLACEHOLDER_KPI_PHRASES = (
    "confirm metric",
    "confirm grain",
    "confirm grain and dimensions",
    "confirm business question",
    "what operational kpi should this dataset support",
)


@dataclass(frozen=True)
class ResolverResult:
    mapping_path: str
    open_questions_path: str
    question_panel_path: str
    question_panel_markdown_path: str
    kpi_count: int
    ready_kpi_count: int
    blocked_kpi_count: int
    unresolved_feature_count: int

    def summary(self) -> dict[str, Any]:
        return {
            "mapping_path": self.mapping_path,
            "open_questions_path": self.open_questions_path,
            "question_panel_path": self.question_panel_path,
            "question_panel_markdown_path": self.question_panel_markdown_path,
            "kpi_count": self.kpi_count,
            "ready_kpi_count": self.ready_kpi_count,
            "blocked_kpi_count": self.blocked_kpi_count,
            "unresolved_feature_count": self.unresolved_feature_count,
            "next_step": (
                f"Read {self.question_panel_markdown_path} before asking any KPI blocker question."
                if self.blocked_kpi_count
                else "No blocker question panel review is required."
            ),
        }


class KPIFeatureResolver:
    def __init__(
        self,
        repo_root: str | Path,
        workspace: str | Path,
        *,
        domain: str = "general",
        include_candidates: bool = False,
    ):
        self.repo_root = Path(repo_root).resolve()
        self.workspace = (self.repo_root / workspace).resolve()
        self.domain = domain
        self.include_candidates = include_candidates
        self.layout = WorkspaceLayout(project_root=self.workspace)
        self.searcher = DerivationPatternSearcher()
        self.metadata_store = build_metadata_store(self.layout, repo_root=self.repo_root)

    def run(self) -> ResolverResult:
        self._validate_workspace()
        self.layout.ensure_runtime_dirs()
        kpis = self._load_kpis()
        schema_index = self._schema_index()
        alias_index = self._alias_index(schema_index)
        available_columns = sorted({entry["column"] for entries in schema_index.values() for entry in entries})
        mapping = {
            "artifact_type": "kpi_feature_mapping.json",
            "version": GENERATOR_VERSION,
            "generated_by": "resolve-kpi-features",
            "workspace": _rel(self.workspace, self.repo_root),
            "mode": "candidate_enriched" if self.include_candidates else "first_pass",
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "derived_feature_evidence_contract": {
                "version": DERIVED_FEATURE_EVIDENCE_VERSION,
                "required_fields": [
                    "derived_column_name",
                    "business_meaning",
                    "formula",
                    "input_columns",
                    "example",
                    "evidence_sources",
                    "derivation_reasoning",
                    "evidence_state",
                    "confidence",
                    "needs_user_confirmation",
                ],
                "rule": "Candidate derivations are not ground truth until proven or user-confirmed.",
            },
            "states": [
                "proven_direct",
                "proven_alias",
                "proven_join",
                "proven_formula",
                "proven_taxonomy",
                "user_confirmed",
                "blocked_missing_evidence",
                "blocked_ambiguous",
                "candidate_unconfirmed",
                "candidate_pattern",
                "rejected",
            ],
            "kpis": [
                self._resolve_kpi(idx, kpi, schema_index, alias_index, available_columns)
                for idx, kpi in enumerate(kpis, start=1)
            ],
        }
        definitions = load_workspace_definitions(self.layout)
        if definitions.get("definitions"):
            apply_workspace_definitions_to_mapping(mapping, definitions)
        mapping["blocker_clusters"] = prioritize_blockers(mapping)
        summary = summarize_mapping(mapping)
        mapping["summary"] = summary
        mapping_path = self.layout.contracts_dir / "kpi_feature_mapping.json"
        mapping_path.write_text(json.dumps(mapping, indent=2, default=str) + "\n", encoding="utf-8")
        self._store_metadata("contracts", "kpi_feature_mapping", mapping)
        open_questions_path = self._write_open_questions(mapping)
        question_panel = BlockerQuestionPanelBuilder(
            self.repo_root,
            _rel(self.workspace, self.repo_root),
            mapping_path=mapping_path,
        ).run()
        return ResolverResult(
            mapping_path=_rel(mapping_path, self.repo_root),
            open_questions_path=open_questions_path,
            question_panel_path=question_panel.current_json,
            question_panel_markdown_path=question_panel.current_markdown,
            kpi_count=summary["kpi_count"],
            ready_kpi_count=summary["ready_kpi_count"],
            blocked_kpi_count=summary["blocked_kpi_count"],
            unresolved_feature_count=summary["unresolved_feature_count"],
        )

    def _resolve_kpi(
        self,
        idx: int,
        kpi: dict[str, Any],
        schema_index: dict[str, dict[str, str]],
        alias_index: dict[str, list[dict[str, str]]],
        available_columns: list[str],
    ) -> dict[str, Any]:
        metric = str(kpi.get("metric", "") or "")
        cuts = str(kpi.get("cuts", "") or "")
        expression_context = " ".join(value for value in [metric, cuts] if value)
        full_context = _kpi_context(kpi)
        extracted = extract_expression(expression_context)
        if _requires_kpi_definition(kpi, expression_context, extracted):
            feature = _kpi_definition_feature(kpi)
            return {
                "kpi_id": f"kpi_{idx:03d}",
                "name": kpi.get("name", ""),
                "source": kpi.get("source", ""),
                "metric": metric,
                "cuts": cuts,
                "status": "blocked_questions_pending",
                "features": [feature],
                "function_context": extracted.functions,
                "join_candidates": [],
                "open_questions": [feature["question"]],
            }
        features = []
        for token in extracted.identifiers:
            norm = normalize_blocker(token)
            contextual_candidates = contextual_column_candidates(token, full_context, schema_index)
            if contextual_candidates and contextual_candidates[0].get("auto_proven"):
                features.append(_contextual_feature(token, contextual_candidates, proven=True))
                continue
            if norm in schema_index:
                evidences = schema_index[norm]
                features.append({
                    "feature": token,
                    "state": "proven_direct",
                    "resolution_type": "direct_column",
                    "source_columns": source_columns(evidences),
                    "grain": "physical_column",
                    "conflicts": [],
                    "decision_history": [],
                    "evidence": [
                        {
                            "type": "schema_profile",
                            "source": evidence["dataset"],
                            "column": evidence["column"],
                        }
                        for evidence in evidences
                    ],
                    "candidate_patterns": [],
                    "candidates": [],
                    "question": None,
                })
                continue
            alias_candidates = alias_index.get(norm, [])
            if alias_candidates:
                state = "proven_alias" if safe_structural_alias(token, alias_candidates) else "candidate_unconfirmed"
                features.append({
                    "feature": token,
                    "state": state,
                    "resolution_type": "alias_column",
                    "source_columns": source_columns(alias_candidates),
                    "grain": "alias_requires_context_review" if state != "proven_alias" else "physical_column",
                    "conflicts": [],
                    "decision_history": [],
                    "evidence": [
                        {
                            "type": "schema_alias",
                            "source": candidate["dataset"],
                            "column": candidate["column"],
                            "reason": candidate["reason"],
                        }
                        for candidate in alias_candidates
                    ],
                    "candidate_patterns": [],
                    "candidates": [
                        {
                            "state": "candidate_unconfirmed" if state != "proven_alias" else "accepted",
                            "column": candidate["column"],
                            "source": candidate["dataset"],
                            "reason": candidate["reason"],
                        }
                        for candidate in alias_candidates
                    ],
                    "question": (
                        None
                        if state == "proven_alias"
                        else f"Should `{token}` use alias candidate(s): {', '.join(candidate_labels(alias_candidates))}?"
                    ),
                })
                continue
            if contextual_candidates:
                features.append(_contextual_feature(token, contextual_candidates, proven=False))
                continue
            candidate_patterns = []
            if self.include_candidates:
                candidate_patterns = [
                    candidate.as_dict()
                    for candidate in self.searcher.search(
                        DerivationSearchInput(
                            feature=token,
                            available_columns=available_columns,
                            expression_context=expression_context,
                            domain=self.domain,
                        )
                    )
                ]
            candidate_derived_options = derived_feature_options(
                token,
                candidate_patterns,
                schema_index,
                kpi,
                expression_context,
            )
            features.append({
                "feature": token,
                "state": "blocked_missing_evidence",
                "resolution_type": "unresolved",
                "source_columns": [],
                "grain": "unknown",
                "conflicts": [],
                "decision_history": [],
                "evidence": [],
                "candidate_patterns": candidate_patterns,
                "derived_feature_options": candidate_derived_options,
                "candidates": [],
                "question": f"What source, formula, or accepted rule should define `{token}` for this KPI?",
            })
        blocked = [feature for feature in features if feature.get("state") not in READY_STATES]
        return {
            "kpi_id": f"kpi_{idx:03d}",
            "name": kpi.get("name", ""),
            "source": kpi.get("source", ""),
            "metric": metric,
            "cuts": cuts,
            "status": "ready_for_sql" if not blocked and features else "blocked_questions_pending",
            "features": features,
            "function_context": extracted.functions,
            "join_candidates": infer_join_candidates(features),
            "open_questions": [feature["question"] for feature in blocked if feature.get("question")],
        }

    def _load_kpis(self) -> list[dict[str, Any]]:
        path = self.layout.contracts_dir / "kpi_registry.json"
        if not path.exists():
            return []
        data = json.loads(path.read_text(encoding="utf-8"))
        return list(data.get("kpis", []))

    def _schema_index(self) -> dict[str, list[dict[str, str]]]:
        schema_index = load_schema_index(self.layout.profiles_dir / "profile_index.json")
        dictionaries = self._load_data_dictionaries()
        if dictionaries:
            enrich_schema_index_with_dictionaries(schema_index, dictionaries)
        return schema_index

    def _alias_index(self, schema_index: dict[str, list[dict[str, str]]]) -> dict[str, list[dict[str, str]]]:
        return build_alias_index(schema_index)

    def _write_open_questions(self, mapping: dict[str, Any]) -> str:
        path = self.layout.reports_dir / "open_questions.md"
        existing = path.read_text(encoding="utf-8") if path.exists() else "# Open Questions\n"
        lines = [existing.rstrip(), "", "## KPI Feature Resolution Questions", ""]
        added = 0
        for kpi in mapping.get("kpis", []):
            questions = kpi.get("open_questions", [])
            if not questions:
                continue
            lines.append(f"### {kpi.get('kpi_id')} - {kpi.get('name')}")
            for question in questions:
                lines.append(f"- {question}")
                added += 1
            lines.append("")
        if not added:
            lines.append("- No blocking KPI feature questions detected.")
        path.write_text("\n".join(lines).rstrip() + "\n", encoding="utf-8")
        return _rel(path, self.repo_root)

    def _store_metadata(self, collection: str, document_id: str, payload: dict[str, Any]) -> None:
        self.metadata_store.upsert(
            collection,
            document_id,
            payload,
            workspace=str(self.workspace),
        )

    def _validate_workspace(self) -> None:
        if not self.workspace.exists():
            raise FileNotFoundError(f"workspace not found: {self.workspace}")
        if self.workspace == self.repo_root or not self.workspace.is_relative_to(self.repo_root):
            raise ValueError(f"workspace must be inside repo root: {self.workspace}")

    def _load_data_dictionaries(self) -> list[dict[str, Any]]:
        paths: list[Path] = []
        inventory_path = self.layout.requirements_dir / "input_inventory.json"
        if inventory_path.exists():
            try:
                inventory = json.loads(inventory_path.read_text(encoding="utf-8"))
            except json.JSONDecodeError:
                inventory = {}
            for item in inventory.get("data_models") or []:
                path = (self.repo_root / str(item)).resolve()
                if path.suffix.lower() == ".csv" and "dictionary" in path.stem.lower():
                    paths.append(path)
        paths.extend(
            path
            for path in self.workspace.rglob("*dictionary*.csv")
            if path.is_file() and "/interns/" not in path.as_posix()
        )
        rows: list[dict[str, Any]] = []
        for path in sorted(set(paths)):
            try:
                with path.open("r", encoding="utf-8-sig", newline="") as handle:
                    reader = csv.DictReader(handle)
                    fieldnames = {str(name).strip().lower(): name for name in (reader.fieldnames or [])}
                    table_key = _first_present(fieldnames, ["table", "entity", "dataset", "file"])
                    field_key = _first_present(fieldnames, ["field", "column", "name"])
                    description_key = _first_present(fieldnames, ["description", "definition", "meaning"])
                    if not table_key or not field_key:
                        continue
                    for row in reader:
                        table = str(row.get(table_key) or "").strip()
                        field = str(row.get(field_key) or "").strip()
                        if not table or not field:
                            continue
                        rows.append(
                            {
                                "table": table,
                                "field": field,
                                "description": str(row.get(description_key) or "").strip()
                                if description_key
                                else "",
                                "path": _rel(path, self.repo_root),
                            }
                        )
            except OSError:
                continue
        return rows


def extract_expression(expression: str) -> ExtractedExpression:
    return parse_feature_expression(expression)


def prioritize_blockers(mapping: dict[str, Any]) -> list[dict[str, Any]]:
    return prioritize_feature_blockers(mapping)


def infer_join_candidates(features: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return infer_feature_join_candidates(features)


def enrich_schema_index_with_dictionaries(
    schema_index: dict[str, list[dict[str, Any]]],
    dictionaries: list[dict[str, Any]],
) -> None:
    by_table_field: dict[tuple[str, str], dict[str, Any]] = {}
    by_field: dict[str, list[dict[str, Any]]] = {}
    for row in dictionaries:
        field_norm = normalize_blocker(str(row.get("field") or ""))
        table_norm = normalize_blocker(str(row.get("table") or ""))
        if not field_norm:
            continue
        by_field.setdefault(field_norm, []).append(row)
        if table_norm:
            by_table_field[(table_norm, field_norm)] = row

    for entries in schema_index.values():
        for entry in entries:
            column_norm = normalize_blocker(str(entry.get("column") or ""))
            dataset_table = normalize_blocker(Path(str(entry.get("dataset") or "")).stem)
            row = by_table_field.get((dataset_table, column_norm))
            if row is None:
                matches = by_field.get(column_norm) or []
                row = matches[0] if len(matches) == 1 else None
            if row is None:
                continue
            entry["dictionary_table"] = row.get("table", "")
            entry["dictionary_field"] = row.get("field", "")
            entry["dictionary_description"] = row.get("description", "")
            entry["dictionary_path"] = row.get("path", "")


def contextual_column_candidates(
    feature: str,
    full_context: str,
    schema_index: dict[str, list[dict[str, Any]]],
) -> list[dict[str, Any]]:
    feature_norm = normalize_blocker(feature)
    if not feature_norm or feature_norm in {"year", "quarter", "month", "day", "duration"}:
        return []
    if feature_norm not in {
        "cost",
        "amount",
        "paid",
        "coverage",
        "claim",
        "charge",
        "revenue",
        "procedure",
        "encounter",
    }:
        return []
    context_tokens = _semantic_tokens(full_context)
    if not context_tokens:
        return []
    context_norm = normalize_blocker(full_context)
    scored: list[dict[str, Any]] = []
    for entries in schema_index.values():
        for entry in entries:
            score, reasons = _contextual_score(feature_norm, context_tokens, context_norm, entry)
            if score < 8:
                continue
            scored.append(
                {
                    **entry,
                    "score": score,
                    "reason": "; ".join(reasons),
                }
            )
    scored.sort(
        key=lambda item: (
            -float(item.get("score", 0)),
            str(item.get("dataset") or ""),
            str(item.get("column") or ""),
        )
    )
    if not scored:
        return []
    top = scored[0]
    second = float(scored[1].get("score", 0)) if len(scored) > 1 else 0.0
    top_score = float(top.get("score", 0))
    auto_proven = top_score >= 14 and (len(scored) == 1 or top_score - second >= 4)
    limit = 1 if auto_proven else 5
    candidates = scored[:limit]
    if auto_proven:
        candidates[0]["auto_proven"] = True
    return candidates


def _contextual_feature(
    token: str,
    contextual_candidates: list[dict[str, Any]],
    *,
    proven: bool,
) -> dict[str, Any]:
    state = "proven_alias" if proven else "candidate_unconfirmed"
    return {
        "feature": token,
        "state": state,
        "resolution_type": (
            "contextual_dictionary_column"
            if state == "proven_alias"
            else "contextual_column_candidate"
        ),
        "source_columns": source_columns(contextual_candidates),
        "grain": "physical_column" if state == "proven_alias" else "alias_requires_context_review",
        "conflicts": [],
        "decision_history": [],
        "evidence": [
            {
                "type": "data_dictionary_context_match",
                "source": candidate["dataset"],
                "column": candidate["column"],
                "score": candidate.get("score"),
                "reason": candidate.get("reason"),
            }
            for candidate in contextual_candidates
        ],
        "candidate_patterns": [],
        "candidates": [
            {
                "state": "accepted" if state == "proven_alias" else "candidate_unconfirmed",
                "column": candidate["column"],
                "source": candidate["dataset"],
                "reason": candidate.get("reason", ""),
            }
            for candidate in contextual_candidates
        ],
        "question": (
            None
            if state == "proven_alias"
            else f"Should `{token}` use context/dictionary candidate(s): "
            f"{', '.join(candidate_labels(contextual_candidates))}?"
        ),
    }


def _contextual_score(
    feature_norm: str,
    context_tokens: set[str],
    context_norm: str,
    entry: dict[str, Any],
) -> tuple[float, list[str]]:
    column = str(entry.get("column") or "")
    dataset = Path(str(entry.get("dataset") or "")).stem
    dictionary_description = str(entry.get("dictionary_description") or "")
    dictionary_field = str(entry.get("dictionary_field") or column)
    column_norm = normalize_blocker(column)
    dataset_norm = normalize_blocker(dataset)
    if feature_norm == "encounter":
        if dataset_norm != "encounters" or column_norm != "id":
            return 0.0, []
        score = 24.0
        reasons = [
            "KPI asks for total encounters, so the encounter table primary key is the correct grain",
        ]
        if entry.get("dictionary_description"):
            reasons.append("data dictionary identifies encounters.Id as the encounter primary key")
        return score, reasons
    if feature_norm == "procedure":
        if dataset_norm not in {"procedure", "procedures"}:
            return 0.0, []
        if "reason" in column_norm or column_norm not in {"code", "description"}:
            return 0.0, []
    description_tokens = _semantic_tokens(dictionary_description)
    column_tokens = _semantic_tokens(_split_identifier(column))
    field_tokens = _semantic_tokens(_split_identifier(dictionary_field))
    dataset_tokens = _semantic_tokens(dataset)
    reasons: list[str] = []
    score = 0.0
    if feature_norm in column_norm:
        score += 6.0
        reasons.append(f"`{feature_norm}` appears in column `{column}`")
    if column_norm and column_norm in context_norm:
        score += 8.0
        reasons.append(f"KPI context explicitly contains column phrase `{column}`")
    if feature_norm == "procedure" and column_norm == "description":
        score += 4.0
        reasons.append("procedure grouping can use the procedure description label")
    elif feature_norm == "procedure" and column_norm == "code":
        score += 3.0
        reasons.append("procedure grouping can use the procedure code")
    if feature_norm and any(feature_norm in normalize_blocker(token) for token in description_tokens):
        score += 3.0
        reasons.append("data dictionary description mentions the feature")
    column_overlap = context_tokens.intersection(column_tokens.union(field_tokens))
    if column_overlap:
        score += 2.0 * len(column_overlap)
        reasons.append(f"context matches column terms: {', '.join(sorted(column_overlap))}")
    description_overlap = context_tokens.intersection(description_tokens)
    if description_overlap:
        score += min(4.0, float(len(description_overlap)))
        reasons.append("context overlaps data dictionary description")
    dataset_overlap = context_tokens.intersection(dataset_tokens)
    if dataset_overlap:
        score += 6.0
        reasons.append(f"context matches dataset/table `{dataset}`")
    dtype = str(entry.get("dtype") or "").lower()
    if feature_norm in {"cost", "amount", "paid", "coverage", "claim"} and any(
        token in dtype for token in ("int", "float", "double", "decimal")
    ):
        score += 2.0
        reasons.append("profile dtype is numeric")
    return score, reasons


def _semantic_tokens(value: str) -> set[str]:
    tokens = {
        token
        for token in re.findall(r"[a-z0-9]+", _split_identifier(value).lower())
        if len(token) > 1
    }
    tokens.update(token[:-1] for token in list(tokens) if len(token) > 3 and token.endswith("s"))
    return tokens.difference(
        {
            "and",
            "for",
            "the",
            "each",
            "what",
            "which",
            "with",
            "from",
            "data",
            "key",
            "primary",
            "foreign",
            "unique",
            "identifier",
        }
    )


def _split_identifier(value: str) -> str:
    value = re.sub(r"([a-z0-9])([A-Z])", r"\1 \2", str(value))
    return value.replace("_", " ").replace("-", " ")


def _kpi_context(kpi: dict[str, Any]) -> str:
    return " ".join(
        str(value or "")
        for value in [
            kpi.get("name"),
            kpi.get("description"),
            kpi.get("metric"),
            kpi.get("cuts"),
            kpi.get("refinement_required"),
        ]
    )


def _first_present(mapping: dict[str, str], candidates: list[str]) -> str | None:
    for candidate in candidates:
        if candidate in mapping:
            return mapping[candidate]
    return None


def _requires_kpi_definition(
    kpi: dict[str, Any],
    expression_context: str,
    extracted: ExtractedExpression,
) -> bool:
    text = " ".join(
        str(value or "")
        for value in [
            kpi.get("name"),
            kpi.get("description"),
            expression_context,
            kpi.get("refinement_required"),
        ]
    ).lower()
    has_placeholder_phrase = any(phrase in text for phrase in PLACEHOLDER_KPI_PHRASES)
    if not has_placeholder_phrase:
        return False
    identifiers = {identifier.lower() for identifier in extracted.identifiers}
    return not identifiers or identifiers.issubset(PLACEHOLDER_KPI_TERMS)


def _kpi_definition_feature(kpi: dict[str, Any]) -> dict[str, Any]:
    return {
        "feature": "KPI definition",
        "state": "blocked_missing_evidence",
        "resolution_type": "kpi_definition_required",
        "source_columns": [],
        "grain": "undefined",
        "conflicts": [],
        "decision_history": [],
        "evidence": [
            {
                "type": "kpi_registry_placeholder",
                "source": kpi.get("source", ""),
                "refinement_required": kpi.get("refinement_required", ""),
            }
        ],
        "candidate_patterns": [],
        "derived_feature_options": [],
        "candidates": [],
        "question": (
            "Which concrete business question, metric expression, grain/dimensions, owner, "
            "and acceptance tests should replace this seed KPI?"
        ),
    }


def apply_user_decision(
    repo_root: str | Path,
    workspace: str | Path,
    *,
    kpi_id: str,
    feature: str,
    state: str,
    resolution_type: str,
    evidence_note: str,
    source_columns: list[str] | None = None,
) -> dict[str, Any]:
    return apply_kpi_user_decision(
        repo_root,
        workspace,
        kpi_id=kpi_id,
        feature=feature,
        state=state,
        resolution_type=resolution_type,
        evidence_note=evidence_note,
        source_columns=source_columns,
    )


def apply_workspace_definition(
    repo_root: str | Path,
    workspace: str | Path,
    *,
    feature: str,
    state: str,
    resolution_type: str,
    evidence_note: str,
    definition: str = "",
    source_columns: list[str] | None = None,
    applies_to_kpis: list[str] | None = None,
    exceptions: list[str] | None = None,
) -> dict[str, Any]:
    return apply_workspace_feature_definition(
        repo_root,
        workspace,
        feature=feature,
        state=state,
        resolution_type=resolution_type,
        evidence_note=evidence_note,
        definition=definition,
        source_columns=source_columns,
        applies_to_kpis=applies_to_kpis,
        exceptions=exceptions,
    )


def _rel(path: Path, root: Path) -> str:
    try:
        return str(path.resolve().relative_to(root.resolve())).replace("\\", "/")
    except ValueError:
        return str(path)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Resolve KPI features from generated workspace evidence.")
    parser.add_argument("--workspace", required=True, help="Workspace path relative to repo root.")
    parser.add_argument("--repo-root", default=".", help="Repository root. Defaults to current directory.")
    parser.add_argument("--domain", default="general", help="Domain used for derivation candidate search.")
    parser.add_argument("--apply-decision", action="store_true", help="Apply a user-confirmed feature decision.")
    parser.add_argument(
        "--apply-workspace-definition",
        action="store_true",
        help="Apply a reusable workspace-level feature definition to all matching KPIs.",
    )
    parser.add_argument("--kpi-id", help="KPI id for --apply-decision.")
    parser.add_argument("--feature", help="Feature name for --apply-decision.")
    parser.add_argument("--state", default="user_confirmed", help="Decision state for --apply-decision.")
    parser.add_argument("--resolution-type", default="user_confirmed", help="Resolution type for --apply-decision.")
    parser.add_argument("--evidence-note", help="Evidence note for --apply-decision.")
    parser.add_argument("--definition", default="", help="Reusable feature definition text.")
    parser.add_argument(
        "--applies-to-kpi",
        action="append",
        default=[],
        help="Limit --apply-workspace-definition to a KPI id. May be passed multiple times.",
    )
    parser.add_argument(
        "--exception-kpi",
        action="append",
        default=[],
        help="Exclude a KPI id from --apply-workspace-definition. May be passed multiple times.",
    )
    parser.add_argument(
        "--source-column",
        action="append",
        default=[],
        help="Source column for --apply-decision. May be passed multiple times.",
    )
    parser.add_argument(
        "--include-candidates",
        action="store_true",
        help="Attach reusable derivation pattern candidates to unresolved features.",
    )
    args = parser.parse_args(argv)
    if args.apply_decision:
        missing = [
            name
            for name, value in {
                "--kpi-id": args.kpi_id,
                "--feature": args.feature,
                "--evidence-note": args.evidence_note,
            }.items()
            if not value
        ]
        if missing:
            raise SystemExit(f"Missing required arguments for --apply-decision: {', '.join(missing)}")
        summary = apply_user_decision(
            args.repo_root,
            args.workspace,
            kpi_id=args.kpi_id,
            feature=args.feature,
            state=args.state,
            resolution_type=args.resolution_type,
            evidence_note=args.evidence_note,
            source_columns=args.source_column,
        )
        print(json.dumps(summary, indent=2))
        return 0
    if args.apply_workspace_definition:
        missing = [
            name
            for name, value in {
                "--feature": args.feature,
                "--evidence-note": args.evidence_note,
            }.items()
            if not value
        ]
        if missing:
            raise SystemExit(
                "Missing required arguments for --apply-workspace-definition: "
                + ", ".join(missing)
            )
        summary = apply_workspace_definition(
            args.repo_root,
            args.workspace,
            feature=args.feature,
            state=args.state,
            resolution_type=args.resolution_type,
            evidence_note=args.evidence_note,
            definition=args.definition,
            source_columns=args.source_column,
            applies_to_kpis=args.applies_to_kpi,
            exceptions=args.exception_kpi,
        )
        print(json.dumps(summary, indent=2))
        return 0
    result = KPIFeatureResolver(
        args.repo_root,
        args.workspace,
        domain=args.domain,
        include_candidates=args.include_candidates,
    ).run()
    print(json.dumps(result.summary(), indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
