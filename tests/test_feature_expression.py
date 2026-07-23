from __future__ import annotations

import unittest

from core.onboarding.features.expression import extract_expression


class FeatureExpressionTests(unittest.TestCase):
    def test_extract_expression_skips_sql_keywords_functions_and_literals(self):
        extracted = extract_expression(
            "SUM(PaidAmount) / NULLIF(COUNT(DISTINCT ClaimID), 0) "
            "WHERE LineOfBusiness = 'commercial'"
        )

        self.assertEqual(extracted.identifiers, ["PaidAmount", "ClaimID", "LineOfBusiness"])
        self.assertIn({"function": "SUM", "arguments": ["PaidAmount"]}, extracted.functions)
        self.assertIn({"function": "COUNT", "arguments": ["ClaimID"]}, extracted.functions)

    def test_extract_expression_deduplicates_case_insensitively(self):
        extracted = extract_expression("paidamount + PaidAmount + PAIDAMOUNT")

        self.assertEqual(extracted.identifiers, ["paidamount"])

    def test_extract_expression_skips_common_distinct_typo(self):
        extracted = extract_expression(
            "percentage of sum(distinct PatientID) / sum(disitnct PatientID) for departement"
        )

        self.assertEqual(extracted.identifiers, ["PatientID", "departement"])
        self.assertIn({"function": "sum", "arguments": ["PatientID"]}, extracted.functions)

    def test_extract_expression_skips_free_text_filler_words_and_possessive_fragments(self):
        # Found live: "count(distinct disputed inv_no) / count(distinct inv_no),
        # as a percentage" and "using KPI2's active definition" produced bogus
        # candidate features "a" and "s" (the possessive apostrophe splits
        # "KPI2's" into "KPI2" + "s"), which the downstream validator then
        # rejected outright rather than ever asking the user about them.
        extracted = extract_expression(
            "count(distinct disputed inv_no) / count(distinct inv_no), as a percentage, "
            "using KPI2's active definition, no damage claim"
        )
        self.assertNotIn("a", extracted.identifiers)
        self.assertNotIn("s", extracted.identifiers)
        self.assertNotIn("no", extracted.identifiers)
        self.assertIn("inv_no", extracted.identifiers)
        self.assertIn("KPI2", extracted.identifiers)


if __name__ == "__main__":
    unittest.main()
