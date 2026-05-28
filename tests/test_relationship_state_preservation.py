"""Regression tests: relationship-rebuild must not wipe user state.

Locks in the 2026-05-28 loop-bug fix: RelationshipContractBuilder.build()
re-derives relationships from profiles + docs every time, but must preserve
any `user_confirmed` or `rejected` state that prior apply-relationship-answer
calls wrote to the file.

Uses a synthetic generic (non-healthcare) workspace so the regression test
proves the platform is workspace-agnostic. No KPI semantics involved — pure
relationship-state preservation.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from core.onboarding.relationships.contracts import (
    RelationshipContractBuilder,
    apply_relationship_answer,
)
from core.storage.workspace_layout import WorkspaceLayout
from tools.workflow_state_tripwire import (
    snapshot_user_state,
    verify_user_state_preserved,
)


def _make_synthetic_workspace(tmp_path: Path) -> tuple[Path, str]:
    """Create a minimal generic workspace under tmp_path.

    Two tiny CSV datasets with a documented FK in a generic data dictionary.
    No domain words, no healthcare/hospital references. The builder reads
    the dictionary and infers an executable relationship.
    """
    repo_root = tmp_path
    workspace_rel = "workspaces/synthetic_generic"
    workspace = repo_root / workspace_rel
    workspace.mkdir(parents=True)
    layout = WorkspaceLayout(project_root=workspace)
    layout.ensure_runtime_dirs()
    (workspace / "left.csv").write_text(
        "shared_id,value\n1,a\n2,b\n3,c\n", encoding="utf-8"
    )
    (workspace / "right.csv").write_text(
        "shared_id,label\n1,x\n2,y\n3,z\n", encoding="utf-8"
    )
    (workspace / "data_dictionary.csv").write_text(
        "table,column,description\n"
        "left,shared_id,Primary key for the left table.\n"
        "right,shared_id,Foreign key to the left table shared_id.\n",
        encoding="utf-8",
    )
    profile_index = {
        "profiles": [
            {
                "path": f"{workspace_rel}/left.csv",
                "schema": {"shared_id": "Int64", "value": "String"},
                "profile_path": f"{workspace_rel}/interns/generated/profiles/left.csv.profile.json",
            },
            {
                "path": f"{workspace_rel}/right.csv",
                "schema": {"shared_id": "Int64", "label": "String"},
                "profile_path": f"{workspace_rel}/interns/generated/profiles/right.csv.profile.json",
            },
        ]
    }
    layout.profile_index_path.write_text(
        json.dumps(profile_index, indent=2), encoding="utf-8"
    )
    (layout.contracts_dir / "domain_model.json").write_text(
        json.dumps({"data_models": [f"{workspace_rel}/data_dictionary.csv"]}),
        encoding="utf-8",
    )
    return repo_root, workspace_rel


def test_rebuild_preserves_user_confirmed_state(tmp_path: Path) -> None:
    repo_root, workspace_rel = _make_synthetic_workspace(tmp_path)
    layout = WorkspaceLayout(project_root=(repo_root / workspace_rel).resolve())

    first = RelationshipContractBuilder(repo_root, workspace_rel).build()
    assert first.relationship_count >= 1, "synthetic workspace must yield a candidate FK"

    contract = json.loads(layout.relationship_contracts_path.read_text(encoding="utf-8"))
    rid = contract["relationships"][0]["relationship_id"]
    apply_relationship_answer(
        repo_root,
        workspace_rel,
        relationship_id=rid,
        answer="approve",
    )

    snap = snapshot_user_state(layout)
    assert snap.relationship_states.get(rid) == "user_confirmed", (
        "apply-relationship-answer must persist user_confirmed state"
    )

    RelationshipContractBuilder(repo_root, workspace_rel).build()

    diff = verify_user_state_preserved(layout, snap)
    assert diff.ok, diff.report()

    after = json.loads(layout.relationship_contracts_path.read_text(encoding="utf-8"))
    matched = [r for r in after["relationships"] if r.get("relationship_id") == rid]
    assert matched, "approved relationship must still be present after rebuild"
    assert matched[0]["state"] == "user_confirmed", (
        "rebuild must NOT downgrade user_confirmed back to profile_validated"
    )
    history = matched[0].get("decision_history") or []
    preserved_marker = any(
        entry.get("source") == "build-relationship-contracts.preserve"
        for entry in history
    )
    assert preserved_marker, (
        "decision_history must carry a `preserve` marker after rebuild for traceability"
    )


def test_tripwire_detects_state_regression(tmp_path: Path) -> None:
    """Belt-and-suspenders: if the preservation code ever regresses,
    the tripwire alone (not just the test above) must catch it.
    """
    repo_root, workspace_rel = _make_synthetic_workspace(tmp_path)
    layout = WorkspaceLayout(project_root=(repo_root / workspace_rel).resolve())

    RelationshipContractBuilder(repo_root, workspace_rel).build()
    contract = json.loads(layout.relationship_contracts_path.read_text(encoding="utf-8"))
    rid = contract["relationships"][0]["relationship_id"]
    apply_relationship_answer(
        repo_root, workspace_rel, relationship_id=rid, answer="approve"
    )

    snap = snapshot_user_state(layout)

    fake = json.loads(layout.relationship_contracts_path.read_text(encoding="utf-8"))
    for rel in fake["relationships"]:
        rel["state"] = "profile_validated"
    layout.relationship_contracts_path.write_text(
        json.dumps(fake, indent=2), encoding="utf-8"
    )

    diff = verify_user_state_preserved(layout, snap)
    assert not diff.ok, "tripwire must flag a manual state regression"
    assert any("regressed" in msg or "lost user state" in msg for msg in diff.regressions)
