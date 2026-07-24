"""Regressions for Q1 (lingering-issues plan): deprecated next-command string,
percent-scaling headline bug, and non-SQL-dialect KPIs no longer rendering as
"blocked". See ~/.claude/plans/dynamic-cooking-firefly.md Q1.
"""
from __future__ import annotations

import unittest
from unittest.mock import patch


class OnboardingNextCommandTests(unittest.TestCase):
    """onboarding.py must never hand out the deprecated resolve-kpi-features
    command -- prepare-kpi-blocker-panel is the current entrypoint."""

    def test_default_branch_recommends_prepare_kpi_blocker_panel(self):
        from core.onboarding.workspace.onboarding import (
            KpiDefinition,
            WorkspaceInputs,
            _onboarding_next_command,
        )

        inputs = WorkspaceInputs(workspace="workspaces/demo")
        kpis = [KpiDefinition(name="Total revenue", metric="sum(amount)", cuts="region")]
        cmd = _onboarding_next_command(inputs, kpis, profiles=[{"path": "x.csv"}])
        self.assertIn("prepare-kpi-blocker-panel", cmd)
        self.assertNotIn("resolve-kpi-features", cmd)

    def test_no_kpi_branch_is_unaffected(self):
        from core.onboarding.workspace.onboarding import WorkspaceInputs, _onboarding_next_command

        inputs = WorkspaceInputs(workspace="workspaces/demo")
        cmd = _onboarding_next_command(inputs, kpis=[], profiles=[{"path": "x.csv"}])
        self.assertIn("build-source-family-contracts", cmd)


class FormatMeasurePercentTests(unittest.TestCase):
    """A declared percent measure (y_format="percent") must be trusted as
    already-in-percent-units, never guessed from magnitude -- a genuine <1%
    share was previously misread as a 0-1 fraction and scaled to >1%."""

    def test_sub_one_percent_share_is_not_rescaled(self):
        from core.dashboard.renderer import _format_measure

        self.assertEqual(_format_measure(0.8, "conversion_rate", percent=True), "0.8%")

    def test_above_one_percent_share_still_renders_correctly(self):
        from core.dashboard.renderer import _format_measure

        self.assertEqual(_format_measure(45.2, "conversion_rate", percent=True), "45.2%")

    def test_name_only_inference_unchanged_when_percent_not_declared(self):
        # Weaker, magnitude-based heuristic is untouched outside the declared-percent path.
        from core.dashboard.renderer import _format_measure

        self.assertEqual(_format_measure(0.5, "share_pct", percent=False), "50.0%")


class NonSqlDialectTileTests(unittest.TestCase):
    """A Polars/PySpark-dialect KPI (no sql_path -> the live DuckDB renderer can't
    execute it) must render its dialect card, not fall into the generic
    "blocked / no executable SQL" bucket."""

    def test_non_sql_kpi_gets_dialect_tile_not_blocked_tile(self):
        try:
            import plotly  # noqa: F401
            from core.dashboard.spec import DashboardSpec
            import core.dashboard.renderer as rnd
        except Exception:
            self.skipTest("dashboard extra (plotly/dash) not installed")

        defs = {"kpi_001": {}, "kpi_002": {}}

        def _spec(kpi_id):
            if kpi_id == "kpi_001":
                cfg = {
                    "sql_path": "x/kpi_001.sql", "chart_type": "bar", "x": "region", "y": "amount",
                    "definition": {"metric": "sum(amount)"}, "title": "KPI 001",
                }
            else:
                # No sql_path: this KPI's only artifact is a Polars script.
                cfg = {"title": "KPI 002 (Polars)"}
            return DashboardSpec(
                kpi_id=kpi_id, config=cfg, machine_defaults=cfg,
                user_overrides={}, spec_path=f"dashboard/{kpi_id}.json",
            )

        def _dialect(repo_root, layout, kpi_id):
            return "polars" if kpi_id == "kpi_002" else "sql"

        rows = [{"region": "n", "amount": 10}]
        with patch.object(rnd, "load_kpi_definitions", return_value=defs), \
             patch.object(rnd, "compute_workflow_diff", return_value={"kpi_gaps": []}), \
             patch.object(rnd, "load_kpi_spec", side_effect=lambda layout, kid: _spec(kid)), \
             patch.object(rnd, "_execute_sql_view", return_value=rows), \
             patch.object(rnd, "_detect_artifact_dialect", side_effect=_dialect):
            app = rnd.build_dash_app(".", "workspaces/demo")

        def _walk(node):
            yield node
            children = getattr(node, "children", None)
            if isinstance(children, (list, tuple)):
                for c in children:
                    yield from _walk(c)
            elif children is not None and hasattr(children, "children"):
                yield from _walk(children)

        texts = [
            str(getattr(n, "children", ""))
            for n in _walk(app.layout)
            if isinstance(getattr(n, "children", None), str)
        ]
        self.assertNotIn("no executable SQL", texts)
        self.assertTrue(any("polars" in t for t in texts), texts)

        kpi_pick = next(n for n in _walk(app.layout) if getattr(n, "id", None) == "kpi-pick")
        values = {opt["value"] for opt in kpi_pick.options}
        self.assertIn("kpi_002", values)


if __name__ == "__main__":
    unittest.main()
