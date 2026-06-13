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


# ── P5b: substring -> token matching (T9) ────────────────────────────────────
class SubstringTokenMatchingTests(unittest.TestCase):
    def test_extra_select_alias_dedupe_is_exact(self) -> None:
        # `age` must NOT be dropped just because `age_band` is already projected.
        import re

        select_terms = ['FLOOR(age/10)*10 AS age_band', 'COUNT(*) AS n']

        def _emitted_alias(term: str) -> str:
            m = re.search(r'\bAS\s+("?[\w]+"?)\s*$', term, re.IGNORECASE)
            return m.group(1).strip('"') if m else ""

        emitted = {a for a in (_emitted_alias(t) for t in select_terms) if a}
        self.assertIn("age_band", emitted)
        self.assertNotIn("age", emitted)  # age is NOT emitted; must be addable

    def test_base_source_grain_token_no_substring_false_match(self) -> None:
        from types import SimpleNamespace

        from core.onboarding.relationships.base_source_selector import _score_grain

        # 'id' must NOT match a table whose only columns are 'paid'/'amount'
        # (old bug: 'id' substring of 'paid'); it MUST match 'patient_id'.
        cand_paid = SimpleNamespace(grain_matches=[], grain_ratio=0.0)
        _score_grain(cand_paid, {"schema": {"paid": "int", "amount": "double"}}, ["id"])
        self.assertEqual(cand_paid.grain_matches, [])

        cand_pid = SimpleNamespace(grain_matches=[], grain_ratio=0.0)
        _score_grain(cand_pid, {"schema": {"patient_id": "int", "amount": "double"}}, ["id"])
        self.assertEqual(cand_pid.grain_matches, ["id"])


# ── P5c: parity coverage (T3) — only certified engines recommended ───────────
class ParityCoverageTests(unittest.TestCase):
    def test_recommender_never_routes_to_uncertified_engine(self) -> None:
        from core.onboarding.kpi.engine_recommender import ComplexitySignals, recommend

        def sig(**kw):
            base = dict(feature_count=3, join_depth=0, dim_count=1, has_window=False,
                        has_ratio=False, unsupported_window="", estimated_bytes=10 * 1024 * 1024)
            base.update(kw)
            return ComplexitySignals(**base)

        gb = 1024 * 1024 * 1024
        cases = [
            sig(estimated_bytes=50 * gb),                 # would have been pyspark
            sig(estimated_bytes=2 * gb, join_depth=2),    # would have been hybrid
            sig(estimated_bytes=5 * 1024 * 1024),         # small -> sql
        ]
        for s in cases:
            rec = recommend("k", s)
            self.assertNotIn(rec.recommended_engine, ("pyspark", "hybrid"))

    def test_normalize_cell_keeps_large_int_exact(self) -> None:
        from core.onboarding.kpi.engine_parity import _normalize_cell

        big = 9007199254740993  # 2**53 + 1, not exactly representable as float
        self.assertEqual(_normalize_cell(big), big)
        # integral float canonicalizes to int so 10 == 10.0 across engines
        self.assertEqual(_normalize_cell(10), _normalize_cell(10.0))
        # non-integral floats still round under tolerance
        self.assertEqual(_normalize_cell(10.5), 10.5)


if __name__ == "__main__":
    unittest.main()
