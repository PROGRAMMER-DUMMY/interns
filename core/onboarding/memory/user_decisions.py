from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from core.onboarding.memory.workspace_definitions import (
    append_decision_history,
    recompute_mapping_status,
)
from core.storage.atomic_io import read_json_or_quarantine, write_text_atomic
from core.storage.workspace_layout import WorkspaceLayout
from core.storage.workspace_lock import workspace_lock


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

    # This legacy direct-apply path (feature_resolver.py --apply-decision)
    # does NOT route through run_workspace_command/workspace_lock the way
    # blocker_cli.py's apply-kpi-panel-answer does -- without its own lock
    # here, a read-modify-write on kpi_feature_mapping.json/requirements.json
    # racing either itself or the locked panel-answer path corrupts either
    # file. write_text_atomic only makes the FINAL write atomic; it does
    # nothing for the read-then-modify gap this function's own read creates.
    with workspace_lock(workspace_path):
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
                apply_decision_to_feature(
                    item,
                    state=state,
                    resolution_type=resolution_type,
                    evidence_note=evidence_note,
                    source_columns=source_columns,
                )
                updated = True
        if not updated:
            raise ValueError(f"Feature not found: {kpi_id}/{feature}")

        recompute_mapping_status(mapping)
        write_text_atomic(mapping_path, json.dumps(mapping, indent=2, default=str) + "\n")
        append_decision_history(layout, kpi_id, feature, state, evidence_note)
        append_user_decision_requirement(
            layout,
            kpi_id=kpi_id,
            feature=feature,
            state=state,
            resolution_type=resolution_type,
            evidence_note=evidence_note,
        )
        return mapping["summary"]


def apply_decision_to_feature(
    item: dict[str, Any],
    *,
    state: str,
    resolution_type: str,
    evidence_note: str,
    source_columns: list[str] | None = None,
) -> None:
    timestamp = datetime.now(timezone.utc).isoformat()
    item["state"] = state
    item["resolution_type"] = resolution_type
    item.setdefault("evidence", []).append(
        {
            "type": "user_confirmed" if state == "user_confirmed" else "user_decision",
            "source": "cli",
            "detail": evidence_note,
            "timestamp": timestamp,
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
            "timestamp": timestamp,
        }
    )
    if state == "user_confirmed" or state.startswith("proven_"):
        item["question"] = None


def append_user_decision_requirement(
    layout: WorkspaceLayout,
    *,
    kpi_id: str,
    feature: str,
    state: str,
    resolution_type: str,
    evidence_note: str,
) -> None:
    requirements_path = layout.requirements_dir / "requirements.json"
    requirements_path.parent.mkdir(parents=True, exist_ok=True)
    # Fail loud (quarantine + raise) on corruption rather than zeroing the audit
    # mirror. Ref: ob-memory.md (T6).
    requirements = read_json_or_quarantine(requirements_path, default={}) or {}
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
    write_text_atomic(requirements_path, json.dumps(requirements, indent=2) + "\n")
