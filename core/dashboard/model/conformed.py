"""Conformed semantic model -- the "missing Silver" the dashboard needs.

Builds ONE clean star from the raw bronze entities, applying the silver-standard
transforms (dedup, type/timestamp normalization, null handling, canonical naming,
approved reference joins) so the dashboard can aggregate measure x ANY dimension
live -- the foundation a true Power BI canvas requires.

Workspace-agnostic: the join graph comes from relationship_contracts.json (only
edges marked executable), the fact is inferred as the many-side hub, and measures
are derived from the fact's numeric columns. Nothing healthcare-specific is hard
coded. PII columns (per pii_redaction) are dropped from the model entirely.

Gold remains the parity check (see dq.py): this model's aggregation must reproduce
the validated KPI totals. We CLEAN bronze here; we never aggregate it raw.
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Any

import polars as pl

from core.dashboard.model.aggregate import is_numeric_dtype, resolve_column
from core.dashboard.model.layers import list_bronze_tables, read_bronze
from core.dashboard.ui.governance import WorkspaceRedaction
from core.storage.workspace_layout import WorkspaceLayout

_DATE_NAME_RE = re.compile(r"(date|dob|timestamp|_at$|_dt$)", re.IGNORECASE)
_MEASURE_NAME_RE = re.compile(
    r"(amount|paid|cost|charge|price|total|qty|quantity|revenue|spend|sales|value)",
    re.IGNORECASE,
)
_ID_RE = re.compile(r"id$", re.IGNORECASE)


@dataclass(frozen=True)
class Measure:
    name: str            # short label e.g. "Total Paid"
    column: str          # source column (or "*" for count)
    agg: str             # "sum" | "count" | "count_distinct"
    fmt: str = ""        # "currency" | "int" | ...


@dataclass(frozen=True)
class Edge:
    left_table: str
    left_col: str
    right_table: str
    right_col: str


@dataclass
class ConformedModel:
    """A cleaned, joined star ready for live measure x dimension aggregation."""

    layout: WorkspaceLayout
    fact: str
    frame: pl.DataFrame                       # the joined, cleaned, derived star
    measures: dict[str, Measure] = field(default_factory=dict)
    dimensions: list[str] = field(default_factory=list)
    edges: list[Edge] = field(default_factory=list)

    def measure_names(self) -> list[str]:
        return list(self.measures.keys())


# ---------------------------------------------------------------------------
# Approved join graph from relationship_contracts.json
# ---------------------------------------------------------------------------


def _stem(path: str) -> str:
    base = str(path).replace("\\", "/").rsplit("/", 1)[-1]
    return base.rsplit(".", 1)[0].lower()


def approved_edges(layout: WorkspaceLayout) -> list[Edge]:
    """Executable foreign-key edges only (the rest are advisory / failed RI)."""
    path = layout.relationship_contracts_path
    if not path.exists():
        return []
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return []
    out: list[Edge] = []
    for rel in data.get("relationships", []):
        pol = rel.get("executable_usage_policy", {})
        if not pol.get("allowed_in_sql_generation"):
            continue
        out.append(Edge(
            left_table=_stem(rel.get("left_dataset", "")),
            left_col=rel.get("left_column", ""),
            right_table=_stem(rel.get("right_dataset", "")),
            right_col=rel.get("right_column", ""),
        ))
    return out


def _infer_fact(edges: list[Edge], tables: list[str]) -> str:
    """The fact is the many-side hub: the table that is the LEFT of the most
    approved edges."""
    if not edges:
        return tables[0] if tables else ""
    counts: dict[str, int] = {}
    for e in edges:
        counts[e.left_table] = counts.get(e.left_table, 0) + 1
    return max(counts, key=counts.get)


# ---------------------------------------------------------------------------
# Silver-grade cleaning
# ---------------------------------------------------------------------------


def _normalize_types(df: pl.DataFrame) -> pl.DataFrame:
    """Cast obvious date-named string columns to Date (timestamp normalization)."""
    exprs = []
    for col, dtype in df.schema.items():
        if dtype == pl.Utf8 and _DATE_NAME_RE.search(col):
            exprs.append(pl.col(col).str.to_date(strict=False).alias(col))
    return df.with_columns(exprs) if exprs else df


def _dedup_on(df: pl.DataFrame, key: str | None) -> pl.DataFrame:
    """Exact dedup on a dimension's primary key (keep first)."""
    k = resolve_column(key, df.columns) if key else None
    return df.unique(subset=[k], keep="first") if k else df


# A small lookup dimension's label column is a category, not PII. The PII
# pattern ^name$ would otherwise strip a departments.Name; a large person
# table's Name stays redacted.
_SMALL_DIM_MAX = 2000


def _clean_table(
    df: pl.DataFrame, pk: str | None, redaction: WorkspaceRedaction,
    *, entity: str = "", is_dim: bool = False,
) -> pl.DataFrame:
    df = _normalize_types(df)
    # Derive age from DOB BEFORE redaction drops DOB (DOB is the raw input; age
    # is a non-PII derived dimension -- same rule the KPI SQL uses).
    dob = next((c for c in df.columns if c.lower() == "dob"), None)
    if dob and df.schema.get(dob) == pl.Date and "age" not in df.columns:
        df = df.with_columns((pl.lit(2026) - pl.col(dob).dt.year()).alias("age"))
    # Qualify a small lookup-dim label column so it survives as a category.
    if is_dim and entity and "Name" in df.columns and df.height <= _SMALL_DIM_MAX:
        df = df.rename({"Name": f"{entity.rstrip('s')}_name"})
    df = _dedup_on(df, pk)
    # Governance: drop PII columns from the model entirely (never displayable).
    keep = [c for c in df.columns if not redaction.is_redacted(c)]
    return df.select(keep)


# ---------------------------------------------------------------------------
# Build
# ---------------------------------------------------------------------------


def _derive_columns(df: pl.DataFrame) -> pl.DataFrame:
    """Add common derived dimensions on the joined star (age is derived earlier,
    per-table, before DOB redaction). Here: month from a service date."""
    exprs = []
    svc = next((c for c in df.columns if c.lower() in ("servicedate", "service_date")), None)
    if svc and df.schema.get(svc) == pl.Date:
        exprs.append(pl.col(svc).dt.truncate("1mo").alias("month"))
    return df.with_columns(exprs) if exprs else df


def _build_measures(frame: pl.DataFrame, fact_cols: list[str]) -> dict[str, Measure]:
    measures: dict[str, Measure] = {}
    for col in fact_cols:
        if col not in frame.columns or _ID_RE.search(col):
            continue
        if is_numeric_dtype(frame.schema.get(col)) and _MEASURE_NAME_RE.search(col):
            label = re.sub(r"(?<=[a-z0-9])(?=[A-Z])", " ", col).replace("_", " ").title()
            measures[f"Total {label}"] = Measure(f"Total {label}", col, "sum", "currency")
    # always provide a row count
    measures["Record Count"] = Measure("Record Count", "*", "count", "int")
    return measures


def build_conformed_model(layout: WorkspaceLayout) -> ConformedModel | None:
    """Build the cleaned, joined star. None when bronze/edges are unavailable."""
    tables = list_bronze_tables(layout)
    if not tables:
        return None
    redaction = WorkspaceRedaction(layout.project_root)
    edges = [e for e in approved_edges(layout)
             if e.left_table in tables and e.right_table in tables]
    fact = _infer_fact(edges, tables)
    if not fact:
        return None

    # primary keys: a dimension's PK is the right_col of an edge pointing to it.
    pk_by_table = {e.right_table: e.right_col for e in edges}

    fact_raw = read_bronze(layout, fact)
    if fact_raw is None or fact_raw.height == 0:
        return None
    fact_cols = list(fact_raw.columns)
    frame = _clean_table(fact_raw, pk_by_table.get(fact), redaction, entity=fact)

    joined_tables = {fact}
    for e in edges:
        if e.left_table != fact or e.right_table in joined_tables:
            continue
        dim_raw = read_bronze(layout, e.right_table)
        if dim_raw is None:
            continue
        dim = _clean_table(dim_raw, e.right_col, redaction,
                           entity=e.right_table, is_dim=True)
        lk = resolve_column(e.left_col, frame.columns)
        rk = resolve_column(e.right_col, dim.columns)
        if lk is None or rk is None:
            continue
        frame = frame.join(dim, how="left", left_on=lk, right_on=rk)
        joined_tables.add(e.right_table)

    frame = _derive_columns(frame)
    measures = _build_measures(frame, fact_cols)
    measure_cols = {m.column for m in measures.values()}
    dimensions = [
        c for c in frame.columns
        if c not in measure_cols and not _ID_RE.search(c)
        and not redaction.is_redacted(c)
    ]
    return ConformedModel(
        layout=layout, fact=fact, frame=frame,
        measures=measures, dimensions=dimensions, edges=edges,
    )


# ---------------------------------------------------------------------------
# Live aggregation
# ---------------------------------------------------------------------------


def aggregate_measure(
    model: ConformedModel, measure_name: str, *,
    group_by: list[str] | None = None, filters: dict[str, Any] | None = None,
) -> pl.DataFrame:
    """Aggregate one measure over the conformed star at any grain, live."""
    from core.dashboard.model.aggregate import apply_filters
    m = model.measures.get(measure_name)
    if m is None:
        raise KeyError(f"unknown measure {measure_name!r}")
    df = apply_filters(model.frame, filters)
    gb = [c for c in (resolve_column(g, df.columns) for g in (group_by or [])) if c]

    if m.agg == "count":
        agg_expr = pl.len().alias(m.name)
    elif m.agg == "count_distinct":
        agg_expr = pl.col(resolve_column(m.column, df.columns)).n_unique().alias(m.name)
    else:  # sum
        col = resolve_column(m.column, df.columns)
        agg_expr = pl.col(col).sum().alias(m.name)

    if not gb:
        return df.select(agg_expr) if m.agg != "count" else df.select(pl.len().alias(m.name))
    return df.group_by(gb, maintain_order=True).agg(agg_expr)


__all__ = [
    "ConformedModel", "Edge", "Measure", "aggregate_measure", "approved_edges",
    "build_conformed_model",
]
