"""Regression tests: dashboard spec refresh must preserve user_overrides.

Mirrors the relationship-state preservation pattern. The two-section
machine_defaults/user_overrides contract is the load-bearing UX promise of
the dashboard: once a user edits user_overrides, no automated regeneration
ever destroys their work.

Uses a fully synthetic generic workspace (no healthcare/RCM references)
to lock in workspace-agnosticism.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from core.dashboard.agent_panel import (
    apply_panel_override,
    parse_panel_request,
)
from core.dashboard.spec import (
    load_kpi_spec,
    merge_spec,
    refresh_workspace_dashboard,
)
from core.storage.workspace_layout import WorkspaceLayout


def _make_synthetic_workspace(tmp_path: Path) -> tuple[Path, str]:
    """Build a generic workspace with a tiny KPI registry. No domain words."""
    repo_root = tmp_path
    workspace_rel = "workspaces/synthetic_dashboard"
    workspace = repo_root / workspace_rel
    workspace.mkdir(parents=True)
    layout = WorkspaceLayout(project_root=workspace)
    layout.ensure_runtime_dirs()
    (workspace / "facts.csv").write_text(
        "event_date,segment,value\n"
        "2025-01-01,alpha,10\n2025-01-02,beta,12\n2025-01-03,alpha,15\n",
        encoding="utf-8",
    )
    profile_index = {
        "profiles": [
            {
                "path": f"{workspace_rel}/facts.csv",
                "schema": {"event_date": "Date", "segment": "String", "value": "Int64"},
                "profile_path": f"{workspace_rel}/interns/generated/profiles/facts.csv.profile.json",
            }
        ]
    }
    layout.profile_index_path.write_text(json.dumps(profile_index), encoding="utf-8")
    layout.kpi_registry_path.parent.mkdir(parents=True, exist_ok=True)
    layout.kpi_registry_path.write_text(
        json.dumps(
            {
                "kpis": [
                    {
                        "kpi_id": "kpi_001",
                        "name": "Daily value trend",
                        "business_question": "How does daily value trend across segments?",
                        "metric": "sum(value)",
                        "cuts": "event_date, segment",
                    },
                    {
                        "kpi_id": "kpi_002",
                        "name": "Total value",
                        "business_question": "What is total value?",
                        "metric": "sum(value)",
                        "cuts": "",
                    },
                ]
            }
        ),
        encoding="utf-8",
    )
    return repo_root, workspace_rel


def test_refresh_creates_specs_for_each_kpi(tmp_path: Path) -> None:
    repo_root, workspace_rel = _make_synthetic_workspace(tmp_path)
    layout = WorkspaceLayout(project_root=(repo_root / workspace_rel).resolve())

    summary = refresh_workspace_dashboard(layout)

    assert summary["kpi_count"] == 2
    assert sorted(summary["spec_paths"]) == [
        "dashboard/kpi_001.json",
        "dashboard/kpi_002.json",
    ]
    assert (layout.project_root / "dashboard" / "index.json").exists()

    kpi1 = load_kpi_spec(layout, "kpi_001")
    assert kpi1 is not None
    assert kpi1.machine_defaults["chart_type"] == "line"

    kpi2 = load_kpi_spec(layout, "kpi_002")
    assert kpi2 is not None
    assert kpi2.machine_defaults["chart_type"] == "big_number"


def test_refresh_preserves_user_overrides(tmp_path: Path) -> None:
    repo_root, workspace_rel = _make_synthetic_workspace(tmp_path)
    layout = WorkspaceLayout(project_root=(repo_root / workspace_rel).resolve())

    refresh_workspace_dashboard(layout)
    spec_path = layout.project_root / "dashboard" / "kpi_001.json"
    payload = json.loads(spec_path.read_text(encoding="utf-8"))
    payload["user_overrides"] = {
        "chart_type": "bar",
        "title": "Custom — do not overwrite",
        "color": "segment",
    }
    spec_path.write_text(json.dumps(payload), encoding="utf-8")

    refresh_workspace_dashboard(layout)

    after = json.loads(spec_path.read_text(encoding="utf-8"))
    assert after["user_overrides"] == {
        "chart_type": "bar",
        "title": "Custom — do not overwrite",
        "color": "segment",
    }, "user_overrides MUST survive a refresh verbatim"
    assert after["machine_defaults"]["chart_type"] == "line", (
        "machine_defaults should still reflect the inferred default, untouched by overrides"
    )

    merged = merge_spec(after["machine_defaults"], after["user_overrides"])
    assert merged["chart_type"] == "bar"
    assert merged["title"] == "Custom — do not overwrite"
    assert merged["color"] == "segment"


def test_index_preserves_its_own_user_overrides(tmp_path: Path) -> None:
    repo_root, workspace_rel = _make_synthetic_workspace(tmp_path)
    layout = WorkspaceLayout(project_root=(repo_root / workspace_rel).resolve())

    refresh_workspace_dashboard(layout)
    index_path = layout.project_root / "dashboard" / "index.json"
    payload = json.loads(index_path.read_text(encoding="utf-8"))
    payload["user_overrides"] = {
        "page_title": "My BI page",
        "show_legend": False,
        "kpi_order": ["kpi_002", "kpi_001"],
    }
    index_path.write_text(json.dumps(payload), encoding="utf-8")

    refresh_workspace_dashboard(layout)

    after = json.loads(index_path.read_text(encoding="utf-8"))
    assert after["user_overrides"] == {
        "page_title": "My BI page",
        "show_legend": False,
        "kpi_order": ["kpi_002", "kpi_001"],
    }


def test_dashboard_agent_panel_override_survives_regen(tmp_path: Path) -> None:
    """A DashboardAgent-added panel (written to user_overrides via
    apply_panel_override) must survive a machine_defaults regeneration verbatim
    AND remain visible in the merged spec the renderer reads."""
    repo_root, workspace_rel = _make_synthetic_workspace(tmp_path)
    layout = WorkspaceLayout(project_root=(repo_root / workspace_rel).resolve())

    refresh_workspace_dashboard(layout)

    # Parse a generic NL request against the synthetic columns (no domain words).
    columns = ["event_date", "segment", "value"]
    parsed = parse_panel_request(
        "I want a new plot stating value by segment",
        columns,
        rows=[
            {"event_date": "2025-01-01", "segment": "alpha", "value": 10},
            {"event_date": "2025-01-02", "segment": "beta", "value": 12},
        ],
        workspace_root=layout.project_root,
    )
    assert parsed.ok, parsed.to_dict()
    assert parsed.panel["x"] == "segment"
    assert parsed.panel["y"] == "value"

    applied = apply_panel_override(layout, "kpi_001", parsed.panel)
    assert applied["panel_count"] >= 1

    spec_path = layout.project_root / "dashboard" / "kpi_001.json"
    before = json.loads(spec_path.read_text(encoding="utf-8"))
    added = before["user_overrides"]["panels"][-1]
    assert added["x"] == "segment"
    assert added["source"] == "dashboard-agent"
    # The agent edit must NOT have written into machine_defaults.
    assert added not in (before["machine_defaults"].get("panels") or [])

    # Regenerate machine_defaults — user_overrides (incl. our panel) must survive.
    refresh_workspace_dashboard(layout)

    after = json.loads(spec_path.read_text(encoding="utf-8"))
    assert after["user_overrides"]["panels"][-1] == added, (
        "the DashboardAgent panel in user_overrides MUST survive a regen verbatim"
    )

    # And the merged spec the renderer reads must include the user's panel.
    merged_spec = load_kpi_spec(layout, "kpi_001")
    assert merged_spec is not None
    merged_titles = [p.get("title") for p in (merged_spec.config.get("panels") or [])]
    assert added["title"] in merged_titles


def test_dashboard_agent_refuses_redacted_column(tmp_path: Path) -> None:
    """The helper must never build a panel on a display-redacted column."""
    repo_root, workspace_rel = _make_synthetic_workspace(tmp_path)
    layout = WorkspaceLayout(project_root=(repo_root / workspace_rel).resolve())

    # A column named exactly "name" matches the default PII patterns (^name$)
    # -> must be rejected as an axis, and with no other dimension the request fails.
    parsed = parse_panel_request(
        "plot value by name",
        ["name", "value"],
        rows=[{"name": "x", "value": 1}, {"name": "y", "value": 2}],
        workspace_root=layout.project_root,
    )
    assert not parsed.ok
    assert any("name" in r for r in parsed.rejected)
