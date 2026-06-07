"""KPI workbook extraction must be deterministic.

Regression for the 3-vs-42 non-determinism: `openpyxl`'s `wb.active` returns
whichever sheet the file's metadata marks active, which varies across xlsx
editors/saves. The reader now always uses the first sheet in document order
(`worksheets[0]`), so the same workbook yields the same grid every read.
"""
from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from core.onboarding.kpi.workbook_structure import read_workbook_grid


def _two_sheet_workbook(path: Path, *, active_second: bool) -> None:
    from openpyxl import Workbook

    wb = Workbook()
    first = wb.active
    first.title = "KPIs"
    first.append(["KPI", "Metric"])
    first.append(["k1", "sum(x)"])
    second = wb.create_sheet("Other")
    second.append(["OtherCol", "Junk"])
    second.append(["z", "y"])
    if active_second:
        # Simulate an editor that left a different sheet marked active.
        wb.active = wb.worksheets.index(second)
    wb.save(path)


class WorkbookExtractionDeterminismTests(unittest.TestCase):
    def test_reads_first_sheet_regardless_of_active_marker(self):
        try:
            import openpyxl  # noqa: F401
        except ImportError:
            self.skipTest("openpyxl not installed")
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "kpis.xlsx"
            _two_sheet_workbook(path, active_second=True)
            grid = read_workbook_grid(path)
            # Must read the first sheet ("KPIs"), NOT the active-marked second one.
            self.assertEqual(grid.columns, ["KPI", "Metric"])
            self.assertNotIn("OtherCol", grid.columns)

    def test_active_marker_does_not_change_result(self):
        try:
            import openpyxl  # noqa: F401
        except ImportError:
            self.skipTest("openpyxl not installed")
        with tempfile.TemporaryDirectory() as tmp:
            a = Path(tmp) / "active_first.xlsx"
            b = Path(tmp) / "active_second.xlsx"
            _two_sheet_workbook(a, active_second=False)
            _two_sheet_workbook(b, active_second=True)
            grid_a = read_workbook_grid(a)
            grid_b = read_workbook_grid(b)
            # Same content, different active marker -> identical extraction.
            self.assertEqual(grid_a.columns, grid_b.columns)
            self.assertEqual(grid_a.rows, grid_b.rows)

    def test_repeated_reads_are_identical(self):
        try:
            import openpyxl  # noqa: F401
        except ImportError:
            self.skipTest("openpyxl not installed")
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "kpis.xlsx"
            _two_sheet_workbook(path, active_second=True)
            first = read_workbook_grid(path)
            second = read_workbook_grid(path)
            self.assertEqual(first.columns, second.columns)
            self.assertEqual(first.rows, second.rows)


if __name__ == "__main__":
    unittest.main()
