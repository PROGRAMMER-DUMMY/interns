from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from core.onboarding.harness.trajectory_recorder import WorkspaceTrajectoryRecorder
from core.onboarding.harness.workflow_guard_harness import WorkflowGuardHarness


class WorkspaceTrajectoryRecorderTests(unittest.TestCase):
    def test_records_event_and_writes_reports(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            workspace = root / "workspaces" / "demo"
            workspace.mkdir(parents=True)

            result = WorkspaceTrajectoryRecorder(root, "workspaces/demo").record(
                event_type="command",
                status="ok",
                summary="Listed workspace.",
                command="uv run list-workspace-files --workspace workspaces/demo",
                artifact="workspaces/demo/interns/reports/example.md",
            )

            self.assertTrue(result.ok)
            self.assertEqual(result.event_count, 1)
            trajectory = workspace / "interns" / "state" / "trajectory.jsonl"
            self.assertTrue(trajectory.exists())
            row = json.loads(trajectory.read_text(encoding="utf-8").strip())
            self.assertEqual(row["event_type"], "command")
            self.assertEqual(row["status"], "ok")
            self.assertTrue((workspace / "interns" / "reports" / "trajectory" / "current.md").exists())
            self.assertTrue((workspace / "interns" / "generated" / "evidence" / "trajectory" / "current.json").exists())

    def test_redacts_secret_like_values(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            workspace = root / "workspaces" / "demo"
            workspace.mkdir(parents=True)

            WorkspaceTrajectoryRecorder(root, "workspaces/demo").record(
                event_type="command",
                status="failed",
                summary="token=super-secret-value",
                command="tool --api-key sk-1234567890abcdef",
            )

            text = (workspace / "interns" / "state" / "trajectory.jsonl").read_text(encoding="utf-8")
            self.assertNotIn("super-secret-value", text)
            self.assertNotIn("sk-1234567890abcdef", text)
            self.assertIn("<redacted>", text)

    def test_workflow_guard_consumes_trajectory_by_default(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            workspace = root / "workspaces" / "demo"
            workspace.mkdir(parents=True)
            WorkspaceTrajectoryRecorder(root, "workspaces/demo").record(
                event_type="command",
                status="failed",
                exit_code=1,
                summary="PowerShell command failed.",
                command="cat workspaces/demo/datasets/raw.csv | head -n 1",
            )

            result = WorkflowGuardHarness(root, "workspaces/demo").run()

            self.assertFalse(result.ok)
            report = json.loads(
                (workspace / "interns" / "reports" / "workflow_guard_harness" / "current.json").read_text(
                    encoding="utf-8"
                )
            )
            codes = {finding["code"] for finding in report["findings"]}
            self.assertIn("trajectory_unsupported_shell_command", codes)
            self.assertIn("trajectory_raw_data_read_before_profile", codes)
            self.assertIn("trajectory_failed_step_without_recovery", codes)


if __name__ == "__main__":
    unittest.main()
