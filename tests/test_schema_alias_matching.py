from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from core.onboarding.lexicon.builder import ColumnAliasEntry, WorkspaceLexicon
from core.onboarding.relationships.schema_alias_matching import (
    alias_index,
    aliases_for_column,
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

    def test_alias_index_uses_structural_suffix_patterns_without_lexicon(self):
        """Without a workspace lexicon only universal structural patterns
        (suffix variants, id/identifier) fire. The previous curated
        ``dept``↔``department`` rewrite has been removed."""
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

        # Universal structural: id↔identifier still produces a deptidentifier alias.
        self.assertIn("deptidentifier", aliases)
        self.assertEqual(
            candidate_labels(aliases["deptidentifier"]),
            ["DeptID (datasets/transactions.csv)"],
        )
        # The healthcare-specific dept↔department curated rewrite is GONE.
        self.assertNotIn("departmentid", aliases)

    def test_alias_index_consumes_workspace_lexicon_aliases(self):
        """When a workspace lexicon is supplied, its ``column_aliases``
        contribute. This is how DeptID → departmentid is learned now: from the
        workspace's own data dictionary or accepted user definitions, not from
        a curated dictionary shipped in code."""
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
        lexicon = WorkspaceLexicon(
            metric_phrases=[],
            cut_phrases=[],
            column_aliases={
                "DeptID": ColumnAliasEntry(
                    canonical="DeptID",
                    normalized={"deptid"},
                    from_user_definitions={"departmentid", "department"},
                )
            },
            cuts_headers=[],
        )

        aliases = alias_index(index, lexicon=lexicon)

        self.assertIn("departmentid", aliases)
        self.assertTrue(
            safe_structural_alias("DepartmentID", aliases["departmentid"], lexicon=lexicon)
        )
        self.assertEqual(
            candidate_labels(aliases["departmentid"]),
            ["DeptID (datasets/transactions.csv)"],
        )

    def test_aliases_for_column_without_lexicon_only_returns_structural(self):
        """Pure structural aliases only: no healthcare-RCM business terms."""
        result = aliases_for_column("PaidAmount")
        self.assertEqual(result, {"paidamount"})
        self.assertNotIn("paid", result)
        self.assertNotIn("amount", result)

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
