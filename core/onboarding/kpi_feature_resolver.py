"""Evidence-backed KPI feature resolution.

The first pass is intentionally conservative: it extracts identifiers from KPI
metric expressions, proves only direct schema matches, and blocks everything
else. A deeper pass can attach reusable derivation candidates, but candidates are
never executable proof.
"""
from __future__ import annotations

import argparse
import json
import re
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from core.onboarding.derivation_search import (
    PATTERNS_PATH,
    DerivationPatternSearcher,
    DerivationSearchInput,
)
from core.onboarding.blocker_question_panel import BlockerQuestionPanelBuilder
from core.storage.metadata_store import build_metadata_store
from core.storage.workspace_layout import WorkspaceLayout


SQL_KEYWORDS = {
    "and",
    "as",
    "asc",
    "between",
    "by",
    "case",
    "cast",
    "desc",
    "distinct",
    "else",
    "end",
    "false",
    "from",
    "group",
    "having",
    "in",
    "is",
    "like",
    "not",
    "null",
    "or",
    "order",
    "over",
    "partition",
    "select",
    "then",
    "true",
    "when",
    "where",
}
BUSINESS_TEXT_STOPWORDS = {
    "across",
    "above",
    "commercial",
    "for",
    "medicaid",
    "medicare",
    "of",
    "percentage",
    "share",
    "top",
    "trend",
    "with",
}
COMMON_FUNCTIONS = {
    "avg",
    "coalesce",
    "count",
    "date_diff",
    "datediff",
    "floor",
    "greatest",
    "lower",
    "max",
    "min",
    "month",
    "nullif",
    "percentile",
    "round",
    "sum",
    "upper",
}
STRUCTURAL_ALIAS_SUFFIXES = ("id", "code", "date", "type", "status")
JOIN_KEY_SUFFIXES = ("id", "code")
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
GENERATOR_VERSION = 2
WORKSPACE_DEFINITIONS_VERSION = 1
DERIVED_FEATURE_EVIDENCE_VERSION = 1


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


@dataclass(frozen=True)
class ExtractedExpression:
    identifiers: list[str]
    functions: list[dict[str, Any]] = field(default_factory=list)


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
            "version": GENERATOR_VERSION,
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
        definitions = _load_workspace_definitions(self.layout)
        if definitions.get("definitions"):
            _apply_workspace_definitions_to_mapping(mapping, definitions)
        mapping["blocker_clusters"] = prioritize_blockers(mapping)
        summary = _summarize(mapping)
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
        extracted = extract_expression(expression_context)
        features = []
        for token in extracted.identifiers:
            norm = _norm(token)
            if norm in schema_index:
                evidences = schema_index[norm]
                features.append({
                    "feature": token,
                    "state": "proven_direct",
                    "resolution_type": "direct_column",
                    "source_columns": _source_columns(evidences),
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
                state = "proven_alias" if _safe_structural_alias(token, alias_candidates) else "candidate_unconfirmed"
                features.append({
                    "feature": token,
                    "state": state,
                    "resolution_type": "alias_column",
                    "source_columns": _source_columns(alias_candidates),
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
                        else f"Should `{token}` use alias candidate(s): {', '.join(_candidate_labels(alias_candidates))}?"
                    ),
                })
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
            derived_feature_options = _derived_feature_options(
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
                "derived_feature_options": derived_feature_options,
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
        path = self.layout.profiles_dir / "profile_index.json"
        if not path.exists():
            return {}
        data = json.loads(path.read_text(encoding="utf-8"))
        index: dict[str, list[dict[str, str]]] = {}
        for profile in data.get("profiles", []):
            dataset = profile.get("path", "")
            row_count = profile.get("row_count")
            for column, dtype in (profile.get("schema") or {}).items():
                index.setdefault(_norm(column), []).append(
                    {
                        "dataset": dataset,
                        "column": column,
                        "dtype": str(dtype),
                        "row_count": row_count,
                        "profile_path": profile.get("profile_path"),
                        **_column_profile_summary(profile, column),
                    }
                )
        return index

    def _alias_index(self, schema_index: dict[str, list[dict[str, str]]]) -> dict[str, list[dict[str, str]]]:
        aliases: dict[str, list[dict[str, str]]] = defaultdict(list)
        for entries in schema_index.values():
            for entry in entries:
                column = entry["column"]
                norm = _norm(column)
                for alias in _aliases_for_column(column):
                    if alias != norm:
                        aliases[alias].append({**entry, "reason": "normalized_structural_alias"})
        return dict(aliases)

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


def extract_expression(expression: str) -> ExtractedExpression:
    cleaned = _strip_literals(expression)
    function_names = _function_names(cleaned)
    identifiers = []
    seen = set()
    for token in re.findall(r"\b[A-Za-z_][A-Za-z0-9_]*\b", cleaned):
        token_norm = token.lower()
        if (
            token_norm in SQL_KEYWORDS
            or token_norm in COMMON_FUNCTIONS
            or token_norm in BUSINESS_TEXT_STOPWORDS
            or token in function_names
        ):
            continue
        if token_norm not in seen:
            identifiers.append(token)
            seen.add(token_norm)
    return ExtractedExpression(
        identifiers=identifiers,
        functions=_function_contexts(cleaned),
    )


def prioritize_blockers(mapping: dict[str, Any]) -> list[dict[str, Any]]:
    counts: Counter[str] = Counter()
    examples: dict[str, list[str]] = defaultdict(list)
    for kpi in mapping.get("kpis", []):
        for feature in kpi.get("features", []):
            if str(feature.get("state", "")).startswith("blocked") or feature.get("state") == "candidate_unconfirmed":
                name = str(feature.get("feature", ""))
                counts[name] += 1
                if len(examples[name]) < 3:
                    examples[name].append(kpi.get("kpi_id", ""))
    clusters = []
    for feature, count in counts.items():
        risk = _risk_class(feature)
        clusters.append(
            {
                "feature": feature,
                "count": count,
                "risk": risk,
                "priority_score": _risk_score(risk) * 100 + count,
                "example_kpis": examples[feature],
                "recommended_question": _question_for_feature(feature, risk),
            }
        )
    clusters.sort(key=lambda item: (-item["priority_score"], item["feature"]))
    return clusters


def infer_join_candidates(features: list[dict[str, Any]]) -> list[dict[str, Any]]:
    by_name: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for feature in features:
        for column in feature.get("source_columns", []):
            normalized = _norm(column.get("column", ""))
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


def _derived_feature_options(
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
            _derived_input_column(input_name, column, schema_index)
            for input_name, column in selected_bindings.items()
        ]
        formula_templates = {
            engine: _fill_formula_template(template, selected_bindings)
            for engine, template in (pattern.get("templates") or {}).items()
        }
        formula = formula_templates.get("duckdb_sql") or next(iter(formula_templates.values()), "")
        example = _derived_feature_example(feature, pattern, selected_bindings, input_columns, formula)
        evidence_sources = _derived_evidence_sources(feature, pattern, input_columns, kpi, expression_context)
        options.append(
            {
                "derived_column_name": feature,
                "business_meaning": pattern.get("description", ""),
                "formula": formula,
                "formula_templates": formula_templates,
                "input_columns": input_columns,
                "example": example,
                "evidence_sources": evidence_sources,
                "derivation_reasoning": _derivation_reasoning(feature, pattern, missing_inputs, input_columns),
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


def _derived_input_column(
    input_name: str,
    column: str,
    schema_index: dict[str, list[dict[str, Any]]],
) -> dict[str, Any]:
    evidences = schema_index.get(_norm(column), [])
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
        "value_profile": _value_profile(evidence),
        "semantic_meaning_sources": _semantic_meaning_sources(input_name, column, evidence),
        "reason": _input_column_reason(input_name, column, evidence),
        "example_value": _example_value(input_name, evidence),
    }


def _semantic_meaning_sources(
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


def _input_column_reason(input_name: str, column: str, evidence: dict[str, Any]) -> str:
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


def _derivation_reasoning(
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
        "why_not_ground_truth": (
            "No source artifact explicitly defines this derived column with this formula."
        ),
        "remaining_risk": risk,
    }


def _derived_evidence_sources(
    feature: str,
    pattern: dict[str, Any],
    input_columns: list[dict[str, Any]],
    kpi: dict[str, Any],
    expression_context: str,
) -> list[dict[str, Any]]:
    sources = [
        {
            "file": _repo_style_path(PATTERNS_PATH),
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


def _derived_feature_example(
    feature: str,
    pattern: dict[str, Any],
    selected_bindings: dict[str, str],
    input_columns: list[dict[str, Any]],
    formula: str,
) -> dict[str, Any]:
    pattern_id = str(pattern.get("pattern_id") or "")
    by_input = {column["input_name"]: column.get("example_value") for column in input_columns}
    example_inputs = dict(by_input)
    example_inputs.update(_pattern_example_inputs(pattern_id, selected_bindings))
    output_value = _pattern_example_output(feature, pattern_id, example_inputs)
    return {
        "example_type": "synthetic_formula_example",
        "input": {
            column_name: example_inputs.get(input_name)
            for input_name, column_name in selected_bindings.items()
        },
        "output": {feature: output_value},
        "substituted_formula": _substitute_formula_values(formula, selected_bindings, example_inputs),
        "warning": "Example demonstrates formula mechanics only; it is not workspace ground truth.",
    }


def _pattern_example_inputs(pattern_id: str, selected_bindings: dict[str, str]) -> dict[str, Any]:
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


def _pattern_example_output(feature: str, pattern_id: str, example_inputs: dict[str, Any]) -> Any:
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


def _substitute_formula_values(
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


def _fill_formula_template(template: str, selected_bindings: dict[str, str]) -> str:
    filled = template
    for input_name, column in selected_bindings.items():
        filled = filled.replace("{" + input_name + "}", column)
    return filled


def _example_value(input_name: str, evidence: dict[str, Any]) -> Any:
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


def _column_profile_summary(profile: dict[str, Any], column: str) -> dict[str, Any]:
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


def _value_profile(evidence: dict[str, Any]) -> dict[str, Any]:
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


def _repo_style_path(path: Path) -> str:
    return path.as_posix()


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
    root = Path(repo_root).resolve()
    workspace_path = (root / workspace).resolve()
    layout = WorkspaceLayout(project_root=workspace_path)
    mapping_path = layout.contracts_dir / "kpi_feature_mapping.json"
    mapping = json.loads(mapping_path.read_text(encoding="utf-8"))
    allowed = {"user_confirmed", "rejected", "blocked_ambiguous", "blocked_missing_evidence"}
    if state not in allowed and not state.startswith("proven_"):
        raise ValueError(f"Unsupported decision state: {state}")
    updated = False
    for kpi in mapping.get("kpis", []):
        if kpi.get("kpi_id") != kpi_id:
            continue
        for item in kpi.get("features", []):
            if item.get("feature") != feature:
                continue
            item["state"] = state
            item["resolution_type"] = resolution_type
            item.setdefault("evidence", []).append(
                {
                    "type": "user_confirmed" if state == "user_confirmed" else "user_decision",
                    "source": "cli",
                    "detail": evidence_note,
                    "timestamp": datetime.now(timezone.utc).isoformat(),
                }
            )
            if source_columns:
                item["source_columns"] = [
                    {"dataset": "", "column": column, "source": "user_decision"}
                    for column in source_columns
                ]
            item.setdefault("decision_history", []).append(
                {
                    "state": state,
                    "resolution_type": resolution_type,
                    "note": evidence_note,
                    "timestamp": datetime.now(timezone.utc).isoformat(),
                }
            )
            item["question"] = None if state == "user_confirmed" or state.startswith("proven_") else item.get("question")
            updated = True
    if not updated:
        raise ValueError(f"Feature not found: {kpi_id}/{feature}")
    for kpi in mapping.get("kpis", []):
        blocked = [
            feature
            for feature in kpi.get("features", [])
            if feature.get("state") not in READY_STATES
        ]
        kpi["status"] = "ready_for_sql" if not blocked and kpi.get("features") else "blocked_questions_pending"
        kpi["open_questions"] = [
            feature.get("question")
            for feature in blocked
            if feature.get("question")
        ]
    mapping["summary"] = _summarize(mapping)
    mapping["blocker_clusters"] = prioritize_blockers(mapping)
    mapping_path.write_text(json.dumps(mapping, indent=2, default=str) + "\n", encoding="utf-8")
    _append_decision_history(layout, kpi_id, feature, state, evidence_note)
    requirements_path = layout.requirements_dir / "requirements.json"
    requirements_path.parent.mkdir(parents=True, exist_ok=True)
    requirements = {}
    if requirements_path.exists():
        try:
            requirements = json.loads(requirements_path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            requirements = {}
    requirements.setdefault("user_confirmed_feature_decisions", []).append(
        {
            "kpi_id": kpi_id,
            "feature": feature,
            "state": state,
            "resolution_type": resolution_type,
            "note": evidence_note,
            "timestamp": datetime.now(timezone.utc).isoformat(),
        }
    )
    requirements_path.write_text(json.dumps(requirements, indent=2) + "\n", encoding="utf-8")
    return mapping["summary"]


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
    root = Path(repo_root).resolve()
    workspace_path = (root / workspace).resolve()
    layout = WorkspaceLayout(project_root=workspace_path)
    mapping_path = layout.contracts_dir / "kpi_feature_mapping.json"
    if not mapping_path.exists():
        raise FileNotFoundError(f"KPI feature mapping not found: {mapping_path}")
    mapping = json.loads(mapping_path.read_text(encoding="utf-8"))
    allowed = {"user_confirmed", "rejected", "blocked_ambiguous", "blocked_missing_evidence"}
    if state not in allowed and not state.startswith("proven_"):
        raise ValueError(f"Unsupported decision state: {state}")

    timestamp = datetime.now(timezone.utc).isoformat()
    definitions = _load_workspace_definitions(layout)
    definition_record = {
        "feature": feature,
        "status": state,
        "state": state,
        "scope": "workspace",
        "resolution_type": resolution_type,
        "definition": definition or evidence_note,
        "applies_to_kpis": list(applies_to_kpis or []),
        "exceptions": list(exceptions or []),
        "source_columns": [
            {"dataset": "", "column": column, "source": "workspace_definition"}
            for column in (source_columns or [])
        ],
        "evidence": [
            {
                "type": "user_confirmed" if state == "user_confirmed" else "user_decision",
                "source": "cli",
                "detail": evidence_note,
                "timestamp": timestamp,
            }
        ],
        "verification_status": "needs_data_validation",
        "updated_at": timestamp,
    }
    _upsert_workspace_definition(definitions, definition_record)
    definitions_path = _workspace_definitions_path(layout)
    definitions_path.parent.mkdir(parents=True, exist_ok=True)
    definitions["workspace"] = mapping.get("workspace", str(workspace))
    definitions["updated_at"] = timestamp
    definitions_path.write_text(json.dumps(definitions, indent=2, default=str) + "\n", encoding="utf-8")

    updated = _apply_workspace_definition_to_mapping(mapping, definition_record)
    if not updated:
        raise ValueError(f"Feature not found in mapping: {feature}")
    _recompute_mapping_status(mapping)
    mapping_path.write_text(json.dumps(mapping, indent=2, default=str) + "\n", encoding="utf-8")
    _append_decision_history(layout, "workspace", feature, state, evidence_note)
    _append_workspace_definition_requirement(
        layout,
        feature=feature,
        state=state,
        resolution_type=resolution_type,
        definition=definition or evidence_note,
        evidence_note=evidence_note,
        applies_to_kpis=applies_to_kpis or [],
    )
    return mapping["summary"]


READY_STATES = {
    "proven_direct",
    "proven_alias",
    "proven_join",
    "proven_formula",
    "proven_taxonomy",
    "user_confirmed",
}


def _workspace_definitions_path(layout: WorkspaceLayout) -> Path:
    return layout.contracts_dir / "workspace_feature_definitions.json"


def _load_workspace_definitions(layout: WorkspaceLayout) -> dict[str, Any]:
    path = _workspace_definitions_path(layout)
    if not path.exists():
        return {
            "version": WORKSPACE_DEFINITIONS_VERSION,
            "workspace": "",
            "definitions": [],
        }
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return {
            "version": WORKSPACE_DEFINITIONS_VERSION,
            "workspace": "",
            "definitions": [],
        }
    data.setdefault("version", WORKSPACE_DEFINITIONS_VERSION)
    data.setdefault("definitions", [])
    return data


def _upsert_workspace_definition(definitions: dict[str, Any], record: dict[str, Any]) -> None:
    target = _norm(record.get("feature", ""))
    kept = [
        item
        for item in definitions.get("definitions", [])
        if _norm(item.get("feature", "")) != target
    ]
    kept.append(record)
    kept.sort(key=lambda item: _norm(item.get("feature", "")))
    definitions["definitions"] = kept


def _apply_workspace_definitions_to_mapping(mapping: dict[str, Any], definitions: dict[str, Any]) -> int:
    updated = 0
    for definition in definitions.get("definitions", []):
        updated += _apply_workspace_definition_to_mapping(mapping, definition)
    if updated:
        _recompute_mapping_status(mapping)
    return updated


def _apply_workspace_definition_to_mapping(mapping: dict[str, Any], definition: dict[str, Any]) -> int:
    updated = 0
    feature_name = str(definition.get("feature", ""))
    applies_to = set(definition.get("applies_to_kpis") or [])
    exceptions = set(definition.get("exceptions") or [])
    for kpi in mapping.get("kpis", []):
        kpi_id = str(kpi.get("kpi_id", ""))
        if applies_to and kpi_id not in applies_to:
            continue
        if kpi_id in exceptions:
            continue
        for item in kpi.get("features", []):
            if _norm(item.get("feature", "")) != _norm(feature_name):
                continue
            if item.get("state") in READY_STATES and item.get("state") != "user_confirmed":
                continue
            _apply_definition_to_feature(item, definition, kpi_id)
            updated += 1
    return updated


def _apply_definition_to_feature(item: dict[str, Any], definition: dict[str, Any], kpi_id: str) -> None:
    state = definition.get("state") or definition.get("status") or "user_confirmed"
    item["state"] = state
    item["resolution_type"] = definition.get("resolution_type", "workspace_definition")
    item["grain"] = definition.get("grain", item.get("grain", "workspace_defined"))
    if definition.get("source_columns"):
        item["source_columns"] = definition["source_columns"]
    item.setdefault("evidence", []).append(
        {
            "type": "workspace_feature_definition",
            "source": "workspace_feature_definitions.json",
            "feature": definition.get("feature"),
            "detail": definition.get("definition", ""),
            "verification_status": definition.get("verification_status", "needs_data_validation"),
        }
    )
    item.setdefault("decision_history", []).append(
        {
            "state": state,
            "resolution_type": definition.get("resolution_type", "workspace_definition"),
            "note": f"Applied workspace definition to {kpi_id}.",
            "timestamp": datetime.now(timezone.utc).isoformat(),
        }
    )
    if state in READY_STATES or str(state).startswith("proven_"):
        item["question"] = None


def _recompute_mapping_status(mapping: dict[str, Any]) -> None:
    for kpi in mapping.get("kpis", []):
        blocked = [
            feature
            for feature in kpi.get("features", [])
            if feature.get("state") not in READY_STATES
        ]
        kpi["status"] = "ready_for_sql" if not blocked and kpi.get("features") else "blocked_questions_pending"
        kpi["open_questions"] = [
            feature.get("question")
            for feature in blocked
            if feature.get("question")
        ]
        kpi["join_candidates"] = infer_join_candidates(kpi.get("features", []))
    mapping["summary"] = _summarize(mapping)
    mapping["blocker_clusters"] = prioritize_blockers(mapping)


def _append_decision_history(layout: WorkspaceLayout, kpi_id: str, feature: str, state: str, note: str) -> None:
    path = layout.memory_dir / "decision_history.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(
            "\n".join(
                [
                    f"## {datetime.now(timezone.utc).date()} KPI Feature Decision",
                    "",
                    f"- KPI: `{kpi_id}`",
                    f"- Feature: `{feature}`",
                    f"- State: `{state}`",
                    f"- Evidence: {note}",
                    "",
                ]
            )
        )


def _append_workspace_definition_requirement(
    layout: WorkspaceLayout,
    *,
    feature: str,
    state: str,
    resolution_type: str,
    definition: str,
    evidence_note: str,
    applies_to_kpis: list[str],
) -> None:
    requirements_path = layout.requirements_dir / "requirements.json"
    requirements_path.parent.mkdir(parents=True, exist_ok=True)
    requirements = {}
    if requirements_path.exists():
        try:
            requirements = json.loads(requirements_path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            requirements = {}
    requirements.setdefault("workspace_feature_definitions", []).append(
        {
            "feature": feature,
            "state": state,
            "resolution_type": resolution_type,
            "definition": definition,
            "evidence_note": evidence_note,
            "applies_to_kpis": applies_to_kpis,
            "timestamp": datetime.now(timezone.utc).isoformat(),
        }
    )
    requirements_path.write_text(json.dumps(requirements, indent=2) + "\n", encoding="utf-8")


def _source_columns(evidences: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [
        {
            "dataset": evidence["dataset"],
            "column": evidence["column"],
            "dtype": evidence.get("dtype"),
            "row_count": evidence.get("row_count"),
            "profile_path": evidence.get("profile_path"),
            "observed_values": evidence.get("sample_values") or [],
            "value_profile": _value_profile(evidence),
            "semantic_meaning_sources": _semantic_meaning_sources(
                "physical_column_candidate",
                evidence["column"],
                evidence,
            ),
            "reason": (
                "Column is present in generated profile evidence and matched the KPI term "
                "directly or through a configured alias."
            ),
        }
        for evidence in evidences
    ]


def _aliases_for_column(column: str) -> set[str]:
    normalized = _norm(column)
    aliases = {normalized}
    aliases.update(BUSINESS_COLUMN_ALIASES.get(normalized, set()))
    compact = normalized
    for suffix in STRUCTURAL_ALIAS_SUFFIXES:
        if compact.endswith(suffix) and not compact.endswith(f"{suffix}{suffix}"):
            aliases.add(compact[:-len(suffix)] + suffix)
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


def _safe_structural_alias(feature: str, candidates: list[dict[str, str]]) -> bool:
    feature_norm = _norm(feature)
    if _risk_class(feature) != "structural":
        return False
    return any(feature_norm in _aliases_for_column(candidate["column"]) for candidate in candidates)


def _candidate_labels(candidates: list[dict[str, str]]) -> list[str]:
    return [f"{candidate['column']} ({candidate['dataset']})" for candidate in candidates[:5]]


def _risk_class(feature: str) -> str:
    value = _norm(feature)
    financial_terms = ("amount", "paid", "payor", "payer", "denied", "allowed", "balance", "cost", "revenue", "margin", "refund", "charge")
    temporal_terms = ("date", "day", "month", "age", "aging", "admit", "discharge")
    if any(term in value for term in financial_terms):
        return "financial_correctness"
    if any(term in value for term in temporal_terms):
        return "temporal_correctness"
    if value.endswith(JOIN_KEY_SUFFIXES) or any(term in value for term in ("department", "dept", "provider", "patient", "claim", "encounter")):
        return "structural"
    return "business_semantics"


def _risk_score(risk: str) -> int:
    return {
        "financial_correctness": 4,
        "temporal_correctness": 3,
        "business_semantics": 2,
        "structural": 1,
    }.get(risk, 1)


def _question_for_feature(feature: str, risk: str) -> str:
    if risk == "financial_correctness":
        return f"What authoritative source or accepted rule defines `{feature}` and its grain?"
    if risk == "temporal_correctness":
        return f"What temporal anchor/source defines `{feature}`?"
    return f"What source, formula, or accepted rule should define `{feature}`?"


def _strip_literals(expression: str) -> str:
    expression = re.sub(r"'(?:''|[^'])*'", " ", expression)
    expression = re.sub(r'"(?:""|[^"])*"', " ", expression)
    expression = re.sub(r"=\s*[A-Za-z_][A-Za-z0-9_]*", "= ", expression)
    return re.sub(r"\b\d+(?:\.\d+)?\b", " ", expression)


def _function_names(expression: str) -> set[str]:
    return set(re.findall(r"\b([A-Za-z_][A-Za-z0-9_]*)\s*\(", expression))


def _function_contexts(expression: str) -> list[dict[str, Any]]:
    contexts = []
    for match in re.finditer(r"\b([A-Za-z_][A-Za-z0-9_]*)\s*\(([^()]*)\)", expression):
        name = match.group(1)
        args = [
            token
            for token in re.findall(r"\b[A-Za-z_][A-Za-z0-9_]*\b", match.group(2))
            if token.lower() not in SQL_KEYWORDS
        ]
        contexts.append({"function": name, "arguments": args})
    return contexts


def _summarize(mapping: dict[str, Any]) -> dict[str, int]:
    kpis = mapping.get("kpis", [])
    ready = [kpi for kpi in kpis if kpi.get("status") == "ready_for_sql"]
    unresolved = 0
    for kpi in kpis:
        unresolved += sum(
            1
            for feature in kpi.get("features", [])
            if feature.get("state") not in READY_STATES
        )
    return {
        "kpi_count": len(kpis),
        "ready_kpi_count": len(ready),
        "blocked_kpi_count": len(kpis) - len(ready),
        "unresolved_feature_count": unresolved,
    }


def _norm(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", "", str(value).lower())


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
