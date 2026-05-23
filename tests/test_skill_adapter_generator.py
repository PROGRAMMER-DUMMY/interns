import json
import tempfile
import unittest
from pathlib import Path

from core.skills.adapter_generator import SkillAdapterGenerator


class SkillAdapterGeneratorContractTests(unittest.TestCase):
    def test_tool_registry_contract_is_validated_and_rendered(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            _write_skill(root)
            registry_path = root / ".agents" / "tools.json"
            registry_path.parent.mkdir(parents=True)
            registry_path.write_text(
                json.dumps(
                    {
                        "version": 1,
                        "source": "TOOLS.md",
                        "evidence_policy": {
                            "secret_display_rule": "Report only redacted secret status.",
                            "raw_dataset_rule": "Prefer generated profiles before raw datasets.",
                        },
                        "tools": [
                            {
                                "name": "demo-tool",
                                "command": "uv run demo-tool --workspace <workspace>",
                                "use_when": ["demo workflow needs a project tool"],
                                "outputs": ["interns/reports/demo.md"],
                                "safety": "local_safe",
                                "recovery": "If it fails, inspect the generated report before retrying.",
                            }
                        ],
                    }
                )
                + "\n",
                encoding="utf-8",
            )

            result = SkillAdapterGenerator(root, tools=("codex",)).run()

            index = json.loads((root / result.index_path).read_text(encoding="utf-8"))
            self.assertEqual(index["tool_registry"]["tools"][0]["name"], "demo-tool")
            self.assertEqual(index["tool_registry"]["tools"][0]["safety"], "local_safe")
            adapter = (root / ".agents" / "codex" / "SKILLS.md").read_text(encoding="utf-8")
            self.assertIn("## Project Tool Registry", adapter)
            self.assertIn("Secret display safety: Report only redacted secret status.", adapter)
            self.assertIn("Dataset access safety: Prefer generated profiles before raw datasets.", adapter)
            self.assertIn("- `demo-tool`", adapter)
            self.assertIn("Safety: local_safe", adapter)
            self.assertIn("Recovery: If it fails, inspect the generated report before retrying.", adapter)

    def test_tool_registry_requires_core_contract_fields(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            _write_skill(root)
            registry_path = root / ".agents" / "tools.json"
            registry_path.parent.mkdir(parents=True)
            registry_path.write_text(
                json.dumps(
                    {
                        "tools": [
                            {
                                "name": "broken-tool",
                                "command": "uv run broken-tool",
                                "use_when": ["broken registry check"],
                                "outputs": ["report.md"],
                            }
                        ]
                    }
                )
                + "\n",
                encoding="utf-8",
            )

            with self.assertRaisesRegex(ValueError, "missing non-empty safety"):
                SkillAdapterGenerator(root, tools=("codex",)).run()

    def test_tool_registry_rejects_empty_use_when_and_outputs(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            _write_skill(root)
            registry_path = root / ".agents" / "tools.json"
            registry_path.parent.mkdir(parents=True)
            registry_path.write_text(
                json.dumps(
                    {
                        "tools": [
                            {
                                "name": "broken-tool",
                                "command": "uv run broken-tool",
                                "use_when": [],
                                "outputs": ["report.md"],
                                "safety": "local_safe",
                            }
                        ]
                    }
                )
                + "\n",
                encoding="utf-8",
            )

            with self.assertRaisesRegex(ValueError, "missing non-empty use_when"):
                SkillAdapterGenerator(root, tools=("codex",)).run()


def _write_skill(root: Path) -> None:
    skill_dir = root / "skills" / "demo-skill"
    skill_dir.mkdir(parents=True)
    skill_dir.joinpath("SKILL.md").write_text(
        "---\n"
        "name: demo-skill\n"
        "description: Use when testing adapter contracts.\n"
        "---\n"
        "\n"
        "# Demo Skill\n"
        "\n"
        "Follow the demo procedure.\n",
        encoding="utf-8",
    )


if __name__ == "__main__":
    unittest.main()
