"""Context-size estimator for active workspace sessions.

The orchestrating LLM (Gemini / Claude Code / Codex) has a finite chat
context. Today nothing tells the user how much of that budget has been
consumed by workspace artifacts loaded into the chat. This estimator
inspects the *external* artifacts the orchestrator is likely loading
and reports an upper-bound estimate in bytes.

It does NOT see the LLM's actual chat context — that's CLI-private.
What it gives is a deterministic upper bound based on workspace state:
the panel JSONs, recent trajectory tail, current session state, manifest,
and any skill bodies referenced by the active panel.

Generic. No CLI hardcoded.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from core.storage.workspace_layout import WorkspaceLayout


DEFAULT_BUDGET_KB = 180
WARN_THRESHOLD_PCT = 60
CRITICAL_THRESHOLD_PCT = 85


@dataclass
class ContextItem:
    label: str
    path: str
    size_bytes: int
    reason: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "label": self.label,
            "path": self.path,
            "size_bytes": self.size_bytes,
            "reason": self.reason,
        }


@dataclass
class ContextStatus:
    workspace: str
    session_id: str
    budget_kb: int
    estimated_kb: float
    pct_of_budget: float
    severity: str
    items: list[ContextItem] = field(default_factory=list)
    recommendation: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "workspace": self.workspace,
            "session_id": self.session_id,
            "budget_kb": self.budget_kb,
            "estimated_kb": self.estimated_kb,
            "pct_of_budget": self.pct_of_budget,
            "severity": self.severity,
            "recommendation": self.recommendation,
            "item_count": len(self.items),
            "items": [item.to_dict() for item in self.items],
        }


def _file_size(path: Path) -> int:
    try:
        return path.stat().st_size if path.exists() and path.is_file() else 0
    except OSError:
        return 0


def _trajectory_tail_size(path: Path, *, tail_bytes: int = 50_000) -> int:
    """Return min(file_size, tail_bytes) — orchestrators usually only read the tail."""
    size = _file_size(path)
    return min(size, tail_bytes)


def _skill_body_size(skill_name: str) -> int:
    from core.paths import PROJECT_ROOT

    path = PROJECT_ROOT / "skills" / skill_name / "SKILL.md"
    return _file_size(path)


def estimate_context(
    layout: WorkspaceLayout,
    *,
    session_id: str = "",
    budget_kb: int = DEFAULT_BUDGET_KB,
) -> ContextStatus:
    """Estimate the workspace-state context the orchestrator is likely holding."""
    items: list[ContextItem] = []

    manifest_path = layout.interns_dir / "MANIFEST.md"
    items.append(
        ContextItem(
            label="manifest",
            path=str(manifest_path),
            size_bytes=_file_size(manifest_path),
            reason="orchestrator typically reads MANIFEST.md once per session",
        )
    )

    readme_path = layout.interns_dir / "README.md"
    items.append(
        ContextItem(
            label="interns_readme",
            path=str(readme_path),
            size_bytes=_file_size(readme_path),
            reason="auto-loaded on every workspace-flow start",
        )
    )

    trajectory_path = layout.state_dir / "trajectory.jsonl"
    items.append(
        ContextItem(
            label="trajectory_tail",
            path=str(trajectory_path),
            size_bytes=_trajectory_tail_size(trajectory_path),
            reason="orchestrators typically tail-read trajectory for recent events",
        )
    )

    session_path: Path | None = None
    if session_id:
        session_path = layout.workflow_sessions_dir / session_id / "session.json"
        if session_path.exists():
            items.append(
                ContextItem(
                    label=f"session({session_id})",
                    path=str(session_path),
                    size_bytes=_file_size(session_path),
                    reason="active session state",
                )
            )
            panel_path = layout.workflow_sessions_dir / session_id / "current.json"
            if panel_path.exists():
                items.append(
                    ContextItem(
                        label="current_panel_json",
                        path=str(panel_path),
                        size_bytes=_file_size(panel_path),
                        reason="orchestrator renders the active panel inline",
                    )
                )

    required_skills: set[str] = set()
    suggested_skills: set[str] = set()
    if session_path and session_path.exists():
        try:
            state = json.loads(session_path.read_text(encoding="utf-8"))
            panel = state.get("current_panel") or {}
            summary = panel.get("summary") or {}
            for entry in summary.get("suggested_skills") or panel.get("suggested_skills") or []:
                name = entry.get("name") if isinstance(entry, dict) else entry
                if name:
                    suggested_skills.add(str(name))
            for spec_name in summary.get("required_specialists") or []:
                required_skills.add(str(spec_name))
        except (json.JSONDecodeError, OSError):
            pass
    for skill in sorted(suggested_skills | required_skills):
        size = _skill_body_size(skill)
        if size:
            items.append(
                ContextItem(
                    label=f"skill({skill})",
                    path=f"skills/{skill}/SKILL.md",
                    size_bytes=size,
                    reason="referenced by active panel; orchestrator loads on activation",
                )
            )

    blocker_panel = layout.reports_dir / "blocker_question_panel" / "current.md"
    items.append(
        ContextItem(
            label="open_blocker_panel",
            path=str(blocker_panel),
            size_bytes=_file_size(blocker_panel),
            reason="rendered inline when a KPI blocker is open",
        )
    )

    total_bytes = sum(item.size_bytes for item in items)
    estimated_kb = round(total_bytes / 1024, 1)
    pct = round((estimated_kb / max(1, budget_kb)) * 100, 1)
    if pct >= CRITICAL_THRESHOLD_PCT:
        severity = "critical"
        recommendation = (
            f"Estimated context ({estimated_kb} KB) is at {pct}% of the {budget_kb} KB budget. "
            "Run `workspace-flow handoff --workspace <ws>` and start a new session."
        )
    elif pct >= WARN_THRESHOLD_PCT:
        severity = "warning"
        recommendation = (
            f"Estimated context ({estimated_kb} KB) is at {pct}% of the {budget_kb} KB budget. "
            "Consider a handoff before opening more panels."
        )
    else:
        severity = "ok"
        recommendation = (
            f"Estimated context ({estimated_kb} KB) is well below the {budget_kb} KB budget."
        )

    return ContextStatus(
        workspace=layout.project_root.name,
        session_id=session_id,
        budget_kb=budget_kb,
        estimated_kb=estimated_kb,
        pct_of_budget=pct,
        severity=severity,
        items=items,
        recommendation=recommendation,
    )


__all__ = [
    "ContextItem",
    "ContextStatus",
    "DEFAULT_BUDGET_KB",
    "estimate_context",
]
