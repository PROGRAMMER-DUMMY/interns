"""Single source of truth for sensitive-column masking across all KPI engines.

The SQL, Polars, and PySpark generators all consult this module so they agree on
(1) WHICH columns are sensitive and (2) HOW to mask them. Masking is SHA-256 hex
in every engine, so a sensitive value:

  - never reaches Silver/Gold in raw form in ANY engine (previously only the SQL
    engine masked; Polars/PySpark wrote raw values — a real PHI/PCI leak), and
  - produces an IDENTICAL digest across engines for identical string input, so
    masked columns still match in cross-engine parity instead of spuriously
    mismatching.

DuckDB ``sha256(CAST(x AS VARCHAR))``, Spark ``sha2(CAST(x AS STRING), 256)`` and
Python ``hashlib.sha256(x.encode()).hexdigest()`` all yield the same lowercase
hex for the same input, and NULL maps to NULL/None in every engine.

Ref: core-audit ob-kpi-b.md (theme T2).
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any


def load_sensitive_columns(layout: Any) -> set[str]:
    """Lowercased names of columns marked sensitive in semantic_contract.json.

    Reads BOTH shapes the platform uses: the flat ``columns.<name>.is_sensitive``
    map the onboarder writes, and the nested ``datasets[].columns[].pii`` /
    ``datasets[].pii_columns`` form. Ref: ob-workspace-b.md (shape divergence).
    """
    path = layout.contracts_dir / "semantic_contract.json"
    sensitive: set[str] = set()
    if not path.exists():
        return sensitive
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return sensitive
    if not isinstance(data, dict):
        return sensitive
    flat = data.get("columns")
    if isinstance(flat, dict):
        for name, meta in flat.items():
            if isinstance(meta, dict) and (meta.get("is_sensitive") or meta.get("pii")):
                sensitive.add(str(name).lower())
    for ds in data.get("datasets") or []:
        if not isinstance(ds, dict):
            continue
        for name, meta in (ds.get("columns") or {}).items():
            if isinstance(meta, dict) and meta.get("pii"):
                sensitive.add(str(name).lower())
        for name in ds.get("pii_columns") or []:
            sensitive.add(str(name).lower())
    return sensitive


def feature_sensitive_columns(feature: dict[str, Any], sensitive: set[str]) -> set[str]:
    """Source column names of ``feature`` that are sensitive.

    Resolved from the feature's source-column METADATA (``source_columns[].column``)
    and the feature name — NOT by string-splitting a rendered SQL expression, which
    mis-detected derived-formula features (a formula body is not a column name).
    Ref: ob-kpi-b.md line 126.
    """
    hits: set[str] = set()
    for sc in feature.get("source_columns") or []:
        if isinstance(sc, dict):
            col = str(sc.get("column") or "")
            if col and col.lower() in sensitive:
                hits.add(col)
    name = str(feature.get("feature") or "")
    if name and name.lower() in sensitive:
        hits.add(name)
    return hits


def is_feature_sensitive(feature: dict[str, Any], sensitive: set[str]) -> bool:
    """True if any of the feature's source columns (or its name) is sensitive."""
    return bool(feature_sensitive_columns(feature, sensitive))


def mask_sql_expr(column_expr: str, dialect: str) -> str:
    """SHA-256 hex mask for a SQL column expression.

    ``column_expr`` may be a qualified ref (``s0."Amount"``) or any scalar
    expression. The digest matches the Polars/PySpark masks for the same string
    input, so masked columns stay parity-consistent.
    """
    if dialect == "databricks":
        return f"sha2(CAST({column_expr} AS STRING), 256)"
    return f"sha256(CAST({column_expr} AS VARCHAR))"


def pyspark_mask_expr(column_name: str) -> str:
    """SHA-256 hex mask for a PySpark column (string form for F.expr/selectExpr)."""
    return f"sha2(cast(`{column_name}` as string), 256)"


# Name of the Polars helper the generator emits and references.
POLARS_MASK_HELPER = "_mask_sensitive"


def polars_mask_helper_lines() -> list[str]:
    """Source lines defining the Polars masking helper, emitted into the script.

    The helper hashes a column to SHA-256 hex, matching DuckDB
    ``sha256(CAST(col AS VARCHAR))``. None-safe and distinctness-preserving so
    grouping on a masked dimension behaves like the SQL path.
    """
    return [
        "def _mask_sensitive(col: str) -> pl.Expr:",
        '    """SHA-256 hex mask matching DuckDB sha256(CAST(col AS VARCHAR)).',
        "    None-safe; preserves row distinctness so grouping is unaffected.\"\"\"",
        "    return (",
        "        pl.col(col)",
        "        .cast(pl.Utf8, strict=False)",
        "        .map_elements(",
        "            lambda v: None if v is None else hashlib.sha256(v.encode('utf-8')).hexdigest(),",
        "            return_dtype=pl.Utf8,",
        "        )",
        "        .alias(col)",
        "    )",
        "",
    ]


__all__ = [
    "load_sensitive_columns",
    "feature_sensitive_columns",
    "is_feature_sensitive",
    "mask_sql_expr",
    "pyspark_mask_expr",
    "POLARS_MASK_HELPER",
    "polars_mask_helper_lines",
]
