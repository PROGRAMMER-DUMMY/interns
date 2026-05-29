"""Regression tests: stage-triggered specialist delegation pipeline.

Locks in the agent-utilization phase. Each of the 5 wired stages must:

1. Produce a `DelegationEvent` with a populated programmatic verdict
2. Append a `delegation` event to `interns/state/trajectory.jsonl`
3. Carry a `delegation_request` payload with non-empty
   `agent` / `prompt` / `expected_return` (orchestrator can paste-and-spawn)
4. Reference the correct agent name per stage

Workspace-agnostic — uses a synthetic generic workspace, no healthcare
references.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from core.onboarding.workspace.delegation import (
    DelegationVerdict,
    record_delegation,
    recent_delegations,
    verdict_from_dashboard_summary,
    verdict_from_kpi_completion,
    verdict_from_relationship_summary,
    verdict_from_source_to_target_summary,
    verdict_from_validation_summary,
)
from core.storage.workspace_layout import WorkspaceLayout


def _make_workspace(tmp_path: Path) -> tuple[WorkspaceLayout, str]:
    workspace_rel = "workspaces/synthetic_delegation"
    workspace = tmp_path / workspace_rel
    workspace.mkdir(parents=True)
    layout = WorkspaceLayout(project_root=workspace)
    layout.ensure_runtime_dirs()
    return layout, workspace_rel


def test_data_engineer_relationship_review_delegation(tmp_path):
    layout, workspace_rel = _make_workspace(tmp_path)
    relationship_summary = {
        "relationship_count": 5,
        "executable_relationship_count": 5,
        "candidate_relationship_count": 0,
    }
    event = record_delegation(
        layout,
        workspace_rel,
        agent="data-engineer",
        stage="relationship_review",
        reason="multi-source KPI generation requires executable relationship contracts",
        verdict_fn=lambda: verdict_from_relationship_summary(relationship_summary),
    )
    assert event.agent == "data-engineer"
    assert event.stage == "relationship_review"
    assert event.verdict.status == "ok"
    assert "5/5" in event.verdict.summary

    assert event.delegation_request is not None
    request = event.delegation_request
    assert request.agent == "data-engineer"
    assert request.prompt, "prompt must be non-empty"
    assert "data-engineer" in request.prompt
    assert workspace_rel in request.prompt
    assert request.context_keys, "context_keys must be specified for the agent"
    assert request.expected_return, "expected_return shape must be declared"


def test_source_to_target_reviewer_delegation_blocked_verdict(tmp_path):
    layout, workspace_rel = _make_workspace(tmp_path)
    plan_summary = {"kpi_count": 3, "ready_kpi_count": 1, "blocked_kpi_count": 2}
    event = record_delegation(
        layout,
        workspace_rel,
        agent="source-to-target-reviewer",
        stage="source_to_target_review",
        reason="every plan must pass selected-sources + join-proof + grain checks",
        verdict_fn=lambda: verdict_from_source_to_target_summary(plan_summary),
    )
    assert event.verdict.status == "blocked"
    assert "2/3" in event.verdict.summary
    assert event.delegation_request.agent == "source-to-target-reviewer"
    assert "source_to_target_plan.json" in " ".join(event.delegation_request.context_keys)


def test_validation_gatekeeper_delegation_error_verdict(tmp_path):
    layout, workspace_rel = _make_workspace(tmp_path)
    event = record_delegation(
        layout,
        workspace_rel,
        agent="validation-gatekeeper",
        stage="artifact_validation",
        reason="every workflow-produced contract must pass schema + cross-artifact validation",
        verdict_fn=lambda: verdict_from_validation_summary({"error_count": 2, "warning_count": 1}),
    )
    assert event.verdict.status == "error"
    assert event.verdict.details["error_count"] == 2
    assert event.delegation_request.agent == "validation-gatekeeper"


def test_kpi_analyst_completion_delegation_partial_verdict(tmp_path):
    layout, workspace_rel = _make_workspace(tmp_path)
    entries = [
        {"kpi_id": "kpi_001", "status": "ok"},
        {"kpi_id": "kpi_002", "status": "error", "error": "missing relationship"},
    ]
    event = record_delegation(
        layout,
        workspace_rel,
        agent="kpi-analyst",
        stage="kpi_completion_review",
        reason="completed KPIs must be reviewed for definition+sql+result coverage",
        verdict_fn=lambda: verdict_from_kpi_completion(entries),
    )
    assert event.verdict.status == "partial"
    assert event.verdict.details["failed_kpi_ids"] == ["kpi_002"]
    assert "completed_kpis" in " ".join(event.delegation_request.context_keys)


def test_dashboard_engineer_delegation_ok_verdict(tmp_path):
    layout, workspace_rel = _make_workspace(tmp_path)
    dash_summary = {
        "kpi_count": 3,
        "spec_paths": ["dashboard/kpi_001.json", "dashboard/kpi_002.json", "dashboard/kpi_003.json"],
    }
    event = record_delegation(
        layout,
        workspace_rel,
        agent="dashboard-engineer",
        stage="dashboard_refresh",
        reason="every workflow completion must refresh dashboard specs",
        verdict_fn=lambda: verdict_from_dashboard_summary(dash_summary),
    )
    assert event.verdict.status == "ok"
    assert "3" in event.verdict.summary
    assert event.delegation_request.agent == "dashboard-engineer"
    assert "dashboard/" in " ".join(event.delegation_request.context_keys)


def test_all_five_stages_recorded_in_trajectory_log(tmp_path):
    """End-to-end: fire one delegation per stage, then read the trajectory log
    and confirm all five events landed with the right shape."""
    layout, workspace_rel = _make_workspace(tmp_path)
    stages = [
        ("data-engineer", "relationship_review",
         lambda: verdict_from_relationship_summary({"relationship_count": 1, "executable_relationship_count": 1, "candidate_relationship_count": 0})),
        ("source-to-target-reviewer", "source_to_target_review",
         lambda: verdict_from_source_to_target_summary({"kpi_count": 1, "ready_kpi_count": 1, "blocked_kpi_count": 0})),
        ("validation-gatekeeper", "artifact_validation",
         lambda: verdict_from_validation_summary({"error_count": 0, "warning_count": 0})),
        ("dashboard-engineer", "dashboard_refresh",
         lambda: verdict_from_dashboard_summary({"kpi_count": 1, "spec_paths": ["dashboard/kpi_001.json"]})),
        ("kpi-analyst", "kpi_completion_review",
         lambda: verdict_from_kpi_completion([{"kpi_id": "kpi_001", "status": "ok"}])),
    ]
    for agent, stage, fn in stages:
        record_delegation(layout, workspace_rel, agent=agent, stage=stage, reason="test", verdict_fn=fn)

    events = recent_delegations(layout, tail=20)
    assert len(events) == 5, f"expected 5 delegation events, got {len(events)}"

    seen_agents = {e["agent"] for e in events}
    assert seen_agents == {
        "data-engineer",
        "source-to-target-reviewer",
        "validation-gatekeeper",
        "dashboard-engineer",
        "kpi-analyst",
    }

    for event in events:
        assert event.get("event_type") == "delegation"
        assert event.get("agent") and event.get("stage") and event.get("status")
        assert event.get("metadata", {}).get("details") is not None


def test_verdict_fn_exception_yields_error_status(tmp_path):
    """If a verdict function raises, the delegation is still recorded with status=error."""
    layout, workspace_rel = _make_workspace(tmp_path)

    def _boom() -> DelegationVerdict:
        raise RuntimeError("verdict computation failed")

    event = record_delegation(
        layout,
        workspace_rel,
        agent="data-engineer",
        stage="relationship_review",
        reason="test exception path",
        verdict_fn=_boom,
    )
    assert event.verdict.status == "error"
    assert "verdict computation failed" in event.verdict.summary
    events = recent_delegations(layout, tail=5)
    assert len(events) == 1
    assert events[0]["status"] == "error"


def test_delegation_request_prompt_is_paste_ready(tmp_path):
    """The prompt field must be self-contained — orchestrator should not need to add context."""
    layout, workspace_rel = _make_workspace(tmp_path)
    event = record_delegation(
        layout,
        workspace_rel,
        agent="kpi-analyst",
        stage="kpi_completion_review",
        reason="self-contained prompt check",
        verdict_fn=lambda: verdict_from_kpi_completion([{"kpi_id": "kpi_001", "status": "ok"}]),
    )
    prompt = event.delegation_request.prompt
    assert "kpi-analyst" in prompt
    assert workspace_rel in prompt
    assert event.verdict.status in prompt
    assert event.verdict.summary in prompt
