"""Shared wiki memory and governed reuse cards for workspace decisions."""
from __future__ import annotations
from core.observability.cost_ledger import anchored

import argparse
import json
import re
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from core.storage.workspace_layout import WorkspaceLayout


WIKI_MEMORY_VERSION = 1


@dataclass(frozen=True)
class WikiMemoryResult:
    workspace: str
    shared_memory_path: str
    workspace_memory_path: str
    current_json_path: str
    current_markdown_path: str
    card_count: int
    conflict_count: int
    auto_fill_count: int

    def summary(self) -> dict[str, Any]:
        return asdict(self)


class WorkspaceWikiMemoryBuilder:
    """Build scoped, reviewable reuse suggestions from governed artifacts."""

    def __init__(self, repo_root: str | Path, workspace: str | Path, *, domain: str = "general") -> None:
        self.repo_root = Path(repo_root).resolve()
        self.workspace = (self.repo_root / workspace).resolve()
        self.workspace_rel = _rel(self.workspace, self.repo_root)
        self.domain = domain
        self.layout = WorkspaceLayout(project_root=self.workspace)
        self.shared_memory_path = self.repo_root / "state" / "team_memory" / "wiki_memory_index.json"
        self.workspace_memory_path = self.layout.memory_dir / "wiki_memory_candidates.json"
        self.current_json_path = self.layout.reports_dir / "wiki_memory" / "current.json"
        self.current_markdown_path = self.layout.reports_dir / "wiki_memory" / "current.md"

    def prepare(self) -> WikiMemoryResult:
        self._validate_workspace()
        self.layout.ensure_runtime_dirs()
        definitions = self._collect_workspace_definitions()
        shared = self._load_shared_memory()
        cards = self._reuse_cards(definitions, shared)
        shared = self._merge_shared_memory(shared, definitions)
        self._write_json(self.shared_memory_path, shared)
        workspace_memory = {
            "artifact_type": "wiki_memory_candidates.json",
            "version": WIKI_MEMORY_VERSION,
            "generated_by": "prepare-wiki-memory",
            "workspace": self.workspace_rel,
            "domain": self.domain,
            "generated_at": _now(),
            "definitions": definitions,
            "reuse_cards": cards,
        }
        self._write_json(self.workspace_memory_path, workspace_memory)
        report = self._report(definitions, cards, shared)
        self._write_json(self.current_json_path, report)
        self.current_markdown_path.parent.mkdir(parents=True, exist_ok=True)
        self.current_markdown_path.write_text(_render_markdown(report), encoding="utf-8")
        return WikiMemoryResult(
            workspace=self.workspace_rel,
            shared_memory_path=_rel(self.shared_memory_path, self.repo_root),
            workspace_memory_path=_rel(self.workspace_memory_path, self.repo_root),
            current_json_path=_rel(self.current_json_path, self.repo_root),
            current_markdown_path=_rel(self.current_markdown_path, self.repo_root),
            card_count=len(cards),
            conflict_count=sum(1 for card in cards if card["recommended_action"] == "review_conflict"),
            auto_fill_count=sum(1 for card in cards if card["recommended_action"] == "auto_fill_draft_block_execution"),
        )

    def _collect_workspace_definitions(self) -> list[dict[str, Any]]:
        definitions: list[dict[str, Any]] = []
        for state, path in self._kpi_sources():
            payload = _load_json(path)
            if not payload:
                continue
            definitions.extend(_kpi_definitions(payload, state, path, self.repo_root, self.workspace_rel, self.domain))
        for state, path in self._data_model_sources():
            payload = _load_json(path)
            if not payload:
                continue
            if state == "onboarded_domain_model":
                payload = _domain_model_to_tables(payload)
            definitions.extend(_data_model_definitions(payload, state, path, self.repo_root, self.workspace_rel, self.domain))
        for state, path in self._decision_sources():
            payload = _load_json(path)
            if not payload:
                continue
            definitions.extend(_decision_definitions(payload, state, path, self.repo_root, self.workspace_rel, self.domain))
        return _dedupe_definitions(definitions)

    def _kpi_sources(self) -> list[tuple[str, Path]]:
        return [
            ("finalized", self.workspace / "docs" / "kpi_registry.generated.json"),
            ("draft", self.layout.requirements_dir / "kpi_registry_draft.json"),
            ("onboarded", self.layout.contracts_dir / "kpi_registry.json"),
        ]

    def _data_model_sources(self) -> list[tuple[str, Path]]:
        return [
            ("finalized", self.layout.contracts_dir / "data_model_contract.json"),
            ("draft", self.layout.requirements_dir / "data_model_draft.json"),
            ("onboarded_domain_model", self.layout.contracts_dir / "domain_model.json"),
        ]

    def _decision_sources(self) -> list[tuple[str, Path]]:
        return [
            ("kpi_generation_session", self.layout.requirements_dir / "kpi_generation_session.json"),
            ("data_model_generation_session", self.layout.requirements_dir / "data_model_generation_session.json"),
            ("workflow_checkpoint", self.layout.reports_dir / "workflow" / "current.json"),
            ("presentation_manifest", self.layout.reports_dir / "presentation" / "presentation_manifest.json"),
        ]

    def _load_shared_memory(self) -> dict[str, Any]:
        payload = _load_json(self.shared_memory_path)
        if payload:
            payload.setdefault("definitions", [])
            return payload
        return {
            "artifact_type": "wiki_memory_index.json",
            "version": WIKI_MEMORY_VERSION,
            "generated_by": "prepare-wiki-memory",
            "created_at": _now(),
            "updated_at": _now(),
            "definitions": [],
        }

    def _merge_shared_memory(
        self,
        shared: dict[str, Any],
        definitions: list[dict[str, Any]],
    ) -> dict[str, Any]:
        existing = {_definition_key(item): item for item in shared.get("definitions", [])}
        for definition in definitions:
            key = _definition_key(definition)
            current = existing.get(key)
            if current:
                sources = {source["path"]: source for source in current.get("sources", [])}
                for source in definition.get("sources", []):
                    sources[source["path"]] = source
                workspaces = sorted(set(current.get("workspaces", []) + [self.workspace_rel]))
                existing[key] = {
                    **current,
                    "updated_at": _now(),
                    "sources": list(sources.values()),
                    "workspaces": workspaces,
                    "repeat_count": len(workspaces),
                    "approval_state": _stronger_state(current.get("approval_state"), definition.get("approval_state")),
                }
            else:
                existing[key] = {
                    **definition,
                    "created_at": _now(),
                    "updated_at": _now(),
                    "workspaces": [self.workspace_rel],
                    "repeat_count": 1,
                }
        shared["definitions"] = sorted(existing.values(), key=lambda item: (item["definition_type"], item["canonical_name"]))
        shared["updated_at"] = _now()
        return shared

    def _reuse_cards(self, definitions: list[dict[str, Any]], shared: dict[str, Any]) -> list[dict[str, Any]]:
        cards: list[dict[str, Any]] = []
        by_term: dict[tuple[str, str], list[dict[str, Any]]] = {}
        for item in shared.get("definitions", []):
            by_term.setdefault((_norm(item.get("canonical_name", "")), item.get("definition_type", "")), []).append(item)
        for definition in definitions:
            matches = by_term.get((_norm(definition.get("canonical_name", "")), definition.get("definition_type", "")), [])
            compatible = [item for item in matches if _scope_matches(definition, item)]
            conflicts = [item for item in matches if not _same_definition(definition, item)]
            if not matches:
                action = "record_new_definition"
            elif conflicts and not compatible:
                action = "review_conflict"
            elif definition.get("approval_state") == "inferred_candidate" and any(
                item.get("approval_state") == "approved" for item in compatible
            ):
                action = "auto_fill_draft_block_execution"
            elif compatible:
                action = "reuse_available"
            else:
                action = "suggest_only"
            cards.append(
                {
                    "card_id": f"{definition['definition_type']}__{_slug(definition['canonical_name'])}",
                    "canonical_name": definition["canonical_name"],
                    "definition_type": definition["definition_type"],
                    "scope": definition["scope"],
                    "definition": definition["definition"],
                    "approval_state": definition["approval_state"],
                    "confidence": definition["confidence"],
                    "evidence_paths": [source["path"] for source in definition.get("sources", [])],
                    "compatible_memory_ids": [item["memory_id"] for item in compatible],
                    "conflicting_memory_ids": [item["memory_id"] for item in conflicts if item not in compatible],
                    "compatible_workspaces": sorted({ws for item in compatible for ws in item.get("workspaces", [])}),
                    "repeat_count": 1 + len(matches),
                    "recommended_action": action,
                    "automation_policy": _automation_policy(action),
                }
            )
        return sorted(cards, key=lambda item: (item["recommended_action"], item["definition_type"], item["canonical_name"]))

    def _report(
        self,
        definitions: list[dict[str, Any]],
        cards: list[dict[str, Any]],
        shared: dict[str, Any],
    ) -> dict[str, Any]:
        return {
            "artifact_type": "wiki_memory/current.json",
            "version": WIKI_MEMORY_VERSION,
            "generated_by": "prepare-wiki-memory",
            "workspace": self.workspace_rel,
            "domain": self.domain,
            "generated_at": _now(),
            "definition_count": len(definitions),
            "shared_definition_count": len(shared.get("definitions", [])),
            "card_count": len(cards),
            "conflict_count": sum(1 for card in cards if card["recommended_action"] == "review_conflict"),
            "auto_fill_count": sum(1 for card in cards if card["recommended_action"] == "auto_fill_draft_block_execution"),
            "cards": cards,
            "scan_scope": {
                "included": [
                    "KPI registries",
                    "data-model drafts/contracts",
                    "KPI/data-model generation sessions",
                    "workflow checkpoints",
                    "presentation manifests",
                ],
                "excluded": ["raw datasets", "broad docs/wiki text", "logs", "profiles as authority"],
            },
            "next_steps": [
                "Review reuse cards before relying on inferred candidates.",
                "Allow blocker panels to prefill approved exact matches as draft answers.",
                "Keep executable generation blocked until the current workspace evidence or user approval is present.",
            ],
        }

    def _write_json(self, path: Path, payload: dict[str, Any]) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")

    def _validate_workspace(self) -> None:
        if not self.workspace.exists():
            raise FileNotFoundError(f"workspace not found: {self.workspace}")
        if self.workspace == self.repo_root or not self.workspace.is_relative_to(self.repo_root):
            raise ValueError(f"workspace must be inside repo root: {self.workspace}")


def _kpi_definitions(
    payload: dict[str, Any],
    state: str,
    path: Path,
    repo_root: Path,
    workspace: str,
    domain: str,
) -> list[dict[str, Any]]:
    definitions = []
    for idx, kpi in enumerate(payload.get("kpis", []), start=1):
        name = str(kpi.get("metric") or kpi.get("name") or kpi.get("business_question") or f"kpi_{idx:03d}")
        definition = str(kpi.get("description") or kpi.get("metric_definition") or kpi.get("business_question") or name)
        scope = _scope(domain=domain, workspace=workspace, grain=kpi.get("grain") or kpi.get("cuts") or kpi.get("dimensions"))
        definitions.append(
            _definition(
                canonical_name=name,
                definition_type="kpi_metric",
                definition=definition,
                scope=scope,
                approval_state="approved" if state == "finalized" else "inferred_candidate",
                confidence=0.9 if state == "finalized" else 0.62,
                source_path=_rel(path, repo_root),
                source_state=state,
            )
        )
    return definitions


def _data_model_definitions(
    payload: dict[str, Any],
    state: str,
    path: Path,
    repo_root: Path,
    workspace: str,
    domain: str,
) -> list[dict[str, Any]]:
    definitions = []
    approval = "approved" if state == "finalized" else "inferred_candidate"
    confidence = 0.92 if state == "finalized" else 0.66
    for table in payload.get("tables", []):
        name = str(table.get("name") or "")
        if not name:
            continue
        scope = _scope(domain=domain, workspace=workspace, grain=(table.get("grain") or {}).get("description"))
        definitions.append(
            _definition(
                canonical_name=name,
                definition_type="data_model_entity",
                definition=json.dumps(
                    {
                        "role": table.get("role", ""),
                        "table_pattern": table.get("table_pattern", ""),
                        "primary_key": table.get("primary_key", []),
                        "grain": table.get("grain", {}),
                        "scd_type": table.get("scd_type", ""),
                        "medallion_layer": table.get("medallion_layer", ""),
                    },
                    sort_keys=True,
                ),
                scope=scope,
                approval_state=approval,
                confidence=confidence,
                source_path=_rel(path, repo_root),
                source_state=state,
            )
        )
    for rel in payload.get("relationships", []):
        name = str(rel.get("relationship_id") or f"{rel.get('from_table')}->{rel.get('to_table')}")
        approval_state = "approved" if rel.get("approval", {}).get("state") in {"approved", "approved_for_execution"} else approval
        definitions.append(
            _definition(
                canonical_name=name,
                definition_type="data_model_relationship",
                definition=json.dumps(
                    {
                        "from_table": rel.get("from_table", ""),
                        "from_column": rel.get("from_column", ""),
                        "to_table": rel.get("to_table", ""),
                        "to_column": rel.get("to_column", ""),
                        "cardinality": rel.get("cardinality", ""),
                    },
                    sort_keys=True,
                ),
                scope=_scope(domain=domain, workspace=workspace, grain=rel.get("grain", "")),
                approval_state=approval_state,
                confidence=confidence if approval_state == "approved" else 0.55,
                source_path=_rel(path, repo_root),
                source_state=state,
            )
        )
    return definitions


def _decision_definitions(
    payload: dict[str, Any],
    state: str,
    path: Path,
    repo_root: Path,
    workspace: str,
    domain: str,
) -> list[dict[str, Any]]:
    definitions = []
    for decision in payload.get("decisions", []):
        label = str(decision.get("accepted_label") or decision.get("accepted_option_id") or "")
        stage = str(decision.get("stage") or state)
        if not label:
            continue
        definitions.append(
            _definition(
                canonical_name=f"{stage}: {label}",
                definition_type="workflow_decision",
                definition=json.dumps(decision, sort_keys=True),
                scope=_scope(domain=domain, workspace=workspace, grain=""),
                approval_state="approved",
                confidence=0.86,
                source_path=_rel(path, repo_root),
                source_state=state,
            )
        )
    return definitions


def _domain_model_to_tables(payload: dict[str, Any]) -> dict[str, Any]:
    return {
        "tables": [
            {
                "name": Path(str(dataset.get("path", ""))).stem,
                "role": "profiled_dataset",
                "table_pattern": "profiled_dataset",
                "primary_key": [],
                "grain": {"description": "needs review"},
            }
            for dataset in payload.get("datasets", [])
        ],
        "relationships": [],
    }


def _definition(
    *,
    canonical_name: str,
    definition_type: str,
    definition: str,
    scope: dict[str, Any],
    approval_state: str,
    confidence: float,
    source_path: str,
    source_state: str,
) -> dict[str, Any]:
    return {
        "memory_id": f"{definition_type}__{_slug(canonical_name)}__{_slug(scope.get('domain', 'general'))}",
        "canonical_name": canonical_name,
        "definition_type": definition_type,
        "definition": definition,
        "scope": scope,
        "approval_state": approval_state,
        "confidence": confidence,
        "sources": [{"path": source_path, "state": source_state}],
    }


def _scope(*, domain: str, workspace: str, grain: Any) -> dict[str, Any]:
    return {
        "domain": domain,
        "workspace": workspace,
        "grain": grain or "",
        "source_system": "",
        "usage_context": "kpi_data_model_governance",
    }


def _dedupe_definitions(definitions: list[dict[str, Any]]) -> list[dict[str, Any]]:
    merged: dict[str, dict[str, Any]] = {}
    for item in definitions:
        key = _definition_key(item)
        if key not in merged:
            merged[key] = item
            continue
        sources = {source["path"]: source for source in merged[key].get("sources", [])}
        for source in item.get("sources", []):
            sources[source["path"]] = source
        merged[key]["sources"] = list(sources.values())
        merged[key]["approval_state"] = _stronger_state(merged[key].get("approval_state"), item.get("approval_state"))
        merged[key]["confidence"] = max(float(merged[key].get("confidence", 0)), float(item.get("confidence", 0)))
    return sorted(merged.values(), key=lambda item: (item["definition_type"], item["canonical_name"]))


def _definition_key(item: dict[str, Any]) -> str:
    scope = item.get("scope", {})
    return "|".join(
        [
            item.get("definition_type", ""),
            _norm(item.get("canonical_name", "")),
            _norm(scope.get("domain", "")),
            _norm(scope.get("grain", "")),
            _norm(item.get("definition", "")),
        ]
    )


def _scope_matches(left: dict[str, Any], right: dict[str, Any]) -> bool:
    left_scope = left.get("scope", {})
    right_scope = right.get("scope", {})
    if _norm(left_scope.get("domain", "")) != _norm(right_scope.get("domain", "")):
        return False
    left_grain = _norm(left_scope.get("grain", ""))
    right_grain = _norm(right_scope.get("grain", ""))
    return not left_grain or not right_grain or left_grain == right_grain


def _same_definition(left: dict[str, Any], right: dict[str, Any]) -> bool:
    return _norm(left.get("definition", "")) == _norm(right.get("definition", ""))


def _stronger_state(left: Any, right: Any) -> str:
    states = ["inferred_candidate", "approved"]
    left_idx = states.index(left) if left in states else 0
    right_idx = states.index(right) if right in states else 0
    return states[max(left_idx, right_idx)]


def _automation_policy(action: str) -> dict[str, Any]:
    if action == "auto_fill_draft_block_execution":
        return {
            "can_auto_apply": True,
            "mode": "draft_prefill_only",
            "must_block_before": ["final approval", "relationship approval", "executable generation"],
        }
    if action in {"reuse_available", "record_new_definition"}:
        return {"can_auto_apply": False, "mode": "review_or_record", "must_block_before": ["executable generation"]}
    return {"can_auto_apply": False, "mode": "manual_review_required", "must_block_before": ["all downstream use"]}


def _render_markdown(report: dict[str, Any]) -> str:
    lines = [
        "# Wiki Memory Reuse Report",
        "",
        f"- Workspace: `{report.get('workspace', '')}`",
        f"- Domain: `{report.get('domain', '')}`",
        f"- Definitions scanned: `{report.get('definition_count', 0)}`",
        f"- Reuse cards: `{report.get('card_count', 0)}`",
        f"- Conflicts: `{report.get('conflict_count', 0)}`",
        f"- Draft auto-fill candidates: `{report.get('auto_fill_count', 0)}`",
        "",
        "## Cards",
        "",
    ]
    for card in report.get("cards", []):
        lines.extend(
            [
                f"### {card.get('canonical_name', '')}",
                "",
                f"- Type: `{card.get('definition_type', '')}`",
                f"- Approval state: `{card.get('approval_state', '')}`",
                f"- Recommended action: `{card.get('recommended_action', '')}`",
                f"- Repeat count: `{card.get('repeat_count', 0)}`",
                f"- Evidence: {', '.join(f'`{path}`' for path in card.get('evidence_paths', [])) or '(none)'}",
                "",
            ]
        )
    if not report.get("cards"):
        lines.append("- No reusable KPI or data-model definitions were found yet.")
    lines.extend(["", "## Scan Scope", ""])
    for item in report.get("scan_scope", {}).get("included", []):
        lines.append(f"- Included: {item}")
    for item in report.get("scan_scope", {}).get("excluded", []):
        lines.append(f"- Excluded: {item}")
    lines.append("")
    return "\n".join(lines)


def _load_json(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return {}
    return data if isinstance(data, dict) else {}


def _slug(value: Any) -> str:
    text = re.sub(r"[^a-z0-9]+", "_", str(value).lower()).strip("_")
    return text[:80] or "definition"


def _norm(value: Any) -> str:
    return re.sub(r"[^a-z0-9]+", "", str(value).lower())


def _rel(path: Path, root: Path) -> str:
    try:
        return path.resolve().relative_to(root.resolve()).as_posix()
    except ValueError:
        return path.as_posix()


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()



@anchored("prepare-wiki-memory")
def prepare_main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Prepare governed wiki memory reuse cards.")
    parser.add_argument("--workspace", required=True)
    parser.add_argument("--repo-root", default=".")
    parser.add_argument("--domain", default="general")
    args = parser.parse_args(argv)
    result = WorkspaceWikiMemoryBuilder(args.repo_root, args.workspace, domain=args.domain).prepare()
    print(json.dumps(result.summary(), indent=2))
    return 0

