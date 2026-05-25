from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from core.onboarding.bronze_silver_standards import BronzeSilverStandardsBuilder
from core.onboarding.harness.workflow_guard_harness import WorkflowGuardHarness


class BronzeSilverStandardsTests(unittest.TestCase):
    def test_builder_writes_standards_manifest_and_reroute_policy(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            workspace = root / "workspaces" / "demo"
            workspace.mkdir(parents=True)

            result = BronzeSilverStandardsBuilder(root, "workspaces/demo", domain="healthcare").build()

            standards = json.loads((root / result.standards_path).read_text(encoding="utf-8"))
            manifest = json.loads((root / result.manifest_path).read_text(encoding="utf-8"))
            reroute = json.loads((root / result.reroute_policy_path).read_text(encoding="utf-8"))
            self.assertIn("deduplication_application", standards["baseline"]["bronze"]["forbidden_transformations"])
            self.assertIn("approved_reference_data_join", standards["baseline"]["silver"]["allowed_transformations"])
            self.assertTrue(manifest["engine_parity_policy"]["block_on_unsupported_rule"])
            self.assertEqual(reroute["max_auto_reroutes_per_rule_per_session"], 1)
            self.assertTrue((root / result.markdown_path).exists())

    def test_workflow_guard_flags_wrong_route_and_structured_reroute_details(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            workspace = root / "workspaces" / "demo"
            workspace.mkdir(parents=True)
            BronzeSilverStandardsBuilder(root, "workspaces/demo").build()
            command_log = workspace / "commands.jsonl"
            command_log.write_text(
                json.dumps(
                    {
                        "command": "uv run prepare-kpi-blocker-panel --workspace workspaces/demo --domain healthcare",
                        "status": "ok",
                        "exit_code": 0,
                    }
                )
                + "\n"
                + json.dumps(
                    {
                        "command": "uv run generate-pipeline-sql --workspace workspaces/demo",
                        "status": "ok",
                        "exit_code": 0,
                    }
                )
                + "\n",
                encoding="utf-8",
            )

            result = WorkflowGuardHarness(root, "workspaces/demo", command_log=command_log).run()

            self.assertFalse(result.ok)
            current = json.loads((root / result.current_json_path).read_text(encoding="utf-8"))
            findings = {finding["code"]: finding for finding in current["findings"]}
            self.assertIn("skip_data_quality_before_kpi_blocker", findings)
            self.assertIn("generate_code_before_contracts_and_harnesses", findings)
            details = findings["skip_data_quality_before_kpi_blocker"]["details"]
            self.assertEqual(details["violated_rule_id"], "skip_data_quality_before_kpi_or_generation")
            self.assertIn("replacement_command", details)

    def test_workflow_guard_flags_workspace_source_mutation(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            workspace = root / "workspaces" / "demo"
            workspace.mkdir(parents=True)
            command_log = workspace / "commands.jsonl"
            command_log.write_text(
                json.dumps(
                    {
                        "command": "Move-Item -Path workspaces/demo/citation.txt -Destination workspaces/demo/citation.txt.ignored",
                        "status": "ok",
                        "exit_code": 0,
                    }
                )
                + "\n",
                encoding="utf-8",
            )

            result = WorkflowGuardHarness(root, "workspaces/demo", command_log=command_log).run()

            self.assertFalse(result.ok)
            current = json.loads((root / result.current_json_path).read_text(encoding="utf-8"))
            codes = {finding["code"] for finding in current["findings"]}
            self.assertIn("workspace_source_mutation_without_delete_plan", codes)


if __name__ == "__main__":
    unittest.main()
