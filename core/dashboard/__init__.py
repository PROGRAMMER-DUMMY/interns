"""Per-workspace BI dashboard.

Generic across workspaces. Each workspace's `dashboard/` directory holds:

- `dashboard/index.json`           — page layout (which KPIs appear, in what order)
- `dashboard/kpi_<id>.json`        — per-KPI chart spec (machine_defaults + user_overrides)
- `dashboard/exports/*.html`       — optional static HTML exports (derived, gitignored)

The two-section JSON contract:

```json
{
  "kpi_id": "kpi_001",
  "machine_defaults": {
    "chart_type": "line",
    "x": "ServiceDate",
    "y": "PaidAmount",
    "agg": "sum",
    "color": "LineOfBusiness",
    "title": "..."
  },
  "user_overrides": {
    "chart_type": "bar",
    "color": "Gender"
  }
}
```

Regeneration rewrites `machine_defaults` but never touches `user_overrides`.
The renderer merges overrides on top of defaults at render time.
"""
from core.dashboard.spec import (
    DashboardSpec,
    build_kpi_spec,
    load_kpi_spec,
    merge_spec,
    refresh_workspace_dashboard,
    save_kpi_spec,
)
from core.dashboard.inference import infer_chart

__all__ = [
    "DashboardSpec",
    "build_kpi_spec",
    "infer_chart",
    "load_kpi_spec",
    "merge_spec",
    "refresh_workspace_dashboard",
    "save_kpi_spec",
]
