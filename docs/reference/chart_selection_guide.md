# Chart Selection Guide (knowledge source for `core/dashboard/chart_knowledge.py`)

Sources, explored 2026-06-12:
- From Data to Viz — https://www.data-to-viz.com/ (decision tree: data shape -> chart)
- Data Viz Project — https://datavizproject.com/ (160+ chart catalog; filter by
  family / input / function / shape). Site blocks automated fetch (HTTP 403);
  structure confirmed via https://data.europa.eu/apps/data-visualisation-guide/dataviz-project

The platform's panel decider (`core/dashboard/profile.py::decide_panels`)
consults `core/dashboard/chart_knowledge.py`, which encodes the subset of the
data-to-viz framework that can apply to KPI result views. Result views are
pre-aggregated GROUP BY outputs, so the only input families that occur are:

| data-to-viz family | our shape | charts encoded |
|---|---|---|
| Ordered numeric / time series (10) | temporal dim + measure | line; stacked_area (few-series share); no color split beyond 6 series (spaghetti caveat) |
| One categorical + numeric (5/7) | one cat dim + measure | bar (few cats), ranked_bar top-N (>15), lollipop (similar heights), donut (share, <=5), treemap (share, 6-15) |
| Two categorical + numeric (6/8) | two cat dims + measure | heatmap (both sides 2..20 distinct) |
| No dimension | measure only | big_number |

Families that can NOT occur on pre-aggregated views (no row-level data):
distribution (violin/boxplot/histogram/ridgeline), two-numeric correlation
(scatter/bubble), network, geographic (no geo columns in result contracts yet).

## Caveats encoded (data-to-viz)

- Ordering: bars/lollipops sort by value; banded categories (age bands) sort
  by their natural numeric order.
- Pie/donut: max 5 slices, label+percent on the slice, no legend, shares only.
- Spaghetti: a trend with more than 6 series drops the color split.
- Bars are never log-scaled (length must stay proportional); log remains a
  line-chart option when values span >= 50x.
- Lollipop replaces bars when the plotted values are within 35% of each other
  ("many bars of similar height" caveat).
- Bubble/area-not-diameter, dual-axis, rainbow palettes: not applicable or
  already enforced by the Okabe-Ito ramp in DESIGN.md tokens.

## Refresh procedure

Selection never fetches the network. To refresh this knowledge: re-explore the
two sites (WebFetch data-to-viz.com works; datavizproject.com 403s — use the
europa.eu review or search), update this document, then adjust the rules and
thresholds in `core/dashboard/chart_knowledge.py` and their tests
(`tests/test_chart_knowledge.py`). Every rule carries `selection_reason` +
`selection_source` into the emitted panel specs, so generated dashboards stay
traceable to this guide.
