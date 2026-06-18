"""KPI improvement suggestions: only emitted when the readings warrant one."""
from __future__ import annotations

import unittest
from dataclasses import dataclass

import polars as pl

from core.dashboard.model.cuts import KpiModel
from core.dashboard.suggestions import (
    _measure_column,
    _suggest_add_cut,
    _suggest_parameterize_scope,
)


@dataclass
class _Meas:
    agg: str
    column: str


class _Model:
    """Minimal stand-in for ConformedModel: exposes .measures.values()."""

    def __init__(self, frame, measures):
        self.frame = frame
        self.measures = measures


def _km(measure, cuts, kind="sum", title="kpi") -> KpiModel:
    return KpiModel(
        kpi_id="kpi_x", title=title, gold_columns=list(cuts) + [measure],
        measure=measure, cuts=list(cuts), kind=kind, additive=(kind != "ratio"),
        card_label="Paid Amount",
    )


class TestParameterizeScope(unittest.TestCase):
    def test_fires_when_kpi_pins_a_dimension_the_data_varies_over(self):
        gold = pl.DataFrame({
            "LineOfBusiness": ["Commercial", "Commercial"],
            "PayorID": ["P1", "P2"], "sum_paid": [10.0, 20.0],
        })
        conf = pl.DataFrame({
            "LineOfBusiness": ["Commercial", "Medicaid", "Medicare", "Self-Pay"],
            "PayorID": ["P1", "P2", "P3", "P4"], "PaidAmount": [10.0, 5.0, 7.0, 3.0],
        })
        out = _suggest_parameterize_scope(_km("sum_paid", ["LineOfBusiness", "PayorID"]),
                                          gold, conf)
        self.assertEqual(len(out), 1)
        self.assertEqual(out[0].kind, "parameterize_scope")
        self.assertEqual(out[0].evidence["distinct_in_data"], 4)
        self.assertIn("slicer/parameter", out[0].text)

    def test_silent_when_dimension_is_constant_in_the_data_too(self):
        gold = pl.DataFrame({"LineOfBusiness": ["Commercial"], "PayorID": ["P1"],
                             "sum_paid": [10.0]})
        conf = pl.DataFrame({"LineOfBusiness": ["Commercial", "Commercial"],
                             "PayorID": ["P1", "P2"], "PaidAmount": [10.0, 5.0]})
        out = _suggest_parameterize_scope(_km("sum_paid", ["LineOfBusiness", "PayorID"]),
                                          gold, conf)
        self.assertEqual(out, [])


class TestAddCut(unittest.TestCase):
    def _setup(self):
        conf = pl.DataFrame({
            "PayorID": ["P1", "P1", "P2", "P2"],
            "Region": ["A", "B", "A", "B"],
            "PaidAmount": [100.0, 1.0, 100.0, 1.0],  # measure swings hard on Region
        })
        gold = pl.DataFrame({"PayorID": ["P1", "P2"], "sum_paid": [101.0, 101.0]})
        model = _Model(conf, {"tot": _Meas("sum", "PaidAmount")})
        return gold, conf, model

    def test_measure_column_reconstructs_from_row_grain(self):
        gold, conf, model = self._setup()
        src, scope = _measure_column(_km("sum_paid", ["PayorID"]), gold, conf, model)
        self.assertEqual(src, "PaidAmount")
        self.assertEqual(scope, {})

    def test_fires_for_decisive_aggregated_away_dimension(self):
        gold, conf, model = self._setup()
        out = _suggest_add_cut(_km("sum_paid", ["PayorID"]), gold, conf, model)
        self.assertEqual(len(out), 1)
        self.assertEqual(out[0].kind, "add_cut")
        self.assertEqual(out[0].evidence["dimension"], "Region")
        self.assertGreaterEqual(out[0].evidence["importance_eta2"], 0.12)

    def test_silent_when_measure_not_reconstructable(self):
        # A share KPI: no fact sum reproduces it -> no add_cut assertion.
        conf = pl.DataFrame({"PayorID": ["P1", "P2"], "Region": ["A", "B"],
                             "PaidAmount": [1.0, 1.0]})
        gold = pl.DataFrame({"PayorID": ["P1", "P2"], "share": [60.0, 40.0]})
        model = _Model(conf, {"tot": _Meas("sum", "PaidAmount")})
        out = _suggest_add_cut(_km("share", ["PayorID"], kind="share"), gold, conf, model)
        self.assertEqual(out, [])


if __name__ == "__main__":
    unittest.main()
