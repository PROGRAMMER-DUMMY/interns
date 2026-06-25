"""Build a rich, navigable .pdf from a generated MinusAnalyst project (reportlab).

Navigation is real PDF interactivity:
  * ``afterFlowable`` turns H1/H2 headings into bookmarks (``bookmarkPage``) and
    outline entries (``addOutlineEntry``) -> the reader's outline/bookmarks pane.
  * the Contents page uses ``<a href="#key">`` links that resolve to those
    bookmarks -> click an entry to jump to the section/figure.

Charts are the live app's exact figures rendered to PNG; KPI cards and data
tables are drawn natively in the Claude palette so the document matches the
served dashboard.
"""

from __future__ import annotations

import io
from datetime import date
from pathlib import Path
from typing import Any, Optional
from xml.sax.saxutils import escape as _esc

from reportlab.lib import colors
from reportlab.lib.enums import TA_CENTER, TA_LEFT, TA_RIGHT
from reportlab.lib.pagesizes import landscape, letter
from reportlab.lib.styles import ParagraphStyle
from reportlab.lib.units import inch
from reportlab.platypus import (
    BaseDocTemplate,
    CondPageBreak,
    Frame,
    Image,
    NextPageTemplate,
    PageBreak,
    PageTemplate,
    Paragraph,
    Spacer,
    Table,
    TableStyle,
)
from reportlab.platypus.flowables import HRFlowable

from minus.export.model import DashboardExport, Palette, PageItem, TableView

PAGE = landscape(letter)            # 11in x 8.5in
PW, PH = PAGE
LEFT = 0.6 * inch
RIGHT = 0.6 * inch
CONTENT_W = PW - LEFT - RIGHT

SERIF = "Times-Bold"
SANS = "Helvetica"
SANS_B = "Helvetica-Bold"
MONO = "Courier"


def _hexrgb(h: str) -> tuple[int, int, int]:
    h = h.lstrip("#")
    return int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16)


def _mix(a: str, b: str, t: float) -> str:
    ar, ag, ab = _hexrgb(a)
    br, bg, bb = _hexrgb(b)
    t = max(0.0, min(1.0, t))
    return "#%02X%02X%02X" % (round(ar + (br - ar) * t),
                              round(ag + (bg - ag) * t),
                              round(ab + (bb - ab) * t))


class PressDoc(BaseDocTemplate):
    """Adds outline bookmarks from heading flowables tagged with ``_bookmark``."""

    def __init__(self, filename, palette: Palette, project_name: str, **kw):
        super().__init__(filename, **kw)
        self.pal = palette
        self.project_name = project_name

    def afterFlowable(self, flowable):
        bm = getattr(flowable, "_bookmark", None)
        if bm:
            key, text, level = bm
            self.canv.bookmarkPage(key)
            self.canv.addOutlineEntry(text, key, level=level, closed=(level > 0))

    # page furniture -----------------------------------------------------
    def _bg(self, canvas):
        canvas.saveState()
        canvas.setFillColor(colors.HexColor(self.pal.canvas))
        canvas.rect(0, 0, PW, PH, fill=1, stroke=0)
        canvas.restoreState()

    def deco_cover(self, canvas, doc):
        self._bg(canvas)

    def deco_body(self, canvas, doc):
        self._bg(canvas)
        canvas.saveState()
        # header
        canvas.setFillColor(colors.HexColor(self.pal.muted))
        canvas.setFont(SANS_B, 8)
        canvas.drawString(LEFT, PH - 0.5 * inch,
                          self.project_name.replace("-", " ").upper())
        canvas.setFillColor(colors.HexColor(self.pal.faint))
        canvas.setFont(MONO, 8)
        canvas.drawRightString(PW - RIGHT, PH - 0.5 * inch, "MINUSPRESS")
        canvas.setFillColor(colors.HexColor(self.pal.accent))
        canvas.rect(LEFT, PH - 0.6 * inch, CONTENT_W, 1.4, fill=1, stroke=0)
        # footer
        canvas.setFillColor(colors.HexColor(self.pal.faint))
        canvas.setFont(MONO, 8)
        canvas.drawString(LEFT, 0.4 * inch, f"generated {date.today().isoformat()}")
        canvas.drawRightString(PW - RIGHT, 0.4 * inch, str(doc.page))
        canvas.restoreState()


class PressBuilder:
    def __init__(self, dx: DashboardExport):
        self.dx = dx
        self.pal = dx.palette
        self._styles()

    def _styles(self):
        p = self.pal
        self.s_eyebrow = ParagraphStyle("eyebrow", fontName=MONO, fontSize=11,
                                        textColor=colors.HexColor(p.accent),
                                        spaceAfter=10, leading=14)
        self.s_title = ParagraphStyle("title", fontName=SERIF, fontSize=40,
                                      textColor=colors.HexColor(p.ink), leading=42)
        self.s_subtitle = ParagraphStyle("subtitle", fontName=SANS, fontSize=15,
                                         textColor=colors.HexColor(p.muted),
                                         spaceBefore=12, leading=20)
        self.s_stamp = ParagraphStyle("stamp", fontName=MONO, fontSize=10,
                                      textColor=colors.HexColor(p.faint),
                                      spaceBefore=18, leading=14)
        self.s_h1 = ParagraphStyle("h1", fontName=SERIF, fontSize=24,
                                   textColor=colors.HexColor(p.ink),
                                   spaceBefore=6, spaceAfter=4, leading=27)
        self.s_h2 = ParagraphStyle("h2", fontName=SERIF, fontSize=15,
                                   textColor=colors.HexColor(p.ink),
                                   spaceBefore=10, spaceAfter=3, leading=18)
        self.s_desc = ParagraphStyle("desc", fontName=SANS, fontSize=10,
                                     textColor=colors.HexColor(p.muted),
                                     spaceAfter=8, leading=14)
        self.s_meta = ParagraphStyle("meta", fontName=MONO, fontSize=8,
                                     textColor=colors.HexColor(p.faint), spaceAfter=4)
        self.s_link = ParagraphStyle("link", fontName=SANS, fontSize=11,
                                     textColor=colors.HexColor(p.muted),
                                     leading=18, leftIndent=14)
        self.s_link_pg = ParagraphStyle("linkpg", fontName=SANS_B, fontSize=12.5,
                                        textColor=colors.HexColor(p.accent_ink),
                                        leading=20, spaceBefore=8)
        # KPI card
        self.s_klabel = ParagraphStyle("klabel", fontName=SANS_B, fontSize=8,
                                       textColor=colors.HexColor(p.muted), leading=11)
        self.s_kvalue = ParagraphStyle("kvalue", fontName=SERIF, fontSize=26,
                                       textColor=colors.HexColor(p.accent),
                                       leading=28, spaceBefore=2)
        self.s_ksub = ParagraphStyle("ksub", fontName=SANS, fontSize=7.5,
                                     textColor=colors.HexColor(p.faint),
                                     leading=9.5, spaceBefore=4)
        self.s_kdelta = ParagraphStyle("kdelta", fontName=SANS_B, fontSize=8,
                                       leading=11, spaceBefore=4)
        # tables
        self.s_th = ParagraphStyle("th", fontName=SANS_B, fontSize=8,
                                   textColor=colors.HexColor(p.surface), leading=10)
        self.s_th_num = ParagraphStyle("thn", parent=self.s_th, alignment=TA_RIGHT)
        self.s_td = ParagraphStyle("td", fontName=SANS, fontSize=8,
                                   textColor=colors.HexColor(p.ink), leading=10)
        self.s_td_num = ParagraphStyle("tdn", fontName=MONO, fontSize=8,
                                       textColor=colors.HexColor(p.ink),
                                       leading=10, alignment=TA_RIGHT)

    # -- heading flowables tagged for the outline -----------------------
    def _heading(self, text: str, key: str, level: int):
        style = self.s_h1 if level == 0 else self.s_h2
        para = Paragraph(f'<a name="{key}"/>{_esc(text)}', style)
        para._bookmark = (key, text, level)
        return para

    # -- KPI scorecard ---------------------------------------------------
    def _kpi_row(self, kpis: list[PageItem]):
        p = self.pal
        n = min(len(kpis), 4)
        if n == 0:
            return None
        cells = []
        for it in kpis[:n]:
            kv = it.kpi
            flow = [Paragraph(_esc(kv.label.upper()), self.s_klabel),
                    Paragraph(_esc(kv.value), self.s_kvalue)]
            if kv.subtitle:
                flow.append(Paragraph(_esc(kv.subtitle), self.s_ksub))
            meta_bits = []
            if kv.target:
                col = p.good if kv.on_target else p.bad
                meta_bits.append(f'<font color="{col}">&#9679; {_esc(kv.target)}</font>')
            if kv.delta is not None:
                up = kv.delta >= 0
                arrow = "&#9650;" if up else "&#9660;"
                col = p.good if up else p.bad
                meta_bits.append(
                    f'<font color="{col}">{arrow} {abs(kv.delta):.1f}% '
                    f'{_esc(kv.delta_label or "")}</font>')
            if meta_bits:
                flow.append(Paragraph("  ".join(meta_bits), self.s_kdelta))
            cells.append(flow)
        cw = CONTENT_W / n
        t = Table([cells], colWidths=[cw] * n, rowHeights=[1.35 * inch])
        ts = [("VALIGN", (0, 0), (-1, -1), "TOP"),
              ("BACKGROUND", (0, 0), (-1, -1), colors.HexColor(p.surface)),
              ("LEFTPADDING", (0, 0), (-1, -1), 11),
              ("RIGHTPADDING", (0, 0), (-1, -1), 11),
              ("TOPPADDING", (0, 0), (-1, -1), 9),
              ("BOTTOMPADDING", (0, 0), (-1, -1), 9)]
        for c in range(n):
            ts.append(("BOX", (c, 0), (c, 0), 0.75, colors.HexColor(p.border)))
            ts.append(("LINEBEFORE", (c, 0), (c, 0), 2.2, colors.HexColor(p.accent)))
        t.setStyle(TableStyle(ts))
        return t

    # -- data table ------------------------------------------------------
    def _data_table(self, tv: TableView, *, max_rows: int = 12):
        p = self.pal
        head = [Paragraph(_esc(label), self.s_th_num if num else self.s_th)
                for _col, label, num in tv.headers]
        data = [head]
        bg_ops = []
        for ri, cells in enumerate(tv.rows[:max_rows], start=1):
            row = []
            for ci, cd in enumerate(cells):
                txt = cd.text
                if cd.rule == "icon" and cd.up is not None:
                    txt = ("▲ " if cd.up else "▼ ") + txt
                row.append(Paragraph(_esc(txt), self.s_td_num if cd.num else self.s_td))
                if cd.rule in ("data_bar", "color_scale") and cd.frac is not None:
                    base = p.surface if ri % 2 else p.surface_2
                    alpha = (0.10 + 0.42 * cd.frac) if cd.rule == "color_scale" \
                        else (0.30 * cd.frac)
                    bg_ops.append(("BACKGROUND", (ci, ri), (ci, ri),
                                   colors.HexColor(_mix(base, cd.color or p.accent, alpha))))
            data.append(row)
        ncols = len(tv.headers)
        cw = CONTENT_W / ncols
        t = Table(data, colWidths=[cw] * ncols, repeatRows=1)
        ts = [("BACKGROUND", (0, 0), (-1, 0), colors.HexColor(p.ink)),
              ("LINEBELOW", (0, 0), (-1, 0), 1.2, colors.HexColor(p.ink)),
              ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
              ("LEFTPADDING", (0, 0), (-1, -1), 6),
              ("RIGHTPADDING", (0, 0), (-1, -1), 6),
              ("TOPPADDING", (0, 0), (-1, -1), 3),
              ("BOTTOMPADDING", (0, 0), (-1, -1), 3)]
        for ri in range(1, len(data)):
            base = p.surface if ri % 2 else p.surface_2
            ts.append(("BACKGROUND", (0, ri), (-1, ri), colors.HexColor(base)))
            ts.append(("LINEBELOW", (0, ri), (-1, ri), 0.4, colors.HexColor(p.border)))
        ts.extend(bg_ops)
        t.setStyle(TableStyle(ts))
        return t

    # -- chart image -----------------------------------------------------
    def _chart_image(self, it: PageItem, *, max_h_in: float = 4.6):
        try:
            png = self.dx.figure_png(it.figure, width=1480, height=int(1480 / 1.62),
                                     scale=2, bg=self.pal.surface)
        except Exception:
            return None
        bio = io.BytesIO(png)
        ar = 1.62
        w = min(CONTENT_W, max_h_in * inch * ar)
        h = w / ar
        img = Image(bio, width=w, height=h)
        img.hAlign = "CENTER"
        return img

    # ===================================================================
    def build(self, out_path: Path) -> dict[str, Any]:
        dx = self.dx
        meta = dx.meta()
        page_items = [(pg, dx.items_for(pg)) for pg in dx.pages]

        doc = PressDoc(str(out_path), self.pal, meta["name"],
                       pagesize=PAGE, leftMargin=LEFT, rightMargin=RIGHT,
                       topMargin=0.95 * inch, bottomMargin=0.6 * inch,
                       title=f"{meta['name']} - MinusPress", author="MinusPress")
        cover_frame = Frame(0.7 * inch, 0.7 * inch, PW - 1.4 * inch, PH - 1.4 * inch,
                            id="cover")
        body_frame = Frame(LEFT, 0.6 * inch, CONTENT_W, PH - 0.6 * inch - 0.95 * inch,
                           id="body")
        doc.addPageTemplates([
            PageTemplate(id="cover", frames=[cover_frame], onPage=doc.deco_cover),
            PageTemplate(id="body", frames=[body_frame], onPage=doc.deco_body),
        ])

        story: list[Any] = []
        # ---- cover ----
        story.append(Spacer(1, 1.3 * inch))
        story.append(Paragraph("WORKSPACE INTELLIGENCE &#183; MINUSPRESS", self.s_eyebrow))
        story.append(HRFlowable(width=2.0 * inch, thickness=3,
                                color=colors.HexColor(self.pal.accent),
                                spaceBefore=0, spaceAfter=14, lineCap="round"))
        story.append(Paragraph(_esc(meta["name"].replace("-", " ")), self.s_title))
        story.append(Paragraph(
            "Rich PDF export of the live MinusAnalyst dashboard.", self.s_subtitle))
        stamp = [date.today().isoformat(), f"{meta['pages']} pages",
                 f"{meta['measures']} measures"]
        if meta.get("rows"):
            stamp.append(f"{meta['rows']:,} rows")
        if meta.get("data_through"):
            stamp.append(f"data through {meta['data_through']}")
        story.append(Paragraph("   &#183;   ".join(stamp), self.s_stamp))
        story.append(NextPageTemplate("body"))
        story.append(PageBreak())

        # ---- contents (linked) ----
        story.append(self._heading("Contents", "contents", 0))
        story.append(Spacer(1, 6))
        for pg, items in page_items:
            story.append(Paragraph(
                f'<a href="#pg_{pg.id}" color="{self.pal.accent_ink}">&#9656; '
                f'{_esc(pg.title)}</a>', self.s_link_pg))
            for it in items:
                if it.kind in ("chart", "table"):
                    story.append(Paragraph(
                        f'<a href="#w_{pg.id}_{it.widget.id}" color="{self.pal.muted}">'
                        f'&#8211; {_esc(it.title)}</a>', self.s_link))
        story.append(PageBreak())

        # ---- pages ----
        n_charts = n_tables = n_kpis = 0
        for pg, items in page_items:
            story.append(self._heading(pg.title, f"pg_{pg.id}", 0))
            if pg.description:
                story.append(Paragraph(_esc(pg.description), self.s_desc))
            else:
                story.append(Spacer(1, 4))
            kpis = [it for it in items if it.kind == "kpi"]
            n_kpis += len(kpis)
            krow = self._kpi_row(kpis)
            if krow is not None:
                story.append(krow)
                story.append(Spacer(1, 10))
            for it in items:
                if it.kind == "chart":
                    img = self._chart_image(it)
                    # Keep the heading with its figure (don't orphan the title at a
                    # page foot). CondPageBreak reserves room for heading + image so
                    # afterFlowable still fires on the real heading (its bookmark).
                    story.append(CondPageBreak(5.6 * inch))
                    # The anchor is ALWAYS emitted: the Contents page links to
                    # `#w_<pg>_<widget>`, so skipping it on a failed render leaves a
                    # dangling destination that aborts the whole PDF build (0-byte
                    # output). A chart that could not render is surfaced as a note,
                    # never silently dropped.
                    story.append(self._heading(it.title, f"w_{pg.id}_{it.widget.id}", 1))
                    if img is None:
                        story.append(Paragraph("Chart could not be rendered.", self.s_meta))
                        story.append(Spacer(1, 8))
                        continue
                    n_charts += 1
                    story.append(img)
                    # the data behind the chart (richer info), when narrow enough
                    if it.result is not None and 0 < len(it.result.frame.columns) <= 6 \
                            and it.result.frame.height:
                        tv = dx.table_view(it.widget, it.result, max_rows=8)
                        story.append(Spacer(1, 4))
                        story.append(self._data_table(tv, max_rows=8))
                    story.append(Spacer(1, 8))
                elif it.kind == "table":
                    n_tables += 1
                    tv = it.table
                    story.append(CondPageBreak(2.4 * inch))
                    story.append(self._heading(it.title, f"w_{pg.id}_{it.widget.id}", 1))
                    story.append(Paragraph(
                        f"{tv.shown_rows} of {tv.total_rows} rows", self.s_meta))
                    story.append(self._data_table(tv, max_rows=14))
                    story.append(Spacer(1, 8))
            story.append(PageBreak())

        doc.multiBuild(story)
        return {
            "ok": True, "path": str(out_path), "pages": len(dx.pages),
            "charts": n_charts, "tables": n_tables, "kpis": n_kpis,
            "theme": dx.theme_name,
        }


def build_pdf(root: Path | str, out_path: Path | str, *,
              theme: Optional[str] = None) -> dict[str, Any]:
    """Build a rich, navigable .pdf for the generated project at ``root``."""
    dx = DashboardExport(root, theme=theme)
    out_path = Path(out_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    return PressBuilder(dx).build(out_path)


__all__ = ["build_pdf", "PressBuilder"]
