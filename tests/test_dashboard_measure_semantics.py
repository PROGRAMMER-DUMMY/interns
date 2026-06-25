"""Regression tests for workspace-agnostic dashboard measure semantics.

Locks the de-RCM fix: format/aggregation derive from the metric FUNCTION, so a
count/avg/share-heavy workspace renders correctly -- not the old `currency`/`sum`
defaults that were silently fit to RCM's all-money KPIs. Also covers the
deterministic screener guard, the `count(*)` title fix, and the Decimal-coercion
that lets every chart rasterize for PDF/PPTX export.
"""
from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

import yaml

from core.dashboard.model.cuts import (
    clean_measure_name,
    headline_agg,
    measure_fmt,
    measure_func,
)
from core.dashboard.screener import _check_measure_semantics


class MeasureSemanticsTests(unittest.TestCase):
    def test_metric_function_detection(self) -> None:
        self.assertEqual(measure_func("count(distinct Id)", "count_distinct_id"), "count")
        self.assertEqual(measure_func("count(*)", "row_count"), "count")
        self.assertEqual(measure_func("avg(BASE_COST)", "avg_base_cost"), "avg")
        self.assertEqual(measure_func("sum(PaidAmount)", "sum_paidamount"), "sum")
        # falls back to the measure column name when metric text is missing
        self.assertEqual(measure_func("", "avg_total_claim_cost"), "avg")

    def test_counts_are_int_not_currency(self) -> None:
        self.assertEqual(measure_fmt("count(distinct Id)", "count_distinct_id", ""), "int")
        self.assertEqual(measure_fmt("count(*)", "row_count", ""), "int")
        self.assertEqual(headline_agg("count(distinct Id)", "count_distinct_id", ""), "sum")

    def test_averages_are_not_summed(self) -> None:
        self.assertEqual(headline_agg("avg(BASE_COST)", "avg_base_cost", ""), "avg")
        self.assertEqual(measure_fmt("avg(BASE_COST)", "avg_base_cost", ""), "currency")
        # average of a non-money quantity stays a plain float, not currency
        self.assertEqual(measure_fmt("avg(age)", "avg_age", ""), "float")

    def test_money_sum_stays_currency_sum(self) -> None:
        # RCM's shape must keep working unchanged.
        self.assertEqual(measure_fmt("sum(PaidAmount)", "sum_paidamount", ""), "currency")
        self.assertEqual(headline_agg("sum(PaidAmount)", "sum_paidamount", ""), "sum")

    def test_share_is_percent_max(self) -> None:
        self.assertEqual(measure_fmt("count(distinct Id)", "percent_of_total", "percent"), "percent")
        self.assertEqual(headline_agg("count(distinct Id)", "percent_of_total", "percent"), "max")

    def test_count_star_title_is_not_asterisk(self) -> None:
        # `count(*)` previously produced a bare "*" card label.
        label = clean_measure_name("count(*)", "row_count", "")
        self.assertNotIn("*", label)
        self.assertTrue(label.strip())


class ScreenerSemanticGuardTests(unittest.TestCase):
    def _project(self, measures: list[dict]) -> Path:
        root = Path(tempfile.mkdtemp())
        (root / "project.yaml").write_text(
            yaml.safe_dump({"measures": measures}), encoding="utf-8")
        return root

    def test_clean_config_has_no_findings(self) -> None:
        root = self._project([
            {"name": "kpi_001_count_distinct_id", "agg": "sum", "fmt": "int",
             "field": "kpi_001.count_distinct_id"},
            {"name": "kpi_006_avg_base_cost", "agg": "avg", "fmt": "currency",
             "field": "kpi_006.avg_base_cost"},
            {"name": "kpi_004_percent_of_total", "agg": "max", "fmt": "percent",
             "field": "kpi_004.percent_of_total"},
        ])
        self.assertEqual(_check_measure_semantics(root), [])

    def test_currency_on_count_is_flagged(self) -> None:
        root = self._project([
            {"name": "kpi_001_count_distinct_id", "agg": "sum", "fmt": "currency",
             "field": "kpi_001.count_distinct_id"},
        ])
        findings = _check_measure_semantics(root)
        self.assertTrue(any("count" in f and "currency" in f for f in findings))

    def test_summed_average_is_flagged(self) -> None:
        root = self._project([
            {"name": "kpi_006_avg_base_cost", "agg": "sum", "fmt": "currency",
             "field": "kpi_006.avg_base_cost"},
        ])
        findings = _check_measure_semantics(root)
        self.assertTrue(any("average" in f and "sum" in f for f in findings))

    def test_summed_share_is_flagged(self) -> None:
        root = self._project([
            {"name": "kpi_004_share", "agg": "sum", "fmt": "percent"},
        ])
        findings = _check_measure_semantics(root)
        self.assertTrue(any("share" in f or "percent" in f for f in findings))


class DecimalCoercionTests(unittest.TestCase):
    def test_json_safe_coerces_decimal(self) -> None:
        from tools.dashboard_export_common import add_vendor_to_path
        add_vendor_to_path(Path(".").resolve())
        try:
            from minus.export.model import _json_safe
        except Exception as exc:  # pragma: no cover - export deps optional
            self.skipTest(f"export stack unavailable: {exc}")
        import decimal
        out = _json_safe({"x": [decimal.Decimal("1.5"), 2], "y": decimal.Decimal("3")})
        self.assertEqual(out, {"x": [1.5, 2], "y": 3.0})
        self.assertIsInstance(out["y"], float)


if __name__ == "__main__":
    unittest.main()
