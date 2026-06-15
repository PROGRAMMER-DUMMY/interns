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
        # generic RCM measures + one measure per gold KPI
        self.assertIn("collection_rate", self.res["measures"])
        self.assertIn("days_in_ar", self.res["measures"])   # Days in A/R (RCM KPI)
        self.assertTrue(any(m.startswith("kpi_") for m in self.res["measures"]))

    def test_days_in_ar_is_avg_with_lower_target(self):
        from minus.config.loader import load_project
        proj = load_project(minus_root(self.layout))
        m = proj.measure("days_in_ar")
        self.assertEqual(m.agg, "avg")
        self.assertEqual(m.target, 30.0)
        self.assertEqual(m.goal, "lower")

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


class TestKpiTargetColor(unittest.TestCase):
    """Vendored MinusAnalyst KPI tile colors the value vs a target (Stage 2a)."""

    def _render(self, scalar, target, goal="higher"):
        import polars as pl
        from minus.config.models import Measure, Project, Widget
        from minus.query.engine import QueryResult
        from minus.render.widgets.render import _kpi
        proj = Project(name="t", measures=[Measure(
            name="collection_rate", label="Collection Rate", kind="expression",
            expression="1", fmt="percent", target=target, goal=goal)])
        w = Widget(id="k", type="kpi", measure="collection_rate")
        res = QueryResult(frame=pl.DataFrame({"collection_rate": [scalar]}), scalar=scalar)
        return _kpi(w, res, proj)

    def test_below_target_is_red(self):
        value_div = self._render(77.6, 96.0)[1]
        self.assertEqual(value_div.style.get("color"), "#C0563F")

    def test_at_or_above_target_is_green(self):
        value_div = self._render(98.0, 96.0)[1]
        self.assertEqual(value_div.style.get("color"), "#3F8C6E")

    def test_lower_is_better_goal(self):
        # denial-rate style: 3% <= 5% target -> good (green)
        value_div = self._render(3.0, 5.0, goal="lower")[1]
        self.assertEqual(value_div.style.get("color"), "#3F8C6E")


@unittest.skipUnless((_WS / "interns/state/medallion/bronze").exists(),
                     "bronze layer not present")
class TestRefresh(unittest.TestCase):
    """Stage 3: DQ-gated refresh + live-reload wiring."""

    def test_refresh_seconds_written_to_project(self):
        from unittest import mock
        layout = WorkspaceLayout(project_root=_WS.resolve())
        res = generate(layout, refresh_seconds=900)
        self.assertEqual(res["refresh_seconds"], 900)
        txt = (minus_root(layout) / "project.yaml").read_text(encoding="utf-8")
        self.assertIn("refresh_seconds: 900", txt)

    def test_dq_failure_keeps_last_good(self):
        from unittest import mock
        layout = WorkspaceLayout(project_root=_WS.resolve())
        generate(layout)  # publish a good snapshot first
        parquet = minus_root(layout) / "data" / "conformed.parquet"
        self.assertTrue(parquet.exists())
        before = parquet.stat().st_mtime_ns
        # now a refresh whose DQ fails must NOT overwrite the last-good data
        with mock.patch("core.dashboard.minus_adapter.certify",
                        return_value={"ok": False, "failed": [{"check": "no_fanout"}]}):
            res = generate(layout)
        self.assertFalse(res["ok"])
        self.assertFalse(res["published"])
        self.assertEqual(parquet.stat().st_mtime_ns, before)  # untouched


@unittest.skipUnless((_WS / "interns/state/medallion/bronze").exists(),
                     "bronze layer not present")
class TestPushdownScan(unittest.TestCase):
    """Stage 4: pushdown queries the parquet file in place (no full RAM load)."""

    @classmethod
    def setUpClass(cls):
        cls.layout = WorkspaceLayout(project_root=_WS.resolve())
        generate(cls.layout)

    def _model_engine(self):
        from minus.config.loader import load_project
        from minus.data.model import SemanticModel
        from minus.query.engine import QueryEngine
        proj = load_project(minus_root(self.layout))
        model = SemanticModel(proj, minus_root(self.layout))
        return proj, model, QueryEngine(proj, model)

    def test_scan_source_is_read_parquet(self):
        _, model, _ = self._model_engine()
        scan = model.scan_source("claims")
        self.assertIsNotNone(scan)
        self.assertTrue(scan.startswith("read_parquet("), scan)

    def test_pushdown_result_matches_direct_aggregation(self):
        import polars as pl
        from minus.config.models import Widget
        proj, model, engine = self._model_engine()
        # measure total_paid_amount by department_name via the engine (pushdown)
        w = Widget(id="t", type="bar", measure="total_paid_amount",
                   dimension="claims.department_name")
        res = engine.run(w)
        got = dict(zip(res.frame.get_column("claims.department_name").to_list(),
                       res.frame.get_column("total_paid_amount").to_list()))
        # ground truth: aggregate the parquet directly
        raw = pl.read_parquet(minus_root(self.layout) / "data" / "conformed.parquet")
        truth = (raw.group_by("department_name")
                 .agg(pl.col("PaidAmount").sum().alias("v")))
        truth_d = dict(zip(truth.get_column("department_name").to_list(),
                           truth.get_column("v").to_list()))
        self.assertEqual(set(got), set(truth_d))
        for k, v in truth_d.items():
            self.assertAlmostEqual(got[k], v, places=2)


class TestAdapterNoBronze(unittest.TestCase):
    def test_missing_bronze_returns_not_ok(self):
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            layout = WorkspaceLayout(project_root=Path(tmp))
            res = generate(layout)
            self.assertFalse(res["ok"])


if __name__ == "__main__":
    unittest.main()
