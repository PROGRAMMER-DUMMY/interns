from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from core.onboarding.relationships.schema_alias_matching import (
    alias_index,
    candidate_labels,
    load_schema_index,
    safe_structural_alias,
    schema_index_from_profiles,
    source_columns,
)


class SchemaAliasMatchingTests(unittest.TestCase):
    def test_schema_index_from_profiles_preserves_profile_evidence(self):
        index = schema_index_from_profiles(
            [
                {
                    "path": "datasets/transactions.csv",
                    "row_count": 2,
                    "profile_path": "profiles/transactions.profile.json",
                    "schema": {"PaidAmount": "Float64"},
                    "columns": [
                        {
                            "name": "PaidAmount",
                            "sample_min": 10.5,
                            "sample_max": 20.25,
                            "sample_values": [10.5],
                            "null_count": 0,
                        }
                    ],
                }
            ]
        )

        evidence = index["paidamount"][0]
        self.assertEqual(evidence["dataset"], "datasets/transactions.csv")
        self.assertEqual(evidence["column"], "PaidAmount")
        self.assertEqual(evidence["sample_values"], [10.5])
        self.assertEqual(evidence["null_count"], 0)

    def test_load_schema_index_returns_empty_when_missing(self):
        with tempfile.TemporaryDirectory() as td:
            self.assertEqual(load_schema_index(Path(td) / "missing.json"), {})

    def test_alias_index_supports_structural_aliases(self):
        index = schema_index_from_profiles(
            [
                {
                    "path": "datasets/transactions.csv",
                    "row_count": 1,
                    "profile_path": "profiles/transactions.profile.json",
                    "schema": {"DeptID": "Utf8"},
                    "columns": [{"name": "DeptID", "sample_values": ["D1"]}],
                }
            ]
        )

        aliases = alias_index(index)

        self.assertIn("departmentid", aliases)
        self.assertTrue(safe_structural_alias("DepartmentID", aliases["departmentid"]))
        self.assertEqual(candidate_labels(aliases["departmentid"]), ["DeptID (datasets/transactions.csv)"])

    def test_source_columns_include_profile_payload(self):
        columns = source_columns(
            [
                {
                    "dataset": "datasets/transactions.csv",
                    "column": "ClaimID",
                    "dtype": "Utf8",
                    "row_count": 2,
                    "profile_path": "profiles/transactions.profile.json",
                    "sample_values": ["C1"],
                    "null_count": 0,
                }
            ]
        )

        self.assertEqual(columns[0]["observed_values"], ["C1"])
        self.assertEqual(columns[0]["value_profile"]["null_count"], 0)
        self.assertEqual(columns[0]["semantic_meaning_sources"][0]["field"], "ClaimID")


if __name__ == "__main__":
    unittest.main()
