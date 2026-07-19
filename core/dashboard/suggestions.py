"""Per-KPI improvement suggestions, emitted ONLY when the readings warrant one.

This is the "I noticed something" step: after a workspace's KPIs are validated,
look at each KPI against the row-grain data and surface optional, plain-English
suggestions -- never manufactured. A KPI with nothing worth saying gets no
suggestion. Each suggestion states the observation and the concrete benefit.

Workspace-agnostic: detectors read the actual gold result + the conformed
row-grain frame + generic dimension importance. No domain words, no favoured
columns. Writes ``interns/reports/kpi_suggestions/current.{json,md}``.

Detectors (each fires only on clear evidence):

- ``parameterize_scope`` -- the KPI hard-codes a dimension to ONE value that the
  data actually varies over (a column constant across every gold row, but with
  several distinct values in the source). Suggest making it a slicer/parameter.
- ``add_cut`` -- a low-cardinality dimension the KPI does NOT break down by
  explains a large share of the measure's variance (eta^2 above a threshold).
  Suggest adding it as a cut. Only for additive measures whose value can be
  reproduced from the row grain (so the claim is verifiable), never for shares
  or range-scoped KPIs we can't reconstruct.
"""
from __future__ import annotations
from core.observability.cost_ledger import anchored

import argparse
import json
import re
import sys
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import polars as pl

from core.dashboard.importance import rank_dimensions
from core.dashboard.model.conformed import build_conformed_model
from core.dashboard.model.cuts import build_kpi_model
from core.dashboard.model.layers import list_gold_kpis, read_gold
from core.paths import PROJECT_ROOT
from core.storage.workspace_layout import WorkspaceLayout

# A dimension is only a readable, comparable parameter/cut up to this many values.
_LOW_CARD = 40
_SPLIT_CARD = 12          # good "add a cut" candidates are small splits
_IMPORTANCE_MIN = 0.12    # eta^2: the cut must explain a real share of the measure


@dataclass
class Suggestion:
    kpi_id: str
    kpi_name: str
    kind: str
    text: str               # plain-English observation + the suggested change
    benefit: str            # what the user gains
    evidence: dict[str, Any] = field(default_factory=dict)


def _human(name: str) -> str:
    return re.sub(r"(?<=[a-z0-9])(?=[A-Z])", " ", str(name)).replace("_", " ").strip().title()


def _ci(name: str, cols: list[str]) -> str | None:
    if name in cols:
        return name
    low = name.lower()
    return next((c for c in cols if c.lower() == low), None)


def _equality_scope(km, gold: pl.DataFrame, conf: pl.DataFrame) -> dict[str, Any]:
    """Conformed columns the KPI pins to one value (constant across all gold rows)."""
    scope: dict[str, Any] = {}
    for c in gold.columns:
        if c == km.measure:
            continue
        uniq = gold.get_column(c).drop_nulls().unique()
        if uniq.len() == 1:
            cc = _ci(c, list(conf.columns))
            if cc is not None:
                scope[cc] = uniq.item()
    return scope


def _measure_column(km, gold, conf, model) -> tuple[str | None, dict[str, Any]]:
    """The conformed numeric column whose sum, under the KPI's equality scope,
    reproduces the validated gold at the KPI's leading cut -- or (None, scope)
    when the KPI can't be reconstructed from the row grain (share/ratio/range
    scope). Returning a column is the proof an ``add_cut`` claim is verifiable."""
    scope = _equality_scope(km, gold, conf)
    cuts = [c for c in (_ci(x, list(conf.columns)) for x in km.cuts) if c]
    lead = next((c for c in cuts if c not in scope), None)
    if lead is None:
        return None, scope
    gx = _ci(lead, list(gold.columns))
    if gx is None:
        return None, scope
    gref = gold.group_by(gx).agg(pl.col(km.measure).sum().alias("__m"))
    gmap = {r[gx]: round(float(r["__m"]), 2) for r in gref.iter_rows(named=True)
            if r["__m"] is not None}
    if not gmap:
        return None, scope
    sub = conf
    for k, v in scope.items():
        sub = sub.filter(pl.col(k) == v)
    for meas in model.measures.values():
        if (getattr(meas, "agg", "") or "").lower() != "sum" or not getattr(meas, "column", None):
            continue
        src = _ci(meas.column, list(conf.columns))
        if src is None:
            continue
        rec = sub.group_by(lead).agg(pl.col(src).sum().round(2).alias("__m"))
        rmap = {r[lead]: round(float(r["__m"]), 2) for r in rec.iter_rows(named=True)
                if r["__m"] is not None}
        if all(abs(rmap.get(k, float("inf")) - v) <= 0.01 for k, v in gmap.items()):
            return src, scope
    return None, scope


def _suggest_parameterize_scope(km, gold, conf) -> list[Suggestion]:
    out: list[Suggestion] = []
    for c in gold.columns:
        cl = c.lower()
        # Ids / dates are keys, not business parameters worth comparing across.
        if c == km.measure or cl.endswith("id") or "date" in cl or cl == "month":
            continue
        uniq = gold.get_column(c).drop_nulls().unique()
        if uniq.len() != 1:
            continue
        val = uniq.item()
        cc = _ci(c, list(conf.columns))
        if cc is None:
            continue
        col = conf.get_column(cc).drop_nulls()
        if col.dtype.is_numeric():
            continue
        nd = col.n_unique()
        if not (2 <= nd <= _LOW_CARD):
            continue
        others = [v for v in col.unique().to_list() if v != val][:4]
        out.append(Suggestion(
            kpi_id=km.kpi_id, kpi_name=km.title, kind="parameterize_scope",
            text=(f"This KPI is fixed to {_human(c)} = '{val}', but the data carries "
                  f"{nd} distinct {_human(c)} values (e.g. {', '.join(map(str, others))}). "
                  f"Consider making {_human(c)} a slicer/parameter instead of hard-coding it."),
            benefit=(f"One definition instead of {nd}: you could compare and trend "
                     f"{_human(km.card_label or km.measure)} across every {_human(c)} "
                     f"from a single KPI, rather than authoring one per value."),
            evidence={"column": c, "fixed_value": val, "distinct_in_data": nd},
        ))
    return out


def _suggest_add_cut(km, gold, conf, model) -> list[Suggestion]:
    src, scope = _measure_column(km, gold, conf, model)
    if src is None:                       # not reconstructable -> don't assert
        return []
    kpi_cut_cols = {(_ci(c, list(conf.columns)) or c) for c in km.cuts}
    cands: list[str] = []
    for d in conf.columns:
        dl = d.lower()
        if d in kpi_cut_cols or d in scope or d == src:
            continue
        if dl.endswith("id") or "date" in dl or dl == "month":
            continue
        s = conf.get_column(d)
        if s.dtype.is_numeric():
            continue
        if 2 <= s.n_unique() <= _SPLIT_CARD:
            cands.append(d)
    if not cands:
        return []
    sub = conf
    for k, v in scope.items():
        sub = sub.filter(pl.col(k) == v)
    ranked = rank_dimensions(sub, cands, measure=src, is_share=False)
    out: list[Suggestion] = []
    for dim, score in ranked[:1]:
        if score >= _IMPORTANCE_MIN:
            out.append(Suggestion(
                kpi_id=km.kpi_id, kpi_name=km.title, kind="add_cut",
                text=(f"{_human(km.card_label or km.measure)} varies substantially by "
                      f"{_human(dim)} -- it explains a large share of the differences in "
                      f"the data -- yet this KPI doesn't break down by {_human(dim)}."),
                benefit=(f"Adding {_human(dim)} as a cut surfaces a major driver of the "
                         f"metric you'd otherwise average over and miss."),
                evidence={"dimension": dim, "importance_eta2": round(float(score), 3)},
            ))
    return out


def generate_kpi_suggestions(layout: WorkspaceLayout) -> dict[str, Any]:
    """Build per-KPI suggestions and write the report. Returns a summary dict."""
    model = build_conformed_model(layout)
    report_dir = layout.reports_dir / "kpi_suggestions"
    report_dir.mkdir(parents=True, exist_ok=True)

    kpi_ids = list_gold_kpis(layout)
    per_kpi: list[tuple[str, str, list[Suggestion]]] = []
    if model is not None:
        conf = model.frame
        for kpi_id in kpi_ids:
            gold = read_gold(layout, kpi_id)
            if gold is None or gold.height == 0:
                continue
            km = build_kpi_model(layout, kpi_id, gold)
            sugg = _suggest_parameterize_scope(km, gold, conf)
            sugg += _suggest_add_cut(km, gold, conf, model)
            per_kpi.append((kpi_id, km.title, sugg))

    all_suggestions = [s for _, _, ss in per_kpi for s in ss]
    report = {
        "artifact_type": "kpi_suggestions/current.json",
        "version": 1,
        "workspace": layout.project_root.name,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "kpi_count": len(per_kpi),
        "suggestion_count": len(all_suggestions),
        "suggestions": [asdict(s) for s in all_suggestions],
    }
    (report_dir / "current.json").write_text(json.dumps(report, indent=2) + "\n",
                                             encoding="utf-8")

    lines = [
        "# KPI Suggestions",
        "",
        f"- Workspace: `{layout.project_root.name}`",
        f"- KPIs reviewed: {len(per_kpi)}  |  Suggestions: {len(all_suggestions)}",
        "",
        "Suggestions are optional and only shown where the readings warranted one. "
        "A KPI with nothing to add is listed as such -- nothing is forced.",
        "",
    ]
    for kpi_id, name, ss in per_kpi:
        lines.append(f"## {kpi_id} - {name}")
        if not ss:
            lines.append("- (no suggestion - the readings didn't warrant one)")
        for s in ss:
            lines.append(f"- [~] {s.text}")
            lines.append(f"      Benefit: {s.benefit}")
        lines.append("")
    (report_dir / "current.md").write_text("\n".join(lines), encoding="utf-8")

    return {
        "ok": True,
        "kpi_count": len(per_kpi),
        "suggestion_count": len(all_suggestions),
        "report_md": str((report_dir / "current.md")),
        "report_json": str((report_dir / "current.json")),
    }



@anchored("suggest-kpi-improvements")
def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="suggest-kpi-improvements")
    parser.add_argument("--workspace", required=True)
    parser.add_argument("--repo-root", default=str(PROJECT_ROOT))
    parser.add_argument("--json", action="store_true",
                        help="Print the machine-readable summary instead of human text.")
    args = parser.parse_args(argv)

    repo_root = Path(args.repo_root).resolve()
    workspace = (repo_root / args.workspace).resolve()
    if not workspace.exists():
        print(f"workspace not found: {workspace}", file=sys.stderr)
        return 2
    layout = WorkspaceLayout(project_root=workspace)
    summary = generate_kpi_suggestions(layout)
    if args.json:
        print(json.dumps(summary, indent=2))
    else:
        print(f"[ok] {summary['suggestion_count']} suggestion(s) across "
              f"{summary['kpi_count']} KPI(s)")
        print(f"report: {summary['report_md']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
