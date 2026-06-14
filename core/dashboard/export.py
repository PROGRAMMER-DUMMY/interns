"""Static HTML export of the workspace dashboard.

Generic. Writes one HTML page per KPI plus an index.html under
`dashboard/exports/`. Uses the same renderer as the live Dash app so the
spec → chart pipeline is identical.

The index mirrors the live app's interaction model with vanilla JS only:
a clickable KPI tile strip (headline + status badge per tile), per-panel
view toggles, and an inline detail region — so the file that auto-opens at
KPI completion behaves like the served dashboard without needing a server.
"""
from __future__ import annotations

from datetime import date
from pathlib import Path
from typing import Any

from core.dashboard.design_md import DesignTokens, load_design_tokens
from core.dashboard.renderer import (
    load_kpi_statuses as _load_kpi_statuses,
    render_kpi_inline,
    set_active_design,
)
from core.dashboard.spec import load_kpi_spec
from core.onboarding.kpi.registry_loader import load_kpi_definitions
from core.onboarding.workspace.flow import compute_workflow_diff
from core.storage.workspace_layout import WorkspaceLayout


# The look (palette, fonts) comes from a swappable DESIGN.md (Phase 1d) — the CSS
# below uses var(--token) and the :root block + fonts link are generated from the
# active DesignTokens, so a workspace's DESIGN.md changes the aesthetic with no code
# change. Layout/structure (masthead, grid, grain, staggered load) stays constant.
def _fonts_link(t: DesignTokens) -> str:
    fams = "&".join(f"family={f}" for f in t.font_families)
    if not fams:
        return ""
    return (
        '<link rel="preconnect" href="https://fonts.googleapis.com">'
        '<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>'
        f'<link href="https://fonts.googleapis.com/css2?{fams}&display=swap" rel="stylesheet">'
    )


def _root_css(t: DesignTokens) -> str:
    return (
        ":root {"
        f"--paper: {t.paper}; --card: {t.card}; --ink: {t.ink}; --ink-soft: {t.ink_soft};"
        f"--rule: {t.rule}; --rule-soft: {t.rule_soft}; --accent: {t.accent}; --accent-deep: {t.accent_deep};"
        f"--serif: {t.serif}; --sans: {t.sans}; --mono: {t.mono};"
        "}"
    )


# Faint paper grain as an inline SVG data URI (fixed overlay, very low opacity).
_GRAIN = (
    "data:image/svg+xml;utf8,"
    "<svg xmlns='http://www.w3.org/2000/svg' width='180' height='180'>"
    "<filter id='n'><feTurbulence type='fractalNoise' baseFrequency='0.85' numOctaves='2'/>"
    "</filter><rect width='100%25' height='100%25' filter='url(%23n)' opacity='0.5'/></svg>"
)

# Structural CSS (uses var(--token) set by _root_css). Token-independent.
_BASE_CSS = """
* { box-sizing: border-box; }
html { -webkit-font-smoothing: antialiased; text-rendering: optimizeLegibility; }
body { font-family: var(--sans); margin: 0; color: var(--ink); background: var(--paper); }
.grain { position: fixed; inset: 0; pointer-events: none; z-index: 9; opacity: 0.035;
  background-image: url("GRAINURI"); background-size: 180px 180px; mix-blend-mode: multiply; }

/* Masthead — a broadsheet nameplate */
.topbar { background: var(--paper); border-bottom: 2px solid var(--ink); padding: 2.2rem 1.5rem 1.1rem; }
.topbar .wrap { max-width: 1320px; margin: 0 auto; }
.topbar .eyebrow { font-family: var(--mono); font-size: 0.7rem; letter-spacing: 0.28em;
  text-transform: uppercase; color: var(--accent); margin: 0 0 0.5rem; }
.topbar h1 { font-family: var(--serif); font-optical-sizing: auto; font-weight: 900;
  font-size: clamp(1.9rem, 4.2vw, 3.2rem); line-height: 0.98; letter-spacing: -0.02em;
  margin: 0; color: var(--ink); }
.topbar .stamp { font-family: var(--mono); font-size: 0.76rem; color: var(--ink-soft);
  letter-spacing: 0.04em; margin-top: 0.7rem; display: flex; gap: 0.6rem; flex-wrap: wrap;
  align-items: center; }
.topbar .stamp .dot { color: var(--accent); }

.container { max-width: 1320px; margin: 0 auto; padding: 1.8rem 1.5rem 4rem; }
.grid { display: grid; grid-template-columns: repeat(auto-fit, minmax(460px, 1fr)); gap: 1.4rem; }

.kpi-card { position: relative; background: var(--card); border: 1px solid var(--rule);
  padding: 1.3rem 1.4rem 1.1rem; display: flex; flex-direction: column;
  box-shadow: 0 1px 0 rgba(27,26,23,0.04); transition: transform .35s cubic-bezier(.2,.8,.2,1), box-shadow .35s, border-color .35s;
  opacity: 0; transform: translateY(14px); animation: rise .7s cubic-bezier(.2,.8,.2,1) forwards; }
.kpi-card:hover { transform: translateY(-3px); box-shadow: -6px 10px 0 rgba(180,68,28,0.12); border-color: var(--ink); }
@keyframes rise { to { opacity: 1; transform: translateY(0); } }
.kpi-card .idx { position: absolute; top: 0.9rem; right: 1.1rem; font-family: var(--mono);
  font-size: 0.72rem; color: var(--ink-soft); letter-spacing: 0.1em; }
.kpi-card .head { display: flex; align-items: flex-end; justify-content: space-between;
  gap: 1rem; margin-bottom: 0.7rem; padding-bottom: 0.7rem; border-bottom: 1px solid var(--rule-soft); }
.kpi-card h2 { font-family: var(--serif); font-weight: 600; font-size: 1.16rem; line-height: 1.12;
  margin: 0; max-width: 30ch; letter-spacing: -0.01em; }
.kpi-card h2 a { color: var(--ink); text-decoration: none; }
.kpi-card h2 a:hover { text-decoration: underline; text-decoration-color: var(--accent); text-underline-offset: 3px; }
.kpi-card .metric { font-family: var(--mono); font-size: 0.72rem; color: var(--ink-soft);
  margin-top: 0.35rem; letter-spacing: 0.01em; }
.kpi-card .headline { font-family: var(--serif); font-weight: 900; font-size: 2rem;
  color: var(--accent); white-space: nowrap; font-variant-numeric: tabular-nums; line-height: 1; }

.panel-grid { display: grid; grid-template-columns: repeat(auto-fit, minmax(280px, 1fr)); gap: 0.9rem; margin-top: 0.3rem; min-width: 0; }
.panel { border-top: 1px solid var(--rule-soft); padding: 0.55rem 0.2rem 0.2rem; min-width: 0; overflow: hidden; }
.panel-title { font-family: var(--mono); font-size: 0.7rem; font-weight: 600; text-transform: uppercase;
  letter-spacing: 0.14em; color: var(--accent-deep); margin: 0 0 0.2rem; }
.kpi-card .chart { margin-top: 0.4rem; min-width: 0; }
/* Containment: Plotly renders at a default pixel width; force it to honor the
   cell so charts never spill out of their card/panel (verified via dashboard-verify). */
.kpi-card { overflow: hidden; }
.chart .js-plotly-plot, .chart .plot-container, .chart .svg-container { width: 100% !important; max-width: 100% !important; }
.kpi-card .foot { margin-top: 0.9rem; padding-top: 0.7rem; border-top: 1px solid var(--rule-soft);
  display: flex; align-items: center; justify-content: space-between; }
.badge { font-family: var(--mono); font-size: 0.66rem; font-weight: 600; text-transform: uppercase;
  letter-spacing: 0.16em; color: var(--ink-soft); }
.foot a { font-family: var(--mono); font-size: 0.7rem; letter-spacing: 0.08em; color: var(--ink);
  text-transform: uppercase; }
.foot a:hover { color: var(--accent); }
.blocker { border-color: var(--accent); }
.blocker .head { border-color: rgba(180,68,28,0.3); }
.recovery code { display: block; font-family: var(--mono); background: rgba(27,26,23,0.05);
  padding: 0.5rem; margin: 0.25rem 0; font-size: 0.74rem; white-space: pre-wrap; }
a { color: var(--accent-deep); }
.empty { color: var(--ink-soft); padding: 3rem 0; font-family: var(--mono); }

/* Tile strip — the live app's clickable KPI buttons, in static form. */
.strip { display: flex; gap: 0.7rem; flex-wrap: wrap; margin: 0 0 1rem; }
.tile { background: var(--card); border: 1px solid var(--rule); padding: 0.65rem 0.9rem 0.55rem;
  cursor: pointer; min-width: 200px; flex: 0 1 auto; user-select: none;
  transition: border-color .25s, transform .25s, box-shadow .25s; }
.tile:hover { transform: translateY(-2px); border-color: var(--ink); }
.tile.sel { border-color: var(--accent); box-shadow: -4px 6px 0 rgba(180,68,28,0.12); }
.tile .t { font-family: var(--mono); font-size: 0.64rem; letter-spacing: 0.18em;
  color: var(--ink-soft); text-transform: uppercase; }
.tile .h { font-family: var(--serif); font-weight: 900; font-size: 1.25rem; color: var(--accent);
  line-height: 1.15; font-variant-numeric: tabular-nums; }
.tile .n { font-size: 0.72rem; color: var(--ink-soft); max-width: 30ch; white-space: nowrap;
  overflow: hidden; text-overflow: ellipsis; }
.tile .st { font-family: var(--mono); font-size: 0.6rem; letter-spacing: 0.12em;
  text-transform: uppercase; color: var(--accent-deep); margin-top: 0.25rem; }
.tile.blockedt { opacity: 0.75; }
.tile.blockedt .h { color: var(--ink-soft); }

/* View toggles row */
.ctl { display: flex; gap: 0.9rem; align-items: center; flex-wrap: wrap;
  border-top: 1px solid var(--rule-soft); border-bottom: 1px solid var(--rule-soft);
  padding: 0.55rem 0.2rem; margin-bottom: 1.1rem; }
.ctl .lab { font-family: var(--mono); font-size: 0.66rem; letter-spacing: 0.18em;
  text-transform: uppercase; color: var(--ink-soft); }
.ctl label { font-size: 0.8rem; display: inline-flex; gap: 0.3rem; align-items: center;
  cursor: pointer; }
.ctl input { accent-color: var(--accent); }

/* Inline detail sections (one per KPI; selected one visible) */
.kdetail { display: none; }
.kdetail.on { display: block; }
.kdetail .pane.hid { display: none; }

/* Data viewer: the rows behind the charts (display-redacted) */
.dataview { margin-top: 1.1rem; border-top: 1px solid var(--rule-soft); padding-top: 0.7rem; }
.dataview summary { font-family: var(--mono); font-size: 0.7rem; font-weight: 600;
  letter-spacing: 0.14em; text-transform: uppercase; color: var(--accent-deep);
  cursor: pointer; user-select: none; }
.dataview .dnote { font-family: var(--mono); font-size: 0.68rem; color: var(--ink-soft);
  margin: 0.4rem 0; }
.dataview .dwrap { max-height: 420px; overflow: auto; border: 1px solid var(--rule); }
.dataview table { border-collapse: collapse; width: 100%; font-size: 0.78rem;
  font-variant-numeric: tabular-nums; background: var(--card); }
.dataview th { position: sticky; top: 0; background: var(--paper); text-align: left;
  font-family: var(--mono); font-size: 0.66rem; letter-spacing: 0.1em;
  text-transform: uppercase; color: var(--accent-deep); padding: 0.45rem 0.6rem;
  border-bottom: 2px solid var(--ink); }
.dataview td { padding: 0.32rem 0.6rem; border-bottom: 1px solid var(--rule-soft); }
.dataview tr:hover td { background: rgba(180,68,28,0.05); }
.dataview th { cursor: pointer; }
.dataview th.sorted-asc::after { content: " ▲"; font-size: 0.6em; }
.dataview th.sorted-desc::after { content: " ▼"; font-size: 0.6em; }
.dataview .dl { color: var(--accent-deep); }
.dataview .dnote-hint { opacity: 0.7; }
""".replace("GRAINURI", _GRAIN)

# Detail-page-only overrides: charts grow to fill the viewport instead of
# leaving dead space (the #1 screenshot-review finding).
_DETAIL_CSS = """
.detail-page .panel-grid { grid-template-columns: repeat(auto-fit, minmax(420px, 1fr)); gap: 1.2rem; }
.detail-page .panel { padding-top: 0.8rem; }
"""

# Index interactivity: tile click selects a KPI (hash-addressable), view
# checkboxes toggle panels. Plotly charts rendered inside display:none
# sections refit on reveal via the abFit() helper _wrap_page already ships.
_INDEX_JS = """
<script>
function pick(kid){
  document.querySelectorAll('.kdetail').forEach(function(s){
    s.classList.toggle('on', s.dataset.kpi===kid);
  });
  document.querySelectorAll('.tile[data-kpi]').forEach(function(t){
    t.classList.toggle('sel', t.dataset.kpi===kid);
  });
  buildViews(kid);
  if (history.replaceState) history.replaceState(null, '', '#'+kid);
  setTimeout(abFit, 60); setTimeout(abFit, 400);
}
function buildViews(kid){
  var box = document.getElementById('views'); if(!box) return;
  box.innerHTML = '';
  var sec = document.querySelector('.kdetail[data-kpi="'+kid+'"]'); if(!sec) return;
  sec.querySelectorAll('.pane').forEach(function(p, i){
    var id = 'v_'+kid+'_'+i;
    var l = document.createElement('label');
    var c = document.createElement('input');
    c.type='checkbox'; c.checked = !p.classList.contains('hid'); c.id=id;
    c.addEventListener('change', function(){
      p.classList.toggle('hid', !c.checked);
      setTimeout(abFit, 60);
    });
    l.appendChild(c);
    l.appendChild(document.createTextNode(p.dataset.title || ('View '+(i+1))));
    box.appendChild(l);
  });
}
document.querySelectorAll('.tile[data-kpi]').forEach(function(t){
  t.addEventListener('click', function(){ pick(t.dataset.kpi); });
});
window.addEventListener('load', function(){
  var first = document.querySelector('.tile[data-kpi]');
  var kid = (location.hash || '').replace('#','');
  if (!document.querySelector('.kdetail[data-kpi="'+kid+'"]')) kid = first ? first.dataset.kpi : '';
  if (kid) pick(kid);
});
</script>
"""


def _panels_html(card: dict[str, Any]) -> str:
    """Render one or more data-derived panels as a sub-grid inside the card.

    Every panel cell carries class ``pane`` and a ``data-title`` so the index
    page's view toggles can show/hide individual panels.
    """
    panels = card.get("panels") or [{"chart_html": card.get("chart_html") or "", "sub_title": ""}]
    if len(panels) == 1:
        p = panels[0]
        sub = p.get("sub_title") or ""
        return (
            f'<div class="pane" data-title="{sub or "View 1"}">'
            f'<div class="chart">{p.get("chart_html") or ""}</div></div>'
        )
    cells = []
    for i, p in enumerate(panels):
        sub = p.get("sub_title") or ""
        title = f'<div class="panel-title">{sub}</div>' if sub else ""
        cells.append(
            f'<div class="panel pane" data-title="{sub or f"View {i + 1}"}">'
            f'{title}<div class="chart">{p.get("chart_html") or ""}</div></div>'
        )
    return f'<div class="panel-grid">{"".join(cells)}</div>'


_DATA_VIEW_ROW_CAP = 200
# The index embeds every KPI's table inline; a 50-KPI workspace at the full
# cap would ship ~10k rows in one file. Inline sections stay light and link
# to the standalone page for the full table.
_DATA_VIEW_ROW_CAP_INLINE = 25


def _data_view_html(
    workspace: Path,
    kpi_id: str,
    rows: list[dict[str, Any]],
    total_hint: str = "",
    *,
    open_default: bool = False,
    max_rows: int = _DATA_VIEW_ROW_CAP,
    full_table_link: str = "",
) -> str:
    """Collapsible table of the KPI's result rows — the data behind the charts.

    A RENDERED surface: every value passes display redaction (default PII
    patterns widened by the workspace's data_policy.json) and HTML escaping.
    Capped at _DATA_VIEW_ROW_CAP rows with an explicit note; the full result
    set stays in the results packet/evidence, not the dashboard.
    """
    import html as _html

    if not rows:
        return ""
    from core.governance.data_policy import (
        load_workspace_data_policy,
        policy_redaction_patterns,
    )
    from core.onboarding.kpi.pii_redaction import (
        DEFAULT_PII_COLUMN_PATTERNS,
        redact_rows,
        workspace_aggregate_ages,
    )

    patterns = DEFAULT_PII_COLUMN_PATTERNS + tuple(
        policy_redaction_patterns(load_workspace_data_policy(workspace))
    )
    shown = redact_rows(
        rows[:max_rows], patterns=patterns, aggregate_ages=workspace_aggregate_ages(workspace)
    )
    columns = list(shown[0].keys())

    def cell(value: Any) -> str:
        if isinstance(value, float):
            value = f"{value:,.2f}"
        return _html.escape("" if value is None else str(value))

    head = "".join(f"<th>{_html.escape(c)}</th>" for c in columns)
    body = "".join(
        "<tr>" + "".join(f"<td>{cell(r.get(c))}</td>" for c in columns) + "</tr>"
        for r in shown
    )
    capped = len(rows) > max_rows
    full_ref = (
        f'<a href="{full_table_link}">full table on the KPI page</a>'
        if capped and full_table_link
        else "full results in the workspace results packet"
    )
    note = (
        f"{len(shown)} of {total_hint or len(rows)}{'+' if capped and not total_hint else ''} rows"
        f"{' (capped)' if capped else ''} · PII display-redacted · {full_ref}"
    )
    open_attr = " open" if open_default else ""
    return (
        f'<details class="dataview" id="data_{kpi_id}"{open_attr}><summary>Data</summary>'
        f'<div class="dnote">{note} · <a href="#" class="dl" data-kpi="{kpi_id}">download CSV</a>'
        f' <span class="dnote-hint">(click a column header to sort)</span></div>'
        f'<div class="dwrap"><table data-kpi="{kpi_id}"><thead><tr>{head}</tr></thead>'
        f"<tbody>{body}</tbody></table></div></details>"
    )




def _explore_preview_html(
    spec: Any, rows: list[dict[str, Any]], kpi_id: str, *, height: int = 360
) -> str:
    """Static QA proxy for the live Explore pane (Phase 1d).

    The live Explore pane is interactive (dropdowns) and only works in the served
    Dash app. For the screenshot/static export we bake one representative
    *combined* view: a grouped bar of the measure with x = the first dimension and
    color = the second dimension (when two dimensions exist), so the export reads
    like the richer interactive view instead of going blank. Falls back to a plain
    bar with one dimension, and renders nothing when the data lacks a usable
    dimension+measure pair. Workspace-agnostic — columns are classified by dtype.
    """
    from core.dashboard.renderer import _classify_columns, _figure_from_spec
    from core.dashboard.spec import DashboardSpec

    if not rows:
        return ""
    dims, measures, date_cols = _classify_columns(rows)
    if not measures or not (dims or date_cols):
        return ""
    measure = measures[0]
    if date_cols:
        # A timeline reads best as a line; color by the first dimension if present.
        chart_type = "line"
        x = date_cols[0]
        color = dims[0] if dims else ""
    elif len(dims) >= 2:
        chart_type = "grouped_bar"
        x, color = dims[0], dims[1]
    else:
        chart_type = "bar"
        x, color = dims[0], ""
    name = (measure or "").lower()
    y_format = "percent" if any(
        t in name for t in ("percent", "share", "ratio", "rate", "pct")
    ) else ""
    panel: dict[str, Any] = {"chart_type": chart_type, "x": x, "y": measure, "color": color, "title": ""}
    if y_format:
        panel["y_format"] = y_format
    base = spec if isinstance(spec, DashboardSpec) else None
    config = dict(panel)
    config.setdefault("definition", (base.config.get("definition") if base else {}) or {})
    transient = DashboardSpec(
        kpi_id=kpi_id, config=config, machine_defaults=config,
        user_overrides={}, spec_path=(base.spec_path if base else ""),
    )
    fig = _figure_from_spec(transient, rows, show_title=False)
    fig.update_xaxes(title_text="")
    fig.update_yaxes(title_text="")
    fig.update_layout(height=height)
    chart_html = fig.to_html(
        include_plotlyjs="cdn", full_html=False,
        div_id=f"explore_{kpi_id}",
        config={"responsive": True, "displayModeBar": False}, default_width="100%",
    )
    label = {"line": "timeline", "grouped_bar": "x × breakdown", "bar": "by dimension"}[chart_type]
    return (
        f'<div class="panel pane" data-title="Explore (combined · {label})">'
        f'<div class="panel-title">Explore (combined · {label})</div>'
        f'<div class="chart">{chart_html}</div></div>'
    )


def _tile_html(kpi_id: str, headline: str, title: str, badge: str, *, blocked: bool = False) -> str:
    cls = "tile blockedt" if blocked else "tile"
    badge_html = f'<div class="st">{badge}</div>' if badge else ""
    return (
        f'<div class="{cls}" data-kpi="{kpi_id}">'
        f'<div class="t">{kpi_id.replace("_", " ").upper()}</div>'
        f'<div class="h">{headline}</div>'
        f'<div class="n" title="{title}">{title}</div>'
        f"{badge_html}</div>"
    )


def _kpi_blocker_card_html(kpi_id: str, gap: dict[str, Any]) -> str:
    blockers = gap.get("blockers") or []
    recovery = gap.get("recovery_commands") or []
    blocker_summary = ", ".join(
        str(b.get("code", b) if isinstance(b, dict) else b) for b in blockers
    ) or "blocked"
    recovery_html = "".join(
        f'<div class="recovery"><strong>{cmd.get("label") or cmd.get("why") or ""}</strong>'
        f'<code>{cmd.get("command") or ""}</code></div>'
        for cmd in recovery
    )
    if not recovery_html:
        recovery_html = "<p>No recovery commands surfaced. Run <code>workspace-flow status --diff</code>.</p>"
    return (
        f'<div class="kpi-card blocker"><h2>{kpi_id}</h2>'
        f'<div class="meta">[~] {blocker_summary}</div>{recovery_html}</div>'
    )


def _wrap_page(
    title: str, body: str, *, topbar: str = "", stamp: str = "", eyebrow: str = "Workspace Intelligence",
    tokens: DesignTokens | None = None,
) -> str:
    t = tokens or DesignTokens()
    stamp_html = (
        f'<div class="stamp">{stamp}</div>' if stamp else ""
    )
    topbar_html = (
        f'<header class="topbar"><div class="wrap">'
        f'<p class="eyebrow">{eyebrow}</p><h1>{topbar}</h1>{stamp_html}'
        f'</div></header>'
        if topbar
        else ""
    )
    # Plotly with responsive:true renders at a default pixel width and only refits
    # to its container on a resize event — which never fires on a static load, so
    # charts overflow. Dispatch resize after load (twice, for late layout/fonts) to
    # force every chart to honor its cell. Verified by `dashboard-verify`.
    # Data-viewer interactivity (CSV download builds from the RENDERED —
    # already redacted — cells, never raw rows; header-click sorts, numeric-
    # aware on stripped thousands separators).
    resize_js = (
        "<script>function abFit(){window.dispatchEvent(new Event('resize'));"
        "if(window.Plotly){document.querySelectorAll('.js-plotly-plot').forEach("
        "function(d){try{window.Plotly.Plots.resize(d);}catch(e){}});}}"
        "window.addEventListener('load',function(){setTimeout(abFit,120);"
        "setTimeout(abFit,700);setTimeout(abFit,1600);});"
        "function dvCsv(t){var esc=function(s){return /[\",\\n]/.test(s)?'\"'+s.replace(/\"/g,'\"\"')+'\"':s;};"
        "var rows=[...t.querySelectorAll('tr')].map(function(tr){"
        "return [...tr.querySelectorAll('th,td')].map(function(c){return esc(c.textContent.trim());}).join(',');});"
        "return rows.join('\\n');}"
        "document.querySelectorAll('.dataview .dl').forEach(function(a){"
        "a.addEventListener('click',function(e){e.preventDefault();"
        "var t=document.querySelector('.dataview table[data-kpi=\"'+a.dataset.kpi+'\"]');if(!t)return;"
        "var b=new Blob([dvCsv(t)],{type:'text/csv'});var u=URL.createObjectURL(b);"
        "var l=document.createElement('a');l.href=u;l.download=a.dataset.kpi+'_data.csv';"
        "l.click();setTimeout(function(){URL.revokeObjectURL(u);},500);});});"
        "document.querySelectorAll('.dataview th').forEach(function(th){"
        "th.addEventListener('click',function(){"
        "var table=th.closest('table');var tbody=table.querySelector('tbody');"
        "var idx=[...th.parentNode.children].indexOf(th);"
        "var asc=!th.classList.contains('sorted-asc');"
        "table.querySelectorAll('th').forEach(function(h){h.classList.remove('sorted-asc','sorted-desc');});"
        "th.classList.add(asc?'sorted-asc':'sorted-desc');"
        "var rows=[...tbody.querySelectorAll('tr')];"
        "rows.sort(function(r1,r2){"
        "var a=r1.children[idx].textContent.trim(),b=r2.children[idx].textContent.trim();"
        "var na=parseFloat(a.replace(/,/g,'')),nb=parseFloat(b.replace(/,/g,''));"
        "var cmp=(!isNaN(na)&&!isNaN(nb))?na-nb:a.localeCompare(b);"
        "return asc?cmp:-cmp;});"
        "rows.forEach(function(r){tbody.appendChild(r);});});});"
        "</script>"
    )
    return (
        f'<!doctype html><html lang="en"><head><meta charset="utf-8">'
        f'<meta name="viewport" content="width=device-width, initial-scale=1">'
        f'{_fonts_link(t)}'
        f'<title>{title}</title><style>{_root_css(t)}{_BASE_CSS}{_DETAIL_CSS}</style></head>'
        f'<body><div class="grain"></div>{topbar_html}'
        f'<main class="container">{body}</main>{resize_js}</body></html>'
    )


def export_static_html(repo_root: Path, workspace_rel: str) -> dict[str, Any]:
    """Write the dashboard as static HTML files. Returns paths."""
    repo_root = Path(repo_root).resolve()
    workspace = (repo_root / workspace_rel).resolve()
    layout = WorkspaceLayout(project_root=workspace)
    exports_dir = workspace / "dashboard" / "exports"
    exports_dir.mkdir(parents=True, exist_ok=True)

    # DESIGN.md layer: load the workspace's design language (or the shipped default)
    # and apply it to the renderer so charts + shell share one swappable look.
    tokens = load_design_tokens(workspace)
    set_active_design(tokens)

    definitions = load_kpi_definitions(layout)
    diff = compute_workflow_diff(repo_root, workspace_rel)
    gaps_by_id = {str(g.get("kpi_id")): g for g in (diff.get("kpi_gaps") or [])}

    badges = _load_kpi_statuses(workspace)
    tiles: list[str] = []
    detail_sections: list[str] = []
    written: list[str] = []
    kpi_total = 0
    blocked_total = 0
    for kpi_id, definition in sorted(definitions.items()):
        kpi_total += 1
        spec = load_kpi_spec(layout, kpi_id)
        gap = gaps_by_id.get(kpi_id) or {}
        is_blocked = str(gap.get("status")) == "blocked" or not spec
        if is_blocked or not (spec and spec.config.get("sql_path")):
            blocked_total += 1
            tiles.append(
                _tile_html(kpi_id, "blocked", "no executable SQL", "", blocked=True)
            )
            detail_sections.append(
                f'<section class="kdetail" data-kpi="{kpi_id}">'
                f"{_kpi_blocker_card_html(kpi_id, gap or {})}</section>"
            )
            continue
        # Index detail region: compact panels. Standalone page: the same
        # panels rendered LARGER (the screenshot review found detail pages
        # mostly dead space) — worth the second local SQL execution.
        card = render_kpi_inline(repo_root, layout, spec, height=360)
        page_card = render_kpi_inline(repo_root, layout, spec, height=560)
        # Data viewer: the rows behind the charts (display-redacted, capped).
        from core.dashboard.profile import execute_result_view

        sql_rel = str(spec.config.get("sql_path") or "")
        data_rows = (
            execute_result_view(repo_root, repo_root / sql_rel, kpi_id)
            if sql_rel
            else []
        )
        data_view = _data_view_html(
            workspace, kpi_id, data_rows,
            max_rows=_DATA_VIEW_ROW_CAP_INLINE,
            full_table_link=f"{kpi_id}.html",
        )
        # Static QA proxy for the live Explore pane (one combined chart).
        explore_preview = _explore_preview_html(spec, data_rows, kpi_id, height=420)
        explore_block = (
            f'<div class="panel-grid">{explore_preview}</div>' if explore_preview else ""
        )
        explore_inline = (
            f'<div class="panel-grid">{_explore_preview_html(spec, data_rows, kpi_id, height=300)}</div>'
            if explore_preview else ""
        )
        page_body = (
            f'<div class="kpi-card detail-page"><div class="head"><div>'
            f'<h2>{page_card["title"]}</h2><div class="metric">{page_card.get("metric") or ""}</div></div>'
            f'<div class="headline">{page_card.get("headline") or ""}</div></div>'
            f'{_panels_html(page_card)}'
            f'{explore_block}'
            f"{_data_view_html(workspace, kpi_id, data_rows, open_default=True)}"
            f'<div class="foot"><a href="index.html#{kpi_id}">← back to board</a></div></div>'
        )
        page_path = exports_dir / f"{kpi_id}.html"
        page_path.write_text(
            _wrap_page(
                f"{kpi_id} — {workspace.name}",
                page_body,
                topbar=page_card["title"],
                stamp=f"{kpi_id.upper().replace('_', ' ')}",
                eyebrow="KPI Detail",
                tokens=tokens,
            ),
            encoding="utf-8",
        )
        written.append(page_path.relative_to(workspace).as_posix())
        tiles.append(
            _tile_html(
                kpi_id,
                str(card.get("headline") or ""),
                str(card.get("title") or kpi_id),
                badges.get(kpi_id, ""),
            )
        )
        detail_sections.append(
            f'<section class="kdetail" data-kpi="{kpi_id}">'
            f'<div class="kpi-card"><div class="head"><div>'
            f'<h2><a href="{kpi_id}.html">{card["title"]}</a></h2>'
            f'<div class="metric">{card.get("metric") or ""}</div></div>'
            f'<div class="headline">{card.get("headline") or ""}</div></div>'
            f"{_panels_html(card)}"
            f"{explore_inline}"
            f"{data_view}"
            f'<div class="foot"><span class="badge">{badges.get(kpi_id, "")}</span>'
            f'<a href="{kpi_id}.html">open full page →</a></div></div></section>'
        )

    if tiles:
        body = (
            f'<div class="strip">{"".join(tiles)}</div>'
            f'<div class="ctl"><span class="lab">Views</span><span id="views" '
            f'style="display:flex;gap:0.9rem;flex-wrap:wrap;"></span></div>'
            f'{"".join(detail_sections)}'
            f"{_INDEX_JS}"
        )
    else:
        body = '<div class="empty">No KPIs registered yet.</div>'
    dot = '<span class="dot">/</span>'
    parts = [date.today().isoformat(), f"{kpi_total} indicators"]
    if blocked_total:
        parts.append(f"{blocked_total} blocked")
    parts.append("DuckDB · live re-execution")
    stamp = f" {dot} ".join(parts)
    index_path = exports_dir / "index.html"
    index_path.write_text(
        _wrap_page(
            f"Dashboard — {workspace.name}",
            body,
            topbar=workspace.name.replace("-", " "),
            stamp=stamp,
            tokens=tokens,
        ),
        encoding="utf-8",
    )
    written.insert(0, index_path.relative_to(workspace).as_posix())
    return {
        "workspace": workspace.name,
        "export_dir": exports_dir.relative_to(workspace).as_posix(),
        "files": written,
        "kpi_count": kpi_total,
        "blocked_count": blocked_total,
    }


__all__ = ["export_static_html"]
