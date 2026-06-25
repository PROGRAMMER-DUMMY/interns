"""Workspace advisor: read a generated MinusAnalyst project and/or scan a data
directory, then produce a data-engineering system-design review grounded in the
6-step framework.

The advisor never fabricates numbers. It reports what it can observe (row counts,
measures, pages from a generated project; file formats and footprint from a scan)
and, where a fact is unknown, it says so and asks the question the framework would
ask instead of guessing. Output is a :class:`DataOpsReview` -- ``report.py`` renders
it to Markdown / JSON.

Reading a generated project reuses ``minus.export.DashboardExport`` (the same loader
the live app and the deck/PDF exporters use), so the advisor stays a pure vendor:
it depends only on its sibling ``minus`` package, never on ``core``.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

from minus_dataops import decisions as D
from minus_dataops import volume as V
from minus_dataops.framework import FRAMEWORK, Step


@dataclass
class Finding:
    step: str                       # framework step key
    title: str
    observed: list[str] = field(default_factory=list)   # evidence
    recommendations: list[str] = field(default_factory=list)
    questions: list[str] = field(default_factory=list)   # what to clarify
    decisions: list[D.Decision] = field(default_factory=list)
    at_scale: list[str] = field(default_factory=list)    # deep: what breaks at TB/PB
    pitfalls: list[str] = field(default_factory=list)    # deep: anti-patterns to avoid


@dataclass
class DataOpsReview:
    subject: str
    evidence: dict[str, Any]
    findings: list[Finding]
    volume: Optional[V.VolumeEstimate] = None

    def as_dict(self) -> dict:
        return {
            "subject": self.subject,
            "evidence": self.evidence,
            "volume": self.volume.as_dict() if self.volume else None,
            "findings": [
                {
                    "step": f.step,
                    "title": f.title,
                    "observed": f.observed,
                    "recommendations": f.recommendations,
                    "questions": f.questions,
                    "decisions": [d.__dict__ for d in f.decisions],
                    "at_scale": f.at_scale,
                    "pitfalls": f.pitfalls,
                }
                for f in self.findings
            ],
        }


# ---------------------------------------------------------------------------
# Project loading (optional -- only if a generated minus root is given)
# ---------------------------------------------------------------------------


def _load_project_meta(root: Optional[Path]) -> dict[str, Any]:
    if not root:
        return {}
    try:
        from minus.export import DashboardExport
    except Exception as exc:  # minus not importable -> stay graceful
        return {"project_error": f"could not import minus.export: {exc}"}
    if not (root / "project.yaml").exists():
        return {"project_error": f"no project.yaml under {root}"}
    try:
        exp = DashboardExport(root)
        meta = exp.meta()
        # table inventory (names + row counts where cheap to get)
        tables = []
        for t in exp.project.tables:
            tables.append(t.name)
        meta["tables"] = tables
        return meta
    except Exception as exc:
        return {"project_error": f"failed to load project at {root}: {exc}"}


# ---------------------------------------------------------------------------
# Per-step assessors
# ---------------------------------------------------------------------------


def _f(step: Step) -> Finding:
    return Finding(step=step.key, title=f"Step {step.num}: {step.title}")


def _assess_requirements(meta: dict, est: Optional[V.VolumeEstimate],
                         latency: Optional[str]) -> Finding:
    f = _f(FRAMEWORK[0])
    rows = meta.get("rows")
    if rows is not None:
        f.observed.append(f"Generated fact table carries ~{rows:,} rows.")
    if meta.get("measures") is not None:
        f.observed.append(f"{meta['measures']} measures across {meta.get('pages', '?')} "
                          "dashboard page(s) -- consumers are BI/analyst-facing.")
    if est and est.bytes_retained:
        impl = V.implications(est)
        f.observed.append(f"On-disk data footprint ~{est.human.get('footprint', '?')} "
                          f"({impl['scale']}).")
        f.decisions.append(impl["engine"])
    else:
        f.questions.append("What is the daily data volume (GB/TB/PB)? Needed to pick "
                           "single-node vs distributed compute.")
    if latency:
        f.decisions.append(D.batch_vs_stream(latency=latency))
    else:
        f.questions.append("What is the latency SLA (seconds/minutes -> streaming, "
                           "hours/days -> batch)?")
    f.questions.append("Who is the end user (SQL analyst / ML feature store / real-time "
                       "product), and what is the data retention policy?")
    f.recommendations.append("Do the back-of-the-envelope math (DAU x sessions x events x "
                             "payload) and let that number justify the engine + track.")
    return f


def _assess_pipeline(latency: Optional[str]) -> Finding:
    f = _f(FRAMEWORK[1])
    dec = D.batch_vs_stream(latency=latency)
    f.decisions.append(dec)
    if dec.confidence == "low":
        f.questions.append("Confirm the freshness SLA before fixing the pipeline track.")
    f.recommendations.append("Land raw extracts in object storage, then move progressively "
                             "through layers; pick an orchestrator (Airflow/Prefect/Dagster) "
                             "for batch DAGs.")
    return f


def _assess_modeling(meta: dict) -> Finding:
    f = _f(FRAMEWORK[2])
    tables = meta.get("tables") or []
    if tables:
        f.observed.append(f"{len(tables)} modelled table(s): {', '.join(tables[:8])}"
                          + (" ..." if len(tables) > 8 else "") + ".")
        f.observed.append("Dashboard-serving layer present -> this is the Gold tier; "
                          "ensure cleaned row-grain facts/dimensions exist at Silver "
                          "underneath it.")
    f.decisions.append(D.modeling_shape(predictable_dashboard=True))
    f.decisions.append(D.scd_type())
    f.recommendations.append("Bronze (raw append) -> Silver (clean, de-dup, type-cast, "
                             "conform) -> Gold (pre-aggregated for BI). Partition by event "
                             "date to avoid full scans.")
    f.questions.append("Do any dimensions need history (SCD2) vs current-only (SCD1)?")
    return f


def _assess_storage(est: Optional[V.VolumeEstimate]) -> Finding:
    f = _f(FRAMEWORK[3])
    detected_table_fmt: list[str] = []
    if est and est.method == "file-scan":
        formats = est.detail.get("formats") or {}
        if formats:
            top = ", ".join(f"{k} ({v['files']} files, {v['bytes']})"
                            for k, v in list(formats.items())[:5])
            f.observed.append(f"Formats on disk: {top}.")
        detected_table_fmt = est.detail.get("table_formats_detected") or []
        if detected_table_fmt:
            f.observed.append(f"Open table format detected: {', '.join(detected_table_fmt)}.")
    f.decisions.append(D.storage_format("analytics"))
    f.decisions.append(D.table_format(streaming_small_files=None,
                                      time_travel=None if detected_table_fmt else None))
    if not detected_table_fmt:
        f.recommendations.append("If raw Parquet is being dumped to object storage, adopt an "
                                 "open table format (Delta/Iceberg/Hudi) for ACID, schema "
                                 "enforcement, time travel, and OPTIMIZE compaction.")
    return f


def _assess_quality(meta: dict) -> Finding:
    f = _f(FRAMEWORK[4])
    rows = meta.get("rows")
    if rows is not None:
        f.observed.append("A serving model exists, so quality must be enforced before it "
                          "feeds the dashboard.")
    f.recommendations.append("Add explicit checks across the five DQ dimensions: "
                             "Completeness (row-count drops), Accuracy (cross-field sanity "
                             "e.g. delivery >= order time), Consistency (totals match source "
                             "systems), Freshness (vs SLA), Uniqueness (no dupes).")
    f.recommendations.append("Shift quality left with data contracts (schema registry / "
                             "Great Expectations) so broken schemas are blocked before the "
                             "lake; wire DAG/runtime observability (native or DataDog/"
                             "CloudWatch).")
    return f


def _assess_scalability() -> Finding:
    f = _f(FRAMEWORK[5])
    f.recommendations.append("Make every incremental load idempotent: MERGE (upsert), not "
                             "flat INSERT, so reruns don't duplicate.")
    f.recommendations.append("Plan backfills as atomic partition overwrites (Delta/Iceberg) "
                             "so months of history can be reprocessed cleanly on a bug or "
                             "definition change.")
    f.recommendations.append("Schema evolution: flexible at Bronze (mergeSchema captures new "
                             "columns), strict at Silver/Gold to protect downstream "
                             "dashboards and ML.")
    return f


# ---------------------------------------------------------------------------
# Top-level entry
# ---------------------------------------------------------------------------


def _enrich_with_depth(findings: list[Finding], *, max_pitfalls: int = 4) -> None:
    """Attach each step's deep at-scale notes + top pitfalls from the depth layer.

    Done in-place. Tolerant of a missing dossier (the depth registry returns None),
    so an absent module just leaves the finding as its base form.
    """
    from minus_dataops import depth
    for f in findings:
        deep = depth.get_deep(f.step)
        if deep is None:
            continue
        f.at_scale = list(deep.at_scale)
        f.pitfalls = list(deep.pitfalls[:max_pitfalls])


def review(root: Optional[Path | str] = None,
           scan_path: Optional[Path | str] = None,
           *,
           latency: Optional[str] = None,
           subject: Optional[str] = None,
           deep: bool = False) -> DataOpsReview:
    """Produce a full 6-step system-design review.

    ``root``      -- a generated MinusAnalyst project root (project.yaml). Optional.
    ``scan_path`` -- any directory to estimate data volume/formats from. Optional.
    ``latency``   -- known freshness SLA (e.g. ``"5m"``, ``"daily"``), if any.
    ``deep``      -- when True, enrich each step with the deep layer's
                     what-breaks-at-scale notes and top pitfalls (honest TB/PB-scale
                     guidance), not just the base recommendations.

    With neither ``root`` nor ``scan_path`` the review is still useful: it becomes
    the framework checklist with the questions you must answer.
    """
    root_p = Path(root).resolve() if root else None
    scan_p = Path(scan_path).resolve() if scan_path else None

    meta = _load_project_meta(root_p)
    est: Optional[V.VolumeEstimate] = None
    if scan_p:
        est = V.estimate_from_path(scan_p)

    subj = subject or meta.get("name") or (scan_p.name if scan_p else
                                           root_p.name if root_p else "unscoped design")

    findings = [
        _assess_requirements(meta, est, latency),
        _assess_pipeline(latency),
        _assess_modeling(meta),
        _assess_storage(est),
        _assess_quality(meta),
        _assess_scalability(),
    ]
    if deep:
        _enrich_with_depth(findings)

    evidence = {
        "project_root": str(root_p) if root_p else None,
        "scan_path": str(scan_p) if scan_p else None,
        "latency_sla": latency,
        **{k: v for k, v in meta.items() if k != "tables"},
        "table_count": len(meta.get("tables") or []) if meta else 0,
    }
    return DataOpsReview(subject=subj, evidence=evidence, findings=findings, volume=est)


__all__ = ["Finding", "DataOpsReview", "review"]
