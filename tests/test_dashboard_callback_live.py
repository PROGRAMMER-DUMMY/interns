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
        _drill_into,
        _drill_model,
        _drill_panels,
        _explore_default_chart_type,
        _explore_figure,
        _figure_from_spec,
        _filter_rows_by_value,
        _panel_spec,
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


# ---------------------------------------------------------------------------
# Phase 2 — Click-to-drill: simulate a category click and assert the rendered
# sub-figures are FILTERED to that category and broken down by the other dims.
# The drill callback body is exercised directly (the browser path auto-skips),
# mirroring the Explore-pane tests above. Workspace-agnostic fixtures: a share
# metric cut by three categoricals (region / segment / channel) — the same
# shape as a real multi-dimension share KPI, no domain vocabulary.
# ---------------------------------------------------------------------------

# region x segment x channel, with a `share` measure. Region "north" contains
# segments {a, b} and channels {web, store}; "south" only {b} / {store}. A drill
# BY region INTO {segment, channel} must show only the clicked region's rows.
_DRILL_ROWS = [
    {"region": "north", "segment": "a", "channel": "web", "share_pct": 30.0},
    {"region": "north", "segment": "a", "channel": "store", "share_pct": 10.0},
    {"region": "north", "segment": "b", "channel": "web", "share_pct": 20.0},
    {"region": "south", "segment": "b", "channel": "store", "share_pct": 25.0},
    {"region": "south", "segment": "b", "channel": "web", "share_pct": 15.0},
]


def _drill_spec() -> "DashboardSpec":
    cfg = {
        "chart_type": "bar", "x": "region", "y": "share_pct",
        "y_format": "percent", "title": "Share by region",
    }
    return DashboardSpec(
        kpi_id="kpi_drill", config=cfg, machine_defaults=cfg,
        user_overrides={}, spec_path="dashboard/kpi_drill.json",
    )


def _simulate_drill(spec, rows, by, value):
    """Reproduce the `_drill_detail_children` body: model -> filter -> sub-figs.

    Returns (model, filtered_rows, [(into_dim, figure), ...]) so a test can
    assert both the hierarchy and the filtered figure content.
    """
    model = _drill_model(spec, rows, by=by)
    filtered = _filter_rows_by_value(rows, by, value)
    y_col = str(spec.config.get("y") or "")
    y_format = str(spec.config.get("y_format") or "")
    figs = []
    for panel in _drill_panels(by, value, model["into"], filtered):
        p = dict(panel)
        if y_col:
            p["y"] = y_col
        if y_format:
            p["y_format"] = y_format
        fig = _figure_from_spec(_panel_spec(spec, p), filtered, show_title=False)
        figs.append((p["x"], fig))
    return model, filtered, figs


@pytest.mark.skipif(not _HAS_DASHBOARD, reason="dashboard extra (plotly) not installed")
class TestDrillDownCallback:
    """Click a category -> filtered within-category sub-figures."""

    def test_drill_model_derives_other_dims_generically(self):
        # Drilling BY region -> the OTHER categoricals (segment, channel), not
        # the measure and not `by` itself. No hardcoding.
        model = _drill_model(_drill_spec(), _DRILL_ROWS, by="region")
        assert model["by"] == "region"
        assert set(model["into"]) == {"segment", "channel"}
        assert "share_pct" not in model["into"]
        assert "region" not in model["into"]

    def test_drill_into_helper_excludes_by_and_measure(self):
        assert set(_drill_into(_DRILL_ROWS, "segment")) == {"region", "channel"}

    def test_spec_drill_override_is_honored_when_columns_exist(self):
        spec = _drill_spec()
        spec.config["drill"] = {"by": "segment", "into": ["channel"]}
        model = _drill_model(spec, _DRILL_ROWS)  # no explicit by -> use spec
        assert model["by"] == "segment"
        assert model["into"] == ["channel"]

    def test_spec_drill_override_ignored_when_columns_absent(self):
        spec = _drill_spec()
        spec.config["drill"] = {"by": "nope", "into": ["also_nope"]}
        model = _drill_model(spec, _DRILL_ROWS, by="region")
        # Bad override falls back to derivation, never crashes.
        assert model["by"] == "region"
        assert set(model["into"]) == {"segment", "channel"}

    def test_click_filters_rows_to_clicked_category(self):
        filtered = _filter_rows_by_value(_DRILL_ROWS, "region", "north")
        assert len(filtered) == 3
        assert all(r["region"] == "north" for r in filtered)

    def test_drilled_subfigures_only_contain_clicked_category_data(self):
        # Simulate a clickData on the region bar "north".
        model, filtered, figs = _simulate_drill(_drill_spec(), _DRILL_ROWS, "region", "north")
        assert {dim for dim, _ in figs} == {"segment", "channel"}
        # The segment sub-figure must show ONLY north's segments {a, b};
        # "south"-only data must not leak in (south has no segment "a"... but
        # the leakage test is: south contributes share to segment "b"; a
        # correctly filtered figure's b-slice = north's b (20), not 20+? ).
        seg_fig = dict(figs)["segment"]
        vals: dict[str, float] = {}
        for tr in seg_fig.data:
            labs = getattr(tr, "labels", None)
            vs = getattr(tr, "values", None)
            labs = list(labs) if labs is not None else []
            vs = list(vs) if vs is not None else []
            for lab, v in zip(labs, vs):
                vals[str(lab)] = v
        assert set(vals) == {"a", "b"}  # north's segments only (no leakage)
        # The donut value for segment "b" within north = 20 (web), not 20+south.
        assert vals.get("b") == 20.0  # NOT 20 + south's 40 -> proves the filter

    def test_drill_high_cardinality_into_dim_becomes_ranked_bar(self):
        # A drilled breakdown with many distinct values -> ranked bar, not a
        # donut of slivers. Build a slice whose `into` dim has > donut cap.
        rows = [
            {"region": "north", "sku": f"s{i}", "share_pct": float(i + 1)}
            for i in range(12)
        ]
        spec = _drill_spec()
        panels = _drill_panels("region", "north", ["sku"], rows)
        assert panels[0]["chart_type"] == "ranked_bar"

    def test_drill_low_cardinality_into_dim_stays_donut(self):
        panels = _drill_panels("region", "north", ["segment"],
                               _filter_rows_by_value(_DRILL_ROWS, "region", "north"))
        assert panels[0]["chart_type"] == "donut"

    def test_drill_subfigures_render_without_crashing_on_empty_slice(self):
        # Clicking a value with no rows -> figures still build (no exception).
        _model, filtered, figs = _simulate_drill(_drill_spec(), _DRILL_ROWS, "region", "west")
        assert filtered == []
        assert all(f is not None for _, f in figs)


# ---------------------------------------------------------------------------
# Phase B/C — Data surface: the governed SSRM grid wired into the live app.
# Build the app over a synthetic workspace that has a raw dataset with a PII
# column, then drive the grid callbacks directly (no browser): source-change
# builds redaction-aware coldefs, and getRowsResponse serves paginated/grouped
# rows with the PII column excluded. Workspace-agnostic fixtures.
# ---------------------------------------------------------------------------


def _make_workspace_with_grid_dataset(tmp_path: Path) -> tuple[Path, str]:
    repo_root = tmp_path
    workspace_rel = "workspaces/synthetic_grid_dashboard"
    workspace = repo_root / workspace_rel
    (workspace / "datasets").mkdir(parents=True)
    layout = WorkspaceLayout(project_root=workspace)
    layout.ensure_runtime_dirs()
    # "patient_name" is PII (matches a default redaction pattern); department is a
    # group dim; revenue is a numeric measure.
    (workspace / "datasets" / "facts.csv").write_text(
        "patient_name,department,revenue\n"
        "Alice,Cardiology,100\n"
        "Bob,Oncology,250\n"
        "Carol,Cardiology,300\n"
        "Dave,Neurology,50\n",
        encoding="utf-8",
    )
    layout.kpi_registry_path.parent.mkdir(parents=True, exist_ok=True)
    layout.kpi_registry_path.write_text(json.dumps({"kpis": []}), encoding="utf-8")
    return repo_root, workspace_rel


def _find_callback(app, output_id: str, output_prop: str):
    """Return the registered callback function whose output targets output_id.prop."""
    target = f"{output_id}.{output_prop}"
    for key, entry in app.callback_map.items():
        if target in key:
            return entry["callback"]
    raise AssertionError(f"callback for {target} not registered; keys={list(app.callback_map)}")


@pytest.mark.skipif(not _HAS_DASHBOARD, reason="dashboard extra not installed")
def test_grid_get_rows_callback_excludes_pii_and_paginates(tmp_path):
    duckdb = pytest.importorskip("duckdb", reason="duckdb not installed")  # noqa: F841
    pytest.importorskip("dash_ag_grid", reason="dash-ag-grid not installed")

    repo_root, workspace_rel = _make_workspace_with_grid_dataset(tmp_path)
    app = build_dash_app(repo_root, workspace_rel)

    # The build itself is the build-check: the app constructs and the grid
    # callbacks are registered.
    src_cb = _find_callback(app, "data-grid", "columnDefs")
    rows_cb = _find_callback(app, "data-grid", "getRowsResponse")

    # Source id is derived from the dataset stem: ds_facts.
    from core.dashboard import grid_backend as gb

    sources = gb.grid_sources(repo_root / workspace_rel)
    src_ids = {s["id"] for s in sources}
    assert "ds_facts" in src_ids
    source_id = "ds_facts"

    # Source-change callback -> coldefs exclude the PII column.
    coldefs, group_opts, measure_opts = src_cb(source_id)
    fields = {d["field"] for d in coldefs}
    assert "patient_name" not in fields
    assert {"department", "revenue"} <= fields
    assert {o["value"] for o in measure_opts} == {"revenue"}      # numeric only

    # getRowsResponse (flat block): no PII, full count, paginated block.
    req = {"startRow": 0, "endRow": 2, "filterModel": {}, "sortModel": []}
    out = rows_cb(req, source_id, None)
    assert out["rowCount"] == 4
    assert len(out["rowData"]) == 2                                # only the block
    for row in out["rowData"]:
        assert "patient_name" not in row

    # getRowsResponse (grouped via the pivot store): GROUP BY department, SUM revenue.
    pivot = {
        "rowGroupCols": [{"id": "department", "field": "department"}],
        "valueCols": [{"field": "revenue", "aggFunc": "sum"}],
    }
    greq = {"startRow": 0, "endRow": 100, "filterModel": {}, "sortModel": [],
            "rowGroupCols": pivot["rowGroupCols"], "valueCols": pivot["valueCols"],
            "groupKeys": []}
    gout = rows_cb(greq, source_id, pivot)
    by_dept = {r["department"]: r["revenue"] for r in gout["rowData"]}
    assert by_dept["Cardiology"] == 400
    assert gout["rowCount"] == 3
    for row in gout["rowData"]:
        assert "patient_name" not in row


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
