"""Chart-selection knowledge base (data-to-viz framework).

Locks the decision rules: every choice names its reason + source, and the
renderer supports every chart type the knowledge can recommend.
"""
from __future__ import annotations

import unittest

from core.dashboard.chart_knowledge import (
    DATA_TO_VIZ,
    choose_categorical_chart,
    choose_trend_chart,
    choose_two_categorical_chart,
    value_spread,
)


class TrendChoiceTest(unittest.TestCase):
    def test_few_series_line(self) -> None:
        choice = choose_trend_chart(series_count=2, is_share=False)
        self.assertEqual(choice.chart_type, "line")

    def test_share_with_series_becomes_stacked_area(self) -> None:
        choice = choose_trend_chart(series_count=3, is_share=True)
        self.assertEqual(choice.chart_type, "stacked_area")

    def test_spaghetti_cap_drops_color(self) -> None:
        choice = choose_trend_chart(series_count=12, is_share=False)
        self.assertEqual(choice.chart_type, "line")
        self.assertTrue(choice.modifiers.get("drop_color"))
        self.assertIn("spaghetti", choice.reason)


class CategoricalChoiceTest(unittest.TestCase):
    def test_high_cardinality_ranks(self) -> None:
        choice = choose_categorical_chart(distinct=40, is_share=False, value_spread=0.9)
        self.assertEqual(choice.chart_type, "ranked_bar")
        self.assertEqual(choice.modifiers.get("limit"), 10)

    def test_high_cardinality_similar_heights_lollipop(self) -> None:
        choice = choose_categorical_chart(distinct=40, is_share=False, value_spread=0.1)
        self.assertEqual(choice.chart_type, "lollipop")

    def test_share_few_categories_donut(self) -> None:
        choice = choose_categorical_chart(distinct=3, is_share=True)
        self.assertEqual(choice.chart_type, "donut")

    def test_share_mid_categories_treemap(self) -> None:
        choice = choose_categorical_chart(distinct=10, is_share=True)
        self.assertEqual(choice.chart_type, "treemap")

    def test_mid_cardinality_similar_heights_lollipop(self) -> None:
        # The kpi_003 shape: 10 near-equal entities.
        choice = choose_categorical_chart(distinct=10, is_share=False, value_spread=0.16)
        self.assertEqual(choice.chart_type, "lollipop")

    def test_default_bar(self) -> None:
        choice = choose_categorical_chart(distinct=4, is_share=False, value_spread=0.8)
        self.assertEqual(choice.chart_type, "bar")

    def test_ordinal_categories_always_ordered_bar(self) -> None:
        # Age bands are ordinal: their order is information, so a share split
        # across them must NOT become a donut/treemap.
        choice = choose_categorical_chart(distinct=11, is_share=True, is_ordinal=True)
        self.assertEqual(choice.chart_type, "bar")

    def test_is_ordinal_detection(self) -> None:
        from core.dashboard.chart_knowledge import is_ordinal_categories

        self.assertTrue(is_ordinal_categories(["0-9", "10-19", "90+"]))
        self.assertFalse(is_ordinal_categories(["Cardiology", "Oncology"]))

    def test_every_choice_carries_reason_and_source(self) -> None:
        for kwargs in (
            {"distinct": 40, "is_share": False, "value_spread": 0.9},
            {"distinct": 3, "is_share": True},
            {"distinct": 10, "is_share": True},
            {"distinct": 4, "is_share": False},
        ):
            choice = choose_categorical_chart(**kwargs)
            self.assertTrue(choice.reason)
            self.assertEqual(choice.source, DATA_TO_VIZ)
            fields = choice.spec_fields()
            self.assertIn("selection_reason", fields)
            self.assertIn("selection_source", fields)


class TwoCategoricalChoiceTest(unittest.TestCase):
    def test_heatmap_for_readable_pair(self) -> None:
        choice = choose_two_categorical_chart(distinct_a=10, distinct_b=4)
        assert choice is not None
        self.assertEqual(choice.chart_type, "heatmap")

    def test_too_wide_pair_declined(self) -> None:
        self.assertIsNone(choose_two_categorical_chart(distinct_a=200, distinct_b=4))
        self.assertIsNone(choose_two_categorical_chart(distinct_a=10, distinct_b=1))


class ValueSpreadTest(unittest.TestCase):
    def test_spread(self) -> None:
        self.assertAlmostEqual(value_spread([100.0, 80.0]), 0.2)
        self.assertIsNone(value_spread([5.0]))
        self.assertIsNone(value_spread([-1.0, -2.0]))


class RendererCoverageTest(unittest.TestCase):
    """Every chart type the knowledge can recommend must render."""

    def _fig(self, config, rows):
        from core.dashboard.renderer import _figure_from_spec
        from core.dashboard.spec import DashboardSpec

        spec = DashboardSpec(
            kpi_id="kpi_x", config=config, machine_defaults={},
            user_overrides={}, spec_path="x.json",
        )
        return _figure_from_spec(spec, rows)

    def test_lollipop_renders_markers(self) -> None:
        rows = [{"payor": f"P{i}", "v": 1500 + i * 20} for i in range(10)]
        fig = self._fig({"chart_type": "lollipop", "x": "payor", "y": "v", "title": "T"}, rows)
        modes = {trace.mode for trace in fig.data}
        self.assertIn("markers", modes)
        self.assertEqual(fig.layout.yaxis.type, "category")

    def test_treemap_renders(self) -> None:
        rows = [{"dept": f"D{i}", "share": 5 + i} for i in range(10)]
        fig = self._fig({"chart_type": "treemap", "x": "dept", "y": "share", "title": "T"}, rows)
        self.assertEqual(fig.data[0].type, "treemap")

    def test_heatmap_renders_grid(self) -> None:
        rows = [
            {"band": b, "gender": g, "share": i + 1}
            for i, (b, g) in enumerate(
                (b, g) for b in ("0-9", "10-19") for g in ("F", "M")
            )
        ]
        fig = self._fig(
            {"chart_type": "heatmap", "x": "band", "color": "gender", "y": "share", "title": "T"},
            rows,
        )
        self.assertEqual(fig.data[0].type, "heatmap")
        self.assertEqual(len(fig.data[0].z), 2)

    def test_stacked_area_renders(self) -> None:
        rows = [
            {"month": m, "seg": s, "share": 10}
            for m in ("2024-01", "2024-02") for s in ("a", "b")
        ]
        fig = self._fig(
            {"chart_type": "stacked_area", "x": "month", "color": "seg", "y": "share", "title": "T"},
            rows,
        )
        self.assertGreaterEqual(len(fig.data), 2)


class DecidePanelsIntegrationTest(unittest.TestCase):
    def test_panels_carry_selection_provenance(self) -> None:
        from core.dashboard.profile import decide_panels

        rows = [
            {"region": f"r{i % 4}", "seg": f"s{i % 3}", "sum_amount": 100 + i}
            for i in range(24)
        ]
        panels = decide_panels(rows, {"metric": "sum(amount)"})
        self.assertTrue(panels)
        for panel in panels:
            if panel.get("chart_type") == "big_number":
                continue
            self.assertIn("selection_reason", panel)
            self.assertIn("selection_source", panel)

    def test_two_categoricals_emit_heatmap_panel(self) -> None:
        from core.dashboard.profile import decide_panels

        rows = [
            {"region": f"r{i % 4}", "seg": f"s{i % 3}", "sum_amount": (100 * (i % 4)) + i}
            for i in range(24)
        ]
        panels = decide_panels(rows, {"metric": "sum(amount)"})
        types = [p.get("chart_type") for p in panels]
        self.assertIn("heatmap", types)


if __name__ == "__main__":
    unittest.main()
