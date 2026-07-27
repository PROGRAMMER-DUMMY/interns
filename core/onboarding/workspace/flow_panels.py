"""Pure rendering / summary helpers for the workspace flow.

Extracted from flow.py (Phase 1 decomposition) to shrink the orchestrator module.
These are stateless module-level functions -- panel compaction, markdown rendering,
KPI-resolution review, and data-understanding/result summaries. They reference no
WorkspaceFlow state; their only cross-module dependency is _read_json (from
flow_io). flow.py re-imports every name below, so flow.<name> still resolves and no
external caller is affected.
"""
from __future__ import annotations

import re
from pathlib import Path
from typing import Any

from core.onboarding.kpi.registry_loader import render_kpi_block
from core.presentation.console_tables import render_markdown_table
from core.storage.workspace_layout import WorkspaceLayout
from core.onboarding.workspace.flow_io import _read_json


def _compact_panel(
    *,
    stage: str,
    status: str,
    source_panel: dict[str, Any],
    instruction: str,
    artifact_paths: list[str],
    resolution_review: dict[str, Any] | None = None,
    orchestration_context: dict[str, Any] | None = None,
) -> dict[str, Any]:
    panel = {
        "stage": stage,
        "status": status,
        "instruction": instruction,
        "question": source_panel.get("question", ""),
        "options": source_panel.get("options", []),
        "recommended_option_id": source_panel.get("recommended_option_id", ""),
        "why": source_panel.get("why", ""),
        "artifact_paths": artifact_paths,
        "source_panel_summary": {
            key: source_panel.get(key)
            for key in ("stage", "status", "feature", "applies_to_kpis", "reuse_scope")
            if key in source_panel
        },
    }
    for key in ("output_dialect", "immutable_kpi_policy", "kpi_understanding"):
        if source_panel.get(key):
            panel[key] = source_panel[key]
    for key in ("recovery_commands", "suggested_skills"):
        if source_panel.get(key):
            panel[key] = source_panel[key]
    if stage == "kpi_blocker" and not panel.get("suggested_skills"):
        panel["suggested_skills"] = [
            {"name": "kpi-analyst", "why": "Interpret the KPI question and validate proposed mappings."},
            {"name": "feature-derivation-library", "why": "Choose between direct and derived feature options."},
            {"name": "grill-requirements", "why": "Clarify-ambiguity mode: flag missing context before applying an answer."},
        ]
    if orchestration_context:
        panel["orchestration_context"] = orchestration_context
    if resolution_review:
        panel["resolution_review"] = resolution_review
        panel["hidden_panel_harness"] = _build_hidden_panel_harness(panel, resolution_review)
    return panel

def _render_panel_markdown(panel: dict[str, Any]) -> str:
    lines = [
        f"# Workspace Flow: {panel.get('stage', '')}",
        "",
        f"- Session: `{panel.get('session_id', '')}`",
        f"- Workspace: `{panel.get('workspace', '')}`",
        f"- Status: `{panel.get('status', '')}`",
        "",
        "## Instruction",
        "",
        str(panel.get("instruction", "")),
        "",
    ]
    if panel.get("resolution_review"):
        lines.extend(_render_resolution_review(panel["resolution_review"]))
    if panel.get("orchestration_context"):
        context = panel["orchestration_context"]
        route = context.get("layer_route") or {}
        data_quality = context.get("data_quality") or {}
        lines.extend(
            [
                "## Orchestration Context",
                "",
                f"- Mode: `{context.get('mode', '')}`",
                f"- Data quality status: `{data_quality.get('status', '')}`",
                f"- Layer route: `{route.get('selected_track', '')}` from `{route.get('start_layer', '')}`",
                "",
            ]
        )
    if panel.get("output_dialect"):
        dialect = panel["output_dialect"]
        lines.extend(
            [
                "## Output Dialect",
                "",
                f"- Default: `{dialect.get('label', 'SQL (default)')}`",
                "- Alternatives: " + ", ".join(f"`{item}`" for item in dialect.get("alternatives", [])),
                f"- Rule: {dialect.get('rule', '')}",
                "",
            ]
        )
    if panel.get("immutable_kpi_policy"):
        policy = panel["immutable_kpi_policy"]
        lines.extend(
            [
                "## Immutable KPI Policy",
                "",
                str(policy.get("rule", "")),
                "",
            ]
        )
    if panel.get("kpi_understanding"):
        lines.extend(["## KPI Understanding Review", ""])
        for item in panel.get("kpi_understanding") or []:
            original = item.get("original_kpi") or {}
            lines.extend(
                [
                    f"### {item.get('kpi_id', '')}",
                    "",
                    f"- Original KPI: {original.get('business_question', '')}",
                    f"- Source metric: `{original.get('metric', '')}`",
                    f"- Source cuts / filters: {', '.join(original.get('cuts') or [])}",
                    "",
                    "#### My Understanding",
                    "",
                    str(item.get("my_understanding", "")),
                    "",
                    "#### Strict Proven SQL",
                    "",
                    "```sql",
                    str(item.get("strict_proven_sql", "")),
                    "```",
                    "",
                    "#### Placeholder Intent SQL",
                    "",
                    "```sql",
                    str(item.get("intent_sql_sketch", "")),
                    "```",
                    "",
                    "#### Demo Result Table",
                    "",
                    str(item.get("demo_result_table", "")),
                    "",
                ]
            )
    summary = panel.get("summary") or {}
    completed_kpis = summary.get("completed_kpis") or []
    if completed_kpis:
        # Compact at completion: SQL is linked per KPI, not inlined. The full SQL +
        # tables are emitted right after by the `## KPI Result Packet` (compact) and
        # remain in the .sql files / runs snapshot. Inlining SQL here too produced a
        # double full-SQL dump that made the completion output UI-truncate (which
        # agents misread as a failed read and then re-read/paraphrase).
        lines.extend(["## Completed KPIs", ""])
        for entry in completed_kpis:
            lines.extend(render_kpi_block(entry, heading_level=3, include_sql=False))
    else:
        # The `results` stage carries per-KPI previews under `summary.kpis`
        # (definition + SQL + result table), not `completed_kpis`. Render them
        # inline too so `workspace-flow results` emits the full result packet in
        # its own output — the operator does not have to ask "show results"; the
        # presenter forwards the rendered tables automatically.
        result_kpis = summary.get("kpis") or []
        if result_kpis:
            # Compact: definition + table + SQL pointer, no inlined SQL. Full SQL is
            # delivered by `workspace-flow results --full` (the full packet) and lives
            # in the per-KPI .sql files. Keeping it compact here means the session panel
            # markdown never UI-truncates (the truncation that agents misread as a
            # failed read -> re-read loop / paraphrase).
            lines.extend(["## KPI Results", ""])
            for entry in result_kpis:
                lines.extend(render_kpi_block(entry, heading_level=3, include_sql=False))
    recovery_commands = (panel.get("summary") or {}).get("recovery_commands") or panel.get("recovery_commands") or []
    if recovery_commands:
        lines.extend(["## Recovery Commands", ""])
        for cmd in recovery_commands:
            label = str(cmd.get("label") or cmd.get("why") or "").strip()
            command = str(cmd.get("command") or "").strip()
            if label:
                lines.append(f"- **{label}**")
            if command:
                lines.append("  ```bash")
                lines.append(f"  {command}")
                lines.append("  ```")
        lines.append("")
    suggested_skills = (panel.get("summary") or {}).get("suggested_skills") or panel.get("suggested_skills") or []
    if suggested_skills:
        lines.extend(["## Suggested Skills", ""])
        for skill in suggested_skills:
            if isinstance(skill, dict):
                name = str(skill.get("name") or "")
                why = str(skill.get("why") or "")
                lines.append(f"- `{name}`{f' — {why}' if why else ''}")
            else:
                lines.append(f"- `{skill}`")
        lines.append("")
    if panel.get("question"):
        lines.extend(["## Question", "", str(panel.get("question", "")), ""])
    if panel.get("options"):
        lines.extend(["## Options", ""])
        for option in panel.get("options", []):
            lines.extend(
                [
                    f"### {option.get('option_id', '')}: {option.get('label', '')}",
                    "",
                    str(option.get("business_summary") or option.get("description") or ""),
                    "",
                ]
            )
    if panel.get("recommended_option_id"):
        lines.extend(["## Suggested Default", "", f"`{panel.get('recommended_option_id')}`", ""])
    if panel.get("artifact_paths"):
        lines.extend(["## Artifacts", ""])
        lines.extend(f"- `{path}`" for path in panel.get("artifact_paths", []))
        lines.append("")
    return "\n".join(lines).rstrip() + "\n"

def _build_kpi_resolution_review(repo_root: Path, workspace_rel: str) -> dict[str, Any]:
    workspace = repo_root / workspace_rel
    mapping = _read_json(workspace / "interns" / "generated" / "contracts" / "kpi_feature_mapping.json")
    registry = _read_json(workspace / "interns" / "generated" / "contracts" / "kpi_registry.json")
    source_kpis = mapping.get("kpis") or registry.get("kpis") or []
    kpis = []
    for idx, kpi in enumerate(source_kpis, start=1):
        kpi_id = str(kpi.get("kpi_id") or f"kpi_{idx:03d}")
        metric = str(kpi.get("metric") or "")
        cuts = str(kpi.get("cuts") or "")
        filters = _extract_source_filters(kpi.get("name", ""), cuts)
        features = kpi.get("features") or []
        kpis.append(
            {
                "kpi_id": kpi_id,
                "source_question": str(kpi.get("name") or kpi.get("business_question") or ""),
                "source_description": str(kpi.get("description") or ""),
                "metric": metric,
                "cuts_and_grain": cuts,
                "filters": filters,
                "resolved_source_logic": _summarize_source_logic(features),
                "status": str(kpi.get("status") or "unknown"),
                "terms": _term_rows(features),
            }
        )
    return {
        "title": "KPI Resolution Review",
        "source_of_truth": _source_of_truth(source_kpis),
        "source_truth_rule": (
            "KPI question, metric, filters, cuts, and grain from the source workbook/registry "
            "are absolute truth. Do not rewrite or compact them during resolution."
        ),
        "required_visible_sections": [
            "source question",
            "metric",
            "cuts and grain",
            "filters",
            "resolved source logic",
            "status",
            "blocker question",
        ],
        "kpis": kpis,
    }

def _render_resolution_review(review: dict[str, Any]) -> list[str]:
    rows = [
        [
            item.get("kpi_id", ""),
            item.get("source_question", ""),
            item.get("metric", ""),
            item.get("cuts_and_grain", ""),
            ", ".join(item.get("filters") or []) or "None stated",
            item.get("resolved_source_logic", ""),
            item.get("status", ""),
        ]
        for item in review.get("kpis") or []
    ]
    lines = [
        f"## {review.get('title', 'KPI Resolution Review')}",
        "",
        f"- Source of truth: `{review.get('source_of_truth', '')}`",
        f"- Rule: {review.get('source_truth_rule', '')}",
        "",
        render_markdown_table(
            [
                "KPI",
                "Workbook Question",
                "Metric From Workbook",
                "Cuts / Grain From Workbook",
                "Filters",
                "Resolved Source Logic",
                "Status",
            ],
            rows,
        ),
        "",
    ]
    for item in review.get("kpis") or []:
        if not item.get("terms"):
            continue
        lines.extend(
            [
                f"### {item.get('kpi_id', '')} Resolved Source Mapping",
                "",
                render_markdown_table(
                    ["Business Term", "Resolved Column / Formula", "Source Dataset", "Proof Status"],
                    [
                        [
                            term.get("feature", ""),
                            term.get("resolved_as", ""),
                            term.get("dataset", ""),
                            term.get("proof_status", ""),
                        ]
                        for term in item.get("terms") or []
                    ],
                ),
                "",
            ]
        )
    return lines

def _build_hidden_panel_harness(panel: dict[str, Any], review: dict[str, Any]) -> dict[str, Any]:
    checks = []
    for kpi in review.get("kpis") or []:
        checks.extend(
            [
                _panel_check(kpi, "source_question_visible", bool(kpi.get("source_question"))),
                _panel_check(kpi, "metric_visible", bool(kpi.get("metric"))),
                _panel_check(kpi, "cuts_or_grain_visible", bool(kpi.get("cuts_and_grain"))),
                _panel_check(kpi, "resolved_source_logic_visible", bool(kpi.get("resolved_source_logic"))),
                _panel_check(kpi, "status_visible", bool(kpi.get("status"))),
            ]
        )
    checks.append(
        {
            "id": "not_compact_question_only",
            "passed": bool(review.get("kpis")) and bool(panel.get("question")) and bool(panel.get("options")),
            "requirement": "Panel keeps the answer picker but also includes a full KPI resolution review.",
        }
    )
    return {
        "hidden": True,
        "purpose": "Detect CLI regressions that shrink KPI resolution to a one-line question/answer prompt.",
        "passed": all(check["passed"] for check in checks),
        "checks": checks,
    }

def _panel_check(kpi: dict[str, Any], check_id: str, passed: bool) -> dict[str, Any]:
    return {
        "id": f"{kpi.get('kpi_id', 'unknown')}_{check_id}",
        "passed": passed,
        "requirement": check_id.replace("_", " "),
    }

def _source_of_truth(kpis: list[dict[str, Any]]) -> str:
    for kpi in kpis:
        source = str(kpi.get("source") or "")
        if source:
            return source
    return "kpi_registry.json"

def _extract_source_filters(question: str, cuts: str) -> list[str]:
    """Extract filter expressions from KPI cuts + the business question.

    Generic — picks up:
      - Comparison expressions in `cuts` (anything containing `=`, `>`, `<`)
      - Quoted literals in either `cuts` or `question` (e.g., `'Medicare'`,
        `'Wholesale'`, `'Refunded'`) as `<context> = '<literal>'`
      - `top N` phrases as ranking limits
    No domain words hardcoded.
    """
    filters: list[str] = []
    for part in str(cuts).split(","):
        cleaned = part.strip()
        if any(token in cleaned for token in ("=", ">", "<")):
            filters.append(cleaned)
    for source_text in (str(cuts), str(question)):
        for match in re.finditer(r"['\"]([^'\"]{1,80})['\"]", source_text):
            literal = match.group(1).strip()
            if not literal:
                continue
            if not any(literal.lower() in item.lower() for item in filters):
                filters.append(f"`'{literal}'`")
    lowered = str(question).lower()
    top_match = re.search(r"\btop\s+(\d+)\b", lowered)
    if top_match:
        top_n = top_match.group(1)
        if not any(f"top {top_n}" in item.lower() for item in filters):
            filters.append(f"Top {top_n}")
    return filters

def _summarize_source_logic(features: list[dict[str, Any]]) -> str:
    pieces = []
    for feature in features:
        name = str(feature.get("feature") or "")
        columns = feature.get("source_columns") or []
        if not columns:
            if feature.get("formula"):
                pieces.append(f"{name} via formula")
            continue
        column = columns[0]
        pieces.append(f"{name} -> {Path(str(column.get('dataset') or '')).name}.{column.get('column', '')}")
    return "; ".join(pieces[:8])

def _term_rows(features: list[dict[str, Any]]) -> list[dict[str, Any]]:
    rows = []
    for feature in features:
        columns = feature.get("source_columns") or []
        column = columns[0] if columns else {}
        dataset = Path(str(column.get("dataset") or column.get("source") or "")).name
        resolved = str(column.get("column") or feature.get("formula") or feature.get("resolution_type") or "")
        rows.append(
            {
                "feature": str(feature.get("feature") or ""),
                "resolved_as": resolved,
                "dataset": dataset,
                "proof_status": str(feature.get("state") or feature.get("resolution_type") or ""),
            }
        )
    return rows

def _summarize_current_data_model(
    profiles: list[dict[str, Any]],
    relationships: dict[str, Any],
) -> dict[str, Any]:
    """Compact, workspace-agnostic view of the current data model from profiles + relationships."""
    tables = []
    for prof in profiles:
        if not isinstance(prof, dict):
            continue
        name = Path(str(prof.get("path") or prof.get("table") or prof.get("name") or "")).name or str(
            prof.get("table") or prof.get("name") or "table"
        )
        columns = prof.get("columns")
        if isinstance(columns, list):
            col_count = len(columns)
        elif isinstance(prof.get("schema"), dict):
            col_count = len(prof["schema"])
        else:
            col_count = 0
        tables.append(
            {
                "table": name,
                "row_count": prof.get("row_count"),
                "column_count": col_count,
            }
        )
    rels = []
    for rel in (relationships.get("relationships") or []):
        if not isinstance(rel, dict):
            continue
        rels.append(
            {
                "relationship_id": rel.get("relationship_id"),
                "left": rel.get("left_dataset"),
                "right": rel.get("right_dataset"),
                "state": rel.get("state"),
            }
        )
    return {"tables": tables, "relationships": rels}

def _summarize_current_kpi_set(layout: WorkspaceLayout) -> list[dict[str, Any]]:
    """Echo the current KPI set (id + question + status) from the registry/mapping, if present."""
    mapping = _read_json(layout.kpi_feature_mapping_path)
    registry = _read_json(layout.kpi_registry_path)
    source_kpis = mapping.get("kpis") or registry.get("kpis") or []
    kpis = []
    for idx, kpi in enumerate(source_kpis, start=1):
        if not isinstance(kpi, dict):
            continue
        kpis.append(
            {
                "kpi_id": str(kpi.get("kpi_id") or f"kpi_{idx:03d}"),
                "question": str(kpi.get("name") or kpi.get("business_question") or ""),
                "metric": str(kpi.get("metric") or ""),
                "status": str(kpi.get("status") or "unknown"),
            }
        )
    return kpis

def _render_data_understanding_markdown(payload: dict[str, Any]) -> str:
    tier = payload.get("quality_tier") or {}
    schema = payload.get("schema_type") or {}
    lines = [
        "# Data Understanding Gate",
        "",
        f"- Workspace: `{payload.get('workspace', '')}`",
        f"- Profiles analyzed: {payload.get('profile_count', 0)}",
        "",
        "## Detected Quality Tier",
        "",
        f"- Tier: `{tier.get('tier', '')}` (confidence {tier.get('confidence', '')})",
        "",
        "### Evidence",
        "",
    ]
    for item in tier.get("evidence") or []:
        lines.append(f"- {item}")
    lines.extend(
        [
            "",
            "## Detected Schema Type",
            "",
            f"- Schema type: `{schema.get('schema_type', '')}` (confidence {schema.get('confidence', '')})",
            "",
            "### Evidence",
            "",
        ]
    )
    for item in schema.get("evidence") or []:
        lines.append(f"- {item}")
    lines.extend(["", "## Choose How To Proceed", ""])
    for option in payload.get("top_level_options") or []:
        lines.extend(
            [
                f"### {option.get('option_id', '')}: {option.get('label', '')}",
                "",
                str(option.get("description") or ""),
                "",
            ]
        )
    lines.extend(
        [
            f"## Scoped Processing Options (tier `{tier.get('tier', '')}`)",
            "",
        ]
    )
    for option in payload.get("scoped_processing_options") or []:
        lines.extend(
            [
                f"### {option.get('id', '')}: {option.get('label', '')}",
                "",
                str(option.get("description") or ""),
                f"_Applies when:_ `{option.get('applies_when', '')}`",
                "",
            ]
        )
    data_model = payload.get("current_data_model") or {}
    lines.extend(["## Current Data Model", ""])
    tables = data_model.get("tables") or []
    if tables:
        lines.append(
            render_markdown_table(
                ["Table", "Rows", "Columns"],
                [[t.get("table", ""), t.get("row_count", ""), t.get("column_count", "")] for t in tables],
            )
        )
    else:
        lines.append("- (no profiled tables)")
    rels = data_model.get("relationships") or []
    if rels:
        lines.extend(
            [
                "",
                render_markdown_table(
                    ["Relationship", "Left", "Right", "State"],
                    [
                        [r.get("relationship_id", ""), r.get("left", ""), r.get("right", ""), r.get("state", "")]
                        for r in rels
                    ],
                ),
            ]
        )
    lines.extend(["", "## Current KPI Set", ""])
    kpis = payload.get("current_kpi_set") or []
    if kpis:
        lines.append(
            render_markdown_table(
                ["KPI", "Question", "Metric", "Status"],
                [[k.get("kpi_id", ""), k.get("question", ""), k.get("metric", ""), k.get("status", "")] for k in kpis],
            )
        )
    else:
        lines.append("- (no KPI registry/mapping present yet)")
    lines.append("")
    return "\n".join(lines).rstrip() + "\n"

def _run_cost_lines(run_cost: Any) -> list[str]:
    """The run's warehouse cost, or an honest statement that it was never read.

    The audited run rendered `cost_usd: 0` on all 92 rows and that read as
    "this run was free". It was not -- nothing had ever queried the cost back.
    An absent reconciliation now says so and names the command, because a
    missing number must never render as a zero.

    Warehouse dollars only. Agent-token cost is a separate basis and is not
    summed into this figure (see core.observability.warehouse_cost).
    """
    if not isinstance(run_cost, dict):
        return [
            "- Warehouse cost: not reconciled -- run "
            "`uv run reconcile-warehouse-cost --workspace <ws>` "
            "(needs a human-set AUTORESEARCH_ALLOW_REMOTE_EXECUTION).",
            "",
        ]
    status = str(run_cost.get("status") or "")
    if status != "reconciled":
        return [
            f"- Warehouse cost: not available (`{status or 'unknown'}`) -- "
            f"{run_cost.get('reason') or 'see interns/reports/cost_ledger/warehouse_cost.md'}.",
            "",
        ]
    return [
        f"- Warehouse cost (run `{run_cost.get('run_id', '')}`): "
        f"**${float(run_cost.get('warehouse_usd') or 0.0):.2f}** "
        f"({float(run_cost.get('warehouse_dbus') or 0.0):.2f} DBU, "
        f"`{run_cost.get('cost_source', '')}`). Agent-token cost is tracked "
        "separately and is NOT included.",
        "",
    ]


def _render_results_markdown(payload: dict[str, Any], *, compact: bool = False) -> str:
    lines = [
        "# KPI Query Results",
        "",
        f"- Workspace: `{payload.get('workspace', '')}`",
        f"- KPI count: {len(payload.get('kpis', []))}",
        "",
    ]
    lines.extend(_run_cost_lines(payload.get("run_cost")))
    if compact:
        lines.append(
            "- Compact view: SQL is linked per KPI (`SQL:` line), not inlined. "
            "Full SQL is in the linked `.sql` file and the combined packet."
        )
        lines.append("")
    for entry in payload.get("kpis", []):
        lines.extend(render_kpi_block(entry, heading_level=2, include_sql=not compact))
    return "\n".join(lines).rstrip() + "\n"

def _result_view(conn: Any, kpi_id: str) -> str:
    rows = conn.execute(
        "SELECT table_name FROM information_schema.views WHERE lower(table_name) = lower(?)",
        [f"{kpi_id}_results"],
    ).fetchall()
    return str(rows[0][0]) if rows else ""
