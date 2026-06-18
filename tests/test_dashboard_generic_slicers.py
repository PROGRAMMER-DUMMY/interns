"""Generic slicer cross-filtering on the KPIs page.

A KPI's hero chart is normally bound to its pre-aggregated gold table, which
dropped every dimension the KPI didn't itself break down by -- so a Gender
slicer could not touch a "top payers" chart whose gold only had PayorID. These
tests lock in the fix:

1. ``_reroot_hero_to_fact`` re-sources the chart onto the row-grain conformed
   table ONLY when that provably reproduces the validated gold result, carrying
   the KPI's intrinsic equality scope as overridable ``base_filters``. A scope it
   cannot reproduce (a range scope, a precomputed share) leaves the chart
   untouched -- the KPI's meaning is never silently changed.
2. ``_apply_base_filters`` makes that scope the default view yet lets any page
   slicer / cross-filter on the same column override it.
3. ``_stack_by_multiselect`` turns a single-series bar into a stacked bar when a
   slicer is multi-selected, splitting on that dimension.
"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

import polars as pl

_VENDOR = str((Path(__file__).resolve().parents[1] / "vendor"))
if _VENDOR not in sys.path:
    sys.path.insert(0, _VENDOR)

from core.dashboard.minus_adapter import _reroot_hero_to_fact  # noqa: E402
from core.dashboard.model.cuts import KpiModel  # noqa: E402
from minus.config.models import Widget  # noqa: E402
from minus.render.layout import (  # noqa: E402
    _apply_base_filters,
    _stack_by_multiselect,
)


def _fact() -> pl.DataFrame:
    # Row-grain conformed table: two lines of business, a gender, a payer.
    return pl.DataFrame({
        "PayorID": ["P1", "P1", "P2", "P2", "P3", "P3"],
        "LineOfBusiness": ["Commercial", "Commercial", "Commercial",
                            "Medicaid", "Medicaid", "Commercial"],
        "Gender": ["F", "M", "F", "F", "M", "M"],
        "PaidAmount": [10.0, 5.0, 20.0, 7.0, 3.0, 4.0],
    })


def _commercial_gold() -> pl.DataFrame:
    # The KPI's validated result: sum(PaidAmount) by payer, Commercial only.
    # P1=15, P2=20, P3=4  -> a constant LineOfBusiness='Commercial' column.
    return pl.DataFrame({
        "LineOfBusiness": ["Commercial", "Commercial", "Commercial"],
        "PayorID": ["P2", "P1", "P3"],
        "sum_paidamount": [20.0, 15.0, 4.0],
    })


def _km(measure="sum_paidamount", cuts=("LineOfBusiness", "PayorID")) -> KpiModel:
    return KpiModel(
        kpi_id="kpi_x", title="top payers in Commercial", gold_columns=list(cuts) + [measure],
        measure=measure, cuts=list(cuts), kind="sum", additive=True,
    )


_FACT_MEASURES = [
    {"name": "record_count", "agg": "count", "table": "transactions"},
    {"name": "total_paid_amount", "agg": "sum", "field": "transactions.PaidAmount"},
]


class TestReroot(unittest.TestCase):
    def test_reroots_when_gold_is_reproducible(self):
        hero = {"id": "h", "type": "bar", "measure": "kpi_x_sum",
                "dimension": "kpi_x.PayorID", "title": "Paid by Payer"}
        out = _reroot_hero_to_fact(hero, _km(), _commercial_gold(),
                                   "transactions", _fact(), _FACT_MEASURES)
        self.assertIsNotNone(out)
        # rebound onto the row-grain fact + its matching sum measure
        self.assertEqual(out["dimension"], "transactions.PayorID")
        self.assertEqual(out["measure"], "total_paid_amount")
        # intrinsic equality scope carried as an overridable default
        self.assertEqual(out["base_filters"], {"transactions.LineOfBusiness": "Commercial"})
        # gold is the top-N result -> chart keeps top-N (here all 3 payers)
        self.assertEqual(out["limit"], 3)

    def test_refuses_when_scope_not_reproducible(self):
        # Gold here is a Commercial+Female subset, but the only constant column is
        # LineOfBusiness; the Gender restriction is NOT constant-detectable, so
        # recomputing under Commercial alone overshoots and must NOT re-root.
        gold = pl.DataFrame({
            "LineOfBusiness": ["Commercial", "Commercial"],
            "PayorID": ["P2", "P1"],
            "sum_paidamount": [20.0, 10.0],   # P1 Female-only = 10, not 15
        })
        hero = {"id": "h", "type": "bar", "measure": "kpi_x_sum",
                "dimension": "kpi_x.PayorID", "title": "t"}
        out = _reroot_hero_to_fact(hero, _km(), gold, "transactions",
                                   _fact(), _FACT_MEASURES)
        self.assertIsNone(out)


class TestBaseFilterOverride(unittest.TestCase):
    def _w(self):
        return Widget(id="w", type="bar", measure="total_paid_amount",
                      dimension="transactions.PayorID",
                      base_filters={"transactions.LineOfBusiness": "Commercial"})

    def test_default_scope_applies_when_slicer_untouched(self):
        wf = _apply_base_filters(self._w(), {}, {})
        self.assertEqual(wf, {"transactions.LineOfBusiness": "Commercial"})

    def test_slicer_on_same_column_overrides_default(self):
        wf = _apply_base_filters(self._w(), {"LineOfBusiness": "Medicaid"}, {})
        self.assertEqual(wf, {"LineOfBusiness": "Medicaid"})

    def test_orthogonal_slicer_keeps_default_scope(self):
        wf = _apply_base_filters(self._w(), {"Gender": "F"}, {})
        self.assertEqual(wf, {"transactions.LineOfBusiness": "Commercial", "Gender": "F"})


class TestStackOnMultiSelect(unittest.TestCase):
    def _bar(self):
        return Widget(id="w", type="bar", measure="total_paid_amount",
                      dimension="transactions.PayorID")

    def test_multi_select_promotes_to_stacked_bar(self):
        out = _stack_by_multiselect(self._bar(),
                                    {"LineOfBusiness": ["Commercial", "Medicaid"]})
        self.assertEqual(out.type, "stacked_bar")
        self.assertEqual(out.breakdown, "transactions.LineOfBusiness")

    def test_single_value_does_not_promote(self):
        out = _stack_by_multiselect(self._bar(), {"LineOfBusiness": "Commercial"})
        self.assertEqual(out.type, "bar")
        self.assertIsNone(out.breakdown)

    def test_does_not_split_on_its_own_axis(self):
        out = _stack_by_multiselect(self._bar(), {"PayorID": ["P1", "P2"]})
        self.assertEqual(out.type, "bar")
        self.assertIsNone(out.breakdown)


if __name__ == "__main__":
    unittest.main()
