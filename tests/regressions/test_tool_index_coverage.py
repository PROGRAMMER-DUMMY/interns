"""Regression: the agent tool index must cover every registered CLI, and stay fresh.

Origin (2026-07-26 audit): `.agents/tools.json` was hand-curated and covered 56 of
115 registered CLIs. `run-kpi-pipeline` -- the deterministic chain AGENTS.md tells
agents to prefer -- and `check-platform-readiness` were both absent, so an agent
that queried the index found nothing and fell back to reading source. A first-time
"can I use my S3 data" question cost 20 tool calls.

Same failure class as the green gate's curated module list, which had silently
dropped the entire tests/regressions/ tree.
"""
from __future__ import annotations

import json
import unittest

from core.dev.tool_index import (
    INDEX_PATH,
    RAW_DATASET_RULE,
    REPO_ROOT,
    SECRET_DISPLAY_RULE,
    build_index,
    registered_clis,
)


class ToolIndexCoverageTests(unittest.TestCase):
    def setUp(self):
        self.index = build_index()
        self.indexed = {entry["name"] for entry in self.index["tools"]}

    def test_every_registered_cli_is_indexed(self):
        missing = sorted(set(registered_clis()) - self.indexed)
        self.assertEqual(missing, [], f"CLIs registered but not indexed: {missing}")

    def test_the_commands_agents_are_told_to_prefer_are_indexed(self):
        for cli in ("run-kpi-pipeline", "check-platform-readiness",
                    "onboard-workspace", "workspace-dashboard"):
            with self.subTest(cli=cli):
                self.assertIn(cli, self.indexed)

    def test_no_entry_survives_for_a_cli_that_no_longer_exists(self):
        real = set(registered_clis())
        stale = sorted(name for name in self.indexed if name not in real)
        self.assertEqual(stale, [], f"index names with no registered CLI: {stale}")

    def test_every_entry_has_a_runnable_command_string(self):
        for entry in self.index["tools"]:
            with self.subTest(cli=entry["name"]):
                self.assertTrue(entry["command"].startswith("uv run "))
                self.assertIn(entry["name"], entry["command"])

    def test_workspace_scoped_clis_advertise_the_flag(self):
        by_name = {e["name"]: e for e in self.index["tools"]}
        self.assertIn("--workspace", by_name["onboard-workspace"]["command"])


class ToolIndexFreshnessTests(unittest.TestCase):
    def test_checked_in_index_matches_a_fresh_generation(self):
        self.assertTrue(
            INDEX_PATH.exists(),
            f"{INDEX_PATH} is missing; run: uv run build-tool-index",
        )
        on_disk = json.loads(INDEX_PATH.read_text(encoding="utf-8"))
        self.assertEqual(
            on_disk, build_index(),
            "the checked-in tool index is stale; regenerate with: uv run build-tool-index",
        )


class EvidencePolicySafetyRuleTests(unittest.TestCase):
    """Regression: `.agents/tools.json` v2 replaced the v1 evidence_policy dict with
    one prose line. `adapter_generator._render_tool_registry` renders
    `secret_display_rule` / `raw_dataset_rule` only from the dict form, so the two
    guardrails silently vanished from every generated `.agents/<tool>/SKILLS.md` --
    i.e. from every non-Claude CLI's skill index."""

    def test_evidence_policy_declares_both_rules_as_explicit_fields(self):
        policy = build_index()["evidence_policy"]
        self.assertIsInstance(policy, dict, "prose-only policy renders neither rule")
        self.assertEqual(policy["secret_display_rule"], SECRET_DISPLAY_RULE)
        self.assertEqual(policy["raw_dataset_rule"], RAW_DATASET_RULE)
        secret = SECRET_DISPLAY_RULE.lower()
        for token in (".env", ".databrickscfg", "token", "cookies", "redacted"):
            self.assertIn(token, secret)
        raw = RAW_DATASET_RULE.lower()
        for token in ("profile_index.json", "never paste raw dataset", "state why"):
            self.assertIn(token, raw)

    def test_every_generated_adapter_carries_both_rules(self):
        adapters = sorted((REPO_ROOT / ".agents").glob("*/SKILLS.md"))
        self.assertTrue(adapters, "no generated adapters; run: uv run generate-skill-adapters")
        for adapter in adapters:
            with self.subTest(adapter=adapter.name and adapter.parent.name):
                text = adapter.read_text(encoding="utf-8")
                self.assertIn(f"Secret display safety: {SECRET_DISPLAY_RULE}", text)
                self.assertIn(f"Dataset access safety: {RAW_DATASET_RULE}", text)


if __name__ == "__main__":
    unittest.main()
