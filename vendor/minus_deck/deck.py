"""Build an interactive .pptx from a generated MinusAnalyst project.

Two passes: (1) create every slide, registering each under a key and recording
on-click navigation targets by key; (2) resolve those keys to slide objects and
set ``shape.click_action.target_slide`` -- this is what makes the deck navigable
(KPI cards & thumbnails jump to their detail slide; Contents/Home/Prev/Next on
every slide). Charts are the live app's exact figures, rendered to PNG once each
and placed both as a thumbnail and full-size.
"""

from __future__ import annotations

import io
from datetime import date
from pathlib import Path
from typing import Any, Optional

from pptx import Presentation
from pptx.chart.data import CategoryChartData
from pptx.dml.color import RGBColor
from pptx.enum.chart import XL_CHART_TYPE, XL_LABEL_POSITION, XL_LEGEND_POSITION
from pptx.enum.shapes import MSO_SHAPE
from pptx.enum.text import MSO_ANCHOR, PP_ALIGN
from pptx.oxml import parse_xml
from pptx.oxml.ns import qn
from pptx.util import Emu, Inches, Pt

from minus.export.model import DashboardExport, KpiView, Palette, PageItem, TableView

SERIF = "Georgia"          # Fraunces stand-in (no font embedding needed)
SANS = "Segoe UI"
MONO = "Consolas"

# 16:9 canvas
SLIDE_W = Inches(13.333)
SLIDE_H = Inches(7.5)
MARGIN = Inches(0.55)


# ---------------------------------------------------------------------------
# colour helpers
# ---------------------------------------------------------------------------


def _rgb(hex_str: str) -> RGBColor:
    return RGBColor.from_string(hex_str.lstrip("#").upper()[:6])


def _hexrgb(h: str) -> tuple[int, int, int]:
    h = h.lstrip("#")
    return int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16)


def _mix(a: str, b: str, t: float) -> str:
    """Blend hex ``a`` toward ``b`` by ``t`` in [0,1]."""
    ar, ag, ab = _hexrgb(a)
    br, bg, bb = _hexrgb(b)
    t = max(0.0, min(1.0, t))
    return "%02X%02X%02X" % (round(ar + (br - ar) * t),
                             round(ag + (bg - ag) * t),
                             round(ab + (bb - ab) * t))


# ---------------------------------------------------------------------------
# builder
# ---------------------------------------------------------------------------


# Widget types that have no native PowerPoint chart equivalent -> baked image.
_IMAGE_ONLY = {"heatmap", "small_multiples", "combo", "scatter"}

# interns/minus widget type -> python-pptx native chart type (breakdown decides
# whether multi-series clusters or stacks, matching the live app's barmode).
_XL = {
    "bar": XL_CHART_TYPE.COLUMN_CLUSTERED,
    "hbar": XL_CHART_TYPE.BAR_CLUSTERED,
    "stacked_bar": XL_CHART_TYPE.COLUMN_STACKED,
    "line": XL_CHART_TYPE.LINE_MARKERS,
    "area": XL_CHART_TYPE.AREA,
    "pie": XL_CHART_TYPE.PIE,
    "donut": XL_CHART_TYPE.DOUGHNUT,
}


def _numfmt(fmt: Optional[str]) -> str:
    """interns measure fmt -> Excel number format. NOTE: percent values are stored
    already-in-percent (7.9 = 7.9%), so use a literal '%' suffix, not '0.0%'
    (which would x100)."""
    return {"currency": '$#,##0', "percent": '0.0"%"',
            "int": "#,##0", "float": "#,##0.00"}.get(fmt or "", "General")


class DeckBuilder:
    def __init__(self, dx: DashboardExport, charts: str = "native"):
        self.dx = dx
        self.pal: Palette = dx.palette
        self.charts_mode = charts            # "native" (Excel-backed) | "image"
        self.prs = Presentation()
        self.prs.slide_width = SLIDE_W
        self.prs.slide_height = SLIDE_H
        self._blank = self.prs.slide_layouts[6]
        self.slides: dict[str, Any] = {}            # key -> slide
        self._links: list[tuple[Any, str]] = []     # (shape, target_key)
        self.n_native = 0
        self.n_image = 0

    # -- measure helpers --------------------------------------------------
    def _mlabel(self, name: str) -> str:
        try:
            m = self.dx.project.measure(name)
            return m.label or m.name
        except Exception:
            return name

    def _mfmt(self, name: str) -> Optional[str]:
        try:
            return self.dx.project.measure(name).fmt
        except Exception:
            return None

    # -- low-level slide + shape helpers ---------------------------------
    def _slide(self, key: str, bg: str):
        s = self.prs.slides.add_slide(self._blank)
        fill = s.background.fill
        fill.solid()
        fill.fore_color.rgb = _rgb(bg)
        self.slides[key] = s
        return s

    def _rule(self, slide, left, top, width, color, height=Pt(3)):
        sp = slide.shapes.add_shape(MSO_SHAPE.RECTANGLE, left, top, width, height)
        sp.fill.solid()
        sp.fill.fore_color.rgb = _rgb(color)
        sp.line.fill.background()
        sp.shadow.inherit = False
        return sp

    def _text(self, slide, left, top, width, height, runs, *,
              align=PP_ALIGN.LEFT, anchor=MSO_ANCHOR.TOP, wrap=True, spacing=1.0):
        """runs: list of paragraphs; each paragraph is a list of (text, dict)."""
        tb = slide.shapes.add_textbox(left, top, width, height)
        tf = tb.text_frame
        tf.word_wrap = wrap
        tf.vertical_anchor = anchor
        tf.margin_left = tf.margin_right = Pt(2)
        tf.margin_top = tf.margin_bottom = Pt(1)
        for i, para in enumerate(runs):
            p = tf.paragraphs[0] if i == 0 else tf.add_paragraph()
            p.alignment = align
            p.line_spacing = spacing
            for text, st in para:
                r = p.add_run()
                r.text = text
                r.font.name = st.get("font", SANS)
                r.font.size = Pt(st.get("size", 12))
                r.font.bold = st.get("bold", False)
                r.font.italic = st.get("italic", False)
                r.font.color.rgb = _rgb(st.get("color", self.pal.ink))
        return tb

    def _btn(self, slide, text, left, top, width, height, target_key, *, accent=False):
        sp = slide.shapes.add_shape(MSO_SHAPE.ROUNDED_RECTANGLE, left, top, width, height)
        sp.fill.solid()
        sp.fill.fore_color.rgb = _rgb(self.pal.accent if accent else self.pal.surface_2)
        sp.line.color.rgb = _rgb(self.pal.accent if accent else self.pal.border_strong)
        sp.line.width = Pt(0.75)
        sp.shadow.inherit = False
        tf = sp.text_frame
        tf.word_wrap = False
        tf.margin_left = tf.margin_right = Pt(5)
        tf.margin_top = tf.margin_bottom = Pt(1)
        p = tf.paragraphs[0]
        p.alignment = PP_ALIGN.CENTER
        r = p.add_run()
        r.text = text
        r.font.name = SANS
        r.font.size = Pt(9.5)
        r.font.bold = True
        r.font.color.rgb = _rgb("FFFFFF" if accent else self.pal.muted)
        if target_key is not None:
            self._links.append((sp, target_key))
        return sp

    def _card(self, slide, left, top, width, height, *, fill=None, border=None):
        sp = slide.shapes.add_shape(MSO_SHAPE.ROUNDED_RECTANGLE, left, top, width, height)
        sp.fill.solid()
        sp.fill.fore_color.rgb = _rgb(fill or self.pal.surface)
        sp.line.color.rgb = _rgb(border or self.pal.border)
        sp.line.width = Pt(1)
        sp.shadow.inherit = False
        return sp

    def _navbar(self, slide, *, contents=True, home=True, section_key=None,
                prev_key=None, next_key=None):
        """Top-right nav cluster present on every non-cover slide."""
        w, h, gap = Inches(1.02), Inches(0.30), Inches(0.08)
        x = SLIDE_W - MARGIN - w
        y = Inches(0.22)
        items = []
        if next_key:
            items.append(("Next ›", next_key, False))
        if prev_key:
            items.append(("‹ Prev", prev_key, False))
        if section_key:
            items.append(("▲ Section", section_key, False))
        if home:
            items.append(("Home", "cover", False))
        if contents:
            items.append(("Contents", "contents", True))
        for text, key, accent in items:
            self._btn(slide, text, x, y, w, h, key, accent=accent)
            x -= (w + gap)

    # -- chart png cache (render each figure once) ----------------------
    def _png(self, item: PageItem) -> Optional[bytes]:
        if item.figure is None:
            return None
        if not hasattr(item, "_png_cache"):
            try:
                item._png_cache = self.dx.figure_png(  # type: ignore[attr-defined]
                    item.figure, width=1480, height=980, scale=2, bg=self.pal.surface)
            except Exception:
                item._png_cache = None  # type: ignore[attr-defined]
        return item._png_cache  # type: ignore[attr-defined]

    def _picture(self, slide, png: bytes, left, top, width, height, target_key=None):
        pic = slide.shapes.add_picture(io.BytesIO(png), left, top, width, height)
        pic.line.color.rgb = _rgb(self.pal.border)
        pic.line.width = Pt(0.75)
        if target_key is not None:
            self._links.append((pic, target_key))
        return pic

    # -- native (Excel-backed) chart ------------------------------------
    def _native_spec(self, it: PageItem):
        """(xl_type, categories, [(series_name, values, fmt)], value_fmt) for a
        native PowerPoint chart, or None when the widget must stay an image."""
        w, r = it.widget, it.result
        t = w.type
        if t in _IMAGE_ONLY or r is None or r.frame.height == 0:
            return None
        df = r.frame
        dims, measures = r.dimensions, r.measures
        if not dims or not measures:
            return None
        dim = dims[0]
        breakdown = dims[1] if len(dims) > 1 else None
        if t in ("line", "area"):                       # trends read in x order
            try:
                df = df.sort(dim)
            except Exception:
                pass

        if breakdown:
            primary = measures[0]
            fmt = self._mfmt(primary)
            cats = list(dict.fromkeys(df.get_column(dim).to_list()))
            order = list(dict.fromkeys(df.get_column(breakdown).to_list()))
            lut = {(row[dim], row[breakdown]): row[primary]
                   for row in df.iter_rows(named=True)}
            series = [(str(b), [lut.get((c, b)) for c in cats], fmt) for b in order]
            cats = [str(c) for c in cats]
        elif len(measures) > 1 and t in ("bar", "hbar", "line", "area"):
            cats = [str(v) for v in df.get_column(dim).to_list()]
            series = [(self._mlabel(m), df.get_column(m).to_list(), self._mfmt(m))
                      for m in measures]
        else:
            primary = measures[0]
            cats = [str(v) for v in df.get_column(dim).to_list()]
            series = [(self._mlabel(primary), df.get_column(primary).to_list(),
                       self._mfmt(primary))]

        xl = _XL.get(t, XL_CHART_TYPE.COLUMN_CLUSTERED)
        if breakdown and t == "hbar":
            xl = XL_CHART_TYPE.BAR_STACKED
        elif breakdown and t == "area":
            xl = XL_CHART_TYPE.AREA_STACKED
        value_fmt = series[0][2] if series else None
        return xl, cats, series, value_fmt

    def _add_native_chart(self, slide, it: PageItem, left, top, width, height) -> bool:
        spec = self._native_spec(it)
        if spec is None:
            return False
        xl, cats, series, value_fmt = spec
        try:
            cd = CategoryChartData()
            cd.categories = cats
            for name, vals, fmt in series:
                cd.add_series(name, [None if v is None else float(v) for v in vals],
                              number_format=_numfmt(fmt))
            gf = slide.shapes.add_chart(xl, left, top, width, height, cd)
            self._style_native(gf.chart, it, len(series), value_fmt, xl)
            self.n_native += 1
            return True
        except Exception:
            return False

    def _fill_chart_area(self, chart, hexcol: str):
        """Paint the chart area the warm surface colour (low-level: chartSpace has
        no high-level fill API). Best-effort -- default white on failure."""
        try:
            cs = chart._chartSpace
            for ex in cs.findall(qn("c:spPr")):
                cs.remove(ex)
            xml = (
                '<c:spPr xmlns:c="http://schemas.openxmlformats.org/drawingml/2006/chart" '
                'xmlns:a="http://schemas.openxmlformats.org/drawingml/2006/main">'
                f'<a:solidFill><a:srgbClr val="{hexcol.lstrip("#")}"/></a:solidFill>'
                f'<a:ln><a:solidFill><a:srgbClr val="{self.pal.border.lstrip("#")}"/>'
                '</a:solidFill></a:ln></c:spPr>')
            chart_el = cs.find(qn("c:chart"))
            chart_el.addnext(parse_xml(xml))
        except Exception:
            pass

    def _style_native(self, chart, it: PageItem, n_series: int, value_fmt, xl=None):
        pal = self.pal
        t = it.widget.type
        is_stacked = xl in (XL_CHART_TYPE.BAR_STACKED, XL_CHART_TYPE.COLUMN_STACKED,
                            XL_CHART_TYPE.AREA_STACKED)
        is_barlike = xl in (XL_CHART_TYPE.BAR_STACKED, XL_CHART_TYPE.COLUMN_STACKED,
                            XL_CHART_TYPE.BAR_CLUSTERED, XL_CHART_TYPE.COLUMN_CLUSTERED)
        chart.has_title = False
        try:
            chart.font.size = Pt(9)
            chart.font.name = SANS
            chart.font.color.rgb = _rgb(pal.muted)
        except Exception:
            pass
        self._fill_chart_area(chart, pal.surface)
        # legend below for multi-series / pies
        if n_series > 1 or t in ("pie", "donut"):
            chart.has_legend = True
            try:
                chart.legend.position = XL_LEGEND_POSITION.BOTTOM
                chart.legend.include_in_layout = False
                chart.legend.font.size = Pt(8)
            except Exception:
                pass
        else:
            chart.has_legend = False
        for plot in chart.plots:
            try:
                plot.gap_width = 60
                # Stacked bars MUST overlap 100% (else they render side-by-side).
                if is_stacked and is_barlike:
                    plot.overlap = 100
            except Exception:
                pass
            # colours: per-point for pies, per-series otherwise
            if t in ("pie", "donut"):
                try:
                    for j, pt in enumerate(plot.series[0].points):
                        pt.format.fill.solid()
                        pt.format.fill.fore_color.rgb = _rgb(pal.series[j % len(pal.series)])
                except Exception:
                    pass
            else:
                for si, s in enumerate(plot.series):
                    col = _rgb(pal.series[si % len(pal.series)])
                    try:
                        if t == "line":
                            s.format.line.color.rgb = col
                            s.format.line.width = Pt(2.25)
                        else:
                            s.format.fill.solid()
                            s.format.fill.fore_color.rgb = col
                            s.format.line.fill.background()
                    except Exception:
                        pass
            # data labels -> every bar/slice carries its value (matches the app)
            try:
                plot.has_data_labels = t in ("bar", "hbar", "stacked_bar", "pie", "donut")
                if plot.has_data_labels:
                    dl = plot.data_labels
                    dl.font.size = Pt(8)
                    dl.font.color.rgb = _rgb(pal.muted)
                    if t in ("pie", "donut"):
                        dl.show_percentage = True
                        dl.show_value = False
                        dl.number_format = "0.0%"
                        dl.number_format_is_linked = False
                    else:
                        dl.number_format = _numfmt(value_fmt)
                        dl.number_format_is_linked = False
                        # OUTSIDE_END is INVALID on a stacked bar (PowerPoint rejects
                        # the file as corrupt); stacked segments must use CENTER.
                        dl.position = (XL_LABEL_POSITION.CENTER if is_stacked
                                       else XL_LABEL_POSITION.OUTSIDE_END)
            except Exception:
                pass
        # value-axis number format + grid
        try:
            va = chart.value_axis
            va.tick_labels.number_format = _numfmt(value_fmt)
            va.tick_labels.number_format_is_linked = False
            va.tick_labels.font.size = Pt(8)
        except Exception:
            pass
        try:
            chart.category_axis.tick_labels.font.size = Pt(8)
        except Exception:
            pass

    # -- KPI card --------------------------------------------------------
    def _kpi_card(self, slide, kv: KpiView, left, top, width, height, target_key=None):
        card = self._card(slide, left, top, width, height)
        if target_key is not None:
            self._links.append((card, target_key))
        pad = Inches(0.16)
        iw = width - 2 * pad
        # label
        self._text(slide, left + pad, top + Inches(0.12), iw, Inches(0.3),
                   [[(kv.label.upper(), {"font": SANS, "size": 9, "bold": True,
                                         "color": self.pal.muted})]], wrap=False)
        # value
        self._text(slide, left + pad, top + Inches(0.34), iw, Inches(0.7),
                   [[(kv.value, {"font": SERIF, "size": 31, "bold": True,
                                 "color": self.pal.accent})]], wrap=False)
        y = top + Inches(1.06)
        if kv.subtitle:
            self._text(slide, left + pad, y, iw, Inches(0.42),
                       [[(kv.subtitle, {"font": SANS, "size": 8.5, "color": self.pal.faint})]],
                       spacing=0.95)
            y += Inches(0.40)
        bits = []
        if kv.target:
            col = self.pal.good if kv.on_target else self.pal.bad
            bits.append((f"● {kv.target}", {"font": SANS, "size": 8.5, "bold": True, "color": col}))
        if kv.delta is not None:
            up = kv.delta >= 0
            bits.append(((("  " if bits else "") + f"{'▲' if up else '▼'} {abs(kv.delta):.1f}% "
                          f"{kv.delta_label or ''}"),
                         {"font": SANS, "size": 8.5, "bold": True,
                          "color": self.pal.good if up else self.pal.bad}))
        if bits:
            self._text(slide, left + pad, top + height - Inches(0.34), iw, Inches(0.28),
                       [bits], wrap=False)

    # -- native data table ----------------------------------------------
    def _data_table(self, slide, tv: TableView, left, top, width, height, *, max_rows=12):
        rows = tv.rows[:max_rows]
        ncols = len(tv.headers)
        nrows = len(rows) + 1
        gtab = slide.shapes.add_table(nrows, ncols, left, top, width, height).table
        gtab.first_row = False
        gtab.horz_banding = False
        # header
        for c, (_col, label, _num) in enumerate(tv.headers):
            cell = gtab.cell(0, c)
            cell.fill.solid()
            cell.fill.fore_color.rgb = _rgb(self.pal.ink)
            cell.margin_left = cell.margin_right = Pt(5)
            cell.margin_top = cell.margin_bottom = Pt(2)
            cell.vertical_anchor = MSO_ANCHOR.MIDDLE
            p = cell.text_frame.paragraphs[0]
            p.alignment = PP_ALIGN.RIGHT if tv.headers[c][2] else PP_ALIGN.LEFT
            r = p.add_run()
            r.text = label
            r.font.name = SANS
            r.font.size = Pt(8.5)
            r.font.bold = True
            r.font.color.rgb = _rgb(self.pal.surface)
        # body
        for ri, cells in enumerate(rows, start=1):
            base = self.pal.surface if ri % 2 else self.pal.surface_2
            for ci, cell_data in enumerate(cells):
                cell = gtab.cell(ri, ci)
                cell.fill.solid()
                bg = base
                if cell_data.rule in ("data_bar", "color_scale") and cell_data.frac is not None:
                    alpha = (0.10 + 0.42 * cell_data.frac) if cell_data.rule == "color_scale" \
                        else (0.30 * cell_data.frac)
                    bg = _mix(base, cell_data.color or self.pal.accent, alpha)
                cell.fill.fore_color.rgb = _rgb(bg)
                cell.margin_left = cell.margin_right = Pt(5)
                cell.margin_top = cell.margin_bottom = Pt(1)
                cell.vertical_anchor = MSO_ANCHOR.MIDDLE
                p = cell.text_frame.paragraphs[0]
                p.alignment = PP_ALIGN.RIGHT if cell_data.num else PP_ALIGN.LEFT
                txt = cell_data.text
                if cell_data.rule == "icon" and cell_data.up is not None:
                    txt = ("▲ " if cell_data.up else "▼ ") + txt
                r = p.add_run()
                r.text = txt
                r.font.name = MONO if cell_data.num else SANS
                r.font.size = Pt(8.5)
                r.font.color.rgb = _rgb(self.pal.ink)
        # tighten row heights
        gtab.rows[0].height = Inches(0.28)
        for rr in gtab.rows:
            rr.height = Inches(0.26)
        return gtab

    # ===================================================================
    # slides
    # ===================================================================
    def build(self) -> Presentation:
        meta = self.dx.meta()
        page_items = [(p, self.dx.items_for(p)) for p in self.dx.pages]

        # detail order (for prev/next): flatten charts+tables across pages
        detail_keys: list[str] = []
        for p, items in page_items:
            for it in items:
                if it.kind in ("chart", "table"):
                    detail_keys.append(f"detail:{p.id}:{it.widget.id}")

        self._cover(meta)
        self._contents(meta, page_items)
        for p, items in page_items:
            self._section(p, items)
        # detail slides
        for p, items in page_items:
            first_detail = next((f"detail:{p.id}:{it.widget.id}"
                                 for it in items if it.kind in ("chart", "table")), None)
            for it in items:
                if it.kind == "chart":
                    self._chart_detail(p, it, detail_keys)
                elif it.kind == "table":
                    self._table_detail(p, it, detail_keys)

        self._wire()
        return self.prs

    def _cover(self, meta: dict):
        s = self._slide("cover", self.pal.canvas)
        self._rule(s, MARGIN, Inches(1.5), Inches(2.2), self.pal.accent)
        self._text(s, MARGIN, Inches(1.7), SLIDE_W - 2 * MARGIN, Inches(0.4),
                   [[("WORKSPACE INTELLIGENCE  ·  MINUSDECK", {"font": MONO, "size": 12,
                                                                   "bold": True, "color": self.pal.accent})]])
        self._text(s, MARGIN, Inches(2.15), SLIDE_W - 2 * MARGIN, Inches(2.0),
                   [[(meta["name"].replace("-", " "), {"font": SERIF, "size": 50, "bold": True,
                                                       "color": self.pal.ink})]], spacing=0.95)
        self._text(s, MARGIN, Inches(3.7), SLIDE_W - 2 * MARGIN, Inches(0.5),
                   [[("Interactive board export of the live MinusAnalyst dashboard.",
                      {"font": SANS, "size": 16, "color": self.pal.muted})]])
        stamp = [date.today().isoformat(), f"{meta['pages']} pages",
                 f"{meta['measures']} measures"]
        if meta.get("rows"):
            stamp.append(f"{meta['rows']:,} rows")
        if meta.get("data_through"):
            stamp.append(f"data through {meta['data_through']}")
        self._text(s, MARGIN, Inches(4.4), SLIDE_W - 2 * MARGIN, Inches(0.4),
                   [[("   ·   ".join(stamp), {"font": MONO, "size": 11, "color": self.pal.faint})]])
        self._btn(s, "Open contents  →", MARGIN, Inches(5.3), Inches(2.2), Inches(0.5),
                  "contents", accent=True)

    def _contents(self, meta: dict, page_items):
        s = self._slide("contents", self.pal.canvas)
        self._text(s, MARGIN, Inches(0.5), Inches(8), Inches(0.7),
                   [[("Contents", {"font": SERIF, "size": 30, "bold": True, "color": self.pal.ink})]])
        self._rule(s, MARGIN, Inches(1.2), Inches(1.4), self.pal.accent)
        self._navbar(s, contents=False, section_key=None)

        col_w = Inches(5.9)
        xs = [MARGIN, MARGIN + col_w + Inches(0.4)]
        y = Inches(1.6)
        col = 0
        line_h = Inches(0.34)
        for p, items in page_items:
            charts = [it for it in items if it.kind in ("chart", "table")]
            need = line_h + line_h * min(len(charts), 8) + Inches(0.2)
            if y + need > SLIDE_H - Inches(0.5) and col == 0:
                col, y = 1, Inches(1.6)
            x = xs[col]
            # page entry
            self._btn(s, f"▸  {p.title}", x, y, Inches(3.0), Inches(0.32),
                      f"section:{p.id}", accent=False)
            y += line_h + Inches(0.04)
            for it in charts[:8]:
                tb = self._text(s, x + Inches(0.35), y, col_w - Inches(0.4), Inches(0.3),
                                [[(f"–  {it.title}",
                                   {"font": SANS, "size": 11, "color": self.pal.muted})]], wrap=False)
                self._links.append((tb, f"detail:{p.id}:{it.widget.id}"))
                y += line_h
            y += Inches(0.18)

    def _section(self, page, items: list[PageItem]):
        s = self._slide(f"section:{page.id}", self.pal.canvas)
        first_detail = next((f"detail:{page.id}:{it.widget.id}"
                             for it in items if it.kind in ("chart", "table")), None)
        self._navbar(s, section_key=None, next_key=first_detail)
        # header
        self._text(s, MARGIN, Inches(0.42), Inches(9), Inches(0.6),
                   [[(page.title, {"font": SERIF, "size": 28, "bold": True, "color": self.pal.ink})]])
        if page.description:
            self._text(s, MARGIN, Inches(1.04), Inches(11.2), Inches(0.6),
                       [[(page.description, {"font": SANS, "size": 11, "color": self.pal.muted})]],
                       spacing=0.95)
        self._rule(s, MARGIN, Inches(1.66), Inches(1.4), self.pal.accent)

        kpis = [it for it in items if it.kind == "kpi"]
        charts = [it for it in items if it.kind in ("chart", "table")]

        # KPI scorecard row (cards jump to the first detail slide)
        y = Inches(1.92)
        if kpis:
            n = min(len(kpis), 4)
            gap = Inches(0.22)
            cw = (SLIDE_W - 2 * MARGIN - gap * (n - 1)) / n
            for i, it in enumerate(kpis[:n]):
                self._kpi_card(s, it.kpi, MARGIN + i * (cw + gap), y, cw, Inches(1.7),
                               target_key=first_detail)
            y += Inches(1.95)

        # chart/table thumbnails (jump to their own detail slide)
        if charts:
            per_row = 3
            gap = Inches(0.22)
            tw = (SLIDE_W - 2 * MARGIN - gap * (per_row - 1)) / per_row
            avail_h = SLIDE_H - y - Inches(0.4)
            rows = (min(len(charts), 6) + per_row - 1) // per_row
            th = min(Inches(2.5), (avail_h - gap * (rows - 1)) / max(rows, 1))
            for idx, it in enumerate(charts[:6]):
                rr, cc = divmod(idx, per_row)
                x = MARGIN + cc * (tw + gap)
                ty = y + rr * (th + gap)
                key = f"detail:{page.id}:{it.widget.id}"
                self._card(s, x, ty, tw, th)
                self._text(s, x + Inches(0.12), ty + Inches(0.06), tw - Inches(0.24), Inches(0.3),
                           [[(it.title, {"font": SANS, "size": 9.5, "bold": True,
                                         "color": self.pal.ink})]], wrap=False)
                png = self._png(it) if it.kind == "chart" else None
                if png:
                    self._picture(s, png, x + Inches(0.12), ty + Inches(0.40),
                                  tw - Inches(0.24), th - Inches(0.52), target_key=key)
                else:
                    # table thumbnail: a labelled card that still jumps to the detail
                    lbl = (f"{it.table.total_rows} rows × {len(it.table.headers)} cols"
                           if it.table else "table")
                    box = self._card(s, x + Inches(0.12), ty + Inches(0.40),
                                     tw - Inches(0.24), th - Inches(0.52), fill=self.pal.surface_2)
                    self._links.append((box, key))
                    self._text(s, x + Inches(0.12), ty + th / 2 - Inches(0.15),
                               tw - Inches(0.24), Inches(0.3),
                               [[(f"▦  {lbl}", {"font": MONO, "size": 10,
                                                    "color": self.pal.muted})]],
                               align=PP_ALIGN.CENTER)

    def _detail_header(self, s, page, it, detail_keys):
        key = f"detail:{page.id}:{it.widget.id}"
        i = detail_keys.index(key) if key in detail_keys else -1
        prev_key = detail_keys[i - 1] if i > 0 else None
        next_key = detail_keys[i + 1] if 0 <= i < len(detail_keys) - 1 else None
        self._navbar(s, section_key=f"section:{page.id}", prev_key=prev_key, next_key=next_key)
        self._text(s, MARGIN, Inches(0.34), Inches(9.5), Inches(0.6),
                   [[(it.title, {"font": SERIF, "size": 22, "bold": True, "color": self.pal.ink})]])
        self._text(s, MARGIN, Inches(0.92), Inches(9.5), Inches(0.3),
                   [[(f"{page.title}  ·  {it.widget.type}",
                      {"font": MONO, "size": 9.5, "color": self.pal.faint})]], wrap=False)
        self._rule(s, MARGIN, Inches(1.22), Inches(1.2), self.pal.accent)

    def _chart_detail(self, page, it: PageItem, detail_keys):
        s = self._slide(f"detail:{page.id}:{it.widget.id}", self.pal.canvas)
        self._detail_header(s, page, it, detail_keys)
        has_tbl = it.result is not None and it.result.frame.height and len(it.result.frame.columns) <= 6
        chart_w = Inches(8.4) if has_tbl else (SLIDE_W - 2 * MARGIN)
        # Native (Excel-backed, editable) chart when possible; baked image otherwise.
        placed = False
        if self.charts_mode == "native":
            placed = self._add_native_chart(s, it, MARGIN, Inches(1.5), chart_w, Inches(5.4))
        if not placed:
            png = self._png(it)
            if png:
                self.n_image += 1
                self._picture(s, png, MARGIN, Inches(1.5), chart_w, Inches(5.4))
        # the data behind the chart (richer info) -- compact table on the right
        if has_tbl:
            tv = self.dx.table_view(it.widget, it.result, max_rows=14)
            tx = MARGIN + chart_w + Inches(0.3)
            self._text(s, tx, Inches(1.5), SLIDE_W - tx - MARGIN, Inches(0.3),
                       [[("DATA", {"font": MONO, "size": 9, "bold": True, "color": self.pal.accent})]])
            self._data_table(s, tv, tx, Inches(1.84), SLIDE_W - tx - MARGIN,
                             Inches(4.8), max_rows=14)

    def _table_detail(self, page, it: PageItem, detail_keys):
        s = self._slide(f"detail:{page.id}:{it.widget.id}", self.pal.canvas)
        self._detail_header(s, page, it, detail_keys)
        tv = it.table
        note = f"{tv.shown_rows} of {tv.total_rows} rows"
        self._text(s, MARGIN, Inches(1.42), Inches(9), Inches(0.3),
                   [[(note, {"font": MONO, "size": 9, "color": self.pal.faint})]], wrap=False)
        self._data_table(s, tv, MARGIN, Inches(1.8), SLIDE_W - 2 * MARGIN,
                         Inches(5.2), max_rows=16)

    # -- resolve deferred slide links -----------------------------------
    def _wire(self):
        for shape, key in self._links:
            target = self.slides.get(key)
            if target is not None:
                try:
                    shape.click_action.target_slide = target
                except Exception:
                    pass


# ---------------------------------------------------------------------------
# public entry
# ---------------------------------------------------------------------------


def build_deck(root: Path | str, out_path: Path | str, *,
               theme: Optional[str] = None, charts: str = "native") -> dict[str, Any]:
    """Build an interactive .pptx for the generated project at ``root``.

    ``charts``: ``"native"`` (default) renders editable, Excel-backed PowerPoint
    charts on detail slides (with an automatic image fallback for chart types
    PowerPoint can't draw); ``"image"`` bakes every chart as a pixel-perfect
    image of the live dashboard. Returns a summary dict.
    """
    dx = DashboardExport(root, theme=theme)
    builder = DeckBuilder(dx, charts=charts)
    prs = builder.build()
    out_path = Path(out_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    prs.save(str(out_path))
    n_charts = sum(1 for s in builder.slides if s.startswith("detail:"))
    return {
        "ok": True,
        "path": str(out_path),
        "slides": len(prs.slides._sldIdLst),
        "pages": len(dx.pages),
        "detail_slides": n_charts,
        "charts_mode": charts,
        "native_charts": builder.n_native,
        "image_charts": builder.n_image,
        "theme": dx.theme_name,
    }


__all__ = ["build_deck", "DeckBuilder"]
