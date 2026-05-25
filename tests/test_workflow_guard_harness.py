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
                        "interaction_contract": {
                            "display_mode": "project_blocker_panel",
                            "answer_source": "current.json",
                            "generic_answer_picker_allowed": False,
                        },
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

    def test_flags_generated_sql_raw_path_outside_catalog_bootstrap(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            workspace = root / "workspaces" / "demo"
            solutions = workspace / "interns" / "generated" / "solutions"
            solutions.mkdir(parents=True)
            (solutions / "kpi_001.sql").write_text(
                "CREATE OR REPLACE VIEW kpi_001_results AS\n"
                "SELECT * FROM read_csv_auto('workspaces/demo/datasets/transactions.csv');\n",
                encoding="utf-8",
            )

            result = WorkflowGuardHarness(root, "workspaces/demo").run()

            self.assertFalse(result.ok)
            current = json.loads(
                (workspace / "interns" / "reports" / "workflow_guard_harness" / "current.json").read_text(
                    encoding="utf-8"
                )
            )
            codes = {finding["code"] for finding in current["findings"]}
            self.assertIn("generated_sql_raw_path_outside_bootstrap", codes)

    def test_flags_generated_pipeline_sql_raw_path_outside_catalog_bootstrap(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            workspace = root / "workspaces" / "demo"
            pipeline = workspace / "interns" / "generated" / "pipeline"
            pipeline.mkdir(parents=True)
            (pipeline / "pipeline_layers.sql").write_text(
                "CREATE OR REPLACE VIEW silver_transactions AS\n"
                "SELECT * FROM read_csv_auto('workspaces/demo/datasets/transactions.csv');\n",
                encoding="utf-8",
            )

            result = WorkflowGuardHarness(root, "workspaces/demo").run()

            self.assertFalse(result.ok)
            current = json.loads(
                (workspace / "interns" / "reports" / "workflow_guard_harness" / "current.json").read_text(
                    encoding="utf-8"
                )
            )
            codes = {finding["code"] for finding in current["findings"]}
            self.assertIn("generated_sql_raw_path_outside_bootstrap", codes)

    def test_allows_generated_sql_raw_path_inside_catalog_bootstrap(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            workspace = root / "workspaces" / "demo"
            solutions = workspace / "interns" / "generated" / "solutions"
            solutions.mkdir(parents=True)
            (solutions / "kpi_001.sql").write_text(
                "-- BEGIN CATALOG BOOTSTRAP\n"
                "CREATE OR REPLACE VIEW catalog_raw_transactions AS\n"
                "SELECT * FROM read_csv_auto('workspaces/demo/datasets/transactions.csv');\n"
                "-- END CATALOG BOOTSTRAP\n"
                "CREATE OR REPLACE VIEW kpi_001_results AS\n"
                "SELECT * FROM catalog_raw_transactions;\n",
                encoding="utf-8",
            )

            result = WorkflowGuardHarness(root, "workspaces/demo").run()

            self.assertTrue(result.ok)

    def test_command_log_flags_panel_not_read_and_time_budget(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            workspace = root / "workspaces" / "demo"
            workspace.mkdir(parents=True)
            command_log = workspace / "commands.jsonl"
            command_log.write_text(
                json.dumps(
                    {
                        "command": "uv run prepare-kpi-blocker-panel --workspace workspaces/demo --domain healthcare",
                        "status": "ok",
                        "exit_code": 0,
                        "elapsed_seconds": 45,
                    }
                )
                + "\n"
                + json.dumps(
                    {
                        "command": "Get-Content workspaces/demo/interns/generated/contracts/kpi_feature_mapping.json",
                        "status": "ok",
                        "exit_code": 0,
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
            self.assertIn("panel_output_not_read", codes)
            self.assertIn("stage_time_budget_exceeded", codes)

    def test_external_profile_workspace_blocks_route_without_tool_registry_read(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            workspace = root / "workspaces" / "cms"
            profiles = workspace / "interns" / "generated" / "profiles"
            contracts = workspace / "interns" / "generated" / "contracts"
            docs = workspace / "docs"
            profiles.mkdir(parents=True)
            contracts.mkdir(parents=True)
            docs.mkdir(parents=True)
            (profiles / "profile_index.json").write_text(
                json.dumps({"profiles": [{"path": r"D:\raw\CMS\file_CY2025Q1.csv"}]}),
                encoding="utf-8",
            )
            (contracts / "kpi_registry.json").write_text(
                json.dumps({"kpis": []}),
                encoding="utf-8",
            )
            (docs / "source_selection.json").write_text(
                json.dumps({"sources": [{"target_kind": "dataset"}]}),
                encoding="utf-8",
            )
            command_log = workspace / "commands.jsonl"
            command_log.write_text(
                json.dumps(
                    {
                        "command": "uv run resolve-kpi-features --workspace workspaces/cms --domain CMS --include-candidates",
                        "status": "ok",
                        "exit_code": 0,
                    }
                )
                + "\n",
                encoding="utf-8",
            )

            result = WorkflowGuardHarness(root, "workspaces/cms", command_log=command_log).run()

            self.assertFalse(result.ok)
            current = json.loads(
                (workspace / "interns" / "reports" / "workflow_guard_harness" / "current.json").read_text(
                    encoding="utf-8"
                )
            )
            codes = {finding["code"] for finding in current["findings"]}
            self.assertIn("tool_registry_not_read_before_workflow_route", codes)
            self.assertIn("wrong_kpi_route_for_external_source_workspace", codes)

    def test_external_profile_workspace_blocks_kpi_generation_and_manual_selection_copy(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            workspace = root / "workspaces" / "cms"
            profiles = workspace / "interns" / "generated" / "profiles"
            contracts = workspace / "interns" / "generated" / "contracts"
            docs = workspace / "docs"
            profiles.mkdir(parents=True)
            contracts.mkdir(parents=True)
            docs.mkdir(parents=True)
            (profiles / "profile_index.json").write_text(
                json.dumps({"profiles": [{"path": r"D:\raw\CMS\file_CY2025Q1.csv"}]}),
                encoding="utf-8",
            )
            (contracts / "kpi_registry.json").write_text(json.dumps({"kpis": []}), encoding="utf-8")
            (docs / "source_selection.json").write_text(
                json.dumps({"sources": [{"target_kind": "dataset"}]}),
                encoding="utf-8",
            )
            command_log = workspace / "commands.jsonl"
            command_log.write_text(
                json.dumps(
                    {
                        "command": "Get-Content .agents/gemini/SKILLS.md",
                        "status": "ok",
                        "exit_code": 0,
                    }
                )
                + "\n"
                + json.dumps(
                    {
                        "command": "cp workspaces/cms/docs/source_selection.generated.json workspaces/cms/docs/source_selection.json",
                        "status": "ok",
                        "exit_code": 0,
                    }
                )
                + "\n"
                + json.dumps(
                    {
                        "command": "uv run prepare-kpi-generation --workspace workspaces/cms",
                        "status": "ok",
                        "exit_code": 0,
                    }
                )
                + "\n",
                encoding="utf-8",
            )

            result = WorkflowGuardHarness(root, "workspaces/cms", command_log=command_log).run()

            self.assertFalse(result.ok)
            current = json.loads(
                (workspace / "interns" / "reports" / "workflow_guard_harness" / "current.json").read_text(
                    encoding="utf-8"
                )
            )
            codes = {finding["code"] for finding in current["findings"]}
            self.assertIn("manual_source_selection_promotion", codes)
            self.assertIn("wrong_kpi_route_for_external_source_workspace", codes)
            self.assertIn("unsupported_shell_command", codes)

    def test_external_profile_workspace_allows_route_after_tool_registry_read(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            workspace = root / "workspaces" / "cms"
            profiles = workspace / "interns" / "generated" / "profiles"
            contracts = workspace / "interns" / "generated" / "contracts"
            docs = workspace / "docs"
            profiles.mkdir(parents=True)
            contracts.mkdir(parents=True)
            docs.mkdir(parents=True)
            (profiles / "profile_index.json").write_text(
                json.dumps({"profiles": [{"path": r"D:\raw\CMS\file_CY2025Q1.csv"}]}),
                encoding="utf-8",
            )
            (contracts / "kpi_registry.json").write_text(
                json.dumps({"kpis": []}),
                encoding="utf-8",
            )
            (docs / "source_selection.json").write_text(
                json.dumps({"sources": [{"target_kind": "dataset"}]}),
                encoding="utf-8",
            )
            command_log = workspace / "commands.jsonl"
            command_log.write_text(
                json.dumps(
                    {
                        "command": "Get-Content .agents/gemini/SKILLS.md",
                        "status": "ok",
                        "exit_code": 0,
                    }
                )
                + "\n"
                + json.dumps(
                    {
                        "command": "uv run build-source-family-contracts --workspace workspaces/cms",
                        "status": "ok",
                        "exit_code": 0,
                    }
                )
                + "\n",
                encoding="utf-8",
            )

            result = WorkflowGuardHarness(root, "workspaces/cms", command_log=command_log).run()

            self.assertTrue(result.ok)


if __name__ == "__main__":
    unittest.main()
