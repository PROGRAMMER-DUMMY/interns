from __future__ import annotations

import unittest

from core.onboarding.derived_feature_evidence import (
    derived_feature_options,
    fill_formula_template,
    substitute_formula_values,
    value_profile,
)


class DerivedFeatureEvidenceTests(unittest.TestCase):
    def test_derived_feature_options_include_strict_evidence_payload(self):
        schema_index = {
            "dob": [
                {
                    "dataset": "transactions.csv",
                    "column": "DOB",
                    "dtype": "Date",
                    "profile_path": "profiles/transactions.profile.json",
                    "row_count": 2,
                    "sample_values": ["1980-01-01"],
                }
            ],
            "servicedate": [
                {
                    "dataset": "transactions.csv",
                    "column": "ServiceDate",
                    "dtype": "Date",
                    "profile_path": "profiles/transactions.profile.json",
                    "row_count": 2,
                    "sample_values": ["2024-01-01"],
                }
            ],
        }
        pattern = {
            "pattern_id": "age_years",
            "description": "Age in years from birth date to anchor date.",
            "required_inputs": ["birth_date", "anchor_date"],
            "candidate_bindings": {
                "birth_date": ["DOB"],
                "anchor_date": ["ServiceDate"],
            },
            "templates": {
                "duckdb_sql": "date_diff('year', {birth_date}, {anchor_date})",
            },
            "requires": ["anchor_date_confirmation"],
        }

        options = derived_feature_options(
            "Age",
            [pattern],
            schema_index,
            {"source": "docs/kpi_registry.csv"},
            "avg(Age)",
        )

        self.assertEqual(len(options), 1)
        option = options[0]
        self.assertEqual(option["derived_column_name"], "Age")
        self.assertEqual(option["source_pattern_id"], "age_years")
        self.assertEqual(option["confidence"], "medium")
        self.assertEqual(option["missing_inputs"], [])
        self.assertEqual(option["example"]["output"], {"Age": 44})
        self.assertTrue(option["needs_user_confirmation"])
        self.assertEqual(option["evidence_state"], "candidate_derivation_not_ground_truth")
        self.assertTrue(any(src["evidence_type"] == "profile_schema" for src in option["evidence_sources"]))
        self.assertEqual(
            {column["input_name"] for column in option["input_columns"]},
            {"birth_date", "anchor_date"},
        )

    def test_formula_helpers_preserve_existing_rendering(self):
        formula = fill_formula_template("{paid_amount} - {refund_amount}", {
            "paid_amount": "PaidAmount",
            "refund_amount": "RefundAmount",
        })

        self.assertEqual(formula, "PaidAmount - RefundAmount")
        self.assertEqual(
            substitute_formula_values(
                formula,
                {"paid_amount": "PaidAmount", "refund_amount": "RefundAmount"},
                {"paid_amount": 100.0, "refund_amount": 10.0},
            ),
            "100.0 - 10.0",
        )

    def test_value_profile_labels_bounded_profile_evidence(self):
        profile = value_profile({"sample_values": ["A"], "null_count": 0})

        self.assertEqual(profile["sample_values"], ["A"])
        self.assertEqual(profile["null_count"], 0)
        self.assertIn("bounded profile evidence", profile["note"])


if __name__ == "__main__":
    unittest.main()
