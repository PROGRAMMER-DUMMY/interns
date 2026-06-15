"""In-memory re-aggregation over a validated gold frame.

This is the heart of the live dashboard: take the validated rows, apply the
active cross-filter / slicers, group to the requested grain, and return the
measure. It must NEVER produce a number that contradicts the validated headline
-- so the additive vs non-additive distinction is enforced here, not guessed in
the UI.

Column-name matching is case-insensitive throughout: gold uses source casing
(``PayorID``) while spec panels and slicers may use lowercased aliases
(``payorid``). Callers pass either; we resolve against the frame's real columns.
"""
from __future__ import annotations

from typing import Any

import polars as pl

_NUMERIC_DTYPES = {
    pl.Int8, pl.Int16, pl.Int32, pl.Int64,
    pl.UInt8, pl.UInt16, pl.UInt32, pl.UInt64,
    pl.Float32, pl.Float64,
}


class NonAdditiveError(ValueError):
    """Raised when a roll-up would recombine a non-additive measure.

    A ratio / average cannot be re-derived by summing pre-aggregated cells. The
    UI should either keep such a measure filter-only (no roll-up below its native
    grain) or recompute it from a finer layer -- never silently sum it.
    """


def is_numeric_dtype(dtype: Any) -> bool:
    if dtype is None:
        return False
    try:
        return bool(dtype.is_numeric())
    except Exception:
        return dtype in _NUMERIC_DTYPES


def resolve_column(name: str | None, columns: list[str]) -> str | None:
    """Case-insensitive resolution of a column name to the frame's real column."""
    if not name:
        return None
    if name in columns:
        return name
    low = str(name).lower()
    for c in columns:
        if c.lower() == low:
            return c
    return None


def _resolve_filters(
    filters: dict[str, Any] | None, columns: list[str]
) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for key, val in (filters or {}).items():
        col = resolve_column(key, columns)
        if col is not None:
            out[col] = val
    return out


def apply_filters(frame: pl.DataFrame, filters: dict[str, Any] | None) -> pl.DataFrame:
    """Filter rows. Values may be a scalar, a list (IN), or a {from,to} range.

    Empty / None values are no-ops (an inactive slicer must not drop all rows).
    """
    df = frame
    for col, val in _resolve_filters(filters, frame.columns).items():
        if val is None or val == [] or val == "":
            continue
        if isinstance(val, dict) and ("from" in val or "to" in val):
            lo, hi = val.get("from"), val.get("to")
            if lo is not None:
                df = df.filter(pl.col(col) >= lo)
            if hi is not None:
                df = df.filter(pl.col(col) <= hi)
        elif isinstance(val, (list, tuple, set)):
            df = df.filter(_eq_any(df.schema.get(col), col, list(val)))
        else:
            df = df.filter(_eq(df.schema.get(col), col, val))
    return df


def _eq(dtype, col: str, val) -> pl.Expr:
    """Type-tolerant equality. A click on a non-string axis (e.g. a date point on
    a line, or an integer-coded category) arrives as a string from Plotly; compare
    on the column's string form so it still matches the typed gold values."""
    if dtype is not None and not _is_string_dtype(dtype) and isinstance(val, str):
        return pl.col(col).cast(pl.Utf8) == val
    return pl.col(col) == val


def _eq_any(dtype, col: str, vals: list) -> pl.Expr:
    if dtype is not None and not _is_string_dtype(dtype) and any(isinstance(v, str) for v in vals):
        return pl.col(col).cast(pl.Utf8).is_in([str(v) for v in vals])
    return pl.col(col).is_in(vals)


def _is_string_dtype(dtype) -> bool:
    try:
        return dtype in (pl.Utf8, pl.String) or dtype == pl.Categorical
    except Exception:
        return False


def aggregate(
    frame: pl.DataFrame,
    *,
    measure: str,
    additive: bool,
    group_by: list[str] | None = None,
    filters: dict[str, Any] | None = None,
) -> pl.DataFrame:
    """Filter, group to ``group_by``, and aggregate ``measure``.

    Additive measures (sum, count, single-attribution share) are summed. A
    non-additive measure may be filtered and may be grouped only when the grouping
    does not merge multiple source rows into one cell (i.e. it stays at-or-above
    the native grain); otherwise ``NonAdditiveError`` is raised rather than
    fabricating a recombined ratio.
    """
    columns = frame.columns
    measure_c = resolve_column(measure, columns)
    if measure_c is None:
        raise KeyError(f"measure {measure!r} not in columns {columns}")

    filtered = apply_filters(frame, filters)
    gb = [c for c in (resolve_column(g, columns) for g in (group_by or [])) if c]

    if not gb:
        if additive:
            total = filtered.select(pl.col(measure_c).sum().alias(measure_c))
            return total
        # Non-additive scalar over many rows is undefined; only meaningful when a
        # single row remains after filtering.
        if filtered.height <= 1:
            return filtered.select(pl.col(measure_c))
        raise NonAdditiveError(
            f"cannot reduce non-additive measure {measure_c!r} over "
            f"{filtered.height} rows to a scalar"
        )

    if additive:
        return (
            filtered.group_by(gb, maintain_order=True)
            .agg(pl.col(measure_c).sum().alias(measure_c))
        )

    # Non-additive: grouping is only safe if each group is already a single row.
    counts = filtered.group_by(gb).len()
    if counts.height and counts.get_column("len").max() > 1:
        raise NonAdditiveError(
            f"roll-up of non-additive measure {measure_c!r} by {gb} would "
            "recombine multiple cells; recompute from a finer layer instead"
        )
    return filtered.select([*gb, measure_c])


__all__ = [
    "NonAdditiveError",
    "aggregate",
    "apply_filters",
    "is_numeric_dtype",
    "resolve_column",
]
