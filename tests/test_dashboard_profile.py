from __future__ import annotations

import unittest

from core.dashboard.profile import (
    choose_measure,
    decide_panels,
    profile_columns,
)


def _multidim_rows():
    """A kpi_002-shaped result: 4 dimensions + a share measure + a constant col."""
    rows = []
    depts = ["Cardiology", "Neurology", "Surgery"]
    for d in depts:
        for vt in ["Routine", "Emergency"]:
            for g in ["M", "F"]:
                rows.append(
                    {
                        "name": d, "visittype": vt, "gender": g,
                        "lineofbusiness": "Commercial",  # constant (filter-pinned)
                        "percentage_share": 1.5,
                    }
                )
    return rows


class ProfileTests(unittest.TestCase):
    def test_profiles_detect_numeric_temporal_and_constant(self):
        rows = [
            {"month": "2024-01-01", "region": "n", "sum_amount": 10},
            {"month": "2024-02-01", "region": "n", "sum_amount": 20},
        ]
        profiles = profile_columns(rows)
        self.assertTrue(profiles["sum_amount"].numeric)
        self.assertTrue(profiles["month"].temporal)
        self.assertTrue(profiles["region"].constant)  # only one distinct value
        self.assertFalse(profiles["sum_amount"].temporal)

    def test_choose_measure_prefers_share_then_measure_name(self):
        rows = [{"region": "n", "row_count": 5, "percentage_share": 0.2}]
        self.assertEqual(choose_measure(profile_columns(rows), rows), "percentage_share")
        rows2 = [{"region": "n", "id": 1, "sum_amount": 5}]
        self.assertEqual(choose_measure(profile_columns(rows2), rows2), "sum_amount")


class DecidePanelsTests(unittest.TestCase):
    def test_multidim_kpi_emits_one_panel_per_dimension_excluding_constant(self):
        panels = decide_panels(_multidim_rows(), {"metric": "percentage share"})
        xs = {p.get("x") for p in panels}
        # one panel per real dimension; the constant lineofbusiness is excluded
        self.assertIn("name", xs)
        self.assertIn("visittype", xs)
        self.assertIn("gender", xs)
        self.assertNotIn("lineofbusiness", xs)
        # share metric -> percent formatting on each breakdown
        self.assertTrue(all(p.get("y_format") == "percent" for p in panels))

    def test_temporal_dimension_yields_a_line_panel(self):
        rows = [
            {"month": "2024-01-01", "region": "n", "sum_amount": 10},
            {"month": "2024-02-01", "region": "s", "sum_amount": 20},
        ]
        panels = decide_panels(rows, {"metric": "sum(amount)"})
        self.assertTrue(any(p.get("chart_type") == "line" for p in panels))

    def test_high_cardinality_dimension_becomes_ranked_bar(self):
        rows = [{"payor": f"P{i}", "sum_amount": i} for i in range(40)]
        panels = decide_panels(rows, {"metric": "sum(amount)"})
        self.assertTrue(any(p.get("chart_type") == "ranked_bar" for p in panels))

    def test_no_dimensions_yields_big_number(self):
        rows = [{"sum_amount": 1234}]
        panels = decide_panels(rows, {"metric": "sum(amount)"})
        self.assertEqual(len(panels), 1)
        self.assertEqual(panels[0]["chart_type"], "big_number")

    def test_empty_rows_returns_empty(self):
        self.assertEqual(decide_panels([], {"metric": "x"}), [])

    def test_log_scale_never_chosen_for_bar_panels(self):
        # Categorical breakdowns render as bar/ranked_bar, and a log axis on a
        # BAR makes bar length non-proportional to value (the kpi_001 payor
        # chart incident) — so even an orders-of-magnitude spread must not
        # set log_scale on a bar panel.
        rows = [
            {"region": "mega", "sum_amount": 1_000_000},
            {"region": "small1", "sum_amount": 500},
            {"region": "small2", "sum_amount": 800},
            {"region": "small3", "sum_amount": 1200},
        ]
        panels = decide_panels(rows, {"metric": "sum(amount)"})
        region = next(p for p in panels if p.get("x") == "region")
        self.assertIn(region.get("chart_type"), ("bar", "ranked_bar"))
        self.assertFalse(region.get("log_scale"))

    def test_log_scale_not_chosen_for_even_distribution(self):
        rows = [
            {"region": "a", "sum_amount": 100},
            {"region": "b", "sum_amount": 120},
            {"region": "c", "sum_amount": 90},
        ]
        panels = decide_panels(rows, {"metric": "sum(amount)"})
        region = next(p for p in panels if p.get("x") == "region")
        self.assertFalse(region.get("log_scale"))

    def test_log_scale_never_for_share_metric(self):
        rows = [
            {"dept": "a", "percentage_share": 90.0},
            {"dept": "b", "percentage_share": 1.0},
            {"dept": "c", "percentage_share": 0.5},
        ]
        panels = decide_panels(rows, {"metric": "percentage share"})
        for p in panels:
            self.assertNotIn("log_scale", p)  # share is bounded 0-100, never log

    def test_panel_count_capped(self):
        rows = [
            {"a": i % 3, "b": i % 4, "c": i % 5, "d": i % 6, "e": i % 7, "v": i}
            for i in range(60)
        ]
        panels = decide_panels(rows, {"metric": "v"})
        self.assertLessEqual(len(panels), 4)


if __name__ == "__main__":
    unittest.main()
