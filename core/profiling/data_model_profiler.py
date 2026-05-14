"""
Metadata-first data model profiling.

The profiler prefers cheap metadata before scanning data:
catalog stats -> Delta log stats -> Parquet row-group stats -> sample profile
-> exact scan. The current implementation supports local Parquet/CSV/Delta-like
paths and is deliberately safe when optional dependencies are unavailable.
"""
from __future__ import annotations

import json
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
    def __init__(self, downcast_policy: DowncastPolicy | None = None):
        self.downcast_policy = downcast_policy or DowncastPolicy()

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

        if pl and files:
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
        elif not pl:
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
