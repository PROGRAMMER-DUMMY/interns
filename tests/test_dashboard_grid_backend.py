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


@unittest.skipUnless(_HAVE, "duckdb / dashboard extra not installed")
class GridGroupingTests(unittest.TestCase):
    """Phase C: server-side GROUP BY (row-grouping / pivot) correctness +
    governance (redacted columns not groupable/aggregatable) + agg allow-list."""

    def setUp(self) -> None:
        self.con = duckdb.connect(":memory:")
        # name = PII (redacted); department = group dim; revenue = measure.
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

    # --- group-by correctness ---------------------------------------------
    def test_group_by_aggregates_measure_per_group(self):
        req = {
            "startRow": 0, "endRow": 100,
            "rowGroupCols": [{"id": "department", "field": "department"}],
            "valueCols": [{"field": "revenue", "aggFunc": "sum"}],
            "groupKeys": [],
        }
        out = gb.serve_rows(self.con, "src", req)
        by_dept = {r["department"]: r["revenue"] for r in out["rowData"]}
        self.assertEqual(by_dept["Cardiology"], 400)   # 100 + 300
        self.assertEqual(by_dept["Oncology"], 250)
        self.assertEqual(by_dept["Neurology"], 50)
        self.assertEqual(out["rowCount"], 3)           # 3 distinct groups

    def test_group_agg_avg(self):
        req = {
            "startRow": 0, "endRow": 100,
            "rowGroupCols": [{"id": "department", "field": "department"}],
            "valueCols": [{"field": "revenue", "aggFunc": "avg"}],
            "groupKeys": [],
        }
        out = gb.serve_rows(self.con, "src", req)
        by_dept = {r["department"]: r["revenue"] for r in out["rowData"]}
        self.assertEqual(by_dept["Cardiology"], 200)   # (100+300)/2

    def test_expand_group_level_filters_to_group_key(self):
        # With one rowGroupCol opened (groupKeys=['Cardiology']) the request is a
        # leaf -> flat detail rows filtered to that group.
        req = {
            "startRow": 0, "endRow": 100,
            "rowGroupCols": [{"id": "department", "field": "department"}],
            "valueCols": [{"field": "revenue", "aggFunc": "sum"}],
            "groupKeys": ["Cardiology"],
        }
        out = gb.serve_rows(self.con, "src", req)
        self.assertTrue(out["rowData"])
        self.assertTrue(all(r["department"] == "Cardiology" for r in out["rowData"]))
        self.assertNotIn("name", out["rowData"][0])    # PII still excluded at leaf

    # --- governance: redacted column never groupable/aggregatable ----------
    def test_redacted_column_cannot_be_grouped(self):
        # Grouping BY the PII column must be dropped (not in allow-list) -> the
        # request degrades to a flat (ungrouped) read, never a GROUP BY on it.
        req = {
            "startRow": 0, "endRow": 100,
            "rowGroupCols": [{"id": "name", "field": "name"}],
            "valueCols": [{"field": "revenue", "aggFunc": "sum"}],
            "groupKeys": [],
        }
        out = gb.serve_rows(self.con, "src", req)
        # No grouping happened: full row count, and the PII column is absent.
        self.assertEqual(out["rowCount"], 4)
        for row in out["rowData"]:
            self.assertNotIn("name", row)

    def test_redacted_column_cannot_be_aggregated(self):
        allowed = {"department": "VARCHAR", "revenue": "INTEGER"}
        req = {
            "startRow": 0, "endRow": 100,
            "rowGroupCols": [{"id": "department", "field": "department"}],
            "valueCols": [{"field": "name", "aggFunc": "max"}],  # PII measure
            "groupKeys": [],
        }
        sql, _params, _csql, _cp = gb.build_group_query("src", req, allowed)
        self.assertNotIn("name", sql)                  # never projected/aggregated

    # --- security: agg func + group-key value are not injectable -----------
    def test_hostile_agg_func_is_not_interpolated(self):
        allowed = {"department": "VARCHAR", "revenue": "INTEGER"}
        req = {
            "startRow": 0, "endRow": 100,
            "rowGroupCols": [{"id": "department", "field": "department"}],
            "valueCols": [{"field": "revenue", "aggFunc": "SUM(revenue)); DROP TABLE src;--"}],
            "groupKeys": [],
        }
        sql, _params, _csql, _cp = gb.build_group_query("src", req, allowed)
        self.assertNotIn("DROP", sql.upper())
        # Falls back to an allow-listed func (SUM for a numeric measure).
        self.assertIn("SUM(", sql.upper())

    def test_group_key_value_is_bound_not_injected(self):
        allowed = {"department": "VARCHAR", "revenue": "INTEGER"}
        malicious = "x'; DROP TABLE src; --"
        req = {
            "startRow": 0, "endRow": 100,
            "rowGroupCols": [
                {"id": "department", "field": "department"},
                {"id": "revenue", "field": "revenue"},
            ],
            "valueCols": [{"field": "revenue", "aggFunc": "sum"}],
            "groupKeys": [malicious],
        }
        sql, params, _csql, _cp = gb.build_group_query("src", req, allowed)
        self.assertNotIn("DROP", sql.upper())
        self.assertIn(malicious, params)               # value bound, not inlined
        # And executing it must not harm the table.
        gb.serve_rows(self.con, "src", req)
        self.assertEqual(self.con.execute("SELECT COUNT(*) FROM src").fetchone()[0], 4)

    def test_agg_allowlist_maps_only_known_funcs(self):
        self.assertEqual(set(gb._AGG_FUNCS) , {"sum", "avg", "min", "max", "count"})


@unittest.skipUnless(_HAVE, "duckdb / dashboard extra not installed")
class GridSourceDiscoveryTests(unittest.TestCase):
    """Phase B: source discovery + registration against the REAL workspace,
    read-only (no filesystem writes). Proves the live-app wiring path end-to-end:
    grid_sources -> register_source -> generate_column_defs -> serve_rows, with
    redacted columns excluded from a raw dataset."""

    WORKSPACE = "workspaces/Healthcare-RCM-Data-Platform"

    def setUp(self) -> None:
        import os

        self.ws = os.path.abspath(self.WORKSPACE)
        if not os.path.isdir(self.ws):
            self.skipTest("Healthcare workspace not present")
        self.con = duckdb.connect(":memory:")

    def tearDown(self) -> None:
        self.con.close()

    def test_grid_sources_lists_datasets_and_kpis(self):
        sources = gb.grid_sources(self.ws)
        kinds = {s["kind"] for s in sources}
        # Workspace has raw CSV datasets and generated KPI SQL.
        self.assertIn("dataset", kinds)
        self.assertIn("kpi", kinds)
        for s in sources:                              # every id is a safe ident
            self.assertTrue(gb.is_safe_identifier(s["id"]), s["id"])

    def test_register_csv_dataset_and_serve_rows_redaction_safe(self):
        sources = [s for s in gb.grid_sources(self.ws)
                   if s["kind"] == "dataset" and s.get("fmt") == "csv"]
        self.assertTrue(sources, "no CSV datasets discovered")
        # Pick the patients dataset if present (it has PII columns), else the first.
        src = next((s for s in sources if "patient" in s["label"].lower()), sources[0])
        relation = gb.register_source(self.con, src)
        self.assertTrue(gb.is_safe_identifier(relation))
        defs = gb.generate_column_defs(self.con, relation, workspace_root=self.ws)
        fields = {d["field"].lower() for d in defs}
        # No obvious PII identifier columns leak onto the grid.
        for pii in ("firstname", "lastname", "name", "ssn", "dob"):
            self.assertNotIn(pii, fields)
        out = gb.serve_rows(self.con, relation, {"startRow": 0, "endRow": 5},
                            workspace_root=self.ws)
        self.assertLessEqual(len(out["rowData"]), 5)
        self.assertGreaterEqual(out["rowCount"], len(out["rowData"]))

    def test_register_kpi_source_materializes_results_view(self):
        sources = [s for s in gb.grid_sources(self.ws) if s["kind"] == "kpi"]
        if not sources:
            self.skipTest("no KPI sources")
        import os

        repo_root = os.path.abspath(".")
        src = sources[0]
        relation = gb.register_source(self.con, src, repo_root=repo_root)
        # The KPI view registers and is queryable (rows may be 0 if SQL yields none,
        # but the view must exist and serve without raising).
        out = gb.serve_rows(self.con, relation, {"startRow": 0, "endRow": 10},
                            workspace_root=self.ws)
        self.assertIn("rowCount", out)

    def test_build_dash_app_registers_explore_slice_callbacks(self):
        """Build-check (read-only): the live app constructs and the Explore slice
        wiring (filter-value options + the chart figure) is registered. The
        slice-and-dice happens IN the Explore chart, not a separate data grid."""
        from pathlib import Path

        from core.dashboard.renderer import build_dash_app

        app = build_dash_app(Path("."), self.WORKSPACE)
        keys = list(app.callback_map)
        self.assertTrue(any("explore-graph.figure" in k for k in keys), keys)
        self.assertTrue(any("explore-filter-vals.options" in k for k in keys), keys)
        # The removed data-grid surface must NOT be present.
        self.assertFalse(any("data-grid" in k for k in keys), keys)


if __name__ == "__main__":
    unittest.main()
