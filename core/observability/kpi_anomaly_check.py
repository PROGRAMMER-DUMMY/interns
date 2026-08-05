"""KPI headline anomaly detection: median-absolute-deviation over trailing
run history, so a KPI's number lurching for no known reason gets flagged
before it reaches the dashboard silently (audit A8's other half -- "No KPI
threshold/anomaly alerting exists").

Three pieces, deliberately separable:
  - `check_kpi_anomalies` -- the pure MAD math (fully unit-testable, no I/O).
  - `write_kpi_alerts_report` -- the report/webhook writer.
  - `check_kpi_anomalies_main` -- the CLI glue a post-results DAG task shells
    out to: reads this run's KPI results, reads/updates this workspace's own
    headline-history file, calls the two above.
"""
from __future__ import annotations

import argparse
import json
import os
import statistics
from datetime import date
from pathlib import Path
from typing import Any

from core.observability.cost_ledger import anchored

DEFAULT_MAD_THRESHOLD = 3.0
_MIN_HISTORY_POINTS = 4
_ALERT_WEBHOOK_ENV = "AUTORESEARCH_ALERT_WEBHOOK_URL"
# ~a season of daily runs -- history must not grow unbounded.
_MAX_HISTORY_RUNS = 90


def check_kpi_anomalies(
    history: list,
    current: dict,
    mad_threshold: float = DEFAULT_MAD_THRESHOLD,
) -> list:
    """Flag KPIs whose current headline is more than `mad_threshold` median
    absolute deviations from its own trailing history.

    `history` is a list of past runs' ``{kpi_id: headline_value}`` snapshots
    (order doesn't matter); `current` is this run's snapshot. A KPI with
    fewer than 4 trailing points is never flagged -- a young KPI has no
    track record to judge against, and alarming on it would just be noise
    (never alarm early). Findings are sorted by kpi_id for stable output.
    """
    findings: list = []
    for kpi_id, current_value in current.items():
        if not isinstance(current_value, (int, float)) or isinstance(current_value, bool):
            continue
        trailing = [
            float(h[kpi_id])
            for h in history
            if isinstance(h, dict)
            and isinstance(h.get(kpi_id), (int, float))
            and not isinstance(h.get(kpi_id), bool)
        ]
        if len(trailing) < _MIN_HISTORY_POINTS:
            continue
        median = statistics.median(trailing)
        mad = statistics.median(abs(v - median) for v in trailing)
        if mad == 0:
            # A perfectly stable KPI: any change at all is the anomaly: no
            # scale to divide by, so distance is 0 (unchanged) or infinite.
            distance = 0.0 if current_value == median else float("inf")
        else:
            distance = abs(current_value - median) / mad
        if distance > mad_threshold:
            findings.append({
                "kpi_id": kpi_id,
                "current": float(current_value),
                "median": median,
                "mad": mad,
                "distance": distance,
                "mad_threshold": mad_threshold,
            })
    return sorted(findings, key=lambda f: f["kpi_id"])


def write_kpi_alerts_report(
    findings: list,
    *,
    layout: Any,
    webhook_url: str = "",
    http: Any = None,
) -> str:
    """Write `interns/reports/kpi_alerts/current.md` and, when findings exist
    and a webhook is configured, POST a summary. `http` is an injectable
    module/object exposing `Request`/`urlopen` the same shape as
    `urllib.request` (for tests); alerting failures never raise."""
    report_dir = layout.reports_dir / "kpi_alerts"
    report_dir.mkdir(parents=True, exist_ok=True)
    report_path = report_dir / "current.md"
    lines = ["# KPI Anomaly Alerts", ""]
    if not findings:
        lines += ["[ok] No anomalies detected.", ""]
    else:
        lines += [
            f"[x] {len(findings)} anomaly(ies) detected.",
            "",
            "| KPI | Current | Median | MAD distance |",
            "|---|---|---|---|",
        ]
        for f in findings:
            lines.append(
                f"| `{f['kpi_id']}` | {f['current']:.2f} | {f['median']:.2f} | "
                f"{f['distance']:.2f} (threshold {f['mad_threshold']:.1f}) |"
            )
        lines.append("")
    report_path.write_text("\n".join(lines), encoding="utf-8")

    if findings and webhook_url:
        _post_webhook(findings, webhook_url, http=http)
    return report_path.as_posix()


def _post_webhook(findings: list, webhook_url: str, *, http: Any = None) -> None:
    try:
        if http is None:
            import urllib.request as http
        text = f"{len(findings)} KPI anomaly(ies): " + ", ".join(f["kpi_id"] for f in findings)
        req = http.Request(
            webhook_url,
            data=json.dumps({"text": text, "findings": findings}).encode("utf-8"),
            headers={"Content-Type": "application/json"},
        )
        http.urlopen(req, timeout=10)
    except Exception:
        pass  # alerting must never break the pipeline it's alerting about


def _read_mad_threshold(layout: Any) -> float:
    """`kpi_alert_mad_threshold` workspace setting, defensively -- absent,
    unreadable, or non-numeric all fall back to the documented default."""
    try:
        value = layout.load_settings().get("kpi_alert_mad_threshold")
    except Exception:
        value = None
    try:
        return float(value) if value is not None else DEFAULT_MAD_THRESHOLD
    except (TypeError, ValueError):
        return DEFAULT_MAD_THRESHOLD


def _parse_markdown_table(markdown: str) -> tuple:
    """Header + data rows of a `render_markdown_table()` pipe-table. Returns
    ([], []) for anything that isn't one (no rows, error message, etc.)."""
    lines = [ln for ln in (markdown or "").splitlines() if ln.strip().startswith("|")]
    if len(lines) < 2:
        return [], []
    header = [c.strip() for c in lines[0].strip().strip("|").split("|")]
    body = [[c.strip() for c in ln.strip().strip("|").split("|")] for ln in lines[2:]]
    return header, body


def _current_headlines(payload: dict) -> dict:
    """One numeric headline per KPI from the just-written kpi_results
    packet, reusing the platform's own metric-aggregation rule
    (`core.dashboard.model.cuts.headline_agg`: sum/avg/max/min from the
    KPI's declared metric) so an average KPI is never summed into nonsense.
    Skips a KPI entirely when it failed or has no numeric result column --
    no numeric column means no anomaly check is possible for it, not a
    fabricated zero.
    """
    from core.dashboard.model.cuts import headline_agg

    headlines: dict = {}
    for entry in payload.get("kpis") or []:
        if not isinstance(entry, dict) or entry.get("status") != "ok":
            continue
        kpi_id = str(entry.get("kpi_id") or "")
        if not kpi_id:
            continue
        header, rows = _parse_markdown_table(str(entry.get("preview_markdown") or ""))
        if not header or not rows:
            continue
        numeric_col = None
        values: list = []
        for idx, col in enumerate(header):
            candidate: list = []
            ok = True
            for row in rows:
                cell = row[idx] if idx < len(row) else ""
                if cell == "":
                    continue
                try:
                    candidate.append(float(cell))
                except ValueError:
                    ok = False
                    break
            if ok and candidate:
                numeric_col = col
                values = candidate
                break
        if numeric_col is None:
            continue
        metric = str((entry.get("definition") or {}).get("metric") or "")
        agg = headline_agg(metric, numeric_col, "")
        reducer = {"sum": sum, "avg": statistics.fmean, "max": max, "min": min}.get(agg, sum)
        headlines[kpi_id] = float(reducer(values))
    return headlines


@anchored("check-kpi-anomalies")
def check_kpi_anomalies_main(argv: list = None) -> int:
    """Post-results DAG task entrypoint: read this run's KPI results + this
    workspace's headline history, check for anomalies, write the report
    (+ optional webhook), append this run to history. Never raises on a
    missing/malformed artifact -- absent evidence means no findings this
    run, not a crashed task."""
    from core.storage.workspace_layout import WorkspaceLayout

    parser = argparse.ArgumentParser(
        description="Post-results KPI headline anomaly check (MAD over trailing runs)."
    )
    parser.add_argument("--workspace", required=True)
    parser.add_argument("--repo-root", default=".")
    args = parser.parse_args(argv)

    repo_root = Path(args.repo_root).resolve()
    layout = WorkspaceLayout(project_root=(repo_root / args.workspace).resolve())

    results_path = layout.reports_dir / "kpi_results" / "current.json"
    try:
        payload = json.loads(results_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        payload = {}
    current = _current_headlines(payload) if isinstance(payload, dict) else {}

    history_path = layout.evidence_dir / "kpi_headline_history.json"
    try:
        history_records = json.loads(history_path.read_text(encoding="utf-8"))
        if not isinstance(history_records, list):
            history_records = []
    except (OSError, json.JSONDecodeError):
        history_records = []
    history = [r.get("headlines") or {} for r in history_records if isinstance(r, dict)]

    threshold = _read_mad_threshold(layout)
    findings = check_kpi_anomalies(history, current, mad_threshold=threshold)

    webhook_url = os.environ.get(_ALERT_WEBHOOK_ENV, "")
    report_path = write_kpi_alerts_report(findings, layout=layout, webhook_url=webhook_url)

    if current:
        history_records.append({"date": date.today().isoformat(), "headlines": current})
        history_records = history_records[-_MAX_HISTORY_RUNS:]
        history_path.parent.mkdir(parents=True, exist_ok=True)
        history_path.write_text(json.dumps(history_records, indent=2) + "\n", encoding="utf-8")

    print(report_path)
    return 0


if __name__ == "__main__":
    raise SystemExit(check_kpi_anomalies_main())


__all__ = [
    "DEFAULT_MAD_THRESHOLD",
    "check_kpi_anomalies",
    "write_kpi_alerts_report",
    "check_kpi_anomalies_main",
]
