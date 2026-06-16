"""Query engine: widget spec + active filters -> tidy result frame (Polars).

Flow per widget:
  1. Resolve the measures it uses (aggregate + derived).
  2. Infer the base (fact) table.
  3. Try DuckDB pushdown; else assemble a joined Polars frame and aggregate.
  4. Apply active filters, group by the dimensions, compute measures.
  5. Compute derived measures; sort/limit.
"""

from __future__ import annotations

import json
import re
from collections import OrderedDict
from dataclasses import dataclass, field
from typing import Any, Optional

import polars as pl

from minus.config.models import Measure, Project, Widget
from minus.data import duckdb_exec
from minus.data.model import SemanticModel, split_field
from minus.query import measures as M

_IDENT = re.compile(r"[A-Za-z_]\w*")


@dataclass
class QueryResult:
    frame: pl.DataFrame                 # tidy: dimension cols + measure-name cols
    dimensions: list[str] = field(default_factory=list)
    measures: list[str] = field(default_factory=list)
    scalar: Optional[float] = None      # for KPI widgets (no dimension)
    delta: Optional[float] = None       # signed % change vs previous period
    delta_label: Optional[str] = None   # e.g. "vs prev quarter"


Filters = dict[str, Any]


# How many distinct (widget, filters) results to retain per data generation.
# The live grid re-runs every widget on each slicer/cross-filter interaction;
# caching turns repeat interactions into dict lookups instead of full
# join+group-by passes. Bounded LRU so memory stays flat over a long session.
_RESULT_CACHE_MAX = 512


class QueryEngine:
    def __init__(self, project: Project, model: SemanticModel):
        self.project = project
        self.model = model
        self._result_cache: "OrderedDict[tuple, QueryResult]" = OrderedDict()

    # -- result cache -----------------------------------------------------
    def _cache_key(self, widget: Widget, filters: Filters) -> Optional[tuple]:
        """Stable key for a (data-version, widget-spec, filters) result, or None
        when the inputs can't be canonicalized (then we skip caching)."""
        try:
            wkey = widget.model_dump_json()
            fkey = json.dumps(filters, sort_keys=True, default=str)
        except (TypeError, ValueError):
            return None
        gen = getattr(self.model, "generation", 0)
        return (gen, wkey, fkey)

    # -- helpers ----------------------------------------------------------
    def _measures_for(self, widget: Widget) -> list[Measure]:
        names = ([widget.measure] if widget.measure else []) + list(widget.measures)
        return [self.project.measure(n) for n in names]

    def _deps(self, m: Measure) -> list[str]:
        deps = [r for r in (m.numerator, m.denominator, m.of) if r]
        if m.kind == "expression" and m.expression:
            known = {mm.name for mm in self.project.measures}
            deps += [tok for tok in _IDENT.findall(m.expression) if tok in known]
        return deps

    def _base_table(self, widget: Widget, ms: list[Measure]) -> str:
        pending = list(ms)
        while pending:
            m = pending.pop(0)
            if m.field:
                return split_field(m.field)[0]
            if m.table:
                return m.table
            for ref in self._deps(m):
                pending.append(self.project.measure(ref))
        if widget.dimension:
            return split_field(widget.dimension)[0]
        raise ValueError(f"widget {widget.id!r}: cannot infer base table")

    def _agg_fields(self, ms: list[Measure]) -> dict[str, Optional[str]]:
        out: dict[str, Optional[str]] = {}
        pending = list(ms)
        while pending:
            m = pending.pop()
            if M.is_derived(m):
                for ref in self._deps(m):
                    pending.append(self.project.measure(ref))
            else:
                out[m.name] = m.field
        return out

    # -- main entry -------------------------------------------------------
    def run(self, widget: Widget, filters: Optional[Filters] = None) -> QueryResult:
        """Run the widget query, returning a cached result when one exists for
        the same widget + filters at the current data generation."""
        filters = filters or {}
        key = self._cache_key(widget, filters)
        if key is not None and key in self._result_cache:
            self._result_cache.move_to_end(key)        # mark as recently used
            return self._result_cache[key]

        result = self._run_uncached(widget, filters)

        if key is not None:
            self._result_cache[key] = result
            self._result_cache.move_to_end(key)
            while len(self._result_cache) > _RESULT_CACHE_MAX:
                self._result_cache.popitem(last=False)  # evict least-recent
        return result

    def _run_uncached(self, widget: Widget, filters: Filters) -> QueryResult:
        ms = self._measures_for(widget)
        base = self._base_table(widget, ms)
        agg_fields = self._agg_fields(ms)
        dims = [d for d in (widget.dimension, widget.breakdown) if d]

        pushed = self._try_pushdown(widget, ms, base, agg_fields, dims, filters)
        if pushed is not None:
            return pushed

        ref_fields = [f for f in agg_fields.values() if f]
        ref_fields += dims + list(filters.keys())
        df = _apply_filters(self.model.assemble(base, ref_fields), filters)

        if not dims:  # KPI / scalar
            agg_vals = self._aggregate_scalar(df, agg_fields)
            result = self._scalar_result(agg_vals, ms)
            if widget.compare:
                result.delta, result.delta_label = self._trend_delta(
                    widget, ms, agg_fields, filters)
            return result
        grouped = self._aggregate_grouped(df, dims, agg_fields)
        return self._grouped_result(grouped, dims, ms, widget)

    # -- DuckDB pushdown --------------------------------------------------
    def _try_pushdown(self, widget, ms, base, agg_fields, dims, filters):
        if not duckdb_exec.supported(self.model, ms, agg_fields, dims, filters):
            return None
        try:
            frame = duckdb_exec.run_pushdown(self.model, base, ms, agg_fields, dims, filters)
        except Exception:
            return None
        try:
            if not dims:  # one-row frame of measure-name columns
                row = frame.row(0, named=True) if len(frame) else {}
                agg_vals = {name: (float(row[name]) if row.get(name) is not None else float("nan"))
                            for name in agg_fields}
                result = self._scalar_result(agg_vals, ms)
                if widget.compare:
                    result.delta, result.delta_label = self._trend_delta(
                        widget, ms, agg_fields, filters)
                return result
            return self._grouped_result(frame, dims, ms, widget)
        except Exception:
            return None

    # -- scalar (KPI) -----------------------------------------------------
    def _aggregate_scalar(self, df, agg_fields) -> dict[str, float]:
        return {name: M.aggregate_scalar(df, self.project.measure(name), fld)
                for name, fld in agg_fields.items()}

    def _scalar_result(self, agg_vals: dict[str, float], ms) -> QueryResult:
        primary = ms[0]
        value = M.derived_scalar(agg_vals, primary) if M.is_derived(primary) \
            else agg_vals[primary.name]
        return QueryResult(frame=pl.DataFrame([agg_vals]), measures=list(agg_vals),
                           scalar=value)

    # -- trend delta (period-over-period for KPIs) ------------------------
    def _scalar_for_subset(self, sub, ms, agg_fields) -> float:
        agg_vals = {name: M.aggregate_scalar(sub, self.project.measure(name), fld)
                    for name, fld in agg_fields.items()}
        primary = ms[0]
        return M.derived_scalar(agg_vals, primary) if M.is_derived(primary) \
            else agg_vals[primary.name]

    def _trend_delta(self, widget, ms, agg_fields, filters):
        try:
            base = split_field(widget.compare)[0]
            ref_fields = [f for f in agg_fields.values() if f]
            ref_fields += [widget.compare] + list(filters.keys())
            df = _apply_filters(self.model.assemble(base, ref_fields), filters)

            col = pl.col(widget.compare)
            if not df.schema[widget.compare].is_temporal():
                col = col.str.to_datetime(strict=False)
            if widget.compare_period == "year":
                pexpr = col.dt.year()
            elif widget.compare_period == "quarter":
                pexpr = col.dt.year() * 10 + col.dt.quarter()
            else:
                pexpr = col.dt.year() * 100 + col.dt.month()

            df = df.with_columns(pexpr.alias("_period")).drop_nulls("_period")
            periods = sorted(df.get_column("_period").unique().to_list())
            if len(periods) < 2:
                return None, None
            prev_v = self._scalar_for_subset(df.filter(pl.col("_period") == periods[-2]),
                                             ms, agg_fields)
            latest_v = self._scalar_for_subset(df.filter(pl.col("_period") == periods[-1]),
                                               ms, agg_fields)
            if prev_v in (None, 0) or _isnan(prev_v) or _isnan(latest_v):
                return None, None
            return float((latest_v - prev_v) / prev_v * 100.0), f"vs prev {widget.compare_period}"
        except Exception:
            return None, None

    # -- grouped (charts/tables) -----------------------------------------
    def _aggregate_grouped(self, df, dims, agg_fields) -> pl.DataFrame:
        exprs = [M.agg_expr(self.project.measure(name), fld)
                 for name, fld in agg_fields.items()]
        if not exprs:
            return df.select(dims).unique()
        return df.group_by(dims, maintain_order=True).agg(exprs)

    def _grouped_result(self, result, dims, ms, widget) -> QueryResult:
        for m in ms:               # derived measures, in declared order
            if M.is_derived(m):
                result = result.with_columns(M.derived_expr(m))
        measure_cols = [m.name for m in ms]

        primary = ms[0].name if ms else None
        if primary and primary in result.columns:
            result = result.sort(primary, descending=(widget.sort != "asc"), nulls_last=True)
        if widget.limit:
            result = result.head(widget.limit)
        keep = [c for c in (dims + measure_cols) if c in result.columns]
        return QueryResult(frame=result.select(keep), dimensions=dims, measures=measure_cols)


def _isnan(v) -> bool:
    return isinstance(v, float) and v != v


# ---------------------------------------------------------------------------
# Filtering
# ---------------------------------------------------------------------------


def _apply_filters(df: pl.DataFrame, filters: Filters) -> pl.DataFrame:
    for col, val in filters.items():
        if col not in df.columns or val is None or val == [] or val == "":
            continue
        dtype = df.schema[col]
        if isinstance(val, dict) and ("from" in val or "to" in val):
            lo, hi = val.get("from"), val.get("to")
            if lo is not None:
                df = df.filter(pl.col(col) >= _bound(dtype, lo))
            if hi is not None:
                df = df.filter(pl.col(col) <= _bound(dtype, hi))
        elif isinstance(val, (list, tuple)):
            df = df.filter(pl.col(col).is_in(list(val)))
        else:
            df = df.filter(pl.col(col) == val)
    return df


def _bound(dtype, value):
    """A Polars literal comparable against a column of ``dtype``."""
    if dtype.is_temporal():
        return pl.lit(str(value)).str.to_datetime(strict=False)
    if dtype.is_numeric():
        try:
            return pl.lit(float(value))
        except (TypeError, ValueError):
            return pl.lit(value)
    return pl.lit(value)
