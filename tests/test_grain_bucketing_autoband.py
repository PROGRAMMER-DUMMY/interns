"""Grain-bucketing auto-banding: a share KPI cut by a raw continuous value must
always reach the operator as a selectable, meaningful choice -- never a zero-option
deadlock, and never silently through with a fragmented denominator.

Two failure modes are covered, because they fail in opposite directions:

* Under-detection -- `\\bage\\b` never matched `Patient_Age` (`_` is a word char), so
  real column-name cuts skipped the blocker entirely and produced a share over
  one-row denominators. Silently wrong beats loudly stuck, and this was the silent one.
* Over-detection -- flagging `age_band` (already bucketed) or every `*_date` cut would
  block trend KPIs on a question they should never be asked.
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from core.onboarding.kpi.intent_contract import (  # noqa: E402
    _intent_facet_to_panel_question,
    _raw_continuous_cuts,
    build_intent_contract,
    low_confidence_facets,
)
from core.onboarding.kpi.result_view_builder import (  # noqa: E402
    _band_width_from_decision,
)

SHARE_KPI = {
    "kpi_id": "K1",
    "name": "Denial share by age",
    "metric": "percentage of claims denied",
}


def grain_question(cuts: str) -> dict | None:
    kpi = dict(SHARE_KPI, cuts=cuts)
    lows = [
        q
        for q in low_confidence_facets(build_intent_contract(kpi, {}))
        if q["facet"] == "grain_bucketing"
    ]
    if not lows:
        return None
    return _intent_facet_to_panel_question(
        SHARE_KPI["kpi_id"], SHARE_KPI["name"], lows[0], "workspaces/x"
    )


class ContinuousCutDetectionTest(unittest.TestCase):
    def test_column_name_forms_are_detected(self) -> None:
        # The original regex only matched the bare prose word.
        for cut in (
            "age",
            "Age",
            "Patient_Age",
            "patient_age",
            "PatientAge",
            "Patient_DOB",
            "dob",
            "date_of_birth",
            "birth_date",
            "days since admission",
        ):
            with self.subTest(cut=cut):
                self.assertTrue(_raw_continuous_cuts(cut), f"{cut} should be continuous")

    def test_already_banded_cuts_are_not_blocked(self) -> None:
        # Grouping by an age BAND does not fragment the denominator.
        for cut in ("age_band", "AgeGroup", "age_bucket", "age_range", "age_cohort"):
            with self.subTest(cut=cut):
                self.assertFalse(_raw_continuous_cuts(cut))

    def test_substring_lookalikes_do_not_false_positive(self) -> None:
        for cut in ("percentage", "average_amount", "coverage_amount", "usage", "manager"):
            with self.subTest(cut=cut):
                self.assertFalse(_raw_continuous_cuts(cut))

    def test_plain_date_cuts_are_not_treated_as_continuous(self) -> None:
        # Dates are the normal grain of every trend KPI; blocking them here would
        # ask an unanswerable banding question on ordinary time series.
        for cut in ("admission_date", "service_date", "claim_status", "month"):
            with self.subTest(cut=cut):
                self.assertFalse(_raw_continuous_cuts(cut))

    def test_comparison_tokens_remain_filters_not_cuts(self) -> None:
        self.assertFalse(_raw_continuous_cuts("age > 50"))


class AutoBandingPanelTest(unittest.TestCase):
    def test_panel_is_never_a_zero_option_deadlock(self) -> None:
        question = grain_question("Patient_Age")
        self.assertIsNotNone(question, "share KPI cut by Patient_Age must raise the facet")
        self.assertGreaterEqual(len(question["options"]), 2)

    def test_recommended_option_is_banding(self) -> None:
        question = grain_question("Patient_Age")
        options = {o["option_id"]: o for o in question["options"]}
        self.assertEqual(question.get("recommended_option_id"), "option_a")
        self.assertTrue(options["option_a"]["label"].startswith("band_continuous_cuts"))

    def test_options_carry_operator_meaningful_descriptions(self) -> None:
        question = grain_question("Patient_Age")
        options = {o["option_id"]: o for o in question["options"]}
        band_desc = options["option_a"]["description"].lower()
        exact_desc = options["option_b"]["description"].lower()
        # Not the old boilerplate.
        self.assertNotIn("current default / derived value", band_desc)
        self.assertNotIn("alternative interpretation", exact_desc)
        self.assertIn("band", band_desc)
        self.assertIn("denominator", exact_desc)

    def test_exact_value_option_is_offered_and_warns(self) -> None:
        question = grain_question("Patient_Age")
        options = {o["option_id"]: o for o in question["options"]}
        self.assertEqual(options["option_b"]["label"], "exact_value_grain")
        self.assertIn("warning", options["option_b"]["description"].lower())

    def test_option_labels_round_trip_to_real_band_widths(self) -> None:
        # The label IS the recorded decision value, so it must parse downstream.
        question = grain_question("Patient_Age")
        options = {o["option_id"]: o for o in question["options"]}
        self.assertEqual(_band_width_from_decision(options["option_a"]["label"]), 10)
        self.assertIsNone(_band_width_from_decision(options["option_b"]["label"]))

    def test_every_option_is_appliable(self) -> None:
        question = grain_question("Patient_Age")
        for option in question["options"]:
            with self.subTest(option=option["option_id"]):
                self.assertIn("option_id", option)
                self.assertTrue(option.get("label"))
                self.assertTrue(option.get("business_summary"))
                self.assertIn("intent_facet", option)

    def test_non_share_metric_does_not_raise_the_facet(self) -> None:
        kpi = {"kpi_id": "K2", "name": "Claims by age",
               "metric": "count of claims", "cuts": "Patient_Age"}
        lows = [
            q
            for q in low_confidence_facets(build_intent_contract(kpi, {}))
            if q["facet"] == "grain_bucketing"
        ]
        self.assertEqual(lows, [])

    def test_recorded_decision_stops_asking(self) -> None:
        kpi = dict(SHARE_KPI, cuts="Patient_Age")
        decisions = {"grain_bucketing_decisions": {"K1": "band_continuous_cuts:20"}}
        lows = [
            q
            for q in low_confidence_facets(build_intent_contract(kpi, decisions))
            if q["facet"] == "grain_bucketing"
        ]
        self.assertEqual(lows, [], "an answered facet must not be asked again")


if __name__ == "__main__":
    unittest.main()
