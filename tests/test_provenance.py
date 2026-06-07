"""Decision provenance: an agent identity in confirmed_by must not be recorded
as human (the integrity hole where `--confirmed-by agent` got source: human)."""
from __future__ import annotations

import unittest

from core.governance.provenance import decision_source, is_agent_confirmer


class ProvenanceTests(unittest.TestCase):
    def test_empty_is_agent(self) -> None:
        self.assertTrue(is_agent_confirmer(""))
        self.assertEqual(decision_source(""), "agent")

    def test_agent_identities_are_agent(self) -> None:
        for value in ["agent", "Agent", "AGENT", "claude", "gemini", "codex",
                      "cli-agent", "  agent ", "automation", "system", "bot"]:
            self.assertTrue(is_agent_confirmer(value), value)
            self.assertEqual(decision_source(value), "agent", value)

    def test_human_names_are_human(self) -> None:
        for value in ["Dr. Smith", "alice", "Shubham", "j.doe@example.com",
                      "Reviewer 7", "Maria Garcia"]:
            self.assertFalse(is_agent_confirmer(value), value)
            self.assertEqual(decision_source(value), "human", value)


if __name__ == "__main__":
    unittest.main()
