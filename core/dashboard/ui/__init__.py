"""Live dashboard UI: the MinusAnalyst "warm editorial" look-and-feel, rendering
the interns live model (gold layer + recommended panels) as a Power BI-style
canvas.

This package owns presentation only. The data comes from
``core.dashboard.model`` (Phase 1/2): gold is read and re-aggregated there; the
chart RECOMMENDATION comes from the interns spec panels (``decide_panels`` /
``chart_knowledge``). The UI never re-picks charts and never re-runs KPI SQL.

Governance note: redaction + auth are Phase 4. These render from gold, which for
the current KPIs carries no PII; do not bind non-loopback or add slicers over
sensitive columns until Phase 4 lands.
"""
from __future__ import annotations

from core.dashboard.ui.app import build_live_dashboard
from core.dashboard.ui.chart_map import CHART_RENDERERS, map_chart_type

__all__ = ["CHART_RENDERERS", "build_live_dashboard", "map_chart_type"]
