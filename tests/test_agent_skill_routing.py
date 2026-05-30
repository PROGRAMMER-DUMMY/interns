"""Roster coverage lock: every agent and every skill must be routed to at least
one workflow stage in delegation.STAGE_ROUTING. Fails if any is left idle —
prevents the "available but unused" drift the routing was built to fix.
"""
import unittest
from pathlib import Path

from core.onboarding.workspace.delegation import STAGE_ROUTING, routing_for

REPO_ROOT = Path(__file__).resolve().parents[1]


def _present_agents() -> set[str]:
    base = REPO_ROOT / ".claude" / "agents"
    return {p.stem for p in base.iterdir() if p.is_file()} if base.exists() else set()


def _present_skills() -> set[str]:
    base = REPO_ROOT / "skills"
    return {p.name for p in base.iterdir() if p.is_dir()} if base.exists() else set()


def _routed_agents() -> set[str]:
    return {a for entry in STAGE_ROUTING.values() for a in entry.get("agents", [])}


def _routed_skills() -> set[str]:
    return {s for entry in STAGE_ROUTING.values() for s in entry.get("skills", [])}


class RosterCoverageTests(unittest.TestCase):
    def test_every_agent_is_routed(self):
        present = _present_agents()
        self.assertTrue(present, "no agents discovered under .claude/agents/")
        idle = present - _routed_agents()
        self.assertFalse(idle, f"agents present but not routed to any stage: {sorted(idle)}")

    def test_every_skill_is_routed(self):
        present = _present_skills()
        self.assertTrue(present, "no skills discovered under skills/")
        idle = present - _routed_skills()
        self.assertFalse(idle, f"skills present but not routed to any stage: {sorted(idle)}")

    def test_routing_for_returns_agents_and_skills(self):
        r = routing_for("kpi_definition")
        self.assertIn("business-analyst", r["agents"])
        self.assertIn("grill-requirements", r["skills"])
        empty = routing_for("nonexistent_stage")
        self.assertEqual(empty, {"agents": [], "skills": []})


if __name__ == "__main__":
    unittest.main()
