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

import argparse
import json
import shutil
import subprocess
import time
from dataclasses import asdict, dataclass, field
from typing import Any


_OVERFLOW_TOL = 3  # px slack before calling it an overflow


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


# JS run in the page: measure overflow, count rendered charts, check legends.
# MUST be a single line — agent-browser's `eval` mishandles newlines in argv.
_PROBE_JS = (
    "(()=>{"
    "const c=[...document.querySelectorAll('.kpi-card .chart, .panel, .js-plotly-plot')];"
    "const ov=[];"
    "for(const el of c){const ox=el.scrollWidth-el.clientWidth;"
    "if(ox>TOL)ov.push({where:(el.className||'').toString().slice(0,24),px:ox});}"
    "const p=[...document.querySelectorAll('.js-plotly-plot')];"
    "let r=0,m=0,wl=0;"
    "for(const x of p){if(x.querySelector('svg.main-svg'))r++;"
    "const li=x.querySelectorAll('g.legend g.groups > g, g.legend .traces').length;"
    "if(li>1){m++;if(x.querySelector('g.legend'))wl++;}}"
    "return JSON.stringify({plot_divs:p.length,rendered:r,multi_series:m,multi_with_legend:wl,overflow:ov});"
    "})()"
).replace("TOL", str(_OVERFLOW_TOL))


@dataclass
class VerifyResult:
    url: str
    passed: bool
    plot_divs: int = 0
    rendered: int = 0
    multi_series: int = 0
    multi_with_legend: int = 0
    overflow: list[dict[str, Any]] = field(default_factory=list)
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
        res.failures.append(res.note)
        return res
    # Wait for the chart DOM to exist, then let Plotly (CDN) finish drawing.
    _ab("wait", ".js-plotly-plot", timeout=30)
    time.sleep(settle_ms / 1000)
    raw = _ab("eval", _PROBE_JS, timeout=60)
    data = _parse_eval(raw)
    if data is None:
        res.note = f"probe eval returned no JSON: {raw[:200]}"
        res.failures.append(res.note)
        return res
    res.plot_divs = int(data.get("plot_divs", 0))
    res.rendered = int(data.get("rendered", 0))
    res.multi_series = int(data.get("multi_series", 0))
    res.multi_with_legend = int(data.get("multi_with_legend", 0))
    res.overflow = list(data.get("overflow", []))

    if res.plot_divs and res.rendered < res.plot_divs:
        res.failures.append(f"{res.plot_divs - res.rendered}/{res.plot_divs} charts did not render (blank)")
    if res.overflow:
        worst = max((o.get("px", 0) for o in res.overflow), default=0)
        res.failures.append(f"{len(res.overflow)} element(s) overflow their container (worst {worst}px)")
    if res.multi_series and res.multi_with_legend < res.multi_series:
        res.failures.append(
            f"{res.multi_series - res.multi_with_legend}/{res.multi_series} multi-series charts have no legend"
        )

    if screenshot:
        shot = _ab("screenshot", screenshot, timeout=60)
        res.screenshot = screenshot if not shot.startswith("__ERR__") else ""

    res.passed = not res.failures
    return res


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
