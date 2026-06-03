"""Whole-document type classification + candidate-confidence weighting.

The layer above per-block routing: classify what KIND of document this is
(data-dictionary / KPI-spec / report / reference / mixed) and use it to weight
which candidates the block classifier trusts. Deterministic + workspace-agnostic.
"""
from __future__ import annotations

import unittest

from core.onboarding.documents.classifier import (
    CANDIDATE_KPI_REGISTRY,
    CANDIDATE_LEXICON,
    DOC_TYPE_DATA_DICTIONARY,
    DOC_TYPE_KPI_SPEC,
    DOC_TYPE_MIXED,
    DOC_TYPE_REFERENCE,
    DOC_TYPE_REPORT,
    classify_document,
    detect_document_type,
)


def _doc(*blocks: dict) -> dict:
    return {"pages": [{"blocks": list(blocks)}]}


def _text(s: str) -> dict:
    return {"type": "text", "text": s}


def _table(headers: list[str], rows: list[list[str]]) -> dict:
    return {"type": "table", "headers": headers, "rows": rows}


class DetectDocumentTypeTests(unittest.TestCase):
    def test_data_dictionary(self):
        doc = _doc(_text("Data Dictionary — Glossary of Terms, Abbreviations and Data Type"))
        info = detect_document_type(doc)
        self.assertEqual(info["document_type"], DOC_TYPE_DATA_DICTIONARY)
        self.assertEqual(info["confidence"], "high")

    def test_kpi_spec(self):
        doc = _doc(_text("KPI definitions: each metric has a numerator, denominator and target threshold by dimension"))
        info = detect_document_type(doc)
        self.assertEqual(info["document_type"], DOC_TYPE_KPI_SPEC)
        self.assertEqual(info["confidence"], "high")

    def test_report(self):
        doc = _doc(_text("Executive Summary. Background, analysis, findings and conclusion of the results."))
        info = detect_document_type(doc)
        self.assertEqual(info["document_type"], DOC_TYPE_REPORT)

    def test_reference_when_no_signals(self):
        doc = _doc(_text("The quick brown fox jumped over the lazy dog near the river."))
        info = detect_document_type(doc)
        self.assertEqual(info["document_type"], DOC_TYPE_REFERENCE)
        self.assertEqual(info["confidence"], "none")

    def test_mixed_when_two_types_tie(self):
        doc = _doc(_text(
            "KPI metric numerator denominator. "
            "Data dictionary glossary abbreviation acronym."
        ))
        info = detect_document_type(doc)
        self.assertEqual(info["document_type"], DOC_TYPE_MIXED)


class CandidateWeightingTests(unittest.TestCase):
    def test_data_dictionary_boosts_lexicon_candidate(self):
        doc = _doc(
            _text("Data Dictionary / Glossary of Terms and Abbreviations"),
            _table(["Term", "Definition"], [["PaidAmount", "amount the payer paid"]]),
        )
        cands = classify_document(doc)
        lex = [c for c in cands if c["candidate_type"] == CANDIDATE_LEXICON]
        self.assertTrue(lex, "expected a lexicon candidate from a term/definition table")
        self.assertEqual(lex[0]["document_type"], DOC_TYPE_DATA_DICTIONARY)
        self.assertIn("document_type_boost", lex[0])
        self.assertGreater(lex[0]["document_type_boost"]["to"], lex[0]["document_type_boost"]["from"])

    def test_kpi_spec_boosts_kpi_candidate(self):
        doc = _doc(
            _text("KPI specification: metric, numerator, denominator, target, dimension"),
            _table(["KPI", "Metric", "Cuts"], [["Total Paid", "sum(PaidAmount)", "PayorID"]]),
        )
        cands = classify_document(doc)
        kpi = [c for c in cands if c["candidate_type"] == CANDIDATE_KPI_REGISTRY]
        self.assertTrue(kpi)
        self.assertEqual(kpi[0]["document_type"], DOC_TYPE_KPI_SPEC)
        self.assertIn("document_type_boost", kpi[0])

    def test_confidence_boost_is_capped(self):
        doc = _doc(
            _text("Data Dictionary Glossary Terms Abbreviation Acronym Data Type"),
            _table(["Term", "Definition"], [["X", "y"]]),
        )
        cands = classify_document(doc)
        for c in cands:
            self.assertLessEqual(float(c["confidence"]), 0.95)

    def test_every_candidate_tagged_with_document_type(self):
        doc = _doc(_table(["KPI", "Metric"], [["A", "sum(x)"]]))
        cands = classify_document(doc)
        self.assertTrue(cands)
        for c in cands:
            self.assertIn("document_type", c)


if __name__ == "__main__":
    unittest.main()
