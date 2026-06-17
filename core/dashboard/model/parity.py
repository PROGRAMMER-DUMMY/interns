"""Parity gate: prove the live engine reproduces the validated gold layer.

The dashboard's promise is that re-aggregation never contradicts the KPI result
packet. We lock that with two checks per KPI:

1. Identity: re-aggregating gold at its full cut grain (with no filter) returns
   gold unchanged -- same rows, same measure values. This proves the additive /
   non-additive path is wired correctly for that KPI's measure kind.
2. Total preservation (additive only): the scalar total of the measure equals the
   sum of the gold measure column -- i.e. rolling all the way up loses nothing.

A failure here is a build blocker, consistent with the KPI completion gates.
"""
from __future__ import annotations

from dataclasses import asdict
from typing import Any

import polars as pl

from core.dashboard.model.aggregate import NonAdditiveError, aggregate, resolve_column
from core.dashboard.model.cuts import build_kpi_model
from core.dashboard.model.layers import list_gold_kpis, read_gold
from core.storage.workspace_layout import WorkspaceLayout

_TOL = 1e-6


def _frames_match(
    got: pl.DataFrame, want: pl.DataFrame, keys: list[str], measure: str
) -> tuple[bool, str]:
    if got.height != want.height:
        return False, f"row count {got.height} != gold {want.height}"
    if not keys:
        # scalar comparison
        gv = got.get_column(measure).to_list()
        wv = want.get_column(measure).sum()
        return (abs((gv[0] if gv else 0) - wv) <= _TOL * max(1.0, abs(wv)),
                "scalar")
    a = got.sort(keys)
    b = want.sort(keys)
    for k in keys:
        if a.get_column(k).to_list() != b.get_column(k).to_list():
            return False, f"key column {k!r} differs after sort"
    av = a.get_column(measure).to_list()
    bv = b.get_column(measure).to_list()
    for x, y in zip(av, bv):
        if x is None and y is None:
            continue
        if x is None or y is None:
            return False, "null mismatch in measure"
        if abs(x - y) > _TOL * max(1.0, abs(y)):
            return False, f"measure {x} != gold {y}"
    return True, "ok"


def check_parity(layout: WorkspaceLayout) -> list[dict[str, Any]]:
    """Run the parity checks for every KPI with a gold table."""
    results: list[dict[str, Any]] = []
    for kpi_id in list_gold_kpis(layout):
        gold = read_gold(layout, kpi_id)
        if gold is None or gold.height == 0:
            results.append({"kpi_id": kpi_id, "ok": False, "reason": "gold missing/empty"})
            continue
        model = build_kpi_model(layout, kpi_id, gold)
        entry: dict[str, Any] = {
            "kpi_id": kpi_id,
            "measure": model.measure,
            "kind": model.kind,
            "additive": model.additive,
            "cuts": model.cuts,
            "gold_rows": gold.height,
            "ok": True,
            "checks": {},
        }
        # 1. identity at full cut grain
        try:
            regrouped = aggregate(
                gold, measure=model.measure, additive=model.additive,
                group_by=model.cuts,
            )
            ok, detail = _frames_match(regrouped, gold, model.cuts, model.measure)
        except NonAdditiveError as exc:
            ok, detail = False, f"non-additive: {exc}"
        entry["checks"]["identity"] = {"ok": ok, "detail": detail}
        entry["ok"] = entry["ok"] and ok

        # 2. total preservation (additive only)
        if model.additive:
            try:
                total = aggregate(
                    gold, measure=model.measure, additive=True, group_by=[],
                )
                want = gold.get_column(resolve_column(model.measure, gold.columns)).sum()
                got = total.get_column(model.measure).to_list()
                tok = abs((got[0] if got else 0) - want) <= _TOL * max(1.0, abs(want))
                entry["checks"]["total"] = {"ok": tok, "got": got[0] if got else None, "want": want}
                entry["ok"] = entry["ok"] and tok
            except Exception as exc:  # noqa: BLE001 - report, don't crash the gate
                entry["checks"]["total"] = {"ok": False, "detail": str(exc)}
                entry["ok"] = False
        results.append(entry)
    return results


def parity_report(layout: WorkspaceLayout) -> str:
    """Human-readable parity summary (ASCII markers, no emojis)."""
    rows = check_parity(layout)
    if not rows:
        return "[x] no gold KPI tables found"
    lines = []
    for r in rows:
        marker = "[ok]" if r.get("ok") else "[x]"
        if "reason" in r:
            lines.append(f"{marker} {r['kpi_id']}: {r['reason']}")
            continue
        lines.append(
            f"{marker} {r['kpi_id']}: measure={r['measure']} kind={r['kind']} "
            f"additive={r['additive']} rows={r['gold_rows']} "
            f"checks={ {k: v.get('ok') for k, v in r['checks'].items()} }"
        )
    passed = sum(1 for r in rows if r.get("ok"))
    lines.append(f"--- {passed}/{len(rows)} KPIs pass parity ---")
    return "\n".join(lines)


__all__ = ["check_parity", "parity_report"]
