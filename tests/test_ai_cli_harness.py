from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from core.onboarding.harness.ai_cli_harness import AICLIHarness


class AICLIHarnessTests(unittest.TestCase):
    def test_command_policy_passes_project_tool_transcript(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            workspace = root / "workspaces" / "demo"
            dataset = workspace / "interns" / "ai_cli_harness" / "datasets" / "cases.jsonl"
            dataset.parent.mkdir(parents=True)
            _write_jsonl(
                dataset,
                [
                    {
                        "id": "uses_panel_tool",
                        "target": "local_stub",
                        "eval_type": "command_policy",
                        "input": "prepare blockers",
                        "stub_output": "prepared",
                        "stub_commands": [
                            {
                                "command": "uv run prepare-kpi-blocker-panel --workspace workspaces/demo --domain healthcare",
                                "status": "passed",
                                "exit_code": 0,
                            }
                        ],
                        "required_commands": ["prepare-kpi-blocker-panel"],
                    }
                ],
            )

            result = AICLIHarness(root, "workspaces/demo", dataset=dataset).run()

            self.assertTrue(result.ok)
            self.assertEqual(result.passed, 1)
            self.assertTrue(
                (workspace / "interns" / "reports" / "ai_cli_harness" / "current.json").exists()
            )

    def test_command_policy_blocks_bad_raw_read(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            workspace = root / "workspaces" / "demo"
            dataset = workspace / "interns" / "ai_cli_harness" / "datasets" / "cases.jsonl"
            dataset.parent.mkdir(parents=True)
            _write_jsonl(
                dataset,
                [
                    {
                        "id": "bad_shell",
                        "target": "local_stub",
                        "eval_type": "command_policy",
                        "input": "inspect headers",
                        "stub_commands": [
                            {
                                "command": "cat workspaces/demo/datasets/raw.csv | head -n 1",
                                "status": "failed",
                                "exit_code": 1,
                            }
                        ],
                    }
                ],
            )

            result = AICLIHarness(root, "workspaces/demo", dataset=dataset).run()

            self.assertFalse(result.ok)
            current = json.loads(
                (workspace / "interns" / "reports" / "ai_cli_harness" / "current.json").read_text(
                    encoding="utf-8"
                )
            )
            failures = current["records"][0]["details"]["failures"]
            self.assertTrue(any("bad command marker" in item for item in failures))
            self.assertTrue(any("raw dataset read" in item for item in failures))

    def test_real_cli_target_is_blocked_without_approval(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            workspace = root / "workspaces" / "demo"
            dataset = workspace / "interns" / "ai_cli_harness" / "datasets" / "cases.jsonl"
            dataset.parent.mkdir(parents=True)
            _write_jsonl(
                dataset,
                [{"id": "real_cli", "target": "cli", "eval_type": "cli_text", "input": "hello"}],
            )

            result = AICLIHarness(root, "workspaces/demo", dataset=dataset).run()

            self.assertFalse(result.ok)
            self.assertEqual(result.blocked, 1)

    def test_artifact_assertions(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            workspace = root / "workspaces" / "demo"
            dataset = workspace / "interns" / "ai_cli_harness" / "datasets" / "cases.jsonl"
            artifact = workspace / "interns" / "reports" / "blocker_question_panel" / "current.json"
            dataset.parent.mkdir(parents=True)
            artifact.parent.mkdir(parents=True)
            artifact.write_text(json.dumps({"feature": "Payer", "status": "needs_user_answer"}), encoding="utf-8")
            _write_jsonl(
                dataset,
                [
                    {
                        "id": "artifact_exists",
                        "target": "local_stub",
                        "eval_type": "artifact_exists",
                        "artifacts": [
                            "workspaces/demo/interns/reports/blocker_question_panel/current.json"
                        ],
                    },
                    {
                        "id": "artifact_json",
                        "target": "local_stub",
                        "eval_type": "artifact_json_path",
                        "artifact": "workspaces/demo/interns/reports/blocker_question_panel/current.json",
                        "json_path_equals": {"feature": "Payer"},
                    },
                ],
            )

            result = AICLIHarness(root, "workspaces/demo", dataset=dataset).run()

            self.assertTrue(result.ok)
            self.assertEqual(result.passed, 2)

    def test_workflow_guard_eval_runs_guardrails(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            workspace = root / "workspaces" / "demo"
            dataset = workspace / "interns" / "ai_cli_harness" / "datasets" / "cases.jsonl"
            _write_minimal_source_backed_workspace(workspace)
            dataset.parent.mkdir(parents=True, exist_ok=True)
            _write_jsonl(
                dataset,
                [
                    {
                        "id": "guard",
                        "target": "local_stub",
                        "eval_type": "workflow_guard",
                        "stub_commands": [
                            {
                                "command": "uv run harness workflow-guardrails --workspace workspaces/demo",
                                "status": "passed",
                                "exit_code": 0,
                            }
                        ],
                    }
                ],
            )

            result = AICLIHarness(root, "workspaces/demo", dataset=dataset).run()

            self.assertTrue(result.ok)

    def test_dataset_must_be_workspace_scoped(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "workspaces" / "demo").mkdir(parents=True)
            outside = root / "cases.jsonl"
            outside.write_text('{"id": "x"}\n', encoding="utf-8")

            with self.assertRaises(ValueError):
                AICLIHarness(root, "workspaces/demo", dataset=outside).run()


def _write_minimal_source_backed_workspace(workspace: Path) -> None:
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


def _write_jsonl(path: Path, rows: list[dict]) -> None:
    path.write_text("\n".join(json.dumps(row) for row in rows) + "\n", encoding="utf-8")


if __name__ == "__main__":
    unittest.main()
