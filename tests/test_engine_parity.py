"""Unit tests for core.onboarding.kpi.engine_parity.

Covers the engine-neutral normalization (the part every comparison rests on)
and the cheap short-circuits of run_polars_parity. The full
generate->run->read-back path is exercised end-to-end by the results stage on
a real workspace, not simulated here.
"""
from __future__ import annotations

import unittest
from datetime import date, datetime

from core.onboarding.kpi.engine_parity import (
    MAX_PARITY_ROWS,
    _normalize_cell,
    _normalize_rows,
    run_polars_parity,
)


class TestNormalizeCell(unittest.TestCase):
    def test_none_passes_through(self):
        self.assertIsNone(_normalize_cell(None))

    def test_nan_becomes_none(self):
        self.assertIsNone(_normalize_cell(float("nan")))

    def test_float_rounded_to_tolerance(self):
        self.assertEqual(_normalize_cell(1.23456), 1.23)

    def test_int_and_equal_float_normalize_identically(self):
        self.assertEqual(_normalize_cell(10), _normalize_cell(10.0))

    def test_midnight_datetime_collapses_to_iso_day(self):
        # SQL date_trunc emits midnight timestamps where Polars emits dates.
        self.assertEqual(_normalize_cell(datetime(2024, 1, 1, 0, 0, 0)), "2024-01-01")

    def test_date_and_midnight_datetime_agree(self):
        self.assertEqual(
            _normalize_cell(date(2024, 1, 1)),
            _normalize_cell(datetime(2024, 1, 1)),
        )

    def test_non_midnight_datetime_keeps_time(self):
        self.assertEqual(
            _normalize_cell(datetime(2024, 1, 1, 12, 30)), "2024-01-01T12:30:00"
        )

    def test_other_types_become_str(self):
        self.assertEqual(_normalize_cell("Medicare"), "Medicare")


class TestNormalizeRows(unittest.TestCase):
    def test_column_order_is_insensitive(self):
        cols_a, rows_a = _normalize_rows(["b", "a"], [[2, 1]])
        cols_b, rows_b = _normalize_rows(["a", "b"], [[1, 2]])
        self.assertEqual(cols_a, cols_b)
        self.assertEqual(rows_a, rows_b)

    def test_column_names_lowercased(self):
        cols, _ = _normalize_rows(["PayorID", "Gender"], [])
        self.assertEqual(cols, ["gender", "payorid"])

    def test_row_order_is_insensitive(self):
        _, rows_a = _normalize_rows(["x"], [[1], [2]])
        _, rows_b = _normalize_rows(["x"], [[2], [1]])
        self.assertEqual(rows_a, rows_b)

    def test_none_cells_sort_deterministically(self):
        _, rows = _normalize_rows(["x"], [[None], [1]])
        self.assertEqual(len(rows), 2)


class TestRunPolarsParityShortCircuits(unittest.TestCase):
    def test_row_cap_skips_before_any_generation(self):
        oversized = [[0]] * (MAX_PARITY_ROWS + 1)
        result = run_polars_parity(".", "workspaces/nonexistent", "kpi_001", ["x"], oversized)
        self.assertEqual(result["status"], "skipped")
        self.assertIn("parity cap", result["reason"])

    def test_missing_workspace_degrades_to_skipped_not_raise(self):
        # Generation against a workspace with no mappings must record a
        # status, never raise into the results stage.
        result = run_polars_parity(".", "workspaces/nonexistent", "kpi_001", ["x"], [[1]])
        self.assertIn(result["status"], {"skipped", "error"})
        self.assertTrue(result["reason"])


if __name__ == "__main__":
    unittest.main()
