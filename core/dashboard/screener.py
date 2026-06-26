"""Dashboard screener: serve, screenshot, and check every MinusAnalyst page.

Run via ``uv run workspace-dashboard --workspace <ws> --screen``. It:

1. generates (DQ-gated) the MinusAnalyst project + certified-clean data for the
   workspace (same artifacts ``--live`` serves),
2. launches the vendored MinusAnalyst app on a loopback ephemeral port in a
   background thread,
3. screenshots every page (KPIs / Analysis / ...) with a headless browser
   (Edge/Chrome) and dumps the rendered DOM,
4. runs deterministic checks per page — render-failure tiles, chart widgets that
   produced no plotly divs, blank/invalid screenshots — plus design-token color
   clashes (delta-E) via the dashboard-verify helpers,
5. writes ``interns/reports/dashboard_screener/current.json`` + ``current.md``
   listing findings and screenshot paths.

The SUBJECTIVE visual pass (color mismatch against intent, misalignment,
crowding, anything a human would squint at) is deliberately left to the
orchestrating CLI agent: the report's "vision review" section lists every
screenshot for the agent to Read and judge — same pattern as the resolver's
LLM-via-CLI-agent fallback (no vision SDK calls from the platform).

This screens the LIVE MinusAnalyst deliverable; the legacy static export was
removed. The visual-QA gate (vision review) is unchanged in shape.
"""
from __future__ import annotations

import contextlib
import json
import shutil
import socket
import struct
import subprocess
import sys
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

# MinusAnalyst widget types that render a plotly figure (vs kpi cards / tables).
_CHART_TYPES = {
    "line", "area", "stacked_area", "bar", "grouped_bar", "stacked_bar",
    "stacked_bar_percent", "ranked_bar", "hbar", "donut", "pie", "heatmap",
    "scatter", "combo", "small_multiples",
}

_EDGE_CANDIDATES = (
    r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
    r"C:\Program Files\Microsoft\Edge\Application\msedge.exe",
)
_CHROME_CANDIDATES = (
    r"C:\Program Files\Google\Chrome\Application\chrome.exe",
    r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
)


def _browser_bin() -> str | None:
    for candidate in _EDGE_CANDIDATES + _CHROME_CANDIDATES:
        if Path(candidate).is_file():
            return candidate
    for name in ("msedge", "chrome", "chromium"):
        found = shutil.which(name)
        if found:
            return found
    return None


def _screenshot(url: str, out_png: Path, *, width: int = 1500, height: int = 1700) -> str:
    """Headless screenshot. Returns '' on success, else the error string."""
    browser = _browser_bin()
    if not browser:
        return "no headless browser found (Edge/Chrome)"
    try:
        subprocess.run(
            [
                browser, "--headless", "--disable-gpu",
                f"--window-size={width},{height}",
                "--virtual-time-budget=12000",
                f"--screenshot={out_png}", url,
            ],
            capture_output=True, timeout=90,
        )
    except (subprocess.TimeoutExpired, OSError) as exc:
        return str(exc)
    return "" if out_png.is_file() and out_png.stat().st_size > 0 else "screenshot file not written"


def _dump_dom(url: str) -> str:
    """Headless rendered-DOM dump (after JS/callbacks). '' on failure/no browser."""
    browser = _browser_bin()
    if not browser:
        return ""
    try:
        proc = subprocess.run(
            [
                browser, "--headless", "--disable-gpu",
                "--virtual-time-budget=12000", "--dump-dom", url,
            ],
            capture_output=True, timeout=90, text=True,
        )
    except (subprocess.TimeoutExpired, OSError):
        return ""
    return proc.stdout or ""


def _png_size(path: Path) -> tuple[int, int] | None:
    try:
        with path.open("rb") as fh:
            head = fh.read(24)
        if head[:8] != b"\x89PNG\r\n\x1a\n" or head[12:16] != b"IHDR":
            return None
        width, height = struct.unpack(">II", head[16:24])
        return int(width), int(height)
    except (OSError, struct.error):
        return None


def _looks_blank(path: Path, width: int, height: int) -> bool:
    """Cheap blank-page heuristic: a rendered dashboard compresses to far more
    bytes than a flat-color page. ~0.005 byte/pixel is an empty canvas."""
    try:
        return path.stat().st_size < max(4096, int(width * height * 0.005))
    except OSError:
        return True


def _free_port() -> int:
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


@contextlib.contextmanager
def _serve_live(root: Path):
    """Serve the vendored MinusAnalyst app on a loopback ephemeral port.

    Yields ``(port, state)``; shuts the server down on exit. The vendor package
    lives under ``vendor/`` and is added to ``sys.path`` the same way the live
    CLI does it.
    """
    repo_root = Path(__file__).resolve().parents[2]
    vendor_dir = str(repo_root / "vendor")
    if vendor_dir not in sys.path:
        sys.path.insert(0, vendor_dir)
    from werkzeug.serving import make_server

    from minus.render.app import create_app

    app, state = create_app(root)
    port = _free_port()
    server = make_server("127.0.0.1", port, app.server, threaded=True)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    time.sleep(1.0)  # let the server bind + Dash register routes
    try:
        yield port, state
    finally:
        server.shutdown()


@dataclass
class PageFinding:
    page: str
    screenshot: str = ""
    shots: list[str] = field(default_factory=list)   # per-chart crops for this view
    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.errors


def _check_rendered_dom(dom: str, page: str, chart_widgets: int) -> PageFinding:
    """Structural checks on the rendered (post-callback) DOM of a live page.

    Empty ``dom`` (no headless browser) is not an error here — the screenshot
    path reports the browser-unavailable warning so it isn't double-counted.
    """
    finding = PageFinding(page=page)
    if not dom:
        return finding
    # A tile whose query/render raised shows "⚠ <exc>" with class "empty".
    if "⚠" in dom or "chart render failed" in dom:
        finding.errors.append("render-failure tile in rendered page")
    plot_count = dom.count("js-plotly-plot")
    if chart_widgets > 0 and plot_count == 0:
        finding.errors.append(
            f"page declares {chart_widgets} chart widget(s) but rendered 0 plotly divs"
        )
    return finding


def screen_dashboard(repo_root: Path, workspace_rel: str) -> dict[str, Any]:
    """Generate + serve + screenshot + check every MinusAnalyst page; write the report."""
    from core.dashboard.minus_adapter import generate, minus_root
    from core.storage.workspace_layout import WorkspaceLayout
    from tools.dashboard_verify import _delta_e

    repo_root = Path(repo_root).resolve()
    workspace = (repo_root / workspace_rel).resolve()
    layout = WorkspaceLayout(project_root=workspace)
    report_dir = workspace / "interns" / "reports" / "dashboard_screener"
    shots_dir = report_dir / "shots"
    shots_dir.mkdir(parents=True, exist_ok=True)

    findings: list[PageFinding] = []
    gen = generate(layout)
    if not gen.get("ok"):
        # DQ-gated publish failed: nothing to screen. Surface as a finding so the
        # gate blocks rather than silently passing on a stale/empty dashboard.
        reason = gen.get("reason") or "MinusAnalyst project not generated"
        detail = "; ".join(
            f"{c.get('check')}: {c.get('detail')}" for c in (gen.get("failed") or [])
        )
        findings.append(PageFinding(
            page="(generate)",
            errors=[f"MinusAnalyst publish failed: {reason}" + (f" [{detail}]" if detail else "")],
        ))
    else:
        findings.extend(_screen_live_pages(layout, workspace, shots_dir))

    # Design-token sanity: accent vs paper must be distinguishable (delta-E).
    from core.dashboard.design_md import load_design_tokens

    tokens = load_design_tokens(workspace)
    de = _delta_e(tokens.accent, tokens.paper)
    palette_findings: list[str] = []
    if de is not None and de < 20:
        palette_findings.append(
            f"accent {tokens.accent} vs paper {tokens.paper} delta-E {de:.1f} (<20): "
            "charts may not stand out from the background"
        )
    seen: list[str] = []
    for color in tokens.categorical:
        for prior in seen:
            pair_de = _delta_e(color, prior)
            if pair_de is not None and pair_de < 12:
                palette_findings.append(
                    f"categorical ramp colors {prior} and {color} delta-E "
                    f"{pair_de:.1f} (<12): series may be indistinguishable"
                )
        seen.append(color)

    # Deterministic measure-semantics guard: format/aggregation must match the
    # metric (no currency-on-count, no summed averages/shares). These are hard
    # findings -- they fail the gate without needing the vision review.
    from core.dashboard.minus_adapter import minus_root

    semantic_findings = _check_measure_semantics(minus_root(layout))
    # Layout-balance: every dashboard row must fill the 12-col grid (no ragged
    # trailing gaps, no overflow). The CLI both DECIDES the layout (planner) and
    # VERIFIES it here, so a differently-shaped workspace can't ship a broken grid.
    semantic_findings += _check_layout_balance(minus_root(layout))

    ok = all(f.ok for f in findings) and not palette_findings and not semantic_findings
    report = {
        "artifact_type": "dashboard_screener/current.json",
        "version": 2,
        "workspace": workspace_rel,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "ok": ok,
        "page_count": len(findings),
        "error_count": (sum(len(f.errors) for f in findings)
                        + len(palette_findings) + len(semantic_findings)),
        "palette_findings": palette_findings,
        "measure_semantics_findings": semantic_findings,
        # The subjective pass is the agent's job; it starts PENDING and is
        # flipped by `workspace-dashboard --record-vision-review` with
        # provenance (Human-Gate Provenance Rule: empty reviewer -> agent).
        # The workflow guard flags completed workflows that never flipped it.
        "vision_review": {
            "status": "pending",
            "shots": [s for f in findings for s in
                      ([f.screenshot] if f.screenshot else []) + f.shots],
        },
        "pages": [
            {
                "page": f.page,
                "ok": f.ok,
                "screenshot": f.screenshot,
                "shots": f.shots,
                "errors": f.errors,
                "warnings": f.warnings,
            }
            for f in findings
        ],
    }
    report_dir.mkdir(parents=True, exist_ok=True)
    (report_dir / "current.json").write_text(
        json.dumps(report, indent=2) + "\n", encoding="utf-8"
    )

    lines = [
        "# Dashboard Screener (MinusAnalyst live)",
        "",
        f"- Workspace: `{workspace_rel}`",
        f"- Pages: {len(findings)}  |  Status: {'[ok]' if ok else '[x] findings below'}",
        "",
    ]
    for f in findings:
        marker = "[ok]" if f.ok else "[x]"
        lines.append(f"## {marker} {f.page}")
        for e in f.errors:
            lines.append(f"- [x] {e}")
        for w in f.warnings:
            lines.append(f"- [~] {w}")
        if f.screenshot:
            lines.append(f"- screenshot: `{f.screenshot}`")
        for s in f.shots:
            lines.append(f"  - chart crop: `{s}`")
        lines.append("")
    if semantic_findings:
        lines.append("## [x] Measure semantics")
        lines.extend(f"- [x] {s}" for s in semantic_findings)
        lines.append("")
    if palette_findings:
        lines.append("## [x] Palette")
        lines.extend(f"- [x] {p}" for p in palette_findings)
        lines.append("")
    lines += [
        "## Vision review (agent step)",
        "",
        "Deterministic checks cannot judge aesthetics. The reviewing agent",
        "should Read each screenshot above and check: colors match the design",
        "language and differ where they encode different things; no",
        "overlapping/clipped labels; charts aligned to their cards; nothing",
        "rendered as an empty or skewed frame; headline numbers plausible",
        "against the charts. Record findings in the workflow notes.",
        "",
    ]
    (report_dir / "current.md").write_text("\n".join(lines), encoding="utf-8")
    return {
        "ok": ok,
        "report_json": str((report_dir / "current.json").relative_to(repo_root).as_posix()),
        "report_md": str((report_dir / "current.md").relative_to(repo_root).as_posix()),
        "screenshot_dir": str(shots_dir.relative_to(repo_root).as_posix()),
        "page_count": len(findings),
        "error_count": report["error_count"],
    }


def _check_layout_balance(root: Path) -> list[str]:
    """Verify every generated dashboard tab tiles the 12-column grid cleanly --
    no ragged gaps, no overflow. Reads the emitted dashboard YAMLs and runs the
    layout planner's balance check per row of widgets (grouped by tab). Generic."""
    import yaml

    from core.dashboard.layout_planner import layout_findings

    findings: list[str] = []
    dash_dir = root / "config" / "dashboards"
    if not dash_dir.exists():
        return []
    for path in sorted(dash_dir.glob("*.yaml")):
        try:
            dash = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        except Exception:
            continue
        # Group widget widths by tab (untabbed = one group); each group tiles its
        # own grid. KPI cards + their heroes share a span so they align.
        by_tab: dict[str, list[int]] = {}
        for w in dash.get("widgets") or []:
            tab = str(w.get("tab") or "_main")
            width = int(w.get("width") or 0)
            if width:
                by_tab.setdefault(tab, []).append(width)
        for tab, widths in by_tab.items():
            for f in layout_findings(widths):
                findings.append(f"{path.stem}/{tab}: {f}")
    return findings


def _check_measure_semantics(root: Path) -> list[str]:
    """Deterministic guard: a measure's display format / aggregation must not
    contradict its metric. Catches the RCM-shaped-default class of bug (counts
    formatted as currency, per-group averages summed) automatically, instead of
    relying on the subjective vision review. Reads the generated MinusAnalyst
    ``project.yaml`` measures; generic across workspaces.
    """
    import yaml

    from core.dashboard.model.cuts import measure_func

    proj_path = root / "project.yaml"
    if not proj_path.exists():
        return []
    try:
        project = yaml.safe_load(proj_path.read_text(encoding="utf-8")) or {}
    except Exception:
        return []
    findings: list[str] = []
    for m in project.get("measures") or []:
        name = str(m.get("name") or "")
        fmt = str(m.get("fmt") or "")
        agg = str(m.get("agg") or "")
        field = str(m.get("field") or "")
        column = field.split(".", 1)[1] if "." in field else field
        func = measure_func("", name) or measure_func("", column)
        if func == "count" and fmt == "currency":
            findings.append(
                f"measure `{name}` is a count but is formatted as currency "
                "($) -- counts are not money (fix fmt -> int)")
        if func in ("avg", "mean", "median") and agg == "sum":
            findings.append(
                f"measure `{name}` is an average but is aggregated by sum "
                "-- summing per-group averages is meaningless (fix agg -> avg)")
        if fmt == "percent" and agg == "sum":
            findings.append(
                f"measure `{name}` is a share/percent but is aggregated by sum "
                "-- summing shares = 100% (fix agg -> max)")
    return findings


# JS read out of every plotly chart on the current view: trace + data-point
# counts (to catch an empty chart), whether it carries any title/axis label, and
# its on-page bounding box (for a clear per-chart crop).
_CHART_PROBE_JS = """
Array.from(document.querySelectorAll('.js-plotly-plot')).map(function(gd){
  var d = gd.data || [];
  var pts = d.reduce(function(a,t){return a + (
    (t.x&&t.x.length)||(t.y&&t.y.length)||(t.values&&t.values.length)||
    (t.labels&&t.labels.length)||(t.z&&t.z.length)||0);}, 0);
  var lay = gd.layout || {};
  function tt(o){try{return (o&&o.title&&(o.title.text||o.title))||'';}catch(e){return '';}}
  var labelled = !!(tt(lay) || tt(lay.xaxis) || tt(lay.yaxis));
  var r = gd.getBoundingClientRect();
  return {traces:d.length, points:pts, labelled:labelled,
          x:r.x+window.scrollX, y:r.y+window.scrollY, w:r.width, h:r.height};
})
"""
_TABLE_PROBE_JS = (
    "document.querySelectorAll('.dash-table-container,table,.dash-spreadsheet').length"
)
_TAB_PROBE_JS = (
    "Array.from(document.querySelectorAll('.tab,[role=tab],.dash-tab,.dcc-tab'))"
    ".map(function(e){return e.innerText.trim();}).filter(Boolean)"
)


def _slug(text: str) -> str:
    import re
    return re.sub(r"[^a-z0-9]+", "_", str(text).lower()).strip("_") or "page"


def _screen_live_pages(layout, workspace: Path, shots_dir: Path) -> list[PageFinding]:
    """Serve the live app and capture it STRUCTURE-AWARE: every page, every
    sub-tab, and a clear per-chart crop, with deterministic per-chart checks
    (empty data, unlabelled, empty tab). Uses a CDP-driven browser so sub-tabs
    and chart data are actually reachable; falls back to the legacy single-shot
    capture if CDP is unavailable."""
    from core.dashboard.minus_adapter import minus_root

    with _serve_live(minus_root(layout)) as (port, state):
        try:
            return _capture_cdp(state, port, workspace, shots_dir)
        except Exception as exc:  # CDP unavailable/failed -> never break screening
            findings = _capture_fallback(state, port, workspace, shots_dir)
            if findings:
                findings[0].warnings.append(
                    f"structure-aware capture unavailable ({exc}); used single-shot fallback")
            return findings


def _wait_for_charts(browser, expected: int, *, timeout_ms: int = 9000) -> int:
    """Poll until at least ``expected`` plotly charts are present (or the count
    stops growing, or timeout). Returns the final count. Makes chart-presence
    checks robust to slow Dash callback rendering."""
    import time as _t
    if expected <= 0:
        browser.wait(800)
        return 0
    deadline = _t.time() + timeout_ms / 1000.0
    last = -1
    stable = 0
    while _t.time() < deadline:
        try:
            n = int(browser.evaluate(
                "document.querySelectorAll('.js-plotly-plot').length") or 0)
        except Exception:
            n = 0
        if n >= expected:
            return n
        stable = stable + 1 if n == last else 0
        if n > 0 and stable >= 3:        # count plateaued below expected -> stop
            return n
        last = n
        browser.wait(500)
    return last if last > 0 else 0


def _capture_cdp(state, port: int, workspace: Path, shots_dir: Path) -> list[PageFinding]:
    from core.dashboard.cdp import CDPBrowser

    findings: list[PageFinding] = []
    with CDPBrowser() as browser:
        for page in state.pages:
            chart_widgets = sum(
                1 for w in page.widgets if getattr(w, "type", None) in _CHART_TYPES
            )
            browser.navigate(f"http://127.0.0.1:{port}/{page.id}")
            # Wait for Dash callbacks to render the declared charts before judging
            # (a fixed settle can fire before a busy page paints its plots, falsely
            # flagging "0 charts"). Poll until the chart count reaches expectations
            # or stabilizes.
            _wait_for_charts(browser, chart_widgets)
            tabs = _unique(browser.evaluate(_TAB_PROBE_JS) or [])
            views = tabs or [None]
            for view in views:
                label = page.id if view is None else f"{page.id} / {view}"
                if view is not None and not browser.click_text(view):
                    continue
                _wait_for_charts(browser, 1)   # let the tab's charts paint too
                findings.append(
                    _capture_view(browser, label, page.id, chart_widgets if view is None else 0,
                                  workspace, shots_dir)
                )
    return findings


def _capture_view(browser, label: str, page_id: str, chart_widgets: int,
                  workspace: Path, shots_dir: Path) -> PageFinding:
    finding = PageFinding(page=label)
    charts = browser.evaluate(_CHART_PROBE_JS) or []
    n_tables = int(browser.evaluate(_TABLE_PROBE_JS) or 0)

    shot = shots_dir / f"{_slug(label)}.png"
    if browser.screenshot(shot):
        finding.screenshot = str(shot.relative_to(workspace).as_posix())
        size = _png_size(shot)
        if size is None:
            finding.errors.append("screenshot is not a valid PNG")
        elif _looks_blank(shot, *size):
            finding.errors.append("view is nearly blank - likely failed to render")
    else:
        finding.warnings.append("screenshot unavailable")

    # Per-chart deterministic checks + a clear isolated crop for each chart.
    for i, c in enumerate(charts, start=1):
        if int(c.get("traces") or 0) > 0 and int(c.get("points") or 0) == 0:
            finding.errors.append(f"chart {i} rendered with 0 data points (empty chart)")
        if not c.get("labelled"):
            finding.warnings.append(f"chart {i} has no title or axis labels")
        if c.get("w") and c.get("h"):
            crop = shots_dir / f"{_slug(label)}_chart{i}.png"
            if browser.screenshot(crop, clip={"x": c["x"], "y": c["y"],
                                              "width": c["w"], "height": c["h"]}):
                finding.shots.append(str(crop.relative_to(workspace).as_posix()))

    # An empty view: declared charts but rendered none, or no chart AND no table.
    if chart_widgets > 0 and not charts:
        finding.errors.append(
            f"view declares {chart_widgets} chart widget(s) but rendered 0 charts")
    elif not charts and n_tables == 0:
        finding.warnings.append("view has no chart or table (appears empty)")
    return finding


def _capture_fallback(state, port: int, workspace: Path, shots_dir: Path) -> list[PageFinding]:
    """Legacy single-shot-per-page capture (no sub-tabs/interaction)."""
    findings: list[PageFinding] = []
    for page in state.pages:
        chart_widgets = sum(
            1 for w in page.widgets if getattr(w, "type", None) in _CHART_TYPES
        )
        url = f"http://127.0.0.1:{port}/{page.id}"
        finding = _check_rendered_dom(_dump_dom(url), page.id, chart_widgets)
        shot = shots_dir / f"{page.id}.png"
        if _screenshot(url, shot):
            finding.warnings.append("screenshot unavailable")
        else:
            finding.screenshot = str(shot.relative_to(workspace).as_posix())
            size = _png_size(shot)
            if size is None:
                finding.errors.append("screenshot is not a valid PNG")
            elif _looks_blank(shot, *size):
                finding.errors.append("screenshot is nearly blank - page likely failed to render")
        findings.append(finding)
    return findings


def _unique(items: list) -> list:
    seen: set = set()
    out = []
    for it in items:
        if it not in seen:
            seen.add(it)
            out.append(it)
    return out


def record_vision_review(
    repo_root: Path,
    workspace_rel: str,
    *,
    reviewed_by: str = "",
    notes: str = "",
) -> dict[str, Any]:
    """Flip the latest screener report's vision_review to done, with provenance.

    Mirrors the repo's Human-Gate Provenance Rule: an empty ``reviewed_by``
    records ``source: agent`` (the orchestrating agent looked at the shots);
    a name records ``source: human``. Raises FileNotFoundError when no
    screener report exists — you cannot review what was never screened.
    """
    repo_root = Path(repo_root).resolve()
    report_path = (
        repo_root / workspace_rel / "interns" / "reports" / "dashboard_screener" / "current.json"
    )
    if not report_path.exists():
        raise FileNotFoundError(f"no screener report to review: {report_path}")
    report = json.loads(report_path.read_text(encoding="utf-8"))
    # A deterministically-broken dashboard cannot be vision-passed: the eyeball
    # gate is for subjective aesthetics, not for waving through machine-detected
    # defects (empty charts, currency-on-count, summed averages). Fix them and
    # re-screen first.
    if not report.get("ok", True):
        raise ValueError(
            "cannot record vision review: the screener found deterministic defects "
            "(see interns/reports/dashboard_screener/current.md). Fix them and "
            "re-run --screen before reviewing.")
    review = report.get("vision_review") or {}
    shots = review.get("shots") or []
    review.update(
        {
            "status": "done",
            "reviewed_by": reviewed_by,
            "source": "human" if reviewed_by else "agent",
            "notes": notes,
            "shots_reviewed": len(shots),
            "reviewed_at": datetime.now(timezone.utc).isoformat(),
        }
    )
    report["vision_review"] = review
    report_path.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    return review


def vision_review_pending(repo_root: Path, workspace_rel: str) -> bool:
    """True when a screener report exists whose vision review never happened."""
    report_path = (
        Path(repo_root) / workspace_rel / "interns" / "reports" / "dashboard_screener" / "current.json"
    )
    if not report_path.exists():
        return False
    try:
        report = json.loads(report_path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return False
    return (report.get("vision_review") or {}).get("status") == "pending"


__all__ = ["record_vision_review", "screen_dashboard", "vision_review_pending"]
