from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from core.onboarding.features.blockers import (
    infer_join_candidates,
    normalize,
    prioritize_blockers,
)
from core.storage.workspace_layout import WorkspaceLayout


WORKSPACE_DEFINITIONS_VERSION = 1
READY_STATES = {
    "proven_direct",
    "proven_alias",
    "proven_join",
    "proven_formula",
    "proven_taxonomy",
    "user_confirmed",
}


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
    definitions = load_workspace_definitions(layout)
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
            _resolve_source_column(column, layout) for column in (source_columns or [])
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
    upsert_workspace_definition(definitions, definition_record)
    definitions_path = workspace_definitions_path(layout)
    definitions_path.parent.mkdir(parents=True, exist_ok=True)
    definitions["workspace"] = mapping.get("workspace", str(workspace))
    definitions["updated_at"] = timestamp
    definitions_path.write_text(json.dumps(definitions, indent=2, default=str) + "\n", encoding="utf-8")

    updated = apply_workspace_definition_to_mapping(mapping, definition_record)
    if not updated:
        raise ValueError(f"Feature not found in mapping: {feature}")
    recompute_mapping_status(mapping)
    mapping_path.write_text(json.dumps(mapping, indent=2, default=str) + "\n", encoding="utf-8")
    append_decision_history(layout, "workspace", feature, state, evidence_note)
    append_workspace_definition_requirement(
        layout,
        feature=feature,
        state=state,
        resolution_type=resolution_type,
        definition=definition or evidence_note,
        evidence_note=evidence_note,
        applies_to_kpis=applies_to_kpis or [],
    )
    return mapping["summary"]


def _resolve_source_column(column_expr: str, layout: WorkspaceLayout) -> dict[str, str]:
    source_ref, column = _split_source_column_expr(column_expr)
    if not source_ref:
        return {"dataset": "", "column": column, "source": "workspace_definition"}

    source_path = Path(source_ref)
    if source_path.suffix.lower() in {".csv", ".parquet"}:
        if source_path.is_absolute():
            resolved = source_path
        elif source_path.parts[:1] == ("workspaces",):
            resolved = layout.project_root.parents[1] / source_path
        else:
            resolved = layout.project_root / source_path
        return {
            "dataset": _repo_relative_workspace_path(resolved),
            "column": column,
            "source": "workspace_definition",
        }

    table_name = Path(source_ref).stem
    workspace_files = list(layout.project_root.rglob("*.csv")) + list(
        layout.project_root.rglob("*.parquet")
    )
    for path in workspace_files:
        if path.stem.lower() == table_name.lower() and layout.is_dataset_allowed(path):
            return {
                "dataset": _repo_relative_workspace_path(path),
                "column": column,
                "source": "workspace_definition",
            }

    return {"dataset": source_ref, "column": column, "source": "workspace_definition"}


def _split_source_column_expr(column_expr: str) -> tuple[str, str]:
    normalized = str(column_expr).replace("\\", "/").strip()
    if not normalized:
        return "", ""
    for marker in (".csv.", ".parquet."):
        if marker in normalized.lower():
            idx = normalized.lower().index(marker)
            dataset = normalized[: idx + len(marker) - 1]
            column = normalized[idx + len(marker):]
            return dataset, column
    if "." not in normalized:
        return "", normalized
    table_name, column = normalized.rsplit(".", 1)
    return table_name, column


def _repo_relative_workspace_path(path: Path) -> str:
    normalized = str(path.resolve()).replace("\\", "/")
    marker = "workspaces/"
    if marker in normalized:
        return normalized[normalized.index(marker):]
    return str(path).replace("\\", "/")


def workspace_definitions_path(layout: WorkspaceLayout) -> Path:
    return layout.contracts_dir / "workspace_feature_definitions.json"


def load_workspace_definitions(layout: WorkspaceLayout) -> dict[str, Any]:
    path = workspace_definitions_path(layout)
    if not path.exists():
        return {
            "artifact_type": "workspace_feature_definitions.json",
            "version": WORKSPACE_DEFINITIONS_VERSION,
            "generated_by": "apply-kpi-panel-answer",
            "workspace": "",
            "definitions": [],
        }
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return {
            "artifact_type": "workspace_feature_definitions.json",
            "version": WORKSPACE_DEFINITIONS_VERSION,
            "generated_by": "apply-kpi-panel-answer",
            "workspace": "",
            "definitions": [],
        }
    data.setdefault("artifact_type", "workspace_feature_definitions.json")
    data.setdefault("generated_by", "apply-kpi-panel-answer")
    data.setdefault("version", WORKSPACE_DEFINITIONS_VERSION)
    data.setdefault("definitions", [])
    return data


def upsert_workspace_definition(definitions: dict[str, Any], record: dict[str, Any]) -> None:
    target_feature = normalize(record.get("feature", ""))
    target_kpis = sorted(record.get("applies_to_kpis") or [])

    kept = [
        item
        for item in definitions.get("definitions", [])
        if not (
            normalize(item.get("feature", "")) == target_feature
            and sorted(item.get("applies_to_kpis") or []) == target_kpis
        )
    ]
    kept.append(record)
    kept.sort(
        key=lambda item: (
            normalize(item.get("feature", "")),
            str(sorted(item.get("applies_to_kpis") or [])),
        )
    )
    definitions["definitions"] = kept


def apply_workspace_definitions_to_mapping(mapping: dict[str, Any], definitions: dict[str, Any]) -> int:
    updated = 0
    for definition in sorted(definitions.get("definitions", []), key=_definition_apply_key):
        updated += apply_workspace_definition_to_mapping(mapping, definition)
    if updated:
        recompute_mapping_status(mapping)
    return updated


def _definition_apply_key(definition: dict[str, Any]) -> tuple[str, int, int]:
    feature = normalize(definition.get("feature", ""))
    applies_to = definition.get("applies_to_kpis") or []
    # Broad definitions must apply before scoped overrides. Among scoped
    # records, a larger scope is less specific than a smaller KPI subset.
    return (feature, 0 if not applies_to else 1, -len(applies_to))


def apply_workspace_definition_to_mapping(mapping: dict[str, Any], definition: dict[str, Any]) -> int:
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
            if normalize(item.get("feature", "")) != normalize(feature_name):
                continue
            if (
                item.get("state") in READY_STATES
                and item.get("state") != "user_confirmed"
                and not applies_to
            ):
                continue
            apply_definition_to_feature(item, definition, kpi_id)
            updated += 1
    return updated


def apply_definition_to_feature(item: dict[str, Any], definition: dict[str, Any], kpi_id: str) -> None:
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


def recompute_mapping_status(mapping: dict[str, Any]) -> None:
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
    mapping["summary"] = summarize_mapping(mapping)
    mapping["blocker_clusters"] = prioritize_blockers(mapping)


def summarize_mapping(mapping: dict[str, Any]) -> dict[str, int]:
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


def append_decision_history(layout: WorkspaceLayout, kpi_id: str, feature: str, state: str, note: str) -> None:
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


def append_workspace_definition_requirement(
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
