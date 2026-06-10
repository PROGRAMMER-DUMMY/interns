from __future__ import annotations

import unittest
from unittest.mock import patch


def _walk(node):
    """Yield every component in a Dash layout tree."""
    yield node
    children = getattr(node, "children", None)
    if isinstance(children, (list, tuple)):
        for c in children:
            yield from _walk(c)
    elif children is not None and hasattr(children, "children"):
        yield from _walk(children)


def _classes(node):
    return str(getattr(node, "className", "") or "")


class NestedKpiOverviewTests(unittest.TestCase):
    """2d: an optional `group` on a KPI organizes the overview into sections with
    inline separators, while the strip stays one fit-to-viewport row. Verified by
    mocking 4 KPIs across 2 groups (the sample workspace has no nested KPIs)."""

    def test_grouped_overview_inserts_separators_and_is_lazy(self):
        try:
            import plotly  # noqa: F401
            from core.dashboard.spec import DashboardSpec
            import core.dashboard.renderer as rnd
        except Exception:
            self.skipTest("dashboard extra (plotly/dash) not installed")

        defs = {
            "kpi_001": {"group": "Revenue"},
            "kpi_002": {"group": "Revenue"},
            "kpi_003": {"group": "Volume"},
            "kpi_004": {"group": "Volume"},
        }

        def _spec(kpi_id):
            cfg = {
                "sql_path": f"x/{kpi_id}.sql",
                "chart_type": "bar", "x": "region", "y": "amount",
                "panels": [{"chart_type": "bar", "x": "region", "y": "amount", "title": "By Region"}],
                "definition": {"metric": "sum(amount)", "group": defs[kpi_id]["group"]},
                "title": f"KPI {kpi_id}",
            }
            return DashboardSpec(kpi_id=kpi_id, config=cfg, machine_defaults=cfg,
                                 user_overrides={}, spec_path=f"dashboard/{kpi_id}.json")

        rows = [{"region": "n", "amount": 10}, {"region": "s", "amount": 20}]
        build_calls = {"figs": 0}
        real_fig = rnd._figure_from_spec

        def _counting_fig(*a, **k):
            build_calls["figs"] += 1
            return real_fig(*a, **k)

        with patch.object(rnd, "load_kpi_definitions", return_value=defs), \
             patch.object(rnd, "compute_workflow_diff", return_value={"kpi_gaps": []}), \
             patch.object(rnd, "load_kpi_spec", side_effect=lambda layout, kid: _spec(kid)), \
             patch.object(rnd, "_execute_sql_view", return_value=rows), \
             patch.object(rnd, "_figure_from_spec", side_effect=_counting_fig):
            app = rnd.build_dash_app(".", "workspaces/demo")
            nodes = list(_walk(app.layout))

        gseps = [n for n in nodes if _classes(n) == "gsep"]
        tiles = [n for n in nodes if "tile" in _classes(n) and getattr(n, "id", None)]
        # two groups -> two separators; four clickable tiles
        self.assertEqual(len(gseps), 2)
        self.assertEqual(len(tiles), 4)
        # LAZY (2e): no panel figures built at construction time (only on drill).
        self.assertEqual(build_calls["figs"], 0)


if __name__ == "__main__":
    unittest.main()
