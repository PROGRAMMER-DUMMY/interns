"""Generate governed relationship/FK contracts for workspace datasets."""
from __future__ import annotations

import argparse
import json
import re
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from core.storage.workspace_layout import WorkspaceLayout


EXECUTABLE_RELATIONSHIP_STATES = {"proven_data_model", "user_confirmed"}
RELATIONSHIP_VERSION = 1
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
        doc_relationships = _parse_relationships_from_docs(data_model_docs, profiles)
        profile_relationships = _profile_relationship_candidates(profiles)
        profile_relationships = _promote_profile_relationships_with_doc_context(
            profile_relationships,
            data_model_docs,
        )
        relationships = _merge_relationships(doc_relationships, profile_relationships)
        contract = {
            "version": RELATIONSHIP_VERSION,
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
    return data.get("relationships", [])


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


def _merge_relationships(
    doc_relationships: list[dict[str, Any]],
    profile_relationships: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    merged: dict[str, dict[str, Any]] = {}
    for relationship in [*profile_relationships, *doc_relationships]:
        key = _canonical_key(relationship)
        current = merged.get(key)
        if not current or _state_rank(relationship["state"]) > _state_rank(current["state"]):
            merged[key] = relationship
    return sorted(merged.values(), key=lambda item: item["relationship_id"])


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


if __name__ == "__main__":
    raise SystemExit(main())
