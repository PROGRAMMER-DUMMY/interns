"""Governed SSRM grid backend: safety + governance + correctness.

The grid translates untrusted client grid-state into DuckDB queries, so these
tests lock the security-critical properties: no SQL injection (values bound, not
formatted), redacted/PII columns never reach the grid, and filter/sort/paginate
are correct.
"""
from __future__ import annotations

import unittest

try:
    import duckdb
    from core.dashboard import grid_backend as gb
    _HAVE = True
except Exception:  # pragma: no cover - dashboard/duckdb extra not installed
    _HAVE = False


@unittest.skipUnless(_HAVE, "duckdb / dashboard extra not installed")
class GridBackendTests(unittest.TestCase):
    def setUp(self) -> None:
        self.con = duckdb.connect(":memory:")
        # A relation with a clearly-PII column ("name") + a non-PII dimension and
        # a numeric measure. "name" must be redacted off the grid.
        self.con.execute(
            "CREATE TABLE src AS SELECT * FROM (VALUES "
            "('Alice','Cardiology',100),"
            "('Bob','Oncology',250),"
            "('Carol','Cardiology',300),"
            "('Dave','Neurology',50)"
            ") AS t(name, department, revenue)"
        )

    def tearDown(self) -> None:
        self.con.close()

    # --- governance: PII column never reaches the grid ---------------------
    def test_redacted_column_excluded_from_coldefs(self):
        defs = gb.generate_column_defs(self.con, "src", workspace_root=None)
        fields = {d["field"] for d in defs}
        self.assertNotIn("name", fields)              # ^name$ is a default PII pattern
        self.assertEqual({"department", "revenue"}, fields)

    def test_redacted_column_absent_from_rows(self):
        out = gb.serve_rows(self.con, "src", {"startRow": 0, "endRow": 100})
        self.assertTrue(out["rowData"])
        for row in out["rowData"]:
            self.assertNotIn("name", row)

    def test_filter_on_redacted_column_is_ignored(self):
        # A filter targeting the redacted column must be dropped (not allowed_cols).
        req = {
            "startRow": 0, "endRow": 100,
            "filterModel": {"name": {"filterType": "text", "type": "contains", "filter": "Alice"}},
        }
        out = gb.serve_rows(self.con, "src", req)
        self.assertEqual(out["rowCount"], 4)          # filter ignored -> all rows

    # --- security: injection value is bound, not executed -----------------
    def test_text_filter_value_is_bound_not_injected(self):
        malicious = "x'; DROP TABLE src; --"
        req = {
            "startRow": 0, "endRow": 100,
            "filterModel": {"department": {"filterType": "text", "type": "contains", "filter": malicious}},
        }
        out = gb.serve_rows(self.con, "src", req)
        self.assertEqual(out["rowCount"], 0)          # no match, no execution
        # table still exists + intact
        self.assertEqual(self.con.execute("SELECT COUNT(*) FROM src").fetchone()[0], 4)

    def test_unsafe_relation_name_rejected(self):
        from core.sql_safety import UnsafeIdentifierError

        with self.assertRaises(UnsafeIdentifierError):
            gb.serve_rows(self.con, "src; DROP TABLE src", {"startRow": 0, "endRow": 10})

    def test_build_query_binds_values_as_params(self):
        allowed = {"department": "VARCHAR", "revenue": "INTEGER"}
        req = {
            "startRow": 0, "endRow": 50,
            "filterModel": {"department": {"filterType": "text", "type": "contains", "filter": "Card"}},
        }
        sql, params, _csql, _cp = gb.build_rows_query("src", req, allowed)
        self.assertIn("?", sql)                        # parameter placeholder
        self.assertNotIn("Card", sql)                  # value not inlined
        self.assertEqual(params, ["%Card%"])

    # --- correctness: filter / sort / paginate ----------------------------
    def test_number_filter_and_count(self):
        req = {
            "startRow": 0, "endRow": 100,
            "filterModel": {"revenue": {"filterType": "number", "type": "greaterThan", "filter": 100}},
        }
        out = gb.serve_rows(self.con, "src", req)
        self.assertEqual(out["rowCount"], 2)           # 250, 300
        self.assertTrue(all(r["revenue"] > 100 for r in out["rowData"]))

    def test_sort_desc(self):
        req = {"startRow": 0, "endRow": 100, "sortModel": [{"colId": "revenue", "sort": "desc"}]}
        out = gb.serve_rows(self.con, "src", req)
        revenues = [r["revenue"] for r in out["rowData"]]
        self.assertEqual(revenues, sorted(revenues, reverse=True))

    def test_sort_on_unknown_column_ignored(self):
        req = {"startRow": 0, "endRow": 100, "sortModel": [{"colId": "nope", "sort": "asc"}]}
        out = gb.serve_rows(self.con, "src", req)     # must not raise
        self.assertEqual(out["rowCount"], 4)

    def test_pagination_block(self):
        req = {"startRow": 0, "endRow": 2, "sortModel": [{"colId": "revenue", "sort": "asc"}]}
        out = gb.serve_rows(self.con, "src", req)
        self.assertEqual(len(out["rowData"]), 2)       # only the requested block
        self.assertEqual(out["rowCount"], 4)           # full count reported
        self.assertEqual([r["revenue"] for r in out["rowData"]], [50, 100])

    def test_allowlist_dropped_filter_column_not_in_schema(self):
        allowed = {"department": "VARCHAR", "revenue": "INTEGER"}
        req = {
            "startRow": 0, "endRow": 50,
            "filterModel": {"ghost": {"filterType": "text", "type": "contains", "filter": "z"}},
        }
        sql, params, _csql, _cp = gb.build_rows_query("src", req, allowed)
        self.assertNotIn("ghost", sql)
        self.assertEqual(params, [])


if __name__ == "__main__":
    unittest.main()
