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

from core.profiling.data_model_profiler import (
    ColumnProfile,
    DatasetProfile,
    _infer_value_pattern,
    _is_numeric_dtype,
    _is_temporal_dtype,
)
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

    Four queries: DESCRIBE (schema), COUNT(*) (exact row count), one combined
    aggregate query (exact null_count for every column, exact min/max for
    numeric/temporal columns -- computed server-side by the warehouse, the
    same pushdown technique already used by the local CSV profiler's
    ``_duckdb_column_stats``), and a bounded SELECT used ONLY for
    illustrative ``sample_values`` -- never for a statistic. Row count was
    already exact; null_count/min/max previously came from summarizing
    whatever ``sample_rows`` rows the warehouse happened to return first,
    which is silently biased on any large table where nulls or extreme
    values aren't uniformly distributed across physical/partition order.
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

    agg_stats = _aggregate_column_stats(client, fqn, schema_map, row_count, warnings)
    cardinality_stats = _read_cardinality_stats(client, fqn, schema_map, warnings)

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
        stats = agg_stats.get(name)
        distinct_count = cardinality_stats.get(name)
        cardinality_ratio = (
            (distinct_count / row_count) if distinct_count is not None and row_count else None
        )
        columns.append(
            ColumnProfile(
                name=name,
                dtype=dtype,
                nullable=None,
                sample_values=distinct_sorted,
                sample_min=distinct_sorted[0] if distinct_sorted else None,
                sample_max=distinct_sorted[-1] if distinct_sorted else None,
                exact_min=stats["min"] if stats else None,
                exact_max=stats["max"] if stats else None,
                null_count=stats["null_count"] if stats else None,
                source="exact_scan" if stats else "sample_profile",
                cardinality_ratio=cardinality_ratio,
                value_pattern=_infer_value_pattern(distinct_sorted),
                profile_tier="raw",
            )
        )

    sources_used = ["sql_warehouse_sample"]
    if agg_stats:
        sources_used.append("sql_warehouse_aggregate")

    return DatasetProfile(
        path=fqn,
        format="delta",
        row_count=row_count,
        file_count=1,
        size_bytes=0,  # not read here; DESCRIBE DETAIL has it if ever needed
        schema=schema_map,
        columns=columns,
        downcast_recommendations=[],
        sources_used=sources_used,
        warnings=warnings,
    )


def _aggregate_column_stats(
    client: "DatabricksClient",
    fqn: str,
    schema_map: dict[str, str],
    row_count: int,
    warnings: list[str],
) -> dict[str, dict[str, Any]]:
    """One real aggregate query over the FULL table: exact null_count for
    every column, exact min/max for numeric/temporal columns. Server-side,
    via the warehouse -- not fetched and summarized client-side. Returns
    ``{}`` (not raised) on any query failure so a warehouse hiccup degrades
    to the pre-existing sample-based columns rather than failing profiling
    outright; the failure is still recorded in ``warnings``."""
    col_names = list(schema_map)
    if not col_names or not row_count:
        return {}

    exprs: list[str] = []
    plan: list[tuple[str, bool]] = []
    for name in col_names:
        quoted = quote_ident_backtick(assert_safe_identifier(name, context="uc column"))
        has_min_max = _is_numeric_dtype(schema_map[name]) or _is_temporal_dtype(schema_map[name])
        exprs.append(f"count(*) - count({quoted})")
        if has_min_max:
            exprs.append(f"min({quoted})")
            exprs.append(f"max({quoted})")
        plan.append((name, has_min_max))

    try:
        _, agg_rows = client.execute_query(f"SELECT {', '.join(exprs)} FROM {fqn}")
    except Exception as exc:  # pragma: no cover - warehouse/network dependent
        warnings.append(f"aggregate_stats_failed:{type(exc).__name__}:{exc}")
        return {}

    row = agg_rows[0] if agg_rows else ()
    stats: dict[str, dict[str, Any]] = {}
    pos = 0
    for name, has_min_max in plan:
        null_count = int(row[pos]) if pos < len(row) and row[pos] is not None else None
        pos += 1
        col_min = col_max = None
        if has_min_max:
            col_min = row[pos] if pos < len(row) else None
            pos += 1
            col_max = row[pos] if pos < len(row) else None
            pos += 1
        stats[name] = {"null_count": null_count, "min": col_min, "max": col_max}
    return stats


def _read_cardinality_stats(
    client: "DatabricksClient",
    fqn: str,
    schema_map: dict[str, str],
    warnings: list[str],
) -> dict[str, int | None]:
    """Read each column's ``distinct_count`` from Unity Catalog's own cached
    column statistics (``ANALYZE TABLE ... COMPUTE STATISTICS``, run
    automatically by Databricks predictive optimization on managed tables,
    or manually on others) via ``DESCRIBE TABLE EXTENDED``. A metastore
    read, not a data scan.

    Deliberately NOT an approximate or freshly-computed count: this platform
    profiles customer-owned source tables it cannot write to, so there is no
    lever to guarantee or refresh statistics freshness here (see
    docs/superpowers/specs/2026-08-04-databricks-cardinality-profiling-design.md).
    Per-column failure (stats never computed, unsupported table type,
    permissions, a table with no ANALYZE history) degrades that column to
    ``None`` and is recorded in ``warnings`` -- never raised. ``None`` here
    means "signal absent," the same contract the local DuckDB profiler's
    missing-signal paths already use; never treat it as zero.
    """
    stats: dict[str, int | None] = {}
    for name in schema_map:
        quoted_col = quote_ident_backtick(assert_safe_identifier(name, context="uc column"))
        stats[name] = None
        try:
            _, rows = client.execute_query(f"DESCRIBE TABLE EXTENDED {fqn} {quoted_col}")
        except Exception as exc:  # pragma: no cover - warehouse/network dependent
            warnings.append(f"cardinality_stats_failed:{name}:{type(exc).__name__}:{exc}")
            continue
        for row in rows:
            if len(row) >= 2 and str(row[0]).strip().lower() == "distinct_count":
                try:
                    stats[name] = int(row[1])
                except (TypeError, ValueError):
                    warnings.append(f"cardinality_stats_unparseable:{name}:{row[1]!r}")
                break
    return stats
