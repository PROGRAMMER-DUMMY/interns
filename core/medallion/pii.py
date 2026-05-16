"""
core/medallion/pii.py — PII hashing helpers (P3).

Hash function: SHA-256(value || salt). Deterministic within a workspace,
non-portable across workspaces. Salt is never inlined into SQL.
"""
from __future__ import annotations

import hashlib
from typing import Optional


def pii_hash_value(value: Optional[str], *, workspace: str) -> str:
    """Hash a single value with the workspace salt. For Python-side use only."""
    from core.medallion.salt_store import get_workspace_salt
    salt = get_workspace_salt(workspace)
    s = value if value is not None else ""
    return hashlib.sha256((s + salt).encode("utf-8")).hexdigest()


def pii_hash_sql_duckdb(column: str) -> str:
    """
    Emit a DuckDB SQL expression that hashes `column` using a $salt parameter
    bound at execute time — salt never inlined.
    """
    return f"sha256(coalesce(cast({column} AS VARCHAR), '') || $salt)"


def pii_hash_spark_expr(column: str) -> str:
    """
    Emit a Spark SQL expression. Salt comes from a session UDF registered as
    get_workspace_salt() before the script runs.
    """
    return (
        f"sha2(concat(coalesce(cast({column} AS string), ''), get_workspace_salt()), 256)"
    )


def pii_columns_for_silver_table(silver_contract: dict, table: str) -> list[str]:
    """Return the list of PII columns to hash for a given Silver table."""
    return (silver_contract.get(table) or {}).get("pii_hash_columns", []) or []


def register_spark_salt_udf(spark, workspace: str) -> None:
    """Register get_workspace_salt() as a session-scoped UDF on `spark`."""
    from core.medallion.salt_store import get_workspace_salt
    salt = get_workspace_salt(workspace)
    spark.udf.register("get_workspace_salt", lambda: salt, "string")


def collect_pii_columns_from_semantic_contract(sc: dict) -> set[str]:
    """
    Walk semantic_contract.json and collect all columns marked pii=True.
    Returns a set of "column_name" strings (lowercased).
    """
    pii_cols: set[str] = set()
    for dataset in sc.get("datasets", []) or []:
        for col, meta in (dataset.get("columns") or {}).items():
            if isinstance(meta, dict) and meta.get("pii"):
                pii_cols.add(col.lower())
        # Flat list style
        for col in dataset.get("pii_columns", []) or []:
            pii_cols.add(str(col).lower())
    return pii_cols
