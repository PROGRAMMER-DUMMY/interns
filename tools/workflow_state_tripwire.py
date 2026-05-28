"""Workflow state tripwire.

Snapshots user-decided state across workspace artifacts before a CLI op,
then verifies the same state is still present after. Catches the class of
bug where a rebuild silently wipes user_confirmed / approved / rejected
markers (the relationship-rebuild loop discovered in 2026-05-28).

Generic across workspaces. Discovers artifacts via WorkspaceLayout, never
hardcodes a workspace name or domain. Intended to be run in CI across
(empty / mid-resolution / fully-approved) workspace fixtures.

Usage:

    from tools.workflow_state_tripwire import (
        snapshot_user_state,
        verify_user_state_preserved,
    )

    snap = snapshot_user_state(layout)
    run_command_that_might_wipe_state()
    diff = verify_user_state_preserved(layout, snap)
    if diff.regressions:
        raise AssertionError(diff.report())
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from core.storage.workspace_layout import WorkspaceLayout


USER_DECIDED_RELATIONSHIP_STATES = {"user_confirmed", "rejected"}
USER_DECIDED_KPI_FEATURE_KEYS = {"user_confirmed", "user_decision", "evidence_state"}


@dataclass(frozen=True)
class StateSnapshot:
    """A frozen snapshot of user-decided state across workspace artifacts."""

    relationship_states: dict[str, str] = field(default_factory=dict)
    kpi_feature_decisions: dict[str, str] = field(default_factory=dict)
    applied_op_ids: set[str] = field(default_factory=set)


@dataclass(frozen=True)
class StateDiff:
    """Differences between two snapshots. Regressions = user-state loss."""

    regressions: list[str] = field(default_factory=list)
    additions: list[str] = field(default_factory=list)
    removed_ops: list[str] = field(default_factory=list)

    def report(self) -> str:
        lines = ["workflow-state-tripwire results:"]
        if self.regressions:
            lines.append("  REGRESSIONS (user state lost):")
            lines.extend(f"    - {item}" for item in self.regressions)
        else:
            lines.append("  No regressions detected.")
        if self.additions:
            lines.append("  Additions (informational):")
            lines.extend(f"    - {item}" for item in self.additions)
        if self.removed_ops:
            lines.append("  Removed applied-op records (suspicious):")
            lines.extend(f"    - {item}" for item in self.removed_ops)
        return "\n".join(lines)

    @property
    def ok(self) -> bool:
        return not self.regressions and not self.removed_ops


def _read_json(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return {}
    return data if isinstance(data, dict) else {}


def _read_applied_op_ids(layout: WorkspaceLayout) -> set[str]:
    path = layout.state_dir / "applied_ops.jsonl"
    if not path.exists():
        return set()
    ids: set[str] = set()
    try:
        with path.open("r", encoding="utf-8") as handle:
            for line in handle:
                line = line.strip()
                if not line:
                    continue
                try:
                    record = json.loads(line)
                except json.JSONDecodeError:
                    continue
                op_id = str(record.get("op_id") or "").strip()
                if op_id:
                    ids.add(op_id)
    except OSError:
        return ids
    return ids


def snapshot_user_state(layout: WorkspaceLayout) -> StateSnapshot:
    """Read all user-decided state out of the workspace's contract artifacts."""

    rel_states: dict[str, str] = {}
    relationships = _read_json(layout.relationship_contracts_path)
    for rel in relationships.get("relationships") or []:
        if not isinstance(rel, dict):
            continue
        rid = str(rel.get("relationship_id") or "").strip()
        state = str(rel.get("state") or "")
        if rid and state in USER_DECIDED_RELATIONSHIP_STATES:
            rel_states[rid] = state

    feature_decisions: dict[str, str] = {}
    mapping = _read_json(layout.kpi_feature_mapping_path)
    for kpi in mapping.get("kpis") or []:
        if not isinstance(kpi, dict):
            continue
        kpi_id = str(kpi.get("kpi_id") or "")
        for feature in kpi.get("features") or []:
            if not isinstance(feature, dict):
                continue
            name = str(feature.get("name") or feature.get("feature") or "").strip()
            evidence = str(feature.get("evidence_state") or feature.get("user_decision") or "").strip()
            if not name or not evidence:
                continue
            if evidence in {"user_confirmed", "accepted_workspace_definition", "rejected"}:
                feature_decisions[f"{kpi_id}::{name}"] = evidence

    return StateSnapshot(
        relationship_states=rel_states,
        kpi_feature_decisions=feature_decisions,
        applied_op_ids=_read_applied_op_ids(layout),
    )


def verify_user_state_preserved(layout: WorkspaceLayout, prior: StateSnapshot) -> StateDiff:
    """Compare current state to a prior snapshot. Any lost user decision is a regression."""

    current = snapshot_user_state(layout)
    regressions: list[str] = []
    additions: list[str] = []

    for rid, prior_state in prior.relationship_states.items():
        now_state = current.relationship_states.get(rid)
        if now_state is None:
            regressions.append(
                f"relationship `{rid}` lost user state `{prior_state}` (no longer present in "
                "relationship_contracts.json)"
            )
        elif now_state != prior_state:
            regressions.append(
                f"relationship `{rid}` user state regressed from `{prior_state}` to `{now_state}`"
            )

    for rid, now_state in current.relationship_states.items():
        if rid not in prior.relationship_states:
            additions.append(f"new relationship user state `{rid}` = `{now_state}`")

    for key, prior_decision in prior.kpi_feature_decisions.items():
        now_decision = current.kpi_feature_decisions.get(key)
        if now_decision is None:
            regressions.append(
                f"KPI feature `{key}` lost decision `{prior_decision}` (no longer in kpi_feature_mapping.json)"
            )
        elif now_decision != prior_decision:
            regressions.append(
                f"KPI feature `{key}` decision regressed from `{prior_decision}` to `{now_decision}`"
            )

    removed_ops = sorted(prior.applied_op_ids - current.applied_op_ids)
    return StateDiff(regressions=regressions, additions=additions, removed_ops=removed_ops)


__all__ = [
    "StateDiff",
    "StateSnapshot",
    "snapshot_user_state",
    "verify_user_state_preserved",
]
