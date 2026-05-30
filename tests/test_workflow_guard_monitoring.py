import json
import tempfile
import unittest
from pathlib import Path

from core.onboarding.harness.workflow_guard_harness import WorkflowGuardHarness


def _harness(root: Path) -> WorkflowGuardHarness:
    (root / "workspaces" / "demo" / "interns" / "state").mkdir(parents=True, exist_ok=True)
    return WorkflowGuardHarness(root, "workspaces/demo")


def _state(root: Path) -> Path:
    return root / "workspaces" / "demo" / "interns" / "state"


class SessionMonitoringGuardTests(unittest.TestCase):
    def test_activity_without_trajectory_warns(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            h = _harness(root)
            (_state(root) / "applied_ops.jsonl").write_text('{"op":"x"}\n', encoding="utf-8")
            codes = [f["code"] for f in h._check_session_monitored()]
            self.assertIn("session_not_monitored", codes)

    def test_activity_with_trajectory_is_clean(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            h = _harness(root)
            (_state(root) / "applied_ops.jsonl").write_text('{"op":"x"}\n', encoding="utf-8")
            (_state(root) / "trajectory.jsonl").write_text(
                json.dumps({"event_type": "command", "status": "ok", "command": "uv run onboard-workspace"}) + "\n",
                encoding="utf-8",
            )
            codes = [f["code"] for f in h._check_session_monitored()]
            self.assertNotIn("session_not_monitored", codes)

    def test_no_activity_is_clean(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            h = _harness(root)  # empty workspace: nothing ran -> nothing to monitor
            self.assertEqual(h._check_session_monitored(), [])


if __name__ == "__main__":
    unittest.main()
