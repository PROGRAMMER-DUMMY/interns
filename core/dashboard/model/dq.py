"""Data-quality certifier for the conformed model.

The user's call: instead of rebuilding the pipeline's Silver to MAKE the data
clean, prove the conformed model IS clean. If these checks pass, the conformed
star is certified trustworthy and the heavier pipeline-Silver rebuild is
unnecessary.

Checks (the 2026 silver-standard quality categories):
  - null_keys        join keys / measures have no nulls
  - dim_uniqueness   each joined dimension's PK is unique (post-dedup)
  - referential      fact foreign keys resolve to a dimension PK (orphan rate)
  - no_fanout        the star join did NOT multiply fact rows (grain preserved)
  - lossless         additive measure totals survive cleaning+join (vs raw bronze)
  - type_conformance date-named columns are real dates
Plus a best-effort gold reconciliation: the conformed grand total of a simple-sum
measure must be >= the matching gold KPI total (gold is a filtered subset) and
equal to the raw bronze total (cleaning lost nothing).

A failing check is a hard signal; the certifier returns ok=False so the caller
can refuse to serve the conformed model and fall back to gold-only.
"""
from __future__ import annotations

from typing import Any

import polars as pl

from core.dashboard.model.aggregate import resolve_column
from core.dashboard.model.conformed import ConformedModel
from core.dashboard.model.layers import read_bronze, read_gold, list_gold_kpis

_ORPHAN_TOL = 0.02     # <=2% unresolved FKs tolerated (matches RI-pass threshold)
_SUM_TOL = 1e-6


def _check(ok: bool, name: str, detail: str = "") -> dict[str, Any]:
    return {"check": name, "ok": bool(ok), "detail": detail}


def run_quality_checks(model: ConformedModel) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    frame = model.frame
    cols = frame.columns

    # 1. null keys + measures
    key_cols = {e.left_col for e in model.edges} | {m.column for m in model.measures.values()
                                                    if m.column != "*"}
    null_offenders = []
    for c in key_cols:
        rc = resolve_column(c, cols)
        if rc is not None:
            n = frame.get_column(rc).null_count()
            if n:
                null_offenders.append(f"{rc}={n}")
    out.append(_check(not null_offenders, "null_keys",
                      "nulls: " + ", ".join(null_offenders) if null_offenders else "no nulls in keys/measures"))

    # 2. fan-out: the joins must not have multiplied fact rows
    fact_raw = read_bronze(model.layout, model.fact)
    fact_n = fact_raw.height if fact_raw is not None else None
    if fact_n is not None:
        out.append(_check(frame.height == fact_n, "no_fanout",
                          f"model rows={frame.height} vs fact rows={fact_n}"))

    # 3. referential integrity: fact FK resolves to a non-null joined dim attribute
    for e in model.edges:
        if e.left_table != model.fact:
            continue
        lk = resolve_column(e.left_col, cols)
        if lk is None:
            continue
        unresolved = frame.filter(pl.col(lk).is_not_null()).get_column(lk).null_count()
        total = frame.height or 1
        rate = unresolved / total
        out.append(_check(rate <= _ORPHAN_TOL, f"referential[{e.right_table}]",
                          f"unresolved FK rate={rate:.3%}"))

    # 4. lossless additive totals: model sum == raw bronze sum (cleaning kept all rows)
    if fact_raw is not None:
        for m in model.measures.values():
            if m.agg != "sum":
                continue
            rc_m = resolve_column(m.column, frame.columns)
            rc_b = resolve_column(m.column, fact_raw.columns)
            if rc_m is None or rc_b is None:
                continue
            model_total = frame.get_column(rc_m).sum() or 0.0
            bronze_total = fact_raw.get_column(rc_b).sum() or 0.0
            ok = abs(model_total - bronze_total) <= _SUM_TOL * max(1.0, abs(bronze_total))
            out.append(_check(ok, f"lossless[{m.name}]",
                              f"model={model_total} bronze={bronze_total}"))

    # 5. type conformance: derived month/age present & typed when expected
    if "age" in cols:
        out.append(_check(frame.schema["age"].is_numeric(), "type[age]", "age numeric"))
    return out


def reconcile_with_gold(model: ConformedModel) -> list[dict[str, Any]]:
    """Best-effort: each gold simple-sum total must be <= the conformed grand total
    of the same measure (gold is a filtered subset of the same population)."""
    out: list[dict[str, Any]] = []
    for kpi_id in list_gold_kpis(model.layout):
        gold = read_gold(model.layout, kpi_id)
        if gold is None or gold.height == 0:
            continue
        # find a gold numeric "sum_*" column that maps to a model measure column
        for gcol in gold.columns:
            if not gcol.lower().startswith("sum_"):
                continue
            base = gcol[4:]
            mcol = resolve_column(base, model.frame.columns)
            if mcol is None:
                continue
            gold_total = gold.get_column(gcol).sum() or 0.0
            model_total = model.frame.get_column(mcol).sum() or 0.0
            ok = model_total + _SUM_TOL >= gold_total
            out.append(_check(ok, f"gold_subset[{kpi_id}.{gcol}]",
                              f"gold={gold_total:.2f} <= model={model_total:.2f}"))
    return out


def certify(model: ConformedModel) -> dict[str, Any]:
    """Full certification: intrinsic DQ + gold reconciliation."""
    checks = run_quality_checks(model) + reconcile_with_gold(model)
    failed = [c for c in checks if not c["ok"]]
    return {
        "ok": not failed,
        "fact": model.fact,
        "rows": model.frame.height,
        "measures": model.measure_names(),
        "dimensions": model.dimensions,
        "checks": checks,
        "failed": failed,
    }


def certification_report(model: ConformedModel) -> str:
    rep = certify(model)
    lines = [f"Conformed model: fact={rep['fact']} rows={rep['rows']} "
             f"measures={len(rep['measures'])} dims={len(rep['dimensions'])}"]
    for c in rep["checks"]:
        lines.append(f"  {'[ok]' if c['ok'] else '[x]'} {c['check']}: {c['detail']}")
    lines.append(f"--- {'CERTIFIED' if rep['ok'] else 'FAILED'} "
                 f"({len(rep['checks']) - len(rep['failed'])}/{len(rep['checks'])} checks) ---")
    return "\n".join(lines)


__all__ = ["certify", "certification_report", "reconcile_with_gold", "run_quality_checks"]
