"""Regression: formula/statistical vocabulary must never survive as a
KPI "feature" needing resolution.

Origin (2026-08-03 review): a live review of workspaces/rcm's 18 KPIs found
56 of 74 open blocker rows were English words extracted from ratio/
percentile/z-score/banded-tier formula text (e.g. "std", "High",
"benchmark"), several later mis-recommended at "confidence: high" against
an unrelated column. BUSINESS_TEXT_STOPWORDS did not cover this vocabulary,
and strip_literals() does not catch letter+digit tokens like "P95".

Deliberately NOT touched: "LOS" (Length of Stay). It looked like noise
alongside the others in the original review, but it is not -- it is a real,
domain-specific business concept with no column backing it in rcm, and it
must keep surfacing as an open question. Adding it to a stopword list would
also bake a healthcare-specific abbreviation into generic filtering logic,
which this platform's workspace-agnostic rule forbids. This is the line
between "formula glue with zero business meaning" (safe to filter,
generic across any domain) and "a real concept the resolver correctly
doesn't have data for" (must never be silently dropped).
"""
from __future__ import annotations

import unittest

from core.onboarding.features.expression import extract_expression

# Real metric/cuts text from workspaces/rcm/interns/generated/contracts/semantic_contract.json,
# kpi_004 through kpi_018.
REAL_KPI_TEXTS = [
    "(count of unplanned readmissions within 30 days) / (expected readmissions per diagnosis benchmark)",
    "count(ChargeAmount > P95 within ICD group) / count(all encounters in ICD group)",
    "(sum(PaidAmount) / sum(ChargeAmount)) - ContractedRate",
    "percentile_rank(count(distinct EncounterID) per provider, within Specialization peer group)",
    "sum(ChargeAmount) where (Claim_Date - Service_Date) is approaching or past the payor-specific filing limit",
    "count(claims resolved within 30 days, no resubmission) / count(distinct ClaimID)",
    "count(denied claims later paid within 120 days) / count(all initially denied claims)",
    "count(encounters with no matching transaction) / count(distinct EncounterID)",
    "weighted composite score (0-100), banded Low <33 / Medium 33-66 / High >66",
    "avg(ICD relative weight) per department per month",
    "flag = 1 if monthly (PaidAmount/ChargeAmount) falls outside [mean +/- 2 std dev], else 0",
    "avg(Actual LOS) / avg(Expected LOS benchmark) per ICD",
    "sum(PaidAmount) - sum(AdjustmentAmount) - allocated overhead per provider",
    "z-score = (current month volume - 3yr same-month avg) / 3yr same-month std dev",
    "count(distinct PatientID touching >2 departments within 90 days) / count(distinct PatientID with 2+ encounters in 90 days)",
]

CONFIRMED_BAD_TOKENS = {
    "within", "per", "all", "benchmark", "std", "dev", "actual", "at", "on",
    "track", "expected", "expired", "high", "low", "medium", "flag", "if",
    "mean", "outside", "falls", "score", "weight", "weighted", "touching",
    "unplanned", "p95",
}

REAL_COLUMN_TOKENS = {
    "chargeamount", "adjustmentamount", "contractedrate",
    "claim_date", "service_date",
    "paidamount", "encounterid", "patientid", "claimid",
    "specialization",
}


class FormulaVocabularyExtractionTests(unittest.TestCase):
    def test_no_confirmed_bad_token_survives_extraction(self):
        for text in REAL_KPI_TEXTS:
            with self.subTest(text=text):
                extracted = extract_expression(text)
                identifiers_lower = {token.lower() for token in extracted.identifiers}
                leaked = identifiers_lower & CONFIRMED_BAD_TOKENS
                self.assertFalse(
                    leaked,
                    f"formula-vocabulary tokens leaked as features: {leaked} from: {text!r}",
                )

    def test_real_column_names_still_extracted(self):
        # The fix must not become so aggressive it also swallows real columns.
        combined = " ".join(REAL_KPI_TEXTS)
        extracted = extract_expression(combined)
        identifiers_lower = {token.lower() for token in extracted.identifiers}
        missing = REAL_COLUMN_TOKENS - identifiers_lower
        self.assertFalse(missing, f"real column tokens no longer extracted: {missing}")

    def test_percentile_literal_token_is_filtered(self):
        extracted = extract_expression("count(ChargeAmount > P95 within ICD group)")
        self.assertNotIn("P95", extracted.identifiers)
        self.assertNotIn("p95", {t.lower() for t in extracted.identifiers})

    def test_los_still_surfaces_as_a_real_unresolved_feature(self):
        # LOS is a real business concept with no backing column in rcm -- it
        # must keep surfacing as an open question, never get silently
        # dropped as if it were formula-glue noise like "std"/"benchmark".
        extracted = extract_expression("avg(Actual LOS) / avg(Expected LOS benchmark) per ICD")
        self.assertIn("los", {t.lower() for t in extracted.identifiers})
