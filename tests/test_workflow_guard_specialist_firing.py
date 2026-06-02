"""Enforce that listed required_specialists actually FIRE (advisory -> enforced).

Runtime counterpart to roster_not_routed: a completed stage that listed
required_specialists but in which none of them fired (no hand-off note, no
recorded review, no trajectory step naming them) is surfaced.
"""
from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from core.onboarding.harness.workflow_guard_harness import WorkflowGuardHarness


def _harness(tmp: Path) -> WorkflowGuardHarness:
    ws = tmp / "workspaces" / "demo"
    ws.mkdir(parents=True)
    h = WorkflowGuardHarness(tmp, "workspaces/demo")
    h.layout.ensure_runtime_dirs()
    return h


def _write_panel(h: WorkflowGuardHarness, *, status: str, specialists: list[str]) -> None:
    panel_dir = h.layout.reports_dir / "kpi_generation"
    panel_dir.mkdir(parents=True, exist_ok=True)
    (panel_dir / "current.json").write_text(
        json.dumps({
            "artifact_type": "kpi_generation/current.json",
            "stage": "kpi_generation",
            "status": status,
            "required_specialists": specialists,
            "suggested_skills": ["kpi-clarification"],
        }),
        encoding="utf-8",
    )


def _write_trajectory(h: WorkflowGuardHarness, command: str = "uv run onboard-workspace") -> None:
    (h.layout.state_dir / "trajectory.jsonl").write_text(
        json.dumps({"event_type": "tool_result", "command": command, "status": "ok"}) + "\n",
        encoding="utf-8",
    )


def _codes(findings: list[dict]) -> set[str]:
    return {f["code"] for f in findings}


class SpecialistFiringTests(unittest.TestCase):
    def test_completed_stage_with_unfired_specialist_is_flagged(self):
        with tempfile.TemporaryDirectory() as t:
            h = _harness(Path(t))
            _write_panel(h, status="complete", specialists=["kpi-analyst"])
            _write_trajectory(h)
            findings = h._check_required_specialists_fired()
            self.assertIn("required_specialist_not_fired", _codes(findings))

    def test_handoff_note_counts_as_fired(self):
        with tempfile.TemporaryDirectory() as t:
            h = _harness(Path(t))
            _write_panel(h, status="complete", specialists=["kpi-analyst"])
            _write_trajectory(h)
            handoffs = h.layout.state_dir / "handoffs"
            handoffs.mkdir(parents=True, exist_ok=True)
            (handoffs / "kpi-analyst.md").write_text("review handoff", encoding="utf-8")
            findings = h._check_required_specialists_fired()
            self.assertNotIn("required_specialist_not_fired", _codes(findings))

    def test_trajectory_command_naming_specialist_counts_as_fired(self):
        with tempfile.TemporaryDirectory() as t:
            h = _harness(Path(t))
            _write_panel(h, status="complete", specialists=["kpianalyst"])
            _write_trajectory(h, command="uv run workspace-flow review kpianalyst verdict ok")
            findings = h._check_required_specialists_fired()
            self.assertNotIn("required_specialist_not_fired", _codes(findings))

    def test_non_terminal_stage_is_not_flagged(self):
        with tempfile.TemporaryDirectory() as t:
            h = _harness(Path(t))
            _write_panel(h, status="needs_user_answer", specialists=["kpi-analyst"])
            _write_trajectory(h)
            findings = h._check_required_specialists_fired()
            self.assertNotIn("required_specialist_not_fired", _codes(findings))

    def test_unmonitored_session_is_not_flagged_here(self):
        with tempfile.TemporaryDirectory() as t:
            h = _harness(Path(t))
            _write_panel(h, status="complete", specialists=["kpi-analyst"])
            # no trajectory.jsonl -> handled by _check_session_monitored, not here
            findings = h._check_required_specialists_fired()
            self.assertEqual(findings, [])

    def test_no_specialists_listed_is_not_flagged(self):
        with tempfile.TemporaryDirectory() as t:
            h = _harness(Path(t))
            _write_panel(h, status="complete", specialists=[])
            _write_trajectory(h)
            findings = h._check_required_specialists_fired()
            self.assertEqual(findings, [])


if __name__ == "__main__":
    unittest.main()
