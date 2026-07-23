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

    def test_extract_expression_skips_arithmetic_operation_words(self):
        # Found live: "sum(Amount) by month, divided by count(distinct
        # accounts)" produced a bogus candidate feature "divided" -- the same
        # class of false positive as "average"/"total"/"share", which were
        # already stopwords; the arithmetic-verb family (divided/plus/minus/
        # multiplied) was missing.
        extracted = extract_expression(
            "sum(Amount) divided by count(active), plus fee minus discount, "
            "quantity multiplied by rate"
        )
        for word in ("divided", "plus", "minus", "multiplied"):
            self.assertNotIn(word, extracted.identifiers)
        self.assertIn("Amount", extracted.identifiers)
        self.assertIn("fee", extracted.identifiers)
        self.assertIn("discount", extracted.identifiers)
        self.assertIn("quantity", extracted.identifiers)
        self.assertIn("rate", extracted.identifiers)

    def test_extract_expression_skips_common_pronouns(self):
        # Found live: a custom rule description ("... for that account in the
        # period") produced a bogus candidate feature "that".
        extracted = extract_expression(
            "party_key WHERE EXISTS a non-void invoice for that account, this period, it counts"
        )
        for word in ("that", "this", "it"):
            self.assertNotIn(word, extracted.identifiers)
        self.assertIn("party_key", extracted.identifiers)
        self.assertIn("period", extracted.identifiers)
        self.assertIn("account", extracted.identifiers)

    def test_extract_expression_skips_relative_time_qualifiers(self) -> None:
        # Found live: "accounts active last quarter but not active this
        # quarter" produced a bogus candidate feature "last".
        extracted = extract_expression(
            "count(accounts active last quarter) / count(accounts active next quarter)"
        )
        self.assertNotIn("last", extracted.identifiers)
        self.assertNotIn("next", extracted.identifiers)
        self.assertIn("accounts", extracted.identifiers)
        self.assertIn("quarter", extracted.identifiers)

    def test_extract_expression_skips_analysis_vocabulary_and_conjunctions(self) -> None:
        # Closes the gap between this extractor's stopword list and the
        # downstream validator's separate PARSER_ARTIFACT_FEATURES denylist
        # (core.onboarding.workspace.validation) -- both lists exist to
        # reject the same class of non-feature token, and letting them drift
        # apart is exactly how "a" slipped through originally.
        extracted = extract_expression(
            "using the confirmed dimension, but not the grain or metric fields"
        )
        for word in ("using", "the", "but", "dimension", "grain", "metric"):
            self.assertNotIn(word, extracted.identifiers)
        self.assertIn("confirmed", extracted.identifiers)
        self.assertIn("fields", extracted.identifiers)


if __name__ == "__main__":
    unittest.main()
