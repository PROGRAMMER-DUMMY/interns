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
    return specs


# ---------------------------------------------------------------------------
# Dimensions + chart selection
# ---------------------------------------------------------------------------


def _display_dimensions(model: ConformedModel) -> list[tuple[str, int]]:
    """(dimension, distinct) worth charting, ordered by informativeness-ish
    (lower cardinality first), skipping raw dates (keep 'month'), constants, and
    near-unique columns."""
    n = model.frame.height or 1
    out: list[tuple[str, int]] = []
    for d in model.dimensions:
        dl = d.lower()
        if dl != "month" and any(t in dl for t in _DATE_ISH):
            continue
        distinct = model.frame.get_column(d).n_unique()
        if distinct <= 1 or distinct > max(60, n * 0.5):
            continue
        out.append((d, distinct))
    out.sort(key=lambda t: (t[0].lower() != "month", t[1]))  # month first, then low-card
    return out


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
    widgets: list[dict[str, Any]] = []
    exports: dict[str, pl.DataFrame] = {}

    for kpi_id in list_gold_kpis(layout):
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
        # headline card + a chart of the KPI by its lead cut
        widgets.append({"id": f"k_{tname}", "type": "kpi", "measure": mslug,
                        "width": 3, "height": 132})
        lead_cut = km.cuts[0] if km.cuts else None
        if lead_cut:
            distinct = gold.get_column(lead_cut).n_unique()
            w = _widget_for(lead_cut, distinct, mslug, 0)
            w["id"] = f"c_{tname}"
            w["title"] = f"{km.card_label} by {_human(lead_cut)}"
            w["dimension"] = f"{tname}.{lead_cut}"
            widgets.append(w)

    if not widgets:
        return tables, measures, None, exports
    page = {
        "id": "kpis", "title": "KPIs", "order": 5,
        "description": "The workspace's defined KPIs, served from the validated gold layer.",
        "filters": [],
        "widgets": widgets,
    }
    return tables, measures, page, exports


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

    dims = _display_dimensions(model)
    primary = next((m["name"] for m in measures if m.get("agg") == "sum"),
                   measures[0]["name"] if measures else "record_count")
    # KPI row: every measure as a tile (4-wide layout). Add a period-over-period
    # trend (UP/DOWN vs prior quarter) when the fact carries a service date --
    # the healthcare-KPI "value + direction" presentation.
    svc_date = next((c for c in model.frame.columns
                     if c.lower() in ("servicedate", "service_date")), None)
    def _kpi_tile(m):
        w = {"id": f"k_{m['name']}", "type": "kpi", "measure": m["name"],
             "width": 3, "height": 132}
        if svc_date:
            w["compare"] = f"{_TABLE}.{svc_date}"
            w["compare_period"] = "quarter"
        return w
    kpi_widgets = [_kpi_tile(m) for m in measures[:4]]
    chart_widgets = [_widget_for(d, dist, primary, i) for i, (d, dist) in enumerate(dims[:5])]
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

    # Detail page: a conditional-format table by the first categorical dimension.
    cat = next((d for d, _ in dims if d.lower() != "month"), None)
    if cat:
        table_measures = [m["name"] for m in measures][:4]
        conditional = []
        for mname in table_measures:
            mspec = next((m for m in measures if m["name"] == mname), {})
            conditional.append({"column": mname,
                                "type": "color_scale" if mspec.get("fmt") == "percent" else "data_bar"})
        pages.append({
            "id": "detail", "title": "Detail", "order": 20,
            "description": "Row-level breakdown with conditional formatting. Export to CSV.",
            "filters": filters,
            "widgets": [{"id": "tbl", "type": "table", "title": f"By {_human(cat)}",
                         "dimension": f"{_TABLE}.{cat}", "measures": table_measures,
                         "sort": "desc", "limit": 50, "width": 12, "height": 520,
                         "export": True, "conditional": conditional}],
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
