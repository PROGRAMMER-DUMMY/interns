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

    def test_pages_are_kpis_and_analysis(self):
        # two sections: the defined-KPI scorecard + a silver-layer Analysis page
        self.assertIn("kpis", self.res["pages"])
        self.assertIn("analysis", self.res["pages"])
        self.assertNotIn("overview", self.res["pages"])
        self.assertNotIn("detail", self.res["pages"])

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
        self.assertGreaterEqual(len(pages), 2)         # KPIs + Analysis
        page_ids = {p.id for p in pages}
        self.assertIn("kpis", page_ids)
        self.assertIn("analysis", page_ids)

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


class TestResultCache(unittest.TestCase):
    """Phase 1: QueryEngine memoizes results per (data-generation, widget, filters)
    so the live grid's per-interaction re-runs become dict lookups, and a data
    refresh (generation bump) cleanly invalidates them. Self-contained: stubs the
    underlying query so no workspace data is required."""

    def _engine(self):
        from minus.query.engine import QueryEngine, QueryResult
        import polars as pl

        class _FakeModel:
            generation = 0

        engine = QueryEngine(project=None, model=_FakeModel())
        calls = {"n": 0}

        def _fake_run(widget, filters):
            calls["n"] += 1
            return QueryResult(frame=pl.DataFrame({"v": [calls["n"]]}))

        engine._run_uncached = _fake_run
        return engine, calls

    def _widget(self):
        from minus.config.models import Widget
        return Widget(id="w", type="bar", measure="m")

    def test_identical_calls_hit_cache(self):
        engine, calls = self._engine()
        w = self._widget()
        first = engine.run(w)
        second = engine.run(w)
        self.assertEqual(calls["n"], 1)            # underlying query ran once
        self.assertIs(first, second)               # same cached object returned

    def test_generation_bump_invalidates(self):
        engine, calls = self._engine()
        w = self._widget()
        engine.run(w)
        engine.model.generation += 1               # mimics model.clear_cache()
        engine.run(w)
        self.assertEqual(calls["n"], 2)

    def test_distinct_filters_are_separate_entries(self):
        engine, calls = self._engine()
        w = self._widget()
        engine.run(w, {"a": 1})
        engine.run(w, {"a": 2})
        engine.run(w, {"a": 1})                    # repeat of the first -> hit
        self.assertEqual(calls["n"], 2)

    def test_cache_is_lru_bounded(self):
        from minus.query import engine as eng_mod
        engine, _ = self._engine()
        w = self._widget()
        for i in range(eng_mod._RESULT_CACHE_MAX + 50):
            engine.run(w, {"a": i})
        self.assertLessEqual(len(engine._result_cache), eng_mod._RESULT_CACHE_MAX)


@unittest.skipUnless((_WS / "interns/state/medallion/bronze").exists(),
                     "bronze layer not present")
class TestCrossFilterScoping(unittest.TestCase):
    """A click on one KPI's chart must not error the sibling tiles of a
    multi-KPI scorecard, where each card/chart comes from its own unrelated
    gold table. The cross-filter is scoped to widgets that can reach the
    clicked dimension's table; the rest ignore it."""

    @classmethod
    def setUpClass(cls):
        cls.layout = WorkspaceLayout(project_root=_WS.resolve())
        generate(cls.layout)

    def _state(self):
        from minus.render.app import AppState
        return AppState(minus_root(self.layout))

    def test_reachable_false_across_independent_kpi_tables(self):
        state = self._state()
        # independent gold tables -> no join path; self is always reachable
        self.assertTrue(state.model.reachable("kpi_001", "kpi_001"))
        self.assertFalse(state.model.reachable("kpi_002", "kpi_001"))

    def test_unrelated_kpi_filter_is_scoped_out(self):
        state = self._state()
        kpis = next(p for p in state.pages if p.id == "kpis")
        cards = [w for w in kpis.widgets if not w.dimension]
        charts = [w for w in kpis.widgets if w.dimension]
        self.assertTrue(cards and charts)

        dim = charts[0].dimension              # e.g. 'kpi_001.month'
        clicked_table = dim.split(".")[0]
        cf = {dim: "x"}
        for w in cards:
            scoped = state.engine.applicable_filters(w, dict(cf))
            wbase = state.engine._base_table(w, state.engine._measures_for(w))
            if wbase == clicked_table or state.model.reachable(wbase, clicked_table):
                self.assertIn(dim, scoped)      # same KPI: keeps the filter
            else:
                self.assertNotIn(dim, scoped,   # other KPI: drops it (no error)
                                 f"{wbase} must not receive {dim}")


class TestConnectionReuseAndTimeout(unittest.TestCase):
    """Phase 1: the model reuses one DuckDB connection (guarded by a lock), and a
    watchdog interrupts a runaway query so it can't hold that lock forever."""

    def _model(self):
        from minus.data.model import SemanticModel
        return SemanticModel(project=None, root=".")

    def test_duckdb_connection_is_reused(self):
        m = self._model()
        c1 = m.duckdb()
        c2 = m.duckdb()
        self.assertIs(c1, c2)               # same connection, not reconnected
        c1.close()

    def test_duckdb_lock_is_acquirable(self):
        m = self._model()
        self.assertTrue(m.duckdb_lock.acquire(blocking=False))
        m.duckdb_lock.release()

    def test_interrupt_after_fires_on_overrun(self):
        import time
        from minus.data.duckdb_exec import _interrupt_after

        class _Con:
            def __init__(self):
                self.hit = False

            def interrupt(self):
                self.hit = True

        con = _Con()
        with _interrupt_after(con, 0.02):
            time.sleep(0.10)
        self.assertTrue(con.hit)

    def test_interrupt_after_noop_when_fast_or_disabled(self):
        from minus.data.duckdb_exec import _interrupt_after

        class _Con:
            def __init__(self):
                self.hit = False

            def interrupt(self):
                self.hit = True

        # Fast query: timer cancelled before firing.
        con = _Con()
        with _interrupt_after(con, 5):
            pass
        self.assertFalse(con.hit)
        # Disabled: zero timeout never arms the watchdog.
        con2 = _Con()
        with _interrupt_after(con2, 0):
            pass
        self.assertFalse(con2.hit)


class TestAdapterNoBronze(unittest.TestCase):
    def test_missing_bronze_returns_not_ok(self):
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            layout = WorkspaceLayout(project_root=Path(tmp))
            res = generate(layout)
            self.assertFalse(res["ok"])


if __name__ == "__main__":
    unittest.main()
