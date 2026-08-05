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
                                "required_skills": ["demo-skill"],
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
            self.assertEqual(index["tool_registry"]["tools"][0]["required_skills"], ["demo-skill"])
            self.assertEqual(result.route_manifest_path, ".agents/skill_route_manifest.json")
            route_manifest = json.loads((root / result.route_manifest_path).read_text(encoding="utf-8"))
            self.assertEqual(route_manifest["routes"][0]["required_skills"], ["demo-skill"])
            adapter = (root / ".agents" / "codex" / "SKILLS.md").read_text(encoding="utf-8")
            self.assertIn("## Project Tool Registry", adapter)
            self.assertIn("Secret display safety: Report only redacted secret status.", adapter)
            self.assertIn("Dataset access safety: Prefer generated profiles before raw datasets.", adapter)
            self.assertIn("- `demo-tool`", adapter)
            self.assertIn("Safety: local_safe", adapter)
            self.assertIn("Required skills: demo-skill", adapter)
            self.assertIn("Recovery: If it fails, inspect the generated report before retrying.", adapter)

    def test_tool_registry_rejects_present_but_empty_safety(self):
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
                                "safety": "",
                                "required_skills": ["demo-skill"],
                            }
                        ]
                    }
                )
                + "\n",
                encoding="utf-8",
            )

            with self.assertRaisesRegex(ValueError, "invalid safety"):
                SkillAdapterGenerator(root, tools=("codex",)).run()

    def test_tool_registry_accepts_uncurated_v2_entry(self):
        # `.agents/tools.json` v2 is generated from pyproject scripts: name/command/
        # entry_point/summary only, with use_when/outputs/safety/required_skills as an
        # optional human overlay. An uncurated entry must not break generation.
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            _write_skill(root)
            registry_path = root / ".agents" / "tools.json"
            registry_path.parent.mkdir(parents=True)
            registry_path.write_text(
                json.dumps(
                    {
                        "version": 2,
                        "evidence_policy": "Derived, not curated. Do not hand-edit.",
                        "tools": [
                            {
                                "name": "uncurated-tool",
                                "command": "uv run uncurated-tool",
                                "entry_point": "core.demo:main",
                                "summary": "Does the demo thing.",
                            }
                        ],
                    }
                )
                + "\n",
                encoding="utf-8",
            )

            SkillAdapterGenerator(root, tools=("codex",)).run()
            adapter = (root / ".agents" / "codex" / "SKILLS.md").read_text(encoding="utf-8")
            self.assertIn("Use when: Does the demo thing.", adapter)
            self.assertIn("Evidence policy: Derived, not curated.", adapter)

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
                                "required_skills": ["demo-skill"],
                            }
                        ]
                    }
                )
                + "\n",
                encoding="utf-8",
            )

            with self.assertRaisesRegex(ValueError, "missing non-empty use_when"):
                SkillAdapterGenerator(root, tools=("codex",)).run()

    def test_tool_registry_rejects_unknown_required_skills(self):
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
                                "safety": "local_safe",
                                "required_skills": ["missing-skill"],
                            }
                        ]
                    }
                )
                + "\n",
                encoding="utf-8",
            )

            with self.assertRaisesRegex(ValueError, "unknown required skill"):
                SkillAdapterGenerator(root, tools=("codex",)).run()

    def test_subagent_sidecars_are_indexed_and_rendered(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            _write_skill(root)
            agents_dir = root / "skills" / "demo-skill" / "agents"
            agents_dir.mkdir()
            agents_dir.joinpath("enterprise.yaml").write_text(
                "version: 1\n"
                "subagents:\n"
                "  - name: demo-reviewer\n"
                "    display_name: Demo Reviewer\n"
                "    description: Reviews demo workflow artifacts.\n"
                "    skills:\n"
                "      - demo-skill\n"
                "    default_prompt: Load demo-skill and review generated evidence.\n"
                "    safety: read_only_review\n"
                "    model_policy:\n"
                "      default_tier: light\n"
                "      escalate_to_standard_for:\n"
                "        - conflicting evidence\n"
                "    targets:\n"
                "      codex:\n"
                "        enabled: true\n"
                "        sandbox_mode: read-only\n"
                "      claude:\n"
                "        enabled: true\n"
                "        permission: read-only\n"
                "      gemini:\n"
                "        enabled: true\n",
                encoding="utf-8",
            )

            result = SkillAdapterGenerator(root, tools=("codex",)).run()

            self.assertEqual(result.subagent_count, 1)
            self.assertEqual(result.subagents_index_path, ".agents/subagents_index.json")
            index = json.loads((root / ".agents" / "subagents_index.json").read_text())
            self.assertEqual(index["subagents"][0]["name"], "demo-reviewer")
            self.assertEqual(index["subagents"][0]["model_policy"]["default_tier"], "light")
            skill_index = json.loads((root / result.index_path).read_text())
            self.assertEqual(skill_index["subagents"][0]["skills"], ["demo-skill"])
            adapter = (root / ".agents" / "codex" / "SKILLS.md").read_text(encoding="utf-8")
            self.assertIn("## Available Subagents", adapter)
            self.assertIn("### demo-reviewer", adapter)
            self.assertIn("Target sandbox/permission: `read-only`", adapter)
            self.assertIn('"default_tier": "light"', adapter)
            self.assertIn(".claude/agents/demo-reviewer.md", result.native_agent_paths)
            self.assertIn(".gemini/agents/demo-reviewer.md", result.native_agent_paths)
            self.assertIn(".codex/agents/demo-reviewer.toml", result.native_agent_paths)
            claude_agent = (root / ".claude" / "agents" / "demo-reviewer.md").read_text(
                encoding="utf-8"
            )
            gemini_agent = (root / ".gemini" / "agents" / "demo-reviewer.md").read_text(
                encoding="utf-8"
            )
            codex_agent = (root / ".codex" / "agents" / "demo-reviewer.toml").read_text(
                encoding="utf-8"
            )
            self.assertIn("tools: Read, Glob, Grep", claude_agent)
            self.assertIn("kind: local", gemini_agent)
            self.assertIn("tools:", gemini_agent)
            self.assertIn('name = "demo_reviewer"', codex_agent)
            self.assertIn('nickname_candidates = ["Demo Reviewer", "demo-reviewer", "demo_reviewer"]', codex_agent)
            self.assertIn('sandbox_mode = "read-only"', codex_agent)
            self.assertIn('developer_instructions = """', codex_agent)
            self.assertIn("## Model Policy", codex_agent)
            self.assertNotIn("[metadata]", codex_agent)
            self.assertIn("# Skills: demo-skill", codex_agent)

    def test_subagent_sidecars_reject_duplicate_names(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            _write_skill(root)
            agents_dir = root / "skills" / "demo-skill" / "agents"
            agents_dir.mkdir()
            for filename in ("one.yaml", "two.yaml"):
                agents_dir.joinpath(filename).write_text(
                    "version: 1\n"
                    "subagents:\n"
                    "  - name: duplicate-reviewer\n"
                    "    description: Reviews demo workflow artifacts.\n"
                    "    skills: [demo-skill]\n"
                    "    default_prompt: Load demo-skill and review generated evidence.\n"
                    "    safety: read_only_review\n",
                    encoding="utf-8",
                )

            with self.assertRaisesRegex(ValueError, "duplicate subagent names"):
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
