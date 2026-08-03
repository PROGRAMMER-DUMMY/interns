"""DuckDB pushdown profiling must be artifact-equivalent to the legacy path.

Phase 3 track A: CSV profiling pushes row counts, null counts, min/max, and
observed values down to DuckDB SQL over ``read_csv`` instead of materializing
rows in Python. The profile artifact SCHEMA must stay identical -- downstream
readers (validate-workspace-artifacts, resolvers, dashboards) must not notice
the engine swap -- and output must be byte-identical across runs for identical
inputs (determinism). A malformed file or missing duckdb falls back to the
legacy Polars path.
"""
from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from core.profiling import data_model_profiler as dmp
from core.profiling.data_model_profiler import DataModelProfiler

# <= 8 distinct values per column so legacy (first-8-encountered) and pushdown
# (first-8-sorted) sample_values are the same SET.
CSV_CONTENT = (
    "id,amount,name,flag\n"
    "1,10.5,alpha,true\n"
    "2,,beta,false\n"
    "3,7.25,gamma,true\n"
    ",3.0,delta,false\n"
)


def _engines_available() -> bool:
    return dmp.duckdb is not None and dmp.pl is not None


def _columns_by_name(profile) -> dict:
    return {col.name: col for col in profile.columns}


class PushdownEquivalenceTests(unittest.TestCase):
    def setUp(self) -> None:
        if not _engines_available():
            self.skipTest("duckdb and/or polars not installed")
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.csv = Path(self._tmp.name) / "orders.csv"
        self.csv.write_text(CSV_CONTENT, encoding="utf-8")

    def test_pushdown_profile_matches_legacy_profile(self) -> None:
        pushed = DataModelProfiler().profile_path(self.csv, sample_rows=100)
        legacy = DataModelProfiler(pushdown=False).profile_path(self.csv, sample_rows=100)

        self.assertIn("duckdb_pushdown", pushed.sources_used)
        self.assertNotIn("duckdb_pushdown", legacy.sources_used)
        self.assertEqual(pushed.schema, legacy.schema)
        self.assertEqual(pushed.row_count, legacy.row_count)
        self.assertEqual(pushed.format, legacy.format)
        self.assertEqual(pushed.size_bytes, legacy.size_bytes)
        self.assertEqual(pushed.warnings, legacy.warnings)

        pushed_cols = _columns_by_name(pushed)
        legacy_cols = _columns_by_name(legacy)
        self.assertEqual(set(pushed_cols), set(legacy_cols))
        for name, legacy_col in legacy_cols.items():
            pushed_col = pushed_cols[name]
            self.assertEqual(pushed_col.dtype, legacy_col.dtype, name)
            self.assertEqual(pushed_col.null_count, legacy_col.null_count, name)
            self.assertEqual(pushed_col.sample_min, legacy_col.sample_min, name)
            self.assertEqual(pushed_col.sample_max, legacy_col.sample_max, name)
            self.assertEqual(
                sorted(map(repr, pushed_col.sample_values)),
                sorted(map(repr, legacy_col.sample_values)),
                name,
            )

        # Downcast recommendations derive from the same bounds.
        self.assertEqual(
            [rec.column for rec in pushed.downcast_recommendations],
            [rec.column for rec in legacy.downcast_recommendations],
        )
        for pushed_rec, legacy_rec in zip(
            pushed.downcast_recommendations, legacy.downcast_recommendations
        ):
            self.assertEqual(pushed_rec.decision, legacy_rec.decision)
            self.assertEqual(pushed_rec.recommended_dtype, legacy_rec.recommended_dtype)

    def test_exact_mode_matches_legacy_exact_bounds(self) -> None:
        pushed = DataModelProfiler().profile_path(self.csv, sample_rows=100, exact=True)
        legacy = DataModelProfiler(pushdown=False).profile_path(
            self.csv, sample_rows=100, exact=True
        )
        pushed_cols = _columns_by_name(pushed)
        for name, legacy_col in _columns_by_name(legacy).items():
            self.assertEqual(pushed_cols[name].exact_min, legacy_col.exact_min, name)
            self.assertEqual(pushed_cols[name].exact_max, legacy_col.exact_max, name)
            self.assertEqual(pushed_cols[name].null_count, legacy_col.null_count, name)

    def test_pushdown_output_is_byte_identical_across_runs(self) -> None:
        first = DataModelProfiler().profile_path(self.csv, sample_rows=100).to_json()
        second = DataModelProfiler().profile_path(self.csv, sample_rows=100).to_json()
        self.assertEqual(first, second)

    def test_profile_artifact_schema_keys_are_unchanged(self) -> None:
        summary = DataModelProfiler().profile_path(self.csv, sample_rows=100).summary()
        self.assertEqual(
            set(summary),
            {
                "path",
                "format",
                "row_count",
                "file_count",
                "size_bytes",
                "schema",
                "sources_used",
                "warnings",
                "columns",
                "downcast_recommendations",
            },
        )
        for column in summary["columns"]:
            self.assertEqual(
                set(column),
                {
                    "name",
                    "dtype",
                    "nullable",
                    "sample_values",
                    "sample_min",
                    "sample_max",
                    "exact_min",
                    "exact_max",
                    "metadata_min",
                    "metadata_max",
                    "null_count",
                    "source",
                },
            )


class PushdownFallbackTests(unittest.TestCase):
    def setUp(self) -> None:
        if dmp.pl is None:
            self.skipTest("polars not installed")
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.csv = Path(self._tmp.name) / "orders.csv"
        self.csv.write_text(CSV_CONTENT, encoding="utf-8")

    def test_duckdb_error_falls_back_to_legacy_path(self) -> None:
        if dmp.duckdb is None:
            self.skipTest("duckdb not installed")
        profiler = DataModelProfiler()

        def _boom(*args, **kwargs):
            raise RuntimeError("simulated malformed-file failure")

        profiler._profile_csv_duckdb = _boom  # type: ignore[method-assign]
        profile = profiler.profile_path(self.csv, sample_rows=100)
        self.assertTrue(
            any(w.startswith("duckdb_pushdown_failed:") for w in profile.warnings),
            profile.warnings,
        )
        # Legacy path still produced a full profile.
        self.assertEqual(profile.row_count, 4)
        self.assertIn("sample_profile", profile.sources_used)
        self.assertNotIn("duckdb_pushdown", profile.sources_used)

    def test_missing_duckdb_uses_legacy_path_without_warnings(self) -> None:
        original = dmp.duckdb
        dmp.duckdb = None
        try:
            profile = DataModelProfiler().profile_path(self.csv, sample_rows=100)
        finally:
            dmp.duckdb = original
        self.assertEqual(profile.row_count, 4)
        self.assertNotIn("duckdb_pushdown", profile.sources_used)
        self.assertFalse(
            any("duckdb" in warning for warning in profile.warnings), profile.warnings
        )


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
