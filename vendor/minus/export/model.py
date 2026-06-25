"""Shared export model: read a generated MinusAnalyst project and expose its
content for static exporters (the MinusDeck PPTX + MinusPress PDF vendors).

Why this exists
---------------
Both exporters must render the *same* dashboard the live Dash app serves: same
default view, same data, same themed charts. Rather than duplicate the engine,
this module loads the project exactly like ``minus.render.app.AppState`` and
replays the app's INITIAL (unfiltered) render path per widget:

    wf  = engine.applicable_filters(widget, widget.base_filters)   # default scope
    res = engine.run(widget, wf)

That mirrors ``layout._render_tile`` with no slicer/cross-filter/drill, so a
deck/PDF shows what a viewer sees on first load. Charts reuse the exact figure
builder the app uses (``build_chart_figure``) -> zero look drift.

Everything here is workspace-agnostic; nothing is hardcoded to a domain.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

import polars as pl
import plotly.graph_objects as go

from minus.config.loader import load_dashboards, load_project
from minus.config.models import Dashboard, Project, Widget
from minus.data.model import SemanticModel
from minus.query.engine import QueryEngine, QueryResult
from minus.query.measures import format_value
from minus.render.theme import chart_colors

import decimal


def _json_safe(obj: Any) -> Any:
    """Recursively coerce values kaleido cannot JSON-serialize (notably
    ``decimal.Decimal`` from DuckDB DECIMAL columns) into plain floats. Walks
    dicts, lists/tuples, and numpy arrays. Generic; no domain assumptions."""
    if isinstance(obj, decimal.Decimal):
        return float(obj)
    if isinstance(obj, dict):
        return {k: _json_safe(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [_json_safe(v) for v in obj]
    tolist = getattr(obj, "tolist", None)  # numpy arrays / scalars
    if callable(tolist) and obj.__class__.__module__ == "numpy":
        return _json_safe(tolist())
    return obj
from minus.render.widgets.render import PALETTE as SERIES_PALETTE
from minus.render.widgets.render import build_chart_figure


# ---------------------------------------------------------------------------
# Palette -- the full theme colours (CSS variables) charts can't carry. Mirrors
# vendor/minus/themes/{claude,dark}.css so export chrome matches the live app.
# ---------------------------------------------------------------------------

_PALETTES: dict[str, dict[str, str]] = {
    "claude": {
        "canvas": "#F1ECE2", "surface": "#FCFAF5", "surface_2": "#F4EFE4",
        "ink": "#211D18", "muted": "#6C6358", "faint": "#9C9387",
        "accent": "#C2603C", "accent_ink": "#A24B2C", "accent_bright": "#DA7756",
        "border": "#E4DCCD", "border_strong": "#D5CBB6",
        "good": "#3F8C6E", "bad": "#C0563F",
    },
    "dark": {
        "canvas": "#181613", "surface": "#211D19", "surface_2": "#2A251F",
        "ink": "#F1EADE", "muted": "#ABA193", "faint": "#756C5F",
        "accent": "#DD8763", "accent_ink": "#EFA886", "accent_bright": "#E59370",
        "border": "#332E27", "border_strong": "#443D33",
        "good": "#6FB89A", "bad": "#E0856B",
    },
}


@dataclass(frozen=True)
class Palette:
    name: str
    canvas: str
    surface: str
    surface_2: str
    ink: str
    muted: str
    faint: str
    accent: str
    accent_ink: str
    accent_bright: str
    border: str
    border_strong: str
    good: str
    bad: str
    series: tuple[str, ...] = tuple(SERIES_PALETTE)

    @classmethod
    def for_theme(cls, name: str, accent: Optional[str] = None) -> "Palette":
        raw = dict(_PALETTES.get((name or "claude").strip(), _PALETTES["claude"]))
        if accent:
            raw["accent"] = accent
            raw["accent_ink"] = accent
            raw["accent_bright"] = accent
        return cls(name=(name or "claude").strip(), **raw)


# ---------------------------------------------------------------------------
# Per-widget views
# ---------------------------------------------------------------------------


@dataclass
class KpiView:
    label: str
    value: str
    subtitle: Optional[str] = None
    delta: Optional[float] = None          # signed % change
    delta_label: Optional[str] = None
    target: Optional[str] = None           # formatted "Target X"
    on_target: Optional[bool] = None


@dataclass
class Cell:
    text: str
    num: bool = False
    frac: Optional[float] = None           # 0..1 for data_bar / color_scale
    rule: Optional[str] = None             # data_bar | color_scale | icon
    color: Optional[str] = None
    up: Optional[bool] = None              # for icon rule


@dataclass
class TableView:
    headers: list[tuple[str, str, bool]]   # (col, label, is_measure)
    rows: list[list[Cell]]
    total_rows: int
    shown_rows: int


@dataclass
class PageItem:
    """One renderable tile from a page, in its default (unfiltered) view."""

    kind: str                              # kpi | chart | table
    widget: Widget
    result: QueryResult
    title: str = ""
    kpi: Optional[KpiView] = None
    figure: Optional[go.Figure] = None
    table: Optional[TableView] = None


# ---------------------------------------------------------------------------
# DashboardExport
# ---------------------------------------------------------------------------


def _measure_label(name: str, project: Project) -> str:
    try:
        m = project.measure(name)
        return m.label or m.name
    except KeyError:
        return name


class DashboardExport:
    """Loaded, query-ready view of a generated MinusAnalyst project root."""

    def __init__(self, root: Path | str, *, theme: Optional[str] = None):
        self.root = Path(root).resolve()
        self.project: Project = load_project(self.root)
        if theme:
            self.project.theme.name = theme
        self.pages: list[Dashboard] = load_dashboards(self.root, self.project)
        self.model = SemanticModel(self.project, self.root)
        self.engine = QueryEngine(self.project, self.model)
        self.theme_name = (self.project.theme.name or "claude").strip()
        self.palette = Palette.for_theme(self.theme_name, accent=self.project.theme.accent)
        self._chart_colors = chart_colors(self.project)

    # -- classification ---------------------------------------------------
    @staticmethod
    def widget_kind(w: Widget) -> str:
        if w.type == "kpi":
            return "kpi"
        if w.type == "table":
            return "table"
        return "chart"

    # -- default-view result (matches the app's initial render) -----------
    def result_for(self, w: Widget) -> Optional[QueryResult]:
        try:
            base = dict(getattr(w, "base_filters", {}) or {})
            wf = self.engine.applicable_filters(w, base)
            return self.engine.run(w, wf)
        except Exception:
            return None

    # -- KPI --------------------------------------------------------------
    def kpi_view(self, w: Widget, result: QueryResult) -> KpiView:
        m = self.project.measure(w.measure)
        label = w.title or m.label or m.name
        value = format_value(result.scalar, m.fmt)
        target = getattr(m, "target", None)
        on_target: Optional[bool] = None
        tstr: Optional[str] = None
        if target is not None and result.scalar is not None:
            higher = getattr(m, "goal", "higher") == "higher"
            on_target = (result.scalar >= target) if higher else (result.scalar <= target)
            tstr = f"Target {format_value(target, m.fmt)}"
        return KpiView(
            label=label, value=value, subtitle=getattr(w, "subtitle", None),
            delta=result.delta, delta_label=result.delta_label,
            target=tstr, on_target=on_target,
        )

    # -- chart figure -----------------------------------------------------
    def figure_for(self, w: Widget, result: QueryResult) -> Optional[go.Figure]:
        try:
            if result is None or getattr(result, "frame", None) is None:
                return None
            if result.frame.height == 0:
                return None
            return build_chart_figure(w, result, self.project, colors=self._chart_colors)
        except Exception:
            return None

    def figure_png(self, fig: go.Figure, *, width: int, height: int,
                   scale: float = 2.0, bg: Optional[str] = None) -> bytes:
        """Render a figure to PNG bytes (kaleido). ``bg`` paints the paper/plot so
        the image drops seamlessly onto a card of the same colour (no white halo)."""
        f = go.Figure(fig)
        upd: dict[str, Any] = {"width": width, "height": height,
                               "margin": dict(l=14, r=18, t=22, b=18)}
        if bg is not None:
            upd["paper_bgcolor"] = bg
            upd["plot_bgcolor"] = bg
        f.update_layout(**upd)
        # kaleido serializes the figure to JSON to rasterize it; DuckDB DECIMAL
        # columns surface as `decimal.Decimal`, which is not JSON-serializable and
        # aborts to_image for that chart (the live plotly.js app handles Decimal
        # natively, so this only bites exports). Coerce Decimals to float so any
        # figure rasterizes. Workspace-agnostic.
        f = go.Figure(_json_safe(f.to_dict()))
        return f.to_image(format="png", scale=scale)

    # -- table ------------------------------------------------------------
    def table_view(self, w: Widget, result: QueryResult, *, max_rows: int = 40) -> TableView:
        df = result.frame
        headers: list[tuple[str, str, bool]] = []
        fmts: dict[str, Optional[str]] = {}
        for c in df.columns:
            if c in result.measures:
                headers.append((c, _measure_label(c, self.project), True))
                fmts[c] = self.project.measure(c).fmt
            else:
                headers.append((c, c.split(".")[-1], False))

        rules = {r.column: r for r in w.conditional}
        stats: dict[str, tuple[float, float]] = {}
        for col in rules:
            if col in df.columns:
                s = df.get_column(col).cast(pl.Float64, strict=False)
                mn, mx = s.min(), s.max()
                if mn is not None and mx is not None:
                    stats[col] = (float(mn), float(mx))

        rows: list[list[Cell]] = []
        for row in df.head(max_rows).iter_rows(named=True):
            cells: list[Cell] = []
            for col, _label, num in headers:
                raw = row[col]
                text = (format_value(raw, fmts.get(col)) if num
                        else ("" if raw is None else str(raw)))
                cell = Cell(text=text, num=num)
                rule = rules.get(col)
                if rule and col in stats:
                    lo, hi = stats[col]
                    v = float(raw) if raw is not None else float("nan")
                    span = (hi - lo) or 1.0
                    cell.frac = max(0.0, min(1.0, (v - lo) / span)) if v == v else 0.0
                    cell.rule = rule.type
                    cell.color = rule.color or self.palette.accent
                    if rule.type == "icon" and v == v:
                        cell.up = v >= 0
                cells.append(cell)
            rows.append(cells)
        return TableView(headers=headers, rows=rows,
                         total_rows=df.height, shown_rows=len(rows))

    # -- page walk (shared by both exporters) -----------------------------
    def items_for(self, page: Dashboard, *, table_max_rows: int = 40) -> list[PageItem]:
        """Every renderable tile on a page, in the app's default view. Skips
        tiles that error or produce no data (so an export never shows a broken
        cell), exactly like the live grid's honest empty-state."""
        out: list[PageItem] = []
        for w in page.widgets:
            r = self.result_for(w)
            if r is None:
                continue
            kind = self.widget_kind(w)
            if kind == "kpi":
                out.append(PageItem(kind=kind, widget=w, result=r,
                                    title=w.title or "", kpi=self.kpi_view(w, r)))
            elif kind == "table":
                out.append(PageItem(kind=kind, widget=w, result=r,
                                    title=w.title or w.id,
                                    table=self.table_view(w, r, max_rows=table_max_rows)))
            else:
                fig = self.figure_for(w, r)
                if fig is None:
                    continue
                out.append(PageItem(kind=kind, widget=w, result=r,
                                    title=w.title or w.id, figure=fig))
        return out

    # -- project meta -----------------------------------------------------
    def meta(self) -> dict[str, Any]:
        rows: Optional[int] = None
        try:
            fact = self.project.tables[0].name
            rows = int(self.model.table_df(fact).height)
        except Exception:
            rows = None
        return {
            "name": self.project.name,
            "theme": self.theme_name,
            "data_through": self.project.data_through,
            "rows": rows,
            "pages": len(self.pages),
            "measures": len(self.project.measures),
        }


__all__ = ["DashboardExport", "Palette", "KpiView", "TableView", "Cell", "PageItem"]
