"""Dashboard spec fidelity: chart axes must reference real result-view columns.

Covers the dashboard generator's column-fidelity helpers (regression for the
flaw where kpi_002's spec used `x="Department Name"` / `y="value"` — columns
absent from the emitted result view, which the BUG-024 grain fix exposed)."""
from __future__ import annotations

import unittest

from core.dashboard.inference import (
    infer_measure_column,
    parse_result_view_columns,
    validate_spec_columns,
)


# kpi_002 shape after the BUG-024 grain fix: full-grain percentage share.
_KPI_002_SQL = """
CREATE OR REPLACE VIEW "kpi_002_features" AS
SELECT s0."PatientID", s1."Name", s0."VisitType", s2."Gender", s2."DOB"
FROM "t" AS s0;
CREATE OR REPLACE VIEW "kpi_002_results" AS
SELECT DISTINCT
  "Name" AS name,
  "VisitType" AS visittype,
  "Gender" AS gender,
  date_diff('year', CAST("DOB" AS DATE), CURRENT_DATE) AS age,
  COUNT(DISTINCT "PatientID") OVER (PARTITION BY "Name", "VisitType") AS sum_patientid_per_group,
  COUNT(DISTINCT "PatientID") OVER () AS sum_patientid_total,
  CAST(sum_patientid_per_group AS DOUBLE) / NULLIF(sum_patientid_total, 0) * 100 AS percentage_share
FROM "kpi_002_features";
"""


class ParseResultViewColumnsTests(unittest.TestCase):
    def test_extracts_result_view_aliases_only(self):
        cols = parse_result_view_columns(_KPI_002_SQL, "kpi_002")
        self.assertEqual(
            cols,
            [
                "name",
                "visittype",
                "gender",
                "age",
                "sum_patientid_per_group",
                "sum_patientid_total",
                "percentage_share",
            ],
        )

    def test_missing_view_returns_empty(self):
        self.assertEqual(parse_result_view_columns("SELECT 1;", "kpi_002"), [])


class ValidateSpecColumnsTests(unittest.TestCase):
    def setUp(self):
        self.columns = parse_result_view_columns(_KPI_002_SQL, "kpi_002")

    def test_raw_cut_label_and_value_placeholder_are_flagged(self):
        # The exact pre-fix bug: x is the raw cut LABEL, y is the literal "value".
        bad = {"x": "Department Name", "y": "value", "color": "visittype"}
        errors = validate_spec_columns(bad, self.columns)
        joined = " ".join(errors)
        self.assertIn("Department Name", joined)
        self.assertIn("value", joined)
        # color references a real emitted column -> not flagged.
        self.assertNotIn("'color'", joined)

    def test_spec_using_emitted_aliases_is_clean(self):
        good = {"x": "name", "y": "percentage_share", "color": "visittype"}
        self.assertEqual(validate_spec_columns(good, self.columns), [])

    def test_validation_skipped_when_columns_unknown(self):
        # SQL not yet generated -> no columns -> check is skipped, not failed.
        self.assertEqual(validate_spec_columns({"x": "anything"}, []), [])


class InferMeasureColumnTests(unittest.TestCase):
    def test_picks_measure_like_alias(self):
        cols = ["month", "lineofbusiness", "payorid", "gender", "age", "sum_paidamount"]
        self.assertEqual(infer_measure_column(cols), "sum_paidamount")

    def test_percentage_share_is_a_measure(self):
        self.assertEqual(
            infer_measure_column(parse_result_view_columns(_KPI_002_SQL, "kpi_002")),
            "percentage_share",
        )


if __name__ == "__main__":
    unittest.main()
