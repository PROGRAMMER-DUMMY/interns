"""MinusDeck -- interactive PowerPoint export of a MinusAnalyst dashboard.

A sibling vendor to ``minus`` (the live Dash app). It reads the SAME generated
project (``project.yaml`` + ``config/dashboards/*.yaml`` + certified parquet) via
``minus.export`` and renders an interactive ``.pptx``:

  * a clickable Contents slide,
  * one section slide per dashboard page (KPI scorecard + chart thumbnails),
  * one detail slide per chart/table (full-size figure + the data behind it),
  * slide-to-slide navigation (KPI cards & thumbnails jump to detail slides;
    every slide carries Contents / Home / Prev / Next actions).

"Interactive" within PowerPoint = on-click slide jumps (``click_action``), so the
deck behaves like the served dashboard's drill/nav without a server. The look
(warm paper, terracotta accent, editorial serif headings) mirrors the Claude
theme the live app uses.
"""

from minus_deck.deck import build_deck

__all__ = ["build_deck"]
