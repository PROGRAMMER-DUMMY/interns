"""Render a :class:`~minus_dataops.assess.DataOpsReview` to Markdown or JSON.

Output uses ASCII status markers ([ok] / [~] / [x]), never emojis, to match the
rest of the platform's generated reports.
"""

from __future__ import annotations

import json

from minus_dataops.assess import DataOpsReview
from minus_dataops.decisions import Decision

_CONF_MARK = {"high": "[ok]", "medium": "[~]", "low": "[x]"}


def _decision_md(d: Decision) -> list[str]:
    mark = _CONF_MARK.get(d.confidence, "[~]")
    lines = [f"  - {mark} **{d.question}** -> {d.recommendation}  _(confidence: {d.confidence})_",
             f"    - {d.rationale}"]
    if d.tradeoffs:
        for t in d.tradeoffs:
            lines.append(f"    - trade-off: {t}")
    return lines


def to_markdown(review: DataOpsReview) -> str:
    out: list[str] = []
    out.append(f"# DataOps system-design review: {review.subject}")
    out.append("")
    out.append("_Assessed against the 6-step data-engineering framework. "
               "Recommendations are evidence-gated; open questions are listed where a "
               "fact could not be observed._")
    out.append("")

    # Evidence block
    ev = review.evidence
    out.append("## Evidence")
    out.append("")
    for label, key in (("Project root", "project_root"), ("Scan path", "scan_path"),
                       ("Latency SLA", "latency_sla"), ("Rows", "rows"),
                       ("Measures", "measures"), ("Pages", "pages"),
                       ("Tables", "table_count"), ("Data through", "data_through")):
        if ev.get(key) not in (None, 0, ""):
            out.append(f"- {label}: {ev[key]}")
    if review.volume and review.volume.method != "unknown":
        v = review.volume
        out.append(f"- Volume ({v.method}): " +
                   ", ".join(f"{k}={val}" for k, val in v.human.items()))
        fmts = (v.detail or {}).get("formats") or {}
        if fmts:
            out.append("  - formats: " +
                       "; ".join(f"{k}: {val['files']} files / {val['bytes']}"
                                 for k, val in fmts.items()))
    if ev.get("project_error"):
        out.append(f"- [x] {ev['project_error']}")
    out.append("")

    # Findings per step
    for f in review.findings:
        out.append(f"## {f.title}")
        out.append("")
        if f.observed:
            out.append("**Observed**")
            for o in f.observed:
                out.append(f"- {o}")
            out.append("")
        if f.decisions:
            out.append("**Decisions**")
            for d in f.decisions:
                out.extend(_decision_md(d))
            out.append("")
        if f.recommendations:
            out.append("**Recommendations**")
            for r in f.recommendations:
                out.append(f"- {r}")
            out.append("")
        if f.pitfalls:
            out.append("**Pitfalls to avoid**")
            for p in f.pitfalls:
                out.append(f"- [x] {p}")
            out.append("")
        if f.at_scale:
            out.append("**What changes at TB/PB scale**")
            for a in f.at_scale:
                out.append(f"- ~ {a}")
            out.append("")
        if f.questions:
            out.append("**Open questions**")
            for q in f.questions:
                out.append(f"- [ ] {q}")
            out.append("")

    return "\n".join(out).rstrip() + "\n"


def to_json(review: DataOpsReview, *, indent: int = 2) -> str:
    return json.dumps(review.as_dict(), indent=indent, default=str)


__all__ = ["to_markdown", "to_json"]
