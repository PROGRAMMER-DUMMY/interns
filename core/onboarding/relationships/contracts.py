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

from core.storage.workspace_layout import WorkspaceLayout
from core.contracts.versioning import register_contract


EXECUTABLE_RELATIONSHIP_STATES = {"proven_data_model", "user_confirmed"}
USER_DECIDED_RELATIONSHIP_STATES = {"user_confirmed", "rejected"}
RELATIONSHIP_VERSION = 1

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
        profile_relationships = _profile_relationship_candidates(profiles)
        profile_relationships = _promote_profile_relationships_with_doc_context(
            profile_relationships,
            data_model_docs,
        )
        relationships = _merge_relationships(
            doc_relationships,
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
    if normalized_answer == "approve":
        target["state"] = "user_confirmed"
        target.setdefault("approval", {})
        target["approval"].update(
            {
                "state": "approved",
                "owner": "data_engineering",
                "approved_at": now,
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
        }
    )
    _recompute_summary(data)
    data["generated_by"] = "build-relationship-contracts"
    data["updated_by"] = "apply-relationship-answer"
    data["updated_at"] = now
    path.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
    return RelationshipApprovalResult(
        json_path=_rel(path, root),
        relationship_id=relationship_id,
        state=str(target.get("state") or ""),
        executable_relationship_count=int(data["summary"]["executable_relationship_count"]),
        candidate_relationship_count=int(data["summary"]["candidate_relationship_count"]),
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


def _profile_relationship_candidates(profiles: dict[str, dict[str, Any]]) -> list[dict[str, Any]]:
    relationships = []
    sources = sorted(profiles)
    for idx, left in enumerate(sources):
        for right in sources[idx + 1:]:
            columns = _shared_join_columns(profiles[left], profiles[right])
            if not columns:
                continue
            left_col, right_col = columns[0]
            relationships.append(
                _relationship(
                    left_dataset=left,
                    left_column=left_col,
                    right_dataset=right,
                    right_column=right_col,
                    state="profile_validated",
                    confidence=0.62,
                    evidence_sources=[
                        {
                            "type": "profile_shared_key",
                            "left_profile": profiles[left].get("profile_path", ""),
                            "right_profile": profiles[right].get("profile_path", ""),
                            "normalized_key": _norm(left_col),
                        }
                    ],
                )
            )
    return relationships


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
        )
        promoted.append(updated)
    return promoted


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
) -> dict[str, Any]:
    now = _now()
    executable = state in EXECUTABLE_RELATIONSHIP_STATES
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
            "status": "needs_runtime_validation",
        },
        "null_behavior": {
            "left_key_null_check_required": True,
            "right_key_null_check_required": True,
            "status": "needs_runtime_validation",
        },
        "uniqueness_checks": {
            "right_key_uniqueness_check_required": True,
            "status": "needs_runtime_validation",
        },
        "referential_integrity_checks": {
            "orphan_left_key_check_required": True,
            "status": "needs_runtime_validation",
        },
        "grain_impact": {
            "risk": "join may duplicate base rows unless cardinality is validated",
            "requires_review": True,
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
            "block_reason": "" if executable else "candidate relationship requires data-model proof or user confirmation",
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


def _shared_join_columns(
    left_profile: dict[str, Any],
    right_profile: dict[str, Any],
) -> list[tuple[str, str]]:
    left_schema = _schema(left_profile)
    right_schema = _schema(right_profile)
    left_by_norm = {_norm(column): column for column in left_schema}
    right_by_norm = {_norm(column): column for column in right_schema}
    preferred = ["patientid", "encounterid", "transactionid", "claimid", "deptid", "providerid"]
    matches = [
        (left_by_norm[key], right_by_norm[key])
        for key in preferred
        if key in left_by_norm and key in right_by_norm
    ]
    matches.extend(
        (left_by_norm[key], right_by_norm[key])
        for key in sorted(set(left_by_norm).intersection(right_by_norm))
        if (key.endswith("id") or key.endswith("code"))
        and (left_by_norm[key], right_by_norm[key]) not in matches
    )
    return matches


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
    if "transaction" in normalized:
        terms.update({"transaction", "transactions", "facttransactions"})
    if "patient" in normalized:
        terms.update({"patient", "patients", "dimpatient"})
    if "department" in normalized:
        terms.update({"department", "departments", "dimdepartment"})
    if "provider" in normalized:
        terms.update({"provider", "providers", "dimprovider"})
    if "claim" in normalized:
        terms.update({"claim", "claims", "factclaims"})
    if "encounter" in normalized:
        terms.update({"encounter", "encounters"})
    return terms


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
    return (
        relationship.get("state") in EXECUTABLE_RELATIONSHIP_STATES
        and bool(policy.get("allowed_in_sql_generation", True))
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
        ),
        op_args={
            "workspace": args.workspace,
            "relationship_id": args.relationship_id,
            "answer": args.answer,
            "evidence_note": args.evidence_note,
            "_state_fingerprint": state_fingerprint,
        },
        allow_replay=args.allow_replay,
        decision=args.answer,
        metadata={"relationship_id": args.relationship_id, "answer": args.answer},
        record_idempotent=True,
    )


if __name__ == "__main__":
    raise SystemExit(main())
