from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from core.onboarding.harness.reliability_suite import ReliabilitySuite, main


class ReliabilitySuiteTests(unittest.TestCase):
    def test_runs_local_safe_checks_and_skips_project_harness_without_artifacts(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            workspace = root / "workspaces" / "demo"
            workspace.mkdir(parents=True)

            result = ReliabilitySuite(root, "workspaces/demo", domain="healthcare").run()

            self.assertTrue(result.ok)
            self.assertEqual(result.failed_count, 0)
            self.assertEqual(result.skipped_count, 1)
            self.assertTrue((workspace / "interns" / "reports" / "reliability_suite" / "current.json").exists())
            self.assertTrue((workspace / "interns" / "reports" / "reliability_suite" / "current.md").exists())
            self.assertTrue((workspace / "interns" / "generated" / "evidence" / "reliability_suite" / "current.json").exists())
            report = json.loads(
                (workspace / "interns" / "reports" / "reliability_suite" / "current.json").read_text(
                    encoding="utf-8"
                )
            )
            statuses = {check["name"]: check["status"] for check in report["checks"]}
            self.assertEqual(statuses["validate-workflow-guardrails"], "passed")
            self.assertEqual(statuses["build-workspace-evidence-graph"], "passed")
            self.assertEqual(statuses["validate-project-harness"], "skipped")

    def test_failed_workflow_guardrail_blocks_suite(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            workspace = root / "workspaces" / "demo"
            workspace.mkdir(parents=True)
            command_log = workspace / "commands.jsonl"
            command_log.write_text(
                json.dumps(
                    {
                        "command": "cat workspaces/demo/datasets/raw.csv | head -n 1",
                        "status": "failed",
                        "exit_code": 1,
                    }
                )
                + "\n",
                encoding="utf-8",
            )

            result = ReliabilitySuite(
                root,
                "workspaces/demo",
                command_log=command_log,
                project_harness="skip",
            ).run()

            self.assertFalse(result.ok)
            self.assertEqual(result.status, "blocked")
            checks = {check["name"]: check for check in result.checks}
            self.assertEqual(checks["validate-workflow-guardrails"]["status"], "failed")
            self.assertIn("uv run validate-workflow-guardrails --workspace workspaces/demo", result.next_commands)

    def test_project_harness_run_mode_forces_project_harness(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            workspace = root / "workspaces" / "demo"
            workspace.mkdir(parents=True)
            harness_result = SimpleNamespace(
                ok=True,
                manifest_path="workspaces/demo/interns/generated/evidence/project_harness.json",
                report_path="workspaces/demo/interns/reports/project_harness.md",
                summary=lambda: {
                    "artifact_type": "project_harness.json",
                    "ok": True,
                    "status": "passed",
                    "next_commands": [],
                },
            )

            with patch("core.onboarding.harness.reliability_suite.ProjectHarness") as harness:
                harness.return_value.run.return_value = harness_result

                result = ReliabilitySuite(root, "workspaces/demo", project_harness="run").run()

            self.assertTrue(result.ok)
            harness.assert_called_once()
            checks = {check["name"]: check for check in result.checks}
            self.assertEqual(checks["validate-project-harness"]["status"], "passed")

    def test_cli_requires_workspace(self):
        from contextlib import redirect_stderr
        from io import StringIO

        with redirect_stderr(StringIO()), self.assertRaises(SystemExit):
            main([])


if __name__ == "__main__":
    unittest.main()
