"""
================================================================================
 enterprise_profiler.py
 Enterprise Async Stratified Sampler & Profiler (Polars + PySpark + SQL)
================================================================================
"""

import argparse
import asyncio
import logging
import time
import os
import re
from abc import ABC, abstractmethod
from pathlib import Path
from typing import Optional, List, Dict, Any, Tuple, Type

from tools.profiler_utils import (
    clamp_score as util_clamp_score,
    human_size as util_human_size,
    is_numeric_dtype as util_is_numeric_dtype,
    mean_safe as util_mean_safe,
    parse_column_list as util_parse_column_list,
    pct_label as util_pct_label,
    safe_filename_part as util_safe_filename_part,
    unique_preserve_order as util_unique_preserve_order,
)

# Configure rich logging if available
try:
    from rich.console import Console
    from rich.logging import RichHandler
    logging.basicConfig(
        level="INFO",
        format="%(message)s",
        datefmt="[%X]",
        handlers=[RichHandler(rich_tracebacks=True)]
    )
    console = Console()
except ImportError:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")
    console = None

logger = logging.getLogger("profiler")

# Optional Imports (Graceful Degradation)
try:
    import polars as pl
except ImportError:
    pl = None

try:
    from pyspark.sql import SparkSession
    import pyspark.sql.functions as F
    from pyspark.sql.window import Window
except ImportError:
    SparkSession = None

try:
    import sqlalchemy as sa
except ImportError:
    sa = None

try:
    import numpy as np
except ImportError:
    np = None

try:
    from sklearn.ensemble import RandomForestClassifier, RandomForestRegressor
    from sklearn.inspection import permutation_importance
    from sklearn.metrics import accuracy_score, f1_score, mean_absolute_error, r2_score
    from sklearn.model_selection import train_test_split
except ImportError:
    RandomForestClassifier = None
    RandomForestRegressor = None
    permutation_importance = None
    accuracy_score = None
    f1_score = None
    mean_absolute_error = None
    r2_score = None
    train_test_split = None

try:
    import shap
except ImportError:
    shap = None


# ══════════════════════════════════════════════════════════════════════════════
#  UTILITY HELPERS
# ══════════════════════════════════════════════════════════════════════════════

def human_size(n_bytes: int) -> str:
    return util_human_size(n_bytes)

def pct_label(pct: float) -> str:
    """Return a stable filesystem-safe label for sample percentages."""
    return util_pct_label(pct)

def is_numeric_dtype(dtype: Any) -> bool:
    return util_is_numeric_dtype(dtype)

def safe_filename_part(value: str) -> str:
    return util_safe_filename_part(value)

def parse_column_list(value: Optional[str]) -> List[str]:
    return util_parse_column_list(value)

def clamp_score(value: float) -> float:
    return util_clamp_score(value)

def mean_safe(values: List[float], default: float = 0.0) -> float:
    return util_mean_safe(values, default)

def unique_preserve_order(values: List[str]) -> List[str]:
    return util_unique_preserve_order(values)

ID_PATTERN = re.compile(r"^(id|.*_id|.*_key|uuid|guid|npi|.*_npi)$", re.IGNORECASE)
ZIP_PATTERN = re.compile(r"^\d{5}(-\d{4})?$")
NULL_WARN_THRESHOLD = 0.30
NULL_SKIP_THRESHOLD = 0.70
STRATA_HARD_LIMIT = 1_000
RELATIONSHIP_ROW_LIMIT = 2_000_000
RELATIONSHIP_COLUMN_LIMIT = 500

POLARS_DTYPE_MAP = {
    "str": pl.String if pl else None,
    "string": pl.String if pl else None,
    "utf8": pl.String if pl else None,
    "int": pl.Int64 if pl else None,
    "int64": pl.Int64 if pl else None,
    "float": pl.Float64 if pl else None,
    "float64": pl.Float64 if pl else None,
    "bool": pl.Boolean if pl else None,
    "boolean": pl.Boolean if pl else None,
    "date": pl.Date if pl else None,
    "datetime": pl.Datetime if pl else None,
}

def parse_schema_overrides(value: Optional[str]) -> Dict[str, Any]:
    overrides = {}
    if not value:
        return overrides
    for item in value.split(","):
        if not item.strip():
            continue
        if ":" not in item:
            raise ValueError(f"Invalid schema override '{item}'. Use column:type.")
        col, dtype_name = [part.strip() for part in item.split(":", 1)]
        dtype = POLARS_DTYPE_MAP.get(dtype_name.lower())
        if dtype is None:
            raise ValueError(f"Unsupported dtype '{dtype_name}' for schema override '{col}'.")
        overrides[col] = dtype
    return overrides

def validate_s3_config(path: str) -> None:
    if not (path.startswith("s3://") or path.startswith("s3a://")):
        return
    missing = [
        key for key in ["AWS_ACCESS_KEY_ID", "AWS_SECRET_ACCESS_KEY"]
        if not os.environ.get(key)
    ]
    if not os.environ.get("AWS_DEFAULT_REGION") and not os.environ.get("AWS_REGION"):
        missing.append("AWS_DEFAULT_REGION")
    if missing:
        raise EnvironmentError(
            f"S3 path detected but missing environment variables: {', '.join(missing)}"
        )

def validate_spark_config() -> None:
    if SparkSession is None:
        raise RuntimeError("Spark is required for this path but PySpark is not installed.")

def fix_inferred_schema(df: Any, schema_overrides: Optional[Dict[str, Any]] = None) -> Any:
    """Detect ID-like/ZIP-like integer columns and high-null-ratio float
    columns, cast them to String. Works on both a LazyFrame and an eager
    DataFrame -- ``df`` may be either. The two data-dependent probes (ZIP-
    pattern sample match rate, float null ratio) are batched into ONE bounded
    query via ``.select(...).collect()`` rather than per-column eager Series
    access, so this stays scale-safe on a lazily-scanned file: it never
    materializes the whole file, only a `.head(100)` sample per integer
    column and a null-count/length aggregate per float column, all in one
    pass. Semantics are unchanged from the original eager version -- same
    thresholds, same sample size, same full-column null ratio (not a
    sampled one) for the float check.
    """
    if not pl:
        return df
    is_lazy = hasattr(df, "collect_schema")
    schema = df.collect_schema() if is_lazy else {c: df.schema[c] for c in df.columns}

    overrides: Dict[str, Any] = {}
    probe_exprs = []
    int_cols = []
    float_cols = []
    for col, dtype in schema.items():
        if ID_PATTERN.match(col):
            overrides[col] = pl.String
            continue
        if dtype in (pl.Int64, pl.Int32, pl.UInt64, pl.UInt32):
            int_cols.append(col)
            probe_exprs.append(
                pl.col(col).drop_nulls().head(100).cast(pl.String)
                .str.contains(ZIP_PATTERN.pattern).mean().alias(f"__zip__{col}")
            )
        elif dtype in (pl.Float64, pl.Float32):
            float_cols.append(col)
            probe_exprs.append(pl.col(col).null_count().alias(f"__nulls__{col}"))
            probe_exprs.append(pl.col(col).len().alias(f"__len__{col}"))

    if probe_exprs:
        probe_lf = df if is_lazy else df.lazy()
        stats = probe_lf.select(probe_exprs).collect().row(0, named=True)
        for col in int_cols:
            matches = stats.get(f"__zip__{col}")
            if matches and matches > 0.8:
                overrides[col] = pl.String
        for col in float_cols:
            total = stats.get(f"__len__{col}") or 0
            nulls = stats.get(f"__nulls__{col}") or 0
            if total and (nulls / total) > 0.3:
                overrides[col] = pl.String

    if schema_overrides:
        overrides.update(schema_overrides)
    if not overrides:
        return df
    return df.with_columns([
        pl.col(col).cast(dtype, strict=False) for col, dtype in overrides.items()
        if col in schema
    ])

def load_csv(path: str, schema_overrides: Optional[Dict[str, Any]] = None) -> Any:
    lf = pl.scan_csv(path, infer_schema_length=5000, ignore_errors=True, glob=True)
    return fix_inferred_schema(lf, schema_overrides)

def load_polars_frame(path: str, schema_overrides: Optional[Dict[str, Any]] = None) -> Tuple[Any, str]:
    p = Path(path)
    ext = p.suffix.lower()
    if p.is_dir() and (p / "_delta_log").exists():
        return pl.scan_delta(str(p)), "delta"
    if p.is_dir() and list(p.rglob("*.parquet")):
        return pl.scan_parquet(str(p)), "parquet"
    if p.is_dir() and list(p.rglob("*.csv")):
        return load_csv(str(p / "*.csv"), schema_overrides), "csv"
    if ext in [".parquet", ".pq"]:
        return pl.scan_parquet(path), "parquet"
    if ext == ".csv":
        return load_csv(path, schema_overrides), "csv"
    if ext == ".json":
        return pl.read_json(path).lazy(), "json"
    if ext == ".ndjson":
        return pl.read_ndjson(path).lazy(), "ndjson"
    if ext in [".xlsx", ".xls"]:
        return pl.read_excel(path).lazy(), "excel"
    if ext == ".avro":
        return pl.read_avro(path).lazy(), "avro"
    if ext == ".orc":
        import pyarrow.orc as orc
        return pl.from_arrow(orc.read_table(path)).lazy(), "orc"
    supported = [".parquet", ".pq", ".csv", ".json", ".ndjson", ".xlsx", ".xls", ".avro", ".orc"]
    raise ValueError(f"Unsupported format: {ext or '<directory>'}. Supported: {supported}")

def null_audit_from_profile(profile_df: Any) -> Any:
    return profile_df.select([
        "column",
        ((100 - pl.col("fill_pct")) / 100).alias("null_rate"),
    ]).with_columns([
        pl.when(pl.col("null_rate") >= NULL_SKIP_THRESHOLD).then(pl.lit("excluded"))
        .when(pl.col("null_rate") >= NULL_WARN_THRESHOLD).then(pl.lit("warn"))
        .otherwise(pl.lit("ok")).alias("action"),
        pl.when(pl.col("null_rate") >= NULL_SKIP_THRESHOLD)
        .then((pl.col("null_rate") * 100).round(1).cast(pl.String) + pl.lit("% nulls - excluded from model/relationship reports"))
        .when(pl.col("null_rate") >= NULL_WARN_THRESHOLD)
        .then((pl.col("null_rate") * 100).round(1).cast(pl.String) + pl.lit("% nulls - results may be unreliable"))
        .otherwise(pl.lit("")).alias("reason"),
    ])

# ══════════════════════════════════════════════════════════════════════════════
#  ABSTRACT BASE ENGINE
# ══════════════════════════════════════════════════════════════════════════════

class AbstractDataEngine(ABC):
    """Abstract interface for all data processing backends."""

    @abstractmethod
    async def get_metadata(self) -> Dict[str, Any]:
        pass

    @abstractmethod
    async def quick_profile(self, sample_rows: int = 200_000) -> Any:
        pass

    @abstractmethod
    async def sample_data(
        self,
        pct: float,
        strat_cols: List[str],
        min_stratum_rows: int = 0,
        seed: int = 42,
        strategy: str = "stratified",
        target_col: Optional[str] = None,
        tail_cols: Optional[List[str]] = None,
        preserve_tails: bool = False,
        tail_quantile: float = 0.01,
        tail_boost: float = 3.0,
    ) -> Any:
        pass

    @abstractmethod
    async def full_profile(self, df: Any, top_fill_threshold: float) -> Dict[str, Any]:
        pass

    @abstractmethod
    async def save_outputs(self, sample_df: Any, profile: Dict[str, Any], out_dir: str, pct: float, fmt: str):
        pass


# ══════════════════════════════════════════════════════════════════════════════
#  POLARS ENGINE (Single-Node, Lightning Fast, Out-of-Core)
# ══════════════════════════════════════════════════════════════════════════════

class PolarsEngine(AbstractDataEngine):
    def __init__(self, uri: str, schema_overrides: Optional[Dict[str, Any]] = None):
        if not pl:
            raise ImportError("Polars is not installed.")
        self.uri = uri
        self.schema_overrides = schema_overrides or {}
        self.lf, self.fmt, self.size = self._init_lazy_frame(uri)

    def _init_lazy_frame(self, file_path: str) -> Tuple['pl.LazyFrame', str, int]:
        p = Path(file_path)
        size = sum(f.stat().st_size for f in p.rglob("*") if f.is_file()) if p.is_dir() else (p.stat().st_size if p.exists() else 0)
        lf, fmt = load_polars_frame(file_path, self.schema_overrides)
        return lf, fmt, size

    async def get_metadata(self) -> Dict[str, Any]:
        def _fetch():
            schema = self.lf.collect_schema()
            try:
                n_rows = self.lf.select(pl.len()).collect().item()
            except Exception:
                n_rows = None
            return {"file_size": self.size, "n_rows": n_rows, "n_cols": len(schema), "schema": schema}
        return await asyncio.to_thread(_fetch)

    async def quick_profile(self, sample_rows: int = 200_000) -> Any:
        def _profile():
            df = self.lf.head(sample_rows).collect()
            records = []
            total = df.height
            for col in df.columns:
                s = df.get_column(col)
                n_unique = s.n_unique()
                records.append({
                    "column": col,
                    "is_categorical": s.dtype in [pl.Categorical, pl.String, pl.Boolean] or (is_numeric_dtype(s.dtype) and n_unique <= 50),
                    "fill_pct": round(100 * (1 - s.null_count() / total), 2) if total else 0,
                    "n_unique": n_unique,
                    "unique_ratio": round(n_unique / total, 4) if total else 0
                })
            return pl.DataFrame(records)
        return await asyncio.to_thread(_profile)

    async def sample_data(
        self,
        pct: float,
        strat_cols: List[str],
        min_stratum_rows: int = 0,
        seed: int = 42,
        strategy: str = "stratified",
        target_col: Optional[str] = None,
        tail_cols: Optional[List[str]] = None,
        preserve_tails: bool = False,
        tail_quantile: float = 0.01,
        tail_boost: float = 3.0,
    ) -> Any:
        def _sample():
            frac = pct / 100.0
            schema = self.lf.collect_schema()
            effective_strat_cols = list(strat_cols)
            local_min_stratum_rows = min_stratum_rows
            if strategy == "random":
                effective_strat_cols = []
            if strategy == "rare" and local_min_stratum_rows == 0:
                local_min_stratum_rows = 1

            lf_work = self.lf.with_row_index("__row_idx")
            temp_cols = ["__row_idx"]

            if strategy in ["target_quantile", "hybrid"] and target_col and target_col in schema and is_numeric_dtype(schema[target_col]):
                quantiles = [i / 10 for i in range(1, 10)]
                q_values = self.lf.select([
                    pl.col(target_col).quantile(q).alias(f"q_{int(q * 100)}")
                    for q in quantiles
                ]).collect().row(0)
                edges = sorted(set(v for v in q_values if v is not None))
                if edges:
                    target_bin = pl.lit(0)
                    for edge in edges:
                        target_bin = target_bin + (pl.col(target_col) > edge).cast(pl.Int64)
                    lf_work = lf_work.with_columns(__target_bin=target_bin)
                    temp_cols.append("__target_bin")
                    if strategy == "target_quantile":
                        effective_strat_cols = ["__target_bin"]
                    else:
                        effective_strat_cols = [*effective_strat_cols, "__target_bin"]

            if not effective_strat_cols:
                lf_sampled = lf_work.with_columns(
                    __rand=(pl.col("__row_idx").hash(seed) % 1_000_000) / 1_000_000.0
                ).filter(pl.col("__rand") < frac)
            else:
                n_strata = lf_work.select(pl.struct(effective_strat_cols).n_unique().alias("__n_strata")).collect().item()
                if n_strata > STRATA_HARD_LIMIT:
                    logger.warning(
                        f"Stratified sampling skipped: {effective_strat_cols} has {n_strata:,} strata "
                        f"(limit {STRATA_HARD_LIMIT:,}). Falling back to deterministic random sample."
                    )
                    lf_sampled = lf_work.with_columns(
                        __rand=(pl.col("__row_idx").hash(seed) % 1_000_000) / 1_000_000.0
                    ).filter(pl.col("__rand") < frac)
                else:
                    lf_strat = lf_work.with_columns(__rand=pl.col("__row_idx").hash(seed)).sort([*effective_strat_cols, "__rand"])
                    lf_strat = lf_strat.with_columns(
                        __rn=pl.int_range(1, pl.len() + 1).over(effective_strat_cols),
                        __stratum_count=pl.len().over(effective_strat_cols)
                    )
                    quota = (pl.col("__stratum_count") * frac).round().cast(pl.Int64)
                    if local_min_stratum_rows > 0:
                        quota = pl.max_horizontal(local_min_stratum_rows, quota)
                    lf_sampled = lf_strat.filter(pl.col("__rn") <= quota)

            if preserve_tails:
                tail_candidates = unique_preserve_order([*(tail_cols or []), target_col or ""])
                tail_candidates = [
                    c for c in tail_candidates
                    if c in schema and is_numeric_dtype(schema[c])
                ][:12]
                if tail_candidates:
                    exprs = []
                    for col in tail_candidates:
                        exprs.extend([
                            pl.col(col).quantile(tail_quantile).alias(f"{col}__lo"),
                            pl.col(col).quantile(1 - tail_quantile).alias(f"{col}__hi"),
                        ])
                    thresholds = self.lf.select(exprs).collect().row(0, named=True)
                    tail_filter = None
                    for col in tail_candidates:
                        lo = thresholds.get(f"{col}__lo")
                        hi = thresholds.get(f"{col}__hi")
                        if lo is None or hi is None:
                            continue
                        predicate = (pl.col(col) <= lo) | (pl.col(col) >= hi)
                        tail_filter = predicate if tail_filter is None else (tail_filter | predicate)
                    if tail_filter is not None:
                        tail_frac = min(1.0, frac * max(1.0, tail_boost))
                        lf_tails = lf_work.filter(tail_filter).with_columns(
                            __tail_rand=(pl.col("__row_idx").hash(seed + 17) % 1_000_000) / 1_000_000.0
                        ).filter(pl.col("__tail_rand") < tail_frac).drop("__tail_rand")
                        lf_sampled = pl.concat([lf_sampled, lf_tails], how="diagonal").unique(subset=["__row_idx"], keep="first")

            drop_cols = [c for c in [*temp_cols, "__rand", "__rn", "__stratum_count"] if c in lf_sampled.collect_schema()]
            lf_sampled = lf_sampled.drop(drop_cols)
            try:
                return lf_sampled.collect(engine="streaming")
            except Exception:
                return lf_sampled.collect()
        return await asyncio.to_thread(_sample)

    async def full_profile(self, df: Any, top_fill_threshold: float) -> Dict[str, Any]:
        # Implementation mirrors original Polars execution but threaded for async event loop handling
        def _full():
            total = df.height
            records = []
            for col in df.columns:
                s = df.get_column(col)
                n_null = s.null_count()
                fill_pct = round(100 * (total - n_null) / total, 3) if total else 0
                records.append({
                    "column": col, "dtype": str(s.dtype), "fill_pct": fill_pct,
                    "n_unique": s.n_unique(), "above_threshold": fill_pct >= top_fill_threshold
                })
            profile_df = pl.DataFrame(records)
            return {
                "full": profile_df,
                "high_fill": profile_df.filter(pl.col("above_threshold")).sort("fill_pct", descending=True)
            }
        return await asyncio.to_thread(_full)

    async def save_outputs(self, sample_df: Any, profile: Dict[str, Any], out_dir: str, pct: float, fmt: str):
        def _save():
            out = Path(out_dir)
            out.mkdir(parents=True, exist_ok=True)
            path = out / f"sample_{pct_label(pct)}pct.{fmt}"
            if fmt == "parquet":
                sample_df.write_parquet(path)
            elif fmt == "csv":
                sample_df.write_csv(path)
            elif fmt == "delta":
                sample_df.write_delta(path)
            profile["full"].write_csv(out / "full_profile.csv")
            logger.info(f"Polars output saved to {path}")
        await asyncio.to_thread(_save)

    def _dataset_profile_sync(self, top_fill_threshold: float) -> Any:
        schema = self.lf.collect_schema()
        cols = list(schema.keys())
        exprs = [pl.len().alias("__total")]
        for col in cols:
            exprs.extend([
                pl.col(col).null_count().alias(f"{col}__null"),
                pl.col(col).n_unique().alias(f"{col}__unique"),
            ])

        stats = self.lf.select(exprs).collect().row(0, named=True)
        total = stats["__total"]
        records = []
        for col in cols:
            n_null = stats[f"{col}__null"]
            fill_pct = round(100 * (total - n_null) / total, 3) if total else 0
            records.append({
                "column": col,
                "dtype": str(schema[col]),
                "fill_pct": fill_pct,
                "n_unique": stats[f"{col}__unique"],
                "above_threshold": fill_pct >= top_fill_threshold,
            })
        return pl.DataFrame(records)

    async def dataset_profile(self, top_fill_threshold: float) -> Any:
        return await asyncio.to_thread(self._dataset_profile_sync, top_fill_threshold)

    async def save_representation_report(
        self,
        sample_df: Any,
        sample_profile: Dict[str, Any],
        out_dir: str,
        strat_cols: List[str],
        top_fill_threshold: float,
        target_col: Optional[str] = None,
        feature_cols: Optional[List[str]] = None,
        relationship_cols: Optional[List[str]] = None,
        tail_cols: Optional[List[str]] = None,
        tail_quantile: float = 0.01,
        semantic_config: Optional[Dict[str, Any]] = None,
    ):
        def _save():
            out = Path(out_dir)
            out.mkdir(parents=True, exist_ok=True)

            if semantic_config:
                validations = []
                for col, rules in semantic_config.get("columns", {}).items():
                    if col in sample_df.columns and "allowed_values" in rules:
                        allowed = set(rules["allowed_values"])
                        actual = set(sample_df.get_column(col).drop_nulls().unique().to_list())
                        invalid = actual - allowed
                        validations.append({
                            "column": col,
                            "rule": f"allowed_values: {allowed}",
                            "status": "PASS" if not invalid else "FAIL",
                            "violations": str(list(invalid)[:10]) if invalid else ""
                        })
                if validations:
                    pl.DataFrame(validations).write_csv(out / "business_validation.csv")

            dataset_profile = self._dataset_profile_sync(top_fill_threshold)
            sample_prof = sample_profile["full"]
            dataset_profile.write_csv(out / "dataset_profile.csv")
            sample_prof.write_csv(out / "sample_profile.csv")
            dataset_null_audit = null_audit_from_profile(dataset_profile)
            sample_null_audit = null_audit_from_profile(sample_prof)
            dataset_null_audit.write_csv(out / "dataset_null_audit.csv")
            sample_null_audit.write_csv(out / "sample_null_audit.csv")
            excluded_columns = set(
                dataset_null_audit.filter(pl.col("action") == "excluded").get_column("column").to_list()
            ) | set(
                sample_null_audit.filter(pl.col("action") == "excluded").get_column("column").to_list()
            )

            summary = dataset_profile.select([
                "column",
                pl.col("dtype").alias("dataset_dtype"),
                pl.col("fill_pct").alias("dataset_fill_pct"),
                pl.col("n_unique").alias("dataset_n_unique"),
            ]).join(
                sample_prof.select([
                    "column",
                    pl.col("dtype").alias("sample_dtype"),
                    pl.col("fill_pct").alias("sample_fill_pct"),
                    pl.col("n_unique").alias("sample_n_unique"),
                ]),
                on="column",
                how="left",
            ).with_columns([
                (pl.col("sample_fill_pct") - pl.col("dataset_fill_pct")).abs().alias("fill_pct_abs_delta"),
                (pl.col("sample_n_unique") / pl.col("dataset_n_unique")).fill_nan(0).alias("unique_coverage_ratio"),
            ]).sort(["fill_pct_abs_delta", "unique_coverage_ratio"], descending=[True, False])
            summary.write_csv(out / "representation_summary.csv")
            summary.select([
                "column",
                "dataset_fill_pct",
                "sample_fill_pct",
                "fill_pct_abs_delta",
                "dataset_n_unique",
                "sample_n_unique",
                "unique_coverage_ratio",
            ]).filter(
                (pl.col("fill_pct_abs_delta") > 1.0) | (pl.col("unique_coverage_ratio") < 0.5)
            ).write_csv(out / "representation_diff.csv")

            full_columns = set(self.lf.collect_schema().keys())
            feature_base = feature_cols or [c for c in sample_df.columns if c != target_col]
            feature_rows = []
            for col in feature_base:
                if col not in full_columns or col not in sample_df.columns:
                    continue
                match = summary.filter(pl.col("column") == col)
                if match.height == 0:
                    continue
                row = match.row(0, named=True)
                unique_ratio = row["dataset_n_unique"] / max(1, sample_df.height)
                feature_rows.append({
                    "column": col,
                    "dtype": row["dataset_dtype"],
                    "dataset_fill_pct": row["dataset_fill_pct"],
                    "sample_fill_pct": row["sample_fill_pct"],
                    "fill_pct_abs_delta": row["fill_pct_abs_delta"],
                    "dataset_n_unique": row["dataset_n_unique"],
                    "sample_n_unique": row["sample_n_unique"],
                    "unique_coverage_ratio": row["unique_coverage_ratio"],
                    "likely_id_or_high_cardinality": bool(unique_ratio > 0.8),
                })
            if feature_rows:
                pl.DataFrame(feature_rows).write_csv(out / "feature_info.csv")

            distribution_rows = []
            for col in strat_cols:
                if col not in sample_df.columns:
                    continue
                full_dist = self.lf.group_by(col).agg(pl.len().alias("full_count")).collect()
                sample_dist = sample_df.group_by(col).agg(pl.len().alias("sample_count"))
                full_total = full_dist.get_column("full_count").sum()
                sample_total = sample_dist.get_column("sample_count").sum()
                dist = full_dist.join(sample_dist, on=col, how="full", coalesce=True).with_columns([
                    pl.col("full_count").fill_null(0),
                    pl.col("sample_count").fill_null(0),
                ]).with_columns([
                    (pl.col("full_count") / full_total).alias("full_pct"),
                    (pl.col("sample_count") / sample_total).alias("sample_pct"),
                ]).with_columns([
                    (pl.col("sample_pct") - pl.col("full_pct")).abs().alias("abs_pct_diff"),
                ]).sort("abs_pct_diff", descending=True)
                metrics = dist.select([
                    (pl.col("abs_pct_diff").sum() / 2).alias("total_variation_distance"),
                    pl.col("abs_pct_diff").max().alias("max_abs_pct_diff"),
                ]).row(0, named=True)
                distribution_rows.append({"column": col, **metrics})
                dist.write_csv(out / f"distribution_{safe_filename_part(col)}.csv")

            if distribution_rows:
                pl.DataFrame(distribution_rows).write_csv(out / "distribution_summary.csv")

            numeric_cols = [
                name for name, dtype in self.lf.collect_schema().items()
                if is_numeric_dtype(dtype) and name in sample_df.columns and name not in excluded_columns
            ]
            numeric_rows = []
            if numeric_cols:
                quantiles = [0.01, 0.10, 0.25, 0.50, 0.75, 0.90, 0.99]
                exprs = []
                for col in numeric_cols:
                    exprs.extend([
                        pl.col(col).mean().alias(f"{col}__mean"),
                        pl.col(col).std().alias(f"{col}__std"),
                    ])
                    for q in quantiles:
                        exprs.append(pl.col(col).quantile(q).alias(f"{col}__p{int(q * 100):02d}"))

                full_stats = self.lf.select(exprs).collect().row(0, named=True)
                sample_stats = sample_df.select(exprs).row(0, named=True)
                for col in numeric_cols:
                    row = {"column": col}
                    for metric in ["mean", "std", *[f"p{int(q * 100):02d}" for q in quantiles]]:
                        full_value = full_stats[f"{col}__{metric}"]
                        sample_value = sample_stats[f"{col}__{metric}"]
                        row[f"full_{metric}"] = full_value
                        row[f"sample_{metric}"] = sample_value
                        row[f"{metric}_delta"] = None if full_value is None or sample_value is None else sample_value - full_value
                    numeric_rows.append(row)
                pl.DataFrame(numeric_rows).write_csv(out / "numeric_distribution.csv")

            numeric_by_col = {row["column"]: row for row in numeric_rows}
            target_match_score = None
            target_penalty = 0.0
            target_report_rows = []
            if target_col and target_col in numeric_by_col:
                target_row = numeric_by_col[target_col]
                iqr = target_row.get("full_p75") - target_row.get("full_p25") if target_row.get("full_p75") is not None and target_row.get("full_p25") is not None else None
                target_deltas = []
                for metric in ["mean", "p01", "p10", "p25", "p50", "p75", "p90", "p99"]:
                    full_value = target_row.get(f"full_{metric}")
                    sample_value = target_row.get(f"sample_{metric}")
                    delta = target_row.get(f"{metric}_delta")
                    scaled_delta = None
                    if iqr and iqr > 0 and delta is not None:
                        scaled_delta = min(1.0, abs(delta) / iqr)
                        target_deltas.append(scaled_delta)
                    target_report_rows.append({
                        "metric": metric,
                        "full_value": full_value,
                        "sample_value": sample_value,
                        "delta": delta,
                        "scaled_delta": scaled_delta,
                    })
                target_penalty = mean_safe(target_deltas)
                target_match_score = clamp_score(100 * (1 - target_penalty))
                pl.DataFrame(target_report_rows).write_csv(out / "target_distribution.csv")

            tail_report_rows = []
            tail_penalties = []
            tail_candidates = unique_preserve_order([*(tail_cols or []), target_col or ""])
            tail_candidates = [c for c in tail_candidates if c in numeric_cols]
            for col in tail_candidates:
                thresholds = self.lf.select([
                    pl.col(col).quantile(tail_quantile).alias("lo"),
                    pl.col(col).quantile(1 - tail_quantile).alias("hi"),
                ]).collect().row(0, named=True)
                lo = thresholds.get("lo")
                hi = thresholds.get("hi")
                if lo is None or hi is None:
                    continue
                full_stats_tail = self.lf.select([
                    (pl.col(col) <= lo).sum().alias("low_count"),
                    (pl.col(col) >= hi).sum().alias("high_count"),
                    pl.col(col).is_not_null().sum().alias("nonnull_count"),
                ]).collect().row(0, named=True)
                sample_stats_tail = sample_df.select([
                    (pl.col(col) <= lo).sum().alias("low_count"),
                    (pl.col(col) >= hi).sum().alias("high_count"),
                    pl.col(col).is_not_null().sum().alias("nonnull_count"),
                ]).row(0, named=True)
                for side in ["low", "high"]:
                    full_share = full_stats_tail[f"{side}_count"] / full_stats_tail["nonnull_count"] if full_stats_tail["nonnull_count"] else 0
                    sample_share = sample_stats_tail[f"{side}_count"] / sample_stats_tail["nonnull_count"] if sample_stats_tail["nonnull_count"] else 0
                    abs_delta = abs(sample_share - full_share)
                    tail_penalties.append(
                        min(1.0, max(0.0, full_share - sample_share) / max(full_share, 1e-9))
                    )
                    tail_report_rows.append({
                        "column": col,
                        "tail": side,
                        "threshold": lo if side == "low" else hi,
                        "full_share": full_share,
                        "sample_share": sample_share,
                        "abs_share_delta": abs_delta,
                    })
            tail_penalty = mean_safe(tail_penalties)
            tail_coverage_score = clamp_score(100 * (1 - tail_penalty)) if tail_report_rows else None
            if tail_report_rows:
                pl.DataFrame(tail_report_rows).write_csv(out / "tail_coverage.csv")

            relationship_rows = []
            relationship_candidates = relationship_cols or numeric_cols[:8]
            relationship_candidates = [
                c for c in relationship_candidates
                if c in numeric_cols and c not in excluded_columns
            ][:8]
            if np is not None and len(relationship_candidates) >= 2:
                full_rows = self.lf.select(pl.len()).collect().item()
                rel_lf = self.lf.select(relationship_candidates)
                if full_rows > RELATIONSHIP_ROW_LIMIT:
                    frac = RELATIONSHIP_ROW_LIMIT / full_rows
                    logger.warning(
                        f"[relationship] Dataset has {full_rows:,} rows. "
                        f"Using a {RELATIONSHIP_ROW_LIMIT:,}-row deterministic subset."
                    )
                    rel_lf = rel_lf.with_row_index("__row_idx").with_columns(
                        __rand=(pl.col("__row_idx").hash(42) % 1_000_000) / 1_000_000.0
                    ).filter(pl.col("__rand") < frac).drop(["__row_idx", "__rand"])
                if len(relationship_candidates) > RELATIONSHIP_COLUMN_LIMIT:
                    logger.warning(
                        f"[relationship] Column count exceeds {RELATIONSHIP_COLUMN_LIMIT}; "
                        "using the selected candidate subset."
                    )
                full_rel = rel_lf.collect()
                sample_rel = sample_df.select(relationship_candidates)
                for i, left in enumerate(relationship_candidates):
                    for right in relationship_candidates[i + 1:]:
                        full_pair = full_rel.select([left, right]).drop_nulls()
                        sample_pair = sample_rel.select([left, right]).drop_nulls()
                        if full_pair.height < 20 or sample_pair.height < 20:
                            continue
                        full_left = full_pair.get_column(left).to_numpy()
                        full_right = full_pair.get_column(right).to_numpy()
                        sample_left = sample_pair.get_column(left).to_numpy()
                        sample_right = sample_pair.get_column(right).to_numpy()
                        x_edges = np.unique(np.quantile(full_left, np.linspace(0, 1, 11)))
                        y_edges = np.unique(np.quantile(full_right, np.linspace(0, 1, 11)))
                        if len(x_edges) < 3 or len(y_edges) < 3:
                            continue
                        full_hist, _, _ = np.histogram2d(full_left, full_right, bins=[x_edges, y_edges])
                        sample_hist, _, _ = np.histogram2d(sample_left, sample_right, bins=[x_edges, y_edges])
                        full_prob = full_hist / full_hist.sum() if full_hist.sum() else full_hist
                        sample_prob = sample_hist / sample_hist.sum() if sample_hist.sum() else sample_hist
                        relationship_rows.append({
                            "left": left,
                            "right": right,
                            "binned_joint_tvd": float(np.abs(full_prob - sample_prob).sum() / 2),
                        })
                if relationship_rows:
                    pl.DataFrame(relationship_rows).sort("binned_joint_tvd", descending=True).write_csv(out / "nonlinear_relationships.csv")

            model_metrics = self._save_model_report(out, sample_df, target_col, feature_cols, excluded_columns)

            distribution_penalty = mean_safe([r.get("total_variation_distance", 0.0) for r in distribution_rows])
            fill_penalty = summary.get_column("fill_pct_abs_delta").mean() / 100 if summary.height else 0.0
            numeric_penalties = []
            for row in numeric_rows:
                iqr = row.get("full_p75") - row.get("full_p25") if row.get("full_p75") is not None and row.get("full_p25") is not None else None
                delta = abs(row.get("p50_delta")) if row.get("p50_delta") is not None else None
                if iqr and iqr > 0 and delta is not None:
                    numeric_penalties.append(min(1.0, delta / iqr))
            numeric_penalty = mean_safe(numeric_penalties)
            relationship_penalty = mean_safe([r["binned_joint_tvd"] for r in relationship_rows])
            target_relationship_penalty = mean_safe([
                r["binned_joint_tvd"] for r in relationship_rows
                if target_col and target_col in [r["left"], r["right"]]
            ], default=relationship_penalty)
            model_transfer_score = model_metrics.get("model_transfer_score") if model_metrics else None
            model_transfer_penalty = (100 - model_transfer_score) / 100 if model_transfer_score is not None else None
            active_penalties = [
                (0.20, distribution_penalty),
                (0.10, fill_penalty),
                (0.15, numeric_penalty),
                (0.15, relationship_penalty),
            ]
            if target_match_score is not None:
                active_penalties.append((0.20, target_penalty))
            if tail_coverage_score is not None:
                active_penalties.append((0.10, tail_penalty))
            if target_col and relationship_rows:
                active_penalties.append((0.10, target_relationship_penalty))
            if model_transfer_penalty is not None:
                active_penalties.append((0.25, model_transfer_penalty))
            total_weight = sum(weight for weight, _ in active_penalties)
            combined_penalty = sum(weight * penalty for weight, penalty in active_penalties) / total_weight if total_weight else 0
            score = clamp_score(100 * (1 - (
                combined_penalty
            )))

            recommendation = "use_current_sample"
            if score < 90 or (target_match_score is not None and target_match_score < 90) or (tail_coverage_score is not None and tail_coverage_score < 85):
                recommendation = "increase_pct_or_candidates"
            if model_transfer_score is not None and model_transfer_score < 70:
                recommendation = "sample_not_model_ready"

            pl.DataFrame([{
                "matching_score": score,
                "target_match_score": target_match_score,
                "tail_coverage_score": tail_coverage_score,
                "model_transfer_score": model_transfer_score,
                "recommendation": recommendation,
                "current_sample_rows": sample_df.height,
                "suggested_next_pct": "current" if recommendation == "use_current_sample" else "try_2x_to_5x_current_pct",
            }]).write_csv(out / "sample_size_recommendation.csv")

            diagnostics = {
                "matching_score": score,
                "target_match_score": target_match_score,
                "tail_coverage_score": tail_coverage_score,
                "model_transfer_score": model_transfer_score,
                "distribution_penalty": round(distribution_penalty, 6),
                "fill_penalty": round(fill_penalty, 6),
                "numeric_penalty": round(numeric_penalty, 6),
                "relationship_penalty": round(relationship_penalty, 6),
                "target_relationship_penalty": round(target_relationship_penalty, 6),
                "sample_rows": sample_df.height,
                "strat_cols": ",".join(strat_cols),
                "recommendation": recommendation,
                "model_report": "model_report.csv" if model_metrics else "",
                "shap_report": "shap_values.csv" if model_metrics and model_metrics.get("shap_written") else "",
            }
            pl.DataFrame([diagnostics]).write_csv(out / "matching_score.csv")

            logger.info(f"Representation report saved to {out}")
            return diagnostics
        return await asyncio.to_thread(_save)

    def _save_model_report(
        self,
        out: Path,
        sample_df: Any,
        target_col: Optional[str],
        feature_cols: Optional[List[str]],
        excluded_columns: Optional[set] = None,
    ) -> Dict[str, Any]:
        if not target_col:
            return {}
        if RandomForestClassifier is None or np is None:
            pl.DataFrame([{
                "status": "skipped",
                "reason": "Install scikit-learn for model and permutation/SHAP-style feature reports.",
            }]).write_csv(out / "model_report.csv")
            pl.DataFrame([{
                "status": "skipped",
                "reason": "Install scikit-learn and shap to write SHAP values.",
            }]).write_csv(out / "shap_values.csv")
            return {"status": "skipped", "shap_written": False}
        if target_col not in sample_df.columns:
            pl.DataFrame([{"status": "skipped", "reason": f"Target column '{target_col}' not found."}]).write_csv(out / "model_report.csv")
            pl.DataFrame([{"status": "skipped", "reason": "Target column not found."}]).write_csv(out / "shap_values.csv")
            return {"status": "skipped", "shap_written": False}

        schema = self.lf.collect_schema()
        excluded_columns = excluded_columns or set()
        allowed_dtypes = (pl.String, pl.Categorical, pl.Boolean) if pl else ()
        features = feature_cols or [
            c for c, dtype in schema.items()
            if c != target_col
            and c in sample_df.columns
            and c not in excluded_columns
            and (is_numeric_dtype(dtype) or dtype in allowed_dtypes)
            and sample_df.get_column(c).n_unique() <= 250
        ]
        features = [
            c for c in features
            if c in sample_df.columns and c in schema and c not in excluded_columns
        ]
        if not features:
            pl.DataFrame([{"status": "skipped", "reason": "No numeric feature columns available."}]).write_csv(out / "model_report.csv")
            pl.DataFrame([{"status": "skipped", "reason": "No numeric feature columns available."}]).write_csv(out / "shap_values.csv")
            return {"status": "skipped", "shap_written": False}

        sample_eval = sample_df.select([*features, target_col]).drop_nulls(subset=[target_col])
        if sample_eval.height < 100:
            pl.DataFrame([{"status": "skipped", "reason": "Sample has fewer than 100 complete rows."}]).write_csv(out / "model_report.csv")
            pl.DataFrame([{"status": "skipped", "reason": "Sample has fewer than 100 complete rows."}]).write_csv(out / "shap_values.csv")
            return {"status": "skipped", "shap_written": False}

        full_eval = self.lf.select([*features, target_col]).drop_nulls(subset=[target_col]).head(100_000).collect()
        if full_eval.height < 100:
            pl.DataFrame([{"status": "skipped", "reason": "Full-data evaluation slice has fewer than 100 complete rows."}]).write_csv(out / "model_report.csv")
            pl.DataFrame([{"status": "skipped", "reason": "Full-data evaluation slice has fewer than 100 complete rows."}]).write_csv(out / "shap_values.csv")
            return {"status": "skipped", "shap_written": False}

        sample_exprs = []
        full_exprs = []
        for feature in features:
            if is_numeric_dtype(schema[feature]):
                fill_value = sample_eval.get_column(feature).median()
                if fill_value is None:
                    fill_value = 0.0
                sample_exprs.append(pl.col(feature).fill_null(fill_value))
                full_exprs.append(pl.col(feature).fill_null(fill_value))
            elif schema[feature] == pl.Boolean:
                sample_exprs.append(pl.col(feature).fill_null(False).cast(pl.Int32))
                full_exprs.append(pl.col(feature).fill_null(False).cast(pl.Int32))
            else:
                s_col = sample_eval.get_column(feature).fill_null("__MISSING__").cast(pl.String)
                f_col = full_eval.get_column(feature).fill_null("__MISSING__").cast(pl.String)
                combined = s_col.to_list() + f_col.to_list()
                categories = {value: idx for idx, value in enumerate(dict.fromkeys(combined))}
                sample_exprs.append(pl.col(feature).fill_null("__MISSING__").cast(pl.String).replace(categories, default=0).cast(pl.Int32))
                full_exprs.append(pl.col(feature).fill_null("__MISSING__").cast(pl.String).replace(categories, default=0).cast(pl.Int32))

        x_sample = sample_eval.select(sample_exprs).to_numpy()
        x_full = full_eval.select(full_exprs).to_numpy()
        
        y_sample = sample_eval.get_column(target_col).to_numpy()
        y_full = full_eval.get_column(target_col).to_numpy()

        y_unique_count = sample_eval.get_column(target_col).drop_nulls().n_unique()
        is_classification = y_unique_count <= 20 and not is_numeric_dtype(schema[target_col])
        if y_unique_count <= 20 and is_numeric_dtype(schema[target_col]):
            is_classification = True

        model = RandomForestClassifier(n_estimators=80, random_state=42, n_jobs=-1) if is_classification else RandomForestRegressor(n_estimators=80, random_state=42, n_jobs=-1)
        model.fit(x_sample, y_sample)
        pred_sample = model.predict(x_sample)
        pred_full = model.predict(x_full)

        if is_classification:
            sample_metric = f1_score(y_sample, pred_sample, average="weighted")
            full_metric = f1_score(y_full, pred_full, average="weighted")
            model_transfer_score = clamp_score(100 * max(0.0, full_metric) - 25 * abs(sample_metric - full_metric))
            metrics = {
                "status": "ok",
                "task": "classification",
                "sample_accuracy": accuracy_score(y_sample, pred_sample),
                "full_eval_accuracy": accuracy_score(y_full, pred_full),
                "sample_f1_weighted": sample_metric,
                "full_eval_f1_weighted": full_metric,
                "model_transfer_score": model_transfer_score,
                "full_eval_rows": full_eval.height,
            }
        else:
            sample_metric = r2_score(y_sample, pred_sample)
            full_metric = r2_score(y_full, pred_full)
            model_transfer_score = clamp_score(100 * max(0.0, full_metric) - 25 * abs(sample_metric - full_metric))
            metrics = {
                "status": "ok",
                "task": "regression",
                "sample_r2": sample_metric,
                "full_eval_r2": full_metric,
                "sample_mae": mean_absolute_error(y_sample, pred_sample),
                "full_eval_mae": mean_absolute_error(y_full, pred_full),
                "model_transfer_score": model_transfer_score,
                "full_eval_rows": full_eval.height,
            }
        pl.DataFrame([metrics]).write_csv(out / "model_report.csv")

        try:
            perm = permutation_importance(model, x_full, y_full, n_repeats=5, random_state=42, n_jobs=-1)
            pl.DataFrame([
                {"feature": feature, "permutation_importance_mean": float(mean), "permutation_importance_std": float(std)}
                for feature, mean, std in zip(features, perm.importances_mean, perm.importances_std)
            ]).sort("permutation_importance_mean", descending=True).write_csv(out / "feature_importance.csv")
        except Exception as exc:
            logger.warning(f"Permutation importance failed: {exc}")

        metrics["shap_written"] = False
        if shap is not None:
            try:
                explain_rows = x_full[:1000]
                explainer = shap.TreeExplainer(model)
                values = explainer.shap_values(explain_rows)
                if isinstance(values, list):
                    values = np.mean([np.abs(v) for v in values], axis=0)
                else:
                    values = np.abs(values)
                mean_abs = np.asarray(values).mean(axis=0)
                pl.DataFrame([
                    {"feature": feature, "mean_abs_shap": float(value)}
                    for feature, value in zip(features, mean_abs)
                ]).sort("mean_abs_shap", descending=True).write_csv(out / "shap_values.csv")
                metrics["shap_written"] = True
            except Exception as exc:
                logger.warning(f"SHAP report failed: {exc}")
        else:
            pl.DataFrame([{
                "status": "skipped",
                "reason": "Install shap to write shap_values.csv. feature_importance.csv was used as the available fallback.",
            }]).write_csv(out / "shap_values.csv")
        return metrics


# ══════════════════════════════════════════════════════════════════════════════
#  PYSPARK ENGINE (Distributed, Massive Scale)
# ══════════════════════════════════════════════════════════════════════════════

class SparkEngine(AbstractDataEngine):
    def __init__(self, uri: str):
        validate_s3_config(uri)
        validate_spark_config()
        self.uri = uri
        self.spark = SparkSession.builder.appName("EnterpriseProfiler").getOrCreate()

        # Auto-detect read format based on URI
        if "delta" in uri.lower() or Path(uri).joinpath("_delta_log").exists():
            self.df = self.spark.read.format("delta").load(uri)
        elif uri.endswith(".csv"):
            self.df = self.spark.read.csv(uri, header=True, inferSchema=True)
        else:
            self.df = self.spark.read.parquet(uri)

    async def get_metadata(self) -> Dict[str, Any]:
        def _fetch():
            return {
                "file_size": None, # HDFS/S3 size requires specific Hadoop FS APIs
                "n_rows": self.df.count(),
                "n_cols": len(self.df.columns),
                "schema": self.df.dtypes
            }
        return await asyncio.to_thread(_fetch)

    async def quick_profile(self, sample_rows: int = 200_000) -> Any:
        def _profile():
            sampled = self.df.limit(sample_rows)
            dtypes = dict(sampled.dtypes)
            total = sampled.count()
            if total == 0 or not dtypes:
                records = [
                    {"column": c, "is_categorical": dt in ['string', 'boolean'],
                     "fill_pct": 0.0, "n_unique": 0, "unique_ratio": 0.0}
                    for c, dt in dtypes.items()
                ]
            else:
                # One aggregate pass, all columns: approx_count_distinct is
                # HyperLogLog-based (O(1) memory, fast even at billions of
                # rows) -- appropriate here since this is explicitly the
                # QUICK pre-check used only for stratification-column
                # selection (full_profile above already does exact
                # countDistinct for the real profile). This replaces
                # hardcoded mock values that previously made every column
                # look identically eligible for stratification, silently
                # picking an arbitrary column on exactly the datasets large
                # enough to have routed to Spark in the first place.
                exprs = []
                for col in dtypes:
                    exprs.append(F.count(F.col(col)).alias(f"{col}__nonnull"))
                    exprs.append(F.approx_count_distinct(F.col(col)).alias(f"{col}__nunique"))
                stats = sampled.agg(*exprs).collect()[0].asDict()
                records = []
                for col, dtype in dtypes.items():
                    non_null = stats[f"{col}__nonnull"] or 0
                    n_unique = stats[f"{col}__nunique"] or 0
                    records.append({
                        "column": col,
                        "is_categorical": dtype in ['string', 'boolean'],
                        "fill_pct": round(100 * non_null / total, 2),
                        "n_unique": n_unique,
                        "unique_ratio": round(n_unique / total, 4),
                    })
            if pl:
                return pl.DataFrame(records)
            return records
        return await asyncio.to_thread(_profile)

    async def sample_data(
        self,
        pct: float,
        strat_cols: List[str],
        min_stratum_rows: int = 0,
        seed: int = 42,
        strategy: str = "stratified",
        target_col: Optional[str] = None,
        tail_cols: Optional[List[str]] = None,
        preserve_tails: bool = False,
        tail_quantile: float = 0.01,
        tail_boost: float = 3.0,
    ) -> Any:
        def _sample():
            frac = pct / 100.0
            if not strat_cols:
                return self.df.sample(withReplacement=False, fraction=frac, seed=seed)

            # Accurate Stratified sampling in Spark using Windowing
            n_strata = self.df.select(*strat_cols).distinct().limit(STRATA_HARD_LIMIT + 1).count()
            if n_strata > STRATA_HARD_LIMIT:
                logger.warning(
                    f"Stratified sampling skipped: {strat_cols} has more than {STRATA_HARD_LIMIT:,} strata. "
                    "Falling back to random sample."
                )
                return self.df.sample(withReplacement=False, fraction=frac, seed=seed)
            w = Window.partitionBy(*strat_cols).orderBy(F.rand(seed=seed))
            stratum_count_w = Window.partitionBy(*strat_cols)

            quota = F.round(F.col("__stratum_count") * frac).cast("int")
            if min_stratum_rows > 0:
                quota = F.greatest(F.lit(min_stratum_rows), quota)

            sampled = self.df.withColumn("__rn", F.row_number().over(w)) \
                             .withColumn("__stratum_count", F.count("*").over(stratum_count_w)) \
                             .filter(F.col("__rn") <= quota) \
                             .drop("__rn", "__stratum_count")
            return sampled
        return await asyncio.to_thread(_sample)

    async def full_profile(self, df: Any, top_fill_threshold: float) -> Dict[str, Any]:
        def _full():
            # Run concurrent spark jobs (Spark handles concurrency via job scheduler if properly configured)
            total = df.count()
            exprs = []
            for c in df.columns:
                exprs.append(F.count(F.col(c)).alias(f"{c}_cnt"))
                exprs.append(F.countDistinct(F.col(c)).alias(f"{c}_unq"))

            stats = df.agg(*exprs).collect()[0].asDict()

            records = []
            for c in df.columns:
                fill_pct = (stats[f"{c}_cnt"] / total * 100) if total else 0
                records.append({
                    "column": c, "fill_pct": fill_pct,
                    "n_unique": stats[f"{c}_unq"], "above_threshold": fill_pct >= top_fill_threshold
                })

            if pl:
                return {"full": pl.DataFrame(records), "high_fill": pl.DataFrame(records).filter(pl.col("above_threshold"))}
            return {"full": records}
        return await asyncio.to_thread(_full)

    async def save_outputs(self, sample_df: Any, profile: Dict[str, Any], out_dir: str, pct: float, fmt: str):
        def _save():
            out = Path(out_dir)
            out.mkdir(parents=True, exist_ok=True)
            path = str(out / f"sample_{pct_label(pct)}pct.{fmt}")
            sample_df.write.format(fmt).mode("overwrite").save(path)
            logger.info(f"Spark output saved to {path}")
        await asyncio.to_thread(_save)


# ══════════════════════════════════════════════════════════════════════════════
#  SQL ENGINE (Database Pushdown, No Data Movement)
# ══════════════════════════════════════════════════════════════════════════════

class SQLEngine(AbstractDataEngine):
    def __init__(self, uri: str, table_name: str):
        if not sa:
            raise ImportError("SQLAlchemy is not installed.")
        self.uri = uri
        self.table_name = table_name
        self.engine = sa.create_engine(uri)

    async def get_metadata(self) -> Dict[str, Any]:
        def _fetch():
            with self.engine.connect() as conn:
                count = conn.execute(sa.text(f"SELECT COUNT(*) FROM {self.table_name}")).scalar()
                # Dummy schema fetch for brevity
                return {"file_size": 0, "n_rows": count, "n_cols": 0, "schema": {}}
        return await asyncio.to_thread(_fetch)

    async def quick_profile(self, sample_rows: int = 200_000) -> Any:
        return await asyncio.to_thread(lambda: pl.DataFrame() if pl else []) # Simplified for DB

    async def sample_data(
        self,
        pct: float,
        strat_cols: List[str],
        min_stratum_rows: int = 0,
        seed: int = 42,
        strategy: str = "stratified",
        target_col: Optional[str] = None,
        tail_cols: Optional[List[str]] = None,
        preserve_tails: bool = False,
        tail_quantile: float = 0.01,
        tail_boost: float = 3.0,
    ) -> Any:
        # Pushes CTE-based sampling down to the DB
        frac = pct / 100.0
        cols_str = ",".join(strat_cols) if strat_cols else "1"
        query = f"""
        CREATE TABLE sampled_{pct_label(pct)}pct AS
        SELECT * FROM (
            SELECT *, ROW_NUMBER() OVER(PARTITION BY {cols_str} ORDER BY RANDOM()) as __rn,
                   COUNT(*) OVER(PARTITION BY {cols_str}) as __cnt
            FROM {self.table_name}
        ) sub WHERE __rn <= GREATEST(1, CAST(__cnt * {frac} AS INT))
        """
        def _exec():
            with self.engine.begin() as conn:
                conn.execute(sa.text(query))
            return f"sampled_{pct_label(pct)}pct"
        return await asyncio.to_thread(_exec)

    async def full_profile(self, df_table: Any, top_fill_threshold: float) -> Dict[str, Any]:
        return {"full": [], "high_fill": []}

    async def save_outputs(self, sample_df: Any, profile: Dict[str, Any], out_dir: str, pct: float, fmt: str):
        logger.info(f"SQL Engine: Sample materialized in database as table '{sample_df}'")


# ══════════════════════════════════════════════════════════════════════════════
#  ORCHESTRATOR & ROUTING
# ══════════════════════════════════════════════════════════════════════════════

def auto_detect_engine(uri: str) -> Type[AbstractDataEngine]:
    if uri.startswith("jdbc:") or uri.startswith("postgresql:") or uri.startswith("mysql:"):
        return SQLEngine

    # Heuristic: If file > 50GB or starts with s3a:// / hdfs://, prefer Spark
    if uri.startswith("s3://") or uri.startswith("s3a://") or uri.startswith("hdfs://"):
        return SparkEngine

    p = Path(uri)
    if p.exists():
        size = sum(f.stat().st_size for f in p.rglob("*") if f.is_file()) if p.is_dir() else p.stat().st_size
        if size > 50 * 1024 ** 3:  # 50 GB
            return SparkEngine

    return PolarsEngine

async def run_pipeline(
    uri: str,
    pct: float,
    strat: str,
    out_dir: str,
    top_fill: float,
    fmt: str,
    engine_choice: str,
    table_name: str = None,
    min_stratum_rows: int = 0,
    max_strat_cols: int = 2,
    representation_report: bool = True,
    candidates: int = 1,
    target_col: Optional[str] = None,
    feature_cols: Optional[List[str]] = None,
    relationship_cols: Optional[List[str]] = None,
    schema_overrides: Optional[Dict[str, Any]] = None,
    sample_strategies: Optional[List[str]] = None,
    optimize_for_model: bool = False,
    tail_cols: Optional[List[str]] = None,
    preserve_tails: bool = False,
    tail_quantile: float = 0.01,
    tail_boost: float = 3.0,
    semantic_config: Optional[Dict[str, Any]] = None,
):
    start_time = time.time()

    # 1. Dispatcher
    EngineClass = None
    if engine_choice == "auto":
        EngineClass = auto_detect_engine(uri)
    elif engine_choice == "polars":
        EngineClass = PolarsEngine
    elif engine_choice == "spark":
        EngineClass = SparkEngine
    elif engine_choice == "sql":
        EngineClass = SQLEngine

    logger.info(f"[Step 1] Initializing Engine: {EngineClass.__name__}")

    try:
        if engine_choice == "sql":
            engine = EngineClass(uri, table_name)
        elif EngineClass is PolarsEngine:
            engine = EngineClass(uri, schema_overrides=schema_overrides)
        else:
            engine = EngineClass(uri)
    except Exception as e:
        logger.error(f"Failed to initialize engine: {e}")
        return

    # 2. Async Metadata Gathering
    logger.info("[Step 2] Fetching Metadata asynchronously...")
    meta = await engine.get_metadata()
    logger.info(f"Dataset Size: {human_size(meta['file_size'])} | Rows: {meta['n_rows']}")

    # 3. Stratification Column Selection
    strat_cols = []
    if strat == "auto":
        logger.info("[Step 3] Running quick profile for auto-stratification...")
        q_prof = await engine.quick_profile()
        if pl and isinstance(q_prof, pl.DataFrame) and q_prof.height > 0:
            strat_candidates = q_prof.filter(
                pl.col("is_categorical") &
                (pl.col("n_unique") >= 2) &
                (pl.col("n_unique") <= 250) &
                (pl.col("unique_ratio") <= 0.05)
            ).sort(["fill_pct", "n_unique"], descending=[True, True])
            strat_cols = strat_candidates.head(max_strat_cols).get_column("column").to_list()
            if hasattr(engine, "lf"):
                while len(strat_cols) > 1:
                    n_strata = engine.lf.select(pl.struct(strat_cols).n_unique().alias("__n_strata")).collect().item()
                    if n_strata <= STRATA_HARD_LIMIT:
                        break
                    removed = strat_cols.pop()
                    logger.warning(
                        f"Auto-stratification removed '{removed}' because combined strata "
                        f"would be {n_strata:,} (limit {STRATA_HARD_LIMIT:,})."
                    )
            logger.info(f"Auto-selected strat cols: {strat_cols}")
    elif strat != "none":
        strat_cols = [c.strip() for c in strat.split(",")]

    candidate_rows = []
    root_out = Path(out_dir)
    root_out.mkdir(parents=True, exist_ok=True)
    if not sample_strategies or sample_strategies == ["auto"]:
        sample_strategies = ["stratified", "rare", "random"]
        if target_col or optimize_for_model:
            sample_strategies = ["stratified", "target_quantile", "hybrid", "rare", "random"]
    if optimize_for_model and target_col:
        preserve_tails = True
        tail_cols = unique_preserve_order([*(tail_cols or []), target_col])
    for idx in range(candidates):
        seed = 42 + idx * 1009
        strategy = sample_strategies[idx % len(sample_strategies)]
        candidate_name = f"candidate_{idx + 1:02d}"
        candidate_out = root_out / candidate_name if candidates > 1 else root_out

        # 4. Asynchronous Sampling
        logger.info(f"[Step 4] Executing sample candidate {idx + 1}/{candidates} ({pct}%) strategy={strategy} seed={seed}...")
        sample_df = await engine.sample_data(
            pct,
            strat_cols,
            min_stratum_rows,
            seed,
            strategy=strategy,
            target_col=target_col,
            tail_cols=tail_cols,
            preserve_tails=preserve_tails,
            tail_quantile=tail_quantile,
            tail_boost=tail_boost,
        )

        # 5. Parallel Profiling of Final Sample
        logger.info("[Step 5] Computing full profile statistics...")
        profile = await engine.full_profile(sample_df, top_fill)

        # 6. Save State / Write
        logger.info("[Step 6] Persisting results...")
        await engine.save_outputs(sample_df, profile, str(candidate_out), pct, fmt)

        diagnostics = {
            "matching_score": None,
            "sample_rows": getattr(sample_df, "height", None),
            "strat_cols": ",".join(strat_cols),
        }
        if representation_report and hasattr(engine, "save_representation_report"):
            logger.info("[Step 7] Computing sample representation diagnostics...")
            diagnostics = await engine.save_representation_report(
                sample_df,
                profile,
                str(candidate_out),
                strat_cols,
                top_fill,
                target_col=target_col,
                feature_cols=feature_cols,
                relationship_cols=relationship_cols,
                tail_cols=tail_cols,
                tail_quantile=tail_quantile,
            )
        candidate_rows.append({
            "candidate": candidate_name if candidates > 1 else "single",
            "seed": seed,
            "output_dir": str(candidate_out),
            "sample_path": str(candidate_out / f"sample_{pct_label(pct)}pct.{fmt}"),
            "strategy": strategy,
            **diagnostics,
        })

    if candidate_rows and pl:
        ranking = pl.DataFrame(candidate_rows)
        if "matching_score" in ranking.columns:
            ranking = ranking.sort("matching_score", descending=True, nulls_last=True)
        ranking.write_csv(root_out / "candidate_ranking.csv")
        best = ranking.row(0, named=True)
        logger.info(f"Best sample: {best['candidate']} | matching_score={best.get('matching_score')}")

    elapsed = round(time.time() - start_time, 2)
    logger.info(f"Pipeline complete in {elapsed}s")

# ══════════════════════════════════════════════════════════════════════════════
#  CLI ENTRYPOINT
# ══════════════════════════════════════════════════════════════════════════════

def main():
    parser = argparse.ArgumentParser(description="Enterprise Async Data Profiler & Sampler")
    parser.add_argument("--input", required=True, help="URI to data (Local path, S3, HDFS, JDBC connection string)")
    parser.add_argument("--pct", required=True, type=float, help="Percentage to sample (e.g., 5.5)")
    parser.add_argument("--engine", default="auto", choices=["auto", "polars", "spark", "sql"], help="Processing engine to use")
    parser.add_argument("--strat", default="auto", help='"auto", "none", or comma-separated column names')
    parser.add_argument("--out", default="./enterprise_output", help="Output directory")
    parser.add_argument("--fmt", default="parquet", choices=["parquet", "csv", "delta"], help="Output format")
    parser.add_argument("--table", default=None, help="Table name (Required only if using SQL engine)")
    parser.add_argument("--top-fill", type=float, default=90.0, help="Fill threshold for profiling")
    parser.add_argument("--min-stratum-rows", type=int, default=0, help="Minimum rows to keep per stratum. Use 0 for proportional representation, 1+ for rare-group coverage.")
    parser.add_argument("--max-strat-cols", type=int, default=2, help="Maximum columns to auto-select for stratification")
    parser.add_argument("--no-representation-report", action="store_true", help="Skip full-vs-sample representation diagnostics")
    parser.add_argument("--candidates", type=int, default=1, help="Number of candidate samples to generate and rank by matching score")
    parser.add_argument("--target", default=None, help="Optional modeling target column. Enables model/feature reports when scikit-learn is installed.")
    parser.add_argument("--features", default=None, help="Optional comma-separated feature columns for modeling and feature info")
    parser.add_argument("--relationship-cols", default=None, help="Optional comma-separated numeric columns for nonlinear relationship diagnostics")
    parser.add_argument("--schema-overrides", default=None, help="Comma-separated CSV/schema overrides, e.g. zip:string,id:string,amount:float")
    parser.add_argument("--sample-strategies", default="auto", help="Comma-separated strategies: auto, random, stratified, rare, target_quantile, hybrid")
    parser.add_argument("--optimize-for-model", action="store_true", help="Use target-aware strategies, tail preservation, and model-transfer scoring when possible")
    parser.add_argument("--tail-cols", default=None, help="Comma-separated numeric columns whose low/high tails should be preserved and scored")
    parser.add_argument("--preserve-tails", action="store_true", help="Force inclusion of low/high tail rows for target/tail columns")
    parser.add_argument("--tail-quantile", type=float, default=0.01, help="Tail quantile to preserve and score, e.g. 0.01 for bottom/top 1%%")
    parser.add_argument("--tail-boost", type=float, default=3.0, help="Multiplier for tail sampling when preserving tails")
    parser.add_argument("--methodology", default=None, help="Path to a semantic_schema.json generated by methodology_parser.py")

    args = parser.parse_args()

    if not (0 < args.pct <= 100):
        parser.error("--pct must be greater than 0 and less than or equal to 100.")

    if args.min_stratum_rows < 0:
        parser.error("--min-stratum-rows must be 0 or greater.")

    if args.max_strat_cols < 1:
        parser.error("--max-strat-cols must be 1 or greater.")

    if args.candidates < 1:
        parser.error("--candidates must be 1 or greater.")

    if not (0 < args.tail_quantile < 0.5):
        parser.error("--tail-quantile must be greater than 0 and less than 0.5.")

    if args.tail_boost < 1:
        parser.error("--tail-boost must be 1 or greater.")

    if args.engine == "sql" and not args.table:
        parser.error("--table is required when using the SQL engine.")

    try:
        schema_overrides = parse_schema_overrides(args.schema_overrides)
    except ValueError as exc:
        parser.error(str(exc))
    
    semantic_config = {}
    if args.methodology:
        if os.path.exists(args.methodology):
            import json
            try:
                with open(args.methodology, "r", encoding="utf-8") as f:
                    semantic_config = json.load(f)
                    logger.info(f"Loaded semantic methodology from {args.methodology}")
                    # Auto-inject overrides
                    for col, detail in semantic_config.get("columns", {}).items():
                        if "expected_type" in detail and col not in schema_overrides:
                            t = detail["expected_type"].lower()
                            if POLARS_DTYPE_MAP.get(t):
                                schema_overrides[col] = POLARS_DTYPE_MAP[t]
            except Exception as e:
                logger.warning(f"Failed to load methodology JSON: {e}")
        else:
            logger.warning(f"Methodology file not found: {args.methodology}")

    sample_strategies = parse_column_list(args.sample_strategies)
    valid_strategies = {"auto", "random", "stratified", "rare", "target_quantile", "hybrid"}
    invalid_strategies = [s for s in sample_strategies if s not in valid_strategies]
    if invalid_strategies:
        parser.error(f"Unsupported sample strategy: {', '.join(invalid_strategies)}")

    rel_cols = parse_column_list(args.relationship_cols)
    if semantic_config.get("relationships"):
        for rel in semantic_config["relationships"]:
            if "source_col" in rel and rel["source_col"] not in rel_cols:
                rel_cols.append(rel["source_col"])

    # Run the asyncio event loop
    asyncio.run(run_pipeline(
        uri=args.input,
        pct=args.pct,
        strat=args.strat,
        out_dir=args.out,
        top_fill=args.top_fill,
        fmt=args.fmt,
        engine_choice=args.engine,
        table_name=args.table,
        min_stratum_rows=args.min_stratum_rows,
        max_strat_cols=args.max_strat_cols,
        representation_report=not args.no_representation_report,
        candidates=args.candidates,
        target_col=args.target,
        feature_cols=parse_column_list(args.features),
        relationship_cols=rel_cols,
        schema_overrides=schema_overrides,
        sample_strategies=sample_strategies,
        optimize_for_model=args.optimize_for_model,
        tail_cols=parse_column_list(args.tail_cols),
        preserve_tails=args.preserve_tails,
        tail_quantile=args.tail_quantile,
        tail_boost=args.tail_boost,
        semantic_config=semantic_config,
    ))

if __name__ == "__main__":
    main()
