from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from core.onboarding.harness.workflow_guard_harness import WorkflowGuardHarness


class WorkflowGuardHarnessTests(unittest.TestCase):
    def test_flags_invented_created_at_in_registry_mapping_and_panel(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            workspace = root / "workspaces" / "demo"
            contracts = workspace / "interns" / "generated" / "contracts"
            profiles = workspace / "interns" / "generated" / "profiles"
            panel_dir = workspace / "interns" / "reports" / "blocker_question_panel"
            contracts.mkdir(parents=True)
            profiles.mkdir(parents=True)
            panel_dir.mkdir(parents=True)
            (contracts / "kpi_registry.json").write_text(
                json.dumps(
                    {
                        "kpis": [
                            {
                                "name": "What is trend for paid amount?",
                                "metric": "amount paid",
                                "cuts": "LOB = Medicare, created_at",
                            }
                        ]
                    }
                ),
                encoding="utf-8",
            )
            (contracts / "kpi_feature_mapping.json").write_text(
                json.dumps(
                    {
                        "kpis": [
                            {
                                "kpi_id": "kpi_001",
                                "name": "What is trend for paid amount?",
                                "metric": "amount paid",
                                "cuts": "LOB = Medicare, created_at",
                                "features": [
                                    {
                                        "feature": "created_at",
                                        "state": "blocked_missing_evidence",
                                    }
                                ],
                            }
                        ]
                    }
                ),
                encoding="utf-8",
            )
            (profiles / "profile_index.json").write_text(
                json.dumps(
                    {
                        "profiles": [
                            {
                                "columns": [
                                    {"name": "ServiceDate"},
                                    {"name": "PaidAmount"},
                                ]
                            }
                        ]
                    }
                ),
                encoding="utf-8",
            )
            (panel_dir / "current.json").write_text(
                json.dumps(
                    {
                        "status": "needs_user_answer",
                        "feature": "created_at",
                        "evidence_files": ["workspaces/demo/docs/Sample KPI.xlsx"],
                    }
                ),
                encoding="utf-8",
            )

            result = WorkflowGuardHarness(root, "workspaces/demo").run()

            self.assertFalse(result.ok)
            self.assertGreaterEqual(result.error_count, 3)
            current = json.loads(
                (workspace / "interns" / "reports" / "workflow_guard_harness" / "current.json").read_text(
                    encoding="utf-8"
                )
            )
            codes = {finding["code"] for finding in current["findings"]}
            self.assertIn("invented_temporal_feature", codes)
            self.assertIn("unproven_mapping_feature", codes)
            self.assertIn("blocker_panel_invented_feature", codes)

    def test_command_log_flags_unsupported_raw_read_and_missing_recovery(self):
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

            result = WorkflowGuardHarness(root, "workspaces/demo", command_log=command_log).run()

            self.assertFalse(result.ok)
            current = json.loads(
                (workspace / "interns" / "reports" / "workflow_guard_harness" / "current.json").read_text(
                    encoding="utf-8"
                )
            )
            codes = {finding["code"] for finding in current["findings"]}
            self.assertIn("unsupported_shell_command", codes)
            self.assertIn("raw_data_read_before_profile", codes)
            self.assertIn("failed_command_without_recovery", codes)

    def test_passes_source_backed_temporal_feature(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            workspace = root / "workspaces" / "demo"
            contracts = workspace / "interns" / "generated" / "contracts"
            profiles = workspace / "interns" / "generated" / "profiles"
            contracts.mkdir(parents=True)
            profiles.mkdir(parents=True)
            (contracts / "kpi_registry.json").write_text(
                json.dumps({"kpis": [{"cuts": "Month(ServiceDate), Payer"}]}),
                encoding="utf-8",
            )
            (contracts / "kpi_feature_mapping.json").write_text(
                json.dumps({"kpis": [{"kpi_id": "kpi_001", "features": []}]}),
                encoding="utf-8",
            )
            (profiles / "profile_index.json").write_text(
                json.dumps({"profiles": [{"columns": [{"name": "ServiceDate"}]}]}),
                encoding="utf-8",
            )

            result = WorkflowGuardHarness(root, "workspaces/demo").run()

            self.assertTrue(result.ok)


if __name__ == "__main__":
    unittest.main()
