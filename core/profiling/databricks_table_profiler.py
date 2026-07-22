"""Profile a Unity Catalog table by querying it through the SQL warehouse.

Deliberately NOT a local DuckDB/Delta-scan-direct-from-S3 shortcut: that
approach was tried and reverted -- it bypasses Unity Catalog governance,
audit logging, and query history entirely, and never exercises the actual
compute resource (the SQL warehouse) the rest of this pipeline depends on.
Every query here goes through DatabricksClient.execute_query(), the same
warehouse this platform's dbt builds and KPI execution will use -- so this
profiling pass is also, incidentally, the first real proof that warehouse
query execution works end to end.

Produces the same DatasetProfile/ColumnProfile shape core.profiling
.data_model_profiler's local-file profiler produces, so the ~25 downstream
consumers (lexicon builder, relationship planner, KPI feature resolver, SQL
generators, PHI gate, etc.) work unmodified regardless of which profiler
produced a given entry in profile_index.json.
"""
from __future__ import annotations

from typing import TYPE_CHECKING, Any

from core.profiling.data_model_profiler import ColumnProfile, DatasetProfile
from core.sql_safety import assert_safe_identifier, quote_ident_backtick

if TYPE_CHECKING:
    from core.execution.databricks_client import DatabricksClient


def _qualified_name(catalog: str, schema: str, table: str) -> str:
    return ".".join(
        quote_ident_backtick(assert_safe_identifier(part, context="uc table part"))
        for part in (catalog, schema, table)
    )


def profile_uc_table(
    client: "DatabricksClient",
    catalog: str,
    schema: str,
    table: str,
    *,
    sample_rows: int = 1000,
) -> DatasetProfile:
    """Profile one Unity Catalog table via the SQL warehouse.

    Three queries: DESCRIBE (schema), COUNT(*) (exact row count), and a
    bounded SELECT (sample, used to compute per-column null_count/sample
    values/min-max in Python). At this platform's current data scale
    (thousands of rows per table) fetching a sample and computing stats
    client-side is simple and correct; at real production scale this should
    push the aggregation into SQL the way the local CSV profiler's DuckDB
    pushdown path already does, rather than fetching rows to summarize them
    -- noted here as a known scaling limit, not addressed in this pass.
    """
    fqn = _qualified_name(catalog, schema, table)
    warnings: list[str] = []

    desc_cols, desc_rows = client.execute_query(f"DESCRIBE TABLE {fqn}")
    col_idx = {name: i for i, name in enumerate(desc_cols)}
    schema_map: dict[str, str] = {}
    for row in desc_rows:
        name = row[col_idx.get("col_name", 0)]
        dtype = row[col_idx.get("data_type", 1)]
        if not name or name.startswith("#") or name in schema_map:
            # DESCRIBE TABLE appends partition-info/comment sections marked
            # with a leading '#' after the real column rows -- stop reading
            # columns once one of those separator rows appears.
            if name and name.startswith("#"):
                break
            continue
        schema_map[name] = dtype

    _, count_rows = client.execute_query(f"SELECT count(*) FROM {fqn}")
    row_count = int(count_rows[0][0]) if count_rows else 0

    sample_cols, sample_rows_data = client.execute_query(
        f"SELECT * FROM {fqn} LIMIT {int(sample_rows)}"
    )
    if sample_cols and set(sample_cols) != set(schema_map):
        warnings.append(f"describe_select_column_mismatch:{sample_cols}!={list(schema_map)}")

    columns: list[ColumnProfile] = []
    for name, dtype in schema_map.items():
        idx = sample_cols.index(name) if name in sample_cols else None
        values = [row[idx] for row in sample_rows_data] if idx is not None else []
        non_null = [v for v in values if v is not None]
        distinct_sorted = sorted(dict.fromkeys(non_null))[:8]
        columns.append(
            ColumnProfile(
                name=name,
                dtype=dtype,
                nullable=None,
                sample_values=distinct_sorted,
                sample_min=distinct_sorted[0] if distinct_sorted else None,
                sample_max=distinct_sorted[-1] if distinct_sorted else None,
                null_count=(len(values) - len(non_null)) if values else None,
                source="sample_profile",
            )
        )

    return DatasetProfile(
        path=fqn,
        format="delta",
        row_count=row_count,
        file_count=1,
        size_bytes=0,  # not read here; DESCRIBE DETAIL has it if ever needed
        schema=schema_map,
        columns=columns,
        downcast_recommendations=[],
        sources_used=["sql_warehouse_sample"],
        warnings=warnings,
    )
