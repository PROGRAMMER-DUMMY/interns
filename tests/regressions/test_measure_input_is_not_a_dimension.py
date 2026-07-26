"""Regression: the measure's input column must not also be a dimension.

Origin (2026-07-26 audit): for "percentage share of lives by gender, age,
visit type, department", PatientID -- the distinct-count measure input --
was emitted as a cut and placed in GROUP BY, giving one row per patient at
0.02327% each. Those identifiers then appeared on an exported slide.
"""
from __future__ import annotations

import unittest

from core.onboarding.kpi.result_view_builder import build_result_view_sql

KPI = {
    "kpi_id": "kpi_share",
    "name": "percentage share of lives by gender, visit type, department",
    "metric": "percentage of count(distinct PatientID) / count(distinct PatientID) for department",
    "cuts": "Department Name, VisitType, Gender",
    "features": [
        {"feature": "PatientID", "source_columns": [{"column": "PatientID"}]},
        {"feature": "Department Name", "source_columns": [{"column": "Name"}]},
        {"feature": "VisitType", "source_columns": [{"column": "VisitType"}]},
        {"feature": "Gender", "source_columns": [{"column": "Gender"}]},
    ],
}

# The audited shape: the "for <group>" token does NOT resolve to any declared
# cut, so the group resolver falls back -- and the closest emitted column was
# the metric's own distinct-count input.
KPI_UNRESOLVED_GROUP = {
    "kpi_id": "kpi_share_2",
    "name": "percentage share of lives by gender",
    "metric": "percentage of count(distinct PatientID) / count(distinct PatientID) for patients",
    "cuts": "Gender",
    "features": [
        {"feature": "PatientID", "source_columns": [{"column": "PatientID"}]},
        {"feature": "Gender", "source_columns": [{"column": "Gender"}]},
    ],
}


class MeasureInputDimensionTests(unittest.TestCase):
    def _sql(self, kpi: dict) -> str:
        return build_result_view_sql(
            kpi, kpi_id=str(kpi["kpi_id"]), feature_view='"f"', result_view='"r"',
        )

    def test_measure_input_is_not_grouped_by(self):
        for kpi in (KPI, KPI_UNRESOLVED_GROUP):
            with self.subTest(kpi=kpi["kpi_id"]):
                sql = self._sql(kpi)
                upper = sql.upper()
                group_by = upper.split("GROUP BY", 1)[1] if "GROUP BY" in upper else ""
                self.assertNotIn("PATIENTID", group_by)

    def test_measure_input_is_not_projected_as_a_dimension(self):
        for kpi in (KPI, KPI_UNRESOLVED_GROUP):
            with self.subTest(kpi=kpi["kpi_id"]):
                self.assertNotIn("AS patientid", self._sql(kpi))

    def test_the_declared_cuts_survive(self):
        sql = self._sql(KPI)
        for alias in ("department_name", "visittype", "gender"):
            with self.subTest(alias=alias):
                self.assertIn(alias, sql)


if __name__ == "__main__":
    unittest.main()
