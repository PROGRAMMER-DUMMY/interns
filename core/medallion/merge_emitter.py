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

    pk_tuple = ", ".join(primary_key)
    fqn = f"silver.{table_name}"

    return (
        f"-- P1 MERGE: create {fqn} schema on first run\n"
        f"CREATE TABLE IF NOT EXISTS {fqn} AS (\n"
        f"{select_body}\n"
        f") WHERE 1=0;\n"
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
