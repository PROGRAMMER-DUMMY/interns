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

    def test_lollipop_renders_value_zoomed_dot_plot(self) -> None:
        rows = [{"payor": f"P{i}", "v": 1500 + i * 20} for i in range(10)]
        fig = self._fig({"chart_type": "lollipop", "x": "payor", "y": "v", "title": "T"}, rows)
        modes = {trace.mode for trace in fig.data}
        self.assertIn("markers+text", modes)  # dots carry value labels
        self.assertEqual(fig.layout.yaxis.type, "category")
        # Axis zooms to the data range: near-equal values must stay separable,
        # so the lower bound sits near the min value, NOT at zero.
        lo = fig.layout.xaxis.range[0]
        self.assertGreater(lo, 1000)

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


class NewFamilyDetectionTest(unittest.TestCase):
    """Sensor/geo/raw-shaped data unlocks the corresponding chart families."""

    def test_geo_columns_detected_with_range_evidence(self) -> None:
        from core.dashboard.chart_knowledge import detect_geo_columns

        ok = detect_geo_columns(
            ["lat", "lon", "v"],
            {"lat": [12.9, 51.5], "lon": [77.6, -0.1], "v": [1, 2]},
        )
        self.assertEqual(ok, ("lat", "lon"))
        # name matches but values out of range -> no geo family
        bad = detect_geo_columns(["lat", "lon"], {"lat": [500, 600], "lon": [0, 1]})
        self.assertIsNone(bad)

    def test_geo_rows_emit_bubble_map_panel(self) -> None:
        from core.dashboard.profile import decide_panels

        rows = [
            {"lat": 10.0 + i * 0.1, "lon": 70.0 + i * 0.1, "station": f"s{i}", "reading_value": 5.0 + i}
            for i in range(40)
        ]
        panels = decide_panels(rows, {"metric": "avg(reading)"})
        types = [p.get("chart_type") for p in panels]
        self.assertIn("bubble_map", types)
        bm = next(p for p in panels if p["chart_type"] == "bubble_map")
        self.assertEqual((bm.get("lat"), bm.get("lon")), ("lat", "lon"))

    def test_second_continuous_numeric_emits_scatter(self) -> None:
        from core.dashboard.profile import decide_panels

        rows = [
            {"depth": float(i), "reading_value": 100.0 - i * 0.7, "site": f"x{i % 3}"}
            for i in range(60)
        ]
        panels = decide_panels(rows, {"metric": "avg(reading)"})
        self.assertIn("scatter", [p.get("chart_type") for p in panels])

    def test_row_level_values_emit_histogram(self) -> None:
        from core.dashboard.profile import decide_panels

        rows = [
            {"sensor_id": f"id_{i}", "reading_value": 20.0 + (i % 17) * 0.31}
            for i in range(120)
        ]
        panels = decide_panels(rows, {"metric": "reading"})
        self.assertIn("histogram", [p.get("chart_type") for p in panels])

    def test_renderers_for_new_families(self) -> None:
        from core.dashboard.renderer import _figure_from_spec
        from core.dashboard.spec import DashboardSpec

        def fig(config, rows):
            return _figure_from_spec(
                DashboardSpec(kpi_id="k", config=config, machine_defaults={},
                              user_overrides={}, spec_path="x.json"),
                rows,
            )

        scatter = fig(
            {"chart_type": "scatter", "x": "depth", "y": "v", "title": "T"},
            [{"depth": 1.0, "v": 2.0}, {"depth": 2.0, "v": 1.0}],
        )
        self.assertEqual(scatter.data[0].mode, "markers")
        hist = fig(
            {"chart_type": "histogram", "x": "v", "y": "v", "title": "T"},
            [{"v": float(i % 7)} for i in range(50)],
        )
        self.assertEqual(hist.data[0].type, "histogram")
        gmap = fig(
            {"chart_type": "bubble_map", "lat": "lat", "lon": "lon", "y": "v", "title": "T"},
            [{"lat": 10.0, "lon": 70.0, "v": 3.0}, {"lat": 11.0, "lon": 71.0, "v": 4.0}],
        )
        self.assertEqual(gmap.data[0].type, "scattergeo")


class ScreenerChecksTest(unittest.TestCase):
    def test_render_failure_annotation_detected(self) -> None:
        from core.dashboard.screener import _check_html

        finding = _check_html(
            '<div>(chart render failed: KeyError &#x27;x&#x27;)</div>', "kpi_001.html", 2
        )
        self.assertFalse(finding.ok)

    def test_zero_plots_against_spec_detected(self) -> None:
        from core.dashboard.screener import _check_html

        finding = _check_html("<html><body>no charts</body></html>", "kpi_002.html", 3)
        self.assertFalse(finding.ok)

    def test_clean_page_passes(self) -> None:
        from core.dashboard.screener import _check_html

        finding = _check_html(
            '<div class="js-plotly-plot"></div><script src="https://cdn.plot.ly/x.js">',
            "kpi_003.html",
            1,
        )
        self.assertTrue(finding.ok)

    def test_missing_data_view_on_ready_page_is_an_error(self) -> None:
        from core.dashboard.screener import _check_html

        finding = _check_html(
            '<div class="js-plotly-plot"></div>', "kpi_001.html", 2,
            expects_data_view=True,
        )
        self.assertFalse(finding.ok)
        self.assertTrue(any("Data section" in e for e in finding.errors))

    def test_sensitive_header_without_redaction_is_an_error(self) -> None:
        from core.dashboard.screener import _check_html

        html = (
            '<div class="js-plotly-plot"></div><details class="dataview">'
            "<summary>Data</summary><div>10 of 10 rows</div>"
            "<table><thead><tr><th>ssn</th></tr></thead>"
            "<tbody><tr><td>123-45-6789</td></tr></tbody></table></details>"
        )
        finding = _check_html(
            html, "kpi_001.html", 1,
            expects_data_view=True, redaction_patterns=(r"^ssn$",),
        )
        self.assertFalse(finding.ok)
        self.assertTrue(any("without redaction" in e for e in finding.errors))

    def test_redacted_data_view_passes(self) -> None:
        from core.dashboard.screener import _check_html

        html = (
            '<div class="js-plotly-plot"></div><details class="dataview">'
            "<summary>Data</summary><div>10 of 10 rows</div>"
            "<table><thead><tr><th>ssn</th></tr></thead>"
            "<tbody><tr><td>&lt;redacted-pii&gt;</td></tr></tbody></table></details>"
        )
        finding = _check_html(
            html, "kpi_001.html", 1,
            expects_data_view=True, redaction_patterns=(r"^ssn$",),
        )
        self.assertTrue(finding.ok)

    def test_blank_png_heuristic(self) -> None:
        from core.dashboard.screener import _looks_blank
        import tempfile
        from pathlib import Path

        with tempfile.TemporaryDirectory() as tmp:
            tiny = Path(tmp) / "x.png"
            tiny.write_bytes(b"\x89PNG" + b"\x00" * 512)
            self.assertTrue(_looks_blank(tiny, 1500, 1700))


class DataViewerTest(unittest.TestCase):
    def test_rows_render_redacted_escaped_and_capped(self) -> None:
        import tempfile
        from pathlib import Path

        from core.dashboard.export import _DATA_VIEW_ROW_CAP, _data_view_html

        rows = [
            {"ssn": f"00{i}-11", "dept": f"<b>D{i}</b>", "amount": 10.5 + i}
            for i in range(_DATA_VIEW_ROW_CAP + 50)
        ]
        with tempfile.TemporaryDirectory() as tmp:
            html = _data_view_html(Path(tmp), "kpi_x", rows)
        # ssn values hidden (placeholder is itself HTML-escaped on render)
        self.assertIn("&lt;redacted-pii&gt;", html)
        self.assertNotIn("003-11", html)
        self.assertIn("&lt;b&gt;", html)               # markup escaped, not rendered
        self.assertNotIn("<b>D1</b>", html)
        self.assertEqual(html.count("<tr>"), _DATA_VIEW_ROW_CAP + 1)  # cap + header
        self.assertIn("capped", html)

    def test_workspace_policy_widens_data_view_redaction(self) -> None:
        import json
        import tempfile
        from pathlib import Path

        from core.dashboard.export import _data_view_html

        with tempfile.TemporaryDirectory() as tmp:
            (Path(tmp) / "data_policy.json").write_text(
                json.dumps({"sensitive_columns": ["LoyaltyCode"]}), encoding="utf-8"
            )
            html = _data_view_html(
                Path(tmp), "kpi_x", [{"LoyaltyCode": "LX-9", "amount": 5}]
            )
        self.assertIn("&lt;redacted-pii&gt;", html)
        self.assertNotIn("LX-9", html)

    def test_empty_rows_render_nothing(self) -> None:
        from pathlib import Path

        from core.dashboard.export import _data_view_html

        self.assertEqual(_data_view_html(Path("."), "kpi_x", []), "")


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
