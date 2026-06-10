"""Static HTML export of the workspace dashboard.

Generic. Writes one HTML page per KPI plus an index.html under
`dashboard/exports/`. Uses the same renderer as the live Dash app so the
spec → chart pipeline is identical.
"""
from __future__ import annotations

from datetime import date
from pathlib import Path
from typing import Any

from core.dashboard.renderer import render_kpi_html, render_kpi_inline
from core.dashboard.spec import load_kpi_spec
from core.onboarding.kpi.registry_loader import load_kpi_definitions
from core.onboarding.workspace.flow import compute_workflow_diff
from core.storage.workspace_layout import WorkspaceLayout


# Editorial "data desk" aesthetic: warm paper canvas, characterful Fraunces serif
# masthead, Spline Sans Mono datelines/figures, one sharp sienna accent, hairline
# rules, grain texture, staggered load. Distinctive yet credible for a data product.
# Generic — no domain styling.
_FONTS_LINK = (
    '<link rel="preconnect" href="https://fonts.googleapis.com">'
    '<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>'
    '<link href="https://fonts.googleapis.com/css2?'
    'family=Fraunces:opsz,wght@9..144,400;9..144,600;9..144,900&'
    'family=Hanken+Grotesk:wght@400;500;600;700&'
    'family=Spline+Sans+Mono:wght@400;500;600&display=swap" rel="stylesheet">'
)

# Faint paper grain as an inline SVG data URI (fixed overlay, very low opacity).
_GRAIN = (
    "data:image/svg+xml;utf8,"
    "<svg xmlns='http://www.w3.org/2000/svg' width='180' height='180'>"
    "<filter id='n'><feTurbulence type='fractalNoise' baseFrequency='0.85' numOctaves='2'/>"
    "</filter><rect width='100%25' height='100%25' filter='url(%23n)' opacity='0.5'/></svg>"
)

_BASE_CSS = """
:root {
  --paper: #f3efe6; --card: #fbf9f3; --ink: #1b1a17; --ink-soft: #6f6a60;
  --rule: #d7d1c4; --rule-soft: #e7e2d6; --accent: #b4441c; --accent-deep: #2f4452;
  --serif: 'Fraunces', Georgia, serif;
  --sans: 'Hanken Grotesk', system-ui, sans-serif;
  --mono: 'Spline Sans Mono', ui-monospace, monospace;
}
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
""".replace("GRAINURI", _GRAIN)


def _panels_html(card: dict[str, Any]) -> str:
    """Render one or more data-derived panels as a sub-grid inside the card."""
    panels = card.get("panels") or [{"chart_html": card.get("chart_html") or "", "sub_title": ""}]
    if len(panels) == 1:
        return f'<div class="chart">{panels[0].get("chart_html") or ""}</div>'
    cells = []
    for p in panels:
        sub = p.get("sub_title") or ""
        title = f'<div class="panel-title">{sub}</div>' if sub else ""
        cells.append(f'<div class="panel">{title}<div class="chart">{p.get("chart_html") or ""}</div></div>')
    return f'<div class="panel-grid">{"".join(cells)}</div>'


def _kpi_grid_card_html(kpi_id: str, card: dict[str, Any], link: str, idx: int = 0) -> str:
    metric = card.get("metric") or ""
    metric_html = f'<div class="metric">{metric}</div>' if metric else ""
    panels = card.get("panels") or []
    badge = f"{len(panels)} views" if len(panels) > 1 else (card.get("chart_type") or "single view")
    delay = f"animation-delay:{idx * 90}ms;"
    return (
        f'<article class="kpi-card" style="{delay}">'
        f'<span class="idx">{idx + 1:02d}</span>'
        f'<div class="head"><div><h2><a href="{link}">{card.get("title") or kpi_id}</a></h2>'
        f'{metric_html}</div><div class="headline">{card.get("headline") or ""}</div></div>'
        f'{_panels_html(card)}'
        f'<div class="foot"><span class="badge">{badge}</span><a href="{link}">open detail →</a></div>'
        f'</article>'
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
    title: str, body: str, *, topbar: str = "", stamp: str = "", eyebrow: str = "Workspace Intelligence"
) -> str:
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
    resize_js = (
        "<script>function abFit(){window.dispatchEvent(new Event('resize'));"
        "if(window.Plotly){document.querySelectorAll('.js-plotly-plot').forEach("
        "function(d){try{window.Plotly.Plots.resize(d);}catch(e){}});}}"
        "window.addEventListener('load',function(){setTimeout(abFit,120);"
        "setTimeout(abFit,700);setTimeout(abFit,1600);});</script>"
    )
    return (
        f'<!doctype html><html lang="en"><head><meta charset="utf-8">'
        f'<meta name="viewport" content="width=device-width, initial-scale=1">'
        f'{_FONTS_LINK}'
        f'<title>{title}</title><style>{_BASE_CSS}</style></head>'
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

    definitions = load_kpi_definitions(layout)
    diff = compute_workflow_diff(repo_root, workspace_rel)
    gaps_by_id = {str(g.get("kpi_id")): g for g in (diff.get("kpi_gaps") or [])}

    index_cards: list[str] = []
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
            index_cards.append(_kpi_blocker_card_html(kpi_id, gap or {}))
            continue
        card = render_kpi_inline(repo_root, layout, spec, height=360)
        # Detail page: the same data-derived panels, larger.
        page_body = (
            f'<div class="kpi-card"><div class="head"><div>'
            f'<h2>{card["title"]}</h2><div class="metric">{card.get("metric") or ""}</div></div>'
            f'<div class="headline">{card.get("headline") or ""}</div></div>'
            f'{_panels_html(card)}'
            f'<div class="foot"><a href="index.html">← back to board</a></div></div>'
        )
        page_path = exports_dir / f"{kpi_id}.html"
        page_path.write_text(
            _wrap_page(
                f"{kpi_id} — {workspace.name}",
                page_body,
                topbar=card["title"],
                stamp=f"{kpi_id.upper().replace('_', ' ')}",
                eyebrow="KPI Detail",
            ),
            encoding="utf-8",
        )
        written.append(page_path.relative_to(workspace).as_posix())
        index_cards.append(
            _kpi_grid_card_html(kpi_id, card, f"{kpi_id}.html", idx=len(index_cards))
        )

    grid = (
        f'<div class="grid">{"".join(index_cards)}</div>'
        if index_cards
        else '<div class="empty">No KPIs registered yet.</div>'
    )
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
            grid,
            topbar=workspace.name.replace("-", " "),
            stamp=stamp,
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
