"""Tests for the panel preview cache helper."""

from __future__ import annotations

import json
import os
import tempfile
import time
import unittest
from pathlib import Path

from core.onboarding.kpi.panel_preview_cache import (
    cache_path,
    compute_preview_cache_key,
    evict_stale_entries,
    load_cached_preview,
    save_cached_preview,
)


def _write_csv(directory: Path, name: str, content: str = "a,b\n1,2\n") -> Path:
    path = directory / name
    path.write_text(content, encoding="utf-8")
    return path


class ComputePreviewCacheKeyTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_key_is_deterministic(self) -> None:
        ds = _write_csv(self.tmp, "data.csv")
        sql = "SELECT * FROM data"
        a = compute_preview_cache_key(sql, [ds])
        b = compute_preview_cache_key(sql, [ds])
        self.assertEqual(a, b)
        self.assertEqual(len(a), 32)
        # Hex check.
        int(a, 16)

    def test_key_changes_when_sql_changes(self) -> None:
        ds = _write_csv(self.tmp, "data.csv")
        a = compute_preview_cache_key("SELECT * FROM data", [ds])
        b = compute_preview_cache_key("SELECT a FROM data", [ds])
        self.assertNotEqual(a, b)

    def test_key_changes_when_dataset_mtime_changes(self) -> None:
        ds = _write_csv(self.tmp, "data.csv")
        sql = "SELECT * FROM data"
        first = compute_preview_cache_key(sql, [ds])

        # Backdate then forward-date to force a different mtime_ns reliably.
        past = time.time() - 10_000
        os.utime(ds, (past, past))
        second = compute_preview_cache_key(sql, [ds])
        self.assertNotEqual(first, second)

    def test_key_handles_missing_dataset_path(self) -> None:
        missing = self.tmp / "does_not_exist.csv"
        # Should not raise.
        key_missing = compute_preview_cache_key("SELECT 1", [missing])
        self.assertEqual(len(key_missing), 32)

        # Once the file appears, the key must change because mtime_ns flips from 0.
        missing.write_text("a\n1\n", encoding="utf-8")
        key_present = compute_preview_cache_key("SELECT 1", [missing])
        self.assertNotEqual(key_missing, key_present)

    def test_key_independent_of_dataset_order(self) -> None:
        ds_a = _write_csv(self.tmp, "a.csv")
        ds_b = _write_csv(self.tmp, "b.csv")
        sql = "SELECT * FROM a JOIN b USING (id)"
        self.assertEqual(
            compute_preview_cache_key(sql, [ds_a, ds_b]),
            compute_preview_cache_key(sql, [ds_b, ds_a]),
        )

    def test_key_ignores_surrounding_whitespace_in_sql(self) -> None:
        ds = _write_csv(self.tmp, "data.csv")
        a = compute_preview_cache_key("SELECT 1", [ds])
        b = compute_preview_cache_key("   SELECT 1\n  ", [ds])
        self.assertEqual(a, b)


class LoadSaveRoundTripTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.workspace = Path(self._tmp.name)
        self.key = "a" * 32

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_load_returns_none_when_no_entry(self) -> None:
        self.assertIsNone(load_cached_preview(self.workspace, self.key))

    def test_round_trip_payload(self) -> None:
        payload = {
            "columns": ["a", "b"],
            "rows": [[1, 2], [3, 4]],
            "row_count": 2,
        }
        final_path = save_cached_preview(self.workspace, self.key, payload)
        self.assertTrue(final_path.exists())
        self.assertEqual(
            final_path,
            self.workspace / "interns" / "state" / "panel_previews" / f"{self.key}.json",
        )
        loaded = load_cached_preview(self.workspace, self.key)
        self.assertEqual(loaded, payload)

    def test_load_returns_none_for_corrupt_json(self) -> None:
        path = cache_path(self.workspace, self.key)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"not-json-at-all{{{")
        self.assertIsNone(load_cached_preview(self.workspace, self.key))
        # File is left in place for next save to overwrite.
        self.assertTrue(path.exists())

    def test_load_returns_none_when_top_level_is_list(self) -> None:
        path = cache_path(self.workspace, self.key)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps([1, 2, 3]), encoding="utf-8")
        self.assertIsNone(load_cached_preview(self.workspace, self.key))

    def test_save_then_overwrite_corrupt(self) -> None:
        path = cache_path(self.workspace, self.key)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"garbage")
        save_cached_preview(self.workspace, self.key, {"ok": True})
        self.assertEqual(load_cached_preview(self.workspace, self.key), {"ok": True})

    def test_save_is_atomic_no_tmp_left_behind(self) -> None:
        save_cached_preview(self.workspace, self.key, {"ok": True})
        cache_dir = self.workspace / "interns" / "state" / "panel_previews"
        leftover = list(cache_dir.glob("*.tmp"))
        self.assertEqual(leftover, [])

    def test_save_handles_non_json_native_with_default_str(self) -> None:
        # Verifies default=str fallback: a Path object would normally raise.
        payload = {"path": Path("/some/where")}
        save_cached_preview(self.workspace, self.key, payload)
        loaded = load_cached_preview(self.workspace, self.key)
        self.assertIsNotNone(loaded)
        assert loaded is not None
        self.assertIn("path", loaded)


class EvictStaleEntriesTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.workspace = Path(self._tmp.name)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def _cache_dir(self) -> Path:
        return self.workspace / "interns" / "state" / "panel_previews"

    def test_returns_zero_when_directory_missing(self) -> None:
        self.assertEqual(evict_stale_entries(self.workspace), 0)

    def test_removes_entries_older_than_max_age(self) -> None:
        fresh_key = "f" * 32
        stale_key = "s" * 32
        save_cached_preview(self.workspace, fresh_key, {"v": 1})
        save_cached_preview(self.workspace, stale_key, {"v": 2})

        stale_path = cache_path(self.workspace, stale_key)
        old = time.time() - 10_000
        os.utime(stale_path, (old, old))

        removed = evict_stale_entries(self.workspace, max_age_seconds=3_600)
        self.assertEqual(removed, 1)
        self.assertTrue(cache_path(self.workspace, fresh_key).exists())
        self.assertFalse(stale_path.exists())

    def test_caps_directory_at_keep_at_most(self) -> None:
        # Create 5 entries with distinct mtimes; keep only 2.
        keys = [chr(ord("a") + i) * 32 for i in range(5)]
        base_time = time.time() - 1_000
        for offset, key in enumerate(keys):
            save_cached_preview(self.workspace, key, {"i": offset})
            path = cache_path(self.workspace, key)
            ts = base_time + offset  # oldest is keys[0], newest is keys[-1]
            os.utime(path, (ts, ts))

        removed = evict_stale_entries(
            self.workspace,
            max_age_seconds=10_000_000,  # don't evict by age
            keep_at_most=2,
        )
        self.assertEqual(removed, 3)

        remaining = sorted(p.name for p in self._cache_dir().glob("*.json"))
        # The two newest (keys[3] and keys[4]) should survive.
        expected = sorted([f"{keys[3]}.json", f"{keys[4]}.json"])
        self.assertEqual(remaining, expected)

    def test_never_raises_on_unreadable_directory(self) -> None:
        # Point to a workspace whose cache dir is actually a regular file.
        odd_workspace = self.workspace / "weird"
        (odd_workspace / "interns" / "state").mkdir(parents=True, exist_ok=True)
        # Create a file where the cache dir would be.
        fake = odd_workspace / "interns" / "state" / "panel_previews"
        fake.write_text("not a directory", encoding="utf-8")

        # Should swallow the error and return 0.
        result = evict_stale_entries(odd_workspace)
        self.assertEqual(result, 0)


if __name__ == "__main__":
    unittest.main()
