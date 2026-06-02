"""Tests for KPI intent-coverage checks (BUG-024 detector + named harness)."""
from __future__ import annotations

import unittest

from core.onboarding.kpi.intent_coverage import (
    declared_grain,
    denominator_scope_findings,
    evaluate_intent_coverage,
    grain_coverage_findings,
    output_shape_findings,
    result_view_block,
    temporal_anchor_findings,
)


def _kpi(**kwargs):
    base = {"kpi_id": "kpi_002", "name": "", "metric": "", "cuts": "", "features": []}
    base.update(kwargs)
    return base


# The percentage-share KPI from the Gemini session: grain must include
# gender / visit type / department, not department alone.
_PCT_KPI = _kpi(
    name="percentage share of lives by gender, age, visit type, department",
    metric="percentage of sum(distinct PatientID) / sum(distinct PatientID) for departement",
    cuts="Department Name, VisitType, Gender, Age (DOB)",
    features=[
        {"feature": "PatientID", "source_columns": [{"column": "PatientID"}]},
        {"feature": "Name", "source_columns": [{"column": "Name"}]},
        {"feature": "VisitType", "source_columns": [{"column": "VisitType"}]},
        {"feature": "Gender", "source_columns": [{"column": "Gender"}]},
        {"feature": "DOB", "source_columns": [{"column": "DOB"}]},
    ],
)

# The under-grained SQL the generator produced BEFORE BUG-024 was fixed:
# the features view carries VisitType/Gender/DOB, but the RESULT view groups
# on Name only.
_UNDERGRAINED_SQL = """
CREATE OR REPLACE VIEW "kpi_002_features" AS
SELECT s0."PatientID", s1."Name", s0."VisitType", s2."Gender", s2."DOB"
FROM "t" AS s0;
CREATE OR REPLACE VIEW "kpi_002_results" AS
SELECT DISTINCT
  "Name" AS name,
  COUNT(DISTINCT "PatientID") OVER (PARTITION BY "Name") AS sum_patientid_per_name,
  COUNT(DISTINCT "PatientID") OVER () AS sum_patientid_total,
  CAST(sum_patientid_per_name AS DOUBLE) / NULLIF(sum_patientid_total, 0) * 100 AS percentage_share
FROM "kpi_002_features";
"""

# The full-grain SQL after the fix.
_FULLGRAIN_SQL = """
CREATE OR REPLACE VIEW "kpi_002_features" AS
SELECT s0."PatientID", s1."Name", s0."VisitType", s2."Gender", s2."DOB"
FROM "t" AS s0;
CREATE OR REPLACE VIEW "kpi_002_results" AS
SELECT DISTINCT
  "Name" AS name,
  "VisitType" AS visittype,
  "Gender" AS gender,
  date_diff('year', CAST("DOB" AS DATE), CURRENT_DATE) AS age,
  COUNT(DISTINCT "PatientID") OVER (PARTITION BY "Name", "VisitType", "Gender", date_diff('year', CAST("DOB" AS DATE), CURRENT_DATE)) AS sum_patientid_per_group,
  COUNT(DISTINCT "PatientID") OVER () AS sum_patientid_total,
  CAST(sum_patientid_per_group AS DOUBLE) / NULLIF(sum_patientid_total, 0) * 100 AS percentage_share
FROM "kpi_002_features";
"""


class GrainCoverageTests(unittest.TestCase):
    def test_dropped_cuts_are_flagged_as_errors(self):
        findings = grain_coverage_findings(_PCT_KPI, _UNDERGRAINED_SQL)
        expected = {f.expected for f in findings if f.severity == "error"}
        # VisitType, Gender, and Age were dropped from the result grain.
        self.assertIn("VisitType", expected)
        self.assertIn("Gender", expected)
        self.assertTrue(any("Age" in e for e in expected))
        # The department cut IS present, so it must not be flagged.
        self.assertFalse(any("Department" in e for e in expected))

    def test_full_grain_passes(self):
        self.assertEqual(grain_coverage_findings(_PCT_KPI, _FULLGRAIN_SQL), [])

    def test_scoped_to_result_view_not_features_view(self):
        # The cuts all appear in the FEATURES view but only Name is in the result
        # view — coverage must be scoped to the result view, so they still fail.
        findings = grain_coverage_findings(_PCT_KPI, _UNDERGRAINED_SQL)
        self.assertTrue(findings, "cuts present only in the features view must NOT count as realized")

    def test_independent_of_generator_parser(self):
        # declared_grain re-derives intent from raw cuts, not from parse_kpi.
        tokens = {raw for raw, _ in declared_grain(_PCT_KPI)}
        self.assertIn("VisitType", tokens)
        self.assertIn("Gender", tokens)


class EvaluateIntentCoverageTests(unittest.TestCase):
    def test_full_coverage_clean(self):
        self.assertEqual(evaluate_intent_coverage(_PCT_KPI, _FULLGRAIN_SQL), [])

    def test_missing_result_view_reported_once(self):
        findings = evaluate_intent_coverage(_PCT_KPI, "SELECT 1;")
        self.assertEqual([f.code for f in findings], ["result_view_missing"])

    def test_metric_not_realized(self):
        kpi = _kpi(
            kpi_id="kpi_003",
            name="total paid by payor",
            metric="sum(PaidAmount)",
            cuts="PayorID",
            features=[
                {"feature": "PaidAmount", "source_columns": [{"column": "PaidAmount"}]},
                {"feature": "PayorID", "source_columns": [{"column": "PayorID"}]},
            ],
        )
        sql = (
            'CREATE OR REPLACE VIEW "kpi_003_results" AS '
            'SELECT "PayorID" AS payorid, COUNT(*) AS n FROM "f" GROUP BY "PayorID";'
        )
        codes = {f.code for f in evaluate_intent_coverage(kpi, sql)}
        self.assertIn("metric_not_realized", codes)

    def test_explicit_quoted_filter_not_realized(self):
        kpi = _kpi(
            kpi_id="kpi_009",
            name="paid amount",
            metric="sum(PaidAmount)",
            cuts="LineOfBusiness = 'Medicare', PayorID",
            features=[
                {"feature": "PaidAmount", "source_columns": [{"column": "PaidAmount"}]},
                {"feature": "LineOfBusiness", "source_columns": [{"column": "LineOfBusiness"}]},
                {"feature": "PayorID", "source_columns": [{"column": "PayorID"}]},
            ],
        )
        sql = (
            'CREATE OR REPLACE VIEW "kpi_009_results" AS '
            'SELECT "PayorID" AS payorid, ROUND(SUM("PaidAmount"),2) AS paid '
            'FROM "f" GROUP BY "PayorID";'
        )
        codes = {f.code for f in evaluate_intent_coverage(kpi, sql)}
        self.assertIn("filter_not_realized", codes)


class ResultViewBlockTests(unittest.TestCase):
    def test_returns_block_from_result_view_onward(self):
        block = result_view_block(_UNDERGRAINED_SQL, "kpi_002")
        self.assertTrue(block.lstrip().lower().startswith('create or replace view "kpi_002_results"'))
        # The features view DEFINITION is excluded (only its FROM reference remains).
        self.assertNotIn('VIEW "kpi_002_features" AS', block)
        self.assertEqual(block.lower().count("create or replace view"), 1)

    def test_empty_when_no_result_view(self):
        self.assertEqual(result_view_block("SELECT 1;", "kpi_002"), "")


class DenominatorScopeFindingsTests(unittest.TestCase):
    """Phase 1: denominator_scope_findings enforces that a within-group
    denominator-scope decision is realized in the result-view SQL.

    Independent of result_view_builder.parse_kpi: uses only regex on the
    generated SQL so a generator that silently drops the scope cannot pass.
    """

    _KPI = _kpi(
        kpi_id="kpi_002",
        name="percentage share of lives by department",
        metric="percentage of sum(distinct PatientID) / sum(distinct PatientID) for departement",
        cuts="departement, Gender",
        features=[
            {"feature": "PatientID", "source_columns": [{"column": "PatientID"}]},
            {"feature": "departement", "source_columns": [{"column": "departement"}]},
            {"feature": "Gender", "source_columns": [{"column": "Gender"}]},
        ],
    )

    _OVER_GRAND_TOTAL_SQL = """
CREATE OR REPLACE VIEW "kpi_002_results" AS
SELECT DISTINCT
  "departement" AS departement,
  "Gender" AS gender,
  COUNT(DISTINCT "PatientID") OVER (PARTITION BY "departement", "Gender") AS per_group,
  COUNT(DISTINCT "PatientID") OVER () AS total,
  CAST(per_group AS DOUBLE) / NULLIF(total, 0) * 100 AS percentage_share
FROM "kpi_002_features";
"""

    _OVER_PARTITION_SQL = """
CREATE OR REPLACE VIEW "kpi_002_results" AS
SELECT DISTINCT
  "departement" AS departement,
  "Gender" AS gender,
  COUNT(DISTINCT "PatientID") OVER (PARTITION BY "departement", "Gender") AS per_group,
  COUNT(DISTINCT "PatientID") OVER (PARTITION BY "departement") AS total,
  CAST(per_group AS DOUBLE) / NULLIF(total, 0) * 100 AS percentage_share
FROM "kpi_002_features";
"""

    def test_within_group_scope_with_grand_total_over_raises_error(self):
        """The live bug: scope=within_department but SQL has OVER () →
        denominator_scope_not_realized."""
        findings = denominator_scope_findings(
            self._KPI, self._OVER_GRAND_TOTAL_SQL, "within_department"
        )
        codes = [f.code for f in findings]
        self.assertIn("denominator_scope_not_realized", codes)
        # Error severity.
        self.assertTrue(all(f.severity == "error" for f in findings))

    def test_within_group_scope_with_partition_by_passes(self):
        """scope=within_department and SQL has OVER (PARTITION BY ...) → no error."""
        findings = denominator_scope_findings(
            self._KPI, self._OVER_PARTITION_SQL, "within_department"
        )
        self.assertEqual(findings, [])

    def test_none_scope_never_errors(self):
        """No scope recorded → no denominator check performed."""
        findings = denominator_scope_findings(self._KPI, self._OVER_GRAND_TOTAL_SQL, None)
        self.assertEqual(findings, [])

    def test_grand_total_scope_never_errors(self):
        """Explicit grand_total scope → grand-total OVER () is correct."""
        findings = denominator_scope_findings(
            self._KPI, self._OVER_GRAND_TOTAL_SQL, "grand_total"
        )
        self.assertEqual(findings, [])

    def test_global_total_scope_never_errors(self):
        """global_total is also a grand-total alias → no error."""
        findings = denominator_scope_findings(
            self._KPI, self._OVER_GRAND_TOTAL_SQL, "global_total"
        )
        self.assertEqual(findings, [])

    def test_error_message_names_kpi_and_scope(self):
        """Error message must name the KPI and the scope for diagnosability."""
        findings = denominator_scope_findings(
            self._KPI, self._OVER_GRAND_TOTAL_SQL, "within_department"
        )
        self.assertTrue(findings)
        msg = findings[0].message
        self.assertIn("kpi_002", msg)
        self.assertIn("within_department", msg)

    def test_no_result_view_skips_check(self):
        """When the result view is absent from the SQL, no denominator check."""
        findings = denominator_scope_findings(self._KPI, "SELECT 1;", "within_department")
        self.assertEqual(findings, [])

    def test_within_scope_kpi_with_only_partition_by_numerator_not_denominator(self):
        """Numerator has PARTITION BY but denominator is OVER () — this should
        still raise the error since the denominator window is grand-total."""
        # This is the exact pattern the live bug produced.
        findings = denominator_scope_findings(
            self._KPI, self._OVER_GRAND_TOTAL_SQL, "within_department"
        )
        self.assertTrue(findings, "grand-total denominator under within-group scope must error")


class TemporalAnchorFindingsTests(unittest.TestCase):
    """Phase 2: temporal_anchor_findings catches age computed as CURRENT_DATE
    when an event-date grain column is declared (BUG-005 latent risk)."""

    _KPI_WITH_AGE_AND_EVENT = _kpi(
        kpi_id="kpi_003",
        name="amount trend for patients above 50",
        metric="sum(PaidAmount)",
        cuts="Month (ServiceDate), LineOfBusiness, Age(DOB)",
        features=[
            {"feature": "PaidAmount", "source_columns": [{"column": "PaidAmount"}]},
            {"feature": "ServiceDate", "source_columns": [{"column": "ServiceDate"}]},
            {"feature": "DOB", "source_columns": [{"column": "DOB"}]},
            {"feature": "LineOfBusiness", "source_columns": [{"column": "LineOfBusiness"}]},
        ],
    )

    _CURRENT_DATE_SQL = """
CREATE OR REPLACE VIEW "kpi_003_results" AS
SELECT "LineOfBusiness", date_diff('year', CAST("DOB" AS DATE), CURRENT_DATE) AS age,
  ROUND(SUM("PaidAmount"), 2) AS sum
FROM "kpi_003_features"
GROUP BY "LineOfBusiness", age;
"""

    _EVENT_DATE_SQL = """
CREATE OR REPLACE VIEW "kpi_003_results" AS
SELECT "LineOfBusiness",
  date_diff('year', CAST("DOB" AS DATE), CAST("ServiceDate" AS DATE)) AS age,
  ROUND(SUM("PaidAmount"), 2) AS sum
FROM "kpi_003_features"
GROUP BY "LineOfBusiness", age;
"""

    def test_current_date_with_event_grain_raises_error(self):
        findings = temporal_anchor_findings(self._KPI_WITH_AGE_AND_EVENT, self._CURRENT_DATE_SQL)
        codes = [f.code for f in findings]
        self.assertIn("temporal_anchor_not_realized", codes)

    def test_event_date_reference_passes(self):
        findings = temporal_anchor_findings(self._KPI_WITH_AGE_AND_EVENT, self._EVENT_DATE_SQL)
        self.assertEqual(findings, [])

    def test_no_age_cut_skips_check(self):
        kpi_no_age = _kpi(
            kpi_id="kpi_004",
            name="amount by month",
            metric="sum(PaidAmount)",
            cuts="Month (ServiceDate), LineOfBusiness",
            features=[],
        )
        findings = temporal_anchor_findings(kpi_no_age, self._CURRENT_DATE_SQL)
        self.assertEqual(findings, [])

    def test_age_cut_without_event_grain_skips_check(self):
        kpi_no_grain = _kpi(
            kpi_id="kpi_005",
            name="count by age",
            metric="count(*)",
            cuts="Age(DOB)",
            features=[],
        )
        # No Month(col) grain → no check (CURRENT_DATE is correct fallback).
        findings = temporal_anchor_findings(kpi_no_grain, self._CURRENT_DATE_SQL)
        self.assertEqual(findings, [])


class OutputShapeFindingsTests(unittest.TestCase):
    """Phase 2: output_shape_findings catches top-N KPIs missing LIMIT N."""

    _TOP5_KPI = _kpi(
        kpi_id="kpi_006",
        name="Top 5 payers by amount",
        metric="sum(PaidAmount)",
        cuts="PayorID",
        features=[
            {"feature": "PaidAmount", "source_columns": [{"column": "PaidAmount"}]},
            {"feature": "PayorID", "source_columns": [{"column": "PayorID"}]},
        ],
    )

    _WITH_LIMIT_SQL = """
CREATE OR REPLACE VIEW "kpi_006_results" AS
SELECT "PayorID", ROUND(SUM("PaidAmount"),2) AS total
FROM "kpi_006_features"
GROUP BY "PayorID"
ORDER BY total DESC
LIMIT 5;
"""

    _WITHOUT_LIMIT_SQL = """
CREATE OR REPLACE VIEW "kpi_006_results" AS
SELECT "PayorID", ROUND(SUM("PaidAmount"),2) AS total
FROM "kpi_006_features"
GROUP BY "PayorID"
ORDER BY total DESC;
"""

    def test_top_n_kpi_without_limit_raises_error(self):
        findings = output_shape_findings(self._TOP5_KPI, self._WITHOUT_LIMIT_SQL)
        codes = [f.code for f in findings]
        self.assertIn("output_shape_not_realized", codes)

    def test_top_n_kpi_with_correct_limit_passes(self):
        findings = output_shape_findings(self._TOP5_KPI, self._WITH_LIMIT_SQL)
        self.assertEqual(findings, [])

    def test_kpi_without_top_n_name_skips_check(self):
        kpi_no_top = _kpi(
            kpi_id="kpi_007",
            name="Amount paid by payer",
            metric="sum(PaidAmount)",
            cuts="PayorID",
            features=[],
        )
        findings = output_shape_findings(kpi_no_top, self._WITHOUT_LIMIT_SQL)
        self.assertEqual(findings, [])

    def test_wrong_limit_value_raises_error(self):
        wrong_limit_sql = self._WITH_LIMIT_SQL.replace("LIMIT 5", "LIMIT 10")
        findings = output_shape_findings(self._TOP5_KPI, wrong_limit_sql)
        codes = [f.code for f in findings]
        self.assertIn("output_shape_not_realized", codes)


if __name__ == "__main__":
    unittest.main()
