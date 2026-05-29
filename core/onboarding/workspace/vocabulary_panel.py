"""Vocabulary clarify-ambiguity panel.

When `WorkspaceResearcher` returns `needs_user_confirmation=True` (low
overall_confidence), the workflow blocks at a panel that asks the user
to confirm or correct the proposed terms before they get baked in.

Generic across workspaces. The panel ALWAYS shows what was derived from
THIS workspace's evidence — never a curated list.

Output: `interns/reports/vocabulary_confirmation/current.{json,md}` panel.
Apply step: `apply_vocabulary_confirmation_answer` mutates
`workspace_vocabulary.json` by adding `accepted_at` per category +
flipping `needs_user_confirmation` to False.
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from core.onboarding.workspace.research import VocabularyResult, research_workspace_vocabulary
from core.storage.workspace_layout import WorkspaceLayout


PANEL_VERSION = 1


@dataclass(frozen=True)
class VocabularyConfirmationPanel:
    current_json_path: str
    current_markdown_path: str
    needs_user_answer: bool
    category_count: int
    term_count: int

    def summary(self) -> dict[str, Any]:
        return {
            "current_json_path": self.current_json_path,
            "current_markdown_path": self.current_markdown_path,
            "needs_user_answer": self.needs_user_answer,
            "category_count": self.category_count,
            "term_count": self.term_count,
        }


def prepare_vocabulary_confirmation_panel(
    repo_root: str | Path,
    workspace: str | Path,
    *,
    research_result: VocabularyResult | None = None,
) -> VocabularyConfirmationPanel:
    """Run research if needed and emit a confirmation panel.

    Panel only blocks when overall_confidence is below the research module's
    threshold OR when no terms could be derived at all. High-confidence
    workspaces flow through with a `complete` panel and no user prompt.
    """
    layout = WorkspaceLayout(project_root=(Path(repo_root).resolve() / workspace).resolve())
    if research_result is None:
        research_result = research_workspace_vocabulary(repo_root, workspace)

    panel_dir = layout.reports_dir / "vocabulary_confirmation"
    panel_dir.mkdir(parents=True, exist_ok=True)

    category_options: list[dict[str, Any]] = []
    term_count = 0
    for category, items in research_result.categories.items():
        if not items:
            continue
        term_count += len(items)
        option_terms = [
            {
                "term": item.term,
                "confidence": item.confidence,
                "sample_values": item.sample_values,
                "sources": item.sources,
            }
            for item in items[:25]
        ]
        category_options.append(
            {
                "category": category,
                "term_count": len(items),
                "confidence_threshold": 0.5,
                "terms": option_terms,
                "question": (
                    f"Do these terms correctly capture this workspace's "
                    f"`{category.replace('_terms', '')}` vocabulary?"
                ),
                "options": [
                    {"option_id": "accept_all", "label": "Accept all proposed terms"},
                    {"option_id": "accept_high_confidence",
                     "label": "Accept only confidence >= 0.7; keep others as candidates"},
                    {"option_id": "edit", "label": "Provide a custom term list"},
                ],
            }
        )

    payload = {
        "artifact_type": "vocabulary_confirmation/current.json",
        "version": PANEL_VERSION,
        "workspace": research_result.workspace,
        "generated_at": _now(),
        "status": "needs_user_answer" if research_result.needs_user_confirmation else "complete",
        "instruction": (
            "Workspace vocabulary was derived from evidence under this workspace "
            "(profile_index, KPI registry, data dictionary, accepted definitions). "
            "Review each category's proposed terms below. Categories with high overall "
            "confidence appear as informational; low-confidence categories require an "
            "answer before downstream resolution proceeds."
        )
        if research_result.needs_user_confirmation
        else (
            "Workspace vocabulary research completed with high confidence. "
            "Review proposed terms; no answer required."
        ),
        "overall_confidence": research_result.confidence,
        "category_options": category_options,
        "evidence_sources": [
            "interns/generated/contracts/workspace_feature_definitions.json",
            "interns/generated/contracts/kpi_registry.json",
            "interns/generated/contracts/kpi_feature_mapping.json",
            "interns/generated/profiles/profile_index.json",
            "docs/data_dictionary.*",
        ],
        "suggested_skills": [
            {"name": "clarify-ambiguity",
             "why": "Confirm which workspace-derived terms map to which category before they bake into the lexicon."},
            {"name": "domain-model",
             "why": "Cross-check derived entity_terms against the data model."},
        ],
        "applies_to_artifact": "interns/generated/contracts/workspace_vocabulary.json",
    }

    current_json = panel_dir / "current.json"
    current_md = panel_dir / "current.md"
    current_json.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    current_md.write_text(_render_panel_markdown(payload), encoding="utf-8")

    return VocabularyConfirmationPanel(
        current_json_path=_rel(current_json, Path(repo_root).resolve()),
        current_markdown_path=_rel(current_md, Path(repo_root).resolve()),
        needs_user_answer=research_result.needs_user_confirmation,
        category_count=len(category_options),
        term_count=term_count,
    )


def apply_vocabulary_confirmation_answer(
    repo_root: str | Path,
    workspace: str | Path,
    *,
    category: str,
    answer: str,
    custom_terms: list[str] | None = None,
) -> dict[str, Any]:
    """Apply user answer to a vocabulary category. Returns updated state.

    `answer` is one of: `accept_all`, `accept_high_confidence`, `edit`.
    When `answer == "edit"`, `custom_terms` REPLACES the category's terms.
    """
    layout = WorkspaceLayout(project_root=(Path(repo_root).resolve() / workspace).resolve())
    vocab_path = layout.contracts_dir / "workspace_vocabulary.json"
    if not vocab_path.exists():
        raise FileNotFoundError(
            f"workspace_vocabulary.json not found at {vocab_path}; "
            "run research first."
        )
    data = json.loads(vocab_path.read_text(encoding="utf-8"))
    categories = data.get("categories") or {}
    if category not in categories:
        raise ValueError(f"unknown category `{category}`")

    if answer == "accept_all":
        for term in categories[category]:
            if isinstance(term, dict):
                term["confidence"] = max(float(term.get("confidence") or 0), 1.0)
                term["accepted_by_user_at"] = _now()
    elif answer == "accept_high_confidence":
        kept = []
        for term in categories[category]:
            if isinstance(term, dict) and float(term.get("confidence") or 0) >= 0.7:
                term["accepted_by_user_at"] = _now()
                kept.append(term)
        categories[category] = kept
    elif answer == "edit":
        if not custom_terms:
            raise ValueError("answer=edit requires a non-empty custom_terms list")
        categories[category] = [
            {
                "term": str(t),
                "confidence": 1.0,
                "sample_values": [],
                "sources": ["user_edit"],
                "accepted_by_user_at": _now(),
            }
            for t in custom_terms
        ]
    else:
        raise ValueError(f"unknown answer `{answer}`")

    data["categories"] = categories
    data["needs_user_confirmation"] = any(
        not any(
            isinstance(t, dict) and t.get("accepted_by_user_at")
            for t in items
        )
        for items in categories.values()
    )
    data["updated_at"] = _now()
    vocab_path.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
    return {
        "workspace": str(layout.project_root.name),
        "category": category,
        "answer": answer,
        "needs_user_confirmation_after_apply": data["needs_user_confirmation"],
    }


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _rel(path: Path, root: Path) -> str:
    try:
        return path.resolve().relative_to(root.resolve()).as_posix()
    except ValueError:
        return path.as_posix()


def _render_panel_markdown(payload: dict[str, Any]) -> str:
    lines = [
        "# Workspace Vocabulary Confirmation",
        "",
        f"- Workspace: `{payload.get('workspace', '')}`",
        f"- Status: `{payload.get('status', '')}`",
        f"- Overall confidence: `{payload.get('overall_confidence', 0):.2f}`",
        "",
        "## Instruction",
        "",
        str(payload.get("instruction", "")),
        "",
    ]
    for category in payload.get("category_options") or []:
        lines.append(f"## `{category.get('category')}` ({category.get('term_count')} terms)")
        lines.append("")
        lines.append(f"**Question**: {category.get('question')}")
        lines.append("")
        lines.append("| Term | Confidence | Sample evidence | Source |")
        lines.append("| --- | --- | --- | --- |")
        for term in (category.get("terms") or [])[:10]:
            sample = ", ".join(str(v) for v in (term.get("sample_values") or [])[:3])
            source = (term.get("sources") or [""])[0]
            lines.append(
                f"| `{term.get('term')}` | {term.get('confidence', 0):.2f} | {sample or '—'} | `{source}` |"
            )
        lines.append("")
        lines.append("**Options:**")
        for opt in category.get("options") or []:
            lines.append(f"- `{opt.get('option_id')}` — {opt.get('label')}")
        lines.append("")
    if not payload.get("category_options"):
        lines.append("_No terms derived. The workspace may need a data dictionary or KPI registry first._")
        lines.append("")
    return "\n".join(lines).rstrip() + "\n"


__all__ = [
    "VocabularyConfirmationPanel",
    "apply_vocabulary_confirmation_answer",
    "prepare_vocabulary_confirmation_panel",
]
