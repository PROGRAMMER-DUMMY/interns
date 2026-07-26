"""Profiler gap: nested-JSON-payload visibility, P1.

API-sourced data lands as raw JSON in bronze (core/onboarding/sources/catalog.py
-- correctly untouched, matches 2026 Databricks/Snowflake/BigQuery bronze
practice). But core/profiling/data_model_profiler.py's `_profile_polars` only
ever computed stats for numeric/temporal dtypes -- a Struct/List column (what
Polars infers for a nested JSON field via pl.scan_ndjson) got zero visibility:
no null_count, no leaf enumeration, nothing. Downstream, this meant PHI/PCI
detection, schema-alias matching, and KPI feature resolution could never see a
field like `metadata.patient.ssn` nested inside a payload column at all.

Fixed: DatasetProfile gets a NEW, additive `nested_leaf_columns` field (schema/
columns stay exactly flat/physical, unchanged) populated by walking Struct/List
dtypes to leaf dot-paths (`metadata.patient.ssn`, `visits[].date`). Struct
leaves get real min/max/null_count/sample_values via one batched Polars
expression pass. List[Struct] array-element leaves get name/dtype discovery
ONLY -- no .explode()-based stats, to avoid row fan-out on an untrusted
payload; full stats only after explicit human-confirmed promotion (P3).
Recursion is capped at _MAX_NESTED_DEPTH against pathological payloads.

See ~/.claude/plans/dynamic-cooking-firefly.md P1.
"""
from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

import polars as pl

from core.onboarding.features.derived_evidence import iter_nested_leaf_entries
from core.profiling.data_model_profiler import DataModelProfiler, _MAX_NESTED_DEPTH


def _write_ndjson(path: Path, rows: list[dict]) -> None:
    with path.open("w", encoding="utf-8") as handle:
        for row in rows:
            handle.write(json.dumps(row) + "\n")


def _leaves_by_name(profile) -> dict:
    return {col.name: col for col in profile.nested_leaf_columns}


class NestedLeafStructProfilingTests(unittest.TestCase):
    def test_struct_leaf_gets_real_stats_flat_schema_untouched(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "data.json"
            _write_ndjson(
                path,
                [
                    {"metadata": {"patient": {"ssn": "123-45-6789"}}, "amount": 10},
                    {"metadata": {"patient": {"ssn": None}}, "amount": 20},
                ],
            )
            profiler = DataModelProfiler()
            profile = profiler.profile_path(path)

        # Flat schema/columns stay exactly the physical, top-level view --
        # byte-identical to the pre-fix behavior.
        self.assertEqual(set(profile.schema.keys()), {"metadata", "amount"})
        self.assertTrue(profile.schema["metadata"].startswith("Struct"))
        physical_names = {c.name for c in profile.columns}
        self.assertEqual(physical_names, {"metadata", "amount"})

        leaves = _leaves_by_name(profile)
        self.assertIn("metadata.patient.ssn", leaves)
        leaf = leaves["metadata.patient.ssn"]
        self.assertEqual(leaf.source, "nested_leaf_struct")
        self.assertEqual(leaf.null_count, 1)
        self.assertIn("123-45-6789", leaf.sample_values)

    def test_summary_includes_nested_leaf_columns(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "data.json"
            _write_ndjson(path, [{"info": {"x": 1}}])
            profile = DataModelProfiler().profile_path(path)
        payload = profile.summary()
        self.assertIn("nested_leaf_columns", payload)
        self.assertEqual(payload["nested_leaf_columns"][0]["name"], "info.x")


class NestedLeafArrayDiscoveryTests(unittest.TestCase):
    def test_list_of_struct_leaves_are_discovery_only(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "data.json"
            _write_ndjson(
                path,
                [{"visits": [{"date": "2024-01-01", "ssn": "111-11-1111"}]}],
            )
            profile = DataModelProfiler().profile_path(path)

        leaves = _leaves_by_name(profile)
        self.assertIn("visits[].date", leaves)
        self.assertIn("visits[].ssn", leaves)
        for name in ("visits[].date", "visits[].ssn"):
            leaf = leaves[name]
            self.assertEqual(leaf.source, "nested_leaf_list_element")
            self.assertIsNone(leaf.null_count)
            self.assertEqual(leaf.sample_values, [])


class NestedLeafDepthCapTests(unittest.TestCase):
    def test_pathological_depth_is_capped_not_hung_or_dropped(self):
        # Build a struct nested ~20 levels deep -- well past _MAX_NESTED_DEPTH.
        row: dict = {"v": 1}
        for _ in range(20):
            row = {"n": row}
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "data.json"
            _write_ndjson(path, [row])
            profile = DataModelProfiler().profile_path(path)

        self.assertTrue(
            any(
                w.startswith(f"nested_leaf_discovery_capped_at_depth:{_MAX_NESTED_DEPTH}:")
                for w in profile.warnings
            )
        )
        # Completed without raising/hanging and produced SOME leaf entry for
        # the truncated remainder rather than silently dropping it.
        self.assertTrue(len(profile.nested_leaf_columns) >= 1)


class NestedLeafFlatFixtureRegressionTests(unittest.TestCase):
    def test_flat_parquet_has_no_nested_leaf_columns(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "data.parquet"
            pl.DataFrame({"id": [1, 2, 3], "name": ["a", "b", "c"]}).write_parquet(path)
            profile = DataModelProfiler().profile_path(path)
        self.assertEqual(profile.nested_leaf_columns, [])
        self.assertEqual(set(profile.schema.keys()), {"id", "name"})

    def test_flat_csv_has_no_nested_leaf_columns(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "data.csv"
            path.write_text("id,name\n1,a\n2,b\n", encoding="utf-8")
            profile = DataModelProfiler().profile_path(path)
        self.assertEqual(profile.nested_leaf_columns, [])


class IterNestedLeafEntriesHelperTests(unittest.TestCase):
    def test_yields_leaf_entries_from_a_profile_dict(self):
        profile = {
            "nested_leaf_columns": [
                {"name": "metadata.patient.ssn", "dtype": "String", "null_count": 1,
                 "sample_values": ["123-45-6789"]},
            ]
        }
        entries = list(iter_nested_leaf_entries(profile))
        self.assertEqual(len(entries), 1)
        self.assertEqual(entries[0]["name"], "metadata.patient.ssn")
        self.assertEqual(entries[0]["null_count"], 1)

    def test_missing_field_yields_nothing_older_profiles_stay_valid(self):
        self.assertEqual(list(iter_nested_leaf_entries({"schema": {"a": "Int64"}})), [])


if __name__ == "__main__":
    unittest.main()
