"""End-to-end self-test for the dashboard screener's DETERMINISTIC checks.

The screener silently broke twice (a missing ``re`` import; a table!=file path
assumption) because its checks had only unit tests on hand-shaped inputs -- no
coverage that builds a REALISTIC emitted dashboard (project.yaml + dashboards +
data parquets) and asserts each check fires on the bad shape and stays silent on
the good one. This module is that inject-and-assert coverage, so the
"screener silently passes everything" class can't ship again.

Each test builds a complete minus-root and exercises one check both ways.
"""
from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

import polars as pl
import yaml

from core.dashboard.screener import (
    _check_identifier_axes,
    _check_kpi_card_density,
    _check_layout_balance,
    _check_measure_semantics,
)


def _root(*, project: dict | None = None, kpis: dict | None = None,
          data: dict[str, pl.DataFrame] | None = None) -> Path:
    """A throwaway minus-root with the files the checks read."""
    d = Path(tempfile.mkdtemp())
    (d / "config" / "dashboards").mkdir(parents=True)
    (d / "data").mkdir()
    if project is not None:
        (d / "project.yaml").write_text(yaml.safe_dump(project))
    if kpis is not None:
        (d / "config" / "dashboards" / "kpis.yaml").write_text(yaml.safe_dump(kpis))
    for name, df in (data or {}).items():
        df.write_parquet(d / "data" / name)
    return d


class ScreenerSelfTest(unittest.TestCase):
    # --- the checks must be importable AND callable (catches the `re`-unimported
    # class: a module-load error would fail every test here) -----------------
    def test_all_checks_run_without_error_on_empty_root(self):
        d = Path(tempfile.mkdtemp())
        for chk in (_check_identifier_axes, _check_kpi_card_density,
                    _check_layout_balance, _check_measure_semantics):
            self.assertEqual(chk(d), [])     # no config -> no findings, no crash

    # --- identifier axis on a CONFORMED-fact chart (table != file) ----------
    def test_identifier_axis_conformed_table_flagged(self):
        root = _root(
            project={"tables": [{"name": "encounters", "file": "conformed.parquet"}]},
            kpis={"widgets": [{"type": "hbar", "dimension": "encounters.Id"}]},
            data={"conformed.parquet": pl.DataFrame({"Id": [
                "dae98068-065e-3b1a-6d1a-f4f61f04f688",
                "a11cd94b-0bad-9bd3-ae14-ca23c01e77a8",
                "b22de05c-1cbe-0ce4-bf25-db34d12f88c9"]})},
        )
        self.assertTrue(_check_identifier_axes(root))

    def test_readable_axis_conformed_table_clean(self):
        root = _root(
            project={"tables": [{"name": "encounters", "file": "conformed.parquet"}]},
            kpis={"widgets": [{"type": "hbar", "dimension": "encounters.COUNTY"}]},
            data={"conformed.parquet": pl.DataFrame({"COUNTY": [
                "Suffolk County", "Essex County", "Norfolk County"]})},
        )
        self.assertEqual(_check_identifier_axes(root), [])

    # --- too many prominent KPI cards ---------------------------------------
    def test_too_many_hero_cards_flagged(self):
        root = _root(kpis={"widgets": [
            {"type": "kpi", "options": {"emphasis": "hero"}} for _ in range(8)]})
        self.assertTrue(any(">" in f for f in _check_kpi_card_density(root)))

    def test_few_heroes_plus_charts_clean(self):
        root = _root(kpis={"widgets": (
            [{"type": "kpi", "options": {"emphasis": "hero"}} for _ in range(4)]
            + [{"type": "bar"}, {"type": "hbar"}])})
        self.assertEqual(_check_kpi_card_density(root), [])

    # --- ragged (non-12) layout row -----------------------------------------
    def test_ragged_row_flagged(self):
        root = _root(kpis={"widgets": [
            {"type": "bar", "width": 6}, {"type": "bar", "width": 3}]})  # row=9/12
        self.assertTrue(_check_layout_balance(root))

    def test_full_rows_clean(self):
        root = _root(kpis={"widgets": [
            {"type": "bar", "width": 6}, {"type": "bar", "width": 6}]})  # row=12
        self.assertEqual(_check_layout_balance(root), [])

    # --- measure semantics (currency on a count) ----------------------------
    def test_currency_on_count_flagged(self):
        root = _root(project={"measures": [
            {"name": "encounter_count", "fmt": "currency", "agg": "sum",
             "field": "encounters.count"}]})
        self.assertTrue(_check_measure_semantics(root))


if __name__ == "__main__":
    unittest.main()
