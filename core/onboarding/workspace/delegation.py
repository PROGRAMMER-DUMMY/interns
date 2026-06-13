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
class DelegationRequest:
    """Briefing the orchestrator can paste directly into a subagent spawn.

    Lifts a delegation from "verdict recorded programmatically" to
    "ready-to-invoke subagent prompt" — the orchestrator no longer has
    to construct the brief itself.
    """

    agent: str
    prompt: str
    context_keys: list[str]
    expected_return: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "agent": self.agent,
            "prompt": self.prompt,
            "context_keys": self.context_keys,
            "expected_return": self.expected_return,
        }


@dataclass(frozen=True)
class DelegationEvent:
    """One specialist delegation, recorded at a workflow stage."""

    agent: str
    stage: str
    reason: str
    started_at: str
    completed_at: str
    verdict: DelegationVerdict
    delegation_request: DelegationRequest | None = None
    handoff_path: str = ""

    def to_dict(self) -> dict[str, Any]:
        out = {
            "agent": self.agent,
            "stage": self.stage,
            "reason": self.reason,
            "started_at": self.started_at,
            "completed_at": self.completed_at,
            "verdict": asdict(self.verdict),
        }
        if self.delegation_request is not None:
            out["delegation_request"] = self.delegation_request.to_dict()
        if self.handoff_path:
            out["handoff_path"] = self.handoff_path
        return out

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


_STAGE_BRIEFS: dict[str, dict[str, Any]] = {
    "relationship_review": {
        "context_keys": [
            "summary.diff.executable_relationship_ids",
            "summary.diff.pending_relationship_ids",
            "interns/generated/contracts/relationship_contracts.json",
        ],
        "expected_return": "verdict: ok / needs_review / blocked + specific relationship_ids that still need approval",
        "prompt_template": (
            "Act as the data-engineer specialist. Review the relationship contracts for workspace "
            "`{workspace}`. Programmatic verdict already captured: `{verdict_status}` — {verdict_summary}. "
            "Use `interns/generated/contracts/relationship_contracts.json` as the source of truth. "
            "Return either (a) confirmation that the programmatic verdict is correct, or (b) a list of "
            "relationship_ids that need additional review beyond what the verdict captured."
        ),
    },
    "source_to_target_review": {
        "context_keys": [
            "summary.kpi_count",
            "summary.blocked_kpi_count",
            "interns/generated/contracts/source_to_target_plan.json",
        ],
        "expected_return": "verdict: ok / blocked + per-KPI blocker codes for any kpi the verdict flagged",
        "prompt_template": (
            "Act as the source-to-target-reviewer specialist. Review the plan for workspace "
            "`{workspace}`. Programmatic verdict: `{verdict_status}` — {verdict_summary}. Inspect "
            "`interns/generated/contracts/source_to_target_plan.json` and confirm each KPI's "
            "selected_source_datasets + join_plan + grain match the KPI's cuts. Surface anything the "
            "programmatic verdict missed."
        ),
    },
    "artifact_validation": {
        "context_keys": [
            "summary.validation.error_count",
            "summary.validation.warning_count",
            "interns/reports/open_questions.md",
        ],
        "expected_return": "verdict: ok / warning / error + specific artifact paths to fix",
        "prompt_template": (
            "Act as the validation-gatekeeper specialist. Review artifact validation for workspace "
            "`{workspace}`. Programmatic verdict: `{verdict_status}` — {verdict_summary}. Confirm "
            "the verdict or list any cross-artifact inconsistencies the static validator might have "
            "missed (e.g. a KPI references a column not present in profile_index)."
        ),
    },
    "kpi_completion_review": {
        "context_keys": [
            "summary.completed_kpis[*].kpi_id",
            "summary.completed_kpis[*].definition",
            "summary.completed_kpis[*].sql_text",
            "summary.completed_kpis[*].preview_markdown",
        ],
        "expected_return": "verdict: ok / partial + per-KPI interpretation issues (e.g. SQL aggregates the wrong column)",
        "prompt_template": (
            "Act as the kpi-analyst specialist. Review every completed KPI in workspace `{workspace}`. "
            "Programmatic verdict: `{verdict_status}` — {verdict_summary}. For each KPI under "
            "`summary.completed_kpis`, confirm the SQL matches the business_question + metric + cuts. "
            "Flag any KPI where the rendered result table would mislead a stakeholder."
        ),
    },
    "dashboard_refresh": {
        "context_keys": [
            "summary.kpi_count",
            "workspaces/<ws>/dashboard/*.json",
        ],
        "expected_return": "verdict: ok / partial + per-KPI chart issues (e.g. chart type doesn't match metric shape)",
        "prompt_template": (
            "Act as the dashboard-engineer specialist. Review dashboard specs for workspace `{workspace}`. "
            "Programmatic verdict: `{verdict_status}` — {verdict_summary}. For each spec under "
            "`dashboard/*.json`, confirm the inferred chart_type matches the KPI shape and the spec "
            "preserves any user_overrides already set."
        ),
    },
    "kpi_output_verification": {
        "context_keys": [
            "interns/reports/kpi_output_verification.md",
            "interns/generated/evidence/kpi_output_verification.json",
            "summary.completed_kpis",
        ],
        "expected_return": (
            "verdict: ok / blocked + per-KPI judgement on whether the executed result rows actually "
            "answer the business question (semantic correctness, not just 'it ran')"
        ),
        "prompt_template": (
            "Act as the kpi-analyst specialist (grill-requirements self-grill mode) for workspace `{workspace}`. The self-grill "
            "gate already EXECUTED each KPI and cross-checked metric, cuts, filters, and cross-engine "
            "parity: `{verdict_status}` — {verdict_summary}. Read "
            "`interns/reports/kpi_output_verification.md`. For each KPI, judge whether the actual result "
            "rows answer the original business question (beyond merely executing), and flag any result "
            "that would mislead a stakeholder."
        ),
    },
}


# Canonical routing: which specialists and which skills own each workflow stage.
# This is the single source of truth that keeps the FULL agent + skill roster
# reachable. Panels/flows attach `routing_for(stage)` so the orchestrator (and the
# user) stay in control of which to activate — nothing is force-run. The coverage
# test (tests/test_agent_skill_routing.py) fails if any agent or skill is unrouted.
STAGE_ROUTING: dict[str, dict[str, list[str]]] = {
    "flow_entry": {
        # agent-advisor-router removed (Phase 1): STAGE_ROUTING itself is the
        # router; a meta-router agent on top of it was redundant.
        "agents": ["workspace-flow-orchestrator"],
        "skills": ["workspace-governance", "task-onboarding", "handoff"],
    },
    "kpi_definition": {
        "agents": ["business-analyst", "kpi-analyst"],
        "skills": ["grill-requirements", "kpi-clarification",
                   "stakeholder-memory", "to-solution-brief", "workspace-kpi-query-optimizer"],
    },
    "data_model_design": {
        "agents": ["data-engineer", "source-to-target-reviewer"],
        "skills": ["data-model-creation", "domain-model", "grill-requirements"],
    },
    "relationship_review": {
        "agents": ["data-engineer"],
        "skills": ["domain-model"],
    },
    "source_to_target_review": {
        "agents": ["source-to-target-reviewer", "sql-polars-pyspark-specialist"],
        "skills": ["data-engineering-pipeline-design"],
    },
    "engine_generation": {
        "agents": ["sql-polars-pyspark-specialist"],
        "skills": ["data-engineering-pipeline-design", "feature-derivation-library"],
    },
    "feature_derivation": {
        # The skill owns the pattern library; a same-named agent was a literal
        # twin (Phase 1 consolidation). data-analyst judges the evidence.
        "agents": ["data-analyst"],
        "skills": ["feature-derivation-library"],
    },
    "artifact_validation": {
        "agents": ["validation-gatekeeper"],
        "skills": ["workspace-governance"],
    },
    "regression_review": {
        # regression-sweep agent removed (Phase 1): it wrapped one CLI call
        # (green-gate --sweep); validation-gatekeeper owns running/interpreting
        # validation suites.
        "agents": ["validation-gatekeeper"],
        "skills": ["green-gate"],
    },
    "parallel_kpi_completion": {
        "agents": ["workspace-flow-orchestrator", "kpi-analyst"],
        "skills": ["workspace-governance", "kpi-clarification", "feature-derivation-library"],
    },
    "kpi_completion_review": {
        "agents": ["kpi-analyst"],
        "skills": ["kpi-analyst", "grill-requirements"],
    },
    "kpi_output_verification": {
        "agents": ["kpi-analyst", "validation-gatekeeper"],
        "skills": ["kpi-analyst", "grill-requirements"],
    },
    "result_review": {
        "agents": ["data-analyst"],
        "skills": ["grill-requirements"],
    },
    "dashboard_refresh": {
        "agents": ["dashboard-engineer"],
        "skills": ["dashboard-design"],
    },
    "remote_execution": {
        # databricks-access-gates agent removed (Phase 1): it twinned the
        # same-named skill; databricks-engineer carries the role.
        "agents": ["databricks-engineer"],
        "skills": ["databricks-access-gates"],
    },
    "notification": {
        # integration-notification-operator removed (Phase 1): nothing in the
        # repo invoked it (speculative Slack/Teams bridge).
        "agents": [],
        "skills": [],
    },
    "evolution": {
        "agents": [],
        "skills": ["evolution"],
    },
}


def routing_for(stage: str) -> dict[str, list[str]]:
    """Return {'agents': [...], 'skills': [...]} that own a workflow stage.

    Panels call this to attach `required_specialists` + `suggested_skills` so the
    orchestrator activates the right roster for the stage (never force-run).
    """
    entry = STAGE_ROUTING.get(stage) or {}
    return {"agents": list(entry.get("agents") or []), "skills": list(entry.get("skills") or [])}


def _build_delegation_request(
    agent: str, stage: str, workspace: str, verdict: DelegationVerdict
) -> DelegationRequest:
    brief = _STAGE_BRIEFS.get(stage) or {}
    template = str(brief.get("prompt_template") or
                   f"Act as the `{agent}` specialist for workspace `{workspace}`. "
                   f"Verdict: `{verdict.status}` — {verdict.summary}. "
                   "Review the relevant artifacts and confirm or extend the verdict.")
    prompt = template.format(
        workspace=workspace,
        verdict_status=verdict.status,
        verdict_summary=verdict.summary,
    )
    return DelegationRequest(
        agent=agent,
        prompt=prompt,
        context_keys=list(brief.get("context_keys") or []),
        expected_return=str(brief.get("expected_return") or "verdict + actionable next step"),
    )


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
    request = _build_delegation_request(agent, stage, workspace_rel, verdict)
    # Materialize a compact handoff doc the side agent reads, so the orchestrator
    # transfers context by reference (file path) instead of pasting a long brief —
    # keeping the main orchestrator's own context lean.
    handoff_path = _write_delegation_handoff(layout, workspace_rel, agent, stage, request, verdict)
    event = DelegationEvent(
        agent=agent,
        stage=stage,
        reason=reason,
        started_at=started,
        completed_at=completed,
        verdict=verdict,
        delegation_request=request,
        handoff_path=handoff_path,
    )
    _append_trajectory(layout, event.to_trajectory_event(workspace_rel=workspace_rel))
    return event


def _write_delegation_handoff(
    layout: WorkspaceLayout,
    workspace_rel: str,
    agent: str,
    stage: str,
    request: DelegationRequest | None,
    verdict: DelegationVerdict,
) -> str:
    """Write a per-delegation handoff doc (task + artifacts-by-reference + expected
    return + verdict + suggested skills). The side agent reads this file; the
    orchestrator only passes its path. Returns the repo-relative path ("" on failure)."""
    if request is None:
        return ""
    handoffs = layout.state_dir / "handoffs"
    skills = routing_for(stage).get("skills", [])
    lines = [
        f"# Handoff -> {agent} ({stage})",
        "",
        f"- Workspace: `{workspace_rel}`",
        f"- Programmatic verdict: `{verdict.status}` - {verdict.summary}",
        "",
        "## Task",
        "",
        request.prompt,
        "",
        "## Read these (referenced, not pasted)",
        "",
    ]
    lines.extend(f"- `{key}`" for key in request.context_keys)
    if not request.context_keys:
        lines.append("- (none)")
    lines += ["", "## Expected return", "", request.expected_return, ""]
    if skills:
        lines += ["## Suggested skills", "", ", ".join(f"`{s}`" for s in skills), ""]
    try:
        handoffs.mkdir(parents=True, exist_ok=True)
        path = handoffs / f"{stage}__{agent}.md"
        path.write_text("\n".join(lines), encoding="utf-8")
    except OSError:
        return ""
    rel = path.relative_to(layout.project_root).as_posix()
    return f"{workspace_rel}/{rel}" if workspace_rel else rel


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


def verdict_from_verification(summary: dict[str, Any]) -> DelegationVerdict:
    """self-grill verdict from an executed KPIOutputVerifier run.

    `ok` only when every KPI executed AND matched its metric/cuts/filters (and any
    cross-engine check that ran); otherwise `blocked` with the failing KPI errors.
    """
    records = summary.get("records") or []
    if not records:
        return DelegationVerdict(
            status="warning",
            summary="No generated KPI SQL to self-grill yet.",
            details={"kpi_count": 0},
        )
    blocked = [r for r in records if str(r.get("status")) != "passed"]
    total = int(summary.get("kpi_count") or len(records))
    passed = int(summary.get("passed_count") or (total - len(blocked)))
    if not blocked:
        return DelegationVerdict(
            status="ok",
            summary=f"All {passed} KPI outputs executed and matched intent (+ cross-engine where checked).",
            details={"kpi_count": total, "passed_count": passed},
        )
    return DelegationVerdict(
        status="blocked",
        summary=f"{len(blocked)}/{total} KPI outputs failed self-grill (execution or intent mismatch).",
        details={
            "kpi_count": total,
            "blocked_kpi_ids": [r.get("kpi_id") for r in blocked],
            "errors": {r.get("kpi_id"): r.get("errors") for r in blocked},
        },
    )


def verdict_from_kpi_definition(
    low_confidence_count: int, kpi_count: int
) -> DelegationVerdict:
    """business-analyst verdict for the kpi_definition stage.

    Surfaces KPIs whose intent facets (metric / grain / denominator_scope /
    temporal_anchor) parsed with low confidence — the ambiguity a blocker-feature
    count alone misses (e.g. an ambiguous share denominator)."""
    if kpi_count == 0:
        return DelegationVerdict(
            status="warning",
            summary="No KPI definitions found to assess.",
            details={"kpi_count": 0},
        )
    if low_confidence_count:
        return DelegationVerdict(
            status="needs_review",
            summary=(
                f"{low_confidence_count} KPI intent facet(s) are low-confidence "
                "(ambiguous metric/grain/denominator/anchor) and need a governed definition."
            ),
            details={"kpi_count": kpi_count, "low_confidence_facets": low_confidence_count},
        )
    return DelegationVerdict(
        status="ok",
        summary=f"All {kpi_count} KPI definition(s) parsed with confident intent facets.",
        details={"kpi_count": kpi_count},
    )


def verdict_from_engine_generation(
    generated: list[dict[str, Any]], harness_ok: bool
) -> DelegationVerdict:
    """sql-polars-pyspark-specialist verdict for the engine_generation stage.

    The engine (SQL/Polars/PySpark) was selected + implemented for each KPI; the
    verdict confirms the chosen engine actually executed via the harness."""
    total = len(generated or [])
    if total == 0:
        return DelegationVerdict(
            status="warning",
            summary="No KPI engine artifacts were generated.",
            details={"kpi_count": 0},
        )
    if not harness_ok:
        return DelegationVerdict(
            status="blocked",
            summary=f"{total} KPI engine(s) generated but the execution harness failed.",
            details={"kpi_count": total, "harness_ok": False},
        )
    return DelegationVerdict(
        status="ok",
        summary=f"{total} KPI engine(s) generated and passed the execution harness.",
        details={"kpi_count": total, "harness_ok": True},
    )


def verdict_from_result_review(entries: list[dict[str, Any]]) -> DelegationVerdict:
    """data-analyst verdict for the result_review stage.

    Confirms every KPI produced result rows for interpretation; flags any KPI that
    executed but returned no rows (a readiness/anomaly signal worth a human look)."""
    total = len(entries or [])
    if total == 0:
        return DelegationVerdict(
            status="warning",
            summary="No KPI results available to review.",
            details={"kpi_count": 0},
        )
    produced = sum(1 for e in entries if str(e.get("status")) == "ok")
    if produced < total:
        return DelegationVerdict(
            status="needs_review",
            summary=f"{produced}/{total} KPIs produced result rows; review the remainder for anomalies.",
            details={
                "kpi_count": total,
                "produced_count": produced,
                "no_result_kpi_ids": [
                    e.get("kpi_id") for e in entries if str(e.get("status")) != "ok"
                ],
            },
        )
    return DelegationVerdict(
        status="ok",
        summary=f"All {total} KPIs produced result rows for business interpretation.",
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
    "verdict_from_engine_generation",
    "verdict_from_kpi_completion",
    "verdict_from_kpi_definition",
    "verdict_from_relationship_summary",
    "verdict_from_result_review",
    "verdict_from_source_to_target_summary",
    "verdict_from_validation_summary",
    "verdict_from_verification",
]
