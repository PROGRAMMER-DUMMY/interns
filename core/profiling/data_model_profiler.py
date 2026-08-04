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
    # Named cardinality_ratio, NOT cardinality: contracts.py/_ratio_from_stats
    # and data_understanding.py/_DISTINCT_KEYS both already treat a literal
    # "cardinality" key as an ABSOLUTE distinct count -- this field is a 0-1
    # ratio, so it must not collide with that pre-existing, unrelated key name.
    cardinality_ratio: float | None = None
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
            exact_stats = (
                self._duckdb_column_stats(conn, f"(SELECT * FROM {source})", stat_names)
                if exact
                else {}
            )

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
                unique_count = distinct_counts.get(f"{name}__distinct")
                cardinality_ratio = (
                    (unique_count / row_count) if unique_count is not None and row_count else None
                )
                columns[name] = ColumnProfile(
                    name=name,
                    dtype=dtype_str,
                    null_count=(
                        col_exact.get("null_count")
                        if exact and name in exact_stats
                        else col_stats.get("null_count")
                    ),
                    sample_values=sample_values,
                    sample_min=col_stats.get("min"),
                    sample_max=col_stats.get("max"),
                    exact_min=col_exact.get("min"),
                    exact_max=col_exact.get("max"),
                    source="exact_scan" if exact else "sample_profile",
                    cardinality_ratio=cardinality_ratio,
                    value_pattern=_infer_value_pattern(sample_values),
                    profile_tier="raw",
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


_CURRENCY_2DP_RE = re.compile(r"^\d+\.\d{2}$")


def _is_currency_2dp(value: Any) -> bool:
    """Money-shaped: a value carrying at most 2 decimal places.

    A `Float64` money column reaches here as a Python float, and
    `str(100.50) == "100.5"` -- the repr drops the trailing zero, so a
    regex demanding exactly two decimal digits never matches real profiled
    currency data. Floats are therefore judged on precision instead:
    100.50/25.00/9.99 round-trip through 2dp, 3.14159/100.567 do not.
    Integers are excluded -- they round-trip trivially, but a count or key
    column is not 2-decimal-place money. Strings keep the regex, since some
    sources deliver pre-formatted currency text.
    """
    if isinstance(value, float):
        return abs(round(value, 2) - value) < 1e-9
    if isinstance(value, (int, bool)):
        return False
    return bool(_CURRENCY_2DP_RE.match(str(value)))


# Shape checks only -- digits/letters/separators, never a business term.
# currency_2dp needs the value's real type (see above); the rest are
# string-shape by nature and match against str(value).
_VALUE_PATTERN_CHECKS: list[tuple[str, "Callable[[Any], bool]"]] = [
    ("currency_2dp", _is_currency_2dp),
    ("iso_date", lambda v: bool(re.match(r"^\d{4}-\d{2}-\d{2}$", str(v)))),
    ("prefixed_numeric_code", lambda v: bool(re.match(r"^[A-Za-z]+[-_]?\d+$", str(v)))),
    ("fixed_length_alnum", lambda v: bool(re.match(r"^[A-Za-z0-9]{6,12}$", str(v)))),
]


def _infer_value_pattern(sample_values: list[Any]) -> str | None:
    """Named structural pattern shared by >=80% of non-null sample values.

    Evidence-driven, no domain vocabulary: matches shape (digits/letters/
    separators), never a specific business term. Returns None when no
    pattern clears the 80% bar -- a mixed-shape column reports no pattern
    rather than a misleading weak one.
    """
    values = [v for v in sample_values if v is not None and str(v).strip()]
    if not values:
        return None
    for pattern_name, matches_pattern in _VALUE_PATTERN_CHECKS:
        matches = sum(1 for v in values if matches_pattern(v))
        if matches / len(values) >= 0.8:
            return pattern_name
    return None


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
            cardinality_ratio=(
                new.cardinality_ratio
                if new.cardinality_ratio is not None
                else old.cardinality_ratio
            ),
            value_pattern=new.value_pattern if new.value_pattern is not None else old.value_pattern,
            profile_tier=new.profile_tier or old.profile_tier,
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


def _is_numeric_dtype(dtype: str) -> bool:
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
