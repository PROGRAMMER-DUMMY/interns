"""Stage-triggered specialist delegation for workspace-flow.

The 14 subagents (`.claude/agents/`, `.gemini/agents/`, `.codex/agents/`)
are LLM personas that the orchestrating CLI must activate. This module
records, at each workflow stage, which specialist owns that stage and
what programmatic verdict the workflow already computed on its behalf.

Two layers of utilization are wired here:

1. **Programmatic verdict** — workspace-flow runs the validation /
   contract checks the specialist would run, captures the result, and
   stamps the delegation event with it. No LLM call required.

2. **Required-specialist hint** — the panel JSON carries
   `required_specialists` and `delegations` arrays. The orchestrator
   prompt (`.gemini/workspace-workflow-prompt.md` etc.) is updated
   to MUST-activate any listed specialist before answering. Lifts the
   utilization from "available but idle" to "fired at every stage".

Generic across workspaces. No agent names hardcoded outside this file.
"""
from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from core.storage.workspace_layout import WorkspaceLayout


DELEGATION_VERSION = 1


@dataclass(frozen=True)
class DelegationVerdict:
    """Programmatic result of running the specialist's contract checks."""

    status: str
    summary: str
    details: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class DelegationEvent:
    """One specialist delegation, recorded at a workflow stage."""

    agent: str
    stage: str
    reason: str
    started_at: str
    completed_at: str
    verdict: DelegationVerdict

    def to_dict(self) -> dict[str, Any]:
        return {
            "agent": self.agent,
            "stage": self.stage,
            "reason": self.reason,
            "started_at": self.started_at,
            "completed_at": self.completed_at,
            "verdict": asdict(self.verdict),
        }

    def to_trajectory_event(self, *, workspace_rel: str) -> dict[str, Any]:
        return {
            "artifact_type": "trajectory_event",
            "version": DELEGATION_VERSION,
            "event_type": "delegation",
            "workspace": workspace_rel,
            "agent": self.agent,
            "stage": self.stage,
            "reason": self.reason,
            "status": self.verdict.status,
            "summary": self.verdict.summary,
            "timestamp": self.completed_at,
            "metadata": {
                "started_at": self.started_at,
                "completed_at": self.completed_at,
                "details": self.verdict.details,
            },
        }


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def record_delegation(
    layout: WorkspaceLayout,
    workspace_rel: str,
    *,
    agent: str,
    stage: str,
    reason: str,
    verdict_fn: Callable[[], DelegationVerdict],
) -> DelegationEvent:
    """Run a programmatic verdict for a specialist + append the event to the trajectory log.

    Caller is responsible for embedding the returned event into the panel
    JSON (workspace-flow does this in `_advance_until_stop`).
    """
    started = _now()
    try:
        verdict = verdict_fn()
    except Exception as exc:
        verdict = DelegationVerdict(
            status="error",
            summary=f"Verdict function raised: {type(exc).__name__}: {exc}",
            details={"error": str(exc)},
        )
    completed = _now()
    event = DelegationEvent(
        agent=agent,
        stage=stage,
        reason=reason,
        started_at=started,
        completed_at=completed,
        verdict=verdict,
    )
    _append_trajectory(layout, event.to_trajectory_event(workspace_rel=workspace_rel))
    return event


def _append_trajectory(layout: WorkspaceLayout, event: dict[str, Any]) -> None:
    path = layout.state_dir / "trajectory.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        with path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(event, default=str))
            handle.write("\n")
    except OSError:
        pass


def render_delegation_markdown(events: list[DelegationEvent]) -> str:
    """Render a panel-friendly markdown block for a list of delegations."""
    if not events:
        return ""
    lines = ["## Specialist Reviews", ""]
    for event in events:
        lines.append(f"### `{event.agent}` — {event.stage}")
        lines.append("")
        lines.append(f"- **Verdict**: `{event.verdict.status}` — {event.verdict.summary}")
        lines.append(f"- **Reason for delegation**: {event.reason}")
        details = event.verdict.details or {}
        if details:
            for key, value in details.items():
                lines.append(f"- **{key}**: `{value}`")
        lines.append("")
    return "\n".join(lines)


def recent_delegations(layout: WorkspaceLayout, *, tail: int = 50) -> list[dict[str, Any]]:
    """Read the trajectory log and return the last N delegation events.

    Used by manifest writer + state consolidator to surface "what specialists
    fired recently in this workspace".
    """
    path = layout.state_dir / "trajectory.jsonl"
    if not path.exists():
        return []
    out: list[dict[str, Any]] = []
    try:
        with path.open("r", encoding="utf-8") as handle:
            for line in handle:
                line = line.strip()
                if not line:
                    continue
                try:
                    record = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if record.get("event_type") != "delegation":
                    continue
                out.append(record)
    except OSError:
        return out
    if tail and len(out) > tail:
        out = out[-tail:]
    return out


def verdict_from_relationship_summary(summary: dict[str, Any]) -> DelegationVerdict:
    """data-engineer verdict for the relationship-build stage."""
    rel_count = int(summary.get("relationship_count") or 0)
    exec_count = int(summary.get("executable_relationship_count") or 0)
    cand_count = int(summary.get("candidate_relationship_count") or 0)
    if rel_count == 0:
        return DelegationVerdict(
            status="warning",
            summary="No relationships built; multi-source KPIs cannot generate executable SQL.",
            details={"relationship_count": 0},
        )
    if cand_count == 0:
        return DelegationVerdict(
            status="ok",
            summary=f"{exec_count}/{rel_count} relationships executable; no pending candidates.",
            details={
                "relationship_count": rel_count,
                "executable_relationship_count": exec_count,
                "candidate_relationship_count": cand_count,
            },
        )
    return DelegationVerdict(
        status="needs_review",
        summary=f"{exec_count} executable, {cand_count} pending user approval.",
        details={
            "relationship_count": rel_count,
            "executable_relationship_count": exec_count,
            "candidate_relationship_count": cand_count,
        },
    )


def verdict_from_source_to_target_summary(summary: dict[str, Any]) -> DelegationVerdict:
    """source-to-target-reviewer verdict for the plan stage."""
    kpi_count = int(summary.get("kpi_count") or 0)
    ready = int(summary.get("ready_kpi_count") or 0)
    blocked = int(summary.get("blocked_kpi_count") or 0)
    if blocked:
        return DelegationVerdict(
            status="blocked",
            summary=f"{blocked}/{kpi_count} KPIs blocked; {ready} ready for SQL generation.",
            details={
                "kpi_count": kpi_count,
                "ready_kpi_count": ready,
                "blocked_kpi_count": blocked,
            },
        )
    return DelegationVerdict(
        status="ok",
        summary=f"All {kpi_count} KPIs ready for SQL generation.",
        details={"kpi_count": kpi_count, "ready_kpi_count": ready},
    )


def verdict_from_validation_summary(summary: dict[str, Any]) -> DelegationVerdict:
    """validation-gatekeeper verdict for the artifact-validation stage."""
    errors = int(summary.get("error_count") or 0)
    warnings = int(summary.get("warning_count") or 0)
    if errors:
        return DelegationVerdict(
            status="error",
            summary=f"{errors} validation error(s), {warnings} warning(s).",
            details={"error_count": errors, "warning_count": warnings},
        )
    if warnings:
        return DelegationVerdict(
            status="warning",
            summary=f"{warnings} validation warning(s); no errors.",
            details={"warning_count": warnings},
        )
    return DelegationVerdict(
        status="ok",
        summary="All workspace artifacts valid.",
        details={},
    )


def verdict_from_kpi_completion(entries: list[dict[str, Any]]) -> DelegationVerdict:
    """kpi-analyst verdict for the completion stage."""
    total = len(entries or [])
    ok = sum(1 for e in entries if str(e.get("status")) == "ok")
    failed = total - ok
    if total == 0:
        return DelegationVerdict(
            status="warning",
            summary="No KPI SQL artifacts found to validate.",
            details={"kpi_count": 0},
        )
    if failed:
        return DelegationVerdict(
            status="partial",
            summary=f"{ok}/{total} KPIs produced result views; {failed} failed.",
            details={
                "kpi_count": total,
                "ok_count": ok,
                "failed_count": failed,
                "failed_kpi_ids": [e.get("kpi_id") for e in entries if str(e.get("status")) != "ok"],
            },
        )
    return DelegationVerdict(
        status="ok",
        summary=f"All {total} KPIs produced result views with definition + SQL + preview.",
        details={"kpi_count": total},
    )


def verdict_from_dashboard_summary(summary: dict[str, Any]) -> DelegationVerdict:
    """dashboard-engineer verdict for the dashboard-refresh stage."""
    kpi_count = int(summary.get("kpi_count") or 0)
    spec_paths = summary.get("spec_paths") or []
    if not kpi_count:
        return DelegationVerdict(
            status="warning",
            summary="No KPI specs written (KPI registry empty?).",
            details={},
        )
    return DelegationVerdict(
        status="ok",
        summary=f"{kpi_count} KPI specs written; index regenerated.",
        details={"kpi_count": kpi_count, "spec_count": len(spec_paths)},
    )


__all__ = [
    "DelegationEvent",
    "DelegationVerdict",
    "recent_delegations",
    "record_delegation",
    "render_delegation_markdown",
    "verdict_from_dashboard_summary",
    "verdict_from_kpi_completion",
    "verdict_from_relationship_summary",
    "verdict_from_source_to_target_summary",
    "verdict_from_validation_summary",
]
