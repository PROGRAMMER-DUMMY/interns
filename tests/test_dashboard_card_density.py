"""The screener must catch a KPIs page that overwhelms with too many bare number
cards instead of leading with a few headlines + charts (executive-dashboard
convention / Miller's Law). Mutation-verified: the check fires on the bad shape
and stays silent on the good one."""
from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

import yaml

from core.dashboard.screener import _check_identifier_axes, _check_kpi_card_density


def _write(widgets) -> Path:
    d = Path(tempfile.mkdtemp())
    dd = d / "config" / "dashboards"
    dd.mkdir(parents=True)
    (dd / "kpis.yaml").write_text(yaml.safe_dump({"widgets": widgets}))
    return d


class CardDensityTests(unittest.TestCase):
    def test_flags_too_many_hero_cards(self):
        root = _write([{"type": "kpi", "options": {"emphasis": "hero"}}
                       for _ in range(10)])
        findings = _check_kpi_card_density(root)
        self.assertTrue(any("> 5" in f for f in findings))

    def test_flags_all_numbers_no_charts(self):
        root = _write([{"type": "kpi"} for _ in range(8)])
        findings = _check_kpi_card_density(root)
        self.assertTrue(any("0 charts" in f for f in findings))

    def test_clean_tiered_layout_passes(self):
        root = _write(
            [{"type": "kpi", "options": {"emphasis": "hero"}} for _ in range(4)]
            + [{"type": "bar"}, {"type": "hbar"}, {"type": "donut"}]
        )
        self.assertEqual(_check_kpi_card_density(root), [])

    def test_small_scorecard_passes(self):
        # 3 KPIs, no charts -> fine (small dashboard, under the >=6 threshold).
        root = _write([{"type": "kpi"} for _ in range(3)])
        self.assertEqual(_check_kpi_card_density(root), [])


class IdentifierAxisTests(unittest.TestCase):
    def _ws(self, col_values) -> Path:
        import polars as pl
        d = Path(tempfile.mkdtemp())
        (d / "config" / "dashboards").mkdir(parents=True)
        (d / "data").mkdir()
        (d / "config" / "dashboards" / "kpis.yaml").write_text(
            yaml.safe_dump({"widgets": [
                {"type": "hbar", "dimension": "kpi_x.entity"}]}))
        pl.DataFrame({"entity": col_values, "v": list(range(len(col_values)))}) \
            .write_parquet(d / "data" / "kpi_x.parquet")
        return d

    def test_flags_uuid_axis(self):
        root = self._ws(["1712d26d-822d-1e3a-2267-0a9dba31d7c8",
                         "5e055638-0dad-dfd5-005d-1e74b6fd29ac",
                         "3de74169-7f67-9304-91d4-757e0f"])
        self.assertTrue(_check_identifier_axes(root))

    def test_readable_axis_passes(self):
        root = self._ws(["Medicare", "Medicaid", "Anthem", "Humana"])
        self.assertEqual(_check_identifier_axes(root), [])

    def test_resolves_table_to_file_via_project(self):
        # The conformed fact table is named after the entity but stored in
        # conformed.parquet -- the check must read project.yaml's table->file map,
        # not assume "<table>.parquet", or it silently skips conformed charts.
        import polars as pl
        d = Path(tempfile.mkdtemp())
        (d / "config" / "dashboards").mkdir(parents=True)
        (d / "data").mkdir()
        (d / "project.yaml").write_text(yaml.safe_dump(
            {"tables": [{"name": "encounters", "file": "conformed.parquet"}]}))
        (d / "config" / "dashboards" / "kpis.yaml").write_text(yaml.safe_dump(
            {"widgets": [{"type": "hbar", "dimension": "encounters.Id"}]}))
        pl.DataFrame({"Id": ["dae98068-065e-3b1a-6d1a-f4f61f04f688",
                             "a11cd94b-0bad-9bd3-ae14-ca23c01e77a8",
                             "b22de05c-1cbe-0ce4-bf25-db34d12f88c9"]}) \
            .write_parquet(d / "data" / "conformed.parquet")
        self.assertTrue(_check_identifier_axes(d))   # must find conformed.parquet


if __name__ == "__main__":
    unittest.main()
