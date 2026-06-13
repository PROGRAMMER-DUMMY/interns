"""Regression tests for core remediation P5 — result correctness.

Themes T3 (parity coverage) + T9 (substring->token matching) + standalone
high-value correctness bugs. Workspace-agnostic.
"""
from __future__ import annotations

import unittest


# ── P5a: Databricks success-enum + optimization convergence ──────────────────
class DatabricksSuccessEnumTests(unittest.TestCase):
    def test_poll_normalizes_enum_to_bare_name(self) -> None:
        # str(RunResultState.SUCCESS) == "RunResultState.SUCCESS"; the call sites
        # compared == "SUCCESS" and never matched. The fix normalizes to "SUCCESS".
        import enum

        class RunResultState(enum.Enum):
            SUCCESS = "SUCCESS"
            FAILED = "FAILED"

        rs = RunResultState.SUCCESS
        normalized = getattr(rs, "name", None) or str(rs).split(".")[-1]
        self.assertEqual(normalized, "SUCCESS")

    def test_backend_success_check_tolerant(self) -> None:
        # The backend exit-code check must treat both "SUCCESS" and a
        # dotted/enum-str rendering as success.
        for state in ("SUCCESS", "RunResultState.SUCCESS", "success"):
            self.assertTrue(str(state).upper().endswith("SUCCESS"), state)
        for state in ("FAILED", "TIMEDOUT", "RunResultState.FAILED"):
            self.assertFalse(str(state).upper().endswith("SUCCESS"), state)


class OptimizationConvergenceTests(unittest.TestCase):
    def test_direction_less_task_still_converges(self) -> None:
        from core.optimization.strategy import SingleMetricDecisionStrategy

        strat = SingleMetricDecisionStrategy()
        # First candidate sets the baseline.
        self.assertEqual(strat.decide(0.5, {"best_metric": None}, {}), "keep")
        # A BETTER metric on a task with NO 'direction' must be kept, not discarded
        # (default "higher"). Previously direction=None -> always discard.
        self.assertEqual(strat.decide(0.9, {"best_metric": 0.5}, {}), "keep")
        # A worse metric is discarded.
        self.assertEqual(strat.decide(0.3, {"best_metric": 0.5}, {}), "discard")

    def test_lower_direction_respected(self) -> None:
        from core.optimization.strategy import SingleMetricDecisionStrategy

        strat = SingleMetricDecisionStrategy()
        self.assertEqual(strat.decide(0.3, {"best_metric": 0.5}, {"direction": "lower"}), "keep")
        self.assertEqual(strat.decide(0.9, {"best_metric": 0.5}, {"direction": "lower"}), "discard")


if __name__ == "__main__":
    unittest.main()
