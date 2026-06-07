import unittest

from core.onboarding.kpi.engine_recommender import ComplexitySignals, recommend

_MB = 1024 * 1024
_GB = 1024 * _MB


def _signals(**kw) -> ComplexitySignals:
    base = dict(
        feature_count=3, join_depth=0, dim_count=1,
        has_window=False, has_ratio=False, unsupported_window="", estimated_bytes=10 * _MB,
    )
    base.update(kw)
    return ComplexitySignals(**base)


class EngineRecommenderTests(unittest.TestCase):
    def test_small_simple_defaults_to_sql(self):
        rec = recommend("kpi_001", _signals(estimated_bytes=5 * _MB))
        self.assertEqual(rec.recommended_engine, "sql")
        self.assertEqual(rec.default_engine, "sql")

    def test_medium_single_pass_recommends_polars(self):
        rec = recommend("kpi_001", _signals(estimated_bytes=2 * _GB, join_depth=0, dim_count=1))
        self.assertEqual(rec.recommended_engine, "polars")

    def test_medium_multi_join_recommends_hybrid(self):
        rec = recommend("kpi_001", _signals(estimated_bytes=2 * _GB, join_depth=2))
        self.assertEqual(rec.recommended_engine, "hybrid")

    def test_large_recommends_pyspark(self):
        rec = recommend("kpi_001", _signals(estimated_bytes=50 * _GB))
        self.assertEqual(rec.recommended_engine, "pyspark")

    def test_local_blocked_forces_pyspark(self):
        rec = recommend("kpi_001", _signals(estimated_bytes=5 * _MB), local_blocked=True)
        self.assertEqual(rec.recommended_engine, "pyspark")

    def test_unsupported_window_pins_sql_even_when_large(self):
        rec = recommend("kpi_001", _signals(estimated_bytes=50 * _GB, unsupported_window="running_total"))
        self.assertEqual(rec.recommended_engine, "sql")
        self.assertTrue(any("running_total" in r for r in rec.reasons))

    def test_recommendation_is_explained(self):
        rec = recommend("kpi_001", _signals(estimated_bytes=2 * _GB))
        self.assertTrue(rec.reasons)  # never silent


if __name__ == "__main__":
    unittest.main()
