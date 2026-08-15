"""Typed unknown member in the medallion DuckDB gold OBT.

The fact side already LEFT JOINs, so an early-arriving fact keeps its row -- but
every dimension attribute came back NULL, which is the "unhandled NULL dimension"
half of the late-arriving-dimension problem. Blank NULLs then reach every consumer
of the materialised table: CLI queries, the Dash app, CSV/Excel exports.

This layer is deliberately schema-agnostic (silver and gold both emit `SELECT *`
with REPLACE/EXCLUDE; neither Manifest.SilverTable nor TableContract carries a
column list), so the columns are resolved at BUILD time from the DuckDB tables
that exist by then. These tests run real DuckDB rather than asserting on SQL
strings, because the point is that the emitted statement actually executes and
produces the right rows.
"""

from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

import duckdb  # noqa: E402

from core.medallion.delta_emitter import (  # noqa: E402
    UNKNOWN_MEMBER,
    emit_gold_duckdb,
    introspect_columns,
    unknown_member_literal,
)
from core.medallion.manifest import GoldTable  # noqa: E402


def build_warehouse(con) -> None:
    con.execute("CREATE SCHEMA IF NOT EXISTS silver")
    con.execute("CREATE SCHEMA IF NOT EXISTS gold")
    con.execute(
        "CREATE TABLE silver.claim("
        " claim_id INTEGER, cust_id INTEGER, amount DOUBLE, service_date DATE)"
    )
    con.execute(
        "INSERT INTO silver.claim VALUES"
        " (1, 100, 9.50, DATE '2026-01-02'),"
        " (2, 98412, 4.25, DATE '2026-01-03')"  # 98412 has no dimension row yet
    )
    con.execute(
        "CREATE TABLE silver.customer("
        " cust_id INTEGER, name VARCHAR, tier INTEGER, since TIMESTAMP, active BOOLEAN)"
    )
    con.execute(
        "INSERT INTO silver.customer VALUES"
        " (100, 'Acme', 3, TIMESTAMP '2020-05-01 00:00:00', true)"
    )


def fact_table() -> GoldTable:
    return GoldTable(
        name="fact_claim",
        kind="fact",
        derived_from=["silver.claim"],
        foreign_keys=[{
            "from_column": "cust_id",
            "to_table": "silver.customer",
            "to_column": "cust_id",
        }],
    )


class UnknownMemberLiteralTest(unittest.TestCase):
    def test_numeric_types_use_minus_one(self) -> None:
        for dtype in ("INTEGER", "BIGINT", "DOUBLE", "DECIMAL(18,2)", "HUGEINT"):
            with self.subTest(dtype=dtype):
                self.assertEqual(unknown_member_literal(dtype), "-1")

    def test_temporal_types_use_the_epoch(self) -> None:
        self.assertEqual(unknown_member_literal("DATE"), "DATE '1970-01-01'")
        self.assertIn("1970-01-01 00:00:00", unknown_member_literal("TIMESTAMP"))

    def test_text_uses_the_shared_sentinel(self) -> None:
        self.assertEqual(unknown_member_literal("VARCHAR"), f"'{UNKNOWN_MEMBER}'")

    def test_boolean_is_left_null(self) -> None:
        # No honest unknown boolean; matches the dbt path's choice.
        self.assertIsNone(unknown_member_literal("BOOLEAN"))

    def test_unmappable_types_are_left_null(self) -> None:
        for dtype in ("BLOB", "STRUCT(a INTEGER)", "INTEGER[]", ""):
            with self.subTest(dtype=dtype):
                self.assertIsNone(unknown_member_literal(dtype))


class GoldObtIntrospectionTest(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.out_dir = Path(self._tmp.name)
        self.con = duckdb.connect()
        build_warehouse(self.con)

    def tearDown(self) -> None:
        self.con.close()
        self._tmp.cleanup()

    def test_introspection_reads_the_real_schema(self) -> None:
        columns = dict(introspect_columns(self.con, "silver.customer"))
        self.assertEqual(columns["name"], "VARCHAR")
        self.assertEqual(columns["tier"], "INTEGER")
        self.assertTrue(columns["since"].startswith("TIMESTAMP"))

    def test_introspecting_a_missing_table_returns_empty_not_raise(self) -> None:
        self.assertEqual(introspect_columns(self.con, "silver.nope"), [])

    def test_without_introspection_the_sql_is_unchanged_star_expansion(self) -> None:
        sql = emit_gold_duckdb(fact_table(), self.out_dir).read_text(encoding="utf-8")
        self.assertIn("EXCLUDE", sql)
        self.assertNotIn("coalesce", sql.lower())

    def test_late_arriving_fact_gets_a_named_unknown_not_blank_nulls(self) -> None:
        dimension_columns = {"silver.customer": introspect_columns(self.con, "silver.customer")}
        sql = emit_gold_duckdb(
            fact_table(), self.out_dir, dimension_columns=dimension_columns
        ).read_text(encoding="utf-8")

        # The emitted statement must actually run.
        for statement in [s for s in sql.split(";") if s.strip()]:
            self.con.execute(statement)

        rows = self.con.execute(
            'SELECT claim_id, "name", "tier", "since", "active"'
            " FROM gold.fact_claim ORDER BY claim_id"
        ).fetchall()
        self.assertEqual(len(rows), 2, "the early-arriving fact row must survive")

        matched, late = rows
        self.assertEqual(matched[1], "Acme")
        self.assertEqual(matched[2], 3)

        # The late-arriving row: typed unknowns, not NULLs.
        self.assertEqual(late[0], 2)
        self.assertEqual(late[1], UNKNOWN_MEMBER)
        self.assertEqual(late[2], -1)
        self.assertEqual(str(late[3]), "1970-01-01 00:00:00")
        self.assertIsNone(late[4], "BOOLEAN stays NULL on purpose")

    def test_dimension_types_are_preserved_not_stringified(self) -> None:
        dimension_columns = {"silver.customer": introspect_columns(self.con, "silver.customer")}
        sql = emit_gold_duckdb(
            fact_table(), self.out_dir, dimension_columns=dimension_columns
        ).read_text(encoding="utf-8")
        for statement in [s for s in sql.split(";") if s.strip()]:
            self.con.execute(statement)
        types = dict(introspect_columns(self.con, "gold.fact_claim"))
        self.assertEqual(types["tier"], "INTEGER")
        self.assertTrue(types["since"].startswith("TIMESTAMP"))

    def test_join_key_is_not_duplicated_onto_the_obt(self) -> None:
        dimension_columns = {"silver.customer": introspect_columns(self.con, "silver.customer")}
        sql = emit_gold_duckdb(
            fact_table(), self.out_dir, dimension_columns=dimension_columns
        ).read_text(encoding="utf-8")
        for statement in [s for s in sql.split(";") if s.strip()]:
            self.con.execute(statement)
        names = [name for name, _ in introspect_columns(self.con, "gold.fact_claim")]
        self.assertEqual(names.count("cust_id"), 1)

    def test_dimension_table_is_untouched_by_the_change(self) -> None:
        dim = GoldTable(name="dim_customer", kind="dimension",
                        derived_from=["silver.customer"])
        sql = emit_gold_duckdb(dim, self.out_dir).read_text(encoding="utf-8")
        self.assertIn("full-refresh", sql)
        self.assertNotIn("coalesce", sql.lower())


if __name__ == "__main__":
    unittest.main()
