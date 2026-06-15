"""Phase 1 tests: live dashboard model over the medallion gold/silver layers.

Pure-logic tests use synthetic Polars frames (no Delta dependency). One
integration test runs parity against the real Healthcare-RCM gold tables when
they are present; it is skipped otherwise so CI stays hermetic.
"""
from __future__ import annotations

import unittest
from pathlib import Path

import polars as pl

from core.dashboard.model.aggregate import (
    NonAdditiveError,
    aggregate,
    apply_filters,
    resolve_column,
)
from core.dashboard.model.cuts import classify_measure
from core.dashboard.model.layers import list_gold_kpis
from core.dashboard.model.parity import check_parity
from core.storage.workspace_layout import WorkspaceLayout

_WS = Path("workspaces/Healthcare-RCM-Data-Platform")


def _additive_frame() -> pl.DataFrame:
    # gold-like: cuts (month, PayorID, Gender) + additive measure
    return pl.DataFrame(
        {
            "month": ["2024-01", "2024-01", "2024-02", "2024-02"],
            "PayorID": ["A", "B", "A", "B"],
            "Gender": ["M", "F", "M", "F"],
            "sum_paidamount": [100.0, 50.0, 200.0, 25.0],
        }
    )


def _share_frame() -> pl.DataFrame:
    # single-attribution share: cells sum to 100 across the whole frame
    return pl.DataFrame(
        {
            "department_name": ["Cardio", "Cardio", "Neuro", "Neuro"],
            "Gender": ["M", "F", "M", "F"],
            "percentage_share": [40.0, 30.0, 20.0, 10.0],
        }
    )


class TestClassify(unittest.TestCase):
    def test_sum_is_additive(self):
        self.assertEqual(classify_measure(agg="sum", y_format="", metric="sum(PaidAmount)"),
                         ("sum", True))

    def test_percent_share_is_additive(self):
        # share with single attribution -> additive by design
        self.assertEqual(
            classify_measure(agg="sum", y_format="percent",
                             metric="percentage share of lives"),
            ("share", True),
        )

    def test_share_word_in_metric_without_format(self):
        kind, additive = classify_measure(agg="sum", y_format="", metric="market share")
        self.assertEqual((kind, additive), ("share", True))

    def test_avg_is_non_additive(self):
        self.assertEqual(classify_measure(agg="avg", y_format="", metric="avg(LOS)"),
                         ("ratio", False))

    def test_rate_metric_non_additive(self):
        kind, additive = classify_measure(agg="sum", y_format="", metric="readmission rate")
        self.assertFalse(additive)
        self.assertEqual(kind, "ratio")


class TestResolveColumn(unittest.TestCase):
    def test_case_insensitive(self):
        cols = ["month", "PayorID", "Gender", "sum_paidamount"]
        self.assertEqual(resolve_column("payorid", cols), "PayorID")
        self.assertEqual(resolve_column("GENDER", cols), "Gender")
        self.assertEqual(resolve_column("month", cols), "month")
        self.assertIsNone(resolve_column("nope", cols))


class TestApplyFilters(unittest.TestCase):
    def test_scalar_list_and_empty(self):
        df = _additive_frame()
        self.assertEqual(apply_filters(df, {"gender": "M"}).height, 2)  # case-insensitive
        self.assertEqual(apply_filters(df, {"PayorID": ["A", "B"]}).height, 4)
        self.assertEqual(apply_filters(df, {"PayorID": []}).height, 4)   # empty = no-op
        self.assertEqual(apply_filters(df, {"unknown": "x"}).height, 4)  # unknown = no-op


class TestTypeTolerantFilter(unittest.TestCase):
    def test_date_column_matches_string_click_value(self):
        import datetime as dt
        df = pl.DataFrame({
            "month": [dt.date(2024, 1, 1), dt.date(2024, 2, 1)],
            "sum_paid": [100.0, 250.0],
        })
        # a Plotly click on a line point yields the date as a string
        out = apply_filters(df, {"month": "2024-02-01"})
        self.assertEqual(out.height, 1)
        self.assertEqual(out.get_column("sum_paid").to_list(), [250.0])

    def test_int_coded_category_matches_string(self):
        df = pl.DataFrame({"code": [1, 2, 3], "m": [10.0, 20.0, 30.0]})
        out = apply_filters(df, {"code": "2"})
        self.assertEqual(out.get_column("m").to_list(), [20.0])


class TestAggregateAdditive(unittest.TestCase):
    def test_rollup_by_one_cut(self):
        df = _additive_frame()
        out = aggregate(df, measure="sum_paidamount", additive=True, group_by=["PayorID"])
        got = dict(zip(out.get_column("PayorID"), out.get_column("sum_paidamount")))
        self.assertEqual(got["A"], 300.0)
        self.assertEqual(got["B"], 75.0)

    def test_scalar_total(self):
        df = _additive_frame()
        out = aggregate(df, measure="sum_paidamount", additive=True, group_by=[])
        self.assertEqual(out.get_column("sum_paidamount").to_list()[0], 375.0)

    def test_filter_then_rollup(self):
        df = _additive_frame()
        out = aggregate(df, measure="sum_paidamount", additive=True,
                        group_by=["PayorID"], filters={"month": "2024-02"})
        got = dict(zip(out.get_column("PayorID"), out.get_column("sum_paidamount")))
        self.assertEqual(got, {"A": 200.0, "B": 25.0})

    def test_identity_at_full_grain(self):
        df = _additive_frame()
        cuts = ["month", "PayorID", "Gender"]
        out = aggregate(df, measure="sum_paidamount", additive=True, group_by=cuts)
        self.assertEqual(out.sort(cuts).get_column("sum_paidamount").to_list(),
                         df.sort(cuts).get_column("sum_paidamount").to_list())


class TestAggregateShareAdditive(unittest.TestCase):
    def test_share_sums_correctly(self):
        df = _share_frame()
        out = aggregate(df, measure="percentage_share", additive=True,
                        group_by=["department_name"])
        got = dict(zip(out.get_column("department_name"), out.get_column("percentage_share")))
        self.assertEqual(got["Cardio"], 70.0)
        self.assertEqual(got["Neuro"], 30.0)

    def test_share_total_is_100(self):
        df = _share_frame()
        out = aggregate(df, measure="percentage_share", additive=True, group_by=[])
        self.assertEqual(out.get_column("percentage_share").to_list()[0], 100.0)


class TestAggregateNonAdditive(unittest.TestCase):
    def test_rollup_recombination_refused(self):
        df = pl.DataFrame({
            "dept": ["A", "A", "B"],
            "visit": ["in", "out", "in"],
            "avg_los": [3.0, 5.0, 2.0],
        })
        # grouping by dept merges 2 rows -> must refuse, not average-of-averages
        with self.assertRaises(NonAdditiveError):
            aggregate(df, measure="avg_los", additive=False, group_by=["dept"])

    def test_filter_to_single_row_ok(self):
        df = pl.DataFrame({
            "dept": ["A", "A", "B"],
            "visit": ["in", "out", "in"],
            "avg_los": [3.0, 5.0, 2.0],
        })
        out = aggregate(df, measure="avg_los", additive=False, group_by=["dept", "visit"])
        self.assertEqual(out.height, 3)  # already one row per group -> allowed


class TestParityIntegration(unittest.TestCase):
    def test_real_workspace_parity(self):
        if not (_WS / "interns/state/medallion/gold").exists():
            self.skipTest("Healthcare-RCM gold layer not present")
        layout = WorkspaceLayout(project_root=_WS.resolve())
        kpis = list_gold_kpis(layout)
        self.assertTrue(kpis, "expected at least one gold KPI")
        results = check_parity(layout)
        failures = [r for r in results if not r.get("ok")]
        self.assertEqual(failures, [], f"parity failures: {failures}")


if __name__ == "__main__":
    unittest.main()
