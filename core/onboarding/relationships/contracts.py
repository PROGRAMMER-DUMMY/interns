"""Generate governed relationship/FK contracts for workspace datasets."""
from __future__ import annotations

import argparse
import csv
import io
import json
import re
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from core.governance.provenance import decision_source as decision_source_for
from core.storage.workspace_layout import WorkspaceLayout
from core.contracts.versioning import register_contract


EXECUTABLE_RELATIONSHIP_STATES = {"proven_data_model", "user_confirmed"}
USER_DECIDED_RELATIONSHIP_STATES = {"user_confirmed", "rejected"}
RELATIONSHIP_VERSION = 1

# A diagram-declared join proven only by referential integrity against a very
# small dimension table can pass RI coincidentally (few distinct keys => the FK
# resolves by chance, not by genuine design). Such joins stay executable
# (proven_data_model) but are FLAGGED low_cardinality_dimension and their
# recorded confidence is capped, so they are not silently treated as
# high-certainty. Workspace-agnostic: threshold is a structural row count.
LOW_CARDINALITY_DIMENSION_ROWS = 50
LOW_CARDINALITY_CONFIDENCE_CAP = 0.75

register_contract("relationship_contracts.json", current_version=1)
DEFAULT_REVIEW_DAYS = 180


@dataclass(frozen=True)
class RelationshipContractResult:
    json_path: str
    markdown_path: str
    relationship_count: int
    executable_relationship_count: int
    candidate_relationship_count: int

    def summary(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class RelationshipApprovalResult:
    json_path: str
    relationship_id: str
    state: str
    executable_relationship_count: int
    candidate_relationship_count: int
    source: str = "agent"
    confirmed_by: str = ""

    def summary(self) -> dict[str, Any]:
        return asdict(self)


class RelationshipContractBuilder:
    def __init__(self, repo_root: str | Path, workspace: str | Path) -> None:
        self.repo_root = Path(repo_root).resolve()
        self.workspace = (self.repo_root / workspace).resolve()
        self.layout = WorkspaceLayout(project_root=self.workspace)

    def build(self) -> RelationshipContractResult:
        self._validate_workspace()
        self.layout.ensure_runtime_dirs()
        profiles = self._profile_index()
        data_model_docs = self._data_model_docs()
        finalized_model_relationships = _relationships_from_finalized_model(
            self.layout,
            profiles,
            self.repo_root,
        )
        doc_relationships = _parse_relationships_from_docs(data_model_docs, profiles)
        diagram_relationships = _relationships_from_diagram_sidecars(
            self.layout,
            profiles,
            self.repo_root,
        )
        profile_relationships = _profile_relationship_candidates(profiles, self.repo_root)
        profile_relationships = _promote_profile_relationships_with_doc_context(
            profile_relationships,
            data_model_docs,
        )
        relationships = _merge_relationships(
            doc_relationships,
            diagram_relationships,
            profile_relationships,
            finalized_model_relationships,
        )
        relationships = _preserve_user_decided_relationships(
            relationships,
            self._load_existing_relationships(),
        )
        contract = {
            "artifact_type": "relationship_contracts.json",
            "version": RELATIONSHIP_VERSION,
            "generated_by": "build-relationship-contracts",
            "workspace": _rel(self.workspace, self.repo_root),
            "generated_at": _now(),
            "evidence_order": [
                "data_model_docs",
                "profile_schema",
                "user_confirmed_relationships",
            ],
            "executable_usage_policy": {
                "required_for_multi_dataset_generation": True,
                "allowed_states": sorted(EXECUTABLE_RELATIONSHIP_STATES),
                "candidate_policy": "candidate relationships are advisory and block trusted executable generation",
            },
            "relationships": relationships,
            "summary": {
                "relationship_count": len(relationships),
                "executable_relationship_count": sum(
                    1 for item in relationships if _executable_allowed(item)
                ),
                "candidate_relationship_count": sum(
                    1 for item in relationships if not _executable_allowed(item)
                ),
            },
        }
        json_path = self.layout.contracts_dir / "relationship_contracts.json"
        markdown_path = self.layout.reports_dir / "relationship_contracts.md"
        json_path.write_text(json.dumps(contract, indent=2) + "\n", encoding="utf-8")
        markdown_path.write_text(_render_markdown(contract), encoding="utf-8")
        summary = contract["summary"]
        return RelationshipContractResult(
            json_path=_rel(json_path, self.repo_root),
            markdown_path=_rel(markdown_path, self.repo_root),
            relationship_count=summary["relationship_count"],
            executable_relationship_count=summary["executable_relationship_count"],
            candidate_relationship_count=summary["candidate_relationship_count"],
        )

    def _load_existing_relationships(self) -> dict[tuple[str, str, str, str], dict[str, Any]]:
        path = self.layout.contracts_dir / "relationship_contracts.json"
        if not path.exists():
            return {}
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            return {}
        existing: dict[tuple[str, str, str, str], dict[str, Any]] = {}
        for relationship in data.get("relationships") or []:
            if not isinstance(relationship, dict):
                continue
            try:
                existing[_canonical_key(relationship)] = relationship
            except Exception:
                continue
        return existing

    def _profile_index(self) -> dict[str, dict[str, Any]]:
        path = self.layout.profiles_dir / "profile_index.json"
        if not path.exists():
            return {}
        data = json.loads(path.read_text(encoding="utf-8"))
        profiles = {}
        for profile in data.get("profiles", []):
            source = _repo_path(str(profile.get("path") or ""), self.repo_root)
            if source:
                profiles[source] = {**profile, "path": source}
        return profiles

    def _data_model_docs(self) -> list[dict[str, str]]:
        docs = []
        domain_model_path = self.layout.contracts_dir / "domain_model.json"
        if domain_model_path.exists():
            try:
                domain_model = json.loads(domain_model_path.read_text(encoding="utf-8"))
            except json.JSONDecodeError:
                domain_model = {}
            for value in domain_model.get("data_models") or []:
                path = (self.repo_root / str(value)).resolve()
                if not path.is_file():
                    continue
                if path.suffix.lower() not in {".csv", ".md", ".txt", ".json", ".yaml", ".yml", ".sql"}:
                    continue
                try:
                    text = path.read_text(encoding="utf-8", errors="ignore")
                except OSError:
                    continue
                docs.append({"path": _rel(path, self.repo_root), "text": text})
        docs_dir = self.workspace / "docs"
        if not docs_dir.exists():
            return docs
        for path in sorted(docs_dir.rglob("*")):
            if not path.is_file() or path.suffix.lower() not in {".md", ".txt", ".json", ".yaml", ".yml"}:
                continue
            if not any(token in path.name.lower() for token in ("model", "schema", "dictionary", "contract")):
                continue
            try:
                text = path.read_text(encoding="utf-8", errors="ignore")
            except OSError:
                continue
            docs.append({"path": _rel(path, self.repo_root), "text": text})
        return docs

    def _validate_workspace(self) -> None:
        if not self.workspace.exists():
            raise FileNotFoundError(f"workspace not found: {self.workspace}")
        if self.workspace == self.repo_root or not self.workspace.is_relative_to(self.repo_root):
            raise ValueError(f"workspace must be inside repo root: {self.workspace}")


def load_relationship_contracts(
    repo_root: str | Path,
    workspace: str | Path,
) -> list[dict[str, Any]]:
    root = Path(repo_root).resolve()
    workspace_path = (root / workspace).resolve()
    path = WorkspaceLayout(project_root=workspace_path).contracts_dir / "relationship_contracts.json"
    if not path.exists():
        return []
    data = json.loads(path.read_text(encoding="utf-8"))
    from core.onboarding.artifact_contracts import RELATIONSHIP_CONTRACTS_CONTRACT

    error = RELATIONSHIP_CONTRACTS_CONTRACT.validate(data)
    if error:
        raise ValueError(f"{_rel(path, root)}: {error}")
    return data.get("relationships", [])


def apply_relationship_answer(
    repo_root: str | Path,
    workspace: str | Path,
    *,
    relationship_id: str,
    answer: str,
    evidence_note: str = "",
    confirmed_by: str = "",
) -> RelationshipApprovalResult:
    root = Path(repo_root).resolve()
    workspace_path = (root / workspace).resolve()
    layout = WorkspaceLayout(project_root=workspace_path)
    path = layout.contracts_dir / "relationship_contracts.json"
    if not path.exists():
        raise FileNotFoundError(f"relationship contracts not found: {_rel(path, root)}")
    data = json.loads(path.read_text(encoding="utf-8"))
    relationships = data.get("relationships")
    if not isinstance(relationships, list):
        raise ValueError("relationship_contracts.json `relationships` must be a list")
    target = next(
        (item for item in relationships if str(item.get("relationship_id") or "") == relationship_id),
        None,
    )
    if target is None:
        raise ValueError(f"relationship not found: {relationship_id}")
    normalized_answer = answer.strip().lower()
    if normalized_answer not in {"approve", "reject", "keep_blocked"}:
        raise ValueError("--answer must be approve, reject, or keep_blocked")
    now = _now()
    confirmed_by = (confirmed_by or "").strip()
    # BUG-014: distinguish a human-confirmed decision from an agent-asserted one.
    # Empty --confirmed-by OR an agent identity (e.g. "agent"/"claude"/"gemini")
    # is agent-asserted; only a real human name records source:human. (An agent
    # passing --confirmed-by agent must NOT masquerade as human.)
    decision_source = decision_source_for(confirmed_by)
    if normalized_answer == "approve":
        target["state"] = "user_confirmed"
        target.setdefault("approval", {})
        target["approval"].update(
            {
                "state": "approved",
                "owner": "data_engineering",
                "approved_at": now,
                "source": decision_source,
                "confirmed_by": confirmed_by,
            }
        )
        target["executable_usage_policy"] = {
            "allowed_in_sql_generation": True,
            "allowed_in_polars_generation": True,
            "allowed_in_pyspark_generation": True,
            "allowed_in_medallion_generation": True,
            "block_reason": "",
        }
        history_state = "user_confirmed"
    else:
        target["state"] = "rejected" if normalized_answer == "reject" else target.get("state", "profile_validated")
        target.setdefault("approval", {})
        target["approval"]["state"] = "rejected" if normalized_answer == "reject" else "needs_review"
        target["approval"]["source"] = decision_source
        target["approval"]["confirmed_by"] = confirmed_by
        target.setdefault("executable_usage_policy", {})
        for key in (
            "allowed_in_sql_generation",
            "allowed_in_polars_generation",
            "allowed_in_pyspark_generation",
            "allowed_in_medallion_generation",
        ):
            target["executable_usage_policy"][key] = False
        target["executable_usage_policy"]["block_reason"] = (
            "relationship rejected by user"
            if normalized_answer == "reject"
            else "candidate relationship requires data-model proof or user confirmation"
        )
        history_state = "rejected" if normalized_answer == "reject" else "needs_review"
    target.setdefault("decision_history", []).append(
        {
            "state": history_state,
            "note": evidence_note or f"Applied relationship answer `{normalized_answer}` through apply-relationship-answer.",
            "timestamp": now,
            "source": "apply-relationship-answer",
            "decided_by": decision_source,
            "confirmed_by": confirmed_by,
        }
    )
    _recompute_summary(data)
    data["generated_by"] = "build-relationship-contracts"
    data["updated_by"] = "apply-relationship-answer"
    data["updated_at"] = now
    path.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
    _append_relationship_decision(
        WorkspaceLayout(project_root=Path(root) / workspace),
        relationship_id=relationship_id,
        state=history_state,
        note=evidence_note or f"Answer: {normalized_answer}",
    )
    return RelationshipApprovalResult(
        json_path=_rel(path, root),
        relationship_id=relationship_id,
        state=str(target.get("state") or ""),
        executable_relationship_count=int(data["summary"]["executable_relationship_count"]),
        candidate_relationship_count=int(data["summary"]["candidate_relationship_count"]),
        source=decision_source,
        confirmed_by=confirmed_by,
    )


def find_executable_relationship(
    relationships: list[dict[str, Any]],
    left_source: str,
    right_source: str,
) -> dict[str, Any] | None:
    left = _norm_path(left_source)
    right = _norm_path(right_source)
    for relationship in relationships:
        rel_left = _norm_path(relationship.get("left_dataset", ""))
        rel_right = _norm_path(relationship.get("right_dataset", ""))
        if not _executable_allowed(relationship):
            continue
        if rel_left == left and rel_right == right:
            return relationship
        if rel_left == right and rel_right == left:
            return {
                **relationship,
                "left_dataset": relationship.get("right_dataset", ""),
                "left_column": relationship.get("right_column", ""),
                "right_dataset": relationship.get("left_dataset", ""),
                "right_column": relationship.get("left_column", ""),
            }
    return None


def _parse_relationships_from_docs(
    docs: list[dict[str, str]],
    profiles: dict[str, dict[str, Any]],
) -> list[dict[str, Any]]:
    relationships = []
    dataset_lookup = _dataset_lookup(profiles)
    for doc in docs:
        text = doc["text"]
        relationships.extend(_parse_foreign_key_dictionary_rows(doc, dataset_lookup, profiles))
        for match in re.finditer(
            r"(?P<left>[A-Za-z0-9_./ -]+?)\s+joins?\s+"
            r"(?P<right>[A-Za-z0-9_./ -]+?)\s+on\s+"
            r"(?P<left_col>[A-Za-z0-9_]+)"
            r"(?:\s*=\s*(?P<right_col>[A-Za-z0-9_]+))?",
            text,
            flags=re.IGNORECASE,
        ):
            left_dataset = _resolve_dataset(match.group("left"), dataset_lookup)
            right_dataset = _resolve_dataset(match.group("right"), dataset_lookup)
            if not left_dataset or not right_dataset or left_dataset == right_dataset:
                continue
            left_column = _resolve_column(match.group("left_col"), profiles[left_dataset])
            right_column = _resolve_column(match.group("right_col") or match.group("left_col"), profiles[right_dataset])
            if not left_column or not right_column:
                continue
            relationships.append(
                _relationship(
                    left_dataset=left_dataset,
                    left_column=left_column,
                    right_dataset=right_dataset,
                    right_column=right_column,
                    state="proven_data_model",
                    confidence=0.92,
                    evidence_sources=[
                        {
                            "type": "data_model_doc",
                            "path": doc["path"],
                            "excerpt": _short_excerpt(text, match.start(), match.end()),
                        },
                        {
                            "type": "profile_schema",
                            "path": profiles[left_dataset].get("profile_path", ""),
                            "column": left_column,
                        },
                        {
                            "type": "profile_schema",
                            "path": profiles[right_dataset].get("profile_path", ""),
                            "column": right_column,
                        },
                    ],
                )
            )
    return relationships


def _parse_foreign_key_dictionary_rows(
    doc: dict[str, str],
    dataset_lookup: dict[str, str],
    profiles: dict[str, dict[str, Any]],
) -> list[dict[str, Any]]:
    relationships = []
    text = doc["text"]
    try:
        rows = list(csv.reader(io.StringIO(text)))
    except csv.Error:
        return relationships
    if not rows:
        return relationships
    for row in rows:
        if len(row) < 3:
            continue
        left_dataset = _resolve_dataset(row[0], dataset_lookup)
        if not left_dataset:
            continue
        left_column = _resolve_column(row[1], profiles[left_dataset])
        if not left_column:
            continue
        description = " ".join(row[2:])
        match = re.search(r"foreign\s+key\s+to\s+the\s+([A-Za-z0-9_ -]+)", description, flags=re.I)
        if not match:
            match = re.search(r"foreign\s+key\s+to\s+([A-Za-z0-9_ -]+)", description, flags=re.I)
        if match:
            target_name = re.split(r"\bwhere\b|\.", match.group(1), maxsplit=1, flags=re.I)[0]
            target_name = target_name.strip().rstrip(".")
            right_dataset = _resolve_dataset(target_name, dataset_lookup)
            if not right_dataset:
                right_dataset = _resolve_dataset(target_name + "s", dataset_lookup)
            right_column = _resolve_column("Id", profiles[right_dataset]) if right_dataset else ""
        else:
            if "(fk)" not in description.lower() and "foreign key" not in description.lower():
                continue
            right_dataset = _dataset_from_fk_column(left_column, dataset_lookup)
            right_column = _resolve_column(left_column, profiles[right_dataset]) if right_dataset else ""
        if not right_dataset or right_dataset == left_dataset:
            continue
        if not right_column:
            continue
        relationships.append(
            _relationship(
                left_dataset=left_dataset,
                left_column=left_column,
                right_dataset=right_dataset,
                right_column=right_column,
                state="proven_data_model",
                confidence=0.9,
                evidence_sources=[
                    {
                        "type": "data_dictionary_foreign_key",
                        "path": doc["path"],
                        "source_table": row[0],
                        "source_column": row[1],
                        "description": description,
                    },
                    {
                        "type": "profile_schema",
                        "path": profiles[left_dataset].get("profile_path", ""),
                        "column": left_column,
                    },
                    {
                        "type": "profile_schema",
                        "path": profiles[right_dataset].get("profile_path", ""),
                        "column": right_column,
                    },
                ],
            )
        )
    return relationships


def _dataset_from_fk_column(column: str, dataset_lookup: dict[str, str]) -> str:
    normalized = column.strip().lower()
    if not normalized.endswith("_id"):
        return ""
    stem = normalized[:-3]
    candidates = [
        stem,
        stem + "s",
        stem.replace("_", ""),
        stem.replace("_", "") + "s",
    ]
    if stem.startswith("primary_"):
        base = stem.removeprefix("primary_")
        candidates.extend([base, base + "s", base.replace("_", ""), base.replace("_", "") + "s"])
    for candidate in candidates:
        resolved = _resolve_dataset(candidate, dataset_lookup)
        if resolved:
            return resolved
    return ""


def _profile_relationship_candidates(
    profiles: dict[str, dict[str, Any]],
    repo_root: Path | None = None,
) -> list[dict[str, Any]]:
    """Infer profile-evidence relationships, choosing a dimension-side unique key.

    For a table-pair sharing more than one candidate join column, the join key
    is selected by dimension-side uniqueness (the column whose distinct-count
    equals its non-null row-count, i.e. a primary/near-unique key on the "one"
    side), not by first/any shared column name. Choosing a non-unique key fans
    the join out; choosing the unique key on the dimension side does not.

    Each edge is oriented so the unique (dimension/"one") side is the right
    dataset. When neither side of the chosen key is unique, the edge is flagged
    (lower confidence, grain risk) and is NOT silently allowed in SQL
    generation. Workspace-agnostic: the decision uses only profile uniqueness
    stats (or, when absent, a distinct-count read of the dataset).
    """
    relationships = []
    sources = sorted(profiles)
    for idx, left in enumerate(sources):
        for right in sources[idx + 1:]:
            columns = _shared_join_columns(profiles[left], profiles[right])
            if not columns:
                continue
            relationships.extend(
                _pair_relationships(left, right, columns, profiles, repo_root)
            )
    return relationships


def _pair_relationships(
    left: str,
    right: str,
    columns: list[tuple[str, str]],
    profiles: dict[str, dict[str, Any]],
    repo_root: Path | None,
) -> list[dict[str, Any]]:
    """Build the relationship(s) for one table-pair from its shared columns.

    Scores every shared candidate column by per-side uniqueness ratio and
    prefers the candidate that is unique on a dimension side. If several shared
    columns are unique on a dimension side, an edge is emitted for each. If no
    shared column is unique on either side, a single flagged (non-executable)
    edge is emitted on the best-available (highest-uniqueness) candidate.
    """
    scored: list[dict[str, Any]] = []
    for left_col, right_col in columns:
        left_u = _column_uniqueness(profiles[left], left_col, repo_root)
        right_u = _column_uniqueness(profiles[right], right_col, repo_root)
        scored.append(
            {
                "left_col": left_col,
                "right_col": right_col,
                "left_uniqueness": left_u,
                "right_uniqueness": right_u,
                # Best side ratio drives ranking; missing stats sort last.
                "best_ratio": max(
                    (v for v in (left_u, right_u) if v is not None),
                    default=-1.0,
                ),
            }
        )

    unique_candidates = [c for c in scored if _is_unique_ratio(c["left_uniqueness"]) or _is_unique_ratio(c["right_uniqueness"])]

    chosen = unique_candidates if unique_candidates else (
        [max(scored, key=lambda c: c["best_ratio"])] if scored else []
    )

    relationships: list[dict[str, Any]] = []
    for candidate in chosen:
        relationships.append(
            _profile_relationship_from_candidate(left, right, candidate, profiles, repo_root)
        )
    return relationships


def _profile_relationship_from_candidate(
    left: str,
    right: str,
    candidate: dict[str, Any],
    profiles: dict[str, dict[str, Any]],
    repo_root: Path | None = None,
) -> dict[str, Any]:
    left_col = candidate["left_col"]
    right_col = candidate["right_col"]
    left_u = candidate["left_uniqueness"]
    right_u = candidate["right_uniqueness"]

    # Orient the edge so the unique (dimension / "one") side is the right side.
    # Prefer the higher-uniqueness side as the dimension; if the left is the
    # only unique side, swap so the PK column lands on the right.
    left_unique = _is_unique_ratio(left_u)
    right_unique = _is_unique_ratio(right_u)
    if left_unique and not right_unique:
        left, right = right, left
        left_col, right_col = right_col, left_col
        left_u, right_u = right_u, left_u
        left_unique, right_unique = right_unique, left_unique

    # Tri-state: True (unique PK), False (known non-unique -> fan-out risk),
    # or None (uniqueness undeterminable -> preserve prior runtime-validation
    # gating instead of flagging). right_u is None only when no profile stat and
    # no readable dataset were available for the dimension-side column.
    if right_unique:
        dimension_key_unique: bool | None = True
        confidence = 0.7
    elif right_u is None:
        dimension_key_unique = None
        confidence = 0.62
    else:
        dimension_key_unique = False
        confidence = 0.45

    # Referential integrity: fraction of left (fact/"many") key values that
    # resolve to a right (dimension/"one") key value. A 0%/low-resolution edge
    # is unjoinable in the data (e.g. a key-namespace mismatch) and must not be
    # marked executable regardless of uniqueness. Workspace-agnostic — computed
    # purely from observed distinct values via the same bounded CSV read used
    # for uniqueness. None when undeterminable (preserve prior gating).
    ri_ratio = _left_key_resolution_ratio(
        profiles.get(left, {}), left_col, profiles.get(right, {}), right_col, repo_root
    )

    return _relationship(
        left_dataset=left,
        left_column=left_col,
        right_dataset=right,
        right_column=right_col,
        state="profile_validated",
        confidence=confidence,
        evidence_sources=[
            {
                "type": "profile_shared_key",
                "left_profile": profiles[left].get("profile_path", ""),
                "right_profile": profiles[right].get("profile_path", ""),
                "normalized_key": _norm(right_col),
                "left_uniqueness_ratio": left_u,
                "right_uniqueness_ratio": right_u,
                "dimension_side_key_unique": dimension_key_unique,
                "left_key_resolution_ratio": ri_ratio,
            }
        ],
        dimension_key_unique=dimension_key_unique,
        referential_integrity_ratio=ri_ratio,
    )


def _promote_profile_relationships_with_doc_context(
    relationships: list[dict[str, Any]],
    docs: list[dict[str, str]],
) -> list[dict[str, Any]]:
    promoted = []
    for relationship in relationships:
        evidence = _entity_relationship_doc_evidence(relationship, docs)
        if not evidence:
            promoted.append(relationship)
            continue
        updated = _relationship(
            left_dataset=relationship["left_dataset"],
            left_column=relationship["left_column"],
            right_dataset=relationship["right_dataset"],
            right_column=relationship["right_column"],
            state="proven_data_model",
            confidence=0.84,
            evidence_sources=[
                *relationship.get("evidence_sources", []),
                evidence,
            ],
            dimension_key_unique=_dimension_key_unique_from(relationship),
            referential_integrity_ratio=_referential_integrity_ratio_from(relationship),
        )
        promoted.append(updated)
    return promoted


def _referential_integrity_ratio_from(relationship: dict[str, Any]) -> float | None:
    """Recover the left-key resolution ratio from a built relationship.

    Lets a profile edge carry its referential-integrity verdict through a
    doc-context promotion, so a 0%-resolution (key-namespace mismatch) edge is
    not re-marked executable on promotion.
    """
    checks = relationship.get("referential_integrity_checks")
    if isinstance(checks, dict):
        value = checks.get("left_key_resolution_ratio")
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            return float(value)
    for evidence in relationship.get("evidence_sources") or []:
        if isinstance(evidence, dict):
            value = evidence.get("left_key_resolution_ratio")
            if isinstance(value, (int, float)) and not isinstance(value, bool):
                return float(value)
    return None


def _dimension_key_unique_from(relationship: dict[str, Any]) -> bool | None:
    """Recover the dimension-side uniqueness verdict from a built relationship.

    Lets a profile edge carry its uniqueness verdict through a doc-context
    promotion so a non-unique key is not re-marked executable on promotion.
    """
    checks = relationship.get("uniqueness_checks")
    if isinstance(checks, dict) and "right_key_unique" in checks:
        value = checks.get("right_key_unique")
        if isinstance(value, bool):
            return value
    for evidence in relationship.get("evidence_sources") or []:
        if isinstance(evidence, dict) and isinstance(evidence.get("dimension_side_key_unique"), bool):
            return evidence["dimension_side_key_unique"]
    return None


def _entity_relationship_doc_evidence(
    relationship: dict[str, Any],
    docs: list[dict[str, str]],
) -> dict[str, Any] | None:
    left_dataset = str(relationship.get("left_dataset", ""))
    right_dataset = str(relationship.get("right_dataset", ""))
    if _dataset_group(left_dataset) != _dataset_group(right_dataset):
        return None
    left_terms = _dataset_terms(str(relationship.get("left_dataset", "")))
    right_terms = _dataset_terms(str(relationship.get("right_dataset", "")))
    left_column = _norm(str(relationship.get("left_column", "")))
    right_column = _norm(str(relationship.get("right_column", "")))
    for doc in docs:
        text = doc["text"].lower()
        normalized_text = _norm(doc["text"])
        has_relationship_language = any(
            phrase in text
            for phrase in (
                "foreign key",
                "joins",
                "relationships",
                "fact table",
                "dimension",
                "star schema",
            )
        )
        if not has_relationship_language:
            continue
        if not any(term in normalized_text for term in left_terms):
            continue
        if not any(term in normalized_text for term in right_terms):
            continue
        if left_column not in normalized_text and right_column not in normalized_text:
            continue
        return {
            "type": "data_model_entity_context",
            "path": doc["path"],
            "reason": (
                "Data model document references both dataset entities and relationship language; "
                "profile evidence proves the shared join key exists."
            ),
        }
    return None


def _relationship(
    *,
    left_dataset: str,
    left_column: str,
    right_dataset: str,
    right_column: str,
    state: str,
    confidence: float,
    evidence_sources: list[dict[str, Any]],
    dimension_key_unique: bool | None = None,
    referential_integrity_ratio: float | None = None,
) -> dict[str, Any]:
    now = _now()
    # An edge whose dimension-side (right/"one") key is known to be non-unique
    # fans the join out and must never be silently executable, even if a doc or
    # finalized-model promotion would otherwise mark the state executable. When
    # uniqueness is unknown (None) the prior gating is preserved.
    non_unique_dimension_key = dimension_key_unique is False
    # Referential integrity: when the fraction of left ("many") keys resolving to
    # a right ("one") key value is known and below threshold, the edge is
    # unjoinable in the data (e.g. a key-namespace mismatch where 0% resolve) and
    # must never be executable. None means undeterminable -> preserve prior gating.
    ri_failed = (
        referential_integrity_ratio is not None
        and referential_integrity_ratio < _REFERENTIAL_INTEGRITY_THRESHOLD
    )
    executable = (
        state in EXECUTABLE_RELATIONSHIP_STATES
        and not non_unique_dimension_key
        and not ri_failed
    )
    if ri_failed:
        ri_pct = round(referential_integrity_ratio * 100, 1)
        cardinality_status = "referential_integrity_failed"
        uniqueness_status = (
            "right_key_not_unique" if non_unique_dimension_key else "needs_runtime_validation"
        )
        grain_risk = (
            f"referential integrity failed: only {ri_pct}% of left keys resolve to a "
            "right-side key value (likely key-namespace mismatch); join is unusable."
        )
        block_reason = (
            f"referential_integrity_failed: {ri_pct}% of left keys resolve to the "
            "right key (below threshold); data-quality/key-namespace mismatch must "
            "be resolved before this edge is executable"
        )
    elif non_unique_dimension_key:
        cardinality_status = "non_unique_dimension_key_fan_out_risk"
        uniqueness_status = "right_key_not_unique"
        grain_risk = (
            "dimension-side join key is NOT unique (distinct-count < row-count); "
            "join fans out base rows. Resolve to a unique key or confirm explicitly."
        )
        block_reason = (
            "dimension-side join key is not unique; non-unique key fans the join "
            "out and requires a unique key or explicit user confirmation"
        )
    else:
        cardinality_status = "needs_runtime_validation"
        uniqueness_status = "needs_runtime_validation"
        grain_risk = "join may duplicate base rows unless cardinality is validated"
        block_reason = (
            "" if executable else "candidate relationship requires data-model proof or user confirmation"
        )
    return {
        "relationship_id": _relationship_id(left_dataset, left_column, right_dataset, right_column),
        "left_dataset": left_dataset,
        "left_column": left_column,
        "right_dataset": right_dataset,
        "right_column": right_column,
        "join_type": "left",
        "relationship_type": "foreign_key",
        "state": state,
        "confidence": confidence,
        "evidence_sources": evidence_sources,
        "cardinality": {
            "expected": "many_to_one_or_many_to_many_pending_validation",
            "validation_query_required": True,
            "status": cardinality_status,
        },
        "null_behavior": {
            "left_key_null_check_required": True,
            "right_key_null_check_required": True,
            "status": "needs_runtime_validation",
        },
        "uniqueness_checks": {
            "right_key_uniqueness_check_required": True,
            "right_key_unique": dimension_key_unique,
            "status": uniqueness_status,
        },
        "referential_integrity_checks": {
            "orphan_left_key_check_required": True,
            "left_key_resolution_ratio": referential_integrity_ratio,
            "left_keys_resolve": (None if referential_integrity_ratio is None else not ri_failed),
            "status": (
                "referential_integrity_failed"
                if ri_failed
                else "needs_runtime_validation"
                if referential_integrity_ratio is None
                else "left_keys_resolve"
            ),
        },
        "grain_impact": {
            "risk": grain_risk,
            "requires_review": True,
            "fan_out_risk": non_unique_dimension_key,
            "referential_integrity_risk": ri_failed,
        },
        "approval": {
            "state": "approved_for_execution" if executable else "needs_review",
            "owner": "data_engineering",
            "approved_at": now if executable else "",
            "review_due_at": (datetime.now(timezone.utc) + timedelta(days=DEFAULT_REVIEW_DAYS)).isoformat(),
        },
        "source_system_scope": _source_scope(left_dataset, right_dataset),
        "executable_usage_policy": {
            "allowed_in_sql_generation": executable,
            "allowed_in_polars_generation": executable,
            "allowed_in_pyspark_generation": executable,
            "allowed_in_medallion_generation": executable,
            "block_reason": block_reason,
        },
        "decision_history": [
            {
                "state": state,
                "note": "Generated relationship contract from available workspace evidence.",
                "timestamp": now,
            }
        ],
        "lineage_export": {
            "enabled": True,
            "format": "contract_json",
        },
        "promotion_policy": {
            "requires_validation_checks_passed": True,
            "requires_owner_review": state not in EXECUTABLE_RELATIONSHIP_STATES,
            "rollback_policy": "Remove relationship from executable usage if validation or drift checks fail.",
        },
    }


def _relationships_from_finalized_model(
    layout: WorkspaceLayout,
    profiles: dict[str, dict[str, Any]],
    repo_root: Path,
) -> list[dict[str, Any]]:
    path = layout.contracts_dir / "data_model_contract.json"
    if not path.exists():
        return []
    try:
        contract = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return []
    if contract.get("status") != "finalized":
        return []
    by_table = {
        str(table.get("name") or ""): _repo_path(str(table.get("source_dataset") or ""), repo_root)
        for table in contract.get("tables", [])
    }
    relationships = []
    for item in contract.get("relationships", []):
        approval = item.get("approval") or {}
        if approval.get("state") != "approved":
            continue
        left_dataset = _repo_path(
            str(item.get("from_dataset") or by_table.get(str(item.get("from_table") or ""), "")),
            repo_root,
        )
        right_dataset = _repo_path(
            str(item.get("to_dataset") or by_table.get(str(item.get("to_table") or ""), "")),
            repo_root,
        )
        if not left_dataset or not right_dataset:
            continue
        if left_dataset not in profiles or right_dataset not in profiles:
            continue
        left_column = _resolve_column(str(item.get("from_column") or ""), profiles[left_dataset])
        right_column = _resolve_column(str(item.get("to_column") or ""), profiles[right_dataset])
        if not left_column or not right_column:
            continue
        state = "proven_data_model" if item.get("state") == "proven_data_model" else "user_confirmed"
        relationships.append(
            _relationship(
                left_dataset=left_dataset,
                left_column=left_column,
                right_dataset=right_dataset,
                right_column=right_column,
                state=state,
                confidence=float(item.get("confidence") or 0.9),
                evidence_sources=[
                    {
                        "type": "finalized_data_model_contract",
                        "path": _rel(path, repo_root),
                        "relationship_id": item.get("relationship_id", ""),
                        "approval_source": approval.get("approval_source", ""),
                    },
                    *item.get("evidence_sources", []),
                ],
            )
        )
    return relationships


def _root_fact_dataset(
    candidates: list[str],
    profiles: dict[str, dict[str, Any]],
    repo_root: Path,
) -> str:
    """Return the root-fact dataset from a set of candidate fact datasets.

    The root fact is the dataset that is referenced by NONE of the other
    candidates — i.e. it has in-degree 0 in the workspace join graph.

    In-degree of dataset D = number of OTHER datasets that contain a non-unique
    column whose name matches one of D's unique-key columns.  A unique column of
    D that appears as a (non-unique) foreign-key column in another table means D
    is being referenced *as* a dimension by that other table, so D is NOT the
    root fact.

    Tie-break: highest out-degree (most FK columns pointing outward, i.e. most
    non-unique shared-key columns that are unique in some other dataset), then
    shortest name for determinism (lexically first by dataset path stem).

    Workspace-agnostic — no table name is special-cased.
    """
    if len(candidates) == 1:
        return candidates[0]

    # Collect the set of (near-)unique columns for every candidate.
    def unique_cols(dataset: str) -> set[str]:
        p = profiles.get(dataset, {})
        schema = _schema(p)
        result: set[str] = set()
        for col in schema:
            u = _column_uniqueness(p, col, repo_root)
            if _is_unique_ratio(u):
                result.add(_norm(col))
        return result

    # Collect all (normalized) column names for every candidate.
    def all_cols(dataset: str) -> set[str]:
        return {_norm(c) for c in _schema(profiles.get(dataset, {})).keys()}

    unique_by: dict[str, set[str]] = {d: unique_cols(d) for d in candidates}
    all_cols_by: dict[str, set[str]] = {d: all_cols(d) for d in candidates}

    # in-degree(D) = number of OTHER candidates that contain one of D's unique
    # keys as a column (regardless of that other dataset's uniqueness on it).
    def in_degree(dataset: str) -> int:
        my_unique = unique_by[dataset]
        if not my_unique:
            return 0
        count = 0
        for other in candidates:
            if other == dataset:
                continue
            if my_unique & all_cols_by[other]:
                count += 1
        return count

    # out-degree(D) = number of OTHER candidates that have a unique key matching
    # one of D's non-PK columns (i.e. D's FK columns pointing outward).
    def out_degree(dataset: str) -> int:
        my_all = all_cols_by[dataset]
        count = 0
        for other in candidates:
            if other == dataset:
                continue
            if unique_by[other] & my_all:
                count += 1
        return count

    in_degrees = {d: in_degree(d) for d in candidates}
    min_in = min(in_degrees.values())
    roots = [d for d in candidates if in_degrees[d] == min_in]

    # Tie-break by highest out-degree, then lexically shortest stem.
    roots.sort(key=lambda d: (-out_degree(d), Path(d.replace("\\", "/")).stem.lower()))
    return roots[0]


def _relationships_from_diagram_sidecars(
    layout: WorkspaceLayout,
    profiles: dict[str, dict[str, Any]],
    repo_root: Path,
) -> list[dict[str, Any]]:
    """Consume the parsed data-model diagram (DataModel.png) as relationship evidence.

    `parse-data-model-images` (image_parser) writes review-gated sidecars under
    `generated/data_model_images/*.model.json` that already OCR the diagram,
    extract fact->dim FKs, and match each endpoint to a workspace profile
    dataset/column. This reads those sidecars and, for every diagram-declared
    relationship whose BOTH endpoints resolved to profiles, emits a
    `proven_data_model` edge with a `data_model_image_diagram` evidence source.

    Because the diagram is authoritative for relationship *intent*, this evidence
    ranks above raw profile column-name overlap (proven_data_model > profile_validated
    in `_state_rank`). The edge is still subject to the uniqueness and referential
    -integrity gates in `_relationship`, so a diagram FK that does not resolve in
    the data (key-namespace mismatch) stays non-executable.

    When the diagram's abstract ``Fact`` table maps to more than one physical
    dataset (e.g. both ``encounters`` and ``transactions`` carry PatientID), the
    sidecar's first-match choice may be wrong.  This function re-derives the fact
    dataset among all profile datasets that carry the FK column (excluding the
    dimension/right dataset) using the root-fact algorithm: pick the dataset with
    in-degree 0 in the workspace join graph (its unique key is not referenced by
    any other dataset).  Tie-break: highest out-degree, then lexically shortest
    stem.  This ensures the correct root fact is promoted regardless of which
    physical table the image parser happened to match first.
    """
    sidecar_dir = layout.generated_dir / "data_model_images"
    if not sidecar_dir.exists():
        return []
    relationships: list[dict[str, Any]] = []
    seen: set[tuple[str, str, str, str]] = set()
    for path in sorted(sidecar_dir.glob("*.model.json")):
        try:
            record = json.loads(path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            continue
        edges = _diagram_resolved_edges(record)
        for edge in edges:
            sidecar_left = _diagram_profile_dataset(edge["from"], profiles, repo_root)
            right_dataset = _diagram_profile_dataset(edge["to"], profiles, repo_root)
            if not right_dataset:
                continue
            # Re-derive the fact (left) dataset BEFORE the same-dataset guard so
            # that a sidecar whose both sides were matched to the same physical
            # dataset (e.g. both sides matched to departments.csv when one should
            # be the fact table) gets corrected here.
            #
            # Find every profile that contains the FK column (excluding the
            # dimension/right dataset), then pick the root fact among all those
            # candidates via the in-degree algorithm.
            fk_col_norm = _norm(edge["from"].get("profile_column", ""))
            if fk_col_norm:
                fact_candidates = [
                    ds for ds in profiles
                    if ds != right_dataset
                    and fk_col_norm in {_norm(c) for c in _schema(profiles[ds]).keys()}
                ]
                if len(fact_candidates) > 1:
                    left_dataset = _root_fact_dataset(fact_candidates, profiles, repo_root)
                elif len(fact_candidates) == 1:
                    left_dataset = fact_candidates[0]
                else:
                    # No profile contains this FK column outside the dim dataset;
                    # fall back to the sidecar's original match if it is valid.
                    left_dataset = sidecar_left
            else:
                left_dataset = sidecar_left
            if not left_dataset or left_dataset == right_dataset:
                continue
            left_column = _resolve_column(edge["from"].get("profile_column", ""), profiles[left_dataset])
            right_column = _resolve_column(edge["to"].get("profile_column", ""), profiles[right_dataset])
            if not left_column or not right_column:
                continue
            key = (left_dataset, _norm(left_column), right_dataset, _norm(right_column))
            if key in seen:
                continue
            seen.add(key)
            # Orient so the diagram's "to" (dimension/PK) side is the right side,
            # then re-derive uniqueness + RI from the data the same way profile
            # candidates do, so diagram intent never overrides observed data quality.
            left_u = _column_uniqueness(profiles[left_dataset], left_column, repo_root)
            right_u = _column_uniqueness(profiles[right_dataset], right_column, repo_root)
            dimension_key_unique = (
                True if _is_unique_ratio(right_u)
                else None if right_u is None
                else False
            )
            ri_ratio = _left_key_resolution_ratio(
                profiles[left_dataset], left_column,
                profiles[right_dataset], right_column,
                repo_root,
            )
            # Low-cardinality dimension guard: a diagram join whose dimension/PK
            # side has very few rows can satisfy RI by coincidence. Keep it
            # executable but cap confidence and flag it so reviewers see the risk.
            right_rows = _first_number(profiles[right_dataset], ("row_count",))
            low_cardinality_dimension = (
                right_rows is not None and right_rows < LOW_CARDINALITY_DIMENSION_ROWS
            )
            diagram_confidence = float(edge.get("confidence") or 0.8)
            if low_cardinality_dimension:
                diagram_confidence = min(diagram_confidence, LOW_CARDINALITY_CONFIDENCE_CAP)
            relationships.append(
                _relationship(
                    left_dataset=left_dataset,
                    left_column=left_column,
                    right_dataset=right_dataset,
                    right_column=right_column,
                    state="proven_data_model",
                    confidence=diagram_confidence,
                    evidence_sources=[
                        {
                            "type": "data_model_image_diagram",
                            "path": record.get("source_image", _rel(path, repo_root)),
                            "sidecar_path": _rel(path, repo_root),
                            "relationship_id": edge.get("relationship_id", ""),
                            "from_table": edge["from"].get("table_name", ""),
                            "to_table": edge["to"].get("table_name", ""),
                            "left_uniqueness_ratio": left_u,
                            "right_uniqueness_ratio": right_u,
                            "dimension_side_key_unique": dimension_key_unique,
                            "left_key_resolution_ratio": ri_ratio,
                            "low_cardinality_dimension": low_cardinality_dimension,
                            "dimension_row_count": right_rows,
                        }
                    ],
                    dimension_key_unique=dimension_key_unique,
                    referential_integrity_ratio=ri_ratio,
                )
            )
    return relationships


def _diagram_resolved_edges(record: dict[str, Any]) -> list[dict[str, Any]]:
    """Diagram relationships whose both endpoints matched a workspace profile."""
    profile_matching = record.get("profile_matching") or {}
    matches = profile_matching.get("relationship_matches") or []
    rel_by_id = {
        str(rel.get("relationship_id") or ""): rel
        for rel in record.get("relationships") or []
    }
    edges: list[dict[str, Any]] = []
    for match in matches:
        if not isinstance(match, dict) or match.get("state") != "profile_matched":
            continue
        from_match = match.get("from_match") or {}
        to_match = match.get("to_match") or {}
        if not from_match.get("dataset") or not to_match.get("dataset"):
            continue
        rel = rel_by_id.get(str(match.get("relationship_id") or ""), {})
        edges.append(
            {
                "relationship_id": match.get("relationship_id", ""),
                "confidence": match.get("confidence") or rel.get("confidence"),
                "from": from_match,
                "to": to_match,
            }
        )
    return edges


def _diagram_profile_dataset(
    column_match: dict[str, Any],
    profiles: dict[str, dict[str, Any]],
    repo_root: Path,
) -> str:
    dataset = _repo_path(str(column_match.get("dataset") or ""), repo_root)
    if dataset in profiles:
        return dataset
    # Profile keys are repo-relative; the sidecar may carry an absolute or
    # differently-rooted dataset path. Match on normalized path tail.
    target = _norm_path(dataset)
    for source in profiles:
        if _norm_path(source).endswith(target) or target.endswith(_norm_path(source)):
            return source
    return ""


def _preserve_user_decided_relationships(
    rebuilt: list[dict[str, Any]],
    existing_by_key: dict[tuple[str, str, str, str], dict[str, Any]],
) -> list[dict[str, Any]]:
    if not existing_by_key:
        return rebuilt
    now = _now()
    preserved: list[dict[str, Any]] = []
    seen_keys: set[tuple[str, str, str, str]] = set()
    for relationship in rebuilt:
        try:
            key = _canonical_key(relationship)
        except Exception:
            preserved.append(relationship)
            continue
        seen_keys.add(key)
        prior = existing_by_key.get(key)
        if prior and prior.get("state") in USER_DECIDED_RELATIONSHIP_STATES:
            carried = json.loads(json.dumps(prior))
            history = carried.setdefault("decision_history", [])
            history.append(
                {
                    "state": carried.get("state"),
                    "note": "Rebuild preserved prior user decision; refreshed evidence not applied.",
                    "timestamp": now,
                    "source": "build-relationship-contracts.preserve",
                }
            )
            preserved.append(carried)
        else:
            preserved.append(relationship)
    for key, prior in existing_by_key.items():
        if key in seen_keys:
            continue
        if prior.get("state") in USER_DECIDED_RELATIONSHIP_STATES:
            preserved.append(prior)
    return sorted(preserved, key=lambda item: item.get("relationship_id", ""))


def _merge_relationships(*relationship_groups: list[dict[str, Any]]) -> list[dict[str, Any]]:
    merged: dict[str, dict[str, Any]] = {}
    for group in relationship_groups:
        for relationship in group:
            key = _canonical_key(relationship)
            current = merged.get(key)
            if not current or _state_rank(relationship["state"]) > _state_rank(current["state"]):
                merged[key] = relationship
    relationships = list(merged.values())
    executable_pairs = {
        _dataset_pair_key(item)
        for item in relationships
        if _executable_allowed(item)
    }
    relationships = [
        item
        for item in relationships
        if _executable_allowed(item) or _dataset_pair_key(item) not in executable_pairs
    ]
    return sorted(relationships, key=lambda item: item["relationship_id"])


def _recompute_summary(contract: dict[str, Any]) -> None:
    relationships = contract.get("relationships") or []
    executable = sum(1 for item in relationships if _executable_allowed(item))
    contract["summary"] = {
        "relationship_count": len(relationships),
        "executable_relationship_count": executable,
        "candidate_relationship_count": len(relationships) - executable,
    }


def _dataset_pair_key(relationship: dict[str, Any]) -> tuple[str, str]:
    left = _norm_path(str(relationship.get("left_dataset") or ""))
    right = _norm_path(str(relationship.get("right_dataset") or ""))
    return tuple(sorted((left, right)))


# A column is treated as a (near-)unique dimension key when its distinct-count
# reaches this fraction of its non-null row-count. 1.0 = strict PK; the small
# tolerance absorbs trivial sampling/whitespace noise without admitting a key
# that genuinely fans out (e.g. 15 distinct over 25 rows = 0.6).
_UNIQUE_RATIO_THRESHOLD = 0.99

# Minimum fraction of left ("many") key values that must resolve to a right
# ("one") key value for an edge to be join-worthy. Below this the keys live in
# different namespaces (e.g. `PROV0288` vs `H1-PROV0004` -> 0% resolve) and the
# join produces no/near-no matches, so the edge must not be executable.
_REFERENTIAL_INTEGRITY_THRESHOLD = 0.5


def _is_unique_ratio(ratio: float | None) -> bool:
    return ratio is not None and ratio >= _UNIQUE_RATIO_THRESHOLD


def _left_key_resolution_ratio(
    left_profile: dict[str, Any],
    left_column: str,
    right_profile: dict[str, Any],
    right_column: str,
    repo_root: Path | None,
) -> float | None:
    """Fraction of distinct left-key values that exist in the right key column.

    Reads the bounded distinct value set of each side from the CSV (reusing the
    same dataset-resolution + normalization used for the uniqueness check) and
    returns |left ∩ right| / |left|. Returns None when either side's values are
    undeterminable (no readable CSV), so RI gating is skipped rather than guessed.
    Workspace-agnostic — compares observed values only, no key name is special-cased.
    """
    left_values = _distinct_values(left_profile, left_column, repo_root)
    if left_values is None or not left_values:
        return None
    right_values = _distinct_values(right_profile, right_column, repo_root)
    if right_values is None:
        return None
    resolved = sum(1 for value in left_values if value in right_values)
    return resolved / len(left_values)


def _distinct_values(
    profile: dict[str, Any],
    column: str,
    repo_root: Path | None,
) -> set[str] | None:
    """Distinct non-null string values for `column`, or None if unreadable.

    Mirrors `_ratio_from_dataset`'s bounded CSV read so RI and uniqueness share
    one data path. Only CSV-backed datasets are read; other formats return None.
    """
    path = _resolve_dataset_file(profile, repo_root)
    if path is None or path.suffix.lower() != ".csv":
        return None
    schema = _schema(profile)
    by_norm = {_norm(name): name for name in schema}
    physical = by_norm.get(_norm(column), column)
    values: set[str] = set()
    try:
        with path.open("r", encoding="utf-8-sig", newline="") as handle:
            reader = csv.DictReader(handle)
            if not reader.fieldnames:
                return None
            field = next(
                (f for f in reader.fieldnames if _norm(f) == _norm(physical)),
                None,
            )
            if field is None:
                return None
            for row in reader:
                value = str(row.get(field) or "").strip()
                if value:
                    values.add(value)
    except OSError:
        return None
    return values


def _column_uniqueness(
    profile: dict[str, Any],
    column: str,
    repo_root: Path | None,
) -> float | None:
    """Return distinct/non-null ratio for `column`, or None if undeterminable.

    Prefers per-column distinct-count stats carried in the profile; falls back
    to a distinct-count read of the underlying dataset file when the profile
    lacks them. Workspace-agnostic — no column or table name is special-cased.
    """
    column_stats = _column_profile(profile, column)
    if column_stats is not None:
        ratio = _ratio_from_stats(column_stats, profile)
        if ratio is not None:
            return ratio
    return _ratio_from_dataset(profile, column, repo_root)


def _column_profile(profile: dict[str, Any], column: str) -> dict[str, Any] | None:
    target = _norm(column)
    for entry in profile.get("columns") or []:
        if isinstance(entry, dict) and _norm(str(entry.get("name") or entry.get("column") or "")) == target:
            return entry
    return None


def _ratio_from_stats(column_stats: dict[str, Any], profile: dict[str, Any]) -> float | None:
    distinct = _first_number(
        column_stats,
        ("distinct_count", "n_unique", "unique_count", "approx_distinct", "cardinality"),
    )
    if distinct is None:
        return None
    null_count = _first_number(column_stats, ("null_count",)) or 0
    row_count = _first_number(column_stats, ("row_count",))
    if row_count is None:
        row_count = _first_number(profile, ("row_count",))
    if not row_count:
        return None
    non_null = max(row_count - null_count, 0)
    if non_null <= 0:
        return None
    return min(distinct / non_null, 1.0)


def _ratio_from_dataset(
    profile: dict[str, Any],
    column: str,
    repo_root: Path | None,
) -> float | None:
    path = _resolve_dataset_file(profile, repo_root)
    if path is None or path.suffix.lower() != ".csv":
        return None
    schema = _schema(profile)
    by_norm = {_norm(name): name for name in schema}
    physical = by_norm.get(_norm(column), column)
    try:
        with path.open("r", encoding="utf-8-sig", newline="") as handle:
            reader = csv.DictReader(handle)
            if not reader.fieldnames:
                return None
            field = next(
                (f for f in reader.fieldnames if _norm(f) == _norm(physical)),
                None,
            )
            if field is None:
                return None
            distinct: set[str] = set()
            non_null = 0
            for row in reader:
                value = str(row.get(field) or "").strip()
                if not value:
                    continue
                non_null += 1
                distinct.add(value)
    except OSError:
        return None
    if non_null <= 0:
        return None
    return min(len(distinct) / non_null, 1.0)


def _resolve_dataset_file(profile: dict[str, Any], repo_root: Path | None) -> Path | None:
    raw = str(profile.get("path") or "")
    if not raw:
        return None
    path = Path(raw)
    if path.is_absolute() and path.exists():
        return path
    if repo_root is not None:
        candidate = (repo_root / raw).resolve()
        if candidate.exists():
            return candidate
    return path if path.exists() else None


def _first_number(payload: dict[str, Any], keys: tuple[str, ...]) -> float | None:
    for key in keys:
        value = payload.get(key)
        if isinstance(value, bool):
            continue
        if isinstance(value, (int, float)):
            return float(value)
    return None


def _shared_join_columns(
    left_profile: dict[str, Any],
    right_profile: dict[str, Any],
) -> list[tuple[str, str]]:
    left_schema = _schema(left_profile)
    right_schema = _schema(right_profile)
    left_by_norm = {_norm(column): column for column in left_schema}
    right_by_norm = {_norm(column): column for column in right_schema}
    # Workspace-agnostic: any shared column whose name reads like a key (id/code).
    # Downstream relationship contracts score these candidates by cardinality,
    # uniqueness, and referential integrity — no hardcoded domain key names.
    return [
        (left_by_norm[key], right_by_norm[key])
        for key in sorted(set(left_by_norm).intersection(right_by_norm))
        if key.endswith("id") or key.endswith("code")
    ]


def _dataset_lookup(profiles: dict[str, dict[str, Any]]) -> dict[str, str]:
    lookup = {}
    for source in profiles:
        path = Path(source.replace("\\", "/"))
        keys = {
            path.stem.lower(),
            path.name.lower(),
            _norm(path.stem),
            _norm(path.name),
        }
        for key in keys:
            lookup[key] = source
    return lookup


def _dataset_terms(source: str) -> set[str]:
    stem = Path(source.replace("\\", "/")).stem
    normalized = _norm(stem)
    terms = {normalized}
    for token in re.split(r"[^A-Za-z0-9]+", stem):
        clean = _norm(token)
        if len(clean) > 2:
            terms.add(clean)
            if clean.endswith("s"):
                terms.add(clean[:-1])
    # Generic medallion-prefix expansion. No domain words hardcoded.
    # For every token already in `terms`, also seed common medallion-layer
    # prefixes (dim*, fact*) so naming conventions across workspaces are
    # recognized regardless of which entity name appears.
    expanded = set(terms)
    for token in tuple(terms):
        if not token or len(token) < 3:
            continue
        expanded.update({f"dim{token}", f"fact{token}", f"stg{token}"})
        if token.endswith("s"):
            expanded.add(token[:-1])
        else:
            expanded.add(token + "s")
    return expanded


def _resolve_dataset(value: str, lookup: dict[str, str]) -> str:
    cleaned = value.strip().strip("`'\".:- \n\r\t")
    candidates = [
        cleaned.lower(),
        _norm(cleaned),
        _norm(cleaned.replace("Fact_", "").replace("Dim_", "")),
    ]
    for candidate in candidates:
        if candidate in lookup:
            return lookup[candidate]
    for key, source in lookup.items():
        if key and key in _norm(cleaned):
            return source
    return ""


def _resolve_column(value: str, profile: dict[str, Any]) -> str:
    if not value:
        return ""
    schema = _schema(profile)
    by_norm = {_norm(column): column for column in schema}
    return by_norm.get(_norm(value), "")


def _schema(profile: dict[str, Any]) -> dict[str, Any]:
    schema = profile.get("schema") or {}
    return schema if isinstance(schema, dict) else {}


def _canonical_key(relationship: dict[str, Any]) -> tuple[str, str, str, str]:
    left = (
        _norm_path(str(relationship.get("left_dataset", ""))),
        _norm(str(relationship.get("left_column", ""))),
    )
    right = (
        _norm_path(str(relationship.get("right_dataset", ""))),
        _norm(str(relationship.get("right_column", ""))),
    )
    return tuple(sorted([left, right])[0] + sorted([left, right])[1])


def _relationship_id(left_dataset: str, left_column: str, right_dataset: str, right_column: str) -> str:
    value = "__".join([
        Path(left_dataset).stem,
        left_column,
        Path(right_dataset).stem,
        right_column,
    ])
    return re.sub(r"[^a-z0-9_]+", "_", value.lower()).strip("_")


def _state_rank(state: str) -> int:
    return {
        "candidate_needs_review": 1,
        "profile_validated": 2,
        "proven_data_model": 3,
        "user_confirmed": 4,
    }.get(state, 0)


def _executable_allowed(relationship: dict[str, Any]) -> bool:
    policy = relationship.get("executable_usage_policy") or {}
    # Conservative default: a relationship is executable only when the policy
    # EXPLICITLY allows it. An absent/unknown `allowed_in_sql_generation` means
    # NOT executable -- matching the validator's `_relationship_executable`
    # (workspace/validation.py) so the builder/apply/count path and the validator
    # agree on one source of truth. Contract builders always emit the key, so
    # this only changes behavior for malformed/partial contracts (fail safe).
    return (
        relationship.get("state") in EXECUTABLE_RELATIONSHIP_STATES
        and policy.get("allowed_in_sql_generation") is True
    )


def _source_scope(left_dataset: str, right_dataset: str) -> dict[str, Any]:
    left_parts = left_dataset.replace("\\", "/").split("/")
    right_parts = right_dataset.replace("\\", "/").split("/")
    scope = []
    for parts in (left_parts, right_parts):
        if "datasets" in parts:
            idx = parts.index("datasets")
            scope.append("/".join(parts[idx + 1:-1]))
    return {
        "left": scope[0] if scope else "",
        "right": scope[1] if len(scope) > 1 else "",
        "cross_source": len(set(scope)) > 1,
    }


def _dataset_group(source: str) -> str:
    parts = source.replace("\\", "/").split("/")
    if "datasets" not in parts:
        return str(Path(source).parent)
    idx = parts.index("datasets")
    if len(parts) <= idx + 2:
        return "datasets"
    return "/".join(parts[idx + 1:-1])


def _short_excerpt(text: str, start: int, end: int) -> str:
    excerpt = text[max(0, start - 80): min(len(text), end + 80)]
    return " ".join(excerpt.split())[:280]


def _render_markdown(contract: dict[str, Any]) -> str:
    summary = contract.get("summary", {})
    lines = [
        "# Relationship Contracts",
        "",
        f"- Workspace: `{contract.get('workspace', '')}`",
        f"- Relationships: {summary.get('relationship_count', 0)}",
        f"- Executable: {summary.get('executable_relationship_count', 0)}",
        f"- Candidates needing review: {summary.get('candidate_relationship_count', 0)}",
        "",
    ]
    for relationship in contract.get("relationships", []):
        lines.extend(
            [
                f"## {relationship.get('relationship_id')}",
                "",
                f"- State: `{relationship.get('state')}`",
                f"- Confidence: {relationship.get('confidence')}",
                (
                    f"- Join: `{relationship.get('left_dataset')}.{relationship.get('left_column')}` "
                    f"-> `{relationship.get('right_dataset')}.{relationship.get('right_column')}`"
                ),
                f"- SQL allowed: `{relationship.get('executable_usage_policy', {}).get('allowed_in_sql_generation')}`",
                f"- Approval: `{relationship.get('approval', {}).get('state')}`",
                "",
            ]
        )
    return "\n".join(lines).rstrip() + "\n"


def _norm(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", "", str(value).lower())


def _norm_path(value: str) -> str:
    return value.replace("\\", "/").lower()


def _repo_path(value: str, root: Path) -> str:
    if not value:
        return ""
    normalized = value.replace("\\", "/")
    marker = "workspaces/"
    if marker in normalized:
        return normalized[normalized.index(marker):]
    path = Path(value)
    if path.is_absolute():
        return _rel(path, root)
    return normalized


def _rel(path: Path, root: Path) -> str:
    try:
        return str(path.resolve().relative_to(root.resolve())).replace("\\", "/")
    except ValueError:
        return str(path)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Build governed relationship/FK contracts.")
    parser.add_argument("--workspace", required=True)
    parser.add_argument("--repo-root", default=".")
    args = parser.parse_args(argv)
    result = RelationshipContractBuilder(args.repo_root, args.workspace).build()
    print(json.dumps(result.summary(), indent=2))
    return 0


def apply_main(argv: list[str] | None = None) -> int:
    from core.onboarding.workspace.cli_runner import run_workspace_command
    from core.onboarding.workspace.idempotency import fingerprint_paths

    parser = argparse.ArgumentParser(description="Apply a governed relationship contract answer.")
    parser.add_argument("--workspace", required=True)
    parser.add_argument("--repo-root", default=".")
    parser.add_argument("--relationship-id", required=True)
    parser.add_argument("--answer", required=True, choices=["approve", "reject", "keep_blocked"])
    parser.add_argument("--evidence-note", default="")
    parser.add_argument(
        "--confirmed-by",
        default="",
        help="Name of the human confirming this decision. Empty = agent-asserted (no human sign-off).",
    )
    parser.add_argument("--allow-replay", action="store_true")
    args = parser.parse_args(argv)
    workspace_path = (Path(args.repo_root).resolve() / args.workspace).resolve()
    layout = WorkspaceLayout(project_root=workspace_path)
    state_fingerprint = fingerprint_paths(layout.relationship_contracts_path)
    return run_workspace_command(
        command="apply-relationship-answer",
        workspace=args.workspace,
        repo_root=args.repo_root,
        fn=lambda: apply_relationship_answer(
            args.repo_root,
            args.workspace,
            relationship_id=args.relationship_id,
            answer=args.answer,
            evidence_note=args.evidence_note,
            confirmed_by=args.confirmed_by,
        ),
        op_args={
            "workspace": args.workspace,
            "relationship_id": args.relationship_id,
            "answer": args.answer,
            "evidence_note": args.evidence_note,
            "confirmed_by": args.confirmed_by,
            "_state_fingerprint": state_fingerprint,
        },
        allow_replay=args.allow_replay,
        decision=args.answer,
        metadata={
            "relationship_id": args.relationship_id,
            "answer": args.answer,
            "source": decision_source_for(args.confirmed_by),
            "confirmed_by": args.confirmed_by,
        },
        record_idempotent=True,
    )


def _append_relationship_decision(
    layout: WorkspaceLayout,
    *,
    relationship_id: str,
    state: str,
    note: str,
) -> None:
    path = layout.memory_dir / "decision_history.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    today = datetime.now(timezone.utc).date()
    with path.open("a", encoding="utf-8") as fh:
        fh.write("\n".join([
            f"## {today} Relationship Decision",
            "",
            f"- Relationship: `{relationship_id}`",
            f"- State: `{state}`",
            f"- Note: {note}",
            "",
        ]))


if __name__ == "__main__":
    raise SystemExit(main())
