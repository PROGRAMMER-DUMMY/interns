"""Regression: the exact kpi_012 scenario from the 2026-08-03 review --
"Risk Tier (Low/Medium/High)" must never again produce a blocker for
"High" recommending departments.Name, after Tasks 1 and 2 land.
"""
from __future__ import annotations

import unittest

from core.onboarding.features.expression import extract_expression
from core.onboarding.kpi.blocker_question_panel import _physical_option_payload


class Kpi012ScenarioEndToEndTests(unittest.TestCase):
    def test_high_never_extracted_from_risk_tier_cuts(self):
        extracted = extract_expression(
            "weighted composite score (0-100), banded Low <33 / Medium 33-66 / High >66"
        )
        leaked = {t.lower() for t in extracted.identifiers} & {"high", "low", "medium", "weighted", "score", "banded"}
        self.assertFalse(leaked, f"formula-tier vocabulary leaked: {leaked}")

    def test_a_bare_generic_containment_score_never_renders_high_confidence(self):
        # Even if some future change lets a similarly weak token through,
        # the panel's OWN calibration (Task 2) must still refuse to call a
        # +20-only score "high".
        payload = _physical_option_payload(
            {"score": 20.0, "column": "Name", "dataset": "departments.csv"}, 1
        )
        self.assertNotEqual(payload["confidence"], "high")
