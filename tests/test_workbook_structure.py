"""Tests for the structural workbook reader (merged-cell nesting signal)."""
import tempfile
import unittest
from pathlib import Path

from core.onboarding.kpi.kpi_format_detector import detect_kpi_format
from core.onboarding.kpi.workbook_structure import read_merged_spans, read_workbook_grid


def _write_xlsx(path: Path, rows: list[list[str]], merges: list[str]) -> None:
    from openpyxl import Workbook

    wb = Workbook()
    ws = wb.active
    for r in rows:
        ws.append(r)
    for rng in merges:
        ws.merge_cells(rng)
    wb.save(path)


class WorkbookStructureTests(unittest.TestCase):
    def setUp(self):
        try:
            import openpyxl  # noqa: F401
        except ImportError:
            self.skipTest("openpyxl is not installed")

    def test_reads_grid_columns_and_rows(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "kpis.xlsx"
            _write_xlsx(
                path,
                [
                    ["Key Business Question", "Metric", "Cuts"],
                    ["Share of lives", "count(distinct PatientId)", "Department"],
                    ["", "", "Gender"],
                ],
                merges=["A2:A3"],  # key-column label spans the two data rows
            )
            grid = read_workbook_grid(path)
            self.assertEqual(grid.columns, ["Key Business Question", "Metric", "Cuts"])
            self.assertEqual(len(grid.rows), 2)
            self.assertEqual(grid.rows[0]["Cuts"], "Department")

    def test_merged_key_span_is_reported_in_data_row_coordinates(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "kpis.xlsx"
            _write_xlsx(
                path,
                [
                    ["Key Business Question", "Metric", "Cuts"],
                    ["Share of lives", "count(distinct PatientId)", "Department"],
                    ["", "", "Gender"],
                ],
                merges=["A2:A3"],
            )
            spans = read_merged_spans(path)
            # A2:A3 -> column 0, data rows 0..1 (header row excluded).
            self.assertIn((0, 0, 1), spans)

    def test_grid_feeds_detector_and_flags_nesting(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "kpis.xlsx"
            _write_xlsx(
                path,
                [
                    ["Key Business Question", "Metric", "Cuts"],
                    ["Share of lives", "count(distinct PatientId)", "Department"],
                    ["", "", "Gender"],
                ],
                merges=["A2:A3"],
            )
            grid = read_workbook_grid(path)
            det = detect_kpi_format(
                grid.columns, grid.rows, source=str(path), merged_spans=grid.merged_spans,
            )
            self.assertTrue(det.nesting_suspected)
            self.assertEqual(det.role_header("metric"), "Metric")

    def test_missing_engine_degrades_to_empty_spans(self):
        # A non-xlsx path must not raise; it returns no merge info.
        with tempfile.TemporaryDirectory() as tmp:
            bogus = Path(tmp) / "not_a_workbook.xlsx"
            bogus.write_text("not really xlsx", encoding="utf-8")
            self.assertEqual(read_merged_spans(bogus), [])


if __name__ == "__main__":
    unittest.main()
