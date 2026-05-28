from __future__ import annotations

import unittest

from core.onboarding.kpi.text_parser import (
    KPI_CUTS_HEADERS,
    cell_at,
    clean_cell,
    first_existing,
    infer_metric_and_cuts,
    is_template_kpi_row,
)
from core.onboarding.lexicon.builder import (
    CutPhrase,
    MetricPhrase,
    WorkspaceLexicon,
)


class KPITextParserTests(unittest.TestCase):
    def test_template_row_detection(self):
        self.assertTrue(is_template_kpi_row("Key business question"))
        self.assertFalse(is_template_kpi_row("What is paid amount by payer?"))

    def test_infer_without_lexicon_returns_empty(self):
        """The hardcoded healthcare/e-commerce keyword ladders have been
        removed. Without a workspace lexicon, infer_metric_and_cuts is empty —
        derive don't curate."""
        metric, cuts = infer_metric_and_cuts(
            "Paid amount by Medicare LOB and payer over 50"
        )
        self.assertEqual(metric, "")
        self.assertEqual(cuts, "")

    def test_infer_with_lexicon_returns_learned_metric_and_cuts(self):
        """With a workspace lexicon (built from authored KPI cells, accepted
        definitions, etc.) infer_metric_and_cuts surfaces the learned phrases.
        This test seeds a small lexicon directly to validate the wiring."""
        lexicon = WorkspaceLexicon(
            metric_phrases=[
                MetricPhrase(
                    phrase="amount paid",
                    metric="PaidAmount",
                    source="kpi_registry",
                    confidence="authored",
                ),
                MetricPhrase(
                    phrase="paid amount",
                    metric="PaidAmount",
                    source="kpi_registry",
                    confidence="authored",
                ),
            ],
            cut_phrases=[
                CutPhrase(
                    phrase="payer",
                    cut="Payer",
                    source="kpi_registry",
                    confidence="authored",
                ),
                CutPhrase(
                    phrase="medicare",
                    cut="LOB = Medicare",
                    source="workspace_feature_definitions",
                    confidence="user_confirmed",
                ),
            ],
            column_aliases={},
            cuts_headers=[],
        )
        metric, cuts = infer_metric_and_cuts(
            "Paid amount by Medicare LOB and payer over 50",
            lexicon=lexicon,
        )
        self.assertEqual(metric, "PaidAmount")
        self.assertIn("Payer", cuts)
        self.assertIn("LOB = Medicare", cuts)

    def test_infer_does_not_invent_phrases_outside_lexicon(self):
        """Without a learned phrase for 'created_at', it is never invented as
        a cut. The previous "trend → Time" curated rule has been removed."""
        _metric, cuts = infer_metric_and_cuts(
            "What is trend for amount paid for medicare LOB across gender and payer"
        )
        self.assertEqual(cuts, "")
        self.assertNotIn("created_at", cuts)

    def test_small_cell_helpers(self):
        self.assertEqual(first_existing({"a": "A"}, ["b", "a"]), "A")
        self.assertEqual(cell_at(["x"], 3), "")
        self.assertEqual(clean_cell(None), "")

    def test_sample_kpi_cuts_header_alias(self):
        lowered = {"cuts with drg(consolidated)": "Cuts with DRG(Consolidated)"}
        self.assertEqual(
            first_existing(lowered, KPI_CUTS_HEADERS),
            "Cuts with DRG(Consolidated)",
        )

    def test_extract_kpis_from_sql(self):
        from core.onboarding.kpi.text_parser import extract_kpis_from_sql

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
        self.assertEqual(
            kpis[1]["name"],
            "For each year, what percentage of all encounters belonged to each encounter class "
            "(ambulatory, outpatient, wellness, urgent care, emergency, and inpatient)?",
        )


if __name__ == "__main__":
    unittest.main()
