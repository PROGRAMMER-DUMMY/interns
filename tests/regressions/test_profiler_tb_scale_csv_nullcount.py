"""Profiler TB-scale fix P2: core/profiling/data_model_profiler.py's
_profile_csv_duckdb computed null_count from a `LIMIT sample_rows`-restricted
temp table by default (exact=False, the production onboarding default) --
even though the DuckDB pushdown aggregate itself is cheap at any file size.
Fixed: null_count is now always computed over the full file, regardless of
`exact`. min/max's existing sample-vs-exact split is deliberately untouched.
See ~/.claude/plans/dynamic-cooking-firefly.md P2.
"""
from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from core.profiling import data_model_profiler as dmp
from core.profiling.data_model_profiler import DataModelProfiler


def _engines_available() -> bool:
    return dmp.duckdb is not None and dmp.pl is not None


def _columns_by_name(profile) -> dict:
    return {col.name: col for col in profile.columns}


@unittest.skipUnless(_engines_available(), "duckdb/polars not installed")
class NullCountIgnoresSampleCutoffTests(unittest.TestCase):
    def _write_csv(self, tmp: Path) -> Path:
        # sample_rows=5 below -- rows 1-5 are clean (no nulls), the null
        # lands on row 6, strictly AFTER the sample cutoff. Small sample_rows
        # keeps this test fast while exercising the exact same code path a
        # real 100,000-row cutoff would (the logic doesn't care about the
        # absolute value, only whether a null falls outside the LIMIT).
        content = "amount\n" + "\n".join(str(float(i)) for i in range(1, 6)) + "\n\n"
        path = tmp / "data.csv"
        path.write_text(content, encoding="utf-8")
        return path

    def test_null_count_is_correct_even_when_null_is_outside_the_sample_window(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = self._write_csv(Path(tmp))
            profiler = DataModelProfiler(pushdown=True)
            profile = profiler.profile_path(path, sample_rows=5, exact=False)

        self.assertIn("duckdb_pushdown", profile.sources_used)
        col = _columns_by_name(profile)["amount"]
        # The one null (row 6) is outside the 5-row sample window. Before the
        # fix, null_count came from _duckdb_column_stats(__ws_profile_sample)
        # -- the 5-row LIMIT table -- and would have reported 0.
        self.assertEqual(col.null_count, 1)

    def test_sample_min_max_still_reflect_the_sample_not_the_full_file(self):
        """min/max semantics must be UNCHANGED by this fix -- sample_min/max
        still come from the sample window, exact_min/max stay None unless
        exact=True is explicitly passed."""
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "data.csv"
            # 5-row sample window sees 1..5; a 6th, larger value sits outside it.
            path.write_text(
                "amount\n" + "\n".join(str(float(i)) for i in range(1, 6)) + "\n999.0\n",
                encoding="utf-8",
            )
            profiler = DataModelProfiler(pushdown=True)
            profile = profiler.profile_path(path, sample_rows=5, exact=False)

        col = _columns_by_name(profile)["amount"]
        self.assertEqual(col.sample_max, 5.0)  # sample window never saw 999.0
        self.assertIsNone(col.exact_min)
        self.assertIsNone(col.exact_max)

    def test_exact_true_still_populates_exact_min_max_as_before(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "data.csv"
            path.write_text(
                "amount\n" + "\n".join(str(float(i)) for i in range(1, 6)) + "\n999.0\n",
                encoding="utf-8",
            )
            profiler = DataModelProfiler(pushdown=True)
            profile = profiler.profile_path(path, sample_rows=5, exact=True)

        col = _columns_by_name(profile)["amount"]
        self.assertEqual(col.exact_max, 999.0)
        self.assertEqual(col.source, "exact_scan")


if __name__ == "__main__":
    unittest.main()
