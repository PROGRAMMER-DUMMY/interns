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


class RealSchemaFrontmatterTests(unittest.TestCase):
    """Consume the actual awesome-design-md / Stitch schema: YAML frontmatter with a
    `colors:` map in the brand's own vocabulary, mapped onto our tokens."""

    def test_maps_brand_vocabulary_to_our_tokens(self):
        md = (
            "---\n"
            "colors:\n"
            '  primary: "#533afd"\n'
            '  primary-deep: "#4434d4"\n'
            '  ink: "#0d253d"\n'
            '  ink-secondary: "#273951"\n'
            '  canvas: "#ffffff"\n'
            '  canvas-soft: "#f6f9fc"\n'
            '  hairline: "#e3e8ee"\n'
            "typography:\n"
            "  body-md:\n"
            '    fontFamily: "sohne-var, system-ui, sans-serif"\n'
            "---\n\n## Overview\nprose...\n"
        )
        t = parse_design_md(md)
        self.assertEqual(t.accent, "#533afd")       # primary -> accent
        self.assertEqual(t.accent_deep, "#4434d4")  # primary-deep -> accent_deep
        self.assertEqual(t.paper, "#ffffff")        # canvas -> paper
        self.assertEqual(t.card, "#f6f9fc")         # canvas-soft -> card
        self.assertEqual(t.ink, "#0d253d")
        self.assertEqual(t.ink_soft, "#273951")     # ink-secondary -> ink_soft
        self.assertEqual(t.rule, "#e3e8ee")         # hairline -> rule
        self.assertIn("sohne-var", t.sans)          # typography fontFamily -> sans
        self.assertEqual(t.font_families, ())       # proprietary -> don't Google-load

    def test_card_falls_back_to_paper_when_no_surface(self):
        md = '---\ncolors:\n  primary: "#111111"\n  background: "#fafafa"\n---\n'
        t = parse_design_md(md)
        self.assertEqual(t.paper, "#fafafa")
        self.assertEqual(t.card, "#fafafa")  # no distinct surface -> use page bg

    def test_non_frontmatter_falls_back_to_loose_parser(self):
        # our own token-block format still works (not YAML frontmatter)
        t = parse_design_md("```design-tokens\naccent: #abcdef\n```\n")
        self.assertEqual(t.accent, "#abcdef")


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
        # >8 categories so the bar stays single-series accent (low-cardinality
        # bars now color per category from the categorical ramp instead).
        rows = [{"region": f"r{i}", "v": 10 + i} for i in range(12)]
        try:
            set_active_design(parse_design_md("accent: #ff00ff\n"))
            fig = _figure_from_spec(spec, rows)
            self.assertEqual(str(fig.data[0].marker.color).lower(), "#ff00ff")
        finally:
            set_active_design(DesignTokens())  # restore default for other tests


class DashIndexStringTests(unittest.TestCase):
    def test_index_string_is_token_driven_and_has_layout_regions(self):
        try:
            from core.dashboard.renderer import _dash_index_string
        except Exception:
            self.skipTest("dashboard extra (plotly/dash) not installed")
        html = _dash_index_string(parse_design_md("accent: #abcdef\n"))
        # token wired into the CSS shell
        self.assertIn("--accent:#abcdef", html)
        # fit-to-viewport regions present: KPI rail, scroll region (Explore card
        # + value panels), panel grid.
        for cls in (".strip", ".tile", ".scroll", ".explore", ".dgrid", "height:100vh"):
            self.assertIn(cls, html)
        # required Dash template placeholders preserved
        for ph in ("{%app_entry%}", "{%config%}", "{%scripts%}", "{%renderer%}"):
            self.assertIn(ph, html)


if __name__ == "__main__":
    unittest.main()
