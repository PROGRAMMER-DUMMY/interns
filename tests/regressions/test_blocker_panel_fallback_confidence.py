"""Regression: the panel's fallback profile scan must not label a weak,
generic match "confidence: high" or mark it recommended.

Origin (2026-08-03 review): _profile_candidate_options (the fallback that
fires when the resolver found nothing) scores candidates 100/60/30/20/-30,
but _physical_option_payload labeled confidence "high" at score>=6 -- a
single generic "text appears in the KPI's full text" hit (+20, or +40 when
both the column name and the dataset name each separately appear somewhere
in the KPI's text) cleared that bar by 3-6x. Reproduced live: kpi_012's cut
"Risk Tier (Low/Medium/High)" sits beside "Department Name" in the same KPI;
the garbage token "High" scored purely from generic containment against
departments.Name -- nothing about "High" itself relates to a "Name" column
-- and was rendered RECOMMENDED at confidence: high.

Note: the live score for the fixture below is 40 (two generic containment
hits: "name" appears in the KPI text, and "department" -- the dataset's
name -- also appears in the KPI text), not a single +20 hit as the
originating bug report described in shorthand. Either way it is well under
the genuine-match bar (100 exact / 60 partial) and must not be labeled
"high".
"""
from __future__ import annotations

import unittest

from core.onboarding.kpi.blocker_question_panel import (
    _physical_option_payload,
    _profile_candidate_score,
)


class FallbackScorerCalibrationTests(unittest.TestCase):
    def test_generic_text_containment_alone_is_not_high_confidence(self):
        # "High" has no name/dataset relationship to departments.Name; every
        # point it scores comes only from "name" and "department" each
        # separately appearing elsewhere in a shared KPI's text (kpi_text
        # containment), never from anything about "High" itself.
        items = [{"kpi": {
            "name": "Which self-pay balances are collectible",
            "description": "",
            "cuts": "Department Name, Risk Tier (Low/Medium/High)",
        }}]
        score, _reason = _profile_candidate_score("High", "departments.csv", "Name", items)
        self.assertLess(score, 60, f"generic containment score {score} reached the genuine-match bar")
        payload = _physical_option_payload({"score": score, "column": "Name", "dataset": "departments.csv"}, 1)
        self.assertNotEqual(
            payload["confidence"], "high",
            f"score={score} produced confidence=high; generic containment hits must cap below high",
        )

    def test_partial_name_match_can_still_reach_high(self):
        # A genuine partial name match (+60) must still be able to reach
        # high confidence -- this is a calibration fix, not a ban on "high".
        items = [{"kpi": {"name": "", "description": "", "cuts": ""}}]
        score, _reason = _profile_candidate_score("totalchargeamount", "claims.csv", "ChargeAmount", items)
        self.assertGreaterEqual(score, 60)
        payload = _physical_option_payload({"score": score, "column": "ChargeAmount", "dataset": "claims.csv"}, 1)
        self.assertEqual(payload["confidence"], "high")


if __name__ == "__main__":
    unittest.main()
