"""Shared KPI registry / feature-mapping loader.

Single source of truth for "what does this workspace's KPI registry say
about KPI X?". Avoid re-implementing this load+normalize logic in every
caller (flow.py, medallion/build.py, etc.).
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from core.storage.workspace_layout import WorkspaceLayout


def _read_json(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return {}
    return data if isinstance(data, dict) else {}


def load_kpi_definitions(layout: WorkspaceLayout) -> dict[str, dict[str, Any]]:
    """Return a dict keyed by `kpi_id` of normalized KPI definitions.

    Prefer `kpi_feature_mapping.json` because it carries resolved features;
    fall back to `kpi_registry.json` for the raw definition. Returns an
    empty dict if neither file exists.

    The returned shape is the platform-wide KPI contract — never include
    workspace-specific fields here.
    """
    mapping = _read_json(layout.kpi_feature_mapping_path)
    registry = _read_json(layout.kpi_registry_path)
    source_kpis = mapping.get("kpis") or registry.get("kpis") or []
    by_id: dict[str, dict[str, Any]] = {}
    for idx, kpi in enumerate(source_kpis, start=1):
        if not isinstance(kpi, dict):
            continue
        kpi_id = str(kpi.get("kpi_id") or f"kpi_{idx:03d}")
        by_id[kpi_id] = {
            "kpi_id": kpi_id,
            "name": kpi.get("name") or "",
            "business_question": kpi.get("business_question") or kpi.get("name") or "",
            "description": kpi.get("description") or "",
            "metric": kpi.get("metric") or "",
            "cuts": kpi.get("cuts") or "",
            "filters": kpi.get("filters") or "",
            "status": kpi.get("status") or "",
            "features": kpi.get("features") or [],
        }
    return by_id


def render_kpi_block(entry: dict[str, Any], *, heading_level: int = 3) -> list[str]:
    """Return markdown lines for a single KPI's definition + SQL + result table.

    Generic across workspaces. `entry` must carry at least `kpi_id`; optional
    keys are `definition`, `sql_text`, `sql_path`, `status`, `error`,
    `preview_markdown`. Used by both the panel renderer and the standalone
    results renderer to avoid drift.
    """
    definition = entry.get("definition") or {}
    kpi_id = entry.get("kpi_id", "")
    heading_prefix = "#" * max(1, min(6, heading_level))
    sub_prefix = "#" * max(1, min(6, heading_level + 1))
    lines: list[str] = [f"{heading_prefix} {kpi_id}", ""]

    question = str(definition.get("business_question") or definition.get("name") or "").strip()
    description = str(definition.get("description") or "").strip()
    metric = str(definition.get("metric") or "").strip()
    cuts = str(definition.get("cuts") or "").strip()

    if question:
        lines.append(f"**Business question:** {question}")
    if description and description != question:
        lines.append(f"**Description:** {description}")
    if metric:
        lines.append(f"**Metric:** `{metric}`")
    if cuts:
        lines.append(f"**Cuts/grain:** {cuts}")
    if any([question, description, metric, cuts]):
        lines.append("")

    if entry.get("sql_path"):
        lines.append(f"- SQL: `{entry.get('sql_path')}`")
    if entry.get("status"):
        lines.append(f"- Status: `{entry.get('status')}`")
    lines.append("")

    sql_text = str(entry.get("sql_text") or "").strip()
    if sql_text:
        lines.append(f"{sub_prefix} Generated SQL")
        lines.append("")
        lines.append("```sql")
        lines.append(sql_text)
        lines.append("```")
        lines.append("")

    if entry.get("error"):
        lines.append(f"{sub_prefix} Error")
        lines.append("")
        lines.append("```text")
        lines.append(str(entry["error"]))
        lines.append("```")
        lines.append("")
    elif entry.get("preview_markdown"):
        lines.append(f"{sub_prefix} Result Preview")
        lines.append("")
        lines.append(str(entry.get("preview_markdown")))
        lines.append("")
    return lines


__all__ = ["load_kpi_definitions", "render_kpi_block"]
