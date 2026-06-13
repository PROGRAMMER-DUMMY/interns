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
from core.onboarding.lexicon import load_workspace_lexicon
from core.onboarding.memory.workspace_definitions import (
    READY_STATES,
    apply_definition_to_feature,
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
from core.onboarding.documents.dictionary_reconciliation import (
    apply_conflicts_to_mapping as apply_dictionary_conflicts_to_mapping,
    load_data_dictionary_rows,
    write_dictionary_conflicts_contract,
)
from core.storage.metadata_store import build_metadata_store
from core.storage.workspace_layout import WorkspaceLayout
from core.contracts.versioning import register_contract


GENERATOR_VERSION = 2

register_contract("kpi_feature_mapping.json", current_version=GENERATOR_VERSION)
DERIVED_FEATURE_EVIDENCE_VERSION = 1
NO_SUPPORTING_EVIDENCE_LABEL = "no_supporting_evidence"
# Evidence entry types that only restate the KPI itself (its prose, its seed
# placeholder, its machine-derived metric). Every OTHER evidence type anchors
# to something the workspace actually contains (a profiled column, a dataset,
# a dictionary entry, a relationship, an accepted definition, ...).
_SELF_REFERENTIAL_EVIDENCE_TYPES = {
    "kpi_registry_placeholder",
    "kpi_prose",
    "derived_metric_provenance",
    "no_supporting_evidence_scan",
}
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
        self._lexicon = load_workspace_lexicon(self.layout)
        self._column_identity = _column_identity_groups(self._load_relationships())
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
        _label_kpis_without_supporting_evidence(mapping, kpis, schema_index, definitions)
        # Dictionary-vs-profile reconciliation: documented claims that profile
        # evidence contradicts become structured `dictionary_conflicts`, and a
        # proven feature standing on a contradicted column is demoted to an
        # answerable blocker BEFORE clusters/summary/panel are computed.
        self._reconcile_dictionary_claims(mapping)
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
        from core.onboarding.lexicon.vocabulary import terms_for as _vocab_terms_for
        extracted = extract_expression(
            expression_context,
            workspace_filter_terms=_vocab_terms_for(self.layout, "filter_terms"),
        )
        if _requires_kpi_definition(kpi, expression_context, extracted):
            feature = _kpi_definition_feature(kpi, schema_index, placeholder=True)
            return {
                "kpi_id": f"kpi_{idx:03d}",
                "name": kpi.get("name", ""),
                "source": kpi.get("source", ""),
                "metric": metric,
                "cuts": cuts,
                # A seed/placeholder KPI is undefined rather than presupposing
                # missing data; the no-supporting-evidence labeling pass skips it.
                "placeholder_seed": True,
                "status": "blocked_questions_pending",
                "features": [feature],
                "function_context": extracted.functions,
                "join_candidates": [],
                "open_questions": [feature["question"]],
            }
        # A KPI that arrives with neither a metric NOR a grain exposes no
        # expression to extract features from. Without this branch the loop below
        # is empty, the KPI is silently marked blocked with ZERO questions, and a
        # direct `prepare-kpi-blocker-panel` run reports "no blocker question
        # remains" while the KPI is still blocked — a dead end. Surface it as an
        # answerable definition blocker. (In the full workspace-flow this case is
        # caught earlier by the kpi_definition_incomplete gate; this keeps the
        # standalone resolver/panel path honest too.) Generic: the condition is
        # the same empty-metric-AND-cuts test the flow gate and validator use.
        if not metric.strip() and not cuts.strip():
            # When the question matches a reusable derived-feature pattern (a
            # duration bucket or a recurrence self-join), surface that as a
            # confirmable derived option rather than only a generic "define this
            # KPI" ask -- the feature-derivation-library path. Otherwise fall back
            # to the definition blocker.
            pattern_options = self._derivation_pattern_options(kpi)
            if pattern_options:
                feature = _derived_pattern_feature(pattern_options)
            else:
                feature = _kpi_definition_feature(kpi, schema_index)
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
            # An exact physical-column hit is a direct mapping, not an alias: let the
            # schema_index branch below classify it as proven_direct. The contextual
            # auto-proven path only applies when the token is NOT a literal column name.
            if (
                norm not in schema_index
                and contextual_candidates
                and contextual_candidates[0].get("auto_proven")
            ):
                features.append(_contextual_feature(token, contextual_candidates, proven=True))
                continue
            if norm in schema_index:
                evidences = schema_index[norm]
                datasets = {
                    normalize_blocker(Path(str(evidence.get("dataset") or "")).stem)
                    for evidence in evidences
                }
                if len(datasets) > 1:
                    # Name collision: the same column name exists in several
                    # datasets, where it may mean different things. Evidence
                    # order: (1) dictionary context, (2) relationship/lineage
                    # unification, (3) accepted workspace definitions (applied
                    # post-resolution), (4) name matching LAST -- a collision
                    # that no higher tier resolves BLOCKS with per-candidate
                    # evidence instead of silently auto-resolving.
                    features.append(
                        self._resolve_direct_collision(token, norm, evidences, full_context)
                    )
                    continue
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
                state = (
                    "proven_alias"
                    if safe_structural_alias(token, alias_candidates, lexicon=getattr(self, "_lexicon", None))
                    else "candidate_unconfirmed"
                )
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
            if not candidate_derived_options:
                # Reusable derivation patterns (duration bucket / recurrence)
                # detect from the QUESTION text and name their derived column
                # after the threshold (over_24_hour, recurred_within_30_day).
                # When the unresolved token IS that name, attach the pattern's
                # confirmable option so the blocker panel offers the formula
                # instead of a bare "define this" ask. Previously these options
                # only surfaced on the empty-metric branch.
                candidate_derived_options = [
                    option
                    for option in self._derivation_pattern_options(kpi)
                    if normalize_blocker(str(option.get("derived_column_name") or ""))
                    == norm
                ]
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
        features = _dedupe_features_by_physical_column(features)
        kpi_id = f"kpi_{idx:03d}"
        metric_provenance = str(kpi.get("metric_provenance") or "authored").strip() or "authored"
        if not metric.strip():
            # Cuts-only KPI: dimension tokens may resolve, but there is no
            # measure. A KPI without a metric must never be silently ready.
            features.append(_kpi_definition_feature(kpi, schema_index))
        elif metric_provenance == "derived_from_question":
            # The metric is a machine guess derived from the question text
            # during onboarding. It needs human confirmation before the KPI can
            # be ready — silently trusting it bound `avg(hours)` to an
            # unrelated timesheet column on the hostile workspace.
            features.append(
                _derived_metric_confirmation_feature(kpi, kpi_id, features, schema_index)
            )
        blocked = [feature for feature in features if feature.get("state") not in READY_STATES]
        open_questions = [feature["question"] for feature in blocked if feature.get("question")]
        status = "ready_for_sql" if not blocked and features else "blocked_questions_pending"
        if status == "blocked_questions_pending" and not open_questions:
            # F1 invariant: a blocked KPI must always carry at least one
            # answerable question or an explicit machine-readable blocker.
            fallback = _kpi_definition_feature(kpi, schema_index)
            features.append(fallback)
            blocked.append(fallback)
            open_questions.append(fallback["question"])
        return {
            "kpi_id": kpi_id,
            "name": kpi.get("name", ""),
            "source": kpi.get("source", ""),
            "metric": metric,
            "cuts": cuts,
            "status": status,
            "features": features,
            "function_context": extracted.functions,
            "join_candidates": infer_join_candidates(features),
            "open_questions": open_questions,
        }

    def _derivation_pattern_options(self, kpi: dict[str, Any]) -> list[dict[str, Any]]:
        """Reusable derived-feature options (duration bucket / recurrence) that
        match this KPI's question + profiled columns. Returns [] when none apply
        or profiles are absent; never raises."""
        try:
            from core.onboarding.features.derivation_patterns import (
                detect_derivation_patterns,
            )
            from core.onboarding.kpi.metric_derivation import columns_from_profile_index

            question = str(kpi.get("name") or kpi.get("business_question") or "").strip()
            path = self.layout.profiles_dir / "profile_index.json"
            if not question or not path.exists():
                return []
            columns = columns_from_profile_index(json.loads(path.read_text(encoding="utf-8")))
            return detect_derivation_patterns(question, columns)
        except Exception:  # pragma: no cover - pattern detection is advisory
            return []

    def _load_kpis(self) -> list[dict[str, Any]]:
        path = self.layout.contracts_dir / "kpi_registry.json"
        if not path.exists():
            return []
        data = json.loads(path.read_text(encoding="utf-8"))
        return list(data.get("kpis", []))

    def _load_relationships(self) -> list[dict[str, Any]]:
        """Relationship contracts as lineage evidence for feature resolution.

        Missing/unreadable contracts contribute nothing (the resolver still
        works; collisions then simply block instead of unifying).
        """
        path = self.layout.contracts_dir / "relationship_contracts.json"
        if not path.exists():
            return []
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            return []
        relationships = data.get("relationships")
        return list(relationships) if isinstance(relationships, list) else []

    def _resolve_direct_collision(
        self,
        token: str,
        norm: str,
        evidences: list[dict[str, Any]],
        full_context: str,
    ) -> dict[str, Any]:
        """Resolve a multi-dataset direct name match in evidence order.

        1. Dictionary context: if the KPI's own text semantically overlaps one
           candidate's dictionary description / table clearly more than every
           other (real terms, never the column name echoing itself), that
           column wins with the dictionary evidence attached.
        2. Relationship/lineage: if every colliding (dataset, column) endpoint
           is connected through join-worthy relationship contracts (e.g. the
           same key on both sides of a proven fact->dimension join), the
           collision is one logical field and stays proven_direct.
        3. Otherwise the collision BLOCKS with per-candidate evidence -- never
           silently auto-resolved by name.
        """
        dictionary_choice = _dictionary_context_choice(norm, evidences, full_context)
        if dictionary_choice is not None:
            return _contextual_feature(token, [dictionary_choice], proven=True)
        endpoints = [
            (
                normalize_blocker(Path(str(evidence.get("dataset") or "")).stem),
                normalize_blocker(str(evidence.get("column") or "")),
            )
            for evidence in evidences
        ]
        identity = getattr(self, "_column_identity", {})
        roots = {identity.get(endpoint, endpoint) for endpoint in endpoints}
        if len(roots) == 1 and endpoints and endpoints[0] in identity:
            return {
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
                ]
                + [
                    {
                        "type": "relationship_unification",
                        "source": "interns/generated/contracts/relationship_contracts.json",
                        "note": (
                            "The colliding columns are endpoints of join-worthy "
                            "relationship contracts and form one logical field."
                        ),
                    }
                ],
                "candidate_patterns": [],
                "candidates": [],
                "question": None,
            }
        descriptions = [
            f"{Path(str(evidence.get('dataset') or '')).stem}.{evidence.get('column')}"
            + (
                f": {evidence.get('dictionary_description')}"
                if evidence.get("dictionary_description")
                else ""
            )
            for evidence in evidences
        ]
        return {
            "feature": token,
            "state": "blocked_ambiguous",
            "resolution_type": "ambiguous_direct_columns",
            "source_columns": source_columns(evidences),
            "grain": "ambiguous_requires_user_choice",
            "conflicts": descriptions,
            "decision_history": [],
            "evidence": [
                {
                    "type": "schema_profile_collision",
                    "source": evidence["dataset"],
                    "column": evidence["column"],
                    "dictionary_description": evidence.get("dictionary_description", ""),
                }
                for evidence in evidences
            ],
            "candidate_patterns": [],
            "derived_feature_options": [],
            "candidates": [
                {
                    "state": "candidate_unconfirmed",
                    "column": evidence["column"],
                    "source": evidence["dataset"],
                    "reason": (
                        str(evidence.get("dictionary_description") or "").strip()
                        or "Same column name; meaning differs per table -- needs a choice."
                    ),
                }
                for evidence in evidences
            ],
            "question": (
                f"Column name `{token}` exists in {len(evidences)} datasets with "
                "table-specific meanings; no dictionary or relationship evidence "
                f"singles one out. Which dataset.column should define `{token}` "
                "for this KPI?"
            ),
        }

    def _schema_index(self) -> dict[str, list[dict[str, str]]]:
        schema_index = load_schema_index(self.layout.profiles_dir / "profile_index.json")
        dictionaries = self._load_data_dictionaries()
        # Cached for the post-resolution dictionary reconciliation pass.
        self._dictionary_rows = dictionaries
        if dictionaries:
            enrich_schema_index_with_dictionaries(schema_index, dictionaries)
        return schema_index

    def _alias_index(self, schema_index: dict[str, list[dict[str, str]]]) -> dict[str, list[dict[str, str]]]:
        return build_alias_index(schema_index, lexicon=getattr(self, "_lexicon", None))

    def _write_open_questions(self, mapping: dict[str, Any]) -> str:
        path = self.layout.reports_dir / "open_questions.md"
        lines = ["# Open Questions", ""]
        blocked_kpis = [
            kpi for kpi in mapping.get("kpis", [])
            if kpi.get("open_questions") and kpi.get("status") != "ready_for_sql"
        ]
        if blocked_kpis:
            lines += ["## Unresolved KPI Feature Questions", ""]
            for kpi in blocked_kpis:
                lines.append(f"### {kpi.get('kpi_id')} — {kpi.get('name')}")
                for question in kpi.get("open_questions", []):
                    lines.append(f"- {question}")
                lines.append("")
        else:
            lines.append("All KPI features resolved — no open questions.")
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
        return load_data_dictionary_rows(self.workspace, self.repo_root, self.layout)

    def _reconcile_dictionary_claims(self, mapping: dict[str, Any]) -> None:
        """Cross-check dictionary claims against profiles; block tainted KPIs.

        Writes ``contracts/dictionary_conflicts.json`` (only when the workspace
        actually has a data dictionary AND profile evidence -- absence of
        either means there is nothing to reconcile) and applies the conflicts
        to the mapping: error-severity conflicts demote proven features to an
        answerable ``dictionary_conflict`` blocker; ``user_confirmed`` human
        decisions are never demoted.
        """
        rows = getattr(self, "_dictionary_rows", None)
        if rows is None:
            rows = self._load_data_dictionaries()
        if not rows:
            return
        profile_index_path = self.layout.profiles_dir / "profile_index.json"
        if not profile_index_path.exists():
            return
        try:
            profiles = json.loads(profile_index_path.read_text(encoding="utf-8")).get(
                "profiles"
            )
        except json.JSONDecodeError:
            return
        if not isinstance(profiles, list) or not profiles:
            return
        payload = write_dictionary_conflicts_contract(
            self.layout,
            self.repo_root,
            _rel(self.workspace, self.repo_root),
            rows,
            profiles,
            generated_by="resolve-kpi-features",
        )
        self._store_metadata("contracts", "dictionary_conflicts", payload)
        conflicts = payload.get("conflicts") or []
        mapping["dictionary_conflicts"] = {
            "contract_path": _rel(
                self.layout.contracts_dir / "dictionary_conflicts.json", self.repo_root
            ),
            **(payload.get("summary") or {}),
        }
        if conflicts:
            apply_dictionary_conflicts_to_mapping(mapping, conflicts)


def _dictionary_context_choice(
    norm: str,
    evidences: list[dict[str, Any]],
    full_context: str,
) -> dict[str, Any] | None:
    """Dictionary-context disambiguation for a colliding column name.

    Picks one colliding column only when the KPI's own text shares at least
    two REAL semantic terms with that candidate's dictionary description or
    table name, and beats every other candidate by two or more terms. The
    column name itself is excluded from the overlap so a dictionary entry
    merely echoing the name ("Amount: the amount...") never wins -- that would
    be name matching laundered through the dictionary.
    """
    context_tokens = _semantic_tokens(full_context) - {norm}
    if not context_tokens:
        return None
    scored: list[tuple[int, set[str], dict[str, Any]]] = []
    for evidence in evidences:
        description_tokens = _semantic_tokens(
            str(evidence.get("dictionary_description") or "")
        ) - {norm}
        dataset_tokens = _semantic_tokens(
            _split_identifier(Path(str(evidence.get("dataset") or "")).stem)
        )
        overlap = context_tokens & (description_tokens | dataset_tokens)
        scored.append((len(overlap), overlap, evidence))
    scored.sort(key=lambda item: (-item[0], str(item[2].get("dataset") or "")))
    if not scored:
        return None
    top_count, top_overlap, top_evidence = scored[0]
    runner_up = scored[1][0] if len(scored) > 1 else 0
    if top_count >= 2 and top_count >= runner_up + 2:
        return {
            **top_evidence,
            "score": float(top_count),
            "reason": (
                "KPI text semantically matches this candidate's dictionary "
                f"description/table (terms: {', '.join(sorted(top_overlap))}) "
                "clearly more than every colliding alternative."
            ),
        }
    return None


def _relationship_join_worthy(relationship: dict[str, Any]) -> bool:
    """Whether a relationship is strong enough lineage to unify column names.

    Documented/proven/user-confirmed edges qualify. Raw profile name-overlap
    edges qualify only when their observed left->right key overlap passed
    (``left_keys_resolve`` is True) -- name similarity alone must never unify
    a collision (that would be circular name-matching).
    """
    state = str(relationship.get("state") or "")
    if state in {"proven_data_model", "user_confirmed", "documented_data_model"}:
        return True
    if state == "profile_validated":
        checks = relationship.get("referential_integrity_checks") or {}
        return checks.get("left_keys_resolve") is True
    return False


def _column_identity_groups(
    relationships: list[dict[str, Any]],
) -> dict[tuple[str, str], tuple[str, str]]:
    """Union-find over (dataset, column) endpoints of join-worthy relationships.

    Two physical columns connected by a join-worthy relationship are one
    logical field (a fact FK and the dimension key it references). The
    returned map sends each endpoint to its group root; endpoints with no
    relationship evidence are absent.
    """
    parent: dict[tuple[str, str], tuple[str, str]] = {}

    def find(node: tuple[str, str]) -> tuple[str, str]:
        while parent[node] != node:
            parent[node] = parent[parent[node]]
            node = parent[node]
        return node

    def union(left: tuple[str, str], right: tuple[str, str]) -> None:
        parent.setdefault(left, left)
        parent.setdefault(right, right)
        root_left, root_right = find(left), find(right)
        if root_left != root_right:
            parent[root_right] = root_left

    for relationship in relationships:
        if not isinstance(relationship, dict):
            continue
        if not _relationship_join_worthy(relationship):
            continue
        left = (
            normalize_blocker(Path(str(relationship.get("left_dataset") or "")).stem),
            normalize_blocker(str(relationship.get("left_column") or "")),
        )
        right = (
            normalize_blocker(Path(str(relationship.get("right_dataset") or "")).stem),
            normalize_blocker(str(relationship.get("right_column") or "")),
        )
        if not left[1] or not right[1]:
            continue
        union(left, right)

    return {node: find(node) for node in parent}


def _column_pair(source: dict[str, Any]) -> tuple[str, str] | None:
    """Normalized ``(dataset, column)`` identity for one source-column entry.

    The dataset basename is used (not the full path) so the same physical
    column matches whether one feature recorded an absolute path and another a
    repo-relative one. Returns ``None`` when no column is present.
    """
    column = normalize_blocker(str(source.get("column") or ""))
    if not column:
        return None
    raw_dataset = str(source.get("dataset") or source.get("source") or "")
    dataset = normalize_blocker(Path(raw_dataset).name or raw_dataset)
    return (dataset, column)


def _physical_column_key(feature: dict[str, Any]) -> frozenset[tuple[str, str]]:
    """Identity of the physical column(s) a feature resolves to.

    Keyed on normalized (dataset, column) pairs so the dedup is workspace
    agnostic — it never inspects feature names, only the resolved physical
    columns. Features that resolved to no physical column (e.g.
    ``blocked_missing_evidence``) return an empty key and are never collapsed.
    """
    key: set[tuple[str, str]] = set()
    for source in feature.get("source_columns") or []:
        pair = _column_pair(source)
        if pair:
            key.add(pair)
    return frozenset(key)


def _resolved_physical_columns(feature: dict[str, Any]) -> set[tuple[str, str]]:
    """Physical columns a PROVEN feature resolves to.

    A proven feature's ``source_columns`` are its resolved columns, so every
    entry counts. Used as the set a candidate sibling must match to be
    considered already-covered.
    """
    columns: set[tuple[str, str]] = set()
    for source in feature.get("source_columns") or []:
        pair = _column_pair(source)
        if pair:
            columns.add(pair)
    return columns


def _candidate_physical_column(feature: dict[str, Any]) -> tuple[str, str] | None:
    """Top-ranked CANDIDATE physical column for an UNRESOLVED feature.

    An unresolved contextual/alias feature does not have a single resolved
    column; instead it carries one or more ranked candidate columns. The
    target it would resolve to is the highest-ranked candidate — index 0 of
    ``candidates`` (preferred) or, failing that, ``source_columns``, both of
    which preserve descending-score order. Returns ``None`` when no candidate
    column is present.
    """
    for entry in feature.get("candidates") or []:
        pair = _column_pair(entry)
        if pair:
            return pair
    for entry in feature.get("source_columns") or []:
        pair = _column_pair(entry)
        if pair:
            return pair
    return None


def _dedupe_features_by_physical_column(
    features: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Collapse features of one KPI that resolve to the SAME physical column.

    When a misspelling/cut-label/column-word are all extracted as separate
    features but resolve to the same physical ``dataset.column``, keep a single
    feature. If any sibling in the group is already proven (a ``READY_STATES``
    member), the canonical survivor is that proven feature, so a duplicate that
    only reached ``candidate_unconfirmed`` no longer raises a phantom blocker —
    it inherits the proven resolution. The earliest feature in spec order wins
    ties, preserving the canonical name the registry expects downstream.

    Features without a resolved physical column are passed through untouched so
    legitimately distinct (or still-unresolved) features are never merged.

    A second pass handles unresolved features (e.g. ``candidate_unconfirmed``)
    that carry a *ranked candidate* column rather than a resolved one: if such a
    feature's top-ranked candidate column equals a proven sibling's resolved
    column, the candidate is a phantom blocker for an already-proven column and
    is dropped. Its remaining lower-ranked candidates are ignored — the proven
    sibling already covers the column it would have resolved to.
    """
    groups: dict[frozenset[tuple[str, str]], list[int]] = {}
    for position, feature in enumerate(features):
        key = _physical_column_key(feature)
        if not key:
            continue
        groups.setdefault(key, []).append(position)

    drop: set[int] = set()
    for key, positions in groups.items():
        if len(positions) < 2:
            continue
        # A proven sibling wins over an unconfirmed one — the unconfirmed
        # duplicate (a misspelling/cut-label artifact) inherits that proof
        # instead of raising a phantom blocker.
        proven = [pos for pos in positions if features[pos].get("state") in READY_STATES]
        candidates = proven or positions
        survivor = _canonical_survivor(features, candidates)
        for pos in positions:
            if pos != survivor:
                drop.add(pos)

    # Columns already proven by some sibling in this KPI. An unresolved
    # feature whose top-ranked candidate column is in this set is redundant.
    proven_columns: set[tuple[str, str]] = set()
    for position, feature in enumerate(features):
        if position in drop:
            continue
        if feature.get("state") in READY_STATES:
            proven_columns.update(_resolved_physical_columns(feature))

    if proven_columns:
        for position, feature in enumerate(features):
            if position in drop:
                continue
            if feature.get("state") in READY_STATES:
                continue
            candidate_column = _candidate_physical_column(feature)
            if candidate_column is not None and candidate_column in proven_columns:
                drop.add(position)

    return [feature for position, feature in enumerate(features) if position not in drop]


def _canonical_survivor(
    features: list[dict[str, Any]],
    candidates: list[int],
) -> int:
    """Pick which feature name survives a same-physical-column collapse.

    Workspace-agnostic preference, in order:

    1. A feature whose normalized name is a word of the resolved column name
       (the dimension's own column word, e.g. ``Name`` for ``departments.Name``
       or ``cost`` for ``BASE_COST``) — and among those, the one matching the
       column's trailing/head word, which is the canonical noun.
    2. Otherwise the earliest feature in spec order.

    Keying is purely on resolved column tokens, never on specific business
    vocabulary, so it generalises across workspaces.
    """
    column_words: list[str] = []
    for pos in candidates:
        for source in features[pos].get("source_columns") or []:
            column = str(source.get("column") or "")
            for word in re.findall(r"[a-z0-9]+", _split_identifier(column).lower()):
                norm = normalize_blocker(word)
                if norm and norm not in column_words:
                    column_words.append(norm)

    def rank(pos: int) -> tuple[int, int, int]:
        name = normalize_blocker(str(features[pos].get("feature") or ""))
        is_column_word = name in column_words
        # Prefer the trailing column word (head noun) when several names are words.
        trailing_index = column_words.index(name) if is_column_word else -1
        return (
            0 if is_column_word else 1,
            -trailing_index,  # later word in the column name ranks first
            candidates.index(pos),  # stable spec-order tie-break
        )

    return min(candidates, key=rank)


def extract_expression(
    expression: str,
    *,
    workspace_filter_terms: list[str] | set[str] | None = None,
) -> ExtractedExpression:
    return parse_feature_expression(expression, workspace_filter_terms=workspace_filter_terms)


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


# A feature NAME that encodes a threshold/duration/recurrence EXPRESSION
# (over_24_hour, recurred_within_30_day, under_7_days...) names a derived
# computation, never a bare physical column. Generic shape: a numeral plus a
# time unit or comparator word inside the name — no domain vocabulary.
_EXPRESSION_SHAPED_NAME_RE = re.compile(
    r"(?:^|_)(?:over|under|within|above|below|between|atleast|atmost|more|less)(?:_|\d)"
    r"|\d+_?(?:hour|hr|day|week|month|year|minute|min|second|sec)s?(?:$|_)",
    re.IGNORECASE,
)


def _expression_shaped_feature(feature: str) -> bool:
    text = str(feature or "")
    return bool(re.search(r"\d", text) and _EXPRESSION_SHAPED_NAME_RE.search(text))


def contextual_column_candidates(
    feature: str,
    full_context: str,
    schema_index: dict[str, list[dict[str, Any]]],
) -> list[dict[str, Any]]:
    """Score columns against a feature using the surrounding KPI context.

    Previously gated on a curated healthcare/finance term list. Now runs for
    any non-trivial feature name; lexical filtering happens via
    `_semantic_tokens` against actual context evidence rather than against
    a hardcoded vocabulary.
    """
    feature_norm = normalize_blocker(feature)
    if not feature_norm or feature_norm in {"year", "quarter", "month", "day", "duration"}:
        return []
    if _expression_shaped_feature(feature):
        # An expression-shaped name (over_24_hour, recurred_within_30_day)
        # names a derived COMPUTATION; any column candidate is semantically
        # mismatched by construction (the no-mismatched-candidates rule).
        # Returning candidates here also let the per-KPI dedupe MERGE the
        # derived feature into the proven column feature sharing that physical
        # column — silently dropping the derived grain and marking the KPI
        # ready without it. No candidates -> it falls through to the
        # unresolved branch and surfaces derived-feature options instead.
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
    # An expression-shaped feature name (over_24_hour, recurred_within_30_day)
    # is a derived COMPUTATION; auto-proving it as a column alias silently
    # bound `over_24_hour` to an id column and faked an entire KPI. Such names
    # may surface candidates but must always go through human confirmation
    # (the derived-feature panel owns them).
    auto_proven = (
        not _expression_shaped_feature(feature)
        and top_score >= 14
        and (len(scored) == 1 or top_score - second >= 4)
    )
    limit = 1 if auto_proven else 10
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
    if feature_norm and dataset_norm and dataset_norm.rstrip("s") == feature_norm.rstrip("s"):
        if column_norm in {"id", "code", f"{feature_norm}id", f"{feature_norm}_id"}:
            score = 24.0
            reasons = [
                f"KPI feature `{feature_norm}` aligns with table `{dataset_norm}` PK column `{column_norm}`",
            ]
            if entry.get("dictionary_description"):
                reasons.append("data dictionary corroborates the primary key choice")
            return score, reasons
    description_tokens = _semantic_tokens(dictionary_description)
    column_tokens = _semantic_tokens(_split_identifier(column))
    field_tokens = _semantic_tokens(_split_identifier(dictionary_field))
    dataset_tokens = _semantic_tokens(dataset)
    reasons: list[str] = []
    score = 0.0
    # Direct table-feature alignment: a table named after the feature is the
    # strongest non-lexical signal — apply before KPI-text bonuses so it
    # isn't drowned out by unrelated context matches.
    if feature_norm and dataset_norm and feature_norm == dataset_norm.rstrip("s"):
        score += 30.0
        reasons.append(f"table `{dataset}` directly aligns with feature `{feature_norm}`")
    if feature_norm in column_norm:
        score += 6.0
        reasons.append(f"`{feature_norm}` appears in column `{column}`")
    if column_norm and column_norm in context_norm:
        score += 8.0
        reasons.append(f"KPI context explicitly contains column phrase `{column}`")
    # Penalise surrogate/foreign-key columns (ending in "id") when the feature
    # name isn't embedded in the column — they identify records but don't
    # describe the feature dimension.
    if column_norm.endswith("id") and len(column_norm) > 2:
        if not (feature_norm and feature_norm in column_norm):
            score -= 30.0
            reasons.append("column is a key/ID column not matching the feature")
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
    from core.onboarding.lexicon.vocabulary import GENERIC_FINANCIAL_SEED
    if any(seed in feature_norm for seed in GENERIC_FINANCIAL_SEED) and any(
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


def _derived_pattern_feature(options: list[dict[str, Any]]) -> dict[str, Any]:
    """Blocker feature carrying reusable derived-feature pattern options so the
    panel renders them as confirmable JSON-backed options (same contract as the
    derived-formula path), instead of a generic definition ask."""
    primary = options[0]
    name = str(primary.get("derived_column_name") or "derived feature")
    return {
        "feature": name,
        "state": "blocked_missing_evidence",
        "resolution_type": "derived_formula",
        "source_columns": [],
        "grain": "derived",
        "conflicts": [],
        "decision_history": [],
        "evidence": [],
        "candidate_patterns": [],
        "derived_feature_options": options,
        "candidates": [],
        "question": (
            f"Confirm the derived feature `{name}` (pattern "
            f"`{primary.get('source_pattern_id')}`) for this KPI, or supply a definition."
        ),
    }


def _prose_anchor_evidence(
    kpi: dict[str, Any],
    schema_index: dict[str, list[dict[str, Any]]] | None,
    *,
    limit: int = 10,
) -> list[dict[str, Any]]:
    """Workspace-evidence anchors for a prose KPI.

    Scans the KPI's name + prose description against THIS workspace's profiled
    columns, dataset names, and data-dictionary descriptions, and returns the
    top-scoring matches as evidence entries. Derived from workspace evidence
    only — no curated vocabulary. Used so a definition blocker can show the
    user WHERE the prose touches the data instead of asking a bare question.
    """
    if not schema_index:
        return []
    text = " ".join(
        str(kpi.get(key) or "") for key in ("name", "description", "refinement_required")
    )
    tokens = _semantic_tokens(text)
    if not tokens:
        return []
    scored: list[tuple[float, dict[str, Any]]] = []
    seen: set[tuple[str, str]] = set()
    for entries in schema_index.values():
        for entry in entries:
            dataset = str(entry.get("dataset") or "")
            column = str(entry.get("column") or "")
            key = (Path(dataset).name.lower(), column.lower())
            if not column or key in seen:
                continue
            column_hits = tokens & _semantic_tokens(_split_identifier(column))
            dataset_hits = tokens & _semantic_tokens(_split_identifier(Path(dataset).stem))
            description_hits = tokens & _semantic_tokens(
                str(entry.get("dictionary_description") or "")
            )
            score = (
                3.0 * len(column_hits)
                + 2.0 * len(dataset_hits)
                + min(6.0, 2.0 * float(len(description_hits)))
            )
            if score < 3.0:
                continue
            seen.add(key)
            matched = sorted(column_hits | dataset_hits | description_hits)
            scored.append(
                (
                    score,
                    {
                        "type": "prose_term_match",
                        "source": dataset,
                        "column": column,
                        "matched_terms": matched[:8],
                        "dictionary_description": str(
                            entry.get("dictionary_description") or ""
                        ),
                        "score": score,
                    },
                )
            )
    scored.sort(key=lambda item: (-item[0], str(item[1]["source"]), str(item[1]["column"])))
    return [item for _, item in scored[:limit]]


def _kpi_supporting_evidence_present(features: list[dict[str, Any]]) -> bool:
    """True when ANY feature of a KPI anchors to workspace evidence.

    Anchors are: a feature in a READY state, any source column, any candidate
    (alias/contextual/pattern/derived option), or any evidence entry whose type
    is not purely self-referential (the KPI's own prose, seed placeholder, or
    machine-derived-metric note). Generic: no domain vocabulary, no per-KPI
    rules — only the shape of the evidence the workspace produced.
    """
    for feature in features:
        if not isinstance(feature, dict):
            continue
        if feature.get("state") in READY_STATES:
            return True
        if (
            feature.get("source_columns")
            or feature.get("candidates")
            or feature.get("candidate_patterns")
            or feature.get("derived_feature_options")
        ):
            return True
        for evidence in feature.get("evidence") or []:
            if not isinstance(evidence, dict):
                continue
            if str(evidence.get("type") or "") not in _SELF_REFERENTIAL_EVIDENCE_TYPES:
                return True
    return False


def _label_kpis_without_supporting_evidence(
    mapping: dict[str, Any],
    kpis: list[dict[str, Any]],
    schema_index: dict[str, list[dict[str, Any]]],
    definitions: dict[str, Any] | None = None,
) -> None:
    """Label blocked KPIs whose prose anchors to NOTHING in the workspace.

    This is the stronger condition than a generic block: no term in the KPI
    prose maps to any profiled column, dataset name, data-dictionary
    description, or accepted workspace definition, and no resolved feature
    carries workspace-anchored evidence or candidates. Such a KPI may
    presuppose data the workspace does not contain, so the blocker is labeled
    ``no_supporting_evidence`` and carries a question asking the user to
    confirm the absence or point at the source. Detection is evidence-shape
    only (zero anchors); it is never keyed to any KPI's wording or domain.
    """
    entries = mapping.get("kpis") or []
    for idx, entry in enumerate(entries):
        if not isinstance(entry, dict):
            continue
        if entry.get("status") != "blocked_questions_pending":
            continue
        if entry.get("placeholder_seed"):
            continue
        features = list(entry.get("features") or [])
        if _kpi_supporting_evidence_present(features):
            continue
        kpi = kpis[idx] if idx < len(kpis) and isinstance(kpis[idx], dict) else {}
        if _prose_anchor_evidence(kpi, schema_index):
            # At least one prose term anchors to workspace evidence even though
            # no feature captured it; the stronger condition does not hold.
            continue
        kpi_id = str(entry.get("kpi_id") or "")
        feature = _no_supporting_evidence_feature(entry, kpi)
        # A previously accepted answer for this blocker is saved as a workspace
        # definition. The feature is appended AFTER the definitions pass ran
        # over the mapping, so matching records must be re-applied here or the
        # user's answer would stop resolving the blocker on every re-run.
        for definition in (definitions or {}).get("definitions") or []:
            if not isinstance(definition, dict):
                continue
            if normalize_blocker(str(definition.get("feature") or "")) != normalize_blocker(
                str(feature.get("feature") or "")
            ):
                continue
            applies_to = set(definition.get("applies_to_kpis") or [])
            if applies_to and kpi_id not in applies_to:
                continue
            if kpi_id in set(definition.get("exceptions") or []):
                continue
            apply_definition_to_feature(feature, definition, kpi_id)
        features.append(feature)
        entry["features"] = features
        if feature.get("state") in READY_STATES:
            # A saved workspace definition already answered this blocker.
            continue
        entry["blocker_label"] = NO_SUPPORTING_EVIDENCE_LABEL
        open_questions = list(entry.get("open_questions") or [])
        question = feature.get("question")
        if question and question not in open_questions:
            open_questions.append(question)
        entry["open_questions"] = open_questions


def _no_supporting_evidence_feature(
    kpi_entry: dict[str, Any],
    kpi: dict[str, Any],
) -> dict[str, Any]:
    """Machine-readable blocker for a KPI with zero workspace evidence anchors.

    The question tells the user the KPI may presuppose data the workspace
    lacks and asks them to confirm that or point at the source. The feature is
    answerable through the normal blocker panel / apply-kpi-panel-answer path.
    """
    name = str(kpi.get("name") or kpi_entry.get("name") or "").strip() or str(
        kpi_entry.get("kpi_id") or "this KPI"
    )
    source = str(kpi.get("source") or kpi_entry.get("source") or "")
    description = str(kpi.get("description") or "").strip()
    evidence: list[dict[str, Any]] = [
        {
            "type": "no_supporting_evidence_scan",
            "source": source,
            "note": (
                "No term in this KPI's prose matched any profiled column, "
                "dataset name, data-dictionary description, or accepted "
                "workspace definition, and no feature carries workspace-"
                "anchored evidence or candidates."
            ),
        }
    ]
    if description:
        evidence.append(
            {
                "type": "kpi_prose",
                "source": source,
                "excerpt": description[:600],
            }
        )
    return {
        "feature": "KPI supporting evidence",
        "state": "blocked_missing_evidence",
        "resolution_type": "no_supporting_evidence",
        "blocker_label": NO_SUPPORTING_EVIDENCE_LABEL,
        "source_columns": [],
        "grain": "unknown",
        "conflicts": [],
        "decision_history": [],
        "evidence": evidence,
        "candidate_patterns": [],
        "derived_feature_options": [],
        "candidates": [],
        "question": (
            f"No workspace evidence supports `{name}`: no term in the KPI prose "
            "matches any column, dataset, dictionary entry, or accepted "
            "definition in this workspace. The KPI may presuppose data the "
            "workspace does not contain. Confirm that the data does not exist "
            "here, or point to the source dataset, column, or file that holds it."
        ),
    }


def _kpi_definition_feature(
    kpi: dict[str, Any],
    schema_index: dict[str, list[dict[str, Any]]] | None = None,
    *,
    placeholder: bool = False,
) -> dict[str, Any]:
    name = str(kpi.get("name") or "").strip()
    description = str(kpi.get("description") or "").strip()
    evidence: list[dict[str, Any]] = [
        {
            "type": "kpi_registry_placeholder",
            "source": kpi.get("source", ""),
            "refinement_required": kpi.get("refinement_required", ""),
        }
    ]
    if description and not placeholder:
        evidence.append(
            {
                "type": "kpi_prose",
                "source": kpi.get("source", ""),
                "excerpt": description[:600],
            }
        )
    evidence.extend(_prose_anchor_evidence(kpi, schema_index))
    if name and description and not placeholder:
        question = (
            f"Define the metric and grain for `{name}`: which datasets, columns, "
            "filters, or derivations implement the prose definition? Matched "
            "workspace evidence is attached; confirm with apply-kpi-definition."
        )
    else:
        question = (
            "Which concrete business question, metric expression, grain/dimensions, owner, "
            "and acceptance tests should replace this seed KPI?"
        )
    return {
        "feature": "KPI definition",
        "state": "blocked_missing_evidence",
        "resolution_type": "kpi_definition_required",
        "source_columns": [],
        "grain": "undefined",
        "conflicts": [],
        "decision_history": [],
        "evidence": evidence,
        "candidate_patterns": [],
        "derived_feature_options": [],
        "candidates": [],
        "question": question,
    }


def _derived_metric_confirmation_feature(
    kpi: dict[str, Any],
    kpi_id: str,
    resolved_features: list[dict[str, Any]],
    schema_index: dict[str, list[dict[str, Any]]] | None,
) -> dict[str, Any]:
    """Confirmation blocker for a metric the platform GUESSED from prose.

    Onboarding's derivation pass fills empty metric cells from the question
    text + profiled columns (provenance ``derived_from_question``). That guess
    is candidate evidence, never ground truth: it must be confirmed (or
    corrected) by a human before the KPI is ready. The feature carries the
    columns the guess resolved to so the panel renders JSON-backed evidence.
    """
    metric = str(kpi.get("metric") or "")
    name = str(kpi.get("name") or "")
    source_columns: list[dict[str, Any]] = []
    for feature in resolved_features:
        if feature.get("state") in READY_STATES:
            for column in feature.get("source_columns") or []:
                source_columns.append(column)
    evidence: list[dict[str, Any]] = [
        {
            "type": "derived_metric_provenance",
            "source": kpi.get("source", ""),
            "metric": metric,
            "provenance": "derived_from_question",
            "note": (
                "Onboarding derived this metric from the KPI question text and "
                "profiled columns. It is a machine guess, not an authored or "
                "user-confirmed definition; column-name similarity alone is "
                "low-confidence evidence."
            ),
        }
    ]
    evidence.extend(_prose_anchor_evidence(kpi, schema_index))
    return {
        "feature": f"{kpi_id} derived metric",
        "state": "candidate_unconfirmed",
        "resolution_type": "derived_metric_unconfirmed",
        "source_columns": source_columns,
        "grain": "needs_user_confirmation",
        "conflicts": [],
        "decision_history": [],
        "evidence": evidence,
        "candidate_patterns": [],
        "derived_feature_options": [],
        "candidates": [
            {
                "state": "candidate_unconfirmed",
                "column": column.get("column"),
                "source": column.get("dataset"),
                "reason": (
                    "Column matched the machine-derived metric by name; needs "
                    "human confirmation."
                ),
            }
            for column in source_columns
        ],
        "question": (
            f"Onboarding derived metric `{metric}` for `{name}` from the question "
            "text (provenance: derived_from_question). Confirm the metric and its "
            "column binding, or provide the correct definition via "
            "apply-kpi-definition."
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
    from core.onboarding.cli_deprecation import (
        announce_deprecated_cli_redirect,
        is_internal_cli_call,
        warn_soft_deprecated_cli,
    )

    if args.apply_decision or args.apply_workspace_definition:
        warn_soft_deprecated_cli(
            "resolve-kpi-features",
            prefer="apply-kpi-panel-answer",
            reason="panel answers carry option provenance; direct apply is for debugging",
        )
    elif not is_internal_cli_call():
        announce_deprecated_cli_redirect(
            "resolve-kpi-features",
            prefer="prepare-kpi-blocker-panel",
            reason="the wrapper runs resolve + derived-feature markdown + panel + validation in lock-step",
        )
        from core.onboarding.kpi.blocker_cli import prepare_main

        return prepare_main(
            [
                "--workspace",
                args.workspace,
                "--repo-root",
                args.repo_root,
                "--domain",
                args.domain,
            ]
        )
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
