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


if __name__ == "__main__":
    unittest.main()
