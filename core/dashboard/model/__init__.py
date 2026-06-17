"""Live dashboard model: read the validated medallion gold/silver layers and
re-aggregate them in memory for Power BI-style cross-filter / drill, instead of
re-running each KPI's solution SQL on every render.

Layer roles (see PLAN_dashboard_powerbi_upgrade.md):
  gold   <kpi>_results   the validated KPI answer (filter + cuts applied)
  silver <kpi>_features  row-grain joined+derived features (best-effort deep drill)

The chart *selection* still comes from `core.dashboard.profile` / `chart_knowledge`
(the interns recommendation engine). This package only owns the DATA: reading the
validated layers and re-aggregating them consistently with the validated headline.
"""
from __future__ import annotations

from core.dashboard.model.aggregate import NonAdditiveError, aggregate
from core.dashboard.model.cuts import KpiModel, build_kpi_model, classify_measure
from core.dashboard.model.layers import (
    list_gold_kpis,
    read_gold,
    read_silver,
)
from core.dashboard.model.parity import check_parity, parity_report

__all__ = [
    "KpiModel",
    "NonAdditiveError",
    "aggregate",
    "build_kpi_model",
    "check_parity",
    "classify_measure",
    "list_gold_kpis",
    "parity_report",
    "read_gold",
    "read_silver",
]
