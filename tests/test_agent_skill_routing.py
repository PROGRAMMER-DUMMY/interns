"""Roster coverage lock: every agent and every skill must be routed to at least
one workflow stage in delegation.STAGE_ROUTING. Fails if any is left idle —
prevents the "available but unused" drift the routing was built to fix.

Also locks the reverse direction (a routed name must be a real agent/skill) and
the stage list itself, so a newly built spine stage cannot ship unrouted — which
is exactly how the cloud-first stages (source declaration ... schema drift) went
live with no specialist attached.
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


# Every stage that owns a human-visible panel or a generated artifact must have a
# roster. Keyed to the CLI that drives it so a rename is caught here, not in a run.
REQUIRED_STAGES = {
    "source_declaration": "declare-source",
    "source_discovery": "discover-source",
    "intake_interview": "prepare-intake-panel",
    "blueprint_review": "prepare-blueprint",
    "velocity_lane_choice": "core/blueprint/tables/velocity.yaml",
    "provisioning": "plan-provisioning",
    "ingestion_generation": "generate-ingestion",
    "schema_drift_review": "prepare-drift-panel",
    "performance_optimization": "config/optimization_playbook.yaml",
}


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

    def test_routed_names_exist(self):
        # Reverse coverage: a typo'd or deleted agent/skill routes nowhere at runtime
        # and fails silently, so catch it here.
        unknown_agents = _routed_agents() - _present_agents()
        self.assertFalse(unknown_agents, f"routed agents that do not exist: {sorted(unknown_agents)}")
        unknown_skills = _routed_skills() - _present_skills()
        self.assertFalse(unknown_skills, f"routed skills that do not exist: {sorted(unknown_skills)}")

    def test_pipeline_spine_stages_are_routed(self):
        for stage, driver in REQUIRED_STAGES.items():
            with self.subTest(stage=stage):
                roster = routing_for(stage)
                self.assertTrue(
                    roster["agents"],
                    f"stage {stage} (driven by {driver}) has no specialist agent routed",
                )
                self.assertTrue(
                    roster["skills"],
                    f"stage {stage} (driven by {driver}) has no skill routed",
                )

    def test_performance_stage_routes_the_playbook_owner(self):
        # The playbook's 29 rules had zero production callers; the stage exists so a
        # measured symptom reaches the agent that owns the citations, and so the
        # engine specialist is consulted alongside it (engine change is last resort).
        roster = routing_for("performance_optimization")
        self.assertIn("performance-optimizer", roster["agents"])
        self.assertIn("sql-polars-pyspark-specialist", roster["agents"])
        self.assertTrue((REPO_ROOT / "config" / "optimization_playbook.yaml").exists())

    def test_routing_for_returns_agents_and_skills(self):
        r = routing_for("kpi_definition")
        self.assertIn("business-analyst", r["agents"])
        self.assertIn("grill-requirements", r["skills"])
        empty = routing_for("nonexistent_stage")
        self.assertEqual(empty, {"agents": [], "skills": []})


if __name__ == "__main__":
    unittest.main()
