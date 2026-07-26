"""Profiler TB-scale fix P4: tools/profiler.py's PolarsEngine.load_csv called
pl.read_csv (eager) -- fully materializing the file in memory -- even though
every caller immediately called `.lazy()` on the result. Parquet/Delta in the
same module correctly use pl.scan_parquet/pl.scan_delta (true out-of-core).
CSV text typically inflates 2-4x over on-disk size in memory, a real OOM risk
well under the 50GB Spark-routing threshold.

Fixed: load_csv now uses pl.scan_csv (lazy). This required also fixing
fix_inferred_schema, which (on full read) turned out to do real EAGER Series
operations (.get_column(), .null_count(), .str.contains()) that would have
raised outright on a LazyFrame -- the original plan's assumption that "no
change needed there" was wrong; the schema-detection probes (ZIP-pattern
sample match rate, float null ratio) are now batched into one bounded
.select(...).collect() instead of per-column eager access, preserving
identical detection semantics and thresholds without materializing the file.

See ~/.claude/plans/dynamic-cooking-firefly.md P4.
"""
from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

import polars as pl

from tools.profiler import fix_inferred_schema, load_csv, load_polars_frame


def _fixture_csv(tmp: Path) -> Path:
    lines = ["customer_id,zip_int,amount,clean_int"]
    for i in range(20):
        zip_val = 10001 + i  # all 5-digit, ZIP-shaped ints
        amount = "" if i % 2 == 0 else f"{i * 1.5:.2f}"  # 50% null -- over the 30% threshold
        lines.append(f"{i},{zip_val},{amount},{i}")
    path = tmp / "data.csv"
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path


class LoadCsvIsLazyTests(unittest.TestCase):
    def test_load_csv_returns_a_lazyframe_not_a_dataframe(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = _fixture_csv(Path(tmp))
            result = load_csv(str(path))
        self.assertIsInstance(result, pl.LazyFrame)
        self.assertNotIsInstance(result, pl.DataFrame)

    def test_schema_correction_heuristics_still_fire_identically(self):
        """Same semantics as the old eager version: ID-pattern name match,
        ZIP-shaped int sample, and >30% null float all get cast to String;
        an ordinary int column is left alone."""
        with tempfile.TemporaryDirectory() as tmp:
            path = _fixture_csv(Path(tmp))
            lf = load_csv(str(path))
            df = lf.collect()

        self.assertEqual(df.schema["customer_id"], pl.String)  # ID_PATTERN name match
        self.assertEqual(df.schema["zip_int"], pl.String)  # ZIP-pattern sample match
        self.assertEqual(df.schema["amount"], pl.String)  # >30% nulls
        self.assertEqual(df.schema["clean_int"], pl.Int64)  # untouched
        self.assertEqual(df.height, 20)

    def test_fix_inferred_schema_still_accepts_an_eager_dataframe_too(self):
        """fix_inferred_schema is called from other places with an already-
        eager DataFrame (e.g. JSON/Excel loaders in load_polars_frame that
        never went through load_csv) -- must not have become lazy-only."""
        df = pl.DataFrame({"record_id": [1, 2, 3], "note": ["a", "b", "c"]})
        result = fix_inferred_schema(df)
        self.assertIsInstance(result, pl.DataFrame)
        self.assertEqual(result.schema["record_id"], pl.String)  # ID_PATTERN

    def test_load_polars_frame_single_csv_file_is_lazy_with_correct_content(self):
        # Deliberately collect() INSIDE the temp-dir's lifetime: with a truly
        # lazy scan, the file isn't read until collect() -- collecting after
        # the source is gone is expected to fail (this is the same,
        # pre-existing contract Parquet/Delta/JSON already had in this
        # function; CSV now matches them instead of being the one eager
        # exception).
        with tempfile.TemporaryDirectory() as tmp:
            path = _fixture_csv(Path(tmp))
            lf, fmt = load_polars_frame(str(path))
            self.assertEqual(fmt, "csv")
            self.assertIsInstance(lf, pl.LazyFrame)
            self.assertEqual(lf.collect().height, 20)

    def test_load_polars_frame_csv_directory_is_lazy_with_correct_content(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            _fixture_csv(tmp_path)  # writes data.csv directly into tmp
            lf, fmt = load_polars_frame(str(tmp_path))
            self.assertEqual(fmt, "csv")
            self.assertIsInstance(lf, pl.LazyFrame)
            self.assertEqual(lf.collect().height, 20)

    def test_scan_is_deferred_until_collect_not_at_load_time(self):
        """The actual behavioral difference this fix introduces, made
        explicit: load_csv must NOT have read the file by the time it
        returns -- deleting the source between load_csv() and collect()
        must cause collect() to fail, proving no eager read happened."""
        with tempfile.TemporaryDirectory() as tmp:
            path = _fixture_csv(Path(tmp))
            lf = load_csv(str(path))
            path.unlink()
            with self.assertRaises(Exception):
                lf.collect()


if __name__ == "__main__":
    unittest.main()
