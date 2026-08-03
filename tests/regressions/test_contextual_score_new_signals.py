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
