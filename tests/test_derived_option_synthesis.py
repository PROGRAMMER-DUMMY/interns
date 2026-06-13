"""Derived-feature option synthesis (hostile-workspace refinement 1d).

A KPI whose prose describes a quantity stored in MIXED units (a numeric column
plus a sibling unit-of-measure column with more than one observed unit code)
should receive a JSON-backed ``derived_feature_options`` candidate that
normalizes the quantity to one unit -- instead of a bare "define this" blocker.
The detection is evidence-driven and generic: the hazard is recognized from the
data shape and the conversion uses universal physical constants, with no domain
vocabulary or per-workspace column list. When the evidence is genuinely absent
(a single observed unit, or no quantity/unit reference in the prose) the honest
blocker must remain -- no option is fabricated.
"""
from __future__ import annotations

import unittest

from core.onboarding.features.derivation_patterns import (
    detect_derivation_patterns,
    detect_uom_normalization,
)


def _col(
    column: str,
    dataset: str,
    dtype: str,
    sample_values: list,
) -> dict:
    return {
        "column": column,
        "dataset": dataset,
        "dtype": dtype,
        "row_count": 100,
        "sample_values": list(sample_values),
        "profile_path": f"interns/generated/profiles/{dataset}.profile.json",
    }


# A mixed-unit weight column (KG + LB) governed by a wgt_uom descriptor, plus an
# unrelated id column -- the canonical hostile shape, generalized.
_MIXED_WEIGHT = [
    _col("wgt", "ops/shipments.csv", "Float64", [2.9, 3.1, 3.8, 4.2, 5.3]),
    _col("wgt_uom", "ops/shipments.csv", "String", ["KG", "LB", "KG", "KG", "LB"]),
    _col("ship_ref", "ops/shipments.csv", "String", ["S1", "S2", "S3"]),
]

# Required contract fields a JSON-backed derived option must carry for the
# blocker panel / derived-markdown validator to render it.
_REQUIRED_OPTION_FIELDS = {
    "derived_column_name",
    "business_meaning",
    "formula",
    "input_columns",
    "example",
    "evidence_sources",
    "derivation_reasoning",
    "evidence_state",
    "confidence",
    "needs_user_confirmation",
}


class UomNormalizationDetectorTests(unittest.TestCase):
    def test_mixed_units_with_prose_unit_word_yields_option(self) -> None:
        option = detect_uom_normalization(
            "KPI 8 - Average cost per kilogram by lane", _MIXED_WEIGHT
        )
        self.assertIsNotNone(option)
        # Target unit is the one named in the prose ("kilogram" -> kg).
        self.assertEqual(option["derived_column_name"], "wgt_kg")
        formula = option["formula"]
        self.assertIn("upper(trim(\"wgt_uom\"))", formula)
        self.assertIn("WHEN 'KG' THEN \"wgt\"", formula)
        # LB -> KG uses the exact physical constant, not a rounded factor.
        self.assertIn("0.45359237", formula)
        self.assertIn("ELSE NULL END", formula)

    def test_option_is_schema_complete(self) -> None:
        option = detect_uom_normalization(
            "Average cost per kilogram by lane", _MIXED_WEIGHT
        )
        assert option is not None
        self.assertTrue(_REQUIRED_OPTION_FIELDS.issubset(option.keys()))
        self.assertTrue(option["needs_user_confirmation"])
        self.assertEqual(option["evidence_state"], "candidate_derivation_not_ground_truth")
        # Two input columns (quantity + unit), each evidence-backed.
        roles = {c["input_name"] for c in option["input_columns"]}
        self.assertEqual(roles, {"quantity", "unit"})
        for col in option["input_columns"]:
            self.assertIn("observed_values", col)
            self.assertIn("value_profile", col)
            self.assertIn("semantic_meaning_sources", col)
            self.assertIn("reason", col)

    def test_quantity_column_name_in_prose_triggers_without_unit_word(self) -> None:
        # No unit word, but the quantity column itself is referenced: still a
        # mixed-unit hazard worth surfacing. Target falls back to the most
        # frequently observed unit (KG appears 3x vs LB 2x).
        option = detect_uom_normalization(
            "Total wgt shipped by lane", _MIXED_WEIGHT
        )
        self.assertIsNotNone(option)
        self.assertEqual(option["derived_column_name"], "wgt_kg")

    def test_single_unit_yields_no_option(self) -> None:
        # Every observed unit is KG -- no normalization needed, honest blocker
        # must remain (no fabricated option).
        single = [
            _col("wgt", "ops/shipments.csv", "Float64", [2.9, 3.1, 3.8]),
            _col("wgt_uom", "ops/shipments.csv", "String", ["KG", "KG", "KG"]),
        ]
        self.assertIsNone(
            detect_uom_normalization("Average cost per kilogram by lane", single)
        )

    def test_unrelated_prose_yields_no_option(self) -> None:
        # The data has the mixed-unit hazard, but the question references neither
        # the quantity, the unit column, nor a unit dimension -> no noise.
        self.assertIsNone(
            detect_uom_normalization(
                "Monthly revenue per active account", _MIXED_WEIGHT
            )
        )

    def test_no_unit_column_yields_no_option(self) -> None:
        no_units = [
            _col("wgt", "ops/shipments.csv", "Float64", [2.9, 3.1, 3.8]),
            _col("ship_ref", "ops/shipments.csv", "String", ["S1", "S2", "S3"]),
        ]
        self.assertIsNone(
            detect_uom_normalization("Average cost per kilogram by lane", no_units)
        )

    def test_length_dimension_normalizes_to_prose_unit(self) -> None:
        # Generic across dimensions: a distance in mixed mi/km normalized to the
        # prose's unit (kilometre -> km). MI -> KM factor is 1609.344/1000.
        distance = [
            _col("dist", "ops/legs.csv", "Float64", [10.0, 25.0, 40.0]),
            _col("dist_unit", "ops/legs.csv", "String", ["MI", "KM", "MI"]),
        ]
        option = detect_uom_normalization(
            "Average fuel burn per kilometre by route", distance
        )
        self.assertIsNotNone(option)
        self.assertEqual(option["derived_column_name"], "dist_km")
        self.assertIn("1.609344", option["formula"])

    def test_detect_derivation_patterns_includes_uom(self) -> None:
        options = detect_derivation_patterns(
            "Average cost per kilogram by lane", _MIXED_WEIGHT
        )
        names = {o.get("derived_column_name") for o in options}
        self.assertIn("wgt_kg", names)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
