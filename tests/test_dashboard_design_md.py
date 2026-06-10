from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from core.dashboard.design_md import DesignTokens, load_design_tokens, parse_design_md


class ParseDesignMdTests(unittest.TestCase):
    def test_parses_token_block(self):
        md = (
            "# DESIGN.md\n\n```design-tokens\n"
            "accent: #123456\n"
            "ink: #000011\n"
            "serif: 'Times', serif\n"
            "categorical: #aa0000, #00bb00, #0000cc\n"
            "```\n"
        )
        t = parse_design_md(md)
        self.assertEqual(t.accent, "#123456")
        self.assertEqual(t.ink, "#000011")
        self.assertEqual(t.serif, "'Times', serif")
        self.assertEqual(t.categorical, ("#aa0000", "#00bb00", "#0000cc"))

    def test_missing_tokens_fall_back_to_defaults(self):
        t = parse_design_md("accent: #ff0000\n")  # only accent given
        self.assertEqual(t.accent, "#ff0000")
        self.assertEqual(t.paper, DesignTokens().paper)  # untouched -> default
        self.assertEqual(t.categorical, DesignTokens().categorical)

    def test_garbage_returns_defaults(self):
        t = parse_design_md("this file has no tokens at all\n")
        self.assertEqual(t, DesignTokens())

    def test_parses_loose_list_form(self):
        # tokens as a markdown bullet list, not a fenced block
        md = "## Colors\n- accent: #abcabc\n- paper: #fefefe\n"
        t = parse_design_md(md)
        self.assertEqual(t.accent, "#abcabc")
        self.assertEqual(t.paper, "#fefefe")


class LoadDesignTokensTests(unittest.TestCase):
    def test_workspace_design_md_overrides_default(self):
        with tempfile.TemporaryDirectory() as tmp:
            ws = Path(tmp) / "ws"
            ws.mkdir()
            (ws / "DESIGN.md").write_text(
                "```design-tokens\naccent: #654321\n```\n", encoding="utf-8"
            )
            t = load_design_tokens(ws)
            self.assertEqual(t.accent, "#654321")

    def test_falls_back_to_shipped_default_when_no_workspace_md(self):
        with tempfile.TemporaryDirectory() as tmp:
            # no DESIGN.md in this workspace -> shipped default_design.md (editorial)
            t = load_design_tokens(Path(tmp))
            self.assertEqual(t.accent, DesignTokens().accent)


class AppliesToRendererTests(unittest.TestCase):
    def test_swapping_design_changes_rendered_accent(self):
        try:
            import plotly  # noqa: F401
            from core.dashboard.renderer import _figure_from_spec, set_active_design
            from core.dashboard.spec import DashboardSpec
        except Exception:
            self.skipTest("dashboard extra (plotly) not installed")
        spec = DashboardSpec(
            kpi_id="kpi_001",
            config={"chart_type": "bar", "x": "region", "y": "v"},
            machine_defaults={}, user_overrides={}, spec_path="x.json",
        )
        rows = [{"region": "n", "v": 10}, {"region": "s", "v": 20}]
        try:
            set_active_design(parse_design_md("accent: #ff00ff\n"))
            fig = _figure_from_spec(spec, rows)
            self.assertEqual(str(fig.data[0].marker.color).lower(), "#ff00ff")
        finally:
            set_active_design(DesignTokens())  # restore default for other tests


if __name__ == "__main__":
    unittest.main()
