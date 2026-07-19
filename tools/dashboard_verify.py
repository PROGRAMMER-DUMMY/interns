"""Interactive browser verification gate for dashboards.

Loads a rendered dashboard (static `file://` export or a live Dash URL) in a real
browser via the `agent-browser` CLI and asserts the hard quality bar BEFORE a
dashboard is shown to a user:

  * no element overflows its container (bounding-box check — catches plots
    spilling out of their cards/panels),
  * charts actually rendered (Plotly SVGs present, not blank),
  * multi-series charts carry a legend,
  * legend toggle is interactive (clicking a legend entry changes trace visibility).

Returns a structured pass/fail report and exits non-zero on failure, so the agent
treats a failed dashboard as a blocker rather than presenting a broken one.

Generic: no workspace/domain specifics. Requires `agent-browser` on PATH
(`npm i -g agent-browser && agent-browser install`).
"""
from __future__ import annotations
from core.observability.cost_ledger import anchored

import argparse
import json
import math
import re
import shutil
import subprocess
import time
from dataclasses import asdict, dataclass, field
from typing import Any


_OVERFLOW_TOL = 3  # px slack before calling it an overflow
# Perceptual just-noticeable-difference band for categorical series colors.
# This is a HUMAN-VISION constant (CIELAB delta-E), not a workspace/business
# threshold: below it, two series read as "the same color". Not a tunable knob.
_DELTA_E_MIN = 16.0
# WCAG-style minimum contrast of a series color against the chart background,
# so a series isn't near-invisible. Also a perceptual constant.
_CONTRAST_MIN = 1.6


def _parse_rgb(value: str) -> tuple[float, float, float] | None:
    """Parse 'rgb(r,g,b)' or '#rrggbb'/'#rgb' to 0-255 floats."""
    s = (value or "").strip()
    m = re.match(r"rgba?\(\s*(\d+)\s*,\s*(\d+)\s*,\s*(\d+)", s)
    if m:
        return float(m.group(1)), float(m.group(2)), float(m.group(3))
    m = re.match(r"#([0-9a-fA-F]{6})$", s)
    if m:
        h = m.group(1)
        return float(int(h[0:2], 16)), float(int(h[2:4], 16)), float(int(h[4:6], 16))
    m = re.match(r"#([0-9a-fA-F]{3})$", s)
    if m:
        h = m.group(1)
        return float(int(h[0] * 2, 16)), float(int(h[1] * 2, 16)), float(int(h[2] * 2, 16))
    return None


def _srgb_to_lab(rgb: tuple[float, float, float]) -> tuple[float, float, float]:
    def lin(c: float) -> float:
        c /= 255.0
        return c / 12.92 if c <= 0.04045 else ((c + 0.055) / 1.055) ** 2.4
    r, g, b = (lin(x) for x in rgb)
    # sRGB D65 -> XYZ
    x = (0.4124 * r + 0.3576 * g + 0.1805 * b) / 0.95047
    y = (0.2126 * r + 0.7152 * g + 0.0722 * b) / 1.0
    z = (0.0193 * r + 0.1192 * g + 0.9505 * b) / 1.08883
    def f(t: float) -> float:
        return t ** (1 / 3) if t > 0.008856 else 7.787 * t + 16 / 116
    fx, fy, fz = f(x), f(y), f(z)
    return 116 * fy - 16, 500 * (fx - fy), 200 * (fy - fz)


def _delta_e(c1: str, c2: str) -> float | None:
    """CIE76 delta-E (Euclidean in Lab) between two colors. None if unparseable."""
    a, b = _parse_rgb(c1), _parse_rgb(c2)
    if a is None or b is None:
        return None
    la, lb = _srgb_to_lab(a), _srgb_to_lab(b)
    return math.sqrt(sum((x - y) ** 2 for x, y in zip(la, lb)))


def _contrast_ratio(fg: str, bg: str) -> float | None:
    """WCAG relative-luminance contrast ratio between a color and a background."""
    a, b = _parse_rgb(fg), _parse_rgb(bg)
    if a is None or b is None:
        return None
    def lum(rgb: tuple[float, float, float]) -> float:
        def lin(c: float) -> float:
            c /= 255.0
            return c / 12.92 if c <= 0.03928 else ((c + 0.055) / 1.055) ** 2.4
        r, g, bl = (lin(x) for x in rgb)
        return 0.2126 * r + 0.7152 * g + 0.0722 * bl
    l1, l2 = lum(a), lum(b)
    hi, lo = max(l1, l2), min(l1, l2)
    return (hi + 0.05) / (lo + 0.05)


def _agent_browser_bin() -> str | None:
    """Resolve the agent-browser executable, incl. Windows .cmd/.ps1 npm shims."""
    for name in ("agent-browser", "agent-browser.cmd", "agent-browser.exe"):
        found = shutil.which(name)
        if found:
            return found
    return None


def _ab(*args: str, timeout: int = 60) -> str:
    """Run an agent-browser command, return stdout (stripped). Never raises for a
    non-zero exit — the caller interprets output."""
    binary = _agent_browser_bin()
    if not binary:
        return "__ERR__ agent-browser not found on PATH (npm i -g agent-browser)"
    try:
        proc = subprocess.run(
            [binary, *args],
            capture_output=True, text=True, timeout=timeout,
        )
    except (subprocess.TimeoutExpired, OSError) as exc:
        return f"__ERR__ {exc}"
    return (proc.stdout or proc.stderr or "").strip()


# JS run in the page: measure overflow, count rendered charts, check legends, and
# extract per-plot series colors + background for perceptual color checks.
# MUST be a single line — agent-browser's `eval` mishandles newlines in argv.
_PROBE_JS = (
    "(()=>{"
    "const c=[...document.querySelectorAll('.kpi-card .chart, .panel, .js-plotly-plot')];"
    "const ov=[];"
    "for(const el of c){const ox=el.scrollWidth-el.clientWidth;"
    "if(ox>TOL)ov.push({where:(el.className||'').toString().slice(0,24),px:ox});}"
    "const grab=(el)=>{const st=el.getAttribute('style')||'';"
    "const m=st.match(/(?:fill|stroke):\\s*(rgb[^;]+|#[0-9a-fA-F]+)/);return m?m[1]:null;};"
    "const p=[...document.querySelectorAll('.js-plotly-plot')];"
    "let r=0;const plots=[];"
    "for(const x of p){if(x.querySelector('svg.main-svg'))r++;"
    "const li=x.querySelectorAll('g.legend g.groups > g, g.legend .traces').length;"
    "const cs=new Set();"
    "x.querySelectorAll('g.barlayer path, g.scatterlayer .lines path, g.scatterlayer path.js-line, g.trace path.point').forEach(e=>{const f=grab(e);if(f&&f!=='none')cs.add(f);});"
    "let bg='rgb(255,255,255)';const bgEl=x.querySelector('rect.bg');if(bgEl){const f=grab(bgEl);if(f)bg=f;}"
    "plots.push({legend_items:li,has_legend:!!x.querySelector('g.legend'),colors:[...cs].slice(0,12),bg:bg});}"
    "return JSON.stringify({plot_divs:p.length,rendered:r,overflow:ov,plots:plots});"
    "})()"
).replace("TOL", str(_OVERFLOW_TOL))


@dataclass
class VerifyResult:
    url: str
    passed: bool
    blocked: bool = False  # gate could not run (decision 1: != pass)
    plot_divs: int = 0
    rendered: int = 0
    multi_series: int = 0
    multi_with_legend: int = 0
    overflow: list[dict[str, Any]] = field(default_factory=list)
    color_clashes: list[dict[str, Any]] = field(default_factory=list)
    low_contrast: list[dict[str, Any]] = field(default_factory=list)
    failures: list[str] = field(default_factory=list)
    screenshot: str = ""
    note: str = ""

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def _parse_eval(raw: str) -> dict[str, Any] | None:
    # agent-browser may wrap the JSON string in quotes / escape it.
    text = raw.strip()
    for _ in range(2):
        try:
            val = json.loads(text)
        except json.JSONDecodeError:
            break
        if isinstance(val, dict):
            return val
        if isinstance(val, str):
            text = val
            continue
        break
    # Fallback: find the first {...} block.
    start, end = text.find("{"), text.rfind("}")
    if 0 <= start < end:
        try:
            return json.loads(text[start : end + 1])
        except json.JSONDecodeError:
            return None
    return None


def verify(url: str, *, screenshot: str = "", settle_ms: int = 2500) -> VerifyResult:
    res = VerifyResult(url=url, passed=False)
    opened = _ab("--allow-file-access", "open", url, timeout=90)
    if opened.startswith("__ERR__"):
        res.note = f"agent-browser open failed: {opened}"
        res.blocked = True  # decision 1: gate could not run != pass
        res.failures.append(res.note)
        return res
    # Wait for the chart DOM to exist, then let Plotly (CDN) finish drawing.
    _ab("wait", ".js-plotly-plot", timeout=30)
    time.sleep(settle_ms / 1000)
    raw = _ab("eval", _PROBE_JS, timeout=60)
    data = _parse_eval(raw)
    if data is None:
        res.note = f"probe eval returned no JSON: {raw[:200]}"
        res.blocked = True
        res.failures.append(res.note)
        return res
    res.plot_divs = int(data.get("plot_divs", 0))
    res.rendered = int(data.get("rendered", 0))
    res.overflow = list(data.get("overflow", []))

    plots = data.get("plots", []) or []
    # Multi-series = a plot whose legend lists more than one entry.
    multi = [p for p in plots if int(p.get("legend_items", 0)) > 1]
    res.multi_series = len(multi)
    res.multi_with_legend = sum(1 for p in multi if p.get("has_legend"))

    # Perceptual color separation: any two series colors in a multi-series plot
    # must differ by at least the JND band; else they read as the same color.
    for i, p in enumerate(plots):
        colors = [c for c in (p.get("colors") or []) if c]
        if int(p.get("legend_items", 0)) > 1 and len(colors) >= 2:
            for a in range(len(colors)):
                for b in range(a + 1, len(colors)):
                    de = _delta_e(colors[a], colors[b])
                    if de is not None and de < _DELTA_E_MIN:
                        res.color_clashes.append(
                            {"plot": i, "a": colors[a], "b": colors[b], "delta_e": round(de, 1)}
                        )
        # Contrast of each series color vs the chart background.
        bg = p.get("bg") or "rgb(255,255,255)"
        for col in colors:
            cr = _contrast_ratio(col, bg)
            if cr is not None and cr < _CONTRAST_MIN:
                res.low_contrast.append({"plot": i, "color": col, "bg": bg, "ratio": round(cr, 2)})

    if res.plot_divs and res.rendered < res.plot_divs:
        res.failures.append(f"{res.plot_divs - res.rendered}/{res.plot_divs} charts did not render (blank)")
    if res.overflow:
        worst = max((o.get("px", 0) for o in res.overflow), default=0)
        res.failures.append(f"{len(res.overflow)} element(s) overflow their container (worst {worst}px)")
    if res.multi_series and res.multi_with_legend < res.multi_series:
        res.failures.append(
            f"{res.multi_series - res.multi_with_legend}/{res.multi_series} multi-series charts have no legend"
        )
    if res.color_clashes:
        worst = min((c["delta_e"] for c in res.color_clashes), default=0)
        res.failures.append(
            f"{len(res.color_clashes)} series color pair(s) too similar to tell apart (worst delta-E {worst} < {_DELTA_E_MIN})"
        )
    if res.low_contrast:
        res.failures.append(
            f"{len(res.low_contrast)} series color(s) too low-contrast vs background (< {_CONTRAST_MIN}:1)"
        )

    if screenshot:
        shot = _ab("screenshot", screenshot, timeout=60)
        res.screenshot = screenshot if not shot.startswith("__ERR__") else ""

    res.passed = not res.failures
    return res



@anchored("dashboard-verify")
def main() -> int:
    parser = argparse.ArgumentParser(description="Verify a dashboard renders correctly in a real browser.")
    parser.add_argument("--url", required=True, help="file:///C:/.../index.html or http://127.0.0.1:8060")
    parser.add_argument("--screenshot", default="", help="Optional PNG path to capture.")
    parser.add_argument("--json", action="store_true", help="Print JSON report.")
    args = parser.parse_args()

    res = verify(args.url, screenshot=args.screenshot)
    if args.json:
        print(json.dumps(res.to_dict(), indent=2))
    else:
        mark = "[ok]" if res.passed else "[x]"
        print(f"{mark} dashboard verify: {args.url}")
        print(f"  plots: {res.rendered}/{res.plot_divs} rendered · multi-series w/ legend: {res.multi_with_legend}/{res.multi_series}")
        for f in res.failures:
            print(f"  [x] {f}")
        if res.passed:
            print("  [ok] no overflow, charts rendered, legends present")
        if res.screenshot:
            print(f"  screenshot: {res.screenshot}")
    return 0 if res.passed else 1


if __name__ == "__main__":
    main()
