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


if __name__ == "__main__":
    unittest.main()
