"""Cross-filter + drill engine over the Phase 1 live model.

A "canvas" is all of a workspace's gold KPIs loaded together. A global filter set
(``{dimension: value(s)}``) is applied to EACH KPI that actually exposes that
column -- so clicking "Department = Cardiology" refilters every KPI that has a
department column, while leaving the others whole. There is no shared bronze
model; the linkage is by matching column NAMES across independent gold tables
(case-insensitive), which is faithful and never re-derives upstream logic.

All aggregation goes through Phase 1 `aggregate`, so additivity rules (sum /
single-attribution share are additive; ratio/avg are not) and parity with the
validated headline are preserved.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

import polars as pl

from core.dashboard.model.aggregate import (
    NonAdditiveError,
    aggregate,
    apply_filters,
    resolve_column,
)
from core.dashboard.model.cuts import KpiModel, build_kpi_model
from core.dashboard.model.layers import list_gold_kpis, read_gold, read_silver
from core.storage.workspace_layout import WorkspaceLayout


@dataclass(frozen=True)
class CanvasModel:
    """Every gold KPI in a workspace, loaded for cross-filtering."""

    layout: WorkspaceLayout
    kpis: dict[str, KpiModel] = field(default_factory=dict)
    gold: dict[str, pl.DataFrame] = field(default_factory=dict)

    def kpi_ids(self) -> list[str]:
        return list(self.kpis.keys())

    def shared_dimensions(self) -> dict[str, list[str]]:
        """Map a lower-cased dimension name -> the KPI ids that expose it.

        This is what makes a slicer "global": a dimension present in >1 KPI can
        cross-filter all of them. Single-KPI dimensions still slice their own KPI.
        """
        out: dict[str, list[str]] = {}
        for kid, model in self.kpis.items():
            for cut in model.cuts:
                out.setdefault(cut.lower(), []).append(kid)
        return out


def load_canvas(layout: WorkspaceLayout) -> CanvasModel:
    """Load all gold KPIs (model + frame) for a workspace."""
    kpis: dict[str, KpiModel] = {}
    gold_frames: dict[str, pl.DataFrame] = {}
    for kpi_id in list_gold_kpis(layout):
        gold = read_gold(layout, kpi_id)
        if gold is None or gold.height == 0:
            continue
        kpis[kpi_id] = build_kpi_model(layout, kpi_id, gold)
        gold_frames[kpi_id] = gold
    return CanvasModel(layout=layout, kpis=kpis, gold=gold_frames)


def apply_global_filters(
    canvas: CanvasModel, filters: dict[str, Any] | None
) -> dict[str, pl.DataFrame]:
    """Apply a global filter set to every KPI that has the filtered column.

    A KPI lacking a filtered column is returned UNFILTERED for that key (the
    other keys still apply). ``apply_filters`` already ignores unknown columns
    case-insensitively, so this is just per-KPI row filtering.
    """
    out: dict[str, pl.DataFrame] = {}
    for kid, gold in canvas.gold.items():
        out[kid] = apply_filters(gold, filters)
    return out


def _order_and_limit(
    frame: pl.DataFrame, measure: str, panel: dict[str, Any]
) -> pl.DataFrame:
    """Top-N by measure when the panel declares a limit (ranked_bar etc.)."""
    limit = panel.get("limit")
    if not limit:
        return frame
    mcol = resolve_column(measure, frame.columns)
    if mcol is None:
        return frame.head(int(limit))
    return frame.sort(mcol, descending=True, nulls_last=True).head(int(limit))


def panel_data(
    kpi_model: KpiModel,
    gold: pl.DataFrame,
    panel: dict[str, Any],
    filters: dict[str, Any] | None = None,
) -> pl.DataFrame:
    """Aggregate gold for one interns panel dict, honoring the active filters.

    Panel keys consumed: x, color (optional), y (measure, optional), limit.
    The chart RECOMMENDATION (which chart, which x/color) is taken verbatim from
    the panel -- this only produces the data behind it.
    """
    measure = panel.get("y") or kpi_model.measure
    group_by = [c for c in (panel.get("x"), panel.get("color")) if c]
    agg = aggregate(
        gold,
        measure=measure,
        additive=kpi_model.additive,
        group_by=group_by,
        filters=filters,
    )
    return _order_and_limit(agg, measure, panel)


def drill(
    layout: WorkspaceLayout,
    kpi_model: KpiModel,
    gold: pl.DataFrame,
    group_by: list[str],
    filters: dict[str, Any] | None = None,
) -> pl.DataFrame | None:
    """Re-aggregate to an arbitrary grain.

    - All requested columns present in gold -> roll up gold (additive) or
      filter-only (non-additive, via aggregate which raises on recombination).
    - A requested column absent from gold (a finer/raw grain) -> fall back to
      SILVER, but only if silver covers EVERY requested column AND the measure;
      otherwise return None to signal "drill unavailable at this grain". Silver is
      used as-is (already joined+derived upstream); no logic is re-derived here.
    - Returns None instead of raising on a non-additive recombination, so the UI
      can disable the control rather than error.
    """
    gold_cols = gold.columns
    missing = [g for g in group_by if resolve_column(g, gold_cols) is None]
    try:
        if not missing:
            return aggregate(
                gold, measure=kpi_model.measure,
                additive=kpi_model.additive, group_by=group_by, filters=filters,
            )
    except NonAdditiveError:
        return None

    silver = read_silver(layout, kpi_model.kpi_id)
    if silver is None:
        return None
    s_cols = silver.columns
    if resolve_column(kpi_model.measure, s_cols) is None:
        return None
    if any(resolve_column(g, s_cols) is None for g in group_by):
        return None
    try:
        return aggregate(
            silver, measure=kpi_model.measure,
            additive=kpi_model.additive, group_by=group_by, filters=filters,
        )
    except NonAdditiveError:
        return None


__all__ = [
    "CanvasModel",
    "apply_global_filters",
    "drill",
    "load_canvas",
    "panel_data",
]
