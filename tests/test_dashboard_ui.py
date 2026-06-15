"""Phase 3 tests: MinusAnalyst UI port wired to the live model."""
from __future__ import annotations

import unittest
from pathlib import Path

import polars as pl

from core.dashboard.model.crossfilter import panel_data
from core.dashboard.model.cuts import KpiModel
from core.dashboard.ui import widgets
from core.dashboard.ui.chart_map import CHART_RENDERERS, is_big_number, map_chart_type
from core.dashboard.ui.format import format_value
from core.dashboard.ui.theme import THEME_DIR, available_themes, build_theme_css
from core.storage.workspace_layout import WorkspaceLayout

_WS = Path("workspaces/Healthcare-RCM-Data-Platform")

# Every chart_type the interns recommendation engine can emit.
_ALL_CHART_TYPES = [
    "bar", "ranked_bar", "grouped_bar", "stacked_bar_percent", "line",
    "stacked_area", "donut", "treemap", "heatmap", "lollipop", "scatter",
    "histogram", "bubble_map", "big_number",
]


def _frame() -> pl.DataFrame:
    return pl.DataFrame({
        "Department": ["Cardio", "Neuro", "Ortho"],
        "sum_paid": [300.0, 150.0, 90.0],
    })


def _model() -> KpiModel:
    return KpiModel(
        kpi_id="kpi_x", title="Test KPI",
        gold_columns=["Department", "sum_paid"],
        measure="sum_paid", cuts=["Department"], kind="sum", additive=True,
        panels=[
            {"chart_type": "ranked_bar", "x": "Department", "y": "sum_paid", "limit": 2,
             "title": "By Dept"},
            {"chart_type": "donut", "x": "Department", "y": "sum_paid", "title": "Share"},
        ],
    )


class TestFormat(unittest.TestCase):
    def test_currency_percent_int(self):
        self.assertEqual(format_value(1_500_000, "currency"), "$1.5M")
        self.assertEqual(format_value(42.5, "percent"), "42.5%")
        self.assertEqual(format_value(1234, "int"), "1,234")
        self.assertEqual(format_value(None, "int"), "-")


class TestChartMapCoverage(unittest.TestCase):
    def test_every_chart_type_maps(self):
        for ct in _ALL_CHART_TYPES:
            if is_big_number(ct):
                self.assertIsNone(map_chart_type(ct))
            else:
                self.assertIsNotNone(map_chart_type(ct), f"{ct} has no renderer")

    def test_unknown_type_falls_back_not_raises(self):
        self.assertIsNotNone(map_chart_type("no_such_chart"))

    def test_registry_renderers_callable(self):
        for ct, (fn, _note) in CHART_RENDERERS.items():
            self.assertTrue(callable(fn), ct)


class TestRenderers(unittest.TestCase):
    def test_each_renderer_builds_a_figure(self):
        from core.dashboard.ui.theme import chart_colors
        colors = chart_colors("claude")
        frame = _frame()
        panel = {"x": "Department", "y": "sum_paid", "title": "t"}
        for ct in _ALL_CHART_TYPES:
            if is_big_number(ct):
                continue
            fn = map_chart_type(ct)
            fig = fn(frame, panel, "sum_paid", colors)
            self.assertTrue(hasattr(fig, "to_dict"), f"{ct} did not return a figure")

    def test_kpi_card_with_delta(self):
        card = widgets.kpi_card("Revenue", 1_000_000, fmt="currency", delta=12.3,
                                delta_label="vs prev quarter")
        self.assertEqual(card.className, "tile kpi kpi-accent")

    def test_conditional_table(self):
        tbl = widgets.data_table(_frame(), measure="sum_paid", fmt="currency",
                                 conditional={"sum_paid": "data_bar"})
        self.assertEqual(tbl.className, "tbl-wrap")


class TestTheme(unittest.TestCase):
    def test_theme_css_files_exist_and_nonempty(self):
        for name in ("base", "claude", "dark"):
            p = THEME_DIR / f"{name}.css"
            self.assertTrue(p.exists(), p)
            self.assertGreater(len(p.read_text(encoding="utf-8")), 100)

    def test_available_themes(self):
        self.assertEqual(sorted(available_themes()), ["claude", "dark"])

    def test_build_theme_css_assembles(self):
        css = build_theme_css("claude")
        self.assertIn("--accent", css)
        self.assertIn(".tile", css)


class TestLayout(unittest.TestCase):
    def test_section_renders_expected_tiles(self):
        from core.dashboard.ui.layout import kpi_section
        model, frame = _model(), _frame()
        section = kpi_section(model, frame, "claude")
        # the section holds an H2 + a .grid div; the grid has headline + 2 panel tiles
        grid = section.children[1]
        self.assertEqual(len(grid.children), 1 + 2)

    def test_panel_data_topn_applies(self):
        model, frame = _model(), _frame()
        df = panel_data(model, frame, model.panels[0])  # limit=2
        self.assertEqual(df.height, 2)


class TestAppBuildsRealWorkspace(unittest.TestCase):
    def test_build_live_dashboard(self):
        if not (_WS / "interns/state/medallion/gold").exists():
            self.skipTest("gold layer not present")
        from core.dashboard.ui.app import build_live_dashboard
        layout = WorkspaceLayout(project_root=_WS.resolve())
        app = build_live_dashboard(layout)
        self.assertIsNotNone(app.layout)
        self.assertIn("dashboard", app.title)


if __name__ == "__main__":
    unittest.main()
