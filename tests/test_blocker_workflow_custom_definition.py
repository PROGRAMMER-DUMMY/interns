"""A --custom-definition answer's source_columns must never come back empty
for non-empty text.

Found live resolving Hostile_Synthetic's KPI blockers: a plain
"invoices.Amount" definition (not formula-shaped, no quotes) and a formula
whose author didn't quote identifiers ("party.party_key WHERE EXISTS ...
invoices.Status != 'VOID' ...") both produced zero source_columns. That
silently broke the downstream merge into kpi_feature_mapping.json, which
only overwrites a feature's stale pre-override candidate dataset when
source_columns is truthy -- state/resolution_type flipped to
user_confirmed, but the join-proof/source-to-target planner kept reasoning
off the original wrong dataset forever.
"""
from __future__ import annotations

import unittest

from core.onboarding.kpi.blocker_workflow import _custom_definition_source_columns
from core.onboarding.memory.workspace_definitions import apply_definition_to_feature


class CustomDefinitionSourceColumnsTests(unittest.TestCase):
    def test_plain_table_column_reference_is_not_formula_and_has_source_columns(self) -> None:
        looks_like_formula, columns = _custom_definition_source_columns("invoices.Amount")
        self.assertFalse(looks_like_formula)
        self.assertEqual(columns, ["invoices.Amount"])

    def test_unquoted_where_exists_formula_extracts_table_column_refs(self) -> None:
        looks_like_formula, columns = _custom_definition_source_columns(
            "party.party_key WHERE EXISTS a non-void invoice "
            "(invoices.Status != 'VOID') for that account in the period"
        )
        self.assertTrue(looks_like_formula)
        self.assertEqual(columns, ["party.party_key", "invoices.Status"])

    def test_quoted_formula_still_prefers_quoted_identifiers(self) -> None:
        looks_like_formula, columns = _custom_definition_source_columns(
            'CASE WHEN "Status" = \'A\' THEN 1 ELSE 0 END'
        )
        self.assertTrue(looks_like_formula)
        self.assertEqual(columns, ["Status"])

    def test_bare_single_word_definition_has_no_extractable_columns(self) -> None:
        looks_like_formula, columns = _custom_definition_source_columns("manual override")
        self.assertFalse(looks_like_formula)
        self.assertEqual(columns, [])


class MergeOverwritesStaleSourceColumnsTests(unittest.TestCase):
    def test_populated_source_columns_overwrites_stale_candidate(self) -> None:
        item = {
            "feature": "Amount",
            "state": "candidate_unconfirmed",
            "source_columns": [{"dataset": "accessorial_charges.csv", "column": "Amount"}],
        }
        definition = {
            "state": "user_confirmed",
            "resolution_type": "custom_business_definition",
            "source_columns": [{"dataset": "invoices.csv", "column": "Amount", "source": "workspace_definition"}],
            "definition": "invoices.Amount",
        }
        apply_definition_to_feature(item, definition, "kpi_002")
        self.assertEqual(item["source_columns"], definition["source_columns"])
        self.assertNotEqual(item["source_columns"][0]["dataset"], "accessorial_charges.csv")

    def test_empty_source_columns_leaves_stale_candidate_in_place(self) -> None:
        # Documents the actual failure mode this fix prevents: an empty
        # source_columns list on the definition is a no-op merge, silently
        # keeping the wrong dataset even though state flips to confirmed.
        item = {
            "feature": "Amount",
            "state": "candidate_unconfirmed",
            "source_columns": [{"dataset": "accessorial_charges.csv", "column": "Amount"}],
        }
        definition = {
            "state": "user_confirmed",
            "resolution_type": "custom_business_definition",
            "source_columns": [],
            "definition": "invoices.Amount",
        }
        apply_definition_to_feature(item, definition, "kpi_002")
        self.assertEqual(item["state"], "user_confirmed")
        self.assertEqual(item["source_columns"][0]["dataset"], "accessorial_charges.csv")


if __name__ == "__main__":
    unittest.main()
