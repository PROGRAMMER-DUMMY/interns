"""Regression: a blocker panel must not recommend a relation we wrote ourselves.

Origin (2026-07-27, observed on a real workspace): the `Gender` blocker offered

    1. [RECOMMENDED] option_a - kpi_002_results.gender  (confidence: low)
    2.               option_b - patients.Gender          (confidence: low)

i.e. define kpi_002's own `Gender` cut from kpi_002's own OUTPUT, at the
exploded one-row-per-patient grain its own defect had produced. Both options
scored identically, so the ranking fell through to the ALPHABETICAL tiebreak on
dataset name, and `...kpi_002_results` sorts before `...patients`.

Two independent fixes, because either alone leaves a hole:
  * profiling no longer surfaces platform-written relations at all
    (`_is_platform_written_relation`, fixed for UC identifiers separately), and
  * ranking now sorts them last at ANY score -- a profile index written before
    that fix still carries them, and a ranker that CAN prefer a derived
    relation is wrong on its own terms.

This test covers the second.
"""
from __future__ import annotations

import unittest

from core.onboarding.kpi.blocker_question_panel import _physical_column_options


def _item(dataset: str, column: str, score: float) -> dict:
    return {
        "kpi": {"kpi_id": "kpi_002", "name": "share of lives by gender",
                "metric": "percentage of count(distinct PatientID)"},
        "feature": {
            "state": "blocked_ambiguous",
            "resolution_type": "ambiguous_direct_columns",
            "source_columns": [{"dataset": dataset, "column": column, "score": score}],
        },
    }


class PanelRankingTests(unittest.TestCase):
    def test_a_tied_result_view_never_outranks_a_base_table(self):
        # The exact shape observed: equal scores, and the platform's own output
        # alphabetically first.
        options = _physical_column_options([
            _item("`c`.`s`.`kpi_002_results`", "gender", 10.0),
            _item("`c`.`s`.`patients`", "Gender", 10.0),
        ])
        self.assertEqual(options[0]["column"], "Gender")
        self.assertIn("patients", options[0]["dataset"])

    def test_it_holds_even_when_the_platform_relation_scores_HIGHER(self):
        # A KPI's own output describes its own grain, so it will often score
        # well on the KPI's vocabulary. Score must not rescue it.
        options = _physical_column_options([
            _item("`c`.`s`.`kpi_002_results`", "gender", 99.0),
            _item("`c`.`s`.`patients`", "Gender", 1.0),
        ])
        self.assertIn("patients", options[0]["dataset"])

    def test_staging_and_mart_models_are_demoted_too(self):
        for derived in ("`c`.`s`.`stg_patients`", "`c`.`s`.`int_kpi_002_features`",
                        "`c`.`s`.`fct_kpi_002`"):
            with self.subTest(dataset=derived):
                options = _physical_column_options([
                    _item(derived, "Gender", 50.0),
                    _item("`c`.`s`.`patients`", "Gender", 1.0),
                ])
                self.assertIn("patients", options[0]["dataset"])

    def test_score_still_orders_two_genuine_sources(self):
        # The demotion must not flatten normal ranking between real tables.
        options = _physical_column_options([
            _item("`c`.`s`.`patients`", "Gender", 5.0),
            _item("`c`.`s`.`encounters`", "GenderCode", 40.0),
        ])
        self.assertEqual(options[0]["column"], "GenderCode")

    def test_a_source_table_named_like_a_result_is_not_demoted(self):
        # `lab_results` is a real clinical table, not our output.
        options = _physical_column_options([
            _item("`c`.`s`.`lab_results`", "Gender", 40.0),
            _item("`c`.`s`.`patients`", "Gender", 5.0),
        ])
        self.assertIn("lab_results", options[0]["dataset"])


if __name__ == "__main__":
    unittest.main()
