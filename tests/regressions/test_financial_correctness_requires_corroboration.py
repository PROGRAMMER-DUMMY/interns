"""Regression: a financial_correctness-risk feature must never auto-prove
on score/margin alone -- it needs corroboration beyond a bare threshold
pass, because a silently wrong money mapping is the highest-stakes failure
mode (blockers.risk_score ranks financial_correctness highest).

This is the surgical, evidence-driven form of "never silently substitute a
correlated proxy for a true source" (contracted rate vs. avg paid/charge is
the motivating case) -- applied exactly where the existing risk taxonomy
already says the stakes are highest, not a new derivability subsystem.

Fixture note: "margin" is used because it is both (a) a literal entry in
blockers.GENERIC_FINANCIAL_TERMS (so risk_class resolves to
financial_correctness), and (b) a feature whose table-name-alignment bonus
(+30 in _contextual_score, dataset "margins.csv" vs feature "margin",
after pluralization stripping) reliably clears the score>=14/margin>=4
auto-proven bar on its own, with only one candidate in schema_index -- so
the ONLY variable between the two tests is presence/absence of a
dictionary_description, isolating exactly what this fix changes.

full_context is passed as "margin" (not "") because contextual_column_candidates
returns [] immediately whenever _semantic_tokens(full_context) is empty (a
pre-existing, unrelated guard -- see the early `if not context_tokens: return []`
gate). Passing the bare feature name as the KPI context is the minimal non-empty
context that still isolates the same one variable: it contributes nothing beyond
the table-alignment/dataset-overlap bonuses already described above, and is
identical across both tests below.
"""
from __future__ import annotations

import unittest

from core.onboarding.kpi.feature_resolver import contextual_column_candidates


class FinancialCorrectnessCorroborationTests(unittest.TestCase):
    def test_financial_feature_without_dictionary_corroboration_does_not_auto_prove(self):
        schema_index = {
            "value": [
                {"dataset": "margins.csv", "column": "Value", "dtype": "Float64"},
                # No dictionary_description -- score/margin alone must not be enough.
            ],
        }
        candidates = contextual_column_candidates("margin", "margin", schema_index)
        self.assertTrue(candidates, "expected the table-alignment bonus to surface a candidate")
        self.assertFalse(
            candidates[0].get("auto_proven"),
            "a financial_correctness feature auto-proved without dictionary corroboration",
        )

    def test_financial_feature_with_dictionary_corroboration_can_still_auto_prove(self):
        schema_index = {
            "value": [
                {
                    "dataset": "margins.csv", "column": "Value", "dtype": "Float64",
                    "dictionary_description": "The realized margin value for this transaction.",
                },
            ],
        }
        candidates = contextual_column_candidates("margin", "margin", schema_index)
        self.assertTrue(
            candidates and candidates[0].get("auto_proven"),
            "a table-aligned, dictionary-corroborated financial match should still auto-prove",
        )
