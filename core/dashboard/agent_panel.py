"""DashboardAgent helper: NL plot request -> spec user_overrides + verify.

The conversational ``DashboardAgent`` skill (``skills/dashboard-agent/SKILL.md``)
turns a plain-language request ("a new plot stating Percentage Share by Visittype
for each department") into a single dashboard *panel* and persists it through the
governed spec contract:

* the new panel is written to the KPI spec's ``user_overrides`` ONLY (never
  ``machine_defaults``), so a ``refresh_workspace_dashboard`` regeneration —
  which rewrites ``machine_defaults`` — preserves it verbatim
  (``save_kpi_spec`` keeps ``user_overrides``);
* because ``merge_spec`` is a shallow top-level merge, the renderer reads the
  merged ``panels`` key wholesale — so an additive panel must be written as
  ``machine_panels + [new_panel]`` into ``user_overrides["panels"]``. That makes
  the new panel render AND freezes the user's chosen panel set across regen;
* a display-redacted column (PII/PHI/PCI by the workspace's effective patterns)
  is never used as an axis or series — the request is rejected with a reason.

Generic across workspaces: every column is resolved from the live result view
(via DuckDB re-execution) and dtypes; no workspace/dataset/column is hardcoded.

This module owns the deterministic translation so the skill is not a free-hand
JSON edit. It reuses the existing spec-merge code in ``core.dashboard.spec`` and
the chart knowledge base in ``core.dashboard.chart_knowledge``.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from core.dashboard.chart_knowledge import is_ordinal_categories
from core.dashboard.profile import execute_result_view, profile_columns
from core.dashboard.spec import load_kpi_spec, save_kpi_spec
from core.onboarding.kpi.pii_redaction import (
    is_pii_column,
    workspace_redaction_patterns,
)
from core.storage.workspace_layout import WorkspaceLayout


_SHARE_RE = re.compile(r"(percent|share|proportion|ratio|rate|%)", re.IGNORECASE)
_TREND_RE = re.compile(r"(trend|over time|by (month|day|week|year|quarter|date))", re.IGNORECASE)
_RANK_RE = re.compile(r"(top\s*\d*|rank|ranked|leading|biggest|largest)", re.IGNORECASE)
# Grouping connectors come in two strengths so "share BY visittype FOR EACH department"
# resolves correctly: the PRIMARY connector names the x-axis (the thing you iterate
# over — "for each department" -> x=department), the SECONDARY connector names the
# breakdown/series ("by visittype" -> color=visittype). A request with only a
# secondary connector ("share by visittype") puts that single dimension on x.
_PRIMARY_BY_RE = re.compile(r"\b(?:for each|for every|per)\b", re.IGNORECASE)
_SECONDARY_BY_RE = re.compile(
    r"\b(?:split by|grouped by|broken down by|across|by)\b",
    re.IGNORECASE,
)


@dataclass
class PanelRequest:
    """The parsed outcome of an NL panel request."""

    panel: dict[str, Any] = field(default_factory=dict)
    rationale: list[str] = field(default_factory=list)
    rejected: list[str] = field(default_factory=list)
    ok: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "panel": self.panel,
            "rationale": self.rationale,
            "rejected": self.rejected,
            "ok": self.ok,
        }


def _norm(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "", (text or "").lower())


def _match_column(token: str, columns: list[str]) -> str:
    """Resolve a free-text token to a real result column (generic, fuzzy).

    Exact (normalized) match wins; otherwise a substring match either way. No
    workspace-specific aliasing — this is pure string overlap against the live
    result columns so it works for any workspace.
    """
    if not token:
        return ""
    nt = _norm(token)
    if not nt:
        return ""
    by_norm = {c: _norm(c) for c in columns}
    for col, nc in by_norm.items():
        if nc == nt:
            return col
    # Longest containing match, so "visittype" beats a stray "type".
    candidates = [
        col for col, nc in by_norm.items() if nc and (nt in nc or nc in nt)
    ]
    if candidates:
        return max(candidates, key=lambda c: len(by_norm[c]))
    return ""


def _measure_column(columns: list[str], profiles: dict[str, Any]) -> str:
    """Pick the measure (y) column: prefer a share/percent-named numeric column,
    else the first numeric column."""
    numeric = [c for c in columns if c in profiles and profiles[c].numeric]
    for c in numeric:
        if _SHARE_RE.search(c):
            return c
    if numeric:
        return numeric[0]
    # No numeric column profiled (e.g. rows unavailable): fall back to a
    # share/percent-named column or the last column.
    for c in columns:
        if _SHARE_RE.search(c):
            return c
    return columns[-1] if columns else ""


def parse_panel_request(
    request: str,
    columns: list[str],
    *,
    rows: list[dict[str, Any]] | None = None,
    workspace_root: str | Path | None = None,
) -> PanelRequest:
    """Translate an NL plot request into a single panel dict.

    ``columns`` are the live result-view columns; ``rows`` (optional) let the
    parser profile dtypes/cardinality and detect ordinal axes. Display-redacted
    columns are rejected as axes/series. Returns a :class:`PanelRequest` whose
    ``ok`` flag is False when the request cannot be satisfied (e.g. the only
    matching column is redacted, or no plottable dimension was found).
    """
    out = PanelRequest()
    rows = rows or []
    profiles = profile_columns(rows) if rows else {}
    patterns = workspace_redaction_patterns(workspace_root) if workspace_root is not None else ()

    def redacted(col: str) -> bool:
        return bool(col) and bool(patterns) and is_pii_column(col, patterns=patterns)

    # 1) Resolve the grouping structure. "for each C" (primary) names the x-axis;
    #    "by B" (secondary) names the breakdown/series. So "share BY visittype FOR
    #    EACH department" -> x=department, color=visittype. With only a secondary
    #    connector ("share by visittype") that single dimension goes on x.
    text = request or ""

    # The measure (y) is resolved first so it is EXCLUDED from dimension matching
    # — otherwise "value by segment" would pick the numeric "value" as a series.
    measure = _measure_column(columns, profiles)
    is_share = bool(_SHARE_RE.search(text)) or (bool(measure) and bool(_SHARE_RE.search(measure)))
    # Numeric/measure columns are never dimensions (x or color).
    numeric_cols = {c for c in columns if c in profiles and profiles[c].numeric}
    not_dim = set(numeric_cols)
    if measure:
        not_dim.add(measure)

    primary = _PRIMARY_BY_RE.search(text)
    if primary:
        # x = the dimension after "for each"/"per"; series = a "by Y" that
        # precedes the primary connector ("...by VISITTYPE for each DEPARTMENT").
        x_col = _best_column_in(text[primary.end():], columns, exclude=not_dim)
        secondary = _SECONDARY_BY_RE.search(text[: primary.start()])
        series_frag = text[secondary.end(): primary.start()] if secondary else ""
        series_col = (
            _best_column_in(series_frag, columns, exclude=not_dim | {x_col})
            if series_frag else ""
        )
    else:
        secondary = _SECONDARY_BY_RE.search(text)
        if secondary:
            # "X by Y": Y (after the connector) is the x dimension; any dimension
            # named before it becomes the series.
            x_col = _best_column_in(text[secondary.end():], columns, exclude=not_dim)
            head = text[: secondary.start()]
            series_col = (
                _best_column_in(head, columns, exclude=not_dim | {x_col})
                if head else ""
            )
        else:
            x_col = ""
            series_col = ""
    # If no connector resolved an x, recover any dimension mentions from the whole
    # request (and a second one as the series).
    if not x_col:
        x_col = _best_column_in(text, columns, exclude=not_dim)
    if x_col and not series_col:
        series_col = _best_column_in(text, columns, exclude=not_dim | {x_col})

    # 2) Reject redacted columns up front (never plot a masked column).
    for role, col in (("x", x_col), ("series/color", series_col), ("measure", measure)):
        if col and redacted(col):
            out.rejected.append(
                f"column '{col}' (requested as {role}) is display-redacted; refusing to plot it"
            )
            if role == "x":
                x_col = ""
            elif role.startswith("series"):
                series_col = ""
            else:
                measure = ""

    if not x_col:
        out.rationale.append(
            "could not resolve a (non-redacted) grouping dimension from the request "
            f"against columns {columns}"
        )
        out.ok = False
        return out

    # 3) Choose a chart type from the request + data shape (data-to-viz family).
    chart_type, why = _choose_chart_type(
        x_col=x_col,
        series_col=series_col,
        is_share=is_share,
        text=text,
        rows=rows,
    )

    panel: dict[str, Any] = {
        "chart_type": chart_type,
        "x": x_col,
        "y": measure,
        "agg": "sum",
        "title": _panel_title(x_col, series_col, is_share),
        "source": "dashboard-agent",
    }
    if series_col and chart_type in ("stacked_bar_percent", "grouped_bar", "line", "stacked_area", "heatmap"):
        panel["color"] = series_col
    if is_share:
        panel["y_format"] = "percent"

    out.panel = panel
    out.rationale.append(why)
    out.rationale.append(f"x='{x_col}'" + (f", color='{series_col}'" if "color" in panel else ""))
    out.rationale.append(f"measure (y)='{measure}', agg=sum" + (", y_format=percent" if is_share else ""))
    out.ok = True
    return out


def _best_column_in(fragment: str, columns: list[str], *, exclude: set[str] | None = None) -> str:
    """Best column whose normalized name overlaps any word in the fragment."""
    exclude = exclude or set()
    candidates = [c for c in columns if c not in exclude]
    best = ""
    best_len = 0
    nfrag = _norm(fragment)
    for col in candidates:
        nc = _norm(col)
        if not nc:
            continue
        if nc in nfrag or nfrag and nfrag in nc:
            if len(nc) > best_len:
                best, best_len = col, len(nc)
    if best:
        return best
    # Token-level fallback: try each word in the fragment.
    for token in re.findall(r"[A-Za-z0-9_]+", fragment or ""):
        col = _match_column(token, candidates)
        if col and len(_norm(col)) > best_len:
            best, best_len = col, len(_norm(col))
    return best


def _choose_chart_type(
    *,
    x_col: str,
    series_col: str,
    is_share: bool,
    text: str,
    rows: list[dict[str, Any]],
) -> tuple[str, str]:
    """Pick a chart_type the renderer supports, from the request + data shape."""
    x_values = [r.get(x_col) for r in rows] if rows else []
    if _TREND_RE.search(text):
        return "line", "trend/time phrasing -> line (one value per period)"
    if is_share and series_col:
        return (
            "stacked_bar_percent",
            "share split by a second dimension -> 100% stacked bar (renderer "
            "aggregates by (x,color) to a true 0-100%)",
        )
    if _RANK_RE.search(text):
        return "ranked_bar", "ranking phrasing -> ranked top-N bar"
    if is_share and not series_col:
        # Few non-ordinal categories read as a composition; ordinal stays a bar.
        if x_values and is_ordinal_categories(x_values):
            return "bar", "share over ordinal/banded categories -> ordered bar (order is information)"
        return "bar", "single-dimension share -> bar (renderer normalizes to 0-100%)"
    return "bar", "one dimension vs a measure -> bar (data-to-viz barplot family)"


def _panel_title(x_col: str, series_col: str, is_share: bool) -> str:
    measure = "Percentage Share" if is_share else "Value"
    pretty_x = x_col.replace("_", " ").title()
    if series_col:
        pretty_s = series_col.replace("_", " ").title()
        return f"{measure} by {pretty_s} for each {pretty_x}"
    return f"{measure} by {pretty_x}"


def _spec_rows(repo_root: Path, layout: WorkspaceLayout, kpi_id: str, spec) -> list[dict[str, Any]]:
    sql_rel = str(spec.config.get("sql_path") or "")
    if not sql_rel:
        return []
    return execute_result_view(repo_root, repo_root / sql_rel, kpi_id)


def live_result_columns(repo_root: str | Path, layout: WorkspaceLayout, kpi_id: str) -> list[str]:
    """The live result-view columns for a KPI (empty if SQL unavailable)."""
    spec = load_kpi_spec(layout, kpi_id)
    if not spec:
        return []
    rows = _spec_rows(Path(repo_root), layout, kpi_id, spec)
    return list(rows[0].keys()) if rows else list(spec.config.get("_result_columns") or [])


def apply_panel_override(
    layout: WorkspaceLayout,
    kpi_id: str,
    panel: dict[str, Any],
) -> dict[str, Any]:
    """Append ``panel`` to the KPI spec's ``user_overrides['panels']``.

    Writes ``machine_defaults['panels'] + existing_user_panels + [panel]`` into
    ``user_overrides['panels']`` so the renderer (which reads the MERGED, shallow
    ``panels`` key) shows every machine panel plus the new one, while the whole
    list lives under ``user_overrides`` and therefore survives a regeneration
    verbatim. ``machine_defaults`` is never touched.

    Returns a summary dict (spec path + panel count). Raises ``ValueError`` when
    the KPI spec does not exist.
    """
    spec = load_kpi_spec(layout, kpi_id)
    if spec is None:
        raise ValueError(f"no dashboard spec for {kpi_id} in {layout.project_root}")

    machine_panels = list(spec.machine_defaults.get("panels") or [])
    user_overrides = dict(spec.user_overrides or {})
    # If the user already froze a panel list, extend that; otherwise seed from
    # the current machine panels so nothing the machine derived is lost.
    if "panels" in user_overrides and isinstance(user_overrides["panels"], list):
        base = list(user_overrides["panels"])
    else:
        base = machine_panels
    new_panels = base + [panel]
    user_overrides["panels"] = new_panels

    # Persist: save_kpi_spec preserves user_overrides ON DISK, but it reads them
    # from the existing file — so write the merged spec payload with the new
    # user_overrides explicitly.
    payload = {
        "artifact_type": "dashboard_spec",
        "version": spec.config.get("version", 2),
        "kpi_id": kpi_id,
        "machine_defaults": spec.machine_defaults,
        "user_overrides": user_overrides,
    }
    # save_kpi_spec would re-read the OLD user_overrides off disk and overwrite
    # ours, so write the file directly here (still preserving the contract: only
    # user_overrides changed; machine_defaults is untouched).
    import json

    path = layout.project_root / "dashboard" / f"{kpi_id}.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    from core.dashboard.spec import _now  # local: reuse the spec timestamp

    payload["updated_at"] = _now()
    path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    return {
        "kpi_id": kpi_id,
        "spec_path": path.as_posix(),
        "panel_count": len(new_panels),
        "added_panel_title": panel.get("title", ""),
    }


def verify_workspace_dashboard(
    repo_root: str | Path,
    workspace_rel: str,
    *,
    screenshot: str = "",
) -> dict[str, Any]:
    """Re-export the dashboard and run the browser verify gate.

    Returns the verify report dict (``passed``/``failures``/...). The caller
    treats a non-``passed`` result as a blocker, per the dashboard-design gate.
    """
    from core.dashboard.export import export_static_html

    repo_root = Path(repo_root).resolve()
    export = export_static_html(repo_root, workspace_rel)
    index = repo_root / workspace_rel / "dashboard" / "exports" / "index.html"
    url = index.as_uri()
    from tools.dashboard_verify import verify

    result = verify(url, screenshot=screenshot)
    report = result.to_dict()
    report["export"] = export
    report["url"] = url
    return report


def run_panel_request(
    repo_root: str | Path,
    workspace_rel: str,
    kpi_id: str,
    request: str,
    *,
    screenshot: str = "",
    do_verify: bool = True,
) -> dict[str, Any]:
    """End-to-end: parse an NL request, apply the override, then verify.

    This is the single entry point the DashboardAgent skill drives. It never
    edits ``machine_defaults`` and never plots a redacted column.
    """
    repo_root = Path(repo_root).resolve()
    workspace = (repo_root / workspace_rel).resolve()
    layout = WorkspaceLayout(project_root=workspace)

    spec = load_kpi_spec(layout, kpi_id)
    if spec is None:
        return {"ok": False, "error": f"no dashboard spec for {kpi_id}"}
    rows = _spec_rows(repo_root, layout, kpi_id, spec)
    columns = list(rows[0].keys()) if rows else list(spec.config.get("_result_columns") or [])

    parsed = parse_panel_request(request, columns, rows=rows, workspace_root=workspace)
    if not parsed.ok:
        return {"ok": False, "parsed": parsed.to_dict(), "columns": columns}

    applied = apply_panel_override(layout, kpi_id, parsed.panel)
    result: dict[str, Any] = {
        "ok": True,
        "parsed": parsed.to_dict(),
        "applied": applied,
        "columns": columns,
    }
    if do_verify:
        result["verify"] = verify_workspace_dashboard(
            repo_root, workspace_rel, screenshot=screenshot
        )
        result["ok"] = bool(result["verify"].get("passed"))
    return result


__all__ = [
    "PanelRequest",
    "apply_panel_override",
    "live_result_columns",
    "parse_panel_request",
    "run_panel_request",
    "verify_workspace_dashboard",
]
