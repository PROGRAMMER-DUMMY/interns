"""Confirmation panel for a detected KPI file format.

Renders a :class:`KpiFormatDetection` into the project's JSON-backed panel + the
human-readable markdown card, following the rule that drives this whole layer:
never show a mapping without its CONSEQUENCE (a real-row read-back), and never
auto-commit a low-confidence / ambiguous / nested mapping — surface it for
confirmation and save the accepted answer as a workspace decision.

Pure rendering (no I/O): input is the detection plus a few sample rows; output is
the panel dict and its markdown. Workspace-agnostic — no domain vocabulary.
"""
from __future__ import annotations

from typing import Any

from core.onboarding.kpi.kpi_format_detector import (
    CANONICAL_ROLES,
    KpiFormatDetection,
)

# Confidence label -> ASCII status marker (no emojis, per project convention).
_MARKER = {"HIGH": "[ok]", "MED": "[~]", "LOW": "[x]"}

_ROLE_LABEL = {
    "business_question": "Business question",
    "metric": "Metric",
    "cuts": "Cuts / dimensions",
    "filters": "Filters",
    "description": "Description",
    "target": "Target",
    "refinement": "Refinement / open Qs",
}


def build_kpi_confirmation_panel(
    detection: KpiFormatDetection,
    sample_rows: list[dict[str, Any]] | None = None,
    *,
    max_samples: int = 2,
) -> dict[str, Any]:
    """Assemble the JSON-backed confirmation panel for a detected KPI format."""
    sample_rows = sample_rows or []
    read_backs = [
        {"row_index": i, "fields": detection.read_back(row)}
        for i, row in enumerate(sample_rows[:max_samples])
    ]
    # Ambiguity = anything the user must resolve: low/medium-confidence mappings,
    # missing required roles, columns we could not place.
    ambiguous = [
        c.to_dict() for c in detection.columns
        if c.role is not None and c.confidence_label != "HIGH"
    ]

    options = [
        {
            "option_id": "confirm",
            "label": "Mapping + readings are correct - proceed",
            "kind": "accept",
        },
        {
            "option_id": "fix_column",
            "label": "A column is mapped to the wrong role (say which column -> which role)",
            "kind": "correct_mapping",
        },
    ]
    if detection.missing_required_roles:
        options.append({
            "option_id": "set_missing_role",
            "label": "A required role is missing - point me to its column ("
                     + ", ".join(detection.missing_required_roles) + ")",
            "kind": "set_role",
        })
    if detection.nesting_suspected:
        options.append({
            "option_id": "fix_nesting",
            "label": "Nesting is wrong (these rows are flat / belong to a different parent)",
            "kind": "correct_nesting",
        })

    needs_confirmation = detection.to_dict()["needs_user_confirmation"]
    recommended = "confirm" if not needs_confirmation else ""

    return {
        "stage": "kpi_format_confirmation",
        "status": "needs_user_answer",
        "instruction": (
            "Detected the format of a KPI file. Verify my understanding below: the per-column "
            "role mapping, what each column means, and a real row read back through that mapping. "
            "Confirm if correct, or tell me which column/role/nesting to fix. Nothing is committed "
            "until you confirm; the accepted mapping is saved as a workspace decision and reused."
        ),
        "question": "Is this KPI-file interpretation correct?",
        "options": options,
        "recommended_option_id": recommended,
        "summary": {
            "source": detection.source,
            "row_count": detection.row_count,
            "overall_confidence": round(detection.overall_confidence, 3),
            "overall_label": detection.overall_label,
            "column_mapping": [c.to_dict() for c in detection.columns],
            "unmapped_headers": list(detection.unmapped_headers),
            "missing_required_roles": list(detection.missing_required_roles),
            "ambiguous_mappings": ambiguous,
            "nesting": {
                "suspected": detection.nesting_suspected,
                "signals": list(detection.nesting_signals),
            },
            "read_back": read_backs,
            "needs_user_confirmation": needs_confirmation,
        },
    }


def render_kpi_confirmation_markdown(panel: dict[str, Any]) -> str:
    """Render the confirmation panel as the human-readable terminal card."""
    s = panel.get("summary", {})
    lines: list[str] = []
    lines.append("# KPI File - Confirm Detected Format")
    lines.append("")
    src = s.get("source") or "(in-memory)"
    lines.append(
        f"Source: `{src}`  |  rows: {s.get('row_count', 0)}  |  "
        f"confidence: {s.get('overall_label', '?')} ({s.get('overall_confidence', 0)})  |  "
        f"nesting: {'YES' if s.get('nesting', {}).get('suspected') else 'no'}"
    )
    lines.append("")

    lines.append("## Column mapping (verify the Role + Why)")
    lines.append("")
    lines.append("| | Header | Role | Why | Conf |")
    lines.append("|---|---|---|---|---|")
    for col in s.get("column_mapping", []):
        marker = _MARKER.get(col.get("confidence_label", "LOW"), "[x]")
        role = col.get("role")
        role_label = _ROLE_LABEL.get(role, role) if role else "(unmapped)"
        lines.append(
            f"| {marker} | {col.get('header', '')} | {role_label} | "
            f"{col.get('reason', '')} | {col.get('confidence_label', '')} |"
        )
    lines.append("")

    missing = s.get("missing_required_roles") or []
    unmapped = s.get("unmapped_headers") or []
    if missing:
        lines.append(f"**Missing required role(s):** {', '.join(missing)}  (must be resolved)")
    if unmapped:
        lines.append(f"**Unmapped columns:** {', '.join(unmapped)}  (no KPI role - confirm if intended)")
    if missing or unmapped:
        lines.append("")

    read_back = s.get("read_back") or []
    if read_back:
        lines.append("## Real row, read back (the consequence of this mapping)")
        lines.append("")
        for rb in read_back:
            lines.append(f"Row {rb.get('row_index')}:")
            for role in CANONICAL_ROLES:
                val = rb.get("fields", {}).get(role)
                if val:
                    label = _ROLE_LABEL.get(role, role)
                    shown = val if len(val) <= 80 else val[:77] + "..."
                    lines.append(f"  - {label:22} -> {shown}")
            lines.append("")

    nesting = s.get("nesting", {})
    if nesting.get("suspected"):
        lines.append("## Nesting detected")
        lines.append("")
        for sig in nesting.get("signals", []):
            lines.append(f"  - {sig}")
        lines.append("")

    lines.append("## Confirm")
    lines.append("")
    rec = panel.get("recommended_option_id")
    for opt in panel.get("options", []):
        star = "  (recommended)" if opt.get("option_id") == rec and rec else ""
        lines.append(f"- **{opt.get('option_id')}**: {opt.get('label')}{star}")
    if not rec:
        lines.append("")
        lines.append("_No option is pre-selected: confidence is not HIGH, or a required role / "
                     "nesting needs your decision._")
    return "\n".join(lines)


__all__ = ["build_kpi_confirmation_panel", "render_kpi_confirmation_markdown"]
