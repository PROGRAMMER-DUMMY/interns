"""Tests for in-place hierarchical drill-down: pure state reducers (no Dash)
plus the adapter wiring that attaches drill_path to KPI/Analysis charts."""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

_REPO = Path(__file__).resolve().parents[1]
_VENDOR = str(_REPO / "vendor")
if _VENDOR not in sys.path:
    sys.path.insert(0, _VENDOR)

from minus.render.interactions import (  # noqa: E402
    drill_ascend,
    drill_descend,
    drill_state,
)

_WS = _REPO / "workspaces" / "Healthcare-RCM-Data-Platform"


class TestDrillReducers(unittest.TestCase):
    PATH = ["department_name", "visit_type", "gender"]   # 3 levels

    def test_descend_records_trail_and_advances(self):
        d = drill_descend({}, "kpis", "w1", self.PATH, "Oncology")
        st = drill_state(d, "kpis", "w1")
        self.assertEqual(st["level"], 1)
        self.assertEqual(st["trail"], [["department_name", "Oncology"]])

    def test_descend_chains_levels(self):
        d = drill_descend({}, "kpis", "w1", self.PATH, "Oncology")
        d = drill_descend(d, "kpis", "w1", self.PATH, "Follow-up")
        st = drill_state(d, "kpis", "w1")
        self.assertEqual(st["level"], 2)
        self.assertEqual([t[1] for t in st["trail"]], ["Oncology", "Follow-up"])

    def test_descend_stops_at_deepest(self):
        d = drill_descend({}, "kpis", "w1", self.PATH, "Oncology")
        d = drill_descend(d, "kpis", "w1", self.PATH, "Follow-up")
        # at the last level -> None so the caller falls back to cross-filter
        self.assertIsNone(drill_descend(d, "kpis", "w1", self.PATH, "Male"))

    def test_ascend_pops_one_level(self):
        d = drill_descend({}, "kpis", "w1", self.PATH, "Oncology")
        d = drill_descend(d, "kpis", "w1", self.PATH, "Follow-up")
        up = drill_ascend(d, "kpis", "w1")
        st = drill_state(up, "kpis", "w1")
        self.assertEqual(st["level"], 1)
        self.assertEqual(st["trail"], [["department_name", "Oncology"]])

    def test_ascend_at_top_is_noop(self):
        self.assertIsNone(drill_ascend({}, "kpis", "w1"))

    def test_state_isolated_per_widget(self):
        d = drill_descend({}, "kpis", "w1", self.PATH, "Oncology")
        self.assertEqual(drill_state(d, "kpis", "w2")["level"], 0)


@unittest.skipUnless((_WS / "interns/state/medallion/bronze").exists(),
                     "bronze layer not present")
class TestAdapterDrillPaths(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from core.dashboard.minus_adapter import generate, minus_root
        from core.storage.workspace_layout import WorkspaceLayout
        from minus.render.app import AppState
        cls.layout = WorkspaceLayout(project_root=_WS.resolve())
        generate(cls.layout)
        cls.state = AppState(minus_root(cls.layout))

    def _drillable(self, page_id):
        page = next(p for p in self.state.pages if p.id == page_id)
        return [w for w in page.widgets if getattr(w, "drill_path", [])]

    def test_kpi_charts_have_drill_paths(self):
        drillable = self._drillable("kpis")
        self.assertTrue(drillable, "expected drillable KPI charts")
        for w in drillable:
            # the first level matches the chart's own dimension column
            self.assertEqual(w.drill_path[0], w.dimension.split(".")[-1])
            self.assertGreaterEqual(len(w.drill_path), 2)

    def test_drill_advances_dimension_and_filters(self):
        w = self._drillable("kpis")[0]
        table = w.dimension.split(".")[0]
        res0 = self.state.engine.run(w, {})
        label = res0.frame.get_column(res0.dimensions[0]).to_list()[0]
        d = drill_descend({}, "kpis", w.id, list(w.drill_path), label)
        st = drill_state(d, "kpis", w.id)
        eff = w.model_copy(update={"dimension": f"{table}.{w.drill_path[st['level']]}"})
        wf = {f"{table}.{dim}": val for dim, val in st["trail"]}
        res1 = self.state.engine.run(eff, wf)        # must not raise
        self.assertNotEqual(eff.dimension, w.dimension)
        self.assertGreaterEqual(res1.frame.height, 1)


if __name__ == "__main__":
    unittest.main()
