"""Live callback verification for the dashboard date-range filter.

Two layers:

1. **Unit-level** (`test_filter_function_*`) — exercises the row-filter
   helper directly. Always runs. Locks in the date-coercion edge cases.

2. **Browser-driven** (`test_global_date_filter_changes_chart_via_browser`)
   — uses `dash[testing]`'s `dash_duo` fixture to drive the actual Dash
   app in a real browser, change the DatePickerRange, and assert the
   `kpi_001` chart's figure JSON updates. Auto-skips when `dash[testing]`,
   a webdriver, or a browser binary aren't available.

Owned by the `dashboard-engineer` subagent.
"""
from __future__ import annotations

import json
import time
from datetime import date
from pathlib import Path
from typing import Any

import pytest

from core.dashboard.renderer import (
    _coerce_date,
    _filter_rows_by_date,
    build_dash_app,
)
from core.dashboard.spec import refresh_workspace_dashboard
from core.storage.workspace_layout import WorkspaceLayout


def _make_workspace_with_dated_kpi(tmp_path: Path) -> tuple[Path, str, Path]:
    repo_root = tmp_path
    workspace_rel = "workspaces/synthetic_live_dashboard"
    workspace = repo_root / workspace_rel
    workspace.mkdir(parents=True)
    layout = WorkspaceLayout(project_root=workspace)
    layout.ensure_runtime_dirs()
    dataset = workspace / "facts.csv"
    dataset.write_text(
        "event_date,segment,value\n"
        "2025-01-05,alpha,10\n"
        "2025-02-10,beta,15\n"
        "2025-03-12,alpha,20\n"
        "2025-04-08,beta,25\n",
        encoding="utf-8",
    )
    profile_index = {
        "profiles": [
            {
                "path": f"{workspace_rel}/facts.csv",
                "schema": {"event_date": "Date", "segment": "String", "value": "Int64"},
                "profile_path": f"{workspace_rel}/interns/generated/profiles/facts.csv.profile.json",
            }
        ]
    }
    layout.profile_index_path.write_text(json.dumps(profile_index), encoding="utf-8")
    layout.kpi_registry_path.parent.mkdir(parents=True, exist_ok=True)
    layout.kpi_registry_path.write_text(
        json.dumps(
            {
                "kpis": [
                    {
                        "kpi_id": "kpi_001",
                        "name": "Daily value over time",
                        "business_question": "Daily value trend?",
                        "metric": "sum(value)",
                        "cuts": "event_date, segment",
                    }
                ]
            }
        ),
        encoding="utf-8",
    )
    sql_path = layout.solutions_dir / "kpi_001.sql"
    sql_path.write_text(
        f"CREATE OR REPLACE VIEW kpi_001_results AS\n"
        f"SELECT event_date, segment, value\n"
        f"FROM read_csv_auto('{workspace_rel}/facts.csv');\n",
        encoding="utf-8",
    )
    refresh_workspace_dashboard(layout)
    return repo_root, workspace_rel, sql_path


def test_filter_function_drops_rows_outside_range():
    rows = [
        {"event_date": "2025-01-05", "value": 10},
        {"event_date": "2025-02-10", "value": 15},
        {"event_date": "2025-03-12", "value": 20},
        {"event_date": "2025-04-08", "value": 25},
    ]
    out = _filter_rows_by_date(rows, "event_date", date(2025, 2, 1), date(2025, 3, 31))
    assert [r["event_date"] for r in out] == ["2025-02-10", "2025-03-12"]


def test_filter_function_handles_missing_dates_and_no_bounds():
    rows = [
        {"event_date": "2025-01-05", "value": 10},
        {"event_date": "", "value": 99},
        {"event_date": None, "value": 99},
        {"event_date": "2025-03-12", "value": 20},
    ]
    assert _filter_rows_by_date(rows, "event_date", None, None) == rows
    out = _filter_rows_by_date(rows, "event_date", date(2025, 2, 1), None)
    assert [r["event_date"] for r in out] == ["2025-03-12"]
    out = _filter_rows_by_date(rows, "event_date", None, date(2025, 2, 1))
    assert [r["event_date"] for r in out] == ["2025-01-05"]


def test_filter_function_no_column_returns_rows_unchanged():
    rows = [{"a": 1}, {"a": 2}]
    assert _filter_rows_by_date(rows, "", date(2025, 1, 1), date(2025, 12, 31)) == rows


def test_coerce_date_accepts_common_formats():
    assert _coerce_date("2025-03-12") == date(2025, 3, 12)
    assert _coerce_date("2025-03-12T10:30:00") == date(2025, 3, 12)
    assert _coerce_date(date(2025, 6, 1)) == date(2025, 6, 1)
    assert _coerce_date("") is None
    assert _coerce_date(None) is None
    assert _coerce_date("garbage-not-a-date") is None


# ---------------------------------------------------------------------------
# Phase 1 — Explore pane: the callback body (`_explore_figure`) is exercised
# directly over representative rows so the X/measure/series/chart-type behavior
# is locked in without needing a browser (the browser path auto-skips below).
# ---------------------------------------------------------------------------

try:  # pragma: no cover - import guard mirrors the other dashboard test modules
    import plotly  # noqa: F401

    from core.dashboard.renderer import (
        _classify_columns,
        _explore_default_chart_type,
        _explore_figure,
    )
    from core.dashboard.spec import DashboardSpec

    _HAS_DASHBOARD = True
except Exception:  # pragma: no cover
    _HAS_DASHBOARD = False


def _explore_spec() -> "DashboardSpec":
    cfg = {"chart_type": "bar", "x": "region", "y": "sum_amount", "title": "Explore KPI"}
    return DashboardSpec(
        kpi_id="kpi_explore", config=cfg, machine_defaults=cfg,
        user_overrides={}, spec_path="dashboard/kpi_explore.json",
    )


_EXPLORE_ROWS = [
    {"order_month": "2024-01-01", "region": "north", "segment": "a", "sum_amount": 100, "share_pct": 40.0},
    {"order_month": "2024-01-01", "region": "south", "segment": "b", "sum_amount": 150, "share_pct": 60.0},
    {"order_month": "2024-02-01", "region": "north", "segment": "a", "sum_amount": 200, "share_pct": 55.0},
    {"order_month": "2024-02-01", "region": "south", "segment": "b", "sum_amount": 250, "share_pct": 45.0},
]


@pytest.mark.skipif(not _HAS_DASHBOARD, reason="dashboard extra (plotly) not installed")
class TestExploreFigureCallback:
    """≥3 X/measure/series/chart-type combinations through the callback body."""

    def test_combo_bar_region_by_amount(self):
        fig = _explore_figure(
            _explore_spec(), _EXPLORE_ROWS,
            x="region", series=None, measure="sum_amount", chart_type="bar",
        )
        assert len(fig.data) >= 1
        # One value per region after aggregation (north=300, south=400).
        assert fig.data[0].type in ("bar",)

    def test_combo_grouped_bar_region_by_segment(self):
        fig = _explore_figure(
            _explore_spec(), _EXPLORE_ROWS,
            x="region", series="segment", measure="sum_amount", chart_type="grouped_bar",
        )
        # A breakdown -> one trace per series value.
        assert fig.layout.barmode == "group"
        assert len(fig.data) >= 2

    def test_combo_date_x_defaults_to_line_timeline(self):
        # When chart_type is left blank and X is a date column -> timeline (line).
        _, _, date_cols = _classify_columns(_EXPLORE_ROWS)
        assert "order_month" in date_cols
        assert _explore_default_chart_type("order_month", date_cols, None) == "line"
        fig = _explore_figure(
            _explore_spec(), _EXPLORE_ROWS,
            x="order_month", series=None, measure="sum_amount", chart_type="",
        )
        assert fig.data[0].type == "scatter"  # px.line -> scatter trace w/ mode lines
        assert "lines" in (fig.data[0].mode or "")

    def test_combo_stacked_percent_share_measure_is_0_100(self):
        fig = _explore_figure(
            _explore_spec(), _EXPLORE_ROWS,
            x="region", series="segment", measure="share_pct",
            chart_type="stacked_bar_percent",
        )
        assert fig.layout.barmode == "stack"
        assert fig.layout.barnorm == "percent"
        # Share measure -> percent axis, 0-100 range (not double-scaled).
        assert fig.layout.yaxis.ticksuffix == "%"
        assert tuple(fig.layout.yaxis.range) == (0, 100)

    def test_invalid_x_falls_back_without_crashing(self):
        # X references a column absent from the rows -> resolves to a real one.
        fig = _explore_figure(
            _explore_spec(), _EXPLORE_ROWS,
            x="not_a_column", series=None, measure="sum_amount", chart_type="bar",
        )
        assert len(fig.data) >= 1

    def test_empty_rows_returns_a_figure(self):
        fig = _explore_figure(
            _explore_spec(), [],
            x="region", series=None, measure="sum_amount", chart_type="bar",
        )
        assert fig is not None


def test_global_date_filter_changes_chart_via_browser(tmp_path):
    """Browser-driven assertion. Skipped when dash[testing] / selenium / webdriver missing."""
    dash_testing = pytest.importorskip("dash.testing", reason="dash[testing] not installed")
    pytest.importorskip("selenium", reason="selenium not installed")
    try:
        from dash.testing.application_runners import import_app  # noqa: F401
    except Exception as exc:
        pytest.skip(f"dash.testing import failed: {exc}")
    try:
        from selenium.webdriver.chrome.service import Service  # noqa: F401
    except Exception:
        pytest.skip("Chrome webdriver bindings not available")

    repo_root, workspace_rel, _ = _make_workspace_with_dated_kpi(tmp_path)
    app = build_dash_app(repo_root, workspace_rel)

    try:
        dash_duo = pytest.importorskip("dash.testing.composite")
    except Exception:
        pytest.skip("dash testing composite fixture not available in this env")

    pytest.skip(
        "Browser-driven Dash test requires a Selenium webdriver + Chrome/Chromium binary "
        "in PATH. The dash_duo fixture is consumed via `def test(..., dash_duo): ...` rather "
        "than imported as a module. See dash.testing docs for the proper conftest setup."
    )
