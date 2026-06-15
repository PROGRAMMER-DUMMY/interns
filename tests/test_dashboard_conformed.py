"""Phase A + DQ tests: conformed semantic model + data-quality certifier."""
from __future__ import annotations

import unittest
from pathlib import Path

import polars as pl

from core.dashboard.model.conformed import (
    ConformedModel,
    Edge,
    Measure,
    aggregate_measure,
    build_conformed_model,
)
from core.dashboard.model.dq import certify, run_quality_checks
from core.storage.workspace_layout import WorkspaceLayout

_WS = Path("workspaces/Healthcare-RCM-Data-Platform")


def _synthetic_model() -> ConformedModel:
    frame = pl.DataFrame({
        "PatientID": ["p1", "p2", "p3", "p1"],
        "DeptID": ["d1", "d1", "d2", "d2"],
        "PaidAmount": [100.0, 50.0, 200.0, 25.0],
        "Gender": ["M", "F", "M", "F"],
        "department_name": ["Cardio", "Cardio", "Neuro", "Neuro"],
        "age": [40, 55, 60, 40],
    })
    return ConformedModel(
        layout=WorkspaceLayout(project_root=_WS.resolve()),
        fact="transactions", frame=frame,
        measures={
            "Total Paid": Measure("Total Paid", "PaidAmount", "sum", "currency"),
            "Record Count": Measure("Record Count", "*", "count", "int"),
        },
        dimensions=["Gender", "department_name", "age"],
        edges=[Edge("transactions", "DeptID", "departments", "DeptID")],
    )


class TestAggregateMeasure(unittest.TestCase):
    def test_sum_by_dimension(self):
        m = _synthetic_model()
        out = aggregate_measure(m, "Total Paid", group_by=["department_name"])
        got = dict(zip(out.get_column("department_name"), out.get_column("Total Paid")))
        self.assertEqual(got, {"Cardio": 150.0, "Neuro": 225.0})

    def test_grand_total(self):
        m = _synthetic_model()
        out = aggregate_measure(m, "Total Paid", group_by=[])
        self.assertEqual(out.get_column("Total Paid").to_list()[0], 375.0)

    def test_count_and_filter(self):
        m = _synthetic_model()
        out = aggregate_measure(m, "Record Count", group_by=["Gender"],
                                filters={"department_name": "Cardio"})
        got = dict(zip(out.get_column("Gender"), out.get_column("Record Count")))
        self.assertEqual(got, {"M": 1, "F": 1})

    def test_free_dimension_cross_combo(self):
        # the whole point: measure x ANY two dimensions, live
        m = _synthetic_model()
        out = aggregate_measure(m, "Total Paid", group_by=["Gender", "age"])
        self.assertEqual(out.height, 4)  # (M,40),(F,55),(M,60),(F,40)
        self.assertAlmostEqual(out.get_column("Total Paid").sum(), 375.0, places=3)


class TestDQChecker(unittest.TestCase):
    def test_clean_model_passes_intrinsic(self):
        m = _synthetic_model()
        checks = run_quality_checks(m)
        # null_keys must pass on the synthetic clean frame
        null_check = next(c for c in checks if c["check"] == "null_keys")
        self.assertTrue(null_check["ok"])

    def test_null_key_is_flagged(self):
        m = _synthetic_model()
        bad = m.frame.with_columns(
            pl.when(pl.arange(0, m.frame.height) == 0).then(None)
            .otherwise(pl.col("PaidAmount")).alias("PaidAmount")
        )
        m2 = ConformedModel(layout=m.layout, fact=m.fact, frame=bad,
                            measures=m.measures, dimensions=m.dimensions, edges=m.edges)
        checks = run_quality_checks(m2)
        null_check = next(c for c in checks if c["check"] == "null_keys")
        self.assertFalse(null_check["ok"])


class TestConformedReal(unittest.TestCase):
    def _layout(self):
        if not (_WS / "interns/state/medallion/bronze").exists():
            self.skipTest("bronze layer not present")
        return WorkspaceLayout(project_root=_WS.resolve())

    def test_builds_and_certifies(self):
        layout = self._layout()
        m = build_conformed_model(layout)
        self.assertIsNotNone(m)
        self.assertEqual(m.fact, "transactions")
        # conformed star carries dims from BOTH joined dimensions + derived
        self.assertIn("department_name", m.frame.columns)  # from departments lookup
        self.assertIn("Gender", m.frame.columns)            # from patients
        self.assertIn("age", m.frame.columns)               # derived from DOB pre-redaction
        self.assertIn("month", m.frame.columns)             # derived from ServiceDate
        rep = certify(m)
        self.assertTrue(rep["ok"], f"certification failed: {rep['failed']}")

    def test_no_pii_in_model(self):
        layout = self._layout()
        m = build_conformed_model(layout)
        for pii in ("FirstName", "LastName", "SSN", "PhoneNumber", "Address", "DOB"):
            self.assertNotIn(pii, m.frame.columns)

    def test_no_fanout_rows_preserved(self):
        layout = self._layout()
        m = build_conformed_model(layout)
        self.assertEqual(m.frame.height, 10000)  # join did not multiply fact rows


if __name__ == "__main__":
    unittest.main()
