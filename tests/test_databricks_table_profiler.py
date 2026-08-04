"""profile_uc_table: profiles a Unity Catalog table via DatabricksClient
.execute_query() (the SQL warehouse), producing the same DatasetProfile
shape the local-file profiler produces -- no local DuckDB/direct-S3 access,
every read goes through the warehouse.
"""
from __future__ import annotations

import unittest
from unittest import mock

from core.profiling.databricks_table_profiler import profile_uc_table


class FakeClient:
    """Stands in for DatabricksClient -- only execute_query is called.

    Response keys are matched by substring against the issued SQL, in
    insertion order -- first match wins. Keys must be specific enough not to
    collide: the real exact-row-count query is ``SELECT count(*) FROM
    {fqn}``, while the real per-column aggregate query is ``SELECT count(*)
    - count(...), ... FROM {fqn}`` -- both contain the literal substring
    "count(*)", so a bare "count(*)" key would ambiguously match either.
    Use "SELECT count(*) FROM" (with the trailing FROM, right after the
    stars) for the row-count query specifically, and "count(*) - count("
    for the aggregate query specifically.
    """

    def __init__(self, responses: dict) -> None:
        self._responses = responses
        self.queries: list[str] = []

    def execute_query(self, sql: str, **kwargs):
        self.queries.append(sql)
        for key, result in self._responses.items():
            if key in sql:
                return result
        raise AssertionError(f"unexpected query: {sql}")


class ProfileUcTableTests(unittest.TestCase):
    def test_profiles_table_via_warehouse_queries_only(self):
        client = FakeClient(
            {
                "DESCRIBE TABLE": (
                    ["col_name", "data_type", "comment"],
                    [
                        ["DeptID", "string", ""],
                        ["Name", "string", ""],
                        ["# Detailed Table Information", "", ""],
                        ["Catalog", "healthcare_rcm", ""],
                    ],
                ),
                "SELECT count(*) FROM": (["count(1)"], [["20"]]),
                # Neither column is numeric/temporal ("string") -- the
                # aggregate only requests null_count per column, no min/max.
                "count(*) - count(": (["c0", "c1"], [["1", "0"]]),
                "SELECT * FROM": (
                    ["DeptID", "Name"],
                    [["1", "Cardiology"], ["2", "Radiology"], ["3", None]],
                ),
            }
        )

        profile = profile_uc_table(client, "healthcare_rcm", "bronze", "hospital_a_departments")

        self.assertEqual(profile.path, "`healthcare_rcm`.`bronze`.`hospital_a_departments`")
        self.assertEqual(profile.format, "delta")
        self.assertEqual(profile.row_count, 20)
        self.assertEqual(profile.sources_used, ["sql_warehouse_sample", "sql_warehouse_aggregate"])
        self.assertEqual(profile.warnings, [])

        by_name = {c.name: c for c in profile.columns}
        self.assertEqual(set(by_name), {"DeptID", "Name"})
        # The '#' separator row and everything after it must not become a column.
        self.assertNotIn("# Detailed Table Information", by_name)
        self.assertNotIn("Catalog", by_name)
        # null_count now comes from the real aggregate (1 for DeptID, 0 for
        # Name), NOT from counting Nones in the 3-row sample.
        self.assertEqual(by_name["DeptID"].null_count, 1)
        self.assertEqual(by_name["Name"].null_count, 0)
        self.assertIn("Cardiology", by_name["Name"].sample_values)

    def test_identifier_parts_are_safety_checked(self):
        client = FakeClient({})
        with self.assertRaises(Exception):
            profile_uc_table(client, "healthcare_rcm; DROP TABLE x", "bronze", "t")

    def test_schema_sample_column_mismatch_is_warned_not_raised(self):
        client = FakeClient(
            {
                "DESCRIBE TABLE": (["col_name", "data_type"], [["A", "string"]]),
                "SELECT count(*) FROM": (["count(1)"], [["1"]]),
                "count(*) - count(": (["c0"], [["0"]]),
                "SELECT * FROM": (["A", "UnexpectedExtraCol"], [["v", "x"]]),
            }
        )
        profile = profile_uc_table(client, "c", "s", "t")
        self.assertTrue(any("mismatch" in w for w in profile.warnings))

    def test_cardinality_ratio_computed_from_cached_distinct_count(self):
        client = FakeClient(
            {
                # More specific than "DESCRIBE TABLE" below -- must be checked
                # first, since "DESCRIBE TABLE EXTENDED ..." contains the
                # substring "DESCRIBE TABLE" too and FakeClient matches by
                # first-inserted-key-wins. Insertion order in this dict is
                # the match order.
                "EXTENDED `healthcare_rcm`.`bronze`.`hospital_a_departments` `DeptID`": (
                    ["info_name", "info_value"],
                    [
                        ["col_name", "DeptID"],
                        ["data_type", "string"],
                        ["num_nulls", "0"],
                        ["distinct_count", "20"],
                    ],
                ),
                "EXTENDED `healthcare_rcm`.`bronze`.`hospital_a_departments` `Name`": (
                    ["info_name", "info_value"],
                    [
                        ["col_name", "Name"],
                        ["data_type", "string"],
                        ["num_nulls", "0"],
                        ["distinct_count", "5"],
                    ],
                ),
                "DESCRIBE TABLE": (
                    ["col_name", "data_type", "comment"],
                    [
                        ["DeptID", "string", ""],
                        ["Name", "string", ""],
                        ["# Detailed Table Information", "", ""],
                        ["Catalog", "healthcare_rcm", ""],
                    ],
                ),
                "SELECT count(*) FROM": (["count(1)"], [["20"]]),
                "count(*) - count(": (["c0", "c1"], [["1", "0"]]),
                "SELECT * FROM": (
                    ["DeptID", "Name"],
                    [["1", "Cardiology"], ["2", "Radiology"], ["3", None]],
                ),
            }
        )

        profile = profile_uc_table(client, "healthcare_rcm", "bronze", "hospital_a_departments")

        by_name = {c.name: c for c in profile.columns}
        # row_count is 20 (from "SELECT count(*) FROM" above).
        self.assertAlmostEqual(by_name["DeptID"].cardinality_ratio, 20 / 20)
        self.assertAlmostEqual(by_name["Name"].cardinality_ratio, 5 / 20)
        self.assertEqual(profile.warnings, [])

    def test_cardinality_ratio_is_none_when_stats_are_absent(self):
        # No "EXTENDED ..." key registered at all -- FakeClient raises
        # AssertionError("unexpected query") for that call, which the
        # helper must catch and degrade to None + a warning, not propagate.
        # Key is "DESCRIBE TABLE `" (trailing backtick), not bare
        # "DESCRIBE TABLE": a bare key would itself match "DESCRIBE TABLE
        # EXTENDED ..." too (same substring-collision the EXTENDED-specific
        # keys above guard against), which would silently return the schema
        # rows for the cardinality call instead of failing to match --
        # defeating this test's whole premise without ever raising.
        client = FakeClient(
            {
                "DESCRIBE TABLE `": (
                    ["col_name", "data_type", "comment"],
                    [
                        ["DeptID", "string", ""],
                        ["Name", "string", ""],
                    ],
                ),
                "SELECT count(*) FROM": (["count(1)"], [["20"]]),
                "count(*) - count(": (["c0", "c1"], [["1", "0"]]),
                "SELECT * FROM": (
                    ["DeptID", "Name"],
                    [["1", "Cardiology"], ["2", "Radiology"], ["3", None]],
                ),
            }
        )

        profile = profile_uc_table(client, "healthcare_rcm", "bronze", "hospital_a_departments")

        by_name = {c.name: c for c in profile.columns}
        self.assertIsNone(by_name["DeptID"].cardinality_ratio)
        self.assertIsNone(by_name["Name"].cardinality_ratio)
        self.assertTrue(
            any("cardinality_stats_failed" in w for w in profile.warnings),
            f"expected a cardinality_stats_failed warning, got: {profile.warnings}",
        )
        # A stats-read failure must not affect anything else already working.
        self.assertEqual(by_name["DeptID"].null_count, 1)
        self.assertIn("Cardiology", by_name["Name"].sample_values)

    def test_value_pattern_and_profile_tier_are_populated(self):
        client = FakeClient(
            {
                "EXTENDED `healthcare_rcm`.`bronze`.`hospital_a_claims` `ChargeAmount`": (
                    ["info_name", "info_value"],
                    [["distinct_count", "3"]],
                ),
                "DESCRIBE TABLE": (
                    ["col_name", "data_type", "comment"],
                    [["ChargeAmount", "double", ""]],
                ),
                "SELECT count(*) FROM": (["count(1)"], [["3"]]),
                "count(*) - count(": (["c0", "c1", "c2"], [["0", "100.50", "102.50"]]),
                "SELECT * FROM": (
                    ["ChargeAmount"],
                    [["100.50"], ["101.50"], ["102.50"]],
                ),
            }
        )

        profile = profile_uc_table(client, "healthcare_rcm", "bronze", "hospital_a_claims")

        by_name = {c.name: c for c in profile.columns}
        self.assertEqual(by_name["ChargeAmount"].value_pattern, "currency_2dp")
        self.assertEqual(by_name["ChargeAmount"].profile_tier, "raw")


if __name__ == "__main__":
    unittest.main()
