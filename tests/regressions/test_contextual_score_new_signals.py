"""Regression: cardinality_ratio and value_pattern (Task 3) must contribute to
_contextual_score, not sit unused in the profile evidence."""
from __future__ import annotations

import unittest

from core.onboarding.kpi.feature_resolver import _contextual_score


class ContextualScoreNewSignalsTests(unittest.TestCase):
    def test_near_unique_id_shaped_column_gets_identifier_bonus(self):
        entry = {
            "column": "ClaimID", "dataset": "claims", "dtype": "String",
            "cardinality_ratio": 0.99, "value_pattern": None,
        }
        with_cardinality_ratio, _, _ = _contextual_score("claimid", set(), "", entry)
        entry_no_signal = dict(entry, cardinality_ratio=None)
        without_cardinality_ratio, _, _ = _contextual_score("claimid", set(), "", entry_no_signal)
        self.assertGreater(with_cardinality_ratio, without_cardinality_ratio)

    def test_currency_pattern_boosts_a_financial_seed_feature(self):
        entry = {
            "column": "ChargeAmount", "dataset": "claims", "dtype": "Float64",
            "cardinality_ratio": None, "value_pattern": "currency_2dp",
        }
        with_pattern, reasons, _ = _contextual_score("charge", set(), "", entry)
        entry_no_pattern = dict(entry, value_pattern=None)
        without_pattern, _, _ = _contextual_score("charge", set(), "", entry_no_pattern)
        self.assertGreater(with_pattern, without_pattern)
        self.assertTrue(any("pattern" in reason for reason in reasons))

    def test_missing_signals_do_not_raise(self):
        entry = {"column": "Name", "dataset": "departments", "dtype": "String"}
        score, reasons, matched = _contextual_score("department", set(), "", entry)
        self.assertIsInstance(score, float)

    def test_bare_two_character_id_column_does_not_get_the_identifier_bonus(self):
        # Mirrors the sibling ID-penalty's own len(column_norm) > 2 exemption
        # (feature_resolver.py:1359) -- a bare "Id" column must not collect
        # the cardinality bonus either, or it wins unrelated features purely
        # on generic dataset-vocabulary overlap once cardinality_ratio is
        # reachable (2026-08-03 regression, found wiring Task 4b).
        entry = {
            "column": "Id", "dataset": "encounters", "dtype": "String",
            "cardinality_ratio": 1.0, "value_pattern": None,
        }
        score, reasons, _ = _contextual_score("encounterdurationbucket", {"encounters"}, "encounters", entry)
        self.assertLess(score, 8.0)
        self.assertFalse(any("identifier role" in reason for reason in reasons))

    def test_near_unique_code_column_needs_the_feature_name_in_it(self):
        # The `*code` half of the bonus had no equivalent of the `*id`
        # sibling penalty, so a near-unique business-key column (unique by
        # construction, near-universal in real schemas) won ANY unrelated
        # feature on generic dataset-vocabulary overlap alone. The bonus may
        # only corroborate an already name-plausible match.
        for column in ("Code", "DeptCode"):
            entry = {
                "column": column, "dataset": "encounters", "dtype": "String",
                "cardinality_ratio": 1.0, "value_pattern": None,
            }
            score, reasons, _ = _contextual_score(
                "encounterdurationbucket", {"encounters"}, "encounters", entry
            )
            self.assertLess(score, 8.0, column)
            self.assertFalse(any("identifier role" in reason for reason in reasons), column)

    def test_near_unique_code_column_still_corroborates_its_own_feature(self):
        entry = {
            "column": "ChargeCode", "dataset": "charges", "dtype": "String",
            "cardinality_ratio": 1.0, "value_pattern": None,
        }
        with_ratio, reasons, _ = _contextual_score("chargecode", set(), "", entry)
        without_ratio, _, _ = _contextual_score(
            "chargecode", set(), "", dict(entry, cardinality_ratio=None)
        )
        self.assertGreater(with_ratio, without_ratio)
        self.assertTrue(any("identifier role" in reason for reason in reasons))

    def test_all_null_column_is_not_a_categorical_dimension(self):
        # COUNT(DISTINCT) skips NULLs but row_count doesn't, so an all-null
        # column reports cardinality_ratio 0.0 and used to collect the
        # low-cardinality "categorical dimension" bonus. A column with no
        # data is not a dimension.
        entry = {
            "column": "Notes", "dataset": "encounters", "dtype": "String",
            "cardinality_ratio": 0.0, "value_pattern": None,
        }
        _, reasons, _ = _contextual_score(
            "encounterdurationbucket", {"encounters"}, "encounters", entry
        )
        self.assertFalse(any("categorical dimension" in reason for reason in reasons))

    def test_reasons_name_the_signal_as_cardinality_ratio(self):
        # The escalated cardinality -> cardinality_ratio rename must reach the
        # human-facing reason strings too; they land in panels and contracts.
        entry = {
            "column": "ChargeCode", "dataset": "charges", "dtype": "String",
            "cardinality_ratio": 1.0,
        }
        _, reasons, _ = _contextual_score("chargecode", set(), "", entry)
        low = {"column": "Status", "dataset": "claims", "dtype": "String",
               "cardinality_ratio": 0.01}
        _, low_reasons, _ = _contextual_score("status", set(), "", low)
        for reason in [r for r in reasons + low_reasons if "cardinality" in r]:
            self.assertIn("cardinality_ratio=", reason)
