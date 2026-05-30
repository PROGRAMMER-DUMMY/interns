"""Canonical contract for Family-A *decision panels* — the interactive panels the
user answers (KPI generation, KPI blockers, data-model design, format selection).

Every decision panel, whatever builds it, is normalized to one shape so the human
card (`current.md`) and the machine contract (`current.json`) are uniform and a
single renderer/consumer can handle them all:

    artifact_type, version, workspace,
    stage, status,
    question,
    options: [{option_id, label, description, ...}],     # extra keys preserved
    recommended_option_id, recommended_answer, why,
    next_step,
    required_specialists, suggested_skills,              # roster footer
    + any panel-specific payload (scores, evidence, examples) left untouched.

`normalize_decision_panel` is NON-DESTRUCTIVE: it only fills missing keys and
ensures each option has option_id/label/description — it never drops payload an
existing builder set. `validate_decision_panel` returns contract violations
(used by tests to keep panels conformant).
"""
from __future__ import annotations

from typing import Any

DECISION_PANEL_VERSION = 1

REQUIRED_KEYS: tuple[str, ...] = (
    "artifact_type",
    "version",
    "workspace",
    "stage",
    "status",
    "question",
    "options",
    "recommended_option_id",
    "recommended_answer",
    "why",
    "next_step",
)


def _normalize_option(option: dict[str, Any]) -> dict[str, Any]:
    # Preserve every key the builder set (value, columns, upgrade_columns, ...);
    # only guarantee the three the renderer/answer-resolver rely on.
    out = dict(option)
    out["option_id"] = str(option.get("option_id") or "")
    out["label"] = str(option.get("label") or "")
    out["description"] = str(option.get("description") or "")
    return out


def normalize_decision_panel(
    panel: dict[str, Any],
    *,
    workspace: str,
    routing_stage: str | None = None,
) -> dict[str, Any]:
    """Fill the canonical decision-panel contract on `panel` in place and return it.

    Existing values always win (setdefault semantics) so a builder that already set
    score-driven `suggested_skills` or a custom status keeps them.
    """
    options = [_normalize_option(o) for o in (panel.get("options") or []) if isinstance(o, dict)]
    panel["options"] = options
    panel.setdefault("artifact_type", "decision_panel/current.json")
    panel.setdefault("version", DECISION_PANEL_VERSION)
    panel.setdefault("workspace", workspace)
    panel.setdefault("stage", "")
    panel.setdefault("question", "")
    panel.setdefault("recommended_option_id", "")
    panel.setdefault("recommended_answer", panel.get("recommended_option_id", ""))
    panel.setdefault("why", "")
    panel.setdefault("next_step", "")
    # status: needs a choice when there are options, else informational/terminal.
    panel.setdefault("status", "needs_user_choice" if options else "info")
    if routing_stage is not None:
        from core.onboarding.workspace.delegation import routing_for

        routing = routing_for(routing_stage)
        panel.setdefault("required_specialists", routing["agents"])
        panel.setdefault("suggested_skills", routing["skills"])
    return panel


def validate_decision_panel(panel: dict[str, Any]) -> list[str]:
    """Return a list of contract violations ([] means conformant)."""
    errors: list[str] = []
    for key in REQUIRED_KEYS:
        if key not in panel:
            errors.append(f"missing required key: {key}")
    if not isinstance(panel.get("options"), list):
        errors.append("options must be a list")
    else:
        for idx, option in enumerate(panel["options"]):
            if not isinstance(option, dict):
                errors.append(f"options[{idx}] must be an object")
                continue
            for key in ("option_id", "label", "description"):
                if key not in option:
                    errors.append(f"options[{idx}] missing {key}")
    return errors


__all__ = [
    "DECISION_PANEL_VERSION",
    "REQUIRED_KEYS",
    "normalize_decision_panel",
    "validate_decision_panel",
]
