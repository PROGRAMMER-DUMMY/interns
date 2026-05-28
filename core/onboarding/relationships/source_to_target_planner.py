"""Build data-model-backed source-to-target plans for KPI implementations."""
from __future__ import annotations

import argparse
import json
import re
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from core.paths import PROJECT_ROOT
from core.context.router import ContextBudget, ContextRouter
from core.onboarding.kpi.feature_resolver import READY_STATES
from core.onboarding.artifact_contracts import (
    DOMAIN_MODEL_CONTRACT,
    KPI_FEATURE_MAPPING_CONTRACT,
    PROFILE_INDEX_CONTRACT,
)
from core.onboarding.relationships.contracts import (
    find_executable_relationship,
    load_relationship_contracts,
)
from core.optimization.engine_evolution import EngineEvolutionMemory
from core.resource.manager import ResourceManager
from core.storage.workspace_layout import WorkspaceLayout
from core.contracts.versioning import register_contract


SUPPORTED_TARGET_ENGINES = {"sql", "polars", "pyspark", "hybrid"}

register_contract("source_to_target_plan.json", current_version=1)


@dataclass(frozen=True)
class SourceToTargetPlanResult:
    json_path: str
    markdown_path: str
    context_manifest_path: str
    context_wiki_path: str
    kpi_count: int
    ready_kpi_count: int
    blocked_kpi_count: int
    target_engine: str

    def summary(self) -> dict[str, Any]:
        return asdict(self)


class SourceToTargetPlanner:
    def __init__(
        self,
        repo_root: str | Path,
        workspace: str | Path,
        *,
        target_engine: str = "sql",
        context_budget: str = "standard",
    ) -> None:
        self.repo_root = Path(repo_root).resolve()
        self.workspace = (self.repo_root / workspace).resolve()
        self.layout = WorkspaceLayout(project_root=self.workspace)
        if target_engine not in SUPPORTED_TARGET_ENGINES:
            raise ValueError(f"Unsupported target engine: {target_engine}")
        self.target_engine = target_engine
        self.context_budget = context_budget

    def build(self) -> SourceToTargetPlanResult:
        self._validate_workspace()
        self.layout.ensure_runtime_dirs()
        mapping = self._load_json(self.layout.contracts_dir / "kpi_feature_mapping.json")
        domain_model = self._load_json(self.layout.contracts_dir / "domain_model.json", required=False)
        profiles = self._profile_index()
        estimated_bytes = _estimated_profile_bytes(profiles)
        resource_manager = ResourceManager(self.workspace, repo_root=self.repo_root)
        resource_artifacts = resource_manager.write_report(
            estimated_bytes=estimated_bytes,
            workload="transform",
        )
        transformation_settings = resource_manager.transformation_settings(
            target_engine=self.target_engine,
            estimated_bytes=estimated_bytes,
        )
        engine_memory = EngineEvolutionMemory(self.repo_root, _rel(self.workspace, self.repo_root))
        engine_recommendation = engine_memory.recommendation_for(
            stage="gold_kpi",
            resource_mode=transformation_settings.mode,
            target_engine=transformation_settings.target_engine_recommendation,
        )
        context_selection = ContextRouter(
            self.repo_root,
            _rel(self.workspace, self.repo_root),
        ).build(
            task="plan-source-to-target",
            budget=ContextBudget.named(self.context_budget),
            refresh="safe",
        )
        relationships = load_relationship_contracts(
            self.repo_root,
            _rel(self.workspace, self.repo_root),
        )
        plan = {
            "artifact_type": "source_to_target_plan.json",
            "version": 1,
            "generated_by": "plan-source-to-target",
            "workspace": _rel(self.workspace, self.repo_root),
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "target_engine": self.target_engine,
            "resource_mode": transformation_settings.mode,
            "resource_transform_settings": transformation_settings.to_dict(),
            "resource_artifacts": resource_artifacts,
            "engine_evolution_recommendation": engine_recommendation,
            "context_manifest": context_selection.manifest_path,
            "context_wiki": context_selection.wiki_path,
            "context_budget": context_selection.budget.to_dict(),
            "evidence_order": [
                "kpi_feature_mapping",
                "domain_model",
                "profile_index",
                "workspace_feature_definitions",
                "user_clarification",
            ],
            "domain_model_sources": domain_model.get("data_models", []) if domain_model else [],
            "kpis": [
                self._plan_kpi(kpi, profiles, domain_model or {}, relationships)
                for kpi in mapping.get("kpis", [])
            ],
        }
        for kpi_plan in plan["kpis"]:
            kpi_plan["engine_evolution_recommendation"] = engine_recommendation
        ready = [kpi for kpi in plan["kpis"] if kpi["status"] == "ready_for_generation"]
        plan["summary"] = {
            "kpi_count": len(plan["kpis"]),
            "ready_kpi_count": len(ready),
            "blocked_kpi_count": len(plan["kpis"]) - len(ready),
            "target_engine": self.target_engine,
            "resource_mode": transformation_settings.mode,
            "resource_target_engine_recommendation": transformation_settings.target_engine_recommendation,
            "engine_evolution_recommendation": engine_recommendation,
            "local_execution_allowed": transformation_settings.local_execution_allowed,
            "context_manifest": context_selection.manifest_path,
            "context_selected_sections": context_selection.selected_sections,
        }
        json_path = self.layout.contracts_dir / "source_to_target_plan.json"
        markdown_path = self.layout.reports_dir / "source_to_target_plan.md"
        json_path.write_text(json.dumps(plan, indent=2, default=str) + "\n", encoding="utf-8")
        markdown_path.write_text(_render_markdown(plan), encoding="utf-8")
        return SourceToTargetPlanResult(
            json_path=_rel(json_path, self.repo_root),
            markdown_path=_rel(markdown_path, self.repo_root),
            context_manifest_path=context_selection.manifest_path,
            context_wiki_path=context_selection.wiki_path,
            kpi_count=plan["summary"]["kpi_count"],
            ready_kpi_count=plan["summary"]["ready_kpi_count"],
            blocked_kpi_count=plan["summary"]["blocked_kpi_count"],
            target_engine=self.target_engine,
        )

    def _plan_kpi(
        self,
        kpi: dict[str, Any],
        profiles: dict[str, dict[str, Any]],
        domain_model: dict[str, Any],
        relationships: list[dict[str, Any]],
    ) -> dict[str, Any]:
        features = kpi.get("features", [])
        feature_plans = [self._plan_feature(feature, profiles) for feature in features]
        selected_sources = _selected_sources_for_kpi(feature_plans, profiles, relationships)
        rejected_sources = self._rejected_sources(selected_sources, profiles)
        blockers = []
        for feature in feature_plans:
            blockers.extend(feature.get("blockers", []))
        unresolved = [
            str(feature.get("feature") or "")
            for feature in features
            if feature.get("state") not in READY_STATES
        ]
        if unresolved:
            blockers.append(
                {
                    "type": "unresolved_features",
                    "message": "KPI has unresolved features and cannot produce executable logic.",
                    "features": unresolved,
                }
            )
        join_plan = _join_plan(selected_sources, profiles, relationships)
        if len(selected_sources) > 1 and not join_plan["relationship_graph_connected"]:
            blockers.append(
                {
                    "type": "join_proof_missing",
                    "message": (
                        "Multiple source datasets are selected but no executable relationship "
                        "contract was found."
                    ),
                    "sources": selected_sources,
                }
            )
        grain = _grain_from_kpi(kpi, feature_plans)
        medallion = {
            "bronze_inputs": selected_sources,
            "silver_conformed": [
                {
                    "source": source,
                    "checks": ["schema_present", "profile_available", "key_null_review"],
                }
                for source in selected_sources
            ],
            "gold_output": {
                "name": f"{kpi.get('kpi_id', 'kpi')}_gold",
                "grain": grain,
                "engine": self.target_engine,
            },
        }
        validation_checks = [
            "all_required_features_ready",
            "selected_sources_exist_in_profile_index",
            "selected_columns_exist_in_profile_schema",
            "join_keys_profiled_when_multiple_sources",
            "grain_matches_kpi_cuts",
            "temporal_anchor_confirmed_for_date_or_age_features",
        ]
        return {
            "kpi_id": kpi.get("kpi_id", ""),
            "business_question": kpi.get("name", ""),
            "metric": kpi.get("metric", ""),
            "dimensions_and_filters": kpi.get("cuts", ""),
            "target_engine": self.target_engine,
            "status": "blocked" if blockers else "ready_for_generation",
            "selected_source_datasets": selected_sources,
            "rejected_source_datasets": rejected_sources,
            "feature_mappings": feature_plans,
            "join_plan": join_plan,
            "grain": grain,
            "temporal_anchor": _temporal_anchor(feature_plans),
            "medallion_layers": medallion,
            "validation_checks": validation_checks,
            "domain_model_evidence": {
                "status": domain_model.get("status", "unknown"),
                "dataset_count": len(domain_model.get("datasets", [])),
                "data_models": domain_model.get("data_models", []),
            },
            "blockers": blockers,
        }

    def _plan_feature(self, feature: dict[str, Any], profiles: dict[str, dict[str, Any]]) -> dict[str, Any]:
        source_columns = []
        blockers = []
        for source in feature.get("source_columns") or []:
            normalized = _normalize_source_column(source, self.repo_root)
            source_columns.append(normalized)
            dataset = normalized.get("dataset", "")
            column = normalized.get("column", "")
            if dataset and dataset not in profiles:
                blockers.append(
                    {
                        "type": "source_dataset_not_profiled",
                        "feature": feature.get("feature"),
                        "dataset": dataset,
                    }
                )
                continue
            if dataset and column and column not in profiles.get(dataset, {}).get("schema", {}):
                blockers.append(
                    {
                        "type": "source_column_not_profiled",
                        "feature": feature.get("feature"),
                        "dataset": dataset,
                        "column": column,
                    }
                )
        if feature.get("state") in READY_STATES and not source_columns:
            blockers.append(
                {
                    "type": "ready_feature_without_source_columns",
                    "feature": feature.get("feature"),
                    "message": "Ready feature needs source columns or a documented formula/taxonomy source.",
                }
            )
        return {
            "feature": feature.get("feature", ""),
            "state": feature.get("state", ""),
            "resolution_type": feature.get("resolution_type", ""),
            "source_columns": source_columns,
            "source_datasets": _unique_sorted(item.get("dataset", "") for item in source_columns),
            "evidence_types": _unique_sorted(item.get("type", "") for item in feature.get("evidence", [])),
            "blockers": blockers,
        }

    def _rejected_sources(
        self,
        selected_sources: list[str],
        profiles: dict[str, dict[str, Any]],
    ) -> list[dict[str, str]]:
        selected = set(selected_sources)
        rejected = []
        for dataset in sorted(profiles):
            if dataset in selected:
                continue
            rejected.append(
                {
                    "dataset": dataset,
                    "reason": "Profiled dataset was not required by resolved KPI feature mappings.",
                }
            )
        return rejected

    def _profile_index(self) -> dict[str, dict[str, Any]]:
        data = self._load_json(self.layout.profiles_dir / "profile_index.json", required=False)
        profiles = {}
        for profile in data.get("profiles", []) if data else []:
            path = _repo_path(str(profile.get("path") or ""), self.repo_root)
            if path:
                profiles[path] = {**profile, "path": path}
        return profiles

    def _load_json(self, path: Path, *, required: bool = True) -> dict[str, Any]:
        if not path.exists():
            if required:
                raise FileNotFoundError(f"required artifact not found: {path}")
            return {}
        data = json.loads(path.read_text(encoding="utf-8"))
        contracts = {
            "kpi_feature_mapping.json": KPI_FEATURE_MAPPING_CONTRACT,
            "domain_model.json": DOMAIN_MODEL_CONTRACT,
            "profile_index.json": PROFILE_INDEX_CONTRACT,
        }
        contract = contracts.get(path.name)
        if contract:
            error = contract.validate(data)
            if error:
                raise ValueError(f"{_rel(path, self.repo_root)}: {error}")
        return data

    def _validate_workspace(self) -> None:
        if not self.workspace.exists():
            raise FileNotFoundError(f"workspace not found: {self.workspace}")
        if self.workspace == self.repo_root or not self.workspace.is_relative_to(self.repo_root):
            raise ValueError(f"workspace must be inside repo root: {self.workspace}")


def _normalize_source_column(source: dict[str, Any], repo_root: Path) -> dict[str, Any]:
    dataset = _repo_path(str(source.get("dataset") or ""), repo_root)
    column = str(source.get("column") or "")
    if not dataset and "." in column:
        dataset, column = _split_dataset_column(column, repo_root)
    return {
        "dataset": dataset,
        "column": column,
        "dtype": source.get("dtype"),
        "row_count": source.get("row_count"),
        "profile_path": str(source.get("profile_path") or "").replace("\\", "/"),
        "evidence_state": source.get("evidence_state") or source.get("source") or "mapping",
    }


def _split_dataset_column(value: str, repo_root: Path) -> tuple[str, str]:
    normalized = value.replace("\\", "/")
    if ".csv." in normalized:
        dataset, column = normalized.split(".csv.", 1)
        return _repo_path(dataset + ".csv", repo_root), column
    if ".parquet." in normalized:
        dataset, column = normalized.split(".parquet.", 1)
        return _repo_path(dataset + ".parquet", repo_root), column
    parts = normalized.rsplit(".", 1)
    if len(parts) == 2:
        return parts[0], parts[1]
    return "", value


def _join_plan(
    selected_sources: list[str],
    profiles: dict[str, dict[str, Any]],
    relationships: list[dict[str, Any]],
) -> dict[str, Any]:
    columns_by_source = {
        source: set((profiles.get(source, {}).get("schema") or {}).keys())
        for source in selected_sources
    }
    counts: dict[str, list[str]] = {}
    for source, columns in columns_by_source.items():
        for column in columns:
            if _is_join_key(column):
                counts.setdefault(_norm(column), []).append(source)
    join_keys = [
        {
            "key": key,
            "sources": sorted(sources),
            "requires": ["null_check", "uniqueness_or_cardinality_check"],
        }
        for key, sources in sorted(counts.items())
        if len(set(sources)) > 1
    ]
    executable = []
    for idx, left in enumerate(selected_sources):
        for right in selected_sources[idx + 1:]:
            relationship = find_executable_relationship(relationships, left, right)
            if relationship:
                executable.append(
                    {
                        "left_dataset": left,
                        "left_column": relationship.get("left_column", ""),
                        "right_dataset": right,
                        "right_column": relationship.get("right_column", ""),
                        "relationship_id": relationship.get("relationship_id", ""),
                        "state": relationship.get("state", ""),
                    }
                )
    return {
        "state": "single_source_no_join_required" if len(selected_sources) <= 1 else "candidate_join",
        "join_keys": join_keys,
        "executable_relationships": executable,
        "relationship_graph_connected": _relationship_graph_connected(selected_sources, executable),
        "requires_review": len(selected_sources) > 1,
    }


def _relationship_graph_connected(
    selected_sources: list[str],
    executable_relationships: list[dict[str, str]],
) -> bool:
    if len(selected_sources) <= 1:
        return True
    graph = {source: set() for source in selected_sources}
    for relationship in executable_relationships:
        left = relationship.get("left_dataset", "")
        right = relationship.get("right_dataset", "")
        if left in graph and right in graph:
            graph[left].add(right)
            graph[right].add(left)
    seen = set()
    stack = [selected_sources[0]]
    while stack:
        source = stack.pop()
        if source in seen:
            continue
        seen.add(source)
        stack.extend(graph.get(source, set()) - seen)
    return seen == set(selected_sources)


def _selected_sources_for_kpi(
    feature_plans: list[dict[str, Any]],
    profiles: dict[str, dict[str, Any]],
    relationships: list[dict[str, Any]],
) -> list[str]:
    refs = [
        item
        for feature in feature_plans
        for item in feature.get("source_columns", [])
        if item.get("dataset")
    ]
    if not refs:
        return []
    base = _choose_base_source(refs, profiles)
    selected = [base] if base else []
    base_group = _source_group(base)
    for feature in feature_plans:
        columns = feature.get("source_columns", [])
        if any(item.get("dataset") == base for item in columns):
            continue
        same_group = [
            item.get("dataset", "")
            for item in columns
            if item.get("dataset") and _source_group(item.get("dataset", "")) == base_group
        ]
        if same_group:
            selected.append(sorted(same_group)[0])
            continue
        connected = [
            item.get("dataset", "")
            for item in columns
            if item.get("dataset") and find_executable_relationship(relationships, base, item["dataset"])
        ]
        if connected:
            selected.append(sorted(connected)[0])
        else:
            selected.extend(item.get("dataset", "") for item in columns if item.get("dataset"))
    return _unique_sorted(selected)


def _choose_base_source(
    refs: list[dict[str, Any]],
    profiles: dict[str, dict[str, Any]],
) -> str:
    scores: dict[str, int] = {}
    for ref in refs:
        source = str(ref.get("dataset") or "")
        scores[source] = scores.get(source, 0) + 1
        name = source.lower()
        if any(token in name for token in ("transactions", "claim_data", "claims", "encounters")):
            scores[source] += 3
        if source in profiles:
            scores[source] += 1
    if not scores:
        return ""
    return sorted(scores, key=lambda source: (-scores[source], source))[0]


def _source_group(source: str) -> str:
    normalized = source.replace("\\", "/")
    parts = normalized.split("/")
    if "datasets" in parts:
        idx = parts.index("datasets")
        if len(parts) > idx + 2:
            return "/".join(parts[: idx + 3])
    # Root-level workspace CSVs are independent datasets, not partitions of one
    # source group. Collapsing them to the parent folder makes ambiguous column
    # names pick whichever file sorts first.
    return normalized


def _grain_from_kpi(kpi: dict[str, Any], feature_plans: list[dict[str, Any]]) -> dict[str, Any]:
    cuts = str(kpi.get("cuts") or "")
    dimensions = [
        part.strip()
        for part in re.split(r"[,;]", cuts)
        if part.strip()
    ]
    if not dimensions:
        dimensions = [
            feature["feature"]
            for feature in feature_plans
            if feature.get("resolution_type") in {"direct_column", "alias_column", "physical_column"}
        ]
    return {
        "description": ", ".join(dimensions) if dimensions else "KPI-level aggregate",
        "dimensions": dimensions,
        "state": "from_kpi_cuts" if cuts else "inferred_from_resolved_features",
    }


def _temporal_anchor(feature_plans: list[dict[str, Any]]) -> dict[str, Any]:
    temporal_features = [
        feature
        for feature in feature_plans
        if any(term in _norm(feature.get("feature", "")) for term in ("date", "age", "month", "day"))
    ]
    if not temporal_features:
        return {"state": "not_required", "features": []}
    return {
        "state": "requires_confirmation_or_existing_definition",
        "features": [feature.get("feature", "") for feature in temporal_features],
    }


def _estimated_profile_bytes(profiles: dict[str, dict[str, Any]]) -> int:
    return sum(int(profile.get("size_bytes") or 0) for profile in profiles.values())


def _render_markdown(plan: dict[str, Any]) -> str:
    lines = [
        "# Source To Target Plan",
        "",
        f"- Workspace: `{plan.get('workspace', '')}`",
        f"- Target engine: `{plan.get('target_engine', '')}`",
        f"- Resource mode: `{plan.get('resource_mode', '')}`",
        f"- Recommended engine: `{plan.get('resource_transform_settings', {}).get('target_engine_recommendation', '')}`",
        f"- Engine evolution: `{plan.get('engine_evolution_recommendation', {}).get('engine', '')}` "
        f"({plan.get('engine_evolution_recommendation', {}).get('confidence', '')})",
        f"- Context manifest: `{plan.get('context_manifest', '')}`",
        f"- Context wiki: `{plan.get('context_wiki', '')}`",
        f"- KPI count: {plan.get('summary', {}).get('kpi_count', len(plan.get('kpis', [])))}",
        "",
    ]
    for kpi in plan.get("kpis", []):
        lines.extend(
            [
                f"## {kpi.get('kpi_id')}: {kpi.get('business_question')}",
                "",
                f"- Status: `{kpi.get('status')}`",
                f"- Metric: `{kpi.get('metric')}`",
                f"- Grain: {kpi.get('grain', {}).get('description', '')}",
                f"- Selected sources: {', '.join(kpi.get('selected_source_datasets') or []) or 'none'}",
                f"- Target output: `{kpi.get('medallion_layers', {}).get('gold_output', {}).get('name', '')}`",
                "",
                "### Feature Mappings",
                "",
            ]
        )
        for feature in kpi.get("feature_mappings", []):
            columns = [
                f"{item.get('dataset')}.{item.get('column')}".strip(".")
                for item in feature.get("source_columns", [])
            ]
            lines.append(
                f"- `{feature.get('feature')}` -> {', '.join(columns) or 'no source'} "
                f"(`{feature.get('state')}`)"
            )
        lines.extend(["", "### Validation Checks", ""])
        for check in kpi.get("validation_checks", []):
            lines.append(f"- {check}")
        if kpi.get("blockers"):
            lines.extend(["", "### Blockers", ""])
            for blocker in kpi["blockers"]:
                lines.append(f"- `{blocker.get('type')}`: {blocker.get('message', blocker)}")
        lines.append("")
    return "\n".join(lines).rstrip() + "\n"


def _is_join_key(column: str) -> bool:
    normalized = _norm(column)
    return normalized.endswith("id") or normalized.endswith("code")


def _unique_sorted(values: Any) -> list[str]:
    return sorted({str(value) for value in values if value})


def _norm(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", "", str(value).lower())


def _rel(path: Path, root: Path) -> str:
    try:
        return str(path.resolve().relative_to(root.resolve())).replace("\\", "/")
    except ValueError:
        return str(path)


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


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Build a KPI source-to-target implementation plan.")
    parser.add_argument("--workspace", required=True, help="Workspace path relative to repo root.")
    parser.add_argument(
        "--repo-root",
        default=str(PROJECT_ROOT),
        help="Repository root. Defaults to detected project root.",
    )
    parser.add_argument(
        "--target-engine",
        choices=sorted(SUPPORTED_TARGET_ENGINES),
        default="sql",
        help="Implementation target for the plan.",
    )
    parser.add_argument(
        "--context-budget",
        choices=["small", "standard", "deep"],
        default="standard",
        help="Context pack budget for bounded artifact retrieval.",
    )
    args = parser.parse_args(argv)
    result = SourceToTargetPlanner(
        args.repo_root,
        args.workspace,
        target_engine=args.target_engine,
        context_budget=args.context_budget,
    ).build()
    print(json.dumps(result.summary(), indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
