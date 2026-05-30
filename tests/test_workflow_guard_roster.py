import json
import tempfile
import unittest
from pathlib import Path

from core.onboarding.harness.workflow_guard_harness import WorkflowGuardHarness


class RosterUtilizationGuardTests(unittest.TestCase):
    def _panel(self, root: Path, payload: dict) -> WorkflowGuardHarness:
        ws = root / "workspaces" / "demo"
        panel_dir = ws / "interns" / "reports" / "kpi_generation"
        panel_dir.mkdir(parents=True, exist_ok=True)
        (panel_dir / "current.json").write_text(json.dumps(payload), encoding="utf-8")
        return WorkflowGuardHarness(root, "workspaces/demo")

    def test_panel_without_routing_warns(self):
        with tempfile.TemporaryDirectory() as tmp:
            harness = self._panel(Path(tmp), {"stage": "route_selection"})
            codes = [f["code"] for f in harness._check_roster_utilization()]
            self.assertIn("roster_not_routed", codes)

    def test_panel_with_routing_is_clean(self):
        with tempfile.TemporaryDirectory() as tmp:
            harness = self._panel(
                Path(tmp),
                {"stage": "route_selection",
                 "suggested_skills": [{"name": "grill-requirements", "active": True}],
                 "required_specialists": ["business-analyst"]},
            )
            codes = [f["code"] for f in harness._check_roster_utilization()]
            self.assertNotIn("roster_not_routed", codes)


if __name__ == "__main__":
    unittest.main()
