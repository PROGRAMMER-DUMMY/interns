"""Regression: column_profile_summary must forward cardinality_ratio and
value_pattern (Task 3 fields) into the entry dicts _contextual_score reads
(Task 4) -- without this, both new scoring terms are permanently unreachable
on the real contextual_column_candidates path (plan gap found during Task 4
review, 2026-08-03)."""
from __future__ import annotations

import unittest

from core.onboarding.features.derived_evidence import column_profile_summary


class ColumnProfileSummaryNewSignalsTests(unittest.TestCase):
    def test_cardinality_ratio_and_value_pattern_are_forwarded(self):
        profile = {
            "columns": [
                {
                    "name": "ClaimID",
                    "cardinality_ratio": 0.99,
                    "value_pattern": "prefixed_numeric_code",
                }
            ]
        }
        summary = column_profile_summary(profile, "ClaimID")
        self.assertEqual(summary.get("cardinality_ratio"), 0.99)
        self.assertEqual(summary.get("value_pattern"), "prefixed_numeric_code")

    def test_missing_signals_default_to_none_not_a_missing_key(self):
        profile = {"columns": [{"name": "Name"}]}
        summary = column_profile_summary(profile, "Name")
        self.assertIsNone(summary.get("cardinality_ratio"))
        self.assertIsNone(summary.get("value_pattern"))
