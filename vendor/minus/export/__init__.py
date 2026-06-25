"""Static-export support for the vendored MinusAnalyst dashboard.

The live app (``minus.render``) is interactive Dash. The export layer reads a
generated MinusAnalyst project the SAME way the app does -- project.yaml +
config/dashboards/*.yaml + the certified parquet data -- and exposes each page's
content (KPI values, the identical themed Plotly figures, table rows) so static
exporters can render a deck or a document that matches the served dashboard.

Two vendors consume this:
  * ``minus_deck``  -> an interactive .pptx (MinusDeck)
  * ``minus_press`` -> a rich .pdf (MinusPress)
"""

from minus.export.model import (
    Cell,
    DashboardExport,
    KpiView,
    PageItem,
    Palette,
    TableView,
)

__all__ = ["DashboardExport", "Palette", "KpiView", "TableView", "Cell", "PageItem"]
