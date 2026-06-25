"""Multi-workspace SHAPE GATE.

Nearly every dashboard/KPI bug this platform hit was the same class: a default fit
to ONE workspace's shape (Healthcare-RCM: all money, summed, `servicedate` dates)
that broke on a differently-shaped workspace. The platform "passed" by coincidence
of shape, not because it was generic.

This gate runs the ACTUAL decision functions over a matrix of distinct workspace
SHAPES and asserts shape-correctness invariants in BOTH directions:
  - correct, shape-derived config passes (no findings);
  - an RCM-shaped flat default (currency+sum for everything) is CAUGHT.

Adding a new shape is one entry in `SHAPES`. If this gate fails, a shape-specific
assumption leaked back in -- fix the derivation, not this test.
"""
from __future__ import annotations

import tempfile
import unittest
from dataclasses import dataclass
from pathlib import Path
from types import SimpleNamespace

import polars as pl
import yaml

from core.dashboard.model.cuts import headline_agg, measure_fmt
from core.dashboard.minus_adapter import _display_dimensions
from core.dashboard.model.conformed import _derive_columns
from core.dashboard.screener import _check_measure_semantics


@dataclass(frozen=True)
class MeasureShape:
    metric: str
    measure: str
    y_format: str
    expect_fmt: str        # int | currency | float | percent
    expect_agg: str        # sum | avg | max


@dataclass(frozen=True)
class WorkspaceShape:
    name: str
    measures: tuple[MeasureShape, ...]


# The matrix: one entry per distinct workspace shape the platform must handle.
SHAPES: tuple[WorkspaceShape, ...] = (
    WorkspaceShape(
        name="count_and_avg_heavy (Hospital_Patient_Records)",
        measures=(
            MeasureShape("count(distinct Id)", "count_distinct_id", "", "int", "sum"),
            MeasureShape("count(*)", "row_count", "", "int", "sum"),
            MeasureShape("avg(BASE_COST)", "avg_base_cost", "", "currency", "avg"),
            MeasureShape("avg(age)", "avg_age", "", "float", "avg"),
            MeasureShape("count(distinct Id)", "percent_of_total", "percent", "percent", "max"),
        ),
    ),
    WorkspaceShape(
        name="money_and_share_heavy (Healthcare-RCM)",
        measures=(
            MeasureShape("sum(PaidAmount)", "sum_paidamount", "", "currency", "sum"),
            MeasureShape("sum(charge_amount)", "sum_charge_amount", "", "currency", "sum"),
            MeasureShape("percentage of sum", "percentage_share", "percent", "percent", "max"),
        ),
    ),
    WorkspaceShape(
        name="mixed_quantity (generic retail-like)",
        measures=(
            MeasureShape("sum(quantity)", "sum_quantity", "", "int", "sum"),
            MeasureShape("avg(unit_price)", "avg_unit_price", "", "currency", "avg"),
            MeasureShape("count(distinct order_id)", "count_distinct_order_id", "", "int", "sum"),
            MeasureShape("max(rating)", "max_rating", "", "int", "max"),
        ),
    ),
)


class MeasureShapeInvariantTests(unittest.TestCase):
    """Direction 1: shape-derived fmt/agg is correct for every measure."""

    def test_fmt_and_agg_match_shape(self):
        for ws in SHAPES:
            for m in ws.measures:
                with self.subTest(workspace=ws.name, measure=m.measure):
                    self.assertEqual(
                        measure_fmt(m.metric, m.measure, m.y_format), m.expect_fmt,
                        f"{ws.name}: {m.measure} fmt")
                    self.assertEqual(
                        headline_agg(m.metric, m.measure, m.y_format), m.expect_agg,
                        f"{ws.name}: {m.measure} agg")

    def test_counts_never_currency_anywhere_in_matrix(self):
        for ws in SHAPES:
            for m in ws.measures:
                if m.metric.lower().startswith("count"):
                    self.assertNotEqual(
                        measure_fmt(m.metric, m.measure, m.y_format), "currency",
                        f"{ws.name}: count `{m.measure}` must never be currency")

    def test_averages_never_summed_anywhere_in_matrix(self):
        for ws in SHAPES:
            for m in ws.measures:
                if m.metric.lower().startswith("avg"):
                    self.assertNotEqual(
                        headline_agg(m.metric, m.measure, m.y_format), "sum",
                        f"{ws.name}: avg `{m.measure}` must never be summed")


class DeterministicGuardTests(unittest.TestCase):
    """Direction 2: the screener guard passes correct config and catches the
    RCM-shaped flat default for EVERY shape."""

    def _project(self, measures: list[dict]) -> Path:
        root = Path(tempfile.mkdtemp())
        (root / "project.yaml").write_text(yaml.safe_dump({"measures": measures}),
                                           encoding="utf-8")
        return root

    def test_shape_correct_config_has_no_findings(self):
        for ws in SHAPES:
            measures = [
                {"name": m.measure, "agg": m.expect_agg, "fmt": m.expect_fmt,
                 "field": f"t.{m.measure}"}
                for m in ws.measures
            ]
            with self.subTest(workspace=ws.name):
                self.assertEqual(_check_measure_semantics(self._project(measures)), [],
                                 f"{ws.name}: correct config must be clean")

    def test_rcm_shaped_flat_default_is_caught(self):
        # The classic bug: currency+sum for everything. The guard must flag it on
        # any shape that has a count or an average.
        for ws in SHAPES:
            has_count_or_avg = any(
                m.metric.lower().startswith(("count", "avg")) for m in ws.measures)
            if not has_count_or_avg:
                continue
            measures = [
                {"name": m.measure, "agg": "sum", "fmt": "currency",
                 "field": f"t.{m.measure}"}
                for m in ws.measures
            ]
            with self.subTest(workspace=ws.name):
                findings = _check_measure_semantics(self._project(measures))
                self.assertTrue(findings,
                                f"{ws.name}: flat currency/sum default must be caught")


class FrameShapeTests(unittest.TestCase):
    """Temporal axis + concentration guard must hold regardless of column naming."""

    def _model(self, frame: pl.DataFrame, dimensions: list[str]):
        return SimpleNamespace(frame=frame, dimensions=dimensions, fact="t", layout=None)

    def test_temporal_axis_derives_from_any_date_column(self):
        # Not named `servicedate`, and a Datetime (not pl.Date) -- the RCM-only
        # rule would miss this; the generic rule must derive `month`.
        df = pl.DataFrame({
            "START": pl.Series(["2024-01-15T00:00:00", "2024-02-20T00:00:00"]).str.to_datetime(),
            "amount": [10.0, 20.0],
        })
        out = _derive_columns(df)
        self.assertIn("month", out.columns)

    def test_concentration_guard_drops_dominated_dimension(self):
        # A column 97% one value (like SUFFIX blank) must not be charted.
        n = 1000
        suffix = [""] * 970 + ["PhD"] * 20 + ["MD"] * 10
        race = ["A"] * 250 + ["B"] * 250 + ["C"] * 250 + ["D"] * 250
        df = pl.DataFrame({"SUFFIX": suffix, "RACE": race})
        model = self._model(df, ["SUFFIX", "RACE"])
        dims = {d for d, _ in _display_dimensions(model)}
        self.assertNotIn("SUFFIX", dims, "97%-dominated column must be dropped")
        self.assertIn("RACE", dims, "balanced dimension must be kept")

    def test_continuous_float_column_is_not_a_dimension(self):
        # A money/decimal Float column is a MEASURE, not a category -- it makes a
        # meaningless donut/bar dimension even at low cardinality (BASE_ENCOUNTER_
        # COST = 85.55, 142.58, ...). Float excluded; Int category + string kept.
        df = pl.DataFrame({
            "BASE_COST": [85.55, 142.58, 136.80, 85.55, 142.58],   # Float measure
            "RATING": [1, 2, 3, 1, 2],                              # Int category
            "CLASS": ["a", "b", "a", "b", "a"],                    # string category
        })
        model = self._model(df, ["BASE_COST", "RATING", "CLASS"])
        dims = {d for d, _ in _display_dimensions(model)}
        self.assertNotIn("BASE_COST", dims, "continuous Float must not be a dimension")
        self.assertIn("CLASS", dims)
        self.assertIn("RATING", dims, "low-cardinality Int category must be kept")


if __name__ == "__main__":
    unittest.main()
