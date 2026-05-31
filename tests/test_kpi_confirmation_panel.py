"""Tests for the KPI format confirmation panel renderer."""
import unittest

from core.onboarding.kpi.kpi_confirmation_panel import (
    build_kpi_confirmation_panel,
    render_kpi_confirmation_markdown,
)
from core.onboarding.kpi.kpi_format_detector import detect_kpi_format


def _rows(columns, *records):
    return [dict(zip(columns, rec)) for rec in records]


class ConfirmationPanelTests(unittest.TestCase):
    def _high_conf(self):
        cols = ["Key Business Question", "Metric", "Cuts"]
        rows = _rows(cols, ("What is total revenue by region?", "sum(Amount)", "Region"))
        return detect_kpi_format(cols, rows, source="rev.xlsx"), rows

    def test_high_confidence_recommends_confirm(self):
        det, rows = self._high_conf()
        panel = build_kpi_confirmation_panel(det, rows)
        self.assertEqual(panel["stage"], "kpi_format_confirmation")
        self.assertEqual(panel["status"], "needs_user_answer")
        self.assertEqual(panel["recommended_option_id"], "confirm")
        option_ids = [o["option_id"] for o in panel["options"]]
        self.assertIn("confirm", option_ids)
        self.assertIn("fix_column", option_ids)

    def test_read_back_is_present_in_panel_and_markdown(self):
        det, rows = self._high_conf()
        panel = build_kpi_confirmation_panel(det, rows)
        read_back = panel["summary"]["read_back"]
        self.assertEqual(read_back[0]["fields"]["metric"], "sum(Amount)")
        md = render_kpi_confirmation_markdown(panel)
        self.assertIn("read back", md.lower())
        self.assertIn("sum(Amount)", md)
        self.assertIn("Column mapping", md)

    def test_missing_required_role_blocks_recommendation_and_adds_option(self):
        cols = ["Key Business Question", "Cuts"]
        rows = _rows(cols, ("How many orders?", "Region"))
        det = detect_kpi_format(cols, rows)
        panel = build_kpi_confirmation_panel(det, rows)
        # Not auto-confirmable: a required role is missing.
        self.assertEqual(panel["recommended_option_id"], "")
        self.assertIn("set_missing_role", [o["option_id"] for o in panel["options"]])
        md = render_kpi_confirmation_markdown(panel)
        self.assertIn("Missing required role", md)

    def test_nesting_adds_option_and_tree_section(self):
        cols = ["Key Business Question", "Metric", "Cuts"]
        rows = _rows(
            cols,
            ("Share of lives", "count(distinct PatientId)", "Department"),
            ("", "", "Gender"),
        )
        det = detect_kpi_format(cols, rows)
        panel = build_kpi_confirmation_panel(det, rows)
        self.assertIn("fix_nesting", [o["option_id"] for o in panel["options"]])
        md = render_kpi_confirmation_markdown(panel)
        self.assertIn("Nesting detected", md)

    def test_low_confidence_columns_listed_as_ambiguous(self):
        # Header-less file -> content-inferred -> MED -> surfaced as ambiguous.
        cols = ["Col1", "Col2", "Col3"]
        rows = _rows(cols, ("How many encounters?", "count(Id)", "Department, Gender"))
        det = detect_kpi_format(cols, rows)
        panel = build_kpi_confirmation_panel(det, rows)
        self.assertTrue(panel["summary"]["ambiguous_mappings"])
        self.assertEqual(panel["recommended_option_id"], "")


if __name__ == "__main__":
    unittest.main()
