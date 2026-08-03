"""
Metadata-first data model profiling.

The profiler prefers cheap metadata before scanning data:
catalog stats -> Delta log stats -> Parquet row-group stats -> sample profile
-> exact scan. The current implementation supports local Parquet/CSV/Delta-like
paths and is deliberately safe when optional dependencies are unavailable.
"""
from __future__ import annotations

import json
import re
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from core.governance.contracts import DowncastPolicy

try:
    import polars as pl
except ImportError:  # pragma: no cover - optional at runtime
    pl = None

try:
    import pyarrow.parquet as pq
except ImportError:  # pragma: no cover - optional at runtime
    pq = None

try:
    import duckdb
except ImportError:  # pragma: no cover - optional at runtime
    duckdb = None


# Polars dtype string -> DuckDB column type, used to force DuckDB's CSV reader
# onto the exact schema Polars inferred so pushdown aggregates match the legacy
# Polars profile value-for-value. Dtypes outside this map (Decimal, List, ...)
# disable pushdown for that file and fall back to the legacy path.
_POLARS_TO_DUCKDB_TYPES: dict[str, str] = {
    "Int8": "TINYINT",
    "Int16": "SMALLINT",
    "Int32": "INTEGER",
    "Int64": "BIGINT",
    "UInt8": "UTINYINT",
    "UInt16": "USMALLINT",
    "UInt32": "UINTEGER",
    "UInt64": "UBIGINT",
    "Float32": "FLOAT",
    "Float64": "DOUBLE",
    "Boolean": "BOOLEAN",
    "String": "VARCHAR",
    "Utf8": "VARCHAR",
    "Date": "DATE",
    "Time": "TIME",
}


def _duckdb_type_for_polars(dtype: str) -> str | None:
    if dtype in _POLARS_TO_DUCKDB_TYPES:
        return _POLARS_TO_DUCKDB_TYPES[dtype]
    if dtype.startswith("Datetime"):
        return "TIMESTAMP"
    return None


_VALUE_PATTERN_CHECKS: list[tuple[str, "re.Pattern[str]"]] = [
    ("currency_2dp", re.compile(r"^\d+\.\d{2}$")),
    ("iso_date", re.compile(r"^\d{4}-\d{2}-\d{2}$")),
    ("prefixed_numeric_code", re.compile(r"^[A-Za-z]+[-_]?\d+$")),
    ("fixed_length_alnum", re.compile(r"^[A-Za-z0-9]{6,12}$")),
]


def _infer_value_pattern(sample_values: list[Any]) -> str | None:
    """Named structural pattern shared by >=80% of non-null sample values.

    Evidence-driven, no domain vocabulary: matches shape (digits/letters/
    separators), never a specific business term. Returns None when no
    pattern clears the 80% bar -- a mixed-shape column reports no pattern
    rather than a misleading weak one.
    """
    values = [str(v) for v in sample_values if v is not None and str(v).strip()]
    if not values:
        return None
    for pattern_name, pattern in _VALUE_PATTERN_CHECKS:
        matches = sum(1 for v in values if pattern.match(v))
        if matches / len(values) >= 0.8:
            return pattern_name
    return None


# Bound on how deep nested-leaf discovery (Struct/List columns, e.g. from a
# JSON-sourced API payload) will recurse. A real payload can nest deeply or
# pathologically; past this depth a discovery call yields one opaque leaf for
# the unexpanded remainder instead of hanging or dropping it silently.
_MAX_NESTED_DEPTH = 6


INTEGER_BOUNDS = [
    ("Int8", -(2**7), 2**7 - 1),
    ("Int16", -(2**15), 2**15 - 1),
    ("Int32", -(2**31), 2**31 - 1),
    ("Int64", -(2**63), 2**63 - 1),
]
UNSIGNED_BOUNDS = [
    ("UInt8", 0, 2**8 - 1),
    ("UInt16", 0, 2**16 - 1),
    ("UInt32", 0, 2**32 - 1),
    ("UInt64", 0, 2**64 - 1),
]


@dataclass(frozen=True)
class ColumnProfile:
    name: str
    dtype: str
    nullable: bool | None = None
    sample_values: list[Any] = field(default_factory=list)
    sample_min: Any = None
    sample_max: Any = None
    exact_min: Any = None
    exact_max: Any = None
    metadata_min: Any = None
    metadata_max: Any = None
    null_count: int | None = None
    source: str = "schema"
    # Fraction of distinct values over row count (unique_count / row_count).
    # None where not computed (parquet-metadata and polars-fallback paths
    # do not populate it yet -- see Task 3 notes in the implementation plan).
    cardinality: float | None = None
    # Named structural pattern shared by >=80% of observed sample values
    # (see _infer_value_pattern), or None when no pattern clears that bar.
    value_pattern: str | None = None
    # Every profile is stamped "raw": profiling runs pre-medallion, against
    # bronze-shaped (pre-dedup -- bronze_silver_standards.py explicitly
    # forbids deduplication_application in bronze) source data, never
    # silver. A future silver re-profile can stamp "silver" and upgrade a
    # mapping's confidence cap; nothing does that yet.
    profile_tier: str = "raw"

    def authoritative_min(self) -> Any:
        return self.exact_min if self.exact_min is not None else self.metadata_min

    def authoritative_max(self) -> Any:
        return self.exact_max if self.exact_max is not None else self.metadata_max


@dataclass(frozen=True)
class DowncastRecommendation:
    column: str
    current_dtype: str
    recommended_dtype: str | None
    decision: str
    reason: str
    requires_approval: bool = False
    evidence: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class DatasetProfile:
    path: str
    format: str
    row_count: int | None
    file_count: int
    size_bytes: int
    schema: dict[str, str]
    columns: list[ColumnProfile] = field(default_factory=list)
    downcast_recommendations: list[DowncastRecommendation] = field(default_factory=list)
    sources_used: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    # Fields discovered INSIDE a Struct/List column (e.g. a JSON API payload),
    # named by dot path (`metadata.patient.ssn`, `visits[].date`). Additive
    # and separate from `schema`/`columns`, which stay exactly the flat,
    # physical-column view every existing consumer (DDL generation, schema
    # indexing) already relies on -- a nested leaf is never a physical column.
    nested_leaf_columns: list[ColumnProfile] = field(default_factory=list)

    def summary(self) -> dict[str, Any]:
        return {
            "path": self.path,
            "format": self.format,
            "row_count": self.row_count,
            "file_count": self.file_count,
            "size_bytes": self.size_bytes,
            "schema": self.schema,
            "sources_used": self.sources_used,
            "warnings": self.warnings,
            "columns": [asdict(col) for col in self.columns],
            "downcast_recommendations": [
                asdict(rec) for rec in self.downcast_recommendations
            ],
            "nested_leaf_columns": [asdict(col) for col in self.nested_leaf_columns],
        }

    def to_json(self) -> str:
        return json.dumps(self.summary(), indent=2, default=str)


class DataModelProfiler:
    def __init__(
        self,
        downcast_policy: DowncastPolicy | None = None,
        *,
        pushdown: bool = True,
    ):
        self.downcast_policy = downcast_policy or DowncastPolicy()
        # When True (default) CSV profiling pushes aggregations down to DuckDB
        # (row counts, null counts, min/max, observed values via SQL over
        # read_csv) instead of materializing rows in Python. Falls back to the
        # legacy Polars path on any DuckDB error or unmapped dtype.
        self.pushdown = pushdown

    def profile_path(
        self,
        path: str | Path,
        sample_rows: int = 100_000,
        exact: bool = False,
    ) -> DatasetProfile:
        target = Path(path)
        fmt = _detect_format(target)
        files = _list_data_files(target, fmt)
        size_bytes = sum(file.stat().st_size for file in files if file.exists())
        warnings: list[str] = []
        sources_used: list[str] = []

        schema: dict[str, str] = {}
        row_count: int | None = None
        columns: dict[str, ColumnProfile] = {}
        nested_leaf_columns: dict[str, ColumnProfile] = {}

        if fmt == "parquet" and pq:
            parquet_profile = self._profile_parquet_metadata(files)
            schema.update(parquet_profile["schema"])
            row_count = parquet_profile["row_count"]
            sources_used.append("parquet_row_group_stats")
            columns.update(parquet_profile["columns"])
        elif fmt == "delta":
            sources_used.append("delta_log_stats")
            warnings.append("delta_log_stats_limited_to_local_file_inspection")
        elif fmt == "parquet":
            warnings.append("pyarrow_not_available_for_parquet_metadata")

        # Pushdown path: for plain CSV files, compute row counts, null counts,
        # min/max, and observed values in DuckDB SQL over read_csv instead of
        # materializing rows in Python. The legacy Polars path remains the
        # fallback for malformed files, unmapped dtypes, or a missing duckdb.
        pushdown_done = False
        if (
            self.pushdown
            and fmt == "csv"
            and duckdb is not None
            and pl is not None
            and len(files) == 1
            and files[0].is_file()
        ):
            try:
                pushed = self._profile_csv_duckdb(
                    files[0], sample_rows=sample_rows, exact=exact
                )
            except Exception as exc:
                warnings.append(f"duckdb_pushdown_failed:{type(exc).__name__}:{exc}")
                pushed = None
            if pushed is not None:
                schema.update(pushed["schema"])
                row_count = pushed["row_count"]
                columns.update(pushed["columns"])
                sources_used.extend(pushed["sources_used"])
                pushdown_done = True

        if not pushdown_done and pl and files:
            try:
                lf = _scan_with_polars(target, fmt)
                polars_schema = {name: str(dtype) for name, dtype in lf.collect_schema().items()}
                schema.update(polars_schema)
                if row_count is None:
                    row_count = lf.select(pl.len()).collect().item()
                sample_columns = self._profile_polars(lf.head(sample_rows), source="sample_profile")
                columns.update(_merge_columns(columns, sample_columns))
                sources_used.append("sample_profile")
                if exact:
                    exact_columns = self._profile_polars(lf, source="exact_scan")
                    columns.update(_merge_columns(columns, exact_columns))
                    sources_used.append("exact_scan")

                nested_sample, nested_warnings = self._profile_nested_leaves(
                    lf.head(sample_rows), source="sample_profile"
                )
                warnings.extend(nested_warnings)
                nested_leaf_columns.update(_merge_columns(nested_leaf_columns, nested_sample))
                if exact and nested_sample:
                    nested_exact, nested_exact_warnings = self._profile_nested_leaves(
                        lf, source="exact_scan"
                    )
                    warnings.extend(nested_exact_warnings)
                    nested_leaf_columns.update(_merge_columns(nested_leaf_columns, nested_exact))
            except Exception as exc:
                warnings.append(f"polars_profile_failed:{type(exc).__name__}:{exc}")
        elif not pushdown_done and not pl:
            warnings.append("polars_not_available_for_sample_or_exact_profile")

        column_list = [
            columns.get(name)
            or ColumnProfile(name=name, dtype=dtype, source="schema")
            for name, dtype in schema.items()
        ]
        recommendations = [
            self.recommend_downcast(col) for col in column_list if _is_numeric_dtype(col.dtype)
        ]

        return DatasetProfile(
            path=str(target),
            format=fmt,
            row_count=row_count,
            file_count=len(files),
            size_bytes=size_bytes,
            schema=schema,
            columns=column_list,
            downcast_recommendations=recommendations,
            sources_used=list(dict.fromkeys(sources_used)),
            warnings=warnings,
            nested_leaf_columns=list(nested_leaf_columns.values()),
        )

    def recommend_downcast(self, column: ColumnProfile) -> DowncastRecommendation:
        dtype = column.dtype
        lo = column.authoritative_min()
        hi = column.authoritative_max()
        exact_bounds = column.exact_min is not None and column.exact_max is not None

        if _is_integer_dtype(dtype):
            if self.downcast_policy.require_exact_bounds and not exact_bounds:
                return DowncastRecommendation(
                    column=column.name,
                    current_dtype=dtype,
                    recommended_dtype=None,
                    decision="needs_exact_bounds",
                    reason="Integer downcast requires exact min/max proof.",
                    evidence={"min": lo, "max": hi, "source": column.source},
                )
            target = _smallest_integer_dtype(lo, hi, unsigned=_is_unsigned_dtype(dtype))
            decision = "no_change" if target is None or target == dtype else "recommend"
            return DowncastRecommendation(
                column=column.name,
                current_dtype=dtype,
                recommended_dtype=target,
                decision=decision,
                reason="Lossless integer downcast based on exact min/max bounds.",
                evidence={"min": lo, "max": hi, "exact_bounds": exact_bounds},
            )

        if _is_float_or_decimal_dtype(dtype):
            return DowncastRecommendation(
                column=column.name,
                current_dtype=dtype,
                recommended_dtype=None,
                decision="approval_required",
                reason="Float/decimal downcast can change precision and requires explicit approval.",
                requires_approval=True,
                evidence={"min": lo, "max": hi, "exact_bounds": exact_bounds},
            )

        return DowncastRecommendation(
            column=column.name,
            current_dtype=dtype,
            recommended_dtype=None,
            decision="unsupported",
            reason="No safe downcast policy for this dtype.",
        )

    def _profile_parquet_metadata(self, files: list[Path]) -> dict[str, Any]:
        schema: dict[str, str] = {}
        row_count = 0
        columns: dict[str, ColumnProfile] = {}
        for file in files:
            metadata = pq.ParquetFile(file).metadata
            row_count += metadata.num_rows
            arrow_schema = pq.ParquetFile(file).schema_arrow
            for arrow_field in arrow_schema:
                schema[arrow_field.name] = str(arrow_field.type)
                columns.setdefault(
                    arrow_field.name,
                    ColumnProfile(
                        name=arrow_field.name,
                        dtype=str(arrow_field.type),
                        source="parquet_row_group_stats",
                    ),
                )

            for row_group_idx in range(metadata.num_row_groups):
                row_group = metadata.row_group(row_group_idx)
                for column_idx in range(row_group.num_columns):
                    chunk = row_group.column(column_idx)
                    stats = chunk.statistics
                    name = chunk.path_in_schema
                    if not stats or not stats.has_min_max:
                        continue
                    current = columns.get(name) or ColumnProfile(
                        name=name,
                        dtype=schema.get(name, "unknown"),
                        source="parquet_row_group_stats",
                    )
                    metadata_min = _safe_min(current.metadata_min, stats.min)
                    metadata_max = _safe_max(current.metadata_max, stats.max)
                    columns[name] = ColumnProfile(
                        name=current.name,
                        dtype=current.dtype,
                        nullable=current.nullable,
                        sample_min=current.sample_min,
                        sample_max=current.sample_max,
                        exact_min=current.exact_min,
                        exact_max=current.exact_max,
                        metadata_min=metadata_min,
                        metadata_max=metadata_max,
                        null_count=current.null_count,
                        sample_values=current.sample_values,
                        source="parquet_row_group_stats",
                    )
        return {"schema": schema, "row_count": row_count, "columns": columns}

    def _profile_polars(self, lf: Any, source: str) -> dict[str, ColumnProfile]:
        schema = lf.collect_schema()
        exprs = []
        for name, dtype in schema.items():
            if _is_numeric_dtype(str(dtype)) or _is_temporal_dtype(str(dtype)):
                exprs.extend(
                    [
                        pl.col(name).min().alias(f"{name}__min"),
                        pl.col(name).max().alias(f"{name}__max"),
                        pl.col(name).null_count().alias(f"{name}__null_count"),
                    ]
                )
        stats = lf.select(exprs).collect().row(0, named=True) if exprs else {}
        columns: dict[str, ColumnProfile] = {}
        for name, dtype in schema.items():
            dtype_str = str(dtype)
            kwargs: dict[str, Any] = {
                "name": name,
                "dtype": dtype_str,
                "null_count": stats.get(f"{name}__null_count"),
                "source": source,
            }
            if source == "sample_profile":
                kwargs["sample_min"] = stats.get(f"{name}__min")
                kwargs["sample_max"] = stats.get(f"{name}__max")
                kwargs["sample_values"] = _sample_values(lf, name)
            elif source == "exact_scan":
                kwargs["exact_min"] = stats.get(f"{name}__min")
                kwargs["exact_max"] = stats.get(f"{name}__max")
            columns[name] = ColumnProfile(**kwargs)
        return columns

    def _profile_nested_leaves(
        self, lf: Any, source: str
    ) -> tuple[dict[str, ColumnProfile], list[str]]:
        """Discover and profile fields nested inside Struct/List columns (e.g.
        a JSON-sourced ``metadata`` column). Additive: never touches the flat
        ``schema``/``columns`` a physical-column consumer already relies on.
        Leaves that live inside an array element (List[Struct]) get
        name/dtype discovery only -- no ``.explode()``-based stats, to avoid
        row fan-out on an untrusted payload; full stats only happen after
        explicit human-confirmed promotion (see silver_contract.py).
        """
        schema = lf.collect_schema()
        warnings: list[str] = []
        leaf_specs: list[tuple[str, str, bool]] = []
        chains: dict[str, Any] = {}
        for name, dtype in schema.items():
            if not isinstance(dtype, (pl.Struct, pl.List)):
                continue
            for dot_path, leaf_dtype, is_array, capped in _iter_nested_leaf_paths(dtype, name):
                leaf_specs.append((dot_path, leaf_dtype, is_array))
                if capped:
                    warnings.append(
                        f"nested_leaf_discovery_capped_at_depth:{_MAX_NESTED_DEPTH}:{name}"
                    )
                if not is_array:
                    chains[dot_path] = _nested_field_chain(dot_path)

        if not leaf_specs:
            return {}, warnings

        columns: dict[str, ColumnProfile] = {
            dot_path: ColumnProfile(
                name=dot_path,
                dtype=leaf_dtype,
                null_count=None,
                sample_values=[],
                source="nested_leaf_list_element",
            )
            for dot_path, leaf_dtype, is_array in leaf_specs
            if is_array
        }

        struct_leaves = [(p, d) for p, d, is_array in leaf_specs if not is_array]
        exprs = []
        for dot_path, leaf_dtype in struct_leaves:
            chain = chains[dot_path]
            exprs.append(chain.null_count().alias(f"{dot_path}__null_count"))
            if _is_numeric_dtype(leaf_dtype) or _is_temporal_dtype(leaf_dtype):
                exprs.append(chain.min().alias(f"{dot_path}__min"))
                exprs.append(chain.max().alias(f"{dot_path}__max"))

        stats: dict[str, Any] = {}
        if exprs:
            try:
                stats = lf.select(exprs).collect().row(0, named=True)
            except Exception as exc:
                warnings.append(f"nested_leaf_stats_failed:{type(exc).__name__}:{exc}")
                stats = {}

        for dot_path, leaf_dtype in struct_leaves:
            kwargs: dict[str, Any] = {
                "name": dot_path,
                "dtype": leaf_dtype,
                "null_count": stats.get(f"{dot_path}__null_count"),
                "source": "nested_leaf_struct",
            }
            if source == "sample_profile":
                kwargs["sample_min"] = stats.get(f"{dot_path}__min")
                kwargs["sample_max"] = stats.get(f"{dot_path}__max")
                kwargs["sample_values"] = _nested_sample_values(lf, chains[dot_path])
            elif source == "exact_scan":
                kwargs["exact_min"] = stats.get(f"{dot_path}__min")
                kwargs["exact_max"] = stats.get(f"{dot_path}__max")
            columns[dot_path] = ColumnProfile(**kwargs)
        return columns, warnings


    def _profile_csv_duckdb(
        self,
        file: Path,
        *,
        sample_rows: int,
        exact: bool,
    ) -> dict[str, Any]:
        """Profile one CSV by pushing aggregations down to DuckDB SQL.

        The schema (and therefore every dtype string in the artifact) still
        comes from Polars so the profile is value-identical to the legacy
        path; DuckDB is forced onto those dtypes via a per-column ``types``
        override. Aggregates run over a single sample materialization:
        - row_count: ``count(*)`` over the full file;
        - null_count / min / max: one aggregate SELECT over the first
          ``sample_rows`` rows (full file when ``exact``);
        - sample_values: ``SELECT DISTINCT ... ORDER BY ... LIMIT 8`` per
          column, sorted for deterministic byte-identical output.
        Any error (malformed file, header mismatch, unmapped dtype) raises and
        the caller falls back to the legacy Polars path.
        """
        schema = {
            name: str(dtype)
            for name, dtype in pl.scan_csv(str(file)).collect_schema().items()
        }
        duck_types: dict[str, str] = {}
        for name, dtype in schema.items():
            mapped = _duckdb_type_for_polars(dtype)
            if mapped is None:
                raise ValueError(f"unmapped_polars_dtype:{name}:{dtype}")
            duck_types[name] = mapped

        path_literal = "'" + str(file).replace("'", "''") + "'"
        types_literal = "{" + ", ".join(
            "'" + name.replace("'", "''") + "': '" + duck_type + "'"
            for name, duck_type in duck_types.items()
        ) + "}"
        source = f"read_csv_auto({path_literal}, types={types_literal})"

        conn = duckdb.connect()
        try:
            conn.execute("SET preserve_insertion_order=true")
            described = conn.execute(f"DESCRIBE SELECT * FROM {source}").fetchall()
            duck_names = [row[0] for row in described]
            if duck_names != list(schema):
                raise ValueError(
                    f"duckdb_polars_header_mismatch:{duck_names}!={list(schema)}"
                )
            row_count = int(conn.execute(f"SELECT count(*) FROM {source}").fetchone()[0])
            conn.execute(
                "CREATE OR REPLACE TEMP TABLE __ws_profile_sample AS "
                f"SELECT * FROM {source} LIMIT {int(sample_rows)}"
            )

            stat_names = [
                name
                for name, dtype in schema.items()
                if _is_numeric_dtype(dtype) or _is_temporal_dtype(dtype)
            ]
            stats = self._duckdb_column_stats(conn, "__ws_profile_sample", stat_names)
            # null_count is always computed over the FULL file, regardless of
            # `exact` -- the same one-aggregate-query DuckDB pushdown
            # technique, cheap at any file size since DuckDB streams/pushes
            # the aggregation rather than materializing rows. Unlike min/max
            # (which already have an honest sample_min/max vs exact_min/max
            # split downstream consumers can distinguish), null_count has no
            # such split -- it's a single field, so leaving it sample-based
            # by default silently understated the real null rate on any file
            # where nulls aren't uniformly distributed across row order.
            # min/max semantics are deliberately UNCHANGED: sample_min/max
            # always come from the sample; exact_min/max only when
            # `exact=True` -- that distinction stays exactly as it was.
            full_stats = self._duckdb_column_stats(conn, f"(SELECT * FROM {source})", stat_names)
            exact_stats = full_stats if exact else {}

            distinct_selects = ", ".join(
                f"COUNT(DISTINCT {_quote_ident(name)}) AS {_quote_ident(name + '__distinct')}"
                for name in schema
            )
            distinct_row = conn.execute(f"SELECT {distinct_selects} FROM {source}").fetchone()
            distinct_counts = dict(zip([f"{name}__distinct" for name in schema], distinct_row))

            columns: dict[str, ColumnProfile] = {}
            for name, dtype_str in schema.items():
                quoted = _quote_ident(name)
                value_rows = conn.execute(
                    f"SELECT DISTINCT {quoted} AS v FROM __ws_profile_sample "
                    f"WHERE {quoted} IS NOT NULL ORDER BY v LIMIT 8"
                ).fetchall()
                sample_values = [_json_safe_value(row[0]) for row in value_rows]
                col_stats = stats.get(name) or {}
                col_exact = exact_stats.get(name) or {}
                col_full = full_stats.get(name) or {}
                unique_count = distinct_counts.get(f"{name}__distinct")
                cardinality = (
                    (unique_count / row_count) if unique_count is not None and row_count else None
                )
                columns[name] = ColumnProfile(
                    name=name,
                    dtype=dtype_str,
                    null_count=col_full.get("null_count"),
                    sample_values=sample_values,
                    sample_min=col_stats.get("min"),
                    sample_max=col_stats.get("max"),
                    exact_min=col_exact.get("min"),
                    exact_max=col_exact.get("max"),
                    cardinality=cardinality,
                    value_pattern=_infer_value_pattern(sample_values),
                    profile_tier="raw",
                    source="exact_scan" if exact else "sample_profile",
                )
        finally:
            conn.close()

        sources_used = ["sample_profile"]
        if exact:
            sources_used.append("exact_scan")
        sources_used.append("duckdb_pushdown")
        return {
            "schema": schema,
            "row_count": row_count,
            "columns": columns,
            "sources_used": sources_used,
        }

    @staticmethod
    def _duckdb_column_stats(
        conn: Any,
        relation: str,
        stat_names: list[str],
    ) -> dict[str, dict[str, Any]]:
        """min/max/null_count for the given columns in ONE aggregate query."""
        if not stat_names:
            return {}
        exprs: list[str] = []
        for name in stat_names:
            quoted = _quote_ident(name)
            exprs.extend(
                [f"min({quoted})", f"max({quoted})", f"count(*) - count({quoted})"]
            )
        row = conn.execute(f"SELECT {', '.join(exprs)} FROM {relation}").fetchone()
        stats: dict[str, dict[str, Any]] = {}
        for idx, name in enumerate(stat_names):
            stats[name] = {
                "min": row[3 * idx],
                "max": row[3 * idx + 1],
                "null_count": int(row[3 * idx + 2]),
            }
        return stats


def _quote_ident(name: str) -> str:
    return '"' + name.replace('"', '""') + '"'


def _merge_columns(
    existing: dict[str, ColumnProfile],
    incoming: dict[str, ColumnProfile],
) -> dict[str, ColumnProfile]:
    merged: dict[str, ColumnProfile] = {}
    for name, new in incoming.items():
        old = existing.get(name)
        if not old:
            merged[name] = new
            continue
        merged[name] = ColumnProfile(
            name=name,
            dtype=new.dtype or old.dtype,
            nullable=new.nullable if new.nullable is not None else old.nullable,
            sample_values=new.sample_values or old.sample_values,
            sample_min=new.sample_min if new.sample_min is not None else old.sample_min,
            sample_max=new.sample_max if new.sample_max is not None else old.sample_max,
            exact_min=new.exact_min if new.exact_min is not None else old.exact_min,
            exact_max=new.exact_max if new.exact_max is not None else old.exact_max,
            metadata_min=old.metadata_min,
            metadata_max=old.metadata_max,
            null_count=new.null_count if new.null_count is not None else old.null_count,
            source=new.source,
        )
    return merged


def _sample_values(lf: Any, column: str, limit: int = 8) -> list[Any]:
    try:
        values = (
            lf.select(pl.col(column).drop_nulls().unique().head(limit).alias(column))
            .collect()
            .get_column(column)
            .to_list()
        )
    except Exception:
        return []
    return [_json_safe_value(value) for value in values]


def _json_safe_value(value: Any) -> Any:
    if hasattr(value, "isoformat"):
        return value.isoformat()
    return value


def _iter_nested_leaf_paths(
    dtype: Any, prefix: str, depth: int = 0
) -> list[tuple[str, str, bool, bool]]:
    """Recursively walk a Polars dtype, yielding (dot_path, leaf_dtype_str,
    is_inside_array, capped) for every leaf inside a Struct/List.

    A Struct field is joined as ``prefix.field_name``; a List's inner dtype is
    joined as ``prefix[]`` (the ``[]`` marker flags "this leaf lives inside an
    array element", read by callers to decide stats eligibility). Capped at
    ``_MAX_NESTED_DEPTH`` -- a pathological/deeply-recursive payload yields one
    opaque leaf for the unexpanded remainder (``capped=True``) instead of
    hanging or dropping it silently.
    """
    if isinstance(dtype, pl.Struct):
        if depth >= _MAX_NESTED_DEPTH:
            return [(prefix, str(dtype), "[]" in prefix, True)]
        leaves: list[tuple[str, str, bool, bool]] = []
        for f in dtype.fields:
            child_prefix = f"{prefix}.{f.name}" if prefix else f.name
            leaves.extend(_iter_nested_leaf_paths(f.dtype, child_prefix, depth + 1))
        return leaves
    if isinstance(dtype, pl.List):
        if depth >= _MAX_NESTED_DEPTH:
            return [(prefix, str(dtype), True, True)]
        return _iter_nested_leaf_paths(dtype.inner, f"{prefix}[]", depth + 1)
    return [(prefix, str(dtype), "[]" in prefix, False)]


def _nested_field_chain(dot_path: str) -> Any:
    """Build a ``pl.col(...).struct.field(...)...`` expression chain for a
    struct-only dot path (never contains ``[]`` -- array-element leaves are
    name/dtype discovery only and never reach this function)."""
    segments = dot_path.split(".")
    expr = pl.col(segments[0])
    for seg in segments[1:]:
        expr = expr.struct.field(seg)
    return expr


def _nested_sample_values(lf: Any, chain: Any, limit: int = 8) -> list[Any]:
    try:
        values = (
            lf.select(chain.drop_nulls().unique().head(limit).alias("_nested_sample"))
            .collect()
            .get_column("_nested_sample")
            .to_list()
        )
    except Exception:
        return []
    return [_json_safe_value(value) for value in values]


def _detect_format(path: Path) -> str:
    if path.is_dir() and (path / "_delta_log").exists():
        return "delta"
    if path.is_dir() and any(path.rglob("*.parquet")):
        return "parquet"
    suffix = path.suffix.lower()
    if suffix in {".parquet", ".pq"}:
        return "parquet"
    if suffix == ".csv":
        return "csv"
    if suffix in {".json", ".ndjson"}:
        return suffix.lstrip(".")
    return "unknown"


def _list_data_files(path: Path, fmt: str) -> list[Path]:
    if path.is_file():
        return [path]
    if not path.exists():
        return []
    if fmt == "parquet":
        return list(path.rglob("*.parquet"))
    if fmt == "csv":
        return list(path.rglob("*.csv"))
    return [file for file in path.rglob("*") if file.is_file()]


def _scan_with_polars(path: Path, fmt: str) -> Any:
    if fmt == "parquet":
        return pl.scan_parquet(str(path))
    if fmt == "delta":
        return pl.scan_delta(str(path))
    if fmt == "csv":
        return pl.scan_csv(str(path))
    if fmt == "json":
        return pl.scan_ndjson(str(path))
    if fmt == "ndjson":
        return pl.scan_ndjson(str(path))
    raise ValueError(f"Unsupported profile format: {fmt}")


def _smallest_integer_dtype(lo: Any, hi: Any, unsigned: bool = False) -> str | None:
    if lo is None or hi is None:
        return None
    try:
        lo_int = int(lo)
        hi_int = int(hi)
    except (TypeError, ValueError):
        return None
    bounds = UNSIGNED_BOUNDS if unsigned or lo_int >= 0 else INTEGER_BOUNDS
    for dtype, min_value, max_value in bounds:
        if min_value <= lo_int and hi_int <= max_value:
            return dtype
    return bounds[-1][0]


def _is_composite_dtype(dtype: str) -> bool:
    # A Struct/List `str(dtype)` repr embeds its inner field dtypes/names
    # verbatim (e.g. "Struct({'update_date': Int64})") -- naive substring
    # matching on that repr false-positives as numeric/temporal on almost any
    # real composite column. Composite dtypes are never eligible for a scalar
    # min/max/downcast check; nested fields get their own leaf-level check via
    # _iter_nested_leaf_paths / _profile_nested_leaves instead.
    return dtype.strip().startswith(("Struct(", "List(", "Array("))


def _is_numeric_dtype(dtype: str) -> bool:
    if _is_composite_dtype(dtype):
        return False
    value = dtype.lower()
    return any(token in value for token in ("int", "uint", "float", "decimal", "double"))


def _is_integer_dtype(dtype: str) -> bool:
    value = dtype.lower()
    return "int" in value and "float" not in value


def _is_unsigned_dtype(dtype: str) -> bool:
    return dtype.lower().startswith("u")


def _is_float_or_decimal_dtype(dtype: str) -> bool:
    value = dtype.lower()
    return any(token in value for token in ("float", "double", "decimal"))


def _is_temporal_dtype(dtype: str) -> bool:
    if _is_composite_dtype(dtype):
        return False
    value = dtype.lower()
    return "date" in value or "time" in value


def _safe_min(left: Any, right: Any) -> Any:
    if left is None:
        return right
    if right is None:
        return left
    return left if left <= right else right


def _safe_max(left: Any, right: Any) -> Any:
    if left is None:
        return right
    if right is None:
        return left
    return left if left >= right else right
