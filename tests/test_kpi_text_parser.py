from __future__ import annotations

import unittest

from core.onboarding.kpi_text_parser import (
    KPI_CUTS_HEADERS,
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

    def test_sample_kpi_cuts_header_alias(self):
        lowered = {"cuts with drg(consolidated)": "Cuts with DRG(Consolidated)"}

        self.assertEqual(first_existing(lowered, KPI_CUTS_HEADERS), "Cuts with DRG(Consolidated)")

    def test_extract_kpis_from_sql(self):
        from core.onboarding.kpi_text_parser import extract_kpis_from_sql
        sql_text = """-- Connect to database
USE hospital_db;

-- OBJECTIVE 1: ENCOUNTERS OVERVIEW

-- a. How many total encounters occurred each year?

-- b. For each year, what percentage of all encounters belonged to each encounter class
-- (ambulatory, outpatient, wellness, urgent care, emergency, and inpatient)?

-- c. What percentage of encounters were over 24 hours versus under 24 hours?
"""
        kpis = extract_kpis_from_sql(sql_text, "hospital_analytics_questions.sql")
        self.assertEqual(len(kpis), 3)
        self.assertEqual(kpis[0]["name"], "How many total encounters occurred each year?")
        self.assertEqual(kpis[0]["description"], "OBJECTIVE 1: ENCOUNTERS OVERVIEW")
        self.assertEqual(kpis[1]["name"], "For each year, what percentage of all encounters belonged to each encounter class (ambulatory, outpatient, wellness, urgent care, emergency, and inpatient)?")


if __name__ == "__main__":
    unittest.main()
