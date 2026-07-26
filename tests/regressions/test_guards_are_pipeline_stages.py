"""Regression: correctness guards must be pipeline STAGES, not opt-in CLIs.

Origin (2026-07-26 agy-harness audit). This platform's recurring failure mode is a
guard that exists, works, and never runs:

- `validate-kpi-intent-coverage` implements the BUG-005 temporal-anchor check and
  appeared ZERO times in a full 92-stage run -- it was never wired into the DAG.
- the dashboard screener is reachable only via an opt-in `--screen` flag.
- `_fan_trap_risks()` was simply deleted mid-run and nothing noticed.

A guard reachable only when someone remembers to type it is not a guard. These
tests assert the intent check is a real node in both topologies, positioned after
SQL generation and before the dashboard.
"""
from __future__ import annotations

import unittest
from dataclasses import replace

from core.orchestration.pipeline_stages import (
    DBT_BUILD_STAGE,
    STAGES,
    stage_map,
    topological_order,
)

GUARD = "kpi_intent_coverage"


def _cloud_stages():
    """The dbt-path splice, without needing a real workspace on disk."""
    kept = [s for s in STAGES if s.key not in {"medallion_build", "kpi_results"}]
    out = []
    for stage in kept:
        if stage.key == GUARD:
            out.append(DBT_BUILD_STAGE)
            out.append(replace(stage, upstream=("dbt_build",)))
        else:
            out.append(stage)
    return tuple(out)


class GuardIsAStageTests(unittest.TestCase):
    def test_intent_coverage_is_in_the_local_topology(self):
        self.assertIn(GUARD, stage_map(STAGES))

    def test_intent_coverage_is_in_the_cloud_topology(self):
        self.assertIn(GUARD, stage_map(_cloud_stages()))

    def test_it_runs_before_the_dashboard_on_both_paths(self):
        for label, stages in (("local", STAGES), ("cloud", _cloud_stages())):
            order = topological_order(stages)
            with self.subTest(path=label):
                self.assertLess(
                    order.index(GUARD), order.index("dashboard"),
                    f"{label}: the intent guard must gate the dashboard, not trail it",
                )

    def test_it_runs_after_sql_generation_on_both_paths(self):
        # Local: kpi_results generates+executes the views. Cloud: dbt_build does.
        for label, stages, producer in (
            ("local", STAGES, "kpi_results"),
            ("cloud", _cloud_stages(), "dbt_build"),
        ):
            order = topological_order(stages)
            with self.subTest(path=label):
                self.assertLess(
                    order.index(producer), order.index(GUARD),
                    f"{label}: the guard reads generated SQL, so it must run after {producer}",
                )

    def test_both_topologies_remain_acyclic_and_complete(self):
        # topological_order raises on a cycle or an unknown upstream key.
        for label, stages in (("local", STAGES), ("cloud", _cloud_stages())):
            with self.subTest(path=label):
                order = topological_order(stages)
                self.assertEqual(len(order), len(stages))


if __name__ == "__main__":
    unittest.main()
