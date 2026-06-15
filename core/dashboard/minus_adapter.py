"""Generic adapter: interns workspace -> a vendored-MinusAnalyst project + data.

Turns ANY workspace's DQ-certified conformed model into the inputs the vendored
MinusAnalyst app (vendor/minus) consumes:
  - data/conformed.parquet   the certified-clean conformed star (columnar)
  - project.yaml             datasource + table + reusable measures
  - config/dashboards/*.yaml curated pages (KPI overview + detail)

Workspace-agnostic: measures come from the conformed model's fact numerics
(+ a derived collection_rate when a paid and a gross amount both exist), and
chart types are chosen from each dimension's cardinality. Nothing healthcare- or
workspace-specific is hardcoded.

Publishing is DQ-gated: if `dq.certify` fails, we do not (re)write the data, so a
running dashboard keeps the last-good snapshot.
"""
from __future__ import annotations

import re
from pathlib import Path
from typing import Any

import polars as pl
import yaml

from core.dashboard.model.aggregate import resolve_column
from core.dashboard.model.conformed import ConformedModel, build_conformed_model
from core.dashboard.model.cuts import build_kpi_model
from core.dashboard.model.dq import certify
from core.dashboard.model.layers import list_gold_kpis, read_gold
from core.storage.workspace_layout import WorkspaceLayout

_TABLE = "claims"   # the single conformed (pre-joined) table MinusAnalyst reads
_DATE_ISH = ("date", "_at", "_dt")


def minus_root(layout: WorkspaceLayout) -> Path:
    return layout.state_dir / "minus"


def _slug(name: str) -> str:
    s = re.sub(r"[^0-9a-zA-Z]+", "_", str(name)).strip("_").lower()
    return s or "m"


def _fact_pk(model: ConformedModel) -> str | None:
    for c in model.frame.columns:
        if c.lower() == f"{model.fact.rstrip('s')}id" or c.lower().endswith("id"):
            return c
    return None


# ---------------------------------------------------------------------------
# Measures
# ---------------------------------------------------------------------------


def _measure_specs(model: ConformedModel) -> list[dict[str, Any]]:
    specs: list[dict[str, Any]] = []
    paid_slug = amount_slug = None
    for m in model.measures.values():
        slug = _slug(m.name)
        if m.agg == "count":
            specs.append({"name": slug, "label": m.name, "agg": "count",
                          "table": _TABLE, "fmt": m.fmt or "int"})
        else:
            specs.append({"name": slug, "label": m.name, "agg": m.agg,
                          "field": f"{_TABLE}.{m.column}", "fmt": m.fmt or "currency"})
            cl = m.column.lower()
            if "paid" in cl:
                paid_slug = slug
            elif "amount" in cl and amount_slug is None:
                amount_slug = slug
    # Collection Rate = paid / gross amount * 100 -- a real RCM KPI when both exist.
    # Net Collection Rate benchmark is >=96% (RCM 2026), higher is better.
    if paid_slug and amount_slug:
        specs.append({"name": "collection_rate", "label": "Collection Rate",
                      "kind": "expression",
                      "expression": f"{paid_slug} / {amount_slug} * 100",
                      "fmt": "percent", "target": 96.0, "goal": "higher"})
    # Days in A/R = avg(paid date - service date). RCM benchmark <30 days, lower better.
    if "ar_days" in model.frame.columns:
        specs.append({"name": "days_in_ar", "label": "Days in A/R", "agg": "avg",
                      "field": f"{_TABLE}.ar_days", "fmt": "float",
                      "target": 30.0, "goal": "lower"})
    return specs


def _measure_field_columns(measures: list[dict[str, Any]]) -> set[str]:
    """Source columns consumed by measures (excluded from dimension lists)."""
    cols: set[str] = set()
    for m in measures:
        fld = m.get("field")
        if fld and "." in fld:
            cols.add(fld.split(".", 1)[1])
    return cols


# ---------------------------------------------------------------------------
# Dimensions + chart selection
# ---------------------------------------------------------------------------


# A categorical dimension is only a readable chart up to this many categories;
# higher-cardinality columns (procedure/diagnosis codes, ids) make poor overview
# charts and are excluded (still reachable via Detail / drill).
_MAX_DIM_CARDINALITY = 50


def _display_dimensions(model: ConformedModel) -> list[tuple[str, int]]:
    """(dimension, distinct) worth charting, ordered low-cardinality first,
    skipping raw dates (keep 'month'), constants, and high-cardinality columns."""
    out: list[tuple[str, int]] = []
    for d in model.dimensions:
        dl = d.lower()
        if dl != "month" and any(t in dl for t in _DATE_ISH):
            continue
        distinct = model.frame.get_column(d).n_unique()
        if distinct <= 1 or (dl != "month" and distinct > _MAX_DIM_CARDINALITY):
            continue
        out.append((d, distinct))
    out.sort(key=lambda t: (t[0].lower() != "month", t[1]))  # month first, then low-card
    return out


def _diversified_chart_widgets(dims: list[tuple[str, int]], measure: str) -> list[dict[str, Any]]:
    """Assign varied chart types so the canvas is not a wall of donuts:
    month -> line; up to 2 lowest-card -> donut; mid -> bar; high-card -> hbar."""
    widgets: list[dict[str, Any]] = []
    cat = [(d, n) for d, n in dims if d.lower() != "month"]
    for d, n in [(d, n) for d, n in dims if d.lower() == "month"][:1]:
        widgets.append({"id": f"w_{_slug(d)}", "type": "line", "measure": measure,
                        "dimension": f"{_TABLE}.{d}", "title": f"Trend by {_human(d)}",
                        "width": 12, "height": 340})
    donut_budget = 2
    for d, n in sorted(cat, key=lambda x: x[1]):  # lowest cardinality first
        common = {"id": f"w_{_slug(d)}", "measure": measure,
                  "dimension": f"{_TABLE}.{d}", "title": f"By {_human(d)}", "height": 340}
        if n <= 5 and donut_budget > 0:
            widgets.append({**common, "type": "donut", "width": 5})
            donut_budget -= 1
        elif n > 12:
            widgets.append({**common, "type": "hbar", "limit": 12, "width": 7})
        else:
            widgets.append({**common, "type": "bar", "width": 6})
    return widgets


def _widget_for(dim: str, distinct: int, measure_slug: str, idx: int) -> dict[str, Any]:
    field = f"{_TABLE}.{dim}"
    common = {"id": f"w_{_slug(dim)}", "measure": measure_slug,
              "dimension": field, "height": 340}
    if dim.lower() == "month":
        return {**common, "type": "line", "title": f"Trend by {_human(dim)}", "width": 12}
    if distinct <= 6:
        return {**common, "type": "donut", "title": f"By {_human(dim)}", "width": 5}
    if distinct <= 12:
        return {**common, "type": "bar", "title": f"By {_human(dim)}", "width": 6}
    return {**common, "type": "hbar", "title": f"By {_human(dim)}", "limit": 12, "width": 7}


def _human(name: str) -> str:
    return re.sub(r"(?<=[a-z0-9])(?=[A-Z])", " ", str(name)).replace("_", " ").strip().title()


# ---------------------------------------------------------------------------
# Workspace-defined KPIs (gold-backed): surface the ACTUAL KPIs, not just
# generic measures. Each gold result becomes its own table + measure + a tile
# on a dedicated "KPIs" page, showing the validated answer.
# ---------------------------------------------------------------------------


def _kpi_artifacts(layout: WorkspaceLayout):
    """Return (tables, measures, page, exports) for every gold KPI.

    exports maps table-name -> gold frame (written to parquet by generate()).
    """
    tables: list[dict[str, Any]] = []
    measures: list[dict[str, Any]] = []
    cards: list[dict[str, Any]] = []     # headline strip (all KPIs together)
    charts: list[dict[str, Any]] = []    # per-KPI breakdowns, 2-up below
    exports: dict[str, pl.DataFrame] = {}

    kpis = list_gold_kpis(layout)
    card_w = max(3, 12 // max(1, len(kpis)))
    for kpi_id in kpis:
        gold = read_gold(layout, kpi_id)
        if gold is None or gold.height == 0:
            continue
        km = build_kpi_model(layout, kpi_id, gold)
        tname = kpi_id  # e.g. "kpi_001"
        mslug = _slug(f"{kpi_id}_{km.measure}")
        fmt = "percent" if (km.y_format or "").lower() == "percent" else "currency"
        exports[tname] = gold
        tables.append({"name": tname, "source": "conformed_src",
                       "file": f"{tname}.parquet", "label": km.card_label})
        measures.append({"name": mslug, "label": km.card_label, "agg": "sum",
                         "field": f"{tname}.{km.measure}", "fmt": fmt})
        cards.append({"id": f"k_{tname}", "type": "kpi", "measure": mslug,
                      "width": card_w, "height": 132})
        # Render the interns-recommended panels (rich: multi-series, breakdowns,
        # heatmaps). Fall back to simple per-cut charts only if no panels exist.
        rich = _kpi_panel_widgets(tname, mslug, km.card_label, km.measure, gold, km.panels or [])
        charts.extend(rich or _kpi_breakdown_charts(tname, mslug, km.card_label, gold, km.cuts))

    if not cards:
        return tables, measures, None, exports
    page = {
        "id": "kpis", "title": "KPIs", "order": 5,
        "description": "The workspace's defined KPIs, served from the validated gold layer.",
        "filters": [],
        "widgets": cards + charts,
    }
    return tables, measures, page, exports


def _kpi_breakdown_charts(tname, mslug, label, gold, cuts) -> list[dict[str, Any]]:
    """Up to 2 charts per KPI: the measure by its lead cuts, diversified
    (month -> line, one donut, else bar/hbar), 2-up grid."""
    out: list[dict[str, Any]] = []
    donut_budget = 1
    for cut in cuts[:2]:
        if cut not in gold.columns:
            continue
        distinct = gold.get_column(cut).n_unique()
        common = {"id": f"c_{tname}_{_slug(cut)}", "measure": mslug,
                  "dimension": f"{tname}.{cut}",
                  "title": f"{label} by {_human(cut)}", "height": 320, "width": 6}
        if cut.lower() == "month":
            out.append({**common, "type": "line"})
        elif distinct <= 5 and donut_budget > 0:
            out.append({**common, "type": "donut"})
            donut_budget -= 1
        elif distinct > 12:
            out.append({**common, "type": "hbar", "limit": 12})
        else:
            out.append({**common, "type": "bar"})
    return out


# interns chart_type -> MinusAnalyst widget type. The interns recommendation
# engine already chose rich panels per KPI (multi-series, breakdowns, heatmaps);
# we render THOSE so each KPI is shown across its cuts, not minimally.
_CHART_TYPE_MAP = {
    "line": "line", "stacked_area": "area",
    "bar": "bar", "grouped_bar": "bar", "stacked_bar_percent": "bar",
    "ranked_bar": "hbar", "donut": "donut", "pie": "pie",
    "heatmap": "heatmap", "scatter": "scatter",
}


def _clean_panel_title(raw: str, measure_col: str, label: str, x: str) -> str:
    if not raw:
        return f"{label} by {_human(x)}"
    phrase = _human(measure_col)  # e.g. "Sum Paidamount"
    if phrase and label and phrase.lower() in raw.lower():
        return re.sub(re.escape(phrase), label, raw, flags=re.IGNORECASE)
    return raw


def _kpi_panel_widgets(tname, mslug, label, measure_col, gold, panels) -> list[dict[str, Any]]:
    """Render the interns-recommended panels for a KPI as MinusAnalyst widgets on
    its gold table -- multi-series lines, grouped bars, heatmaps, etc. (rich,
    showing the metric across its cuts), instead of a couple of minimal charts."""
    cols = list(gold.columns)
    out: list[dict[str, Any]] = []
    seen: set = set()
    for i, panel in enumerate(panels[:5]):
        ct = str(panel.get("chart_type") or "").lower()
        if ct in ("big_number", ""):
            continue
        x = resolve_column(panel.get("x"), cols)
        if x is None:
            continue
        color = resolve_column(panel.get("color"), cols)
        # Dedup on (x, color) so a 2-categorical interaction panel (e.g. a
        # heatmap of dept x age_band) survives alongside a plain bar on dept.
        key = (x, color or "")
        if key in seen:
            continue
        seen.add(key)
        wtype = _CHART_TYPE_MAP.get(ct, "bar")
        w = {"id": f"p_{tname}_{i}", "type": wtype, "measure": mslug,
             "dimension": f"{tname}.{x}",
             "title": _clean_panel_title(panel.get("title"), measure_col, label, x),
             "width": 6, "height": 320}
        if color and color != x and wtype in ("bar", "line", "area", "heatmap"):
            w["breakdown"] = f"{tname}.{color}"
        if ct == "ranked_bar" or panel.get("limit"):
            w["limit"] = int(panel.get("limit") or 12)
        out.append(w)
    return out


# ---------------------------------------------------------------------------
# Project + dashboards
# ---------------------------------------------------------------------------


def build_minus_project(model: ConformedModel) -> tuple[dict, list[dict], dict[str, pl.DataFrame]]:
    measures = _measure_specs(model)
    pk = _fact_pk(model)
    kpi_tables, kpi_measures, kpi_page, kpi_exports = _kpi_artifacts(model.layout)
    project = {
        "name": f"{model.layout.project_root.name}",
        "datasources": [{"name": "conformed_src", "type": "parquet", "path": "data"}],
        "tables": [{"name": _TABLE, "source": "conformed_src",
                    "file": "conformed.parquet", "primary_key": pk, "label": "Claims"}]
                  + kpi_tables,
        "measures": measures + kpi_measures,
        "theme": {"name": "claude"},
        "agent": {"enabled": False},
        "dashboards_dir": "config/dashboards",
    }

    mfields = _measure_field_columns(measures)
    dims = [(d, dist) for d, dist in _display_dimensions(model) if d not in mfields]
    primary = next((m["name"] for m in measures if m.get("agg") == "sum"),
                   measures[0]["name"] if measures else "record_count")
    # KPI row: a balanced FOUR cards (fills the 12-col row evenly), prioritizing
    # the RCM KPIs, with a period-over-period trend when a service date exists.
    svc_date = next((c for c in model.frame.columns
                     if c.lower() in ("servicedate", "service_date")), None)
    def _kpi_tile(m):
        w = {"id": f"k_{m['name']}", "type": "kpi", "measure": m["name"],
             "width": 3, "height": 132}
        if svc_date:
            w["compare"] = f"{_TABLE}.{svc_date}"
            w["compare_period"] = "quarter"
        return w
    _priority = {"record_count": 0, "total_paid_amount": 1, "collection_rate": 2,
                 "days_in_ar": 3}
    row = sorted(measures, key=lambda m: _priority.get(m["name"], 9))[:4]
    kpi_widgets = [_kpi_tile(m) for m in row]
    # Diversified charts (line + a couple donuts + bars + a department hbar) so the
    # canvas is varied AND high-value dims like department are not crowded out.
    chart_widgets = _diversified_chart_widgets(dims[:6], primary)
    # filters from low-cardinality dimensions
    filters = [{"id": f"f_{_slug(d)}", "field": f"{_TABLE}.{d}", "label": _human(d),
                "type": "multi"} for d, dist in dims if dist <= 40][:4]

    overview = {
        "id": "overview", "title": "Overview", "order": 10,
        "description": "Certified-clean conformed model. Click any bar/slice to cross-filter.",
        "filters": filters,
        "widgets": kpi_widgets + chart_widgets,
    }

    # KPIs page first (order 5): the workspace's defined KPIs from gold.
    pages = [kpi_page, overview] if kpi_page else [overview]

    # Detail page: a KPI strip for context + a conditional-format breakdown table
    # for the highest-cardinality categorical dimensions (most detail, e.g.
    # department) rather than the lowest (gender).
    cat_dims = [d for d, _ in sorted(
        [(d, n) for d, n in dims if d.lower() != "month"],
        key=lambda x: -x[1])][:2]
    if cat_dims:
        table_measures = [m["name"] for m in measures][:4]
        conditional = []
        for mname in table_measures:
            mspec = next((m for m in measures if m["name"] == mname), {})
            conditional.append({"column": mname,
                                "type": "color_scale" if mspec.get("fmt") == "percent" else "data_bar"})
        # context strip: the same 4 KPI cards as the overview row
        detail_widgets = [dict(w, id=f"d_{w['id']}") for w in kpi_widgets]
        for cat in cat_dims:
            detail_widgets.append({
                "id": f"tbl_{_slug(cat)}", "type": "table", "title": f"By {_human(cat)}",
                "dimension": f"{_TABLE}.{cat}", "measures": table_measures,
                "sort": "desc", "limit": 50, "width": 12, "height": 460,
                "export": True, "conditional": conditional})
        pages.append({
            "id": "detail", "title": "Detail", "order": 20,
            "description": "Row-level breakdown with conditional formatting. Export to CSV.",
            "filters": filters,
            "widgets": detail_widgets,
        })
    return project, pages, kpi_exports


# ---------------------------------------------------------------------------
# Generate (DQ-gated publish)
# ---------------------------------------------------------------------------


def generate(layout: WorkspaceLayout, *, force: bool = False,
             refresh_seconds: int = 0) -> dict[str, Any]:
    """Build + certify the conformed model, then (if certified) write the
    MinusAnalyst root. Returns a status dict; never raises on a DQ failure.

    DQ-gated publish: when certification fails (and not ``force``), nothing is
    written, so a running dashboard keeps the last-good snapshot. ``refresh_seconds``
    > 0 makes the generated project tell the live app to re-read data that often.
    """
    model = build_conformed_model(layout)
    if model is None:
        return {"ok": False, "reason": "no conformed model (bronze/edges missing)",
                "root": str(minus_root(layout)), "published": False}
    report = certify(model)
    root = minus_root(layout)
    if not report.get("ok") and not force:
        return {"ok": False, "reason": "DQ certification failed",
                "failed": report.get("failed"), "root": str(root), "published": False}

    data_dir = root / "data"
    dash_dir = root / "config" / "dashboards"
    data_dir.mkdir(parents=True, exist_ok=True)
    dash_dir.mkdir(parents=True, exist_ok=True)

    model.frame.write_parquet(data_dir / "conformed.parquet")
    project, pages, kpi_exports = build_minus_project(model)
    if refresh_seconds and refresh_seconds > 0:
        project["refresh_seconds"] = int(refresh_seconds)
    for tname, frame in kpi_exports.items():
        frame.write_parquet(data_dir / f"{tname}.parquet")
    (root / "project.yaml").write_text(
        yaml.safe_dump(project, sort_keys=False, allow_unicode=True, width=100),
        encoding="utf-8")
    # clear stale page files, then write
    for old in dash_dir.glob("*.y*ml"):
        old.unlink()
    for page in pages:
        (dash_dir / f"{page['id']}.yaml").write_text(
            yaml.safe_dump(page, sort_keys=False, allow_unicode=True, width=100),
            encoding="utf-8")
    return {
        "ok": True, "published": True, "root": str(root),
        "fact": model.fact, "rows": model.frame.height,
        "measures": [m["name"] for m in project["measures"]],
        "pages": [p["id"] for p in pages],
        "certified": report.get("ok"), "force": force,
        "refresh_seconds": int(refresh_seconds or 0),
    }


__all__ = ["build_minus_project", "generate", "minus_root"]
