"""Phase 2 tests: cross-filter + drill engine over the live model."""
from __future__ import annotations

import unittest
from pathlib import Path

import polars as pl

from core.dashboard.model.crossfilter import (
    CanvasModel,
    apply_global_filters,
    drill,
    load_canvas,
    panel_data,
)
from core.dashboard.model.cuts import KpiModel
from core.dashboard.model.layers import list_gold_kpis
from core.storage.workspace_layout import WorkspaceLayout

_WS = Path("workspaces/Healthcare-RCM-Data-Platform")


def _kpi_a_gold() -> pl.DataFrame:
    return pl.DataFrame({
        "month": ["2024-01", "2024-01", "2024-02", "2024-02"],
        "Department": ["Cardio", "Neuro", "Cardio", "Neuro"],
        "sum_paid": [100.0, 50.0, 200.0, 25.0],
    })


def _kpi_b_gold() -> pl.DataFrame:
    # shares Department with A; has no 'month'
    return pl.DataFrame({
        "Department": ["Cardio", "Neuro"],
        "Gender": ["M", "F"],
        "share": [60.0, 40.0],
    })


def _model(kpi_id, measure, cuts, additive=True) -> KpiModel:
    return KpiModel(
        kpi_id=kpi_id, title=kpi_id, gold_columns=cuts + [measure],
        measure=measure, cuts=cuts, kind="sum" if additive else "ratio",
        additive=additive,
    )


def _canvas() -> CanvasModel:
    a, b = _kpi_a_gold(), _kpi_b_gold()
    return CanvasModel(
        layout=WorkspaceLayout(project_root=_WS.resolve()),
        kpis={
            "a": _model("a", "sum_paid", ["month", "Department"]),
            "b": _model("b", "share", ["Department", "Gender"]),
        },
        gold={"a": a, "b": b},
    )


class TestSharedDimensions(unittest.TestCase):
    def test_department_is_shared(self):
        shared = _canvas().shared_dimensions()
        self.assertEqual(sorted(shared["department"]), ["a", "b"])
        self.assertEqual(shared["month"], ["a"])  # single-KPI dim


class TestGlobalFilter(unittest.TestCase):
    def test_applies_only_where_column_exists(self):
        canvas = _canvas()
        out = apply_global_filters(canvas, {"Department": "Cardio", "month": "2024-02"})
        # A has both columns -> Cardio AND 2024-02 -> 1 row
        self.assertEqual(out["a"].height, 1)
        self.assertEqual(out["a"].get_column("sum_paid").to_list(), [200.0])
        # B has Department (->Cardio, 1 row) but not month (ignored, no wipe)
        self.assertEqual(out["b"].height, 1)
        self.assertEqual(out["b"].get_column("Department").to_list(), ["Cardio"])

    def test_unfiltered_when_no_keys(self):
        canvas = _canvas()
        out = apply_global_filters(canvas, {})
        self.assertEqual(out["a"].height, 4)
        self.assertEqual(out["b"].height, 2)


class TestPanelData(unittest.TestCase):
    def test_rollup_by_x(self):
        canvas = _canvas()
        df = panel_data(canvas.kpis["a"], canvas.gold["a"],
                        {"chart_type": "bar", "x": "Department", "y": "sum_paid"})
        got = dict(zip(df.get_column("Department"), df.get_column("sum_paid")))
        self.assertEqual(got, {"Cardio": 300.0, "Neuro": 75.0})

    def test_topn_limit_and_order(self):
        canvas = _canvas()
        df = panel_data(canvas.kpis["a"], canvas.gold["a"],
                        {"chart_type": "ranked_bar", "x": "Department",
                         "y": "sum_paid", "limit": 1})
        self.assertEqual(df.height, 1)
        self.assertEqual(df.get_column("Department").to_list(), ["Cardio"])  # 300 > 75

    def test_filter_flows_through(self):
        canvas = _canvas()
        df = panel_data(canvas.kpis["a"], canvas.gold["a"],
                        {"chart_type": "bar", "x": "Department", "y": "sum_paid"},
                        filters={"month": "2024-01"})
        got = dict(zip(df.get_column("Department"), df.get_column("sum_paid")))
        self.assertEqual(got, {"Cardio": 100.0, "Neuro": 50.0})


class TestAdditiveConsistency(unittest.TestCase):
    def test_subtotals_sum_to_grand_total(self):
        canvas = _canvas()
        per_dept = panel_data(canvas.kpis["a"], canvas.gold["a"],
                              {"chart_type": "bar", "x": "Department", "y": "sum_paid"})
        subtotal = per_dept.get_column("sum_paid").sum()
        grand = canvas.gold["a"].get_column("sum_paid").sum()
        self.assertEqual(subtotal, grand)


class TestDrill(unittest.TestCase):
    def test_rollup_within_gold(self):
        canvas = _canvas()
        out = drill(canvas.layout, canvas.kpis["a"], canvas.gold["a"], ["month"])
        self.assertIsNotNone(out)
        got = dict(zip(out.get_column("month"), out.get_column("sum_paid")))
        self.assertEqual(got, {"2024-01": 150.0, "2024-02": 225.0})

    def test_below_grain_without_silver_returns_none(self):
        # 'PatientID' is not in gold and there is no real silver for synthetic kpi 'a'
        canvas = _canvas()
        out = drill(canvas.layout, canvas.kpis["a"], canvas.gold["a"], ["PatientID"])
        self.assertIsNone(out)

    def test_non_additive_recombination_returns_none(self):
        canvas = _canvas()
        ratio = _model("r", "avg_los", ["dept", "visit"], additive=False)
        gold = pl.DataFrame({"dept": ["A", "A"], "visit": ["in", "out"],
                             "avg_los": [3.0, 5.0]})
        out = drill(canvas.layout, ratio, gold, ["dept"])  # merges 2 rows
        self.assertIsNone(out)


class TestRealCanvas(unittest.TestCase):
    def test_load_real_workspace(self):
        if not (_WS / "interns/state/medallion/gold").exists():
            self.skipTest("gold layer not present")
        layout = WorkspaceLayout(project_root=_WS.resolve())
        canvas = load_canvas(layout)
        self.assertEqual(sorted(canvas.kpi_ids()), sorted(list_gold_kpis(layout)))
        # every panel of every KPI produces data without error
        for kid, model in canvas.kpis.items():
            for panel in model.panels:
                if not panel.get("x"):
                    continue
                df = panel_data(model, canvas.gold[kid], panel)
                self.assertGreater(df.height, 0, f"{kid} panel {panel.get('title')}")


if __name__ == "__main__":
    unittest.main()
