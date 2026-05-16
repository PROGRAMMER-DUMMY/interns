from __future__ import annotations

import unittest

from core.onboarding.kpi_text_parser import (
    cell_at,
    clean_cell,
    first_existing,
    infer_metric_and_cuts,
    is_template_kpi_row,
)


class KPITextParserTests(unittest.TestCase):
    def test_template_row_detection(self):
        self.assertTrue(is_template_kpi_row("Key business question"))
        self.assertFalse(is_template_kpi_row("What is paid amount by payer?"))

    def test_infer_metric_and_cuts(self):
        metric, cuts = infer_metric_and_cuts("Paid amount by Medicare LOB and payer over 50")

        self.assertEqual(metric, "amount paid")
        self.assertIn("LOB = Medicare", cuts)
        self.assertIn("Payer", cuts)
        self.assertIn("Age > 50", cuts)

    def test_small_cell_helpers(self):
        self.assertEqual(first_existing({"a": "A"}, ["b", "a"]), "A")
        self.assertEqual(cell_at(["x"], 3), "")
        self.assertEqual(clean_cell(None), "")


if __name__ == "__main__":
    unittest.main()
