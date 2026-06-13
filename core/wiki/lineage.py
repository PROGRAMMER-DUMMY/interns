"""Derive KPI lineage, cross-links, and decision history from workspace contracts.

Reads the JSON contracts a completed KPI leaves behind and renders the
machine-owned wiki sections (Lineage / Decision history) plus the ``[[name]]``
cross-links. Workspace-agnostic: nothing here knows any domain vocabulary; all
names come from the contracts themselves.

Contracts consumed (best-effort; any missing file degrades gracefully):
- ``source_to_target_plan.json`` — per-KPI sources, join_plan, grain, filters.
- ``relationship_contracts.json`` — join provenance (approval.source / confirmed_by).
- ``pipeline_decisions.json``     — grain_bucketing + percentage_denominator scope.
"""
from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any


_CONTRACTS_REL = ("interns", "generated", "contracts")


def find_contracts_dir(start: Path | None) -> Path | None:
    """Walk up from ``start`` to locate the ``interns/generated/contracts`` dir."""
    if start is None:
        return None
    start = Path(start)
    candidates = [start] + list(start.parents)
    for base in candidates:
        candidate = base.joinpath(*_CONTRACTS_REL)
        if candidate.is_dir():
            return candidate
        # ``start`` may already point inside interns/.../solutions/<file>.sql
        if base.name == "contracts" and base.is_dir():
            return base
    return None


def _load(contracts_dir: Path | None, name: str) -> dict[str, Any]:
    if contracts_dir is None:
        return {}
    path = contracts_dir / name
    if not path.is_file():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return data if isinstance(data, dict) else {}


def _short(name: str) -> str:
    """Last path component without extension — e.g. dataset/table display name."""
    text = str(name or "").strip().replace("\\", "/")
    if not text:
        return ""
    tail = text.rsplit("/", 1)[-1]
    return tail.rsplit(".", 1)[0] if "." in tail else tail


def _wikilink(name: str) -> str:
    short = _short(name)
    return f"[[{short}]]" if short else ""


def _plan_for_kpi(plan: dict[str, Any], kpi_id: str) -> dict[str, Any]:
    for entry in plan.get("kpis") or []:
        if str(entry.get("kpi_id") or "") == kpi_id:
            return entry
    return {}


def collect_lineage(contracts_dir: Path | None, kpi_id: str) -> dict[str, Any]:
    """Pull the lineage facts for one KPI from the source-to-target plan."""
    plan = _load(contracts_dir, "source_to_target_plan.json")
    kpi = _plan_for_kpi(plan, kpi_id)
    join_plan = kpi.get("join_plan") or {}
    grain = kpi.get("grain") or {}

    datasets = [str(d) for d in (kpi.get("selected_source_datasets") or []) if d]
    joins = []
    for rel in join_plan.get("executable_relationships") or []:
        joins.append(
            {
                "relationship_id": str(rel.get("relationship_id") or ""),
                "left_dataset": str(rel.get("left_dataset") or ""),
                "left_column": str(rel.get("left_column") or ""),
                "right_dataset": str(rel.get("right_dataset") or ""),
                "right_column": str(rel.get("right_column") or ""),
                "state": str(rel.get("state") or ""),
            }
        )
    grain_dims = [str(d) for d in (grain.get("dimensions") or []) if d]
    return {
        "datasets": datasets,
        "joins": joins,
        "grain_description": str(grain.get("description") or "").strip(),
        "grain_dimensions": grain_dims,
        "filters": str(kpi.get("dimensions_and_filters") or "").strip(),
        "temporal_features": [
            str(f) for f in ((kpi.get("temporal_anchor") or {}).get("features") or []) if f
        ],
    }


def render_lineage_section(lineage: dict[str, Any]) -> str:
    datasets = lineage.get("datasets") or []
    joins = lineage.get("joins") or []
    grain_dims = lineage.get("grain_dimensions") or []
    grain_desc = lineage.get("grain_description") or ""
    filters = lineage.get("filters") or ""

    if not (datasets or joins or grain_dims or grain_desc or filters):
        return "_No lineage recorded._"

    lines: list[str] = []
    lines.append("**Datasets**")
    if datasets:
        for ds in datasets:
            lines.append(f"- {_wikilink(ds)} (`{_short(ds)}`)")
    else:
        lines.append("- _none recorded_")

    lines.append("")
    lines.append("**Joins**")
    if joins:
        for j in joins:
            left = _short(j["left_dataset"])
            right = _short(j["right_dataset"])
            rel = j["relationship_id"]
            state = f" [{j['state']}]" if j.get("state") else ""
            rel_link = f" via {_wikilink(rel)}" if rel else ""
            lines.append(
                f"- {_wikilink(j['left_dataset'])}.`{j['left_column']}` -> "
                f"{_wikilink(j['right_dataset'])}.`{j['right_column']}`"
                f"{rel_link}{state}"
            )
            # keep short table names visible even when slugs differ
            _ = (left, right)
    else:
        lines.append("- _single source; no joins_")

    lines.append("")
    lines.append("**Grain**")
    if grain_desc:
        lines.append(f"- {grain_desc}")
    for dim in grain_dims:
        lines.append(f"  - `{dim}`")
    if not grain_desc and not grain_dims:
        lines.append("- _not recorded_")

    lines.append("")
    lines.append("**Filters / cuts**")
    lines.append(f"- {filters}" if filters else "- _none recorded_")

    return "\n".join(lines)


def _provenance_phrase(approval: dict[str, Any]) -> str:
    source = str(approval.get("source") or "").strip()
    confirmed_by = str(approval.get("confirmed_by") or "").strip()
    state = str(approval.get("state") or "").strip()
    bits: list[str] = []
    if state:
        bits.append(f"state `{state}`")
    if source:
        bits.append(f"source `{source}`")
    if confirmed_by:
        bits.append(f"confirmed_by `{confirmed_by}`")
    elif source and source != "human":
        bits.append("confirmed_by `agent`")
    return ", ".join(bits)


def collect_decisions(
    contracts_dir: Path | None, kpi_id: str, lineage: dict[str, Any]
) -> list[str]:
    """Build decision-history bullets: grain bucketing, denominator, join approvals."""
    decisions = _load(contracts_dir, "pipeline_decisions.json")
    rel_contract = _load(contracts_dir, "relationship_contracts.json")

    bullets: list[str] = []

    bucketing = (decisions.get("grain_bucketing_decisions") or {}).get(kpi_id)
    if bucketing:
        reason = (decisions.get("grain_bucketing_reasons") or {}).get(kpi_id) or ""
        suffix = f" - {reason}" if reason else ""
        bullets.append(f"- Grain bucketing: `{bucketing}`{suffix}")

    scopes = decisions.get("percentage_denominator_scopes") or {}
    scope = scopes.get(kpi_id)
    if scope:
        bullets.append(f"- Denominator scope: `{scope}`")

    # Relationship approvals, restricted to the joins this KPI actually uses.
    used_ids = {j.get("relationship_id") for j in (lineage.get("joins") or [])}
    rels = rel_contract.get("relationships") or []
    by_id = {str(r.get("relationship_id") or ""): r for r in rels}
    for rel_id in sorted(rid for rid in used_ids if rid):
        rel = by_id.get(rel_id)
        if not rel:
            bullets.append(f"- Relationship {_wikilink(rel_id)}: state `unrecorded`")
            continue
        approval = rel.get("approval") or {}
        phrase = _provenance_phrase(approval)
        approved_at = str(approval.get("approved_at") or "").strip()
        when = f" at {approved_at}" if approved_at else ""
        detail = f" ({phrase}{when})" if phrase else ""
        bullets.append(f"- Relationship {_wikilink(rel_id)}: approval{detail}")

    return bullets


def render_decision_section(bullets: list[str]) -> str:
    if not bullets:
        return "_No machine decisions recorded._"
    return "\n".join(bullets)


def collect_related_links(lineage: dict[str, Any], feature_names: list[str]) -> list[str]:
    """Distinct ``[[name]]`` cross-links to datasets, relationships, and features."""
    seen: list[str] = []

    def _add(value: str) -> None:
        link = _wikilink(value)
        if link and link not in seen:
            seen.append(link)

    for ds in lineage.get("datasets") or []:
        _add(ds)
    for j in lineage.get("joins") or []:
        _add(j.get("relationship_id") or "")
    for feature in feature_names:
        _add(feature)
    return seen


_RESULTS_VIEW_RE = re.compile(
    r"CREATE\s+OR\s+REPLACE\s+VIEW\s+\"?(?P<view>[A-Za-z0-9_]+_results)\"?\s+AS"
    r"(?P<body>.*?);",
    re.DOTALL | re.IGNORECASE,
)


def extract_result_shaping(sql_text: str, kpi_id: str) -> str:
    """Return ONLY the ``<kpi>_results`` SELECT/GROUP BY/filter body, not full SQL.

    Falls back to the first ``*_results`` view in the script, or empty string.
    """
    text = str(sql_text or "")
    if not text.strip():
        return ""
    preferred = f"{kpi_id}_results"
    chosen = None
    for match in _RESULTS_VIEW_RE.finditer(text):
        view = match.group("view")
        body = match.group("body").strip()
        if view == preferred:
            chosen = (view, body)
            break
        if chosen is None:
            chosen = (view, body)
    if chosen is None:
        return ""
    view, body = chosen
    return f"-- result-shaping view: {view}\n{body}"
