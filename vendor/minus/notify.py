"""Alerts, subscriptions, and scheduled delivery.

Pure, dependency-light logic so it is testable and cron-friendly:
  * ``evaluate_alerts`` runs each configured threshold alert as a scalar query.
  * ``deliver`` dispatches messages to the configured channel
    (console / file / webhook / email).
  * ``snapshot`` exports every widget's data to CSV — the "subscription" payload.

The ``minus alerts`` / ``minus report`` CLI commands and an optional in-app
refresh interval drive these. No new dependencies (stdlib smtplib/urllib).
"""

from __future__ import annotations

import json
import math
import os
import smtplib
import urllib.request
from dataclasses import dataclass, field
from email.mime.text import MIMEText
from pathlib import Path

from minus.config.models import Alert, NotifyConfig, Project, Widget

_OPS = {
    "<": lambda a, b: a < b,
    "<=": lambda a, b: a <= b,
    ">": lambda a, b: a > b,
    ">=": lambda a, b: a >= b,
    "==": lambda a, b: a == b,
    "!=": lambda a, b: a != b,
}


@dataclass
class Firing:
    name: str
    measure: str
    actual: float
    op: str
    value: float
    message: str

    def line(self) -> str:
        return (f"[ALERT] {self.name}: {self.measure}={self.actual:,.2f} "
                f"{self.op} {self.value:,.2f} — {self.message}")


# ---------------------------------------------------------------------------
# Evaluation
# ---------------------------------------------------------------------------


def _scalar(engine, measure: str, filters: dict) -> float:
    """Evaluate a measure as a single KPI value under optional filters."""
    w = Widget(id="_alert", type="kpi", measure=measure)
    return engine.run(w, filters or {}).scalar


def evaluate_alerts(project: Project, engine) -> list[Firing]:
    """Return the alerts that are currently firing."""
    fired: list[Firing] = []
    for a in project.alerts:
        try:
            actual = _scalar(engine, a.measure, a.filters)
        except Exception as exc:  # a bad alert must not break the others
            fired.append(Firing(a.name, a.measure, math.nan, a.op, a.value,
                                f"(could not evaluate: {exc})"))
            continue
        if actual is None or (isinstance(actual, float) and math.isnan(actual)):
            continue
        if _OPS[a.op](actual, a.value):
            msg = a.message or f"{a.measure} {a.op} {a.value}"
            fired.append(Firing(a.name, a.measure, float(actual), a.op, a.value, msg))
    return fired


# ---------------------------------------------------------------------------
# Delivery
# ---------------------------------------------------------------------------


def deliver(notify: NotifyConfig, lines: list[str], root: Path | str = ".") -> str:
    """Send messages over the configured channel. Returns a status string."""
    if not lines:
        return "nothing to deliver"
    body = "\n".join(lines)
    ch = notify.channel
    if ch == "console":
        print(body)
        return f"printed {len(lines)} message(s)"
    if ch == "file":
        path = Path(root) / (notify.file or "alerts.log")
        with path.open("a", encoding="utf-8") as fh:
            fh.write(body + "\n")
        return f"appended {len(lines)} message(s) to {path}"
    if ch == "webhook":
        if not notify.webhook:
            return "webhook channel set but no 'webhook' url configured"
        req = urllib.request.Request(
            notify.webhook, data=json.dumps({"text": body}).encode(),
            headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=15) as resp:  # noqa: S310
            return f"POSTed to webhook (HTTP {resp.status})"
    if ch == "email":
        return _send_email(notify.email or {}, body)
    return f"unknown channel {ch!r}"


def _send_email(cfg: dict, body: str) -> str:
    host = cfg.get("host")
    recipients = cfg.get("recipients") or []
    if not host or not recipients:
        return "email channel set but host/recipients missing"
    msg = MIMEText(body)
    msg["Subject"] = "MinusAnalyst alerts"
    msg["From"] = cfg.get("sender", "minusanalyst@localhost")
    msg["To"] = ", ".join(recipients)
    password = os.environ.get(cfg.get("password_env", ""), "")
    with smtplib.SMTP(host, int(cfg.get("port", 587)), timeout=20) as s:
        if cfg.get("user"):
            s.starttls()
            s.login(cfg["user"], password)
        s.sendmail(msg["From"], recipients, msg.as_string())
    return f"emailed {len(recipients)} recipient(s)"


# ---------------------------------------------------------------------------
# Subscriptions / snapshot export
# ---------------------------------------------------------------------------


def snapshot(project: Project, pages, engine, out_dir: Path | str) -> list[Path]:
    """Write each widget's data to a CSV under ``out_dir``. Returns paths written."""
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    written: list[Path] = []
    for page in pages:
        for w in page.widgets:
            try:
                res = engine.run(w, {})
            except Exception:
                continue
            frame = res.frame
            if frame is None or frame.is_empty():
                continue
            path = out / f"{page.id}__{w.id}.csv"
            frame.write_csv(path)
            written.append(path)
    return written
