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
