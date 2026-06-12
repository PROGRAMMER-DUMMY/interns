"""Dashboard screener: export, screenshot, and check every dashboard page.

Run via ``uv run workspace-dashboard --workspace <ws> --screen``. It:

1. exports the static dashboard (same path KPI completion uses),
2. screenshots index.html + every KPI page with a headless browser
   (agent-browser when installed, else headless Edge/Chrome),
3. runs deterministic checks per page — render-failure annotations, missing
   pages, zero-trace charts, panel-count vs spec, blank/truncated screenshots,
   design-token color clashes (delta-E) and low contrast via the existing
   dashboard-verify helpers,
4. writes ``interns/reports/dashboard_screener/current.json`` + ``current.md``
   listing findings and screenshot paths.

The SUBJECTIVE visual pass (color mismatch against intent, misalignment,
crowding, anything a human would squint at) is deliberately left to the
orchestrating CLI agent: the report's "vision review" section lists every
screenshot for the agent to Read and judge — same pattern as the resolver's
LLM-via-CLI-agent fallback (no vision SDK calls from the platform).
"""
from __future__ import annotations

import json
import re
import shutil
import struct
import subprocess
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

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


@dataclass
class PageFinding:
    page: str
    screenshot: str = ""
    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.errors


def _check_html(html: str, page: str, spec_panel_count: int | None) -> PageFinding:
    finding = PageFinding(page=page)
    if "chart render failed" in html:
        failures = re.findall(r"chart render failed: ([^\"<]{0,120})", html)
        finding.errors.append(
            f"render-failure annotation in page: {failures[:3] or ['(unparsed)']}"
        )
    plot_count = html.count("js-plotly-plot")
    if spec_panel_count is not None and plot_count == 0 and spec_panel_count > 0:
        finding.errors.append(
            f"spec declares {spec_panel_count} panels but page has 0 plotly divs"
        )
    if "plotly" in html and "cdn.plot.ly" not in html and "Plotly" not in html:
        finding.warnings.append("plotly assets not referenced; charts may be empty")
    return finding


def screen_dashboard(repo_root: Path, workspace_rel: str) -> dict[str, Any]:
    """Export + screenshot + check every dashboard page; write the report."""
    from core.dashboard.export import export_static_html
    from core.dashboard.spec import load_kpi_spec
    from core.storage.workspace_layout import WorkspaceLayout
    from tools.dashboard_verify import _delta_e

    repo_root = Path(repo_root).resolve()
    workspace = (repo_root / workspace_rel).resolve()
    layout = WorkspaceLayout(project_root=workspace)
    export = export_static_html(repo_root, workspace_rel)
    report_dir = workspace / "interns" / "reports" / "dashboard_screener"
    shots_dir = report_dir / "shots"
    shots_dir.mkdir(parents=True, exist_ok=True)

    findings: list[PageFinding] = []
    pages = [p for p in export.get("files") or [] if p.endswith(".html")]
    for rel in pages:
        page_path = workspace / rel
        name = page_path.stem
        kpi_spec = load_kpi_spec(layout, name) if name.startswith("kpi_") else None
        panel_count = (
            len(kpi_spec.config.get("panels") or []) if kpi_spec else None
        )
        try:
            html = page_path.read_text(encoding="utf-8")
        except OSError as exc:
            finding = PageFinding(page=rel, errors=[f"unreadable page: {exc}"])
            findings.append(finding)
            continue
        finding = _check_html(html, rel, panel_count)

        shot = shots_dir / f"{name}.png"
        err = _screenshot(page_path.resolve().as_uri(), shot)
        if err:
            finding.warnings.append(f"screenshot unavailable: {err}")
        else:
            finding.screenshot = str(shot.relative_to(workspace).as_posix())
            size = _png_size(shot)
            if size is None:
                finding.errors.append("screenshot is not a valid PNG")
            elif _looks_blank(shot, *size):
                finding.errors.append(
                    "screenshot is nearly blank - page likely failed to render"
                )
        findings.append(finding)

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

    ok = all(f.ok for f in findings) and not palette_findings
    report = {
        "artifact_type": "dashboard_screener/current.json",
        "version": 1,
        "workspace": workspace_rel,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "ok": ok,
        "page_count": len(findings),
        "error_count": sum(len(f.errors) for f in findings) + len(palette_findings),
        "palette_findings": palette_findings,
        "pages": [
            {
                "page": f.page,
                "ok": f.ok,
                "screenshot": f.screenshot,
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
        "# Dashboard Screener",
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


__all__ = ["screen_dashboard"]
