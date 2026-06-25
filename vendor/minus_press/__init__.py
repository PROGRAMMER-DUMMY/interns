"""MinusPress -- rich PDF export of a MinusAnalyst dashboard.

A sibling vendor to ``minus`` (the live Dash app). It reads the SAME generated
project via ``minus.export`` and renders a print-quality ``.pdf``:

  * a cover + a linked Contents page,
  * one section per dashboard page (KPI scorecard, then every chart full-width
    with the data behind it, plus the analysis tables),
  * an interactive PDF outline (bookmarks pane) and clickable Contents links
    (intra-document destinations), so the document is navigable, not just paper.

The look mirrors the Claude theme the live app uses (warm paper canvas,
terracotta accent, editorial serif headings, the same chart palette).
"""

from minus_press.press import build_pdf

__all__ = ["build_pdf"]
