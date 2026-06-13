"""Op-signal contract + activation routing + dead-end panel routing.

Proves the closed loop: a command result is read for normalized signals, signals
map to the right skill/specialist, and a blocked-but-empty blocker panel carries
routing instead of going silent. Generic fixtures, no domain vocabulary.
"""
from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from core.governance.op_signals import derive_op_signals, signals_to_skills


class DeriveOpSignalsTests(unittest.TestCase):
    def test_blocked_with_zero_questions_is_empty_panel_and_stuck(self) -> None:
        sig = derive_op_signals(
            "prepare-kpi-blocker-panel",
            {"blocked_kpi_count": 5, "unresolved_feature_count": 0, "question_count": 0},
        )
        self.assertTrue(sig.blocked)
        self.assertTrue(sig.empty_panel)
        self.assertTrue(sig.stuck)
        self.assertTrue(sig.actionable)

    def test_low_confidence_facet_is_detected(self) -> None:
        sig = derive_op_signals(
            "resolve-kpi-features",
            {"metric": {"value": "count(*)", "confidence": 0.55}},
        )
        self.assertIsNotNone(sig.confidence)
        self.assertLess(sig.confidence, 0.6)
        self.assertTrue(sig.actionable)

    def test_clean_result_is_not_actionable(self) -> None:
        sig = derive_op_signals(
            "resolve-kpi-features",
            {"blocked_kpi_count": 0, "unresolved_feature_count": 0, "ready_kpi_count": 8},
        )
        self.assertFalse(sig.actionable)

    def test_unresolved_features_flag(self) -> None:
        sig = derive_op_signals("resolve-kpi-features", {"unresolved_feature_count": 3})
        self.assertEqual(sig.unresolved_count, 3)
        self.assertTrue(sig.actionable)

    def test_non_dict_payload_is_safe(self) -> None:
        self.assertFalse(derive_op_signals("x", None).actionable)  # type: ignore[arg-type]


class SignalsToSkillsTests(unittest.TestCase):
    def test_stuck_routes_to_kpi_analyst_and_self_grill(self) -> None:
        sig = derive_op_signals("prepare-kpi-blocker-panel",
                                {"blocked_kpi_count": 2, "question_count": 0})
        skills = {s["skill"] for s in signals_to_skills(sig)}
        self.assertIn("kpi-analyst", skills)
        # `grill-requirements` is the registered skill (self-grill is a *mode* of
        # it, not a skill); routing must reference the real skill name.
        self.assertIn("grill-requirements", skills)

    def test_unresolved_routes_to_feature_derivation_library(self) -> None:
        sig = derive_op_signals("resolve-kpi-features", {"unresolved_feature_count": 1})
        skills = {s["skill"] for s in signals_to_skills(sig)}
        self.assertIn("feature-derivation-library", skills)

    def test_clean_result_suggests_nothing(self) -> None:
        sig = derive_op_signals("resolve-kpi-features", {"ready_kpi_count": 5})
        self.assertEqual(signals_to_skills(sig), [])


class DeadEndPanelRoutingTests(unittest.TestCase):
    def test_empty_panel_with_blocked_kpis_carries_routing(self) -> None:
        from core.onboarding.kpi.blocker_question_panel import _empty_panel

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            ws = root / "workspaces" / "demo"
            ws.mkdir(parents=True)
            mapping = {"summary": {"blocked_kpi_count": 4, "kpi_count": 10}}
            panel = _empty_panel(mapping, ws, root)
            self.assertEqual(panel["status"], "blocked_without_question")
            self.assertTrue(panel.get("required_specialists"))
            self.assertTrue(panel.get("suggested_skills"))

    def test_empty_panel_with_no_blocked_kpis_stays_quiet(self) -> None:
        from core.onboarding.kpi.blocker_question_panel import _empty_panel

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            ws = root / "workspaces" / "demo"
            ws.mkdir(parents=True)
            panel = _empty_panel({"summary": {"blocked_kpi_count": 0}}, ws, root)
            self.assertEqual(panel["status"], "no_blocker_questions")
            self.assertNotIn("required_specialists", panel)


if __name__ == "__main__":
    unittest.main()
