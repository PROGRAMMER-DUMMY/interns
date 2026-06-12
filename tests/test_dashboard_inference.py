"""Regression tests for chart-type inference (phase D1).

Each chart-type rule has a dedicated test that exercises the inference rule
directly. Renderer-branch coverage lives in the second half of the file and
auto-skips when Dash/Plotly (the `dashboard` extra) are not importable, mirroring
the existing dashboard test modules.

Workspace-agnostic by construction: every fixture uses generic vocabulary
(orders / customers / regions / segments), never workspace domain words.

Owned by the `dashboard-engineer` subagent.
"""
from __future__ import annotations

import unittest

from core.dashboard.inference import (
    infer_chart,
    infer_measure_column,
    parse_result_view_columns,
    validate_spec_columns,
)


# ---------------------------------------------------------------------------
# SQL column extraction
# ---------------------------------------------------------------------------

class ParseResultViewColumnsTests(unittest.TestCase):
    def test_extracts_select_aliases_in_order(self):
        sql = (
            'CREATE OR REPLACE VIEW "kpi_007_results" AS\n'
            "SELECT order_month AS month, region AS region, SUM(amount) AS sum_amount\n"
            "FROM orders;\n"
        )
        self.assertEqual(
            parse_result_view_columns(sql, "kpi_007"),
            ["month", "region", "sum_amount"],
        )

    def test_ignores_cast_type_annotations(self):
        sql = (
            'CREATE OR REPLACE VIEW "kpi_007_results" AS\n'
            'SELECT CAST(ts AS DATE) AS order_date, COUNT(*) AS row_count\n'
            "FROM orders;\n"
        )
        # AS DATE inside CAST(...) must not be treated as an output column.
        self.assertEqual(
            parse_result_view_columns(sql, "kpi_007"),
            ["order_date", "row_count"],
        )

    def test_missing_view_returns_empty(self):
        self.assertEqual(parse_result_view_columns("SELECT 1", "kpi_009"), [])


# ---------------------------------------------------------------------------
# Measure inference
# ---------------------------------------------------------------------------

class InferMeasureColumnTests(unittest.TestCase):
    def test_prefers_aggregate_named_column(self):
        self.assertEqual(
            infer_measure_column(["region", "segment", "sum_amount"]),
            "sum_amount",
        )

    def test_falls_back_to_last_column(self):
        self.assertEqual(infer_measure_column(["region", "thing"]), "thing")

    def test_empty_returns_empty(self):
        self.assertEqual(infer_measure_column([]), "")


# ---------------------------------------------------------------------------
# Chart-type rules
# ---------------------------------------------------------------------------

class TrendRuleTests(unittest.TestCase):
    def test_date_cut_yields_line_with_real_measure(self):
        spec = infer_chart(
            definition={
                "business_question": "How does revenue trend over months by region?",
                "metric": "sum(amount)",
                "cuts": "order_month, region",
            },
            result_columns=["order_month", "region", "sum_amount"],
        )
        self.assertEqual(spec["chart_type"], "line")
        self.assertEqual(spec["x"], "order_month")
        self.assertEqual(spec["y"], "sum_amount")
        self.assertEqual(spec["color"], "region")

    def test_no_columns_defers_to_raw_cut_and_value_placeholder(self):
        spec = infer_chart(
            definition={"metric": "sum(amount)", "cuts": "order_month, region"},
        )
        self.assertEqual(spec["chart_type"], "line")
        self.assertEqual(spec["x"], "order_month")
        self.assertEqual(spec["y"], "value")


class TopNRuleTests(unittest.TestCase):
    def test_top_n_yields_horizontal_ranked_bar(self):
        spec = infer_chart(
            definition={
                "business_question": "What are the top 10 customers by amount?",
                "metric": "sum(amount)",
                "cuts": "customer, region",
            },
            result_columns=["customer", "region", "sum_amount"],
        )
        self.assertEqual(spec["chart_type"], "ranked_bar")
        self.assertEqual(spec["limit"], 10)
        self.assertEqual(spec["sort"], "desc")
        self.assertEqual(spec["orientation"], "h")
        self.assertEqual(spec["x"], "customer")
        self.assertEqual(spec["y"], "sum_amount")


class ShareRuleTests(unittest.TestCase):
    def test_share_metric_text_yields_stacked_percent_bar(self):
        spec = infer_chart(
            definition={
                "business_question": "What is the percentage share of orders by region and segment?",
                "metric": "percentage of sum(amount) per region",
                "cuts": "region, segment",
            },
            result_columns=["region", "segment", "percentage_share"],
        )
        self.assertEqual(spec["chart_type"], "stacked_bar_percent")
        self.assertEqual(spec["x"], "region")
        self.assertEqual(spec["color"], "segment")
        # measure resolves to the emitted share column, axis formatted as percent.
        self.assertEqual(spec["y"], "percentage_share")
        self.assertEqual(spec["y_format"], "percent")

    def test_share_measure_column_alone_triggers_share_rule(self):
        # Metric text has no percent vocabulary, but the emitted column does.
        spec = infer_chart(
            definition={
                "business_question": "Distribution of orders by region",
                "metric": "sum(amount)",
                "cuts": "region",
            },
            result_columns=["region", "share_ratio"],
        )
        self.assertEqual(spec["chart_type"], "stacked_bar_percent")
        self.assertEqual(spec["y"], "share_ratio")
        self.assertEqual(spec["y_format"], "percent")

    def test_share_metric_over_time_stays_line_but_percent_formatted(self):
        spec = infer_chart(
            definition={
                "business_question": "Trend of percentage share over months",
                "metric": "percent share of amount",
                "cuts": "order_month, region",
            },
            result_columns=["order_month", "region", "percentage_share"],
        )
        # A share over time is still a trend -> line wins, but y is percent.
        self.assertEqual(spec["chart_type"], "line")
        self.assertEqual(spec["y_format"], "percent")


class CategoricalAndCardRuleTests(unittest.TestCase):
    def test_single_cut_yields_vertical_bar(self):
        spec = infer_chart(
            definition={
                "business_question": "Orders by region",
                "metric": "count(order_id)",
                "cuts": "region",
            },
            result_columns=["region", "order_count"],
        )
        self.assertEqual(spec["chart_type"], "bar")
        self.assertEqual(spec["x"], "region")
        self.assertEqual(spec["y"], "order_count")

    def test_two_cuts_yields_grouped_bar(self):
        spec = infer_chart(
            definition={
                "business_question": "Orders by region and segment",
                "metric": "count(order_id)",
                "cuts": "region, segment",
            },
            result_columns=["region", "segment", "order_count"],
        )
        self.assertEqual(spec["chart_type"], "grouped_bar")
        self.assertEqual(spec["color"], "segment")

    def test_no_cut_yields_big_number_card(self):
        spec = infer_chart(
            definition={
                "business_question": "What is total revenue?",
                "metric": "sum(amount)",
                "cuts": "",
            },
            result_columns=["sum_amount"],
        )
        self.assertEqual(spec["chart_type"], "big_number")
        self.assertEqual(spec["y"], "sum_amount")


class ValidationTests(unittest.TestCase):
    def test_validate_flags_axis_column_not_in_result(self):
        spec = {"x": "Region Name", "y": "sum_amount", "color": ""}
        errors = validate_spec_columns(spec, ["region", "sum_amount"])
        self.assertEqual(len(errors), 1)
        self.assertIn("'x'", errors[0])

    def test_validate_clean_when_all_axes_present(self):
        spec = {"x": "region", "y": "sum_amount", "color": "segment"}
        self.assertEqual(
            validate_spec_columns(spec, ["region", "segment", "sum_amount"]),
            [],
        )

    def test_validate_skipped_when_columns_unknown(self):
        spec = {"x": "anything", "y": "value"}
        self.assertEqual(validate_spec_columns(spec, []), [])


# ---------------------------------------------------------------------------
# Renderer-branch coverage (env-skips without the `dashboard` extra)
# ---------------------------------------------------------------------------

try:  # pragma: no cover - import guard
    import plotly  # noqa: F401

    from core.dashboard.renderer import _figure_from_spec
    from core.dashboard.spec import DashboardSpec

    _HAS_DASHBOARD = True
except Exception:  # pragma: no cover
    _HAS_DASHBOARD = False


def _spec(config: dict) -> "DashboardSpec":
    return DashboardSpec(
        kpi_id="kpi_001",
        config=config,
        machine_defaults=config,
        user_overrides={},
        spec_path="dashboard/kpi_001.json",
    )


@unittest.skipUnless(_HAS_DASHBOARD, "dashboard extra (plotly) not installed")
class RendererBranchTests(unittest.TestCase):
    def test_ranked_bar_renders_horizontal_sorted_limited(self):
        rows = [
            {"customer": "a", "sum_amount": 10},
            {"customer": "b", "sum_amount": 40},
            {"customer": "c", "sum_amount": 20},
        ]
        fig = _figure_from_spec(
            _spec(
                {
                    "chart_type": "ranked_bar",
                    "x": "customer",
                    "y": "sum_amount",
                    "limit": 2,
                    "title": "Top customers",
                }
            ),
            rows,
        )
        bar = fig.data[0]
        self.assertEqual(bar.orientation, "h")
        # Top-2 by amount: b(40), c(20). Reversed for bottom-up draw -> c then b.
        self.assertEqual(list(bar.y), ["c", "b"])
        self.assertEqual(list(bar.x), [20, 40])

    def test_stacked_bar_percent_uses_percent_barnorm(self):
        rows = [
            {"region": "north", "segment": "x", "percentage_share": 30},
            {"region": "north", "segment": "y", "percentage_share": 70},
        ]
        fig = _figure_from_spec(
            _spec(
                {
                    "chart_type": "stacked_bar_percent",
                    "x": "region",
                    "y": "percentage_share",
                    "color": "segment",
                    "y_format": "percent",
                    "title": "Share by region",
                }
            ),
            rows,
        )
        self.assertEqual(fig.layout.barmode, "stack")
        self.assertEqual(fig.layout.barnorm, "percent")

    def test_big_number_renders_indicator(self):
        fig = _figure_from_spec(
            _spec({"chart_type": "big_number", "y": "sum_amount", "title": "Total"}),
            [{"sum_amount": 1234}],
        )
        self.assertEqual(fig.data[0].type, "indicator")
        self.assertEqual(fig.data[0].value, 1234)

    def test_missing_axis_column_falls_back_without_crashing(self):
        # Spec references a column the rows do not contain -> renderer must not
        # crash; it falls back to a real column and still returns a figure.
        rows = [{"region": "north", "order_count": 5}]
        fig = _figure_from_spec(
            _spec({"chart_type": "bar", "x": "Region Name", "y": "missing", "title": "X"}),
            rows,
        )
        self.assertTrue(len(fig.data) >= 1)


# ---------------------------------------------------------------------------
# D2 — clean-corporate-BI theme + headline numbers
# ---------------------------------------------------------------------------

@unittest.skipUnless(_HAS_DASHBOARD, "dashboard extra (plotly) not installed")
class CorporateThemeTests(unittest.TestCase):
    def test_theme_applies_editorial_colorway_and_transparent_canvas(self):
        fig = _figure_from_spec(
            _spec({"chart_type": "bar", "x": "region", "y": "sum_amount", "title": "X"}),
            [{"region": "north", "sum_amount": 10}],
        )
        # colorway comes from the active DESIGN.md tokens (categorical ramp);
        # canvas is transparent so the warm card shows through.
        from core.dashboard.design_md import DesignTokens
        self.assertEqual(fig.layout.colorway[0], DesignTokens().categorical[0])
        self.assertEqual(fig.layout.paper_bgcolor, "rgba(0,0,0,0)")
        self.assertEqual(fig.layout.plot_bgcolor, "rgba(0,0,0,0)")

    def test_single_series_uses_editorial_accent(self):
        fig = _figure_from_spec(
            _spec({"chart_type": "bar", "x": "region", "y": "sum_amount", "title": "X"}),
            [{"region": "north", "sum_amount": 10}, {"region": "south", "sum_amount": 20}],
        )
        # single series -> all bars the editorial accent (sienna)
        self.assertEqual(str(fig.data[0].marker.color).lower(), "#b4441c")

    def test_multi_series_uses_colorblind_safe_palette(self):
        fig = _figure_from_spec(
            _spec({"chart_type": "bar", "x": "region", "y": "v", "color": "seg", "title": "X"}),
            [
                {"region": "n", "seg": "a", "v": 10}, {"region": "n", "seg": "b", "v": 5},
                {"region": "s", "seg": "a", "v": 8}, {"region": "s", "seg": "b", "v": 9},
            ],
        )
        # multi-series -> first two traces are the CVD-safe blue + vermillion,
        # which are far apart in delta-E (verify gate enforces separation).
        from tools.dashboard_verify import _delta_e
        c0 = str(fig.data[0].marker.color).lower()
        c1 = str(fig.data[1].marker.color).lower()
        self.assertEqual(c0, "#0072b2")
        self.assertEqual(c1, "#d55e00")
        self.assertGreater(_delta_e(c0, c1), 16.0)

    def test_ordinal_banded_x_axis_sorts_by_natural_number(self):
        # Age bands must render 0-9,10-19,... not in arbitrary first-appearance order.
        rows = [
            {"band": "20-29", "v": 3}, {"band": "0-9", "v": 5},
            {"band": "100-109", "v": 1}, {"band": "10-19", "v": 4},
        ]
        fig = _figure_from_spec(
            _spec({"chart_type": "bar", "x": "band", "y": "v", "title": "X"}), rows
        )
        self.assertEqual(fig.layout.xaxis.categoryorder, "array")
        self.assertEqual(list(fig.layout.xaxis.categoryarray), ["0-9", "10-19", "20-29", "100-109"])

    def test_nominal_x_axis_sorts_by_value_descending(self):
        rows = [{"dept": "a", "v": 1}, {"dept": "b", "v": 9}, {"dept": "c", "v": 5}]
        fig = _figure_from_spec(
            _spec({"chart_type": "bar", "x": "dept", "y": "v", "title": "X"}), rows
        )
        self.assertEqual(fig.layout.xaxis.categoryorder, "total descending")

    def test_log_scale_ignored_on_bars_applied_on_lines(self):
        # Bar length must stay proportional to value: a log_scale flag on any
        # bar chart is ignored (axis stays linear). Lines may go log.
        bar = _figure_from_spec(
            _spec({"chart_type": "bar", "x": "region", "y": "sum_amount", "log_scale": True, "title": "X"}),
            [{"region": "mega", "sum_amount": 1_000_000}, {"region": "tiny", "sum_amount": 500}],
        )
        self.assertNotEqual(bar.layout.yaxis.type, "log")
        ranked = _figure_from_spec(
            _spec({"chart_type": "ranked_bar", "x": "region", "y": "sum_amount", "log_scale": True, "title": "X"}),
            [{"region": "mega", "sum_amount": 1_000_000}, {"region": "tiny", "sum_amount": 500}],
        )
        self.assertNotEqual(ranked.layout.xaxis.type, "log")
        line = _figure_from_spec(
            _spec({"chart_type": "line", "x": "month", "y": "sum_amount", "log_scale": True, "title": "X"}),
            [{"month": "2024-01", "sum_amount": 1_000_000}, {"month": "2024-02", "sum_amount": 500}],
        )
        self.assertEqual(line.layout.yaxis.type, "log")

    def test_stacked_percent_uses_percent_suffix_0_100_not_double_scaled(self):
        # barnorm yields 0-100; the axis must use a '%' suffix on a 0-100 range,
        # NOT tickformat '.0%' (which would x100 again -> 5000%/10000%).
        fig = _figure_from_spec(
            _spec({
                "chart_type": "stacked_bar_percent", "x": "region", "y": "share",
                "color": "seg", "y_format": "percent", "title": "X",
            }),
            [{"region": "n", "seg": "a", "share": 30}, {"region": "n", "seg": "b", "share": 70}],
        )
        self.assertEqual(fig.layout.yaxis.ticksuffix, "%")
        self.assertEqual(tuple(fig.layout.yaxis.range), (0, 100))
        self.assertNotEqual(fig.layout.yaxis.tickformat, ".0%")


@unittest.skipUnless(_HAS_DASHBOARD, "dashboard extra (plotly) not installed")
class HeadlineTests(unittest.TestCase):
    def _headline(self, config, rows):
        from core.dashboard.renderer import _kpi_headline
        return _kpi_headline(rows, _spec(config))

    def test_currency_total_for_plain_measure(self):
        out = self._headline(
            {"chart_type": "line", "x": "month", "y": "sum_amount"},
            [{"month": "2024-01", "sum_amount": 200000}, {"month": "2024-02", "sum_amount": 274200}],
        )
        self.assertEqual(out, "$474.2K")

    def test_ranked_bar_shows_top_label_and_value(self):
        out = self._headline(
            {"chart_type": "ranked_bar", "x": "customer", "y": "sum_amount"},
            [{"customer": "a", "sum_amount": 10}, {"customer": "b", "sum_amount": 40}],
        )
        # "amount" -> currency formatting.
        self.assertEqual(out, "b: $40")

    def test_share_metric_headlines_segment_count_not_summed_percent(self):
        # Summing percentages is meaningless; show breadth (distinct categories).
        out = self._headline(
            {"chart_type": "stacked_bar_percent", "x": "dept", "y": "share", "y_format": "percent"},
            [{"dept": "a", "share": 26}, {"dept": "b", "share": 12}, {"dept": "a", "share": 9}],
        )
        self.assertEqual(out, "2 segments")

    def test_share_metric_resolves_x_when_spec_label_mismatches_columns(self):
        # spec x is a human label not present in rows -> fall back to the real
        # categorical column rather than counting 0.
        out = self._headline(
            {"chart_type": "stacked_bar_percent", "x": "Department Name", "y": "share", "y_format": "percent"},
            [{"name": "a", "share": 1}, {"name": "b", "share": 2}, {"name": "c", "share": 3}],
        )
        self.assertEqual(out, "3 segments")

    def test_empty_rows_headline_is_dash(self):
        self.assertEqual(self._headline({"chart_type": "bar", "y": "v"}, []), "—")


if __name__ == "__main__":
    unittest.main()
