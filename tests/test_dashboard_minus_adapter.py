"""Stage 1+2 tests: the MinusAnalyst adapter (config + clean-data generation).

Real-workspace integration (skipped when bronze absent) + vendored-MinusAnalyst
validation that the generated project loads.
"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

_VENDOR = str((Path(__file__).resolve().parents[1] / "vendor"))
if _VENDOR not in sys.path:
    sys.path.insert(0, _VENDOR)

from core.dashboard.minus_adapter import generate, minus_root  # noqa: E402
from core.storage.workspace_layout import WorkspaceLayout  # noqa: E402

_WS = Path("workspaces/Healthcare-RCM-Data-Platform")


@unittest.skipUnless((_WS / "interns/state/medallion/bronze").exists(),
                     "bronze layer not present")
class TestAdapterReal(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.layout = WorkspaceLayout(project_root=_WS.resolve())
        cls.res = generate(cls.layout)

    def test_generate_certified_and_published(self):
        self.assertTrue(self.res["ok"], self.res)
        self.assertTrue(self.res["published"])
        self.assertTrue(self.res["certified"])

    def test_writes_project_and_data(self):
        root = minus_root(self.layout)
        self.assertTrue((root / "project.yaml").exists())
        self.assertTrue((root / "data" / "conformed.parquet").exists())

    def test_pages_include_kpis_overview_detail(self):
        self.assertIn("kpis", self.res["pages"])      # the workspace's defined KPIs
        self.assertIn("overview", self.res["pages"])  # conformed canvas
        self.assertIn("detail", self.res["pages"])

    def test_has_collection_rate_and_kpi_measures(self):
        # generic RCM measure + one measure per gold KPI
        self.assertIn("collection_rate", self.res["measures"])
        self.assertTrue(any(m.startswith("kpi_") for m in self.res["measures"]))

    def test_vendored_minus_validates_generated_project(self):
        from minus.config.loader import load_dashboards, load_project
        root = minus_root(self.layout)
        proj = load_project(root)
        pages = load_dashboards(root, proj)
        self.assertGreaterEqual(len(proj.tables), 2)   # claims + gold KPI tables
        self.assertGreaterEqual(len(pages), 3)
        page_ids = {p.id for p in pages}
        self.assertIn("kpis", page_ids)

    def test_no_pii_column_in_exported_data(self):
        import polars as pl
        root = minus_root(self.layout)
        cols = pl.read_parquet(root / "data" / "conformed.parquet").columns
        for pii in ("FirstName", "LastName", "SSN", "PhoneNumber", "Address", "DOB"):
            self.assertNotIn(pii, cols)


class TestAdapterNoBronze(unittest.TestCase):
    def test_missing_bronze_returns_not_ok(self):
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            layout = WorkspaceLayout(project_root=Path(tmp))
            res = generate(layout)
            self.assertFalse(res["ok"])


if __name__ == "__main__":
    unittest.main()
