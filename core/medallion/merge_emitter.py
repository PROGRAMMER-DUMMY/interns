"""
core/medallion/merge_emitter.py — Rewrite P0 Silver SQL to P1 MERGE semantics.

P0 Silver SQL pattern:
    CREATE OR REPLACE TABLE silver.X AS
    WITH unioned AS (...) SELECT ... FROM unioned;

P1 MERGE pattern (DuckDB):
    CREATE TABLE IF NOT EXISTS silver.X AS (<select>) WHERE 1=0;
    DELETE FROM silver.X WHERE (pk1, pk2) IN (SELECT pk1, pk2 FROM (<select>) AS _src);
    INSERT INTO silver.X <select>;

The DELETE + INSERT approach avoids needing to enumerate all non-PK columns for
ON CONFLICT ... DO UPDATE SET, while still being correct merge semantics.
"""
from __future__ import annotations

import re
from typing import Optional

from core.sql_safety import assert_safe_identifier, quote_ident_sql


def emit_scd2_merge(
    table_name: str,
    business_key: list[str],
    attribute_columns: list[str],
    select_body: str,
    *,
    schema: str = "gold",
) -> str:
    """Emit an idempotent SCD Type 2 upsert for a dimension (Step 3 / Step 6).

    Tracks history with ``valid_from`` / ``valid_to`` (null ``valid_to`` = the
    current row). When a tracked attribute changes for a business key, the open
    row is closed (``valid_to = now``) and a fresh current row is inserted.
    Re-running with the same source is a no-op (idempotent): an unchanged key
    already has an identical open row. All identifiers are injection-validated.
    """
    safe_table = assert_safe_identifier(table_name, context="scd2 table")
    bk = [quote_ident_sql(assert_safe_identifier(c, context="scd2 business key")) for c in business_key]
    attrs = [quote_ident_sql(assert_safe_identifier(c, context="scd2 attribute")) for c in attribute_columns]
    all_cols = bk + attrs
    col_list = ", ".join(all_cols)
    src_list = ", ".join(f"src.{c}" for c in all_cols)
    bk_join = " AND ".join(f"tgt.{c} = src.{c}" for c in bk)
    changed = " OR ".join(f"tgt.{c} IS DISTINCT FROM src.{c}" for c in attrs) or "FALSE"
    fqn = f"{schema}.{safe_table}"
    src = f"(\n{_indent(select_body, 8)}\n    ) AS src"
    return (
        f"-- SCD2: create {fqn} with history columns on first run\n"
        f"CREATE TABLE IF NOT EXISTS {fqn} AS (\n"
        f"    SELECT {col_list}, CAST(NULL AS TIMESTAMP) AS valid_from,\n"
        f"           CAST(NULL AS TIMESTAMP) AS valid_to\n"
        f"    FROM (\n{_indent(select_body, 8)}\n    ) AS _seed LIMIT 0\n"
        f");\n\n"
        f"-- SCD2: close out rows whose tracked attributes changed (open -> now)\n"
        f"UPDATE {fqn} AS tgt SET valid_to = now()\n"
        f"WHERE tgt.valid_to IS NULL AND EXISTS (\n"
        f"    SELECT 1 FROM {src}\n"
        f"    WHERE {bk_join} AND ({changed})\n"
        f");\n\n"
        f"-- SCD2: insert a current row for any key lacking an identical open row\n"
        f"INSERT INTO {fqn} ({col_list}, valid_from, valid_to)\n"
        f"SELECT {src_list}, now(), CAST(NULL AS TIMESTAMP)\n"
        f"FROM {src}\n"
        f"WHERE NOT EXISTS (\n"
        f"    SELECT 1 FROM {fqn} tgt\n"
        f"    WHERE {bk_join} AND tgt.valid_to IS NULL AND NOT ({changed})\n"
        f");\n"
    )


def emit_silver_merge(
    table_name: str,
    primary_key: list[str],
    p0_sql: str,
) -> str:
    """
    Rewrite a P0-style silver SQL file to P1 MERGE (DELETE+INSERT) semantics.

    Returns the full P1 SQL string. If the P0 pattern cannot be parsed,
    returns the original SQL with a warning comment (safe fallback).
    """
    select_body = _extract_select_body(p0_sql)
    if select_body is None:
        return (
            "-- [merge_emitter] WARNING: could not parse P0 pattern; keeping as-is\n"
            + p0_sql
        )

    # Validate the table name + every PK column as bare identifiers so a hostile
    # contract cannot inject SQL into the emitted DELETE/INSERT MERGE (T4).
    # Valid names pass through unchanged; bad names fail the build.
    safe_table = assert_safe_identifier(table_name, context="silver merge table")
    pk_cols = [
        quote_ident_sql(assert_safe_identifier(col, context="silver merge PK column"))
        for col in primary_key
    ]
    pk_tuple = ", ".join(pk_cols)
    fqn = f"silver.{safe_table}"

    return (
        f"-- P1 MERGE: create {fqn} schema on first run\n"
        f"CREATE TABLE IF NOT EXISTS {fqn} AS (\n"
        f"{select_body}\n"
        f"    LIMIT 0\n"
        f");\n"
        f"\n"
        f"-- P1 MERGE: delete existing rows that match incoming PKs\n"
        f"DELETE FROM {fqn}\n"
        f"WHERE ({pk_tuple}) IN (\n"
        f"    SELECT {pk_tuple} FROM (\n"
        f"{_indent(select_body, 8)}\n"
        f"    ) AS _src\n"
        f");\n"
        f"\n"
        f"-- P1 MERGE: insert new/updated rows\n"
        f"INSERT INTO {fqn}\n"
        f"{select_body};\n"
    )


def _extract_select_body(p0_sql: str) -> Optional[str]:
    """
    Extract the SELECT body from a P0 CREATE OR REPLACE TABLE ... AS <body>.
    Returns None if the pattern is not found.
    """
    # Strip line comments and match the CREATE OR REPLACE TABLE ... AS pattern
    # The body is everything from AS to the trailing semicolon.
    pattern = re.compile(
        r"CREATE\s+OR\s+REPLACE\s+TABLE\s+\S+\s+AS\s*([\s\S]+?);\s*"
        r"(?:--.*)?$",
        re.IGNORECASE | re.MULTILINE,
    )
    m = pattern.search(p0_sql)
    if m:
        return m.group(1).strip()

    # Fallback: split on "AS\n" (less strict)
    upper = p0_sql.upper()
    idx = upper.find(" AS\n")
    if idx == -1:
        idx = upper.find(" AS ")
    if idx == -1:
        return None
    body = p0_sql[idx + 4:].strip().rstrip(";").strip()
    return body if body else None


def _indent(text: str, spaces: int) -> str:
    pad = " " * spaces
    return "\n".join(pad + line for line in text.splitlines())
