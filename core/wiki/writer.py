"""Upsert entity wiki notes, preserving human-owned sections across writes."""
from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import yaml

from core.wiki.layout import WikiLayout
from core.wiki.lineage import (
    collect_decisions,
    collect_lineage,
    collect_related_links,
    extract_result_shaping,
    find_contracts_dir,
    render_decision_section,
    render_lineage_section,
)
from core.wiki.reader import read_note
from core.wiki.template import ALL_SECTIONS, HUMAN_SECTIONS, empty_section


_FRONTMATTER_ORDER = (
    "contract_ref",
    "entity_type",
    "entity_id",
    "summary",
    "tags",
    "applies_to_kpis",
    "created",
    "updated",
    "last_validated_against_json",
    "validator_status",
    "related",
)


def build_feature_scaffold(
    *,
    feature: str,
    option: dict[str, Any],
    panel: dict[str, Any],
    custom_definition: str = "",
    evidence_note: str = "",
    contract_ref: str = "",
    validator_status: str = "ok",
    timestamp: str | None = None,
) -> dict[str, Any]:
    """Build the machine-owned scaffold for a feature note from panel apply inputs."""
    now = timestamp or datetime.now(timezone.utc).isoformat()
    option_id = str(option.get("option_id") or "")
    label = str(option.get("label") or "")
    applies_to_kpis = [str(k) for k in (panel.get("applies_to_kpis") or []) if k]
    summary = _summary_line(feature, option, custom_definition)

    rejected = [
        opt
        for opt in (panel.get("options") or [])
        if str(opt.get("option_id") or "") != option_id
    ]

    evidence_files = list(panel.get("evidence_files") or [])

    return {
        "frontmatter": {
            "contract_ref": contract_ref,
            "entity_type": "feature",
            "entity_id": feature,
            "summary": summary,
            "tags": ["feature"],
            "applies_to_kpis": applies_to_kpis,
            "updated": now,
            "last_validated_against_json": now,
            "validator_status": validator_status,
        },
        "current_state": _current_state_block(feature, option, custom_definition),
        "decision_entry": _decision_entry(
            now, option_id, label, option, custom_definition, evidence_note
        ),
        "evidence": evidence_files,
        "rejected_options": rejected,
    }


def upsert_feature_note(layout: WikiLayout, feature: str, scaffold: dict[str, Any]) -> Path:
    layout.ensure_dirs()
    path = layout.feature_note_path(feature)
    existing = read_note(path)
    now = scaffold["frontmatter"].get("updated") or datetime.now(timezone.utc).isoformat()

    if existing is None:
        frontmatter = dict(scaffold["frontmatter"])
        frontmatter.setdefault("created", now)
        sections = {
            "Current state": scaffold["current_state"],
            "Decision history": _render_history([scaffold["decision_entry"]]),
            "Evidence": _render_evidence(scaffold["evidence"]),
            "Rejected options": _render_rejected(scaffold["rejected_options"]),
        }
        for human in HUMAN_SECTIONS:
            sections[human] = empty_section(human)
    else:
        frontmatter = dict(existing.frontmatter)
        frontmatter.update(scaffold["frontmatter"])
        frontmatter.setdefault("created", now)
        history = _parse_history(existing.sections.get("Decision history", ""))
        history.append(scaffold["decision_entry"])
        sections = dict(existing.sections)
        sections["Current state"] = scaffold["current_state"]
        sections["Decision history"] = _render_history(history)
        sections["Evidence"] = _render_evidence(scaffold["evidence"])
        sections["Rejected options"] = _render_rejected(scaffold["rejected_options"])
        for human in HUMAN_SECTIONS:
            sections.setdefault(human, empty_section(human))

    rendered = _render_note(frontmatter, sections, feature)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(rendered, encoding="utf-8")
    return path


KPI_MACHINE_SECTIONS = (
    "Definition",
    "Current state",
    "Lineage",
    "Decision history",
    "Last execution",
)

KPI_HUMAN_SECTIONS = (
    "Why (user)",
    "Business context",
    "Related notes",
)


def build_kpi_completion_scaffold(
    *,
    kpi_id: str,
    entry: dict[str, Any],
    contract_ref: str = "",
    timestamp: str | None = None,
    project_root: Path | str | None = None,
) -> dict[str, Any]:
    """Build the machine-owned scaffold for a KPI completion note.

    ``entry`` is the same per-KPI dict produced by ``_write_result_preview``:
    keys ``definition`` (kpi_registry record), ``sql_path``, ``sql_text``,
    ``status``, ``preview_markdown``, ``error``, ``result_view``.

    W1/W2 behaviour (workspace-agnostic):
    - The full generated SQL is NOT inlined. The Definition section links to the
      ``.sql`` artifact; the Current state section shows only the
      ``<kpi>_results`` result-shaping body plus the small preview table.
    - Lineage / Decision history / cross-links are derived from the workspace
      contracts (``source_to_target_plan.json``, ``relationship_contracts.json``,
      ``pipeline_decisions.json``) located relative to ``project_root`` or, when
      not given, inferred from ``entry['sql_path']``.
    """
    now = timestamp or datetime.now(timezone.utc).isoformat()
    definition = entry.get("definition") or {}
    business_question = str(
        definition.get("business_question") or definition.get("name") or ""
    ).strip()
    metric = str(definition.get("metric") or "").strip()
    cuts = str(definition.get("cuts") or "").strip()
    description = str(definition.get("description") or "").strip()
    status = str(entry.get("status") or "")
    sql_path = str(entry.get("sql_path") or "").strip()

    # --- Locate workspace contracts (best-effort; degrade if absent). ---------
    contracts_dir = None
    if project_root is not None:
        contracts_dir = find_contracts_dir(Path(project_root))
    if contracts_dir is None and sql_path:
        contracts_dir = find_contracts_dir(Path(sql_path))

    lineage = collect_lineage(contracts_dir, kpi_id)
    feature_names = [
        str(fm.get("feature") or "")
        for fm in (definition.get("feature_mappings") or [])
        if fm.get("feature")
    ]
    decision_bullets = collect_decisions(contracts_dir, kpi_id, lineage)
    related_links = collect_related_links(lineage, feature_names)

    # --- Definition (W1: SQL link, not inlined SQL). --------------------------
    definition_lines = [f"- **KPI**: `{kpi_id}`"]
    if business_question:
        definition_lines.append(f"- **Business question**: {business_question}")
    if description and description != business_question:
        definition_lines.append(f"- **Description**: {description}")
    if metric:
        definition_lines.append(f"- **Metric**: `{metric}`")
    if cuts:
        definition_lines.append(f"- **Cuts/grain**: {cuts}")
    if sql_path:
        definition_lines.append(f"- **SQL**: [`{sql_path}`]({sql_path})")
    definition_block = "\n".join(definition_lines)

    # --- Current state (W1: result-shaping only + preview table). -------------
    sql_text = str(entry.get("sql_text") or "")
    shaping = extract_result_shaping(sql_text, kpi_id).strip()
    current_parts: list[str] = ["**Result-shaping logic**", ""]
    if shaping:
        current_parts.append("```sql\n" + shaping + "\n```")
    else:
        current_parts.append("_(no result-shaping view found)_")
    current_parts.append("")
    current_parts.append("**Result preview**")
    current_parts.append("")

    preview = str(entry.get("preview_markdown") or "").strip()
    error = str(entry.get("error") or "").strip()
    if error:
        current_parts.append("```text\n" + error + "\n```")
    elif preview:
        current_parts.append(preview)
    else:
        current_parts.append("_(no result preview available)_")
    if sql_path:
        current_parts.append("")
        current_parts.append(f"Full generated SQL: [`{sql_path}`]({sql_path})")
    current_block = "\n".join(current_parts)

    lineage_block = render_lineage_section(lineage)
    decision_block = render_decision_section(decision_bullets)

    exec_lines = [f"- **Status**: `{status}`", f"- **Last updated**: {now}"]
    if entry.get("result_view"):
        exec_lines.append(f"- **Result view**: `{entry.get('result_view')}`")
    exec_block = "\n".join(exec_lines)

    frontmatter: dict[str, Any] = {
        "contract_ref": contract_ref or "interns/generated/contracts/kpi_registry.json",
        "entity_type": "kpi",
        "entity_id": kpi_id,
        "summary": business_question or f"KPI {kpi_id}",
        "tags": ["kpi"],
        "updated": now,
        "last_validated_against_json": now,
        "validator_status": "ok" if status == "ok" else "blocked",
    }
    if related_links:
        frontmatter["related"] = related_links

    return {
        "frontmatter": frontmatter,
        "definition": definition_block,
        "current_state": current_block,
        "lineage": lineage_block,
        "decision_history": decision_block,
        "last_execution": exec_block,
        "related_links": related_links,
    }


def upsert_kpi_note(layout: WikiLayout, kpi_id: str, scaffold: dict[str, Any]) -> Path:
    """Write/refresh a KPI wiki note. Machine sections refresh; human sections persist."""
    layout.ensure_dirs()
    path = layout.kpi_note_path(kpi_id)
    existing = read_note(path)
    now = scaffold["frontmatter"].get("updated") or datetime.now(timezone.utc).isoformat()

    machine_sections = {
        "Definition": scaffold["definition"],
        "Current state": scaffold["current_state"],
        "Lineage": scaffold["lineage"],
        "Decision history": scaffold["decision_history"],
        "Last execution": scaffold["last_execution"],
    }

    related_links = scaffold.get("related_links") or []

    if existing is None:
        frontmatter = dict(scaffold["frontmatter"])
        frontmatter.setdefault("created", now)
        sections = dict(machine_sections)
        for human in KPI_HUMAN_SECTIONS:
            sections[human] = empty_section(human)
        # Seed the human-owned Related notes with auto cross-links on first
        # creation only; subsequent writes never touch human sections.
        if related_links:
            sections["Related notes"] = "\n".join(f"- {link}" for link in related_links)
    else:
        frontmatter = dict(existing.frontmatter)
        frontmatter.update(scaffold["frontmatter"])
        frontmatter.setdefault("created", now)
        sections = dict(existing.sections)
        sections.update(machine_sections)
        for human in KPI_HUMAN_SECTIONS:
            sections.setdefault(human, empty_section(human))

    rendered = _render_kpi_note(frontmatter, sections, kpi_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(rendered, encoding="utf-8")
    return path


def _render_kpi_note(
    frontmatter: dict[str, Any], sections: dict[str, str], kpi_id: str
) -> str:
    fm_ordered: dict[str, Any] = {}
    for key in _FRONTMATTER_ORDER:
        if key in frontmatter:
            fm_ordered[key] = frontmatter[key]
    for key, value in frontmatter.items():
        if key not in fm_ordered:
            fm_ordered[key] = value
    fm_text = yaml.safe_dump(fm_ordered, sort_keys=False).strip()
    lines = ["---", fm_text, "---", "", f"# KPI: {kpi_id}", ""]
    section_order = list(KPI_MACHINE_SECTIONS) + list(KPI_HUMAN_SECTIONS)
    for name in section_order:
        body = sections.get(name, "").strip() or empty_section(name)
        lines.append(f"## {name}")
        lines.append("")
        lines.append(body)
        lines.append("")
    return "\n".join(lines).rstrip() + "\n"


def _summary_line(feature: str, option: dict[str, Any], custom_definition: str) -> str:
    if physical := option.get("physical_column_option"):
        dataset = physical.get("dataset") or ""
        column = physical.get("column") or ""
        return f"`{feature}` resolved to physical column `{dataset}.{column}`".strip(".")
    if derived := option.get("derived_feature_option"):
        formula = derived.get("formula") or ""
        return f"`{feature}` resolved to derived formula: {formula}"[:200]
    if option.get("option_id") == "custom" and custom_definition:
        return f"`{feature}` resolved via custom definition: {custom_definition}"[:200]
    return f"`{feature}` resolved via `{option.get('option_id') or 'option'}`"


def _current_state_block(feature: str, option: dict[str, Any], custom_definition: str) -> str:
    option_id = option.get("option_id") or ""
    label = option.get("label") or ""
    lines = [
        f"- **Feature**: `{feature}`",
        f"- **Accepted option**: `{option_id}` — {label}",
    ]
    if physical := option.get("physical_column_option"):
        lines.append(
            f"- **Resolution**: physical column `{physical.get('dataset', '')}.{physical.get('column', '')}`"
        )
        if physical.get("dtype"):
            lines.append(f"- **Dtype**: `{physical.get('dtype')}`")
        if physical.get("row_count") is not None:
            lines.append(f"- **Row count**: {physical.get('row_count')}")
    elif derived := option.get("derived_feature_option"):
        lines.append("- **Resolution**: derived formula")
        lines.append("")
        lines.append("```sql")
        lines.append(str(derived.get("formula") or ""))
        lines.append("```")
    elif option_id == "custom" and custom_definition:
        lines.append("- **Resolution**: custom business definition")
        lines.append("")
        lines.append(f"> {custom_definition}")
    return "\n".join(lines)


def _decision_entry(
    timestamp: str,
    option_id: str,
    label: str,
    option: dict[str, Any],
    custom_definition: str,
    evidence_note: str,
) -> str:
    bits = [f"Accepted `{option_id}`"]
    if label:
        bits.append(f"— {label}")
    if option_id == "custom" and custom_definition:
        bits.append(f"({custom_definition})")
    elif physical := option.get("physical_column_option"):
        bits.append(f"({physical.get('dataset', '')}.{physical.get('column', '')})")
    elif option.get("derived_feature_option"):
        bits.append("(derived formula)")
    note = f" — {evidence_note}" if evidence_note else ""
    return f"- {timestamp}: {' '.join(bits)}{note}"


def _parse_history(body: str) -> list[str]:
    entries = []
    for line in body.splitlines():
        if line.startswith("- "):
            entries.append(line.rstrip())
    return entries


def _render_history(entries: list[str]) -> str:
    if not entries:
        return ""
    return "\n".join(entries)


def _render_evidence(files: list[str]) -> str:
    if not files:
        return "_No evidence files recorded._"
    return "\n".join(f"- `{f}`" for f in files)


def _render_rejected(options: list[dict[str, Any]]) -> str:
    if not options:
        return "_No rejected options recorded._"
    parts: list[str] = []
    for opt in options:
        opt_id = opt.get("option_id") or "option"
        label = opt.get("label") or ""
        parts.append(f"### {opt_id}: {label}")
        summary = (opt.get("business_summary") or "").strip()
        if summary:
            parts.append("")
            parts.append(summary)
        evidence = opt.get("derived_feature_option") or opt.get("physical_column_option")
        if evidence:
            parts.append("")
            parts.append("```json")
            parts.append(json.dumps(evidence, indent=2, default=str))
            parts.append("```")
        parts.append("")
    return "\n".join(parts).rstrip()


def _render_note(frontmatter: dict[str, Any], sections: dict[str, str], feature: str) -> str:
    ordered_fm: dict[str, Any] = {}
    for key in _FRONTMATTER_ORDER:
        if key in frontmatter:
            ordered_fm[key] = frontmatter[key]
    for key, value in frontmatter.items():
        if key not in ordered_fm:
            ordered_fm[key] = value

    fm_text = yaml.safe_dump(ordered_fm, sort_keys=False, allow_unicode=True).strip()

    lines = ["---", fm_text, "---", "", f"# Feature: {feature}", ""]
    for name in ALL_SECTIONS:
        body = sections.get(name, "").strip() or empty_section(name)
        lines.append(f"## {name}")
        lines.append("")
        lines.append(body)
        lines.append("")
    return "\n".join(lines).rstrip() + "\n"
