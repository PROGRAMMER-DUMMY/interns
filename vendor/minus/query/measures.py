"""Measure evaluation (DAX-lite) and value formatting — Polars.

Aggregate measures compile to Polars expressions used in select/group_by.
Derived measures (ratio / percent_of_total / yoy / expression) compile to
expressions over the already-aggregated measure-name columns.
"""

from __future__ import annotations

import math
from typing import Optional

import polars as pl

from minus.config.models import Measure

_AGG = {"sum": "sum", "avg": "mean", "min": "min", "max": "max",
        "median": "median", "first": "first"}


def is_derived(m: Measure) -> bool:
    return m.kind is not None


# ---------------------------------------------------------------------------
# Aggregate measures -> Polars expressions
# ---------------------------------------------------------------------------


def agg_expr(m: Measure, field_col: Optional[str]) -> pl.Expr:
    """A Polars aggregation expression aliased to the measure name."""
    if m.agg == "count":
        return pl.len().alias(m.name)
    if m.agg == "count_distinct":
        return pl.col(field_col).n_unique().alias(m.name)
    return getattr(pl.col(field_col), _AGG[m.agg])().alias(m.name)


def aggregate_scalar(df: pl.DataFrame, m: Measure, field_col: Optional[str]) -> float:
    """Aggregate the whole frame to a single value for measure ``m``."""
    if len(df) == 0:
        return 0.0 if m.agg in ("count", "count_distinct") else float("nan")
    val = df.select(agg_expr(m, field_col)).item()
    return float(val) if val is not None else float("nan")


# ---------------------------------------------------------------------------
# Derived measures -> Polars expressions over the aggregated frame
# ---------------------------------------------------------------------------


def derived_expr(m: Measure) -> pl.Expr:
    if m.kind == "ratio":
        den = pl.col(m.denominator)
        return pl.when(den == 0).then(None).otherwise(pl.col(m.numerator) / den).alias(m.name)
    if m.kind == "percent_of_total":
        of = pl.col(m.of)
        return (of / of.sum() * 100).alias(m.name)
    if m.kind == "yoy":
        of = pl.col(m.of)
        prev = of.shift(1)
        return pl.when((prev == 0) | prev.is_null()).then(None) \
            .otherwise((of - prev) / prev * 100).alias(m.name)
    if m.kind == "expression":
        return pl.sql_expr(m.expression).alias(m.name)
    raise ValueError(f"unknown derived kind {m.kind!r} for measure {m.name!r}")


def derived_scalar(values: dict[str, float], m: Measure) -> float:
    """Compute a derived measure for the no-grouping (KPI) case."""
    if m.kind == "ratio":
        den = values.get(m.denominator) or 0.0
        return (values.get(m.numerator, 0.0) / den) if den else math.nan
    if m.kind == "percent_of_total":
        return 100.0  # the whole is 100% of itself
    if m.kind == "yoy":
        return math.nan  # undefined without a time series
    if m.kind == "expression":
        try:  # arithmetic over measure names; no builtins exposed
            return float(eval(m.expression, {"__builtins__": {}}, dict(values)))
        except Exception:
            return math.nan
    raise ValueError(f"unknown derived kind {m.kind!r}")


# ---------------------------------------------------------------------------
# Formatting
# ---------------------------------------------------------------------------


def _compact(value: float) -> str:
    """1_234_567 -> '1.23M'. Keeps big numbers short so KPIs never overflow."""
    a = abs(value)
    if a >= 1e9:
        return f"{value / 1e9:,.2f}B"
    if a >= 1e6:
        return f"{value / 1e6:,.2f}M"
    if a >= 1e4:
        return f"{value / 1e3:,.1f}K"
    return f"{value:,.0f}"


def format_value(value, fmt: Optional[str]) -> str:
    if value is None or (isinstance(value, float) and math.isnan(value)):
        return "—"
    try:
        if fmt == "currency":
            return "$" + _compact(value)            # e.g. $25.38M, $2,538
        if fmt == "percent":
            return f"{value:,.1f}%"
        if fmt == "int":
            return _compact(value) if abs(value) >= 1e6 else f"{int(round(value)):,}"
        if fmt == "float":
            return f"{value:,.2f}"
        if fmt:
            return format(value, fmt)
    except (ValueError, TypeError):
        return str(value)
    if isinstance(value, (int, float)):
        return _compact(value) if abs(value) >= 1000 else f"{value:,.2f}"
    return str(value)
