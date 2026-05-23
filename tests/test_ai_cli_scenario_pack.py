from __future__ import annotations

import json
import unittest
from pathlib import Path


SCENARIO_PACK = Path("config/ai_cli_harness.workflow_scenarios.example.jsonl")
EVAL_TYPES = {
    "artifact_exists",
    "artifact_json_path",
    "cli_text",
    "command_policy",
    "workflow_guard",
}
TARGETS = {"cli", "local_stub"}
REQUIRED_WORKFLOW_STAGES = {
    "workspace_startup",
    "failed_command_recovery",
    "raw_data_policy",
    "blocker_panel_usage",
    "cleanup_approval",
    "kpi_to_sql_path",
}


class AICLIScenarioPackTests(unittest.TestCase):
    def test_example_scenario_pack_is_valid_jsonl(self):
        cases = _load_jsonl(SCENARIO_PACK)

        self.assertGreaterEqual(len(cases), len(REQUIRED_WORKFLOW_STAGES))
        self.assertEqual(len({case["id"] for case in cases}), len(cases))

    def test_example_scenario_pack_matches_harness_shape(self):
        for case in _load_jsonl(SCENARIO_PACK):
            with self.subTest(case=case["id"]):
                self.assertIsInstance(case["id"], str)
                self.assertTrue(case["id"].strip())
                self.assertIn(case["target"], TARGETS)
                self.assertIn(case["eval_type"], EVAL_TYPES)
                self.assertIsInstance(case["workflow_stage"], str)
                self.assertTrue(case["workflow_stage"].strip())
                self.assertIsInstance(case["input"], str)
                self.assertTrue(case["input"].strip())
                self.assertIsInstance(case.get("tags"), list)
                self.assertIn("workflow_scenario", case["tags"])

                if case["eval_type"] == "command_policy":
                    self.assertIsInstance(case.get("stub_commands"), list)
                    self.assertTrue(case["stub_commands"])
                    self.assertIsInstance(case.get("required_commands"), list)
                    self.assertTrue(case["required_commands"])
                    for command in case["stub_commands"]:
                        self.assertIsInstance(command.get("command"), str)
                        self.assertIn(command.get("status"), {"passed", "failed"})
                        self.assertIsInstance(command.get("exit_code"), int)

    def test_example_scenario_pack_covers_required_workflows(self):
        stages = {case["workflow_stage"] for case in _load_jsonl(SCENARIO_PACK)}

        self.assertTrue(REQUIRED_WORKFLOW_STAGES.issubset(stages))

    def test_governance_scenarios_encode_expected_safety_constraints(self):
        cases = {case["workflow_stage"]: case for case in _load_jsonl(SCENARIO_PACK)}

        raw_data = cases["raw_data_policy"]
        self.assertTrue(any("/profiles/" in command["command"].replace("\\", "/") for command in raw_data["stub_commands"]))
        self.assertTrue(any("/datasets" in item.replace("\\", "/") for item in raw_data["forbidden_commands"]))

        cleanup = cases["cleanup_approval"]
        self.assertIn("--apply", cleanup["forbidden_commands"])
        self.assertIn("--confirm-delete", cleanup["forbidden_commands"])

        blocker = cases["blocker_panel_usage"]
        self.assertTrue(any("prepare-kpi-blocker-panel" in item for item in blocker["required_commands"]))
        self.assertTrue(any("validate-workspace-artifacts" in item for item in blocker["required_commands"]))

        sql_path = cases["kpi_to_sql_path"]
        self.assertTrue(any("plan-source-to-target" in item for item in sql_path["required_commands"]))
        self.assertTrue(any("build-relationship-contracts" in item for item in sql_path["required_commands"]))


def _load_jsonl(path: Path) -> list[dict]:
    rows = []
    for line_number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
        if not line.strip():
            continue
        try:
            row = json.loads(line)
        except json.JSONDecodeError as exc:
            raise AssertionError(f"{path}:{line_number} is not valid JSON: {exc}") from exc
        if not isinstance(row, dict):
            raise AssertionError(f"{path}:{line_number} must contain a JSON object")
        rows.append(row)
    return rows


if __name__ == "__main__":
    unittest.main()
