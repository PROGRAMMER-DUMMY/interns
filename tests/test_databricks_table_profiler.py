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
                # EXTENDED-specific keys must be registered (and, since
                # FakeClient matches first-inserted-key-wins, checked before
                # the bare "DESCRIBE TABLE `" key below) so the cardinality
                # queries Fix 2 issues for each column resolve to a real
                # distinct_count row instead of falling through to "no key
                # matched" (AssertionError -> cardinality_stats_failed) or to
                # the schema-description rows (no distinct_count row ->
                # cardinality_stats_missing) -- either of which would break
                # this test's warnings == [] assertion below.
                "EXTENDED `healthcare_rcm`.`bronze`.`hospital_a_departments` `DeptID`": (
                    ["info_name", "info_value"],
                    [["distinct_count", "20"]],
                ),
                "EXTENDED `healthcare_rcm`.`bronze`.`hospital_a_departments` `Name`": (
                    ["info_name", "info_value"],
                    [["distinct_count", "2"]],
                ),
                "DESCRIBE TABLE `": (
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
        self.assertEqual(
            profile.sources_used,
            ["sql_warehouse_sample", "sql_warehouse_aggregate", "unity_catalog_statistics"],
        )
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
        self.assertIn("unity_catalog_statistics", profile.sources_used)

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

    def test_aggregate_min_max_offsets_stay_aligned_across_multiple_numeric_columns(self):
        # Column order: a plain (non-numeric) column first, then TWO
        # min/max-eligible columns back to back. _aggregate_column_stats's
        # `pos` accumulator must advance by 1 slot for a plain column
        # (null_count only) and by 3 for a numeric/temporal one (null_count,
        # min, max) -- every other fixture in this file has at most one
        # min/max-eligible column and never asserts on exact_min/exact_max,
        # so none of them would notice `pos` drifting. Putting the
        # non-numeric column first means a wrong offset misaligns the very
        # first numeric column's stats, not just a later one.
        client = FakeClient(
            {
                "DESCRIBE TABLE `": (
                    ["col_name", "data_type", "comment"],
                    [
                        ["DeptName", "string", ""],
                        ["Amount", "double", ""],
                        ["VisitDate", "date", ""],
                    ],
                ),
                "SELECT count(*) FROM": (["count(1)"], [["5"]]),
                # Slots: DeptName=[null_count], Amount=[null_count,min,max],
                # VisitDate=[null_count,min,max] -- 7 values total.
                "count(*) - count(": (
                    ["c0", "c1", "c2", "c3", "c4", "c5", "c6"],
                    [["2", "1", "10.50", "999.99", "0", "2024-01-01", "2024-12-31"]],
                ),
                "SELECT * FROM": (
                    ["DeptName", "Amount", "VisitDate"],
                    [["Cardiology", "10.50", "2024-01-01"]],
                ),
            }
        )

        profile = profile_uc_table(client, "healthcare_rcm", "bronze", "hospital_a_encounters")

        by_name = {c.name: c for c in profile.columns}
        self.assertEqual(by_name["DeptName"].null_count, 2)
        self.assertEqual(by_name["Amount"].null_count, 1)
        self.assertEqual(by_name["Amount"].exact_min, "10.50")
        self.assertEqual(by_name["Amount"].exact_max, "999.99")
        self.assertEqual(by_name["VisitDate"].null_count, 0)
        self.assertEqual(by_name["VisitDate"].exact_min, "2024-01-01")
        self.assertEqual(by_name["VisitDate"].exact_max, "2024-12-31")

    def test_never_issues_an_analyze_query(self):
        client = FakeClient(
            {
                "EXTENDED `healthcare_rcm`.`bronze`.`hospital_a_departments` `DeptID`": (
                    ["info_name", "info_value"],
                    [["distinct_count", "20"]],
                ),
                "DESCRIBE TABLE `": (
                    ["col_name", "data_type", "comment"],
                    [["DeptID", "string", ""]],
                ),
                "SELECT count(*) FROM": (["count(1)"], [["20"]]),
                "count(*) - count(": (["c0"], [["0"]]),
                "SELECT * FROM": (["DeptID"], [["1"], ["2"]]),
            }
        )
        profile_uc_table(client, "healthcare_rcm", "bronze", "hospital_a_departments")
        self.assertTrue(client.queries)
        self.assertFalse(
            any("analyze" in q.lower() for q in client.queries),
            f"profiling must never trigger ANALYZE TABLE, got queries: {client.queries}",
        )

    def test_unsafe_column_name_degrades_that_column_not_the_whole_profile(self):
        # DESCRIBE TABLE can return a column name that is not a bare SQL
        # identifier (space, punctuation) -- assert_safe_identifier rejects
        # it. Before this fix, that raised UnsafeIdentifierError uncaught out
        # of both _aggregate_column_stats and _read_cardinality_stats,
        # killing the WHOLE table's profile over one odd column name. Now it
        # must degrade only "Bad Col"'s own stats to None + a warning, and
        # "DeptID" must still profile normally.
        client = FakeClient(
            {
                "EXTENDED `healthcare_rcm`.`bronze`.`hospital_a_departments` `DeptID`": (
                    ["info_name", "info_value"],
                    [["distinct_count", "20"]],
                ),
                "DESCRIBE TABLE `": (
                    ["col_name", "data_type", "comment"],
                    [
                        ["DeptID", "string", ""],
                        ["Bad Col", "string", ""],
                    ],
                ),
                "SELECT count(*) FROM": (["count(1)"], [["20"]]),
                "count(*) - count(": (["c0"], [["0"]]),
                "SELECT * FROM": (
                    ["DeptID", "Bad Col"],
                    [["1", "x"], ["2", "y"]],
                ),
            }
        )

        profile = profile_uc_table(client, "healthcare_rcm", "bronze", "hospital_a_departments")

        by_name = {c.name: c for c in profile.columns}
        self.assertAlmostEqual(by_name["DeptID"].cardinality_ratio, 20 / 20)
        self.assertIsNone(by_name["Bad Col"].cardinality_ratio)
        self.assertIsNone(by_name["Bad Col"].null_count)
        self.assertTrue(
            any("unsafe_identifier" in w and "Bad Col" in w for w in profile.warnings),
            f"expected an unsafe-identifier warning naming 'Bad Col', got: {profile.warnings}",
        )

    def test_cardinality_stats_missing_row_is_warned(self):
        # The EXTENDED query succeeds but the returned rows contain no
        # "distinct_count" row at all (stats were never computed for this
        # column) -- distinct from the query itself failing/raising, which
        # test_cardinality_ratio_is_none_when_stats_are_absent already
        # covers. Must be distinguishable in warnings.
        client = FakeClient(
            {
                "EXTENDED `healthcare_rcm`.`bronze`.`hospital_a_departments` `DeptID`": (
                    ["info_name", "info_value"],
                    [["col_name", "DeptID"], ["data_type", "string"]],
                ),
                "DESCRIBE TABLE `": (
                    ["col_name", "data_type", "comment"],
                    [["DeptID", "string", ""]],
                ),
                "SELECT count(*) FROM": (["count(1)"], [["20"]]),
                "count(*) - count(": (["c0"], [["0"]]),
                "SELECT * FROM": (["DeptID"], [["1"], ["2"]]),
            }
        )
        profile = profile_uc_table(client, "healthcare_rcm", "bronze", "hospital_a_departments")
        by_name = {c.name: c for c in profile.columns}
        self.assertIsNone(by_name["DeptID"].cardinality_ratio)
        self.assertIn("cardinality_stats_missing:DeptID", profile.warnings)


if __name__ == "__main__":
    unittest.main()
