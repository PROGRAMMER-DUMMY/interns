"""Static HTML export of the workspace dashboard.

Generic. Writes one HTML page per KPI plus an index.html under
`dashboard/exports/`. Uses the same renderer as the live Dash app so the
spec → chart pipeline is identical.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any

from core.dashboard.renderer import render_kpi_html
from core.dashboard.spec import load_kpi_spec
from core.onboarding.kpi.registry_loader import load_kpi_definitions
from core.onboarding.workspace.flow import compute_workflow_diff
from core.storage.workspace_layout import WorkspaceLayout


_BASE_CSS = """
body { font-family: -apple-system, system-ui, sans-serif; max-width: 1200px; margin: 0 auto; padding: 1.5rem; color: #1f2933; background: #f8f9fb; }
h1 { font-size: 1.6rem; margin-bottom: 0.25rem; }
.subtitle { color: #6b7280; margin-bottom: 1.5rem; }
.kpi-card { background: white; border: 1px solid #e5e7eb; border-radius: 8px; padding: 1rem; margin-bottom: 1rem; box-shadow: 0 1px 2px rgba(0,0,0,0.04); }
.kpi-card h2 { font-size: 1.1rem; margin: 0 0 0.25rem 0; }
.kpi-card .meta { font-size: 0.85rem; color: #6b7280; margin-bottom: 0.5rem; }
.blocker { border-color: #fbbf24; background: #fffbeb; }
.recovery code { display: block; background: #f3f4f6; padding: 0.5rem; border-radius: 4px; margin: 0.25rem 0; font-size: 0.8rem; white-space: pre-wrap; }
a { color: #2563eb; text-decoration: none; }
"""


def _kpi_index_card_html(kpi_id: str, title: str, link: str) -> str:
    return (
        f'<div class="kpi-card"><h2><a href="{link}">{kpi_id}</a></h2>'
        f'<div class="meta">{title}</div></div>'
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


def _wrap_page(title: str, body: str) -> str:
    return (
        f'<!doctype html><html><head><meta charset="utf-8">'
        f'<title>{title}</title><style>{_BASE_CSS}</style></head>'
        f'<body>{body}</body></html>'
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
    for kpi_id, definition in sorted(definitions.items()):
        spec = load_kpi_spec(layout, kpi_id)
        gap = gaps_by_id.get(kpi_id) or {}
        is_blocked = str(gap.get("status")) == "blocked" or not spec
        if is_blocked or not (spec and spec.config.get("sql_path")):
            blocker_html = _kpi_blocker_card_html(kpi_id, gap or {})
            index_cards.append(blocker_html)
            continue
        chart_html = render_kpi_html(repo_root, layout, spec)
        title = str(spec.config.get("title") or kpi_id)
        page_body = (
            f'<h1>{kpi_id}</h1>'
            f'<div class="subtitle">{title}</div>'
            f'<div class="kpi-card">{chart_html}</div>'
            f'<p><a href="index.html">← back to index</a></p>'
        )
        page_path = exports_dir / f"{kpi_id}.html"
        page_path.write_text(_wrap_page(f"{kpi_id} — {workspace.name}", page_body), encoding="utf-8")
        written.append(page_path.relative_to(workspace).as_posix())
        index_cards.append(_kpi_index_card_html(kpi_id, title, f"{kpi_id}.html"))

    index_body = (
        f'<h1>Workspace Dashboard — {workspace.name}</h1>'
        f'<div class="subtitle">Static export. For live charts run '
        f'<code>uv run workspace-dashboard --workspace {workspace_rel}</code>.</div>'
        f'{"".join(index_cards) if index_cards else "<p>No KPIs registered yet.</p>"}'
    )
    index_path = exports_dir / "index.html"
    index_path.write_text(
        _wrap_page(f"Dashboard — {workspace.name}", index_body),
        encoding="utf-8",
    )
    written.insert(0, index_path.relative_to(workspace).as_posix())
    return {
        "workspace": workspace.name,
        "export_dir": exports_dir.relative_to(workspace).as_posix(),
        "files": written,
    }


__all__ = ["export_static_html"]
