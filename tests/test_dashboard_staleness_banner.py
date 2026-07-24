"""core.dashboard.renderer._staleness_banner: the dashboard's "data as of X"
signal (dbt+Airflow plan section D4) -- a blocked/failed pipeline must be
visibly distinguishable from fresh data, never silently indistinguishable
from "no KPIs built yet".
"""
from __future__ import annotations

import unittest

from core.dashboard.model.layers import GoldSourceStatus
from core.dashboard.renderer import _staleness_banner


def _text(node) -> str:
    # dash.html.Div's first positional child is its text content here.
    return node.children


class StalenessBannerTests(unittest.TestCase):
    def test_local_unavailable_says_no_gold_data_yet(self):
        status = GoldSourceStatus("local_files", "unavailable", True, None)
        node = _staleness_banner(status)
        self.assertIn("staleness-warn", node.className)
        self.assertIn("No gold data yet", _text(node))

    def test_databricks_unavailable_gate_closed_says_not_approved(self):
        status = GoldSourceStatus("exclusive", "unavailable", False, None)
        node = _staleness_banner(status)
        self.assertIn("staleness-warn", node.className)
        self.assertIn("not approved", _text(node))

    def test_databricks_unavailable_gate_open_says_no_marts_yet(self):
        status = GoldSourceStatus("exclusive", "unavailable", True, None)
        node = _staleness_banner(status)
        self.assertIn("staleness-warn", node.className)
        self.assertIn("no dbt-built gold marts", _text(node))

    def test_local_delta_shows_as_of_and_label(self):
        status = GoldSourceStatus("local_files", "local_delta", True, "2026-07-24T02:00:00+00:00")
        node = _staleness_banner(status)
        self.assertNotIn("staleness-warn", node.className)
        text = _text(node)
        self.assertIn("2026-07-24T02:00:00+00:00", text)
        self.assertIn("local build", text)

    def test_databricks_dbt_mart_shows_as_of_and_label(self):
        status = GoldSourceStatus("exclusive", "databricks_dbt_mart", True, "2026-07-24T02:09:59Z")
        node = _staleness_banner(status)
        self.assertNotIn("staleness-warn", node.className)
        text = _text(node)
        self.assertIn("2026-07-24T02:09:59Z", text)
        self.assertIn("Databricks dbt mart", text)


if __name__ == "__main__":
    unittest.main()
