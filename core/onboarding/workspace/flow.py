"""User-facing workspace workflow sessions.

This module is the quiet front door for agent UIs.  It runs the existing
governed tools in-process, persists a small session record, and returns compact
panels instead of streaming every lower-level command to the main chat.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import re
from dataclasses import asdict, dataclass
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any

from core.paths import PROJECT_ROOT
from core.onboarding.bronze_silver_standards import BronzeSilverStandardsBuilder
from core.onboarding.data_model.data_understanding import (
    classify_quality_tier,
    classify_schema_type,
    scoped_processing_options,
)
from core.onboarding.data_quality import DataQualityHarness, DuplicateDecisionRecorder, DuplicateReviewPanel
from core.onboarding.harness.trajectory_recorder import record_trajectory_event_safe
from core.onboarding.kpi.blocker_workflow import apply_kpi_panel_answer, prepare_kpi_blocker_panel
from core.onboarding.kpi.engine_parity import PARITY_MODE_ROW, run_polars_parity
from core.onboarding.kpi.execution_harness import KPIExecutionHarness
from core.onboarding.kpi.generation_workflow import KPIGenerationWorkflow
from core.onboarding.kpi.registry_loader import load_kpi_definitions, render_kpi_block
from core.wiki import WikiLayout, build_kpi_completion_scaffold, upsert_kpi_note
from core.dashboard import refresh_workspace_dashboard
from core.onboarding.workspace.delegation import (
    DelegationEvent,
    DelegationVerdict,
    STAGE_ROUTING,
    record_delegation,
    routing_for,
    verdict_from_dashboard_summary,
    verdict_from_engine_generation,
    verdict_from_kpi_completion,
    verdict_from_kpi_definition,
    verdict_from_relationship_summary,
    verdict_from_result_review,
    verdict_from_source_to_target_summary,
    verdict_from_validation_summary,
    verdict_from_verification,
)
from core.onboarding.kpi.sql_generator import DuckDBKPISQLGenerator
from core.onboarding.pipeline_plan import DataEngineeringRoutePlanner
from core.onboarding.relationships.contracts import RelationshipContractBuilder
from core.onboarding.relationships.source_to_target_planner import SourceToTargetPlanner
from core.onboarding.workspace.onboarding import WorkspaceOnboarder
from core.onboarding.workspace.validation import WorkspaceArtifactValidator
from core.onboarding.workspace.workflow import MODES as ORCHESTRATION_MODES
from core.onboarding.workspace.workflow import WorkspaceWorkflowOrchestrator
from core.presentation.console_tables import render_markdown_table, render_query_result_table
from core.onboarding.kpi.pii_redaction import (
    redact_table_rows,
    workspace_aggregate_ages,
    workspace_redaction_patterns,
)
from core.storage.workspace_layout import WorkspaceLayout
from core.onboarding.workspace.flow_io import _read_json
from core.onboarding.workspace.flow_panels import (
    _compact_panel,
    _render_panel_markdown,
    _build_kpi_resolution_review,
    _render_resolution_review,
    _build_hidden_panel_harness,
    _panel_check,
    _source_of_truth,
    _extract_source_filters,
    _summarize_source_logic,
    _term_rows,
    _summarize_current_data_model,
    _summarize_current_kpi_set,
    _render_data_understanding_markdown,
    _render_results_markdown,
    _result_view,
)
from tools.artifact_inventory import (
    gitignore_patterns as artifact_gitignore_patterns,
    inventory as inventory_artifacts,
    render_inventory_markdown as render_artifact_inventory_markdown,
    write_manifest as write_artifact_manifest,
)
from tools.list_workspace_files import list_workspace_files
from tools.state_consolidator import consolidate_all as consolidate_state_all
from tools.workspace_gc import (
    DEFAULT_MAX_LOG_MB,
    DEFAULT_MAX_SESSION_AGE_HOURS,
    garbage_collect as gc_workspace,
)


FLOW_VERSION = 1
INTENTS = {"kpi_generation", "usual_workflow", "full_kpi_sql"}

# Maximum number of rows shown in the KPI result preview packet.
# Raising this constant is the only knob needed to widen the preview;
# the truncation note ([~] N more rows ...) is appended automatically.
PREVIEW_ROW_CAP = 10


@dataclass(frozen=True)
class WorkspaceFlowResult:
    session_id: str
    workspace: str
    state_path: str
    stage: str
    status: str
    current_panel_path: str
    current_markdown_path: str
    next_step: str

    def summary(self) -> dict[str, Any]:
        return asdict(self)


class WorkspaceFlow:
    def __init__(
        self,
        repo_root: str | Path,
        workspace: str | Path,
        *,
        domain: str = "generic",
        session_id: str = "",
        orchestration_mode: str = "local-safe",
    ) -> None:
        if orchestration_mode not in ORCHESTRATION_MODES:
            raise ValueError(f"unsupported orchestration mode: {orchestration_mode}")
        self.repo_root = Path(repo_root).resolve()
        self.workspace = (self.repo_root / workspace).resolve()
        self.workspace_rel = _rel(self.workspace, self.repo_root)
        self.layout = WorkspaceLayout(project_root=self.workspace)
        self.domain = domain
        self.session_id = session_id or _new_session_id()
        self.orchestration_mode = orchestration_mode

    @classmethod
    def from_session(
        cls,
        repo_root: str | Path,
        session_id: str,
    ) -> "WorkspaceFlow":
        root = Path(repo_root).resolve()
        state = _find_session(root, session_id)
        return cls(
            root,
            state["workspace"],
            domain=state.get("domain", "healthcare"),
            session_id=session_id,
            orchestration_mode=state.get("orchestration_mode", "local-safe"),
        )

    def start(self, *, intent: str = "kpi_generation", mode: str | None = None) -> WorkspaceFlowResult:
        self._validate_workspace()
        if mode is not None:
            if mode not in ORCHESTRATION_MODES:
                raise ValueError(f"unsupported orchestration mode: {mode}")
            self.orchestration_mode = mode
        if intent not in INTENTS:
            raise ValueError(f"unsupported intent: {intent}")
        self.layout.ensure_runtime_dirs()
        state = self._base_state(intent)
        self._record_event(
            "workflow_start",
            "running",
            f"Started workspace-flow with intent `{intent}` and mode `{self.orchestration_mode}`.",
        )
        listing = list_workspace_files(self.repo_root, self.workspace_rel).to_dict()
        self._record_step(state, "list_workspace_files", "ok", {"file_count": len(listing.get("files", []))})

        if intent != "kpi_generation" and self.orchestration_mode == "plan":
            return self._workflow_checkpoint(state, mode="plan")

        if intent == "kpi_generation":
            result = KPIGenerationWorkflow(self.repo_root, self.workspace_rel).prepare()
            panel = _read_json(self.repo_root / result.current_json_path)
            self._record_step(state, "prepare_kpi_generation", "ok", result.summary())
            return self._save_panel(
                state,
                panel=_compact_panel(
                    stage="kpi_generation_route",
                    status="needs_user_answer",
                    source_panel=panel,
                    instruction="Choose whether to generate/revise KPIs by interview or use the existing KPI workflow.",
                    artifact_paths=[result.current_json_path, result.current_markdown_path],
                ),
                source="kpi_generation",
            )

        return self._advance_until_stop(state)

    def answer(
        self,
        *,
        answer: str,
        custom_definition: str = "",
        evidence_note: str = "",
    ) -> WorkspaceFlowResult:
        state = self._load_state()
        current = state.get("current_panel", {})
        source = current.get("source", "")
        if source == "kpi_generation":
            result = KPIGenerationWorkflow(self.repo_root, self.workspace_rel).apply_answer(answer=answer)
            panel = _read_json(self.repo_root / result.current_json_path)
            self._record_step(
                state,
                "apply_kpi_generation_answer",
                "ok",
                result.summary(),
                decision=answer,
            )
            if result.stage == "usual_workflow_selected":
                state["intent"] = "usual_workflow"
                return self._advance_until_stop(state)
            return self._save_panel(
                state,
                panel=_compact_panel(
                    stage=f"kpi_generation_{result.stage}",
                    status="needs_user_answer" if result.status == "awaiting_user_answer" else result.status,
                    source_panel=panel,
                    instruction=_panel_instruction(panel),
                    artifact_paths=[result.current_json_path, result.current_markdown_path],
                ),
                source="kpi_generation",
            )

        if source == "kpi_blocker":
            result = apply_kpi_panel_answer(
                self.repo_root,
                self.workspace_rel,
                answer=answer,
                domain=self.domain,
                custom_definition=custom_definition,
                evidence_note=evidence_note,
            )
            self._record_step(state, "apply_kpi_panel_answer", "ok", result.summary(), decision=answer)
            return self._advance_until_stop(state)

        if source == "duplicate_review":
            result = DuplicateDecisionRecorder(self.repo_root, self.workspace_rel).apply(
                answer,
                custom_rule=custom_definition or evidence_note,
            )
            self._record_step(state, "apply_duplicate_review_answer", "ok", result, decision=answer)
            return self._advance_until_stop(state)

        # BUG-021: when the session is blocked on join_proof_missing
        # (`source="source_to_target"`, status="blocked"), out-of-band relationship
        # approvals (e.g. `apply-relationship-answer --answer approve`) happen
        # outside the session and do NOT automatically re-check the plan.
        # Accepting `answer="continue"` here re-evaluates the current relationship
        # contracts and either advances the flow (when joins are now executable) or
        # surfaces the updated blocked state.  This also handles the case where
        # `workspace-flow answer --answer continue` is called after approvals so the
        # caller does not have to mint a brand-new session just to unblock.
        if source == "source_to_target" and answer.strip().lower() == "continue":
            self._record_step(
                state,
                "answer_continue_source_to_target",
                "ok",
                {"answer": answer, "note": "BUG-021: re-evaluating blocker state after out-of-band relationship approval"},
                decision=answer,
            )
            return self._advance_until_stop(state)

        raise ValueError("current workflow stage is not waiting for a supported answer")

    def review(
        self,
        *,
        verdict: str,
        summary: str = "",
        per_kpi: list[dict[str, Any]] | None = None,
        confirmed_by: str = "",
    ) -> WorkspaceFlowResult:
        """Record the kpi-analyst's SEMANTIC verdict and re-advance the flow.

        This is the record-back half of the completion hard gate: the orchestrator
        invokes the kpi-analyst agent/skill, judges whether each generated KPI
        actually answers its intent, then posts the verdict here. The verdict is
        bound to a signature of the CURRENT generated KPIs, so a later regeneration
        that changes the SQL invalidates a stale review and re-gates completion.

        BUG-014: ``confirmed_by`` records the name/identity of the confirming
        party when provided.  Without it the verdict defaults to ``source: agent``
        (machine-asserted).  Pass a non-empty string (e.g. the user's name or
        ``"human"``) to mark the verdict as ``source: human``.
        """
        if verdict not in {"ok", "blocked"}:
            raise ValueError("review verdict must be 'ok' or 'blocked'")
        state = self._load_state()
        # BUG-020: Use the kpi_signature already embedded in the current panel's
        # summary (set by _advance_until_stop when it emitted the gate panel) so
        # the stored review always carries the SAME signature that _advance_until_stop
        # will recompute on this call.  Previously the signature was recomputed from
        # the old completed_kpis list, which could differ after a re-run and cause
        # review_current to be False on the first valid call.
        panel_summary = ((state.get("current_panel") or {}).get("summary") or {})
        stored_kpi_signature: str = str(panel_summary.get("kpi_signature") or "")
        if not stored_kpi_signature:
            # Fallback: compute from completed_kpis if the panel predates BUG-020 fix.
            completed = panel_summary.get("completed_kpis") or []
            stored_kpi_signature = _kpi_review_signature(completed)
        # BUG-014: record provenance so downstream can distinguish agent-asserted
        # verdicts from human-confirmed ones. An agent identity in confirmed_by
        # (e.g. "agent"/"claude") is agent-asserted, NOT human.
        from core.governance.provenance import decision_source as _decision_source

        source = _decision_source(confirmed_by)
        recorded = {
            "verdict": verdict,
            "summary": summary,
            "per_kpi": per_kpi or [],
            "kpi_signature": stored_kpi_signature,
            "source": source,
            "confirmed_by": confirmed_by or "",
            "recorded_at": _now(),
        }
        state["kpi_analyst_review"] = recorded
        self._record_step(
            state, "record_kpi_analyst_review", verdict, recorded, decision=verdict,
        )
        # Stamp a delegation event carrying the ACTUAL kpi-analyst verdict (not a
        # programmatic stand-in), so the trajectory shows the specialist fired.
        self._delegate_and_record(
            state,
            agent="kpi-analyst",
            stage="kpi_output_verification",
            reason="kpi-analyst semantic review posted back by orchestrator",
            verdict_fn=lambda: DelegationVerdict(
                status=verdict,
                summary=summary or "kpi-analyst semantic review recorded",
                details={"per_kpi": per_kpi or [], "source": source, "confirmed_by": confirmed_by or ""},
            ),
        )
        return self._advance_until_stop(state)

    def status(self) -> WorkspaceFlowResult:
        state = self._load_state()
        return self._result_from_state(state)

    def diff(self) -> dict[str, Any]:
        """Read existing artifacts and report per-KPI gaps without re-running.

        Returns a generic structured diff: per-KPI list of missing features,
        missing relationships, validation failures, and the exact recovery
        commands the agent should run. Designed to replace the "re-run
        workspace-flow start to see what's missing" loop pattern.
        """
        return compute_workflow_diff(self.repo_root, self.workspace_rel)

    def results(self, *, preview_rows: int = PREVIEW_ROW_CAP) -> WorkspaceFlowResult:
        state = self._load_state()
        preview = self._write_result_preview(preview_rows=preview_rows)
        self._record_step(state, "preview_kpi_results", "ok", preview)
        return self._save_panel(
            state,
            panel={
                "stage": "results",
                "status": "complete",
                "instruction": "Review generated KPI SQL and result previews.",
                "question": "",
                "options": [],
                "recommended_option_id": "",
                "artifact_paths": [preview["json_path"], preview["markdown_path"]],
                "summary": preview,
            },
            source="results",
        )

    def _advance_until_stop(self, state: dict[str, Any]) -> WorkspaceFlowResult:
        if not (self.layout.contracts_dir / "kpi_registry.json").exists():
            onboarding = WorkspaceOnboarder(self.repo_root, self.workspace_rel).run()
            self._record_step(state, "onboard_workspace", "ok", onboarding.summary())

        # BUG-010 data-understanding gate: classify the data quality tier + schema type
        # from generated profiles BEFORE any irreversible KPI/SQL generation, and surface
        # scoped processing options. Additive and non-blocking — it records its decision
        # and lets the existing flow proceed.
        self._run_data_understanding_gate(state)

        if self.orchestration_mode == "plan":
            return self._workflow_checkpoint(state, mode="plan")

        standards = BronzeSilverStandardsBuilder(self.repo_root, self.workspace_rel, domain=self.domain).build()
        self._record_step(state, "prepare_bronze_silver_standards", "ok", standards.summary())
        data_quality = self._run_data_quality_gate(state)
        if data_quality.get("blocked"):
            orchestration_context = self._prepare_layer_route(
                state,
                data_quality=data_quality.get("harness", {}),
            )
            orchestration_context["blocked_before"] = "kpi_blocker"
            duplicate = data_quality["duplicate_review"]
            panel = _read_json(self.repo_root / duplicate["current_json_path"])
            if self.orchestration_mode == "autopilot" and _safe_duplicate_option(panel):
                result = DuplicateDecisionRecorder(self.repo_root, self.workspace_rel).apply("option_a")
                self._record_step(state, "apply_duplicate_review_answer", "ok", result, decision="option_a")
                return self._advance_until_stop(state)
            return self._save_panel(
                state,
                panel=_compact_panel(
                    stage="data_quality_duplicate_review",
                    status="needs_user_answer",
                    source_panel=panel,
                    instruction=(
                        "Data quality runs immediately after onboarding. Resolve this duplicate/grain "
                        "decision before KPI blocker resolution or executable generation continues."
                    ),
                    artifact_paths=[
                        data_quality["harness"]["current_json_path"],
                        data_quality["harness"]["current_markdown_path"],
                        duplicate["current_json_path"],
                        duplicate["current_markdown_path"],
                    ],
                    orchestration_context=orchestration_context,
                ),
                source="duplicate_review",
            )

        orchestration_context = self._prepare_layer_route(state, data_quality=data_quality.get("harness", {}))

        # Generic invariant: a KPI with no measurable definition (empty metric
        # AND grain) cannot produce executable SQL. Definition-completeness is
        # logically prior to feature-mapping, so this gate runs BEFORE the
        # feature-blocker panel — otherwise an undefined KPI would either stop at
        # a feature-blocker stage (wrong stage) or be silently skipped because it
        # exposes no feature token to map. Surfacing it here keeps the flow
        # generic: it fires for any registry shape, including natural-language
        # question workspaces whose metric/grain were never derived. Mirrors the
        # WorkspaceArtifactValidator condition.
        registry = _read_json(self.layout.contracts_dir / "kpi_registry.json")
        undefined = _undefined_kpis(registry)
        total_kpis = len(registry.get("kpis") or [])
        # Partial-completion: only HARD-block here when NOTHING is generatable
        # (every KPI undefined). When some KPIs are defined, proceed -- the
        # undefined ones are carried as `deferred` and surfaced specifically at
        # the feature-blocker stage (with derived-pattern options when they
        # match), instead of a blanket "define everything" stop. The all-undefined
        # case preserves the original gate (and its test).
        deferred_kpis = undefined
        if undefined and len(undefined) >= max(total_kpis, 1):
            listing = "\n".join(f"- `{u['kpi_id']}`: {u['kpi']}" for u in undefined)
            return self._save_panel(
                state,
                panel={
                    "stage": "kpi_definition_incomplete",
                    "status": "blocked",
                    "instruction": (
                        "These KPIs have no measurable definition (empty metric and "
                        "grain), so executable SQL cannot be generated for them. Define "
                        "each KPI's metric (and grain/cuts) before generation continues. "
                        "Do NOT hand-edit generated contracts or fabricate a result — "
                        "resolve the definition, then re-run `workspace-flow start`.\n\n"
                        + listing
                    ),
                    "question": (
                        "Define the metric and grain for each KPI listed above before "
                        "SQL generation."
                    ),
                    "options": [],
                    "recommended_option_id": "",
                    "artifact_paths": [
                        self.workspace_rel
                        + "/interns/generated/contracts/kpi_registry.json",
                    ],
                    "summary": {
                        "undefined_kpi_count": len(undefined),
                        "undefined_kpis": undefined,
                    },
                    "suggested_skills": [
                        {
                            "name": "kpi-clarification",
                            "why": "Decompose an ambiguous business question into a concrete metric/grain/filter definition.",
                        },
                        {
                            "name": "business-analyst",
                            "why": "Confirm the intended measure and dimensions with the stakeholder.",
                        },
                    ],
                },
                source="kpi_definition_incomplete",
            )

        prepared = prepare_kpi_blocker_panel(
            self.repo_root,
            self.workspace_rel,
            domain=self.domain,
            onboard_if_missing=False,
        )
        self._record_step(state, "prepare_kpi_blocker_panel", "ok", prepared.summary())
        panel = _read_json(self.repo_root / prepared.question_panel_path)
        if panel.get("status") == "needs_user_answer":
            resolution_review = _build_kpi_resolution_review(self.repo_root, self.workspace_rel)
            return self._save_panel(
                state,
                panel=_compact_panel(
                    stage="kpi_blocker",
                    status="needs_user_answer",
                    source_panel=panel,
                    instruction=_panel_instruction(panel),
                    artifact_paths=[prepared.question_panel_path, prepared.question_panel_markdown_path],
                    resolution_review=resolution_review,
                    orchestration_context=orchestration_context,
                ),
                source="kpi_blocker",
            )

        # business-analyst (kpi_definition): the feature-blocker panel can be clear
        # while a KPI's intent is still ambiguous (e.g. a low-confidence share
        # denominator). Surface those low-confidence intent facets so the
        # definition gap is reviewed, not silently defaulted.
        try:
            from core.onboarding.kpi.intent_contract import intent_facet_panel_questions

            _intent_questions = intent_facet_panel_questions(self.repo_root, self.workspace_rel)
        except Exception:
            _intent_questions = []
        _kpi_count = len(
            (_read_json(self.layout.contracts_dir / "kpi_registry.json").get("kpis") or [])
        )
        self._delegate_and_record(
            state,
            agent="business-analyst",
            stage="kpi_definition",
            reason="KPI definitions must be unambiguous (metric/grain/denominator/anchor) before generation",
            verdict_fn=lambda: verdict_from_kpi_definition(len(_intent_questions), _kpi_count),
        )

        relationships = RelationshipContractBuilder(self.repo_root, self.workspace_rel).build()
        self._record_step(state, "build_relationship_contracts", "ok", relationships.summary())
        self._delegate_and_record(
            state,
            agent="data-engineer",
            stage="relationship_review",
            reason="multi-source KPI generation requires executable relationship contracts",
            verdict_fn=lambda: verdict_from_relationship_summary(relationships.summary()),
        )
        plan = SourceToTargetPlanner(self.repo_root, self.workspace_rel, target_engine="sql").build()
        self._record_step(state, "plan_source_to_target", "ok", plan.summary())
        self._delegate_and_record(
            state,
            agent="source-to-target-reviewer",
            stage="source_to_target_review",
            reason="every KPI plan must pass selected-sources + join-proof + grain checks before SQL gen",
            verdict_fn=lambda: verdict_from_source_to_target_summary(plan.summary()),
        )
        if plan.blocked_kpi_count:
            diff = compute_workflow_diff(self.repo_root, self.workspace_rel)
            recovery_commands: list[dict[str, str]] = []
            for gap in diff.get("kpi_gaps") or []:
                recovery_commands.extend(gap.get("recovery_commands") or [])
            seen_cmds: set[str] = set()
            recovery_commands = [
                cmd for cmd in recovery_commands
                if cmd.get("command") and not (cmd["command"] in seen_cmds or seen_cmds.add(cmd["command"]))
            ]
            if not recovery_commands:
                # Dead-end guard (the #13/#15 family): a blocked panel with an
                # EMPTY recovery list strands the operator. Name the standard
                # re-resolution chain instead of going silent.
                recovery_commands = [
                    {
                        "command": (
                            f"uv run prepare-kpi-blocker-panel --workspace {self.workspace_rel} "
                            f"--domain {self.domain}"
                        ),
                        "reason": (
                            "No per-KPI recovery command was derivable; re-resolve features and "
                            "surface the current blocker panel (definitions may have changed)."
                        ),
                    },
                    {
                        "command": (
                            f"uv run workspace-flow status --workspace {self.workspace_rel} --diff --quiet"
                        ),
                        "reason": "Confirm the per-KPI gap list after re-resolution.",
                    },
                ]
            suggested_skills = [
                {"name": "data-engineering-pipeline-design", "why": "Source-to-target blockers need pipeline-design judgment."},
                {"name": "workspace-governance", "why": "Recovery commands mutate workspace contracts."},
            ]
            return self._save_panel(
                state,
                panel={
                    "stage": "source_to_target_blocked",
                    "status": "blocked",
                    "instruction": (
                        "Source-to-target planning found blockers. The `summary.recovery_commands` "
                        "array lists the exact commands to run; render them inline. "
                        "Do NOT re-run `workspace-flow start` to retry — apply the recovery commands "
                        "and then call `workspace-flow status --workspace <ws> --diff` to confirm."
                    ),
                    "question": "Apply the recovery commands shown below to unblock the plan.",
                    "options": [],
                    "recommended_option_id": "",
                    "artifact_paths": [plan.json_path, plan.markdown_path],
                    "recovery_commands": recovery_commands,
                    "suggested_skills": suggested_skills,
                    "summary": {
                        **plan.summary(),
                        "recovery_commands": recovery_commands,
                        "suggested_skills": suggested_skills,
                        "diff": diff,
                    },
                },
                source="source_to_target",
            )

        generated = []
        blocked_generation: list[dict[str, str]] = []
        # Undefined KPIs are deferred (no metric/grain), not generated -- skipping
        # them lets the defined KPIs complete instead of the whole batch failing
        # on an empty-definition KPI.
        deferred_ids = {u["kpi_id"] for u in deferred_kpis}
        for idx in range(1, plan.kpi_count + 1):
            kpi_id = f"kpi_{idx:03d}"
            if kpi_id in deferred_ids:
                continue
            try:
                generated.append(
                    DuckDBKPISQLGenerator(self.repo_root, self.workspace_rel)
                    .generate(kpi_id)
                    .summary()
                )
            except ValueError as exc:
                # Defense in depth: a KPI whose metric/grain looked present but
                # whose features still could not be matched to a column reaches
                # here. Collect rather than crash, then route to a blocker.
                blocked_generation.append({"kpi_id": kpi_id, "reason": str(exc)})
        if blocked_generation:
            listing = "\n".join(
                f"- `{b['kpi_id']}`: {b['reason']}" for b in blocked_generation
            )
            return self._save_panel(
                state,
                panel={
                    "stage": "kpi_generation_blocked",
                    "status": "blocked",
                    "instruction": (
                        "SQL generation could not resolve feature expressions for the "
                        "KPIs below — their features lack source columns or could not be "
                        "matched to a dataset column. Resolve the feature mappings (do "
                        "NOT hand-edit generated contracts or fabricate a harness "
                        "result), then re-run `workspace-flow start`.\n\n" + listing
                    ),
                    "question": (
                        "Resolve feature mappings for the KPIs listed above before SQL "
                        "generation."
                    ),
                    "options": [],
                    "recommended_option_id": "",
                    "artifact_paths": [
                        self.workspace_rel
                        + "/interns/generated/contracts/kpi_feature_mapping.json",
                    ],
                    "summary": {
                        "blocked_kpi_count": len(blocked_generation),
                        "blocked_kpis": blocked_generation,
                    },
                    "suggested_skills": [
                        {
                            "name": "source-to-target-reviewer",
                            "why": "Confirm each KPI's selected sources and column mapping before SQL gen.",
                        },
                    ],
                },
                source="kpi_generation_blocked",
            )
        self._record_step(
            state,
            "generate_kpi_sql",
            "ok",
            {"generated": generated, "deferred_kpis": deferred_kpis},
        )
        harness = KPIExecutionHarness(self.repo_root, self.workspace_rel).run()
        self._record_step(
            state,
            "run_kpi_execution_harness",
            "ok" if harness.ok else "failed",
            harness.summary(),
            validation="run-kpi-execution-harness",
        )
        # sql-polars-pyspark-specialist (engine_generation): the query/runtime
        # engine was selected + implemented for each KPI; confirm the chosen
        # engine actually executed (parity-checked via the harness).
        self._delegate_and_record(
            state,
            agent="sql-polars-pyspark-specialist",
            stage="engine_generation",
            reason="the selected query engine (sql/polars/pyspark) must generate + execute for every KPI",
            verdict_fn=lambda: verdict_from_engine_generation(generated, harness.ok),
        )
        validation = WorkspaceArtifactValidator(self.repo_root, self.workspace_rel).run()
        self._record_step(
            state,
            "validate_workspace_artifacts",
            "ok" if validation.ok else "failed",
            validation.summary(),
            validation="validate-workspace-artifacts",
        )
        self._delegate_and_record(
            state,
            agent="validation-gatekeeper",
            stage="artifact_validation",
            reason="every workflow-produced contract must pass schema + cross-artifact validation",
            verdict_fn=lambda: verdict_from_validation_summary(validation.summary()),
        )
        if not harness.ok:
            return self._save_panel(
                state,
                panel={
                    "stage": "execution_harness_failed",
                    "status": "blocked",
                    "instruction": "Generated KPI SQL did not pass the execution harness.",
                    "question": "Fix the generated SQL so each KPI creates its exact final result view.",
                    "options": [],
                    "recommended_option_id": "",
                    "artifact_paths": [harness.manifest_path, harness.report_path],
                    "summary": harness.summary(),
                },
                source="execution_harness",
            )
        if not validation.ok:
            return self._save_panel(
                state,
                panel={
                    "stage": "validation_failed",
                    "status": "blocked",
                    "instruction": "Generated workspace artifacts failed validation.",
                    "question": "Fix validation errors before reviewing KPI results.",
                    "options": [],
                    "recommended_option_id": "",
                    "artifact_paths": [self.workspace_rel + "/interns/generated/evidence/kpi_execution_harness.json"],
                    "summary": validation.summary(),
                },
                source="validation",
            )
        preview = self._write_result_preview(preview_rows=PREVIEW_ROW_CAP)
        self._record_step(state, "preview_kpi_results", "ok", preview)
        kpi_entries = preview.get("kpis") or []
        wiki_paths = self._emit_side_outputs(state, kpi_entries)
        self._delegate_and_record(
            state,
            agent="kpi-analyst",
            stage="kpi_completion_review",
            reason="completed KPIs must be reviewed for definition+sql+result-row coverage",
            verdict_fn=lambda: verdict_from_kpi_completion(kpi_entries),
        )
        # Self-grill: actually EXECUTE the generated KPI SQL and cross-check each result
        # against its metric/cuts/filters before declaring done — then hand the kpi-analyst
        # a ready-to-invoke review brief. This makes "self-grill" a real gate, not a suggestion.
        from core.onboarding.kpi.verify_kpi_output import KPIOutputVerifier

        verification = KPIOutputVerifier(self.repo_root, self.workspace_rel).verify()
        self._record_step(
            state,
            "verify_kpi_output",
            "ok" if verification.ok else "blocked",
            verification.summary(),
            validation="verify-kpi-output",
        )
        self._delegate_and_record(
            state,
            agent="kpi-analyst",
            stage="kpi_output_verification",
            reason="self-grill: execute generated KPI SQL and cross-check results+intent before completion",
            verdict_fn=lambda: verdict_from_verification(verification.summary()),
        )
        # data-analyst (result_review): results now exist — interpret/validate them
        # for readiness + anomalies (e.g. a KPI that executed but returned no rows)
        # before the run is declared complete.
        self._delegate_and_record(
            state,
            agent="data-analyst",
            stage="result_review",
            reason="produced KPI results must be interpreted/validated for anomalies before completion",
            verdict_fn=lambda: verdict_from_result_review(kpi_entries),
        )
        completed_kpis = [
            {
                "kpi_id": entry.get("kpi_id"),
                "definition": entry.get("definition") or {},
                "sql_path": entry.get("sql_path"),
                "sql_text": entry.get("sql_text"),
                "status": entry.get("status"),
                "result_view": entry.get("result_view"),
                "preview_markdown": entry.get("preview_markdown"),
                "error": entry.get("error"),
                "wiki_path": next(
                    (
                        p for p in wiki_paths
                        if Path(p).stem == str(entry.get("kpi_id") or "")
                    ),
                    "",
                ),
            }
            for entry in kpi_entries
        ]
        preview_summary = {k: v for k, v in preview.items() if k != "kpis"}

        # HARD GATE: a kpi-analyst SEMANTIC review must actually happen before the
        # workflow can report `complete`. The programmatic verdicts and the
        # mechanical self-grill above only prove execution + token-matching, not
        # business intent — a subtly mis-interpreted metric (wrong denominator,
        # wrong grain) passes them while being wrong. Completion therefore
        # requires a recorded kpi-analyst verdict that matches the CURRENT
        # generated KPIs; until one is posted back (via `workspace-flow review`),
        # the flow stops at a blocking review gate carrying the ready-to-invoke
        # brief. This converts the advisory required-specialist into an enforced
        # step (closes the "advisory != enforced" gap).
        review = state.get("kpi_analyst_review") or {}
        kpi_signature = _kpi_review_signature(completed_kpis)
        review_current = bool(review) and review.get("kpi_signature") == kpi_signature
        if not review_current:
            review_event = self._delegate_and_record(
                state,
                agent="kpi-analyst",
                stage="kpi_output_verification",
                reason="semantic review gate: kpi-analyst must judge intent before completion",
                verdict_fn=lambda: verdict_from_verification(verification.summary()),
            )
            stale = bool(review) and not review_current
            return self._save_panel(
                state,
                panel={
                    "stage": "kpi_analyst_review",
                    "status": "needs_specialist_review",
                    "instruction": (
                        "KPI SQL generated and mechanically self-grilled, but completion is "
                        "GATED on a kpi-analyst semantic review. Invoke the `kpi-analyst` agent/skill: "
                        "for each KPI under `summary.completed_kpis`, judge whether the SQL + result rows "
                        "actually answer the business_question + metric + cuts (denominator, grain, "
                        "filters), not merely that they ran. Then post the verdict back with "
                        f"`workspace-flow review --session {self.session_id} --verdict <ok|blocked> "
                        "--summary \"...\" [--kpi-notes '<json>']`. The workflow cannot reach `complete` "
                        "until that verdict is recorded."
                        + (" (A prior review is stale — the generated KPIs changed since it was recorded.)" if stale else "")
                    ),
                    "question": "",
                    "options": [],
                    "recommended_option_id": "",
                    "artifact_paths": [
                        self.workspace_rel + "/interns/generated/solutions",
                        preview["json_path"],
                        preview["markdown_path"],
                        self.workspace_rel + "/interns/reports/kpi_output_verification.md",
                    ],
                    "summary": {
                        "generated_kpi_count": len(generated),
                        "self_grill": verification.summary(),
                        "completed_kpis": completed_kpis,
                        "kpi_signature": kpi_signature,
                        "record_command": (
                            f"workspace-flow review --session {self.session_id} "
                            "--verdict <ok|blocked> --summary \"<one line>\""
                        ),
                        "suggested_skills": [
                            {"name": "kpi-analyst", "why": "Judge whether each result answers its KPI intent."},
                            {"name": "kpi-clarification", "why": "Decompose any ambiguous metric (denominator/grain/output-type) before judging."},
                        ],
                        "required_specialists": ["kpi-analyst"],
                        "delegations": list(state.get("delegations") or []),
                    },
                },
                source="kpi_analyst_review",
            )
        if str(review.get("verdict")) == "blocked":
            return self._save_panel(
                state,
                panel={
                    "stage": "kpi_analyst_review_blocked",
                    "status": "blocked",
                    "instruction": (
                        "kpi-analyst flagged one or more KPIs as not answering their intent. "
                        "See `summary.kpi_analyst_review`. Fix the flagged KPIs, regenerate, then "
                        "re-review before the workflow can complete."
                    ),
                    "question": "",
                    "options": [],
                    "recommended_option_id": "",
                    "artifact_paths": [
                        self.workspace_rel + "/interns/generated/solutions",
                        self.workspace_rel + "/interns/reports/kpi_output_verification.md",
                    ],
                    "summary": {
                        "generated_kpi_count": len(generated),
                        "kpi_analyst_review": review,
                        "completed_kpis": completed_kpis,
                    },
                },
                source="kpi_analyst_review_blocked",
            )
        # BUG-014 (enforcement): collect gate provenance before building the
        # completion panel so agents and humans can see which gates were
        # human-confirmed vs agent-asserted.
        gate_provenance = _collect_gate_provenance(state, self.layout)
        agent_gate_count = sum(1 for g in gate_provenance if g.get("source") != "human")
        completion_headline = _gate_provenance_headline(gate_provenance)
        # P0 (core-audit): regenerate the artifact MANIFEST on completion so its
        # presence counts never lag the actual run. The manifest previously went
        # stale (claimed "Present 17/29" after kpi_results already existed) because
        # nothing refreshed it after onboarding. Non-fatal: a manifest write
        # failure is recorded but must never block a finished workflow.
        try:
            manifest_paths = write_artifact_manifest(self.layout)
            self._record_step(state, "write_artifact_manifest", "ok", manifest_paths)
        except Exception as exc:  # noqa: BLE001 - report, never block completion
            self._record_step(
                state,
                "write_artifact_manifest",
                "blocked",
                {"error": type(exc).__name__, "detail": str(exc)},
            )
        return self._save_panel(
            state,
            panel={
                "stage": "complete",
                "status": "complete",
                "instruction": (
                    "KPI SQL generation and previews are complete. "
                    "Each entry under `summary.completed_kpis` carries the original KPI definition, "
                    "the generated SQL text, and the result-table preview. Render them inline."
                ),
                "question": "",
                "options": [],
                "recommended_option_id": "",
                "artifact_paths": [
                    self.workspace_rel + "/interns/generated/solutions",
                    preview["json_path"],
                    preview["markdown_path"],
                    self.workspace_rel + "/interns/reports/kpi_output_verification.md",
                ],
                "summary": {
                    "generated_kpi_count": len(generated),
                    "orchestration": orchestration_context,
                    "validation": validation.summary(),
                    "self_grill": verification.summary(),
                    "kpi_analyst_review": review,
                    "results": preview_summary,
                    "completed_kpis": completed_kpis,
                    "gate_provenance": gate_provenance,
                    "agent_asserted_gate_count": agent_gate_count,
                    "completion_headline": completion_headline,
                    "suggested_skills": [
                        {"name": "kpi-analyst", "why": "Validate generated SQL and result samples against KPI intent."},
                        {"name": "grill-requirements", "why": "self-grill mode EXECUTED — see summary.self_grill and the delegation brief for kpi_output_verification."},
                    ],
                    "required_specialists": [
                        "data-engineer",
                        "source-to-target-reviewer",
                        "validation-gatekeeper",
                        "kpi-analyst",
                        "dashboard-engineer",
                    ],
                    "delegations": list(state.get("delegations") or []),
                },
            },
            source="complete",
        )

    def _emit_side_outputs(self, state: dict[str, Any], kpi_entries: list[dict[str, Any]]) -> list[str]:
        """Emit opt-in wiki notes and the (default-on) dashboard at KPI completion.

        Side-effect-only: records steps/delegations on ``state`` and returns the wiki
        note paths written (empty when wiki output is opt-out / fails). Extracted from
        _advance_until_stop (phase 2 decomposition); behavior is unchanged.
        """
        # Wiki notes stay an OPT-IN side output (Phase 1 decision: untracked
        # surprises nobody asked for). The dashboard, by explicit user request
        # (2026-06-12), is DEFAULT-ON at KPI completion: refresh specs, export
        # static HTML, and open it. Opt out with AUTORESEARCH_DASHBOARD=0.
        import os as _os

        _side = _os.environ.get("AUTORESEARCH_SIDE_OUTPUTS", "")
        _wiki_on = _side == "1" or _os.environ.get("AUTORESEARCH_WIKI", "") == "1"
        _dash_flag = _os.environ.get("AUTORESEARCH_DASHBOARD", "")
        _dash_on = _dash_flag != "0" and _side != "0"
        if not _wiki_on:
            self._record_step(
                state, "upsert_kpi_wiki_notes", "skipped",
                {"reason": "opt-in side output (set AUTORESEARCH_WIKI=1 or AUTORESEARCH_SIDE_OUTPUTS=1)"},
            )
        if not _dash_on:
            self._record_step(
                state, "refresh_workspace_dashboard", "skipped",
                {"reason": "dashboard disabled (AUTORESEARCH_DASHBOARD=0)"},
            )
        wiki_paths: list[str] = []
        if _wiki_on:
          try:
            wiki_layout = WikiLayout(project_root=self.workspace)
            for entry in kpi_entries:
                kpi_id = str(entry.get("kpi_id") or "")
                if not kpi_id:
                    continue
                scaffold = build_kpi_completion_scaffold(kpi_id=kpi_id, entry=entry)
                note_path = upsert_kpi_note(wiki_layout, kpi_id, scaffold)
                wiki_paths.append(_rel(note_path, self.repo_root))
          except Exception as exc:
            self._record_step(
                state,
                "upsert_kpi_wiki_notes",
                "failed",
                {"error": str(exc), "count": len(wiki_paths)},
            )
          else:
            self._record_step(
                state,
                "upsert_kpi_wiki_notes",
                "ok",
                {"count": len(wiki_paths), "notes": wiki_paths},
            )
        if _dash_on:
          try:
            dash_summary = refresh_workspace_dashboard(
                self.layout, completed_kpi_entries=kpi_entries
            )
          except Exception as exc:
            self._record_step(
                state,
                "refresh_workspace_dashboard",
                "failed",
                {"error": str(exc)},
            )
          else:
            # Export static HTML and show it: KPI completion ends with the
            # dashboard in front of the user, not just files on disk. Outside
            # test runs the SCREENER wraps the export: every page is
            # screenshotted and checked (render failures, blank pages, missing
            # data viewer, redaction, palette) and its findings ride into the
            # step record as warnings — visualization defects surface at
            # completion instead of waiting for a manual look. Opt out with
            # AUTORESEARCH_SCREEN_DASHBOARD=0.
            import sys as _sys

            dash_index_path = ""
            _screen_ok = (
                _os.environ.get("AUTORESEARCH_SCREEN_DASHBOARD", "") != "0"
                and "unittest" not in _sys.modules
                and "pytest" not in _sys.modules
            )
            try:
                if _screen_ok:
                    from core.dashboard.screener import screen_dashboard

                    screen_summary = screen_dashboard(self.repo_root, self.workspace_rel)
                    dash_summary = {**dash_summary, "screener": screen_summary}
                    if not screen_summary.get("ok"):
                        dash_summary["screener_warning"] = (
                            f"{screen_summary.get('error_count')} visual finding(s); "
                            f"see {screen_summary.get('report_md')} and review the "
                            "staged screenshots"
                        )
                    export_dir = "dashboard/exports"
                else:
                    from core.dashboard.export import export_static_html

                    export_summary = export_static_html(self.repo_root, self.workspace_rel)
                    dash_summary = {**dash_summary, "export": export_summary}
                    export_dir = export_summary["export_dir"]
                dash_index_path = f"{self.workspace_rel}/{export_dir}/index.html"
                dash_summary = {**dash_summary, "dashboard_index": dash_index_path}
            except Exception as exc:
                dash_summary = {**dash_summary, "export_error": str(exc)}
            self._record_step(
                state,
                "refresh_workspace_dashboard",
                "ok",
                dash_summary,
            )
            if dash_index_path:
                # The result packet ends with the dashboard link so the
                # completion output always shows where the dashboard lives.
                # current.md and runs/<date>/results.md are verbatim-forward
                # twins (BUG-015 contract) — append to BOTH or neither.
                link_line = f"\n**Dashboard:** `{dash_index_path}`\n"
                packet_paths = [
                    self.workspace / "interns" / "reports" / "kpi_results" / "current.md"
                ]
                runs_dir = self.workspace / "interns" / "runs"
                if runs_dir.is_dir():
                    dated = sorted(d for d in runs_dir.iterdir() if d.is_dir())
                    if dated:
                        packet_paths.append(dated[-1] / "results.md")
                try:
                    for packet_path in packet_paths:
                        if packet_path.exists():
                            content = packet_path.read_text(encoding="utf-8")
                            if "**Dashboard:**" not in content:
                                packet_path.write_text(
                                    content + link_line, encoding="utf-8"
                                )
                except OSError:
                    pass
            import sys as _sys

            _open_flag = _os.environ.get("AUTORESEARCH_OPEN_DASHBOARD", "")
            _in_test_run = "unittest" in _sys.modules or "pytest" in _sys.modules
            _open_ok = _open_flag == "1" or (_open_flag != "0" and not _in_test_run)
            if dash_index_path and _open_ok:
                try:
                    import webbrowser

                    webbrowser.open((self.repo_root / dash_index_path).resolve().as_uri())
                except Exception:
                    pass  # showing the path in the packet is the fallback
            self._delegate_and_record(
                state,
                agent="dashboard-engineer",
                stage="dashboard_refresh",
                reason="every workflow completion must refresh dashboard specs (preserving user_overrides)",
                verdict_fn=lambda: verdict_from_dashboard_summary(dash_summary),
            )
        return wiki_paths

    def _workflow_checkpoint(self, state: dict[str, Any], *, mode: str) -> WorkspaceFlowResult:
        checkpoint = WorkspaceWorkflowOrchestrator(
            self.repo_root,
            self.workspace_rel,
            domain=self.domain,
            mode=mode,
        ).prepare()
        self._record_step(state, "prepare_workspace_workflow", "ok", checkpoint.summary())
        panel = _read_json(self.repo_root / checkpoint.current_json_path)
        return self._save_panel(
            state,
            panel={
                "stage": "workflow_checkpoint",
                "status": "needs_user_choice",
                "instruction": "Choose advisory planning, local-safe orchestration, or bounded autopilot for this workspace.",
                "question": "Which orchestration mode should run next?",
                "options": panel.get("options", []),
                "recommended_option_id": panel.get("recommended_option_id", ""),
                "why": panel.get("recommended_answer", ""),
                "artifact_paths": [checkpoint.current_json_path, checkpoint.current_markdown_path],
                "summary": checkpoint.summary(),
            },
            source="workflow_checkpoint",
        )

    def _run_data_quality_gate(self, state: dict[str, Any]) -> dict[str, Any]:
        harness = DataQualityHarness(self.repo_root, self.workspace_rel).run()
        self._record_step(
            state,
            "run_data_quality_harness",
            "ok" if harness.ok else "blocked",
            harness.summary(),
            validation="run-data-quality-harness",
        )
        result: dict[str, Any] = {"harness": harness.summary(), "blocked": not harness.ok}
        if harness.ok:
            return result
        duplicate = DuplicateReviewPanel(self.repo_root, self.workspace_rel).prepare()
        self._record_step(
            state,
            "prepare_duplicate_review_panel",
            "ok",
            duplicate.summary(),
        )
        result["duplicate_review"] = duplicate.summary()
        result["orchestration_context"] = {
            "mode": self.orchestration_mode,
            "data_quality": harness.summary(),
            "blocked_before": "kpi_blocker",
        }
        return result

    def _load_profiles(self) -> list[dict[str, Any]]:
        """Load generated profiles as a list of profile dicts.

        Reuses the ``profile_index.json`` shape written by onboarding (the same
        index ``RelationshipContractBuilder`` consumes). Each entry already
        carries ``columns``/``schema``/``row_count``/``path`` — exactly the shape
        the data-understanding classifier expects. Falls back to reading the
        per-table ``*.profile.json`` files if the index is absent.
        """
        index_path = self.layout.profile_index_path
        if index_path.exists():
            index = _read_json(index_path)
            profiles = [p for p in (index.get("profiles") or []) if isinstance(p, dict)]
            if profiles:
                return profiles
        profiles: list[dict[str, Any]] = []
        if self.layout.profiles_dir.exists():
            for path in sorted(self.layout.profiles_dir.glob("*.profile.json")):
                data = _read_json(path)
                if data:
                    profiles.append({**data, "path": data.get("path") or str(path)})
        return profiles

    def _run_data_understanding_gate(self, state: dict[str, Any]) -> dict[str, Any]:
        """BUG-010 gate: classify quality tier + schema type and surface scoped options.

        Reads generated profiles + relationship contracts (if present), runs the
        side-effect-free classifier, persists a decision artifact under
        ``interns/reports/data_understanding/current.{json,md}``, and saves the
        classification onto the session state. Never hard-blocks: an
        unclassifiable tier still surfaces as options rather than halting the flow.
        """
        profiles = self._load_profiles()
        relationships = _read_json(self.layout.relationship_contracts_path)

        tier_result = classify_quality_tier(profiles)
        schema_result = classify_schema_type(profiles, relationships)
        tier = str(tier_result.get("tier") or "")
        schema_type = str(schema_result.get("schema_type") or "")
        options = scoped_processing_options(tier, profiles, schema_type)

        current_data_model = _summarize_current_data_model(profiles, relationships)
        current_kpi_set = _summarize_current_kpi_set(self.layout)

        top_level_options = [
            {
                "option_id": "option_generate",
                "label": "Generate KPI and/or data model artifacts",
                "description": (
                    "Run the governed onboarding -> feature resolution -> KPI/SQL generation "
                    "pipeline to produce new KPI and data-model artifacts for this workspace."
                ),
            },
            {
                "option_id": "option_forward",
                "label": "Move forward with the current workflow",
                "description": (
                    "Proceed using the current data model and existing KPI set (echoed below) "
                    "without regenerating artifacts."
                ),
            },
        ]

        understanding = {
            "artifact_type": "data_understanding/current.json",
            "version": FLOW_VERSION,
            "workspace": self.workspace_rel,
            "generated_at": _now(),
            "profile_count": len(profiles),
            "quality_tier": tier_result,
            "schema_type": schema_result,
            "scoped_processing_options": options,
            "top_level_options": top_level_options,
            "current_data_model": current_data_model,
            "current_kpi_set": current_kpi_set,
        }

        report_dir = self.layout.reports_dir / "data_understanding"
        report_dir.mkdir(parents=True, exist_ok=True)
        json_path = report_dir / "current.json"
        md_path = report_dir / "current.md"
        json_path.write_text(json.dumps(understanding, indent=2, default=str) + "\n", encoding="utf-8")
        md_path.write_text(_render_data_understanding_markdown(understanding), encoding="utf-8")

        summary = {
            "quality_tier": tier,
            "tier_confidence": tier_result.get("confidence"),
            "schema_type": schema_type,
            "schema_confidence": schema_result.get("confidence"),
            "scoped_option_count": len(options),
            "profile_count": len(profiles),
            "json_path": _rel(json_path, self.repo_root),
            "markdown_path": _rel(md_path, self.repo_root),
        }
        self._record_step(state, "data_understanding_gate", "ok", summary)
        state["data_understanding"] = summary
        return summary

    def _prepare_layer_route(self, state: dict[str, Any], *, data_quality: dict[str, Any]) -> dict[str, Any]:
        route = DataEngineeringRoutePlanner(
            self.repo_root,
            self.workspace_rel,
            track="auto",
            target_engine="sql",
        ).build()
        self._record_step(
            state,
            "prepare_data_engineering_route",
            "ok",
            route.summary(),
        )
        return {
            "mode": self.orchestration_mode,
            "data_quality": data_quality,
            "layer_route": route.summary(),
            "available_modes": sorted(ORCHESTRATION_MODES),
        }

    # Result columns that look like a share of a total (percentage_share, pct_*,
    # *_percent...). Intentionally excludes "ratio" (not necessarily on a 0-100
    # scale).
    _SHARE_COLUMN_PATTERN = re.compile(r"(percent|share|pct)", re.IGNORECASE)

    @classmethod
    def _share_sum_check(cls, conn: Any, view: str) -> dict[str, Any] | None:
        """Generic share-sum invariant for percentage-share KPI results.

        If the result view has exactly one share-like column, sum it across all
        rows. A partition of a total sums to ~100%; a materially different total
        means overlapping group membership (one entity counted in several grain
        cells) or a non-grand-total denominator scope. Returns None when the
        check does not apply, else {column, sum, status} with status "ok" or
        "not_a_partition". Workspace- and domain-agnostic: driven only by the
        emitted result schema, never by column semantics from any one dataset.

        Parity-mode independence: this check aggregates IN SQL over the result
        view; it never consumes the materialized parity rows, so it works
        identically whether engine parity ran in row_parity or
        aggregate_parity mode. Any future check that NEEDS the materialized
        rows must say so explicitly instead of silently passing when parity
        runs in aggregate mode.
        """
        try:
            description = conn.execute(f'SELECT * FROM "{view}" LIMIT 0').description or []
            share_cols = [col[0] for col in description if cls._SHARE_COLUMN_PATTERN.search(str(col[0]))]
            if len(share_cols) != 1:
                return None
            row_count = conn.execute(f'SELECT COUNT(*) FROM "{view}"').fetchone()[0]
            if int(row_count) <= 1:
                # A single-row percent (e.g. percent-of-total with a filtered
                # numerator) is one share of a whole, not a partition — the
                # sum-to-100 expectation does not apply.
                return None
            column = share_cols[0]
            total = conn.execute(f'SELECT ROUND(SUM("{column}"), 2) FROM "{view}"').fetchone()[0]
            total_value = float(total)
        except Exception:
            return None
        status = "ok" if abs(total_value - 100.0) <= 0.5 else "not_a_partition"
        return {"column": column, "sum": total_value, "status": status}

    def _write_result_preview(self, *, preview_rows: int) -> dict[str, Any]:
        import duckdb

        self.layout.reports_dir.mkdir(parents=True, exist_ok=True)
        sql_files = sorted(path for path in self.layout.solutions_dir.glob("kpi_*.sql") if path.name != "kpi_metrics.sql")
        result_dir = self.layout.reports_dir / "kpi_results"
        evidence_dir = self.layout.evidence_dir / "kpi_results"
        result_dir.mkdir(parents=True, exist_ok=True)
        evidence_dir.mkdir(parents=True, exist_ok=True)
        kpi_definitions = load_kpi_definitions(self.layout)
        entries = []
        # Cross-runtime parity (Polars vs the canonical DuckDB rows). Cached by
        # sql_sha256 so unchanged KPIs never re-run the subprocess; disable with
        # KPI_ENGINE_PARITY=0. Generic: no per-workspace logic.
        import os as _os

        parity_enabled = _os.environ.get("KPI_ENGINE_PARITY", "1").lower() not in {"0", "off", "false"}
        parity_cache_path = self.layout.evidence_dir / "engine_parity" / "current.json"
        parity_cache: dict[str, Any] = {}
        if parity_enabled and parity_cache_path.exists():
            try:
                parity_cache = json.loads(parity_cache_path.read_text(encoding="utf-8")).get("kpis", {})
            except (json.JSONDecodeError, OSError):
                parity_cache = {}
        conn = duckdb.connect(":memory:")
        # Deterministic temporal semantics: CAST(timestamp AS DATE) uses the
        # SESSION timezone, so the same SQL yields different year/date buckets
        # on differently-configured machines (and diverges from engines that
        # bucket in UTC). Pin UTC for reproducible, engine-parity-stable rows.
        try:
            conn.execute("SET TimeZone='UTC'")
        except Exception:
            pass
        from core.storage.atomic_io import pushd

        cwd_cm = pushd(self.repo_root)
        cwd_cm.__enter__()
        try:
            for sql_file in sql_files:
                kpi_id = sql_file.stem
                sql_text = sql_file.read_text(encoding="utf-8")
                entry: dict[str, Any] = {
                    "kpi_id": kpi_id,
                    "sql_path": _rel(sql_file, self.repo_root),
                    "sql_text": sql_text,
                    # Hash of the exact SQL this packet was built from. A later
                    # hand-edit to the .sql changes this, letting the presenter
                    # detect a stale packet rather than show results that no
                    # longer match the on-disk SQL (BUG-015 / BUG-024 follow-on).
                    "sql_sha256": hashlib.sha256(sql_text.encode("utf-8")).hexdigest(),
                    "definition": kpi_definitions.get(kpi_id, {}),
                    "status": "ok",
                }
                try:
                    conn.execute(sql_text)
                    view = _result_view(conn, kpi_id)
                    if view:
                        cursor = conn.execute(f'SELECT * FROM "{view}" LIMIT {int(preview_rows)}')
                        # Redact PHI/PCI before rendering the canonical result packet —
                        # this is the highest-traffic display surface (CLAUDE.md says
                        # forward current.md verbatim) and previously emitted raw rows.
                        # Ref: core-audit ob-kpi-d.md.
                        _cols = [str(d[0]) for d in cursor.description or []]
                        _rows = cursor.fetchall()
                        _patterns = workspace_redaction_patterns(self.layout.project_root)
                        _agg_ages = workspace_aggregate_ages(self.layout.project_root)
                        _rows = redact_table_rows(
                            _cols, _rows, patterns=_patterns, aggregate_ages=_agg_ages
                        )
                        if not _cols:
                            preview_md = "(query returned no tabular result)"
                        elif not _rows:
                            preview_md = render_markdown_table(_cols, []) + "\n\n(no rows)"
                        else:
                            preview_md = render_markdown_table(_cols, _rows)
                        # Append truncation note when total rows exceed the preview cap.
                        try:
                            total_rows = conn.execute(
                                f'SELECT COUNT(*) FROM "{view}"'
                            ).fetchone()[0]
                            remaining = int(total_rows) - int(preview_rows)
                            if remaining > 0:
                                result_json_path = (
                                    self.layout.evidence_dir / "kpi_results" / "current.json"
                                )
                                preview_md = (
                                    preview_md
                                    + f"\n\n[~] {remaining} more rows "
                                    f"(full results in {_rel(result_json_path, self.repo_root)})"
                                )
                        except Exception:
                            pass
                        # Share-sum invariant: a share/percentage result that is a
                        # true partition of its total sums to ~100%. Overlapping
                        # group membership silently breaks that expectation, so
                        # measure it and say so in the packet instead of letting
                        # the reader discover it by adding rows up.
                        share_check = self._share_sum_check(conn, view)
                        if share_check:
                            entry["share_sum_check"] = share_check
                            if share_check["status"] != "ok":
                                preview_md = (
                                    preview_md
                                    + f"\n\n[~] share check: `{share_check['column']}` sums to "
                                    f"{share_check['sum']}% across all result rows (a partition "
                                    "of the total would sum to ~100%). One entity counted in "
                                    "multiple groups (overlapping cuts) or a non-grand-total "
                                    "denominator scope both cause this; verify intent before "
                                    "presenting these shares as portions of a whole."
                                )
                        # Cross-runtime parity: same KPI, generated Polars
                        # variant, compared against the rows just produced.
                        if parity_enabled:
                            cached = parity_cache.get(kpi_id) or {}
                            # Cache only short-circuits a prior MATCH for the same
                            # SQL: mismatch/skip/error re-run every time so a fix
                            # on the engine-generator side self-heals (the cache
                            # key cannot see non-SQL changes).
                            if (
                                cached.get("sql_sha256") == entry["sql_sha256"]
                                and (cached.get("parity") or {}).get("status") == "match"
                            ):
                                parity = dict(cached.get("parity") or {})
                                parity["cached"] = True
                                # Cache entries written before aggregate mode
                                # existed predate the `mode` key; a cached
                                # match was necessarily a row-level compare.
                                parity.setdefault("mode", PARITY_MODE_ROW)
                            else:
                                try:
                                    # Above the parity row cap run_polars_parity
                                    # switches to aggregate-signature mode
                                    # instead of skipping, so every result keeps
                                    # cross-engine protection.
                                    cursor_all = conn.execute(f'SELECT * FROM "{view}"')
                                    all_columns = [col[0] for col in cursor_all.description]
                                    all_rows = cursor_all.fetchall()
                                    parity = run_polars_parity(
                                        self.repo_root, self.workspace_rel, kpi_id,
                                        all_columns, all_rows,
                                    )
                                except Exception as exc:
                                    parity = {
                                        "engine": "polars",
                                        "kpi_id": kpi_id,
                                        "status": "error",
                                        "reason": str(exc),
                                    }
                                parity_cache[kpi_id] = {
                                    "sql_sha256": entry["sql_sha256"],
                                    "parity": {k: v for k, v in parity.items() if k != "cached"},
                                }
                            entry["engine_parity"] = parity
                            parity_marker = "[ok]" if parity.get("status") == "match" else "[~]"
                            # Surface the assurance level (row_parity vs
                            # aggregate_parity), not just pass/fail, so the
                            # kpi-analyst review gate sees what it got.
                            parity_mode = parity.get("mode") or "unknown_mode"
                            preview_md = (
                                preview_md
                                + f"\n\n{parity_marker} engine parity (polars vs sql, {parity_mode}): "
                                f"{parity.get('status')} - {parity.get('reason')}"
                            )
                        entry["preview_markdown"] = preview_md
                        entry["result_view"] = view
                    else:
                        entry["status"] = "error"
                        entry["error"] = f"Exact result view `{kpi_id}_results` was not created."
                        entry["preview_markdown"] = "(no KPI result view found)"
                except Exception as exc:
                    entry["status"] = "error"
                    entry["error"] = str(exc)
                entries.append(entry)
        finally:
            cwd_cm.__exit__(None, None, None)
            conn.close()

        if parity_enabled:
            try:
                parity_cache_path.parent.mkdir(parents=True, exist_ok=True)
                parity_cache_path.write_text(
                    json.dumps(
                        {
                            "artifact_type": "engine_parity/current.json",
                            "workspace": self.workspace_rel,
                            "generated_at": _now(),
                            "kpis": parity_cache,
                        },
                        indent=2,
                        default=str,
                    )
                    + "\n",
                    encoding="utf-8",
                )
            except OSError:
                pass

        json_path = evidence_dir / "current.json"
        md_path = result_dir / "current.md"
        payload = {
            "artifact_type": "kpi_results/current.json",
            "version": FLOW_VERSION,
            "workspace": self.workspace_rel,
            "generated_at": _now(),
            "preview_rows": preview_rows,
            "kpis": entries,
        }
        json_path.write_text(json.dumps(payload, indent=2, default=str) + "\n", encoding="utf-8")
        # `current.md` (the canonical file an agent naively cats and the doc rule says
        # to forward verbatim) is the COMPACT packet: definition + result table + a
        # `SQL:` link, no inlined SQL. Small enough not to trip CLI/UI truncation, which
        # agents misread as a failed read and then re-read 6-7x (the observed loop).
        # The full inlined-SQL packet lives in `current_full.md` for drill-down.
        compact_markdown = _render_results_markdown(payload, compact=True)
        full_markdown = _render_results_markdown(payload)
        md_path.write_text(compact_markdown, encoding="utf-8")
        full_md_path = result_dir / "current_full.md"
        full_md_path.write_text(full_markdown, encoding="utf-8")
        # Back-compat alias (older callers/tools referenced current_compact.md).
        (result_dir / "current_compact.md").write_text(compact_markdown, encoding="utf-8")
        self._write_runs_snapshot(payload, compact_markdown, full_markdown)
        return {
            "json_path": _rel(json_path, self.repo_root),
            "markdown_path": _rel(md_path, self.repo_root),
            "full_markdown_path": _rel(full_md_path, self.repo_root),
            "kpi_count": len(entries),
            "error_count": sum(1 for item in entries if item.get("status") != "ok"),
            "kpis": entries,
        }

    def _write_runs_snapshot(
        self, payload: dict[str, Any], compact_markdown: str, full_markdown: str
    ) -> None:
        """Mirror the just-executed results into a dated runs/<date>/ snapshot.

        The snapshot is written here — by the executor that re-runs the on-disk
        SQL — so the dated record always reflects what was actually executed,
        not what the generator first emitted. Per-KPI files (compact, SQL linked)
        plus a combined `results.md` (compact — the Active Run forward target) and
        `results_full.md` (inlined SQL). All overwritten each run (no graveyard).
        The combined file is compact so following the Active Run pointer doesn't hit
        a long, UI-truncating file.
        """
        run_dir = self.layout.runs_dir / date.today().isoformat()
        run_dir.mkdir(parents=True, exist_ok=True)
        for entry in payload.get("kpis", []):
            kpi_id = entry.get("kpi_id")
            if not kpi_id:
                continue
            section = "\n".join(render_kpi_block(entry, heading_level=2, include_sql=False)).rstrip() + "\n"
            (run_dir / f"{kpi_id}.md").write_text(section, encoding="utf-8")
        (run_dir / "results.md").write_text(compact_markdown, encoding="utf-8")
        (run_dir / "results_full.md").write_text(full_markdown, encoding="utf-8")

    def _base_state(self, intent: str) -> dict[str, Any]:
        return {
            "artifact_type": "workspace_flow_session.json",
            "version": FLOW_VERSION,
            "session_id": self.session_id,
            "workspace": self.workspace_rel,
            "domain": self.domain,
            "intent": intent,
            "orchestration_mode": self.orchestration_mode,
            "created_at": _now(),
            "updated_at": _now(),
            "status": "running",
            "stage": "start",
            "hidden_steps": [],
            "current_panel": {},
        }

    def _save_panel(
        self,
        state: dict[str, Any],
        *,
        panel: dict[str, Any],
        source: str,
    ) -> WorkspaceFlowResult:
        panel = {**panel, "source": source, "session_id": self.session_id, "workspace": self.workspace_rel}
        _attach_stage_routing(panel)
        from core.onboarding.panel_contract import normalize_decision_panel
        normalize_decision_panel(panel, workspace=self.workspace_rel)
        state["updated_at"] = _now()
        state["stage"] = panel.get("stage", "")
        state["status"] = panel.get("status", "")
        state["current_panel"] = panel
        self._write_state(state)
        panel_dir = self._session_dir()
        panel_json = panel_dir / "current.json"
        panel_md = panel_dir / "current.md"
        panel_json.write_text(json.dumps(panel, indent=2, default=str) + "\n", encoding="utf-8")
        panel_md.write_text(_render_panel_markdown(panel), encoding="utf-8")
        state["current_panel_path"] = _rel(panel_json, self.repo_root)
        state["current_markdown_path"] = _rel(panel_md, self.repo_root)
        self._write_state(state)
        event_type = "blocker_question" if panel.get("status") == "needs_user_answer" else "workflow_panel"
        self._record_event(
            event_type,
            str(panel.get("status") or ""),
            f"Workspace-flow saved panel `{panel.get('stage', '')}`.",
            artifact=_rel(panel_json, self.repo_root),
            metadata={"source": source, "session_id": self.session_id, "stage": panel.get("stage")},
        )
        return self._result_from_state(state)

    def _load_state(self) -> dict[str, Any]:
        path = self._state_path()
        if not path.exists():
            raise FileNotFoundError(f"workspace-flow session not found: {self.session_id}")
        return json.loads(path.read_text(encoding="utf-8"))

    def _write_state(self, state: dict[str, Any]) -> None:
        path = self._state_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(state, indent=2, default=str) + "\n", encoding="utf-8")

    def _result_from_state(self, state: dict[str, Any]) -> WorkspaceFlowResult:
        panel = state.get("current_panel", {})
        return WorkspaceFlowResult(
            session_id=self.session_id,
            workspace=self.workspace_rel,
            state_path=_rel(self._state_path(), self.repo_root),
            stage=str(state.get("stage") or panel.get("stage") or ""),
            status=str(state.get("status") or panel.get("status") or ""),
            current_panel_path=str(state.get("current_panel_path") or ""),
            current_markdown_path=str(state.get("current_markdown_path") or ""),
            next_step=_next_step(panel),
        )

    def _session_dir(self) -> Path:
        return self.layout.state_dir / "workflow_sessions" / self.session_id

    def _state_path(self) -> Path:
        return self._session_dir() / "session.json"

    def _validate_workspace(self) -> None:
        if not self.workspace.exists():
            raise FileNotFoundError(f"workspace not found: {self.workspace}")
        if self.workspace == self.repo_root or not self.workspace.is_relative_to(self.repo_root):
            raise ValueError(f"workspace must be inside repo root: {self.workspace}")

    def _delegate_and_record(
        self,
        state: dict[str, Any],
        *,
        agent: str,
        stage: str,
        reason: str,
        verdict_fn,
    ) -> DelegationEvent:
        event = record_delegation(
            self.layout,
            self.workspace_rel,
            agent=agent,
            stage=stage,
            reason=reason,
            verdict_fn=verdict_fn,
        )
        state.setdefault("delegations", []).append(event.to_dict())
        return event

    def _record_step(
        self,
        state: dict[str, Any],
        stage: str,
        status: str,
        detail: dict[str, Any],
        *,
        decision: str | None = None,
        validation: str | None = None,
    ) -> None:
        state["hidden_steps"].append(_step(stage, status, detail))
        self._record_event(
            "workflow_step",
            status,
            f"workspace-flow step `{stage}` completed with status `{status}`.",
            decision=decision,
            validation=validation,
            metadata={"session_id": self.session_id, "stage": stage, "detail": detail},
        )

    def _record_event(
        self,
        event_type: str,
        status: str,
        summary: str,
        *,
        artifact: str | None = None,
        decision: str | None = None,
        validation: str | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> None:
        record_trajectory_event_safe(
            self.repo_root,
            self.workspace_rel,
            event_type=event_type,
            status=status,
            summary=summary,
            artifact=artifact,
            decision=decision,
            validation=validation,
            metadata=metadata,
        )


def _find_session(repo_root: Path, session_id: str) -> dict[str, Any]:
    matches = list(repo_root.glob(f"workspaces/*/interns/state/workflow_sessions/{session_id}/session.json"))
    if not matches:
        raise FileNotFoundError(f"workspace-flow session not found: {session_id}")
    return json.loads(matches[0].read_text(encoding="utf-8"))


def _undefined_kpis(registry: dict[str, Any]) -> list[dict[str, str]]:
    """KPIs that carry no measurable definition (empty metric AND grain).

    Such a KPI cannot produce executable SQL — the generator raises and the
    flow would otherwise crash, or a downstream agent fabricates a result.
    Surfacing them as a definition blocker keeps the flow generic: it works for
    any workspace whose KPI source is natural-language questions rather than
    pre-structured rows. The condition mirrors WorkspaceArtifactValidator
    (validation.py) so the gate and the validator always agree. The kpi_id is
    derived by enumeration to match the generator's `kpi_{idx:03d}` scheme.
    """
    undefined: list[dict[str, str]] = []
    for idx, kpi in enumerate(registry.get("kpis") or [], start=1):
        metric = str(kpi.get("metric") or "").strip()
        cuts = str(kpi.get("cuts") or "").strip()
        if metric or cuts:
            continue
        undefined.append(
            {
                "kpi_id": f"kpi_{idx:03d}",
                "kpi": str(kpi.get("name") or kpi.get("business_question") or ""),
            }
        )
    return undefined


def _result_packet_stale_kpis(repo_root: Path, workspace_rel: str) -> list[str]:
    """Return the kpi_ids whose on-disk SQL no longer matches the SQL the result
    packet was generated from.

    The packet records each KPI's `sql_sha256` at generation time (the SQL was
    re-executed then, so the preview is fresh by construction). If a .sql file is
    hand-edited afterwards and the flow blocks before regenerating, the packet on
    disk is out of date. Comparing the recorded hash to the current file hash
    catches that, so the presenter never shows results that no longer match the
    on-disk SQL as authoritative `ok` (BUG-015 / BUG-024 follow-on)."""
    packet = repo_root / workspace_rel / "interns" / "generated" / "evidence" / "kpi_results" / "current.json"
    if not packet.exists():
        return []
    try:
        data = json.loads(packet.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return []
    stale: list[str] = []
    for entry in data.get("kpis") or []:
        if not isinstance(entry, dict):
            continue
        kpi_id = str(entry.get("kpi_id") or "")
        recorded = entry.get("sql_sha256")
        sql_rel = entry.get("sql_path")
        if not kpi_id or not recorded or not sql_rel:
            continue
        sql_file = repo_root / str(sql_rel)
        if not sql_file.exists():
            stale.append(kpi_id)
            continue
        current = hashlib.sha256(sql_file.read_text(encoding="utf-8").encode("utf-8")).hexdigest()
        if current != recorded:
            stale.append(kpi_id)
    return stale


def _step(stage: str, status: str, detail: dict[str, Any]) -> dict[str, Any]:
    return {"stage": stage, "status": status, "detail": detail, "timestamp": _now()}


def _panel_instruction(panel: dict[str, Any]) -> str:
    return str(panel.get("instruction") or panel.get("recommended_answer") or "Choose an option.")


def _safe_duplicate_option(panel: dict[str, Any]) -> bool:
    recommended = str(panel.get("recommended_option_id") or "")
    for option in panel.get("options") or []:
        if option.get("option_id") == recommended:
            return option.get("action") == "preserve"
    return False


def _next_step(panel: dict[str, Any]) -> str:
    if panel.get("status") == "needs_user_answer":
        return "Answer this panel with `workspace-flow answer --session <id> --answer <option>`."
    if panel.get("status") == "complete":
        return "Review result artifacts or run `workspace-flow results --session <id>`."
    return "Inspect the current panel artifact."


def _new_session_id() -> str:
    return "wf_" + datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def latest_open_session(repo_root: Path, workspace_rel: str, *, max_age_hours: int = 24) -> str | None:
    """Return the most recent workflow_session id for a workspace, if recent.

    Returns None when no session exists, or the latest is older than
    `max_age_hours`, or its status is `complete`. Lets `workspace-flow start`
    resume in-progress work instead of minting yet another session.
    """
    layout = WorkspaceLayout(project_root=(Path(repo_root) / workspace_rel).resolve())
    sessions_dir = layout.workflow_sessions_dir
    if not sessions_dir.exists():
        return None
    candidates = []
    for session_path in sessions_dir.iterdir():
        if not session_path.is_dir():
            continue
        state_file = session_path / "session.json"
        if not state_file.exists():
            continue
        try:
            data = json.loads(state_file.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            continue
        status = str(data.get("status") or data.get("current_panel", {}).get("status") or "")
        if status == "complete":
            continue
        updated = str(data.get("updated_at") or "")
        try:
            updated_dt = datetime.fromisoformat(updated)
        except ValueError:
            continue
        if updated_dt.tzinfo is None:
            updated_dt = updated_dt.replace(tzinfo=timezone.utc)
        age = datetime.now(timezone.utc) - updated_dt
        if age.total_seconds() > max_age_hours * 3600:
            continue
        candidates.append((updated_dt, session_path.name))
    if not candidates:
        return None
    candidates.sort(reverse=True)
    return candidates[0][1]


def latest_session(repo_root: Path, workspace_rel: str) -> str | None:
    """Return the most recent workflow_session id for a workspace, any status.

    Unlike `latest_open_session` this does not filter on status or age:
    `results` / `review` / `status` legitimately target sessions that are
    already complete. Sorted by `updated_at` (session dir mtime as fallback).
    """
    layout = WorkspaceLayout(project_root=(Path(repo_root) / workspace_rel).resolve())
    sessions_dir = layout.workflow_sessions_dir
    if not sessions_dir.exists():
        return None
    candidates: list[tuple[datetime, str]] = []
    for session_path in sessions_dir.iterdir():
        if not session_path.is_dir():
            continue
        state_file = session_path / "session.json"
        if not state_file.exists():
            continue
        updated_dt: datetime | None = None
        try:
            data = json.loads(state_file.read_text(encoding="utf-8"))
            updated_dt = datetime.fromisoformat(str(data.get("updated_at") or ""))
        except (json.JSONDecodeError, OSError, ValueError):
            updated_dt = None
        if updated_dt is None:
            try:
                updated_dt = datetime.fromtimestamp(state_file.stat().st_mtime, tz=timezone.utc)
            except OSError:
                continue
        if updated_dt.tzinfo is None:
            updated_dt = updated_dt.replace(tzinfo=timezone.utc)
        candidates.append((updated_dt, session_path.name))
    if not candidates:
        return None
    candidates.sort(reverse=True)
    return candidates[0][1]


def _resolve_session_id(repo_root: Path, session: str, workspace: str, cmd: str) -> str:
    """Resolve the target session for a session-bound subcommand.

    Explicit `--session` wins. Otherwise `--workspace` resolves to that
    workspace's most recent session (any status). Errors with the exact
    accepted forms instead of a bare `--session is required`, so a driving
    agent can self-correct in one step instead of flailing through flag
    permutations.
    """
    if session:
        return session
    if workspace:
        resolved = latest_session(repo_root, workspace)
        if resolved:
            return resolved
        raise SystemExit(
            f"workspace-flow {cmd}: no workflow sessions found under "
            f"{workspace}/interns/state/workflow_sessions/. "
            f"Run `workspace-flow start --workspace {workspace}` first."
        )
    raise SystemExit(
        f"workspace-flow {cmd}: pass --session <id>, or --workspace <path> "
        f"to target that workspace's latest session."
    )


def write_session_handoff(repo_root: Path, workspace_rel: str, session_id: str) -> str | None:
    """Write a compact handoff doc for a session and return its relative path.

    Generic across workspaces. Persists under
    `<workspace>/interns/state/handoffs/<session_id>.md` so the next session
    can read prior context without paging through the full transcript.
    Returns None if the session does not exist.
    """
    repo_root = Path(repo_root).resolve()
    layout = WorkspaceLayout(project_root=(repo_root / workspace_rel).resolve())
    state_path = layout.workflow_sessions_dir / session_id / "session.json"
    if not state_path.exists():
        return None
    try:
        state = json.loads(state_path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return None
    panel = state.get("current_panel") or {}
    layout.handoffs_dir.mkdir(parents=True, exist_ok=True)
    out_path = layout.handoffs_dir / f"{session_id}.md"
    diff = compute_workflow_diff(repo_root, workspace_rel)
    lines = [
        f"# Handoff: session `{session_id}`",
        "",
        f"- Workspace: `{workspace_rel}`",
        f"- Last updated: {state.get('updated_at', '')}",
        f"- Stage: `{panel.get('stage', state.get('stage', ''))}`",
        f"- Status: `{panel.get('status', state.get('status', ''))}`",
        "",
        "## Snapshot",
        "",
        f"- KPI count: {diff.get('kpi_count', 0)}",
        f"- Ready KPIs: {diff.get('ready_kpi_count', 0)}",
        f"- Blocked KPIs: {diff.get('blocked_kpi_count', 0)}",
        f"- Executable relationships: {len(diff.get('executable_relationship_ids') or [])}",
        f"- Pending relationships: {len(diff.get('pending_relationship_ids') or [])}",
        "",
        "## Open Recovery Commands",
        "",
    ]
    has_recovery = False
    for gap in diff.get("kpi_gaps") or []:
        for cmd in gap.get("recovery_commands") or []:
            has_recovery = True
            lines.append(f"- **{gap.get('kpi_id', '')}** — {cmd.get('label', '')}")
            lines.append(f"  - `{cmd.get('command', '')}`")
    if not has_recovery:
        lines.append("- (none — workspace appears unblocked)")
    lines.extend(
        [
            "",
            "## Next Step",
            "",
            "Resume by reading the panel artifact, then call `workspace-flow status "
            f"--workspace {workspace_rel} --diff` to confirm state is still current.",
            "",
        ]
    )
    out_path.write_text("\n".join(lines), encoding="utf-8")
    return _rel(out_path, repo_root)


def compute_workflow_diff(repo_root: Path, workspace_rel: str) -> dict[str, Any]:
    """Return a generic per-KPI gap report + recovery commands.

    Reads existing artifacts only — does NOT mutate state or re-run the
    pipeline. The orchestrating agent uses this to learn what to fix
    without burning a full workspace-flow start cycle.
    """
    repo_root = Path(repo_root).resolve()
    layout = WorkspaceLayout(project_root=(repo_root / workspace_rel).resolve())
    plan = _read_json(layout.source_to_target_plan_path)
    relationships = _read_json(layout.relationship_contracts_path)
    blocker_panel = _read_json(layout.reports_dir / "blocker_question_panel" / "current.json")
    # The feature resolver (kpi_feature_mapping.json) is the authoritative view
    # of per-KPI readiness; the source-to-target plan can be stale relative to a
    # freshly-resolved mapping. Read it so the headline counts reconcile with the
    # resolver instead of contradicting it (status once reported 20 ready while
    # resolve-kpi-features reported 20 blocked, from a stale plan).
    feature_mapping = _read_json(layout.contracts_dir / "kpi_feature_mapping.json")
    mapping_blocked_ids = {
        str(k.get("kpi_id") or "")
        for k in (feature_mapping.get("kpis") or [])
        if isinstance(k, dict) and k.get("status") != "ready_for_sql"
    }
    feature_mapping_summary = feature_mapping.get("summary") or {}
    rels_by_id: dict[str, dict[str, Any]] = {
        str(r.get("relationship_id", "")): r
        for r in (relationships.get("relationships") or [])
        if isinstance(r, dict)
    }
    executable_rel_ids = sorted(rid for rid, r in rels_by_id.items() if r.get("state") in {"user_confirmed", "proven_data_model"})
    pending_rel_ids = sorted(rid for rid, r in rels_by_id.items() if r.get("state") not in {"user_confirmed", "proven_data_model", "rejected"})

    def _norm(p: str) -> str:
        return str(p or "").replace("\\", "/").lower()

    kpi_gaps: list[dict[str, Any]] = []
    for kpi in plan.get("kpis") or []:
        if not isinstance(kpi, dict):
            continue
        kpi_id = str(kpi.get("kpi_id") or "")
        status = str(kpi.get("status") or "")
        # Partial-completion: a `deferred` KPI (no measurable definition) is
        # intentionally skipped this pass. It is not a blocker and must not emit
        # feature/relationship recovery commands -- record it as a deferred gap
        # so it stays visible, then move on.
        if status == "deferred" or kpi.get("deferred"):
            kpi_gaps.append(
                {
                    "kpi_id": kpi_id,
                    "status": "deferred",
                    "blockers": [],
                    "deferred_reason": str(kpi.get("deferred_reason") or ""),
                    "missing_features": [],
                    "selected_source_datasets": [],
                    "recovery_commands": [],
                }
            )
            continue
        blockers = kpi.get("blockers") or []
        selected_sources = {
            _norm(s) for s in (
                kpi.get("selected_source_datasets")
                or kpi.get("selected_sources")
                or []
            )
        }
        recovery: list[dict[str, str]] = []
        seen_in_kpi: set[str] = set()
        for blocker in blockers:
            code = str(blocker.get("code") or "") if isinstance(blocker, dict) else str(blocker)
            if code == "join_proof_missing":
                for rid in pending_rel_ids:
                    rel = rels_by_id.get(rid) or {}
                    left = _norm(str(rel.get("left_dataset") or ""))
                    right = _norm(str(rel.get("right_dataset") or ""))
                    if selected_sources and not (left in selected_sources and right in selected_sources):
                        continue
                    if rid in seen_in_kpi:
                        continue
                    seen_in_kpi.add(rid)
                    recovery.append(
                        {
                            "label": f"Approve relationship `{rid}` connecting this KPI's selected sources.",
                            "command": (
                                f"uv run apply-relationship-answer --workspace {workspace_rel} "
                                f"--relationship-id {rid} --answer approve"
                            ),
                            "why": f"join_proof_missing for `{kpi_id}` — relationship `{rid}` is the executable link between its selected source datasets",
                        }
                    )
                if not recovery and selected_sources:
                    needed_pairs = sorted(
                        {
                            f"{a} ↔ {b}"
                            for a in selected_sources
                            for b in selected_sources
                            if a < b
                            and not any(
                                {_norm(r.get("left_dataset") or ""), _norm(r.get("right_dataset") or "")}
                                == {a, b}
                                and r.get("state") in {"user_confirmed", "proven_data_model"}
                                for r in rels_by_id.values()
                            )
                        }
                    )
                    for pair in needed_pairs:
                        recovery.append(
                            {
                                "label": f"No relationship candidate yet between {pair} — build one.",
                                "command": (
                                    f"uv run build-relationship-contracts --workspace {workspace_rel}"
                                ),
                                "why": f"join_proof_missing for `{kpi_id}` — neither inferred nor user-supplied; rebuild from latest profiles/docs first",
                            }
                        )
            elif code == "feature_not_ready":
                recovery.append(
                    {
                        "label": "Resolve remaining KPI feature blockers via the panel.",
                        "command": f"uv run prepare-kpi-blocker-panel --workspace {workspace_rel}",
                        "why": code,
                    }
                )
        kpi_gaps.append(
            {
                "kpi_id": kpi_id,
                "status": status,
                "blockers": blockers,
                "missing_features": kpi.get("missing_features") or [],
                "selected_source_datasets": sorted(selected_sources),
                "recovery_commands": recovery,
            }
        )

    if blocker_panel.get("status") == "needs_user_answer":
        feature = str(blocker_panel.get("feature") or "")
        kpi_gaps.append(
            {
                "kpi_id": "blocker_panel",
                "status": "needs_user_answer",
                "blockers": [{"code": "panel_open", "feature": feature}],
                "missing_features": [feature] if feature else [],
                "recovery_commands": [
                    {
                        "label": f"Answer the open blocker panel for `{feature}`.",
                        "command": (
                            f"uv run apply-kpi-panel-answer --workspace {workspace_rel} "
                            f"--answer <option_id|custom>"
                        ),
                        "why": "open blocker panel awaits user decision",
                    }
                ],
            }
        )

    # A KPI is "ready" only if BOTH gates agree: the plan marks it
    # ready_for_generation AND the resolver did not leave it blocked. This makes
    # the headline counts conservative and consistent with resolve-kpi-features —
    # status can no longer claim a KPI is ready while the resolver blocks it.
    plan_ready = [
        g for g in kpi_gaps if g.get("status") == "ready_for_generation"
    ]
    reconciled_ready = [
        g for g in plan_ready if g.get("kpi_id") not in mapping_blocked_ids
    ]
    ready_kpi_count = len(reconciled_ready)
    blocked_kpi_count = sum(
        1 for g in kpi_gaps if g.get("status") == "blocked"
    ) + (len(plan_ready) - len(reconciled_ready))
    mapping_blocked_count = feature_mapping_summary.get("blocked_kpi_count")
    counts_consistent = (
        not feature_mapping
        or mapping_blocked_count is None
        or len(plan_ready) - len(reconciled_ready) == 0
    )
    return {
        "workspace": workspace_rel,
        "plan_present": bool(plan),
        "kpi_count": len(plan.get("kpis") or []),
        "executable_relationship_ids": executable_rel_ids,
        "pending_relationship_ids": pending_rel_ids,
        "kpi_gaps": kpi_gaps,
        "ready_kpi_count": ready_kpi_count,
        "blocked_kpi_count": blocked_kpi_count,
        "feature_mapping_summary": feature_mapping_summary,
        # False when the plan claimed KPIs ready that the resolver still blocks
        # (a stale source-to-target plan). Consumers should re-resolve/re-plan.
        "counts_consistent": counts_consistent,
    }


def _collect_gate_provenance(
    state: dict[str, Any],
    layout: "WorkspaceLayout",
) -> list[dict[str, Any]]:
    """Gather gate provenance for the workspace/session.

    Returns a list of gate records, each with:
      gate        - short identifier for the gate
      source      - 'human' | 'agent'
      confirmed_by - name (or '' when source is 'agent')
      label       - human-readable description
    """
    gates: list[dict[str, Any]] = []

    # (a) Relationship approvals — read relationship_contracts.json
    rel_path = layout.relationship_contracts_path
    if rel_path.exists():
        try:
            contracts_data = json.loads(rel_path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            contracts_data = {}
        for rel in (contracts_data.get("relationships") or []):
            if not isinstance(rel, dict):
                continue
            rel_id = str(rel.get("relationship_id") or "")
            state_val = str(rel.get("state") or "")
            # Only report gates for executable / user_decided relationships
            if state_val not in {"user_confirmed", "proven_data_model", "rejected"}:
                continue
            approval = rel.get("approval") or {}
            source = str(approval.get("source") or "agent")
            confirmed_by = str(approval.get("confirmed_by") or "")
            gates.append({
                "gate": f"relationship:{rel_id}",
                "source": source,
                "confirmed_by": confirmed_by,
                "label": f"Relationship `{rel_id}` ({state_val})",
            })

    # (b) KPI-analyst review — from the recorded review in session state
    review = state.get("kpi_analyst_review") or {}
    if review:
        source = str(review.get("source") or "agent")
        confirmed_by = str(review.get("confirmed_by") or "")
        verdict = str(review.get("verdict") or "")
        gates.append({
            "gate": "kpi_analyst_review",
            "source": source,
            "confirmed_by": confirmed_by,
            "label": f"KPI-analyst review (verdict: {verdict})",
        })

    # (c) KPI intent-contract facets — low-confidence/defaulted facets are
    # agent-asserted unless a human answered them via the blocker panel.
    # Only present when build-intent-contract has been run for the workspace.
    ic_path = layout.contracts_dir / "kpi_intent_contract.json"
    if ic_path.exists():
        try:
            from core.onboarding.kpi.intent_contract import (
                answer_key,
                load_intent_answers,
                low_confidence_facets,
            )

            ic_data = json.loads(ic_path.read_text(encoding="utf-8"))
            answers = load_intent_answers(layout)
            for contract in ic_data.get("contracts") or []:
                if not isinstance(contract, dict):
                    continue
                kpi_id = str(contract.get("kpi_id") or "")
                for facet_q in low_confidence_facets(contract):
                    facet = str(facet_q.get("facet") or "")
                    ans = answers.get(answer_key(kpi_id, facet))
                    if ans:
                        gates.append({
                            "gate": f"intent:{kpi_id}:{facet}",
                            "source": str(ans.get("source") or "human"),
                            "confirmed_by": str(ans.get("confirmed_by") or ""),
                            "label": (
                                f"Intent facet `{facet}` for {kpi_id} = "
                                f"{ans.get('value')!r}"
                            ),
                        })
                    else:
                        gates.append({
                            "gate": f"intent:{kpi_id}:{facet}",
                            "source": "agent",
                            "confirmed_by": "",
                            "label": (
                                f"Intent facet `{facet}` for {kpi_id} is "
                                f"{facet_q.get('confidence')}-confidence and unanswered "
                                "(agent default)"
                            ),
                        })
        except Exception:  # pragma: no cover - defensive; provenance is additive
            pass

    return gates


def _render_gate_provenance_lines(gates: list[dict[str, Any]]) -> list[str]:
    """Render gate provenance as Markdown lines."""
    if not gates:
        return ["## Gate Provenance", "", "- (no executable gates recorded)", ""]
    lines = ["## Gate Provenance", ""]
    for gate in gates:
        source = gate.get("source", "agent")
        confirmed_by = gate.get("confirmed_by", "")
        label = gate.get("label", gate.get("gate", ""))
        if source == "human" and confirmed_by:
            lines.append(f"- [ok] human:{confirmed_by} — {label}")
        else:
            lines.append(f"- [~] agent-asserted (no human confirmation) — {label}")
    lines.append("")
    return lines


def _gate_provenance_headline(gates: list[dict[str, Any]]) -> str:
    """Return a completion headline that reflects gate provenance.

    If any gate is agent-asserted, the headline includes a warning count.
    Returns a plain string (no trailing newline).
    """
    agent_count = sum(1 for g in gates if g.get("source") != "human")
    if agent_count:
        return (
            f"[~] Completed WITHOUT human confirmation on {agent_count} gate(s) "
            "(pass --require-human-gates to block on agent-asserted gates)"
        )
    return "[ok] Completed — all gates confirmed by humans"


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _rel(path: Path, root: Path) -> str:
    try:
        return path.resolve().relative_to(root.resolve()).as_posix()
    except ValueError:
        return path.as_posix()


def _active_run_paths(
    repo_root: Path, workspace_rel: str
) -> tuple[str, list[str]] | None:
    """Resolve the dated ``runs/<date>/`` snapshot to advertise as the active run.

    Prefers today's directory (how ``_write_runs_snapshot`` names it) and falls
    back to the most recent dated directory that has a ``results.md``. Returns
    ``(results.md rel path, [kpi_*.md rel paths])`` or ``None`` if no run snapshot
    exists. Used to print one stable, single-file surface a driving CLI can read
    and forward verbatim — instead of re-reading a UI-truncated inline packet.
    """
    runs_base = repo_root / workspace_rel / "interns" / "runs"
    if not runs_base.is_dir():
        return None
    candidate = runs_base / date.today().isoformat()
    if not (candidate / "results.md").exists():
        dated = sorted(
            (
                p
                for p in runs_base.iterdir()
                if p.is_dir() and (p / "results.md").exists()
            ),
            key=lambda p: p.name,
        )
        if not dated:
            return None
        candidate = dated[-1]
    results_rel = _rel(candidate / "results.md", repo_root)
    kpi_rels = [_rel(p, repo_root) for p in sorted(candidate.glob("kpi_*.md"))]
    return results_rel, kpi_rels


def _emit_result_packet(
    repo_root: Path,
    workspace_rel: str,
    *,
    compact: bool = False,
    kpi_id: str = "",
) -> bool:
    """Print the KPI Result Packet + Active Run pointer for a workspace.

    Source selection (first match wins):
      * ``kpi_id`` set -> the per-KPI file ``runs/<date>/<kpi_id>.md`` (full, with
        SQL): "give me just this KPI's solution". Falls back to the combined packet
        with a note if that KPI file is absent.
      * ``compact`` -> ``reports/kpi_results/current_compact.md`` (SQL linked, not
        inlined; falls back to the full ``current.md`` for runs predating it). Used
        by the auto-surface at completion/review so the emitted output stays small
        enough not to trip CLI truncation (which agents misread as a failed read).
      * otherwise -> the full ``reports/kpi_results/current.md``.

    Then prints the ``## Active Run`` tail pointer naming the dated ``runs/<date>/``
    files. Returns ``True`` if a packet was emitted.

    Shared by the ``workspace-flow`` panel printer and the ``run-kpi-pipeline``
    review-gate stop so the SQL + result rows surface automatically the moment the
    flow stops — no separate "show results" call (the rule: having to ask is a bug).
    """
    reports = repo_root / workspace_rel / "interns" / "reports" / "kpi_results"
    note = ""
    md_path: Path | None = None
    if kpi_id:
        active = _active_run_paths(repo_root, workspace_rel)
        if active:
            _results_rel, kpi_rels = active
            match = next((r for r in kpi_rels if Path(r).stem == kpi_id), "")
            if match:
                md_path = repo_root / match
        if md_path is None:
            note = f"[~] {kpi_id} not found in the latest run — showing the combined packet."
    if md_path is None and compact:
        # current.md IS the compact packet now; current_compact.md kept as alias.
        md_path = reports / "current.md"
        if not md_path.exists():
            md_path = reports / "current_compact.md"
    if md_path is None:
        # Full packet (explicit `results --full`): current_full.md; fall back to
        # current.md for runs predating the split.
        full_path = reports / "current_full.md"
        md_path = full_path if full_path.exists() else reports / "current.md"
    if not md_path.exists():
        return False
    # Packet integrity (BUG-015/BUG-024 follow-on): warn loudly if the on-disk SQL
    # changed after this packet was generated, so the rows are not mistaken for
    # results that still match the SQL.
    stale_kpis = _result_packet_stale_kpis(repo_root, workspace_rel)
    print("")
    print("## KPI Result Packet" + (f": {kpi_id}" if kpi_id else ""))
    print("")
    if note:
        print(note)
        print("")
    if stale_kpis:
        print(f"[x] STALE — on-disk SQL changed after this packet was built for: {', '.join(stale_kpis)}.")
        print("    Re-run `workspace-flow results` to regenerate; the rows below may not match the current SQL.")
        print("")
    print(md_path.read_text(encoding="utf-8").rstrip())
    print("")
    # Active-run surface (token/loop guardrail): name the stable, dated results file
    # as the ONE thing to read and forward. The packet printed above can be UI-
    # truncated by a CLI frontend ("... first N lines hidden ..."); agents have
    # misread that truncation as a read FAILURE and looped re-reading the results in
    # many forms (-TotalCount/-Head/-Tail/Select-String) plus opening the heavy
    # evidence JSON — burning quota and never presenting. Printing the dated file at
    # the visible tail gives any driving CLI a single cheap surface to forward.
    active_run = _active_run_paths(repo_root, workspace_rel)
    if active_run:
        run_rel, kpi_rels = active_run
        print("")
        print("## Active Run")
        print("")
        print(f"- Results (combined): `{run_rel}`")
        for kpi_rel in kpi_rels:
            print(f"- Per-KPI: `{kpi_rel}`")
        print("")
        print(
            "- To present: read each file ONCE with your NATIVE file reader (ReadFile / "
            "read_file), NOT a shell command (Get-Content/cat) — shell output is summarized/"
            "capped per the CLI's tokenBudget, which truncates a long read and makes agents "
            "loop re-reading it. These files are compact (SQL is linked, not inlined) so one "
            "read returns the whole file."
        )
        print(
            "- A `... first N lines hidden ...` notice means the read SUCCEEDED (UI display "
            "truncation, not a failure). Do NOT re-read with -TotalCount/-Head/-Tail/-Raw/"
            "-Encoding/Select-String, and do not open the evidence JSON."
        )
        print(
            "- Forwarding many KPIs: forward the `Per-KPI` files (one read each, each self-"
            "contained) rather than the combined file — this never exceeds the read cap "
            "regardless of KPI count."
        )
    return True


def _print_cli_panel(
    repo_root: Path, result: WorkspaceFlowResult, *, kpi_filter: str = "", full: bool = False
) -> None:
    panel_path = repo_root / result.current_panel_path if result.current_panel_path else None
    panel_payload: dict[str, Any] = {}
    delegations: list[dict[str, Any]] = []
    if panel_path and panel_path.exists():
        try:
            panel_payload = json.loads(panel_path.read_text(encoding="utf-8"))
            delegations = (panel_payload.get("summary") or {}).get("delegations") or []
        except (json.JSONDecodeError, OSError):
            delegations = []
    if delegations:
        print("## Specialist Reviews")
        print("")
        for event in delegations:
            agent = event.get("agent", "")
            stage = event.get("stage", "")
            verdict = event.get("verdict") or {}
            status = verdict.get("status", "")
            summary_text = verdict.get("summary", "")
            # BUG-014: surface provenance (agent-asserted vs human-confirmed)
            provenance = verdict.get("details", {}).get("source", "agent") if isinstance(verdict.get("details"), dict) else "agent"
            confirmed_by = verdict.get("details", {}).get("confirmed_by", "") if isinstance(verdict.get("details"), dict) else ""
            provenance_tag = f"[human:{confirmed_by}]" if provenance == "human" and confirmed_by else f"[{provenance}]"
            if stage == "kpi_output_verification" and (agent == "kpi-analyst"):
                print(f"- `{agent}` @ `{stage}` → **{status}** {provenance_tag} — {summary_text}")
            else:
                print(f"- `{agent}` @ `{stage}` → **{status}** — {summary_text}")
        print("")
    # BUG-014: for the kpi_analyst_review panel, surface provenance in status line
    kpi_review = (panel_payload.get("summary") or {}).get("kpi_analyst_review") or {}
    if kpi_review and result.stage in ("complete", "kpi_analyst_review_blocked"):
        review_source = str(kpi_review.get("source") or "agent")
        review_confirmed_by = str(kpi_review.get("confirmed_by") or "")
        if review_source == "human" and review_confirmed_by:
            print(f"[ok] Review verdict: **{kpi_review.get('verdict', '')}** [human:{review_confirmed_by}]")
        else:
            print(f"[~] Review verdict: **{kpi_review.get('verdict', '')}** [agent-asserted — use --confirmed-by to record human confirmation]")
        print("")
    markdown_path = repo_root / result.current_markdown_path
    if markdown_path.exists():
        print(markdown_path.read_text(encoding="utf-8").rstrip())
    else:
        print(f"# Workspace Flow: {result.stage}")
        print("")
        print(f"- Workspace: `{result.workspace}`")
        print(f"- Status: `{result.status}`")
    # BUG-014 (enforcement): on completion, print the Gate Provenance section
    # so the operator can see which gates were human-confirmed vs agent-asserted.
    if result.stage == "complete":
        provenance = (panel_payload.get("summary") or {}).get("gate_provenance") or []
        headline = (panel_payload.get("summary") or {}).get("completion_headline") or ""
        if headline:
            print("")
            print(headline)
        print("")
        for line in _render_gate_provenance_lines(provenance):
            print(line)
    # BUG-013 / BUG-016: on completion OR explicit `results` call, emit the full
    # kpi_results packet so any driving agent sees KPI + SQL + result rows without
    # needing a separate call.  For `results` stage the packet IS the point of the
    # command; for `complete` it surfaces automatically so agents driving via the
    # CLI see rows immediately after the workflow finishes. The `kpi_analyst_review`
    # gate is included because results are already executed by then (preview is
    # written before the gate) and the reviewer needs the SQL + rows to judge intent
    # — so the packet auto-surfaces when the flow STOPS for review, with no separate
    # "show results" call (KPI Result Packet Forwarding Rule: having to ask is a bug).
    if result.stage in ("complete", "results", "kpi_analyst_review") and result.workspace:
        # Compact everywhere by default so the emitted output never UI-truncates (a
        # truncation banner is what agents misread as a failed read -> re-read loop /
        # paraphrase). The auto-surfaces (complete / review gate) are always compact;
        # the explicit `results` command is compact unless `--full` is passed (full SQL
        # also always lives in the per-KPI .sql files). `--kpi` forwards one KPI's file.
        if result.stage == "results":
            compact = not full
        else:
            compact = True
        _emit_result_packet(repo_root, result.workspace, compact=compact, kpi_id=kpi_filter)
    print("")
    print("## Next Step")
    print("")
    print(result.next_step)
    print("")
    print("## Panel Artifacts")
    print("")
    print(f"- JSON: `{result.current_panel_path}`")
    print(f"- Markdown: `{result.current_markdown_path}`")


# Maps a flow PANEL stage to the canonical STAGE_ROUTING key whose specialist +
# skill roster owns it. This is what makes every stage automatically present its
# full relevant roster to the orchestrator (required_specialists + suggested_skills),
# instead of a few panels hand-listing them and the rest going idle. Unmapped
# panel stages attach no roster (rather than a wrong one).
_PANEL_STAGE_TO_ROUTING: dict[str, str] = {
    "start": "flow_entry",
    "workflow_checkpoint": "flow_entry",
    "kpi_generation_route": "kpi_definition",
    "kpi_blocker": "kpi_definition",
    "kpi_format_confirmation": "kpi_definition",
    "data_quality_duplicate_review": "artifact_validation",
    "relationship_blocked": "relationship_review",
    "source_to_target_blocked": "source_to_target_review",
    "kpi_analyst_review": "kpi_output_verification",
    "kpi_analyst_review_blocked": "kpi_output_verification",
    "complete": "kpi_completion_review",
    "results": "result_review",
}


def _routing_stage_for_panel(stage: str) -> str:
    if stage in _PANEL_STAGE_TO_ROUTING:
        return _PANEL_STAGE_TO_ROUTING[stage]
    if stage.startswith("kpi_generation"):
        return "kpi_definition"
    return stage if stage in STAGE_ROUTING else ""


def _attach_stage_routing(panel: dict[str, Any]) -> None:
    """Attach the stage's full agent + skill roster to the panel so the
    orchestrator activates the right specialists/skills at every stage — not only
    the handful of panels that used to hand-list them. Existing panel-specific
    entries are preserved; the routed roster is merged in (union, de-duplicated).
    """
    routing_stage = _routing_stage_for_panel(str(panel.get("stage") or ""))
    if not routing_stage:
        return
    roster = routing_for(routing_stage)
    if not roster["agents"] and not roster["skills"]:
        return
    summary = panel.setdefault("summary", {})

    agents = list(summary.get("required_specialists") or [])
    for agent in roster["agents"]:
        if agent not in agents:
            agents.append(agent)
    if agents:
        summary["required_specialists"] = agents

    skills = list(summary.get("suggested_skills") or panel.get("suggested_skills") or [])
    have = {s.get("name") if isinstance(s, dict) else s for s in skills}
    for skill in roster["skills"]:
        if skill not in have:
            skills.append(skill)
            have.add(skill)
    if skills:
        summary["suggested_skills"] = skills


def _kpi_review_signature(completed_kpis: list[dict[str, Any]]) -> str:
    """Stable fingerprint of the KPI INTENT a review is bound to.

    Keyed on each KPI's definition fields that determine intent — metric, cuts,
    filters, business question — NOT the generated SQL text. The SQL legitimately
    evolves across runs (e.g. a CSV reader becomes a delta_scan once the medallion
    bronze layer materializes) without the intent changing, so binding to SQL
    would re-gate spuriously. Binding to intent means a recorded kpi-analyst
    verdict stays valid until the metric/cuts/filters actually change, which is
    exactly when a re-review is warranted. Workspace-agnostic: hashes only
    structural definition fields already present on each entry.
    """
    def _intent(entry: dict[str, Any]) -> tuple[str, ...]:
        defn = entry.get("definition") if isinstance(entry.get("definition"), dict) else {}

        def field(key: str) -> str:
            value = defn.get(key) if defn.get(key) is not None else entry.get(key)
            return str(value or "")

        return (
            str(entry.get("kpi_id") or defn.get("kpi_id") or ""),
            field("metric"),
            field("cuts"),
            field("filters"),
            field("business_question") or field("name"),
        )

    basis = sorted(_intent(k) for k in (completed_kpis or []))
    payload = json.dumps(basis, sort_keys=True, default=str)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _utf8_safe_stdio() -> None:
    """Make stdout/stderr UTF-8 tolerant on cp1252 consoles.

    Result packets / generated SQL can contain non-ASCII (em-dash, arrow),
    which crashes a Windows cp1252 console with UnicodeEncodeError when piped.
    `errors="replace"` degrades gracefully instead of aborting mid-output
    (which derails a driving CLI). Shared by every CLI entry point that prints
    packets — main() AND pipeline_main() (the latter crashed on '→' when
    only main() had the guard).
    """
    import sys

    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[attr-defined]
        except (AttributeError, ValueError):
            pass


_SUBCOMMANDS: frozenset[str] = frozenset(
    {"start", "status", "answer", "results", "review", "artifacts",
     "handoff", "context-status", "skill-excerpt", "gc"}
)


def _args_before_subcommand(argv: list[str]) -> list[str]:
    """Return the portion of argv that appears before the first known subcommand.

    Used by BUG-019 to detect top-level --quiet before argparse runs, so the
    merged quiet flag is correct regardless of argument order.
    """
    result: list[str] = []
    for token in argv:
        if token in _SUBCOMMANDS:
            break
        result.append(token)
    return result


def main(argv: list[str] | None = None) -> int:
    _utf8_safe_stdio()
    # BUG-019: --quiet must be accepted both at the top level
    # (workspace-flow --quiet status --diff) AND after the subcommand
    # (workspace-flow status --diff --quiet).
    # Strategy: parse the raw argv for a pre-subcommand --quiet first, then
    # add --quiet to every subparser.  After parse_args we OR the two values
    # so either placement is honoured without either overriding the other.
    # `--json` gets the same treatment: driving agents naturally append it
    # after the subcommand, and "unrecognized arguments: --json" triggers a
    # retry/flail loop.
    _toplevel_quiet: bool = bool(argv and "--quiet" in _args_before_subcommand(argv))
    _toplevel_json: bool = bool(argv and "--json" in _args_before_subcommand(argv))

    parser = argparse.ArgumentParser(prog="workspace-flow")
    parser.add_argument("--repo-root", default=str(PROJECT_ROOT))
    parser.add_argument("--json", action="store_true", help="Print the machine-readable result summary only.")
    parser.add_argument(
        "--quiet", action="store_true",
        help="For status --diff: print a compact KPI-readiness summary + artifact paths instead of the full diff JSON.",
    )
    sub = parser.add_subparsers(dest="cmd", required=True)

    def _add_quiet(p: argparse.ArgumentParser) -> None:
        """Add --quiet to a subparser so it is accepted after the subcommand name."""
        p.add_argument(
            "--quiet", action="store_true",
            help="Print a compact summary + artifact paths instead of full JSON.",
        )

    def _add_session_or_workspace(p: argparse.ArgumentParser) -> None:
        """Session-bound subcommands accept --session OR --workspace.

        --workspace resolves to that workspace's most recent session, so a
        driving agent that only knows the workspace path never errors on a
        missing --session.
        """
        p.add_argument("--session", default="", help="Workflow session id (e.g. wf_...).")
        p.add_argument(
            "--workspace", default="",
            help="Workspace path; targets its latest session when --session is omitted.",
        )

    start = sub.add_parser("start")
    _add_quiet(start)
    start.add_argument("--workspace", required=True)
    start.add_argument("--domain", default="generic")
    start.add_argument("--intent", choices=sorted(INTENTS), default="kpi_generation")
    start.add_argument("--mode", choices=sorted(ORCHESTRATION_MODES), default="local-safe")
    start.add_argument(
        "--new-session",
        action="store_true",
        help="Force minting a new session even if an in-progress one exists.",
    )

    status = sub.add_parser("status")
    _add_quiet(status)
    _add_session_or_workspace(status)
    status.add_argument(
        "--diff",
        action="store_true",
        help="Read existing artifacts and report per-KPI gaps + recovery commands without re-running the pipeline.",
    )

    answer = sub.add_parser("answer")
    _add_quiet(answer)
    _add_session_or_workspace(answer)
    answer.add_argument("--answer", required=True)
    answer.add_argument("--custom-definition", default="")
    answer.add_argument("--evidence-note", default="")

    results = sub.add_parser("results")
    _add_quiet(results)
    _add_session_or_workspace(results)
    results.add_argument("--preview-rows", type=int, default=PREVIEW_ROW_CAP)
    results.add_argument(
        "--kpi", default="",
        help="Emit only this KPI's packet (e.g. kpi_002), forwarding its per-KPI run file.",
    )
    results.add_argument(
        "--full", action="store_true",
        help="Inline the full SQL per KPI (default is compact: SQL linked, tables shown). "
             "Full SQL also always lives in the per-KPI .sql files.",
    )

    review_p = sub.add_parser("review")
    _add_quiet(review_p)
    _add_session_or_workspace(review_p)
    review_p.add_argument(
        "--verdict", choices=["ok", "blocked"], required=True,
        help="kpi-analyst's semantic verdict: ok = every KPI answers its intent; blocked = at least one does not.",
    )
    review_p.add_argument("--summary", default="", help="One-line summary of the kpi-analyst review.")
    review_p.add_argument(
        "--kpi-notes", default="",
        help="Optional JSON list of per-KPI judgements, e.g. '[{\"kpi_id\":\"kpi_002\",\"status\":\"ok\",\"note\":\"...\"}]'.",
    )
    # BUG-014: --confirmed-by records the human identity when the verdict is
    # human-confirmed rather than agent-asserted.
    review_p.add_argument(
        "--confirmed-by", default="",
        help=(
            "Name or identity of the human reviewer confirming this verdict. "
            "When provided the verdict is recorded as source: human. "
            "Omit to record as source: agent (machine-asserted)."
        ),
    )
    # BUG-014 (enforcement): strict opt-in to block completion when any required
    # gate is agent-asserted rather than human-confirmed.
    review_p.add_argument(
        "--require-human-gates",
        action="store_true",
        help=(
            "Block completion (exit non-zero) if any required gate is agent-asserted. "
            "By default completion proceeds with a visible [~] warning. "
            "Requires --confirmed-by on each gate or prior human approvals."
        ),
    )

    artifacts = sub.add_parser("artifacts")
    artifacts.add_argument("--workspace", required=True)
    artifacts.add_argument(
        "--write-manifest",
        action="store_true",
        help="Also write interns/MANIFEST.md + interns/MANIFEST.json.",
    )
    artifacts.add_argument(
        "--print-gitignore",
        action="store_true",
        help="Print suggested .gitignore patterns for non-source-of-truth artifacts.",
    )

    handoff = sub.add_parser("handoff")
    handoff.add_argument("--workspace", required=True)
    handoff.add_argument(
        "--session",
        default="",
        help="Specific session to compact. Defaults to the latest open session.",
    )

    context = sub.add_parser("context-status")
    context.add_argument("--workspace", required=True)
    context.add_argument("--session", default="")
    context.add_argument(
        "--budget-kb",
        type=int,
        default=180,
        help="Approximate orchestrator context budget in KB (default 180).",
    )

    excerpt = sub.add_parser("skill-excerpt")
    excerpt.add_argument("--skill", required=True, help="Skill name (folder under skills/).")
    excerpt.add_argument(
        "--section",
        default="",
        help="Section heading to extract (fuzzy match). Empty returns the whole body.",
    )

    gc = sub.add_parser("gc")
    gc.add_argument("--workspace", required=True)
    gc.add_argument(
        "--apply",
        action="store_true",
        help="Execute the GC (default is dry-run: shows what would be deleted).",
    )
    gc.add_argument(
        "--max-session-age-hours",
        type=int,
        default=DEFAULT_MAX_SESSION_AGE_HOURS,
        help="Delete workflow_sessions/ subdirs inactive longer than this (and any with status=complete).",
    )
    gc.add_argument(
        "--max-log-mb",
        type=int,
        default=DEFAULT_MAX_LOG_MB,
        help="Rotate trajectory.jsonl / events.jsonl when over this size.",
    )

    # Accept --json after any subcommand name (same BUG-019 rationale as --quiet).
    for _subparser in sub.choices.values():
        if not any(action.dest == "json" for action in _subparser._actions):
            _subparser.add_argument(
                "--json", action="store_true",
                help="Print the machine-readable result summary only.",
            )

    args = parser.parse_args(argv)
    # BUG-019: merge top-level --quiet/--json (pre-subcommand) with subparser
    # values (post-subcommand) so both orderings are honoured.
    args.quiet = bool(args.quiet) or _toplevel_quiet
    args.json = bool(getattr(args, "json", False)) or _toplevel_json
    if args.cmd == "start":
        repo_root_path = Path(args.repo_root).resolve()
        workspace_layout = WorkspaceLayout(
            project_root=(repo_root_path / args.workspace).resolve()
        )
        try:
            consolidate_state_all(workspace_layout)
        except Exception:
            pass
        try:
            gc_workspace(workspace_layout, apply=True)
        except Exception:
            pass
        try:
            write_artifact_manifest(workspace_layout)
        except Exception:
            pass
        resume_id: str | None = None
        if not args.new_session:
            resume_id = latest_open_session(repo_root_path, args.workspace)
        if resume_id:
            handoff_path = write_session_handoff(
                Path(args.repo_root).resolve(), args.workspace, resume_id
            )
            print(
                json.dumps(
                    {
                        "status": "resumed_existing_session",
                        "session_id": resume_id,
                        "handoff_path": handoff_path,
                        "note": (
                            "Resumed open session for this workspace. Pass --new-session to "
                            "mint a fresh one. Read the handoff markdown for compact prior "
                            f"state. Then run `workspace-flow status --session {resume_id}` "
                            f"or `workspace-flow status --workspace {args.workspace} --diff` "
                            "to inspect current artifacts."
                        ),
                    },
                    indent=2,
                )
            )
            return 0
        result = WorkspaceFlow(
            args.repo_root,
            args.workspace,
            domain=args.domain,
            orchestration_mode=args.mode,
        ).start(intent=args.intent)
    elif args.cmd == "status":
        if args.diff:
            if not args.workspace:
                raise SystemExit("workspace-flow status --diff requires --workspace")
            repo_root = Path(args.repo_root).resolve()
            layout = WorkspaceLayout(project_root=(repo_root / args.workspace).resolve())
            diff = compute_workflow_diff(repo_root, args.workspace)
            diff["manifest_paths"] = write_artifact_manifest(layout)
            if args.quiet:
                ready = diff.get("ready_kpi_count", 0)
                blocked = diff.get("blocked_kpi_count", 0)
                gaps = diff.get("kpi_gaps") or []
                print(f"[ok] workflow-diff: {args.workspace} - ready {ready}, blocked {blocked}, gaps {len(gaps)}")
                for gap in gaps[:20]:
                    if isinstance(gap, dict):
                        kpi_id = gap.get("kpi_id", "?")
                        status = gap.get("status", "")
                        blockers = gap.get("blockers") or gap.get("missing_features") or []
                        suffix = f" - {'; '.join(str(b) for b in blockers)}" if blockers else ""
                        print(f"  [~] {kpi_id}: {status}{suffix}")
                    else:
                        print(f"  [~] {gap}")
                manifest = diff.get("manifest_paths", {})
                if manifest.get("markdown_path"):
                    print(f"detail: {manifest['markdown_path']}")
            else:
                print(json.dumps(diff, indent=2))
            return 0
        session_id = _resolve_session_id(
            Path(args.repo_root).resolve(), args.session, args.workspace, args.cmd
        )
        result = WorkspaceFlow.from_session(args.repo_root, session_id).status()
    elif args.cmd == "answer":
        session_id = _resolve_session_id(
            Path(args.repo_root).resolve(), args.session, args.workspace, args.cmd
        )
        result = WorkspaceFlow.from_session(args.repo_root, session_id).answer(
            answer=args.answer,
            custom_definition=args.custom_definition,
            evidence_note=args.evidence_note,
        )
    elif args.cmd == "results":
        session_id = _resolve_session_id(
            Path(args.repo_root).resolve(), args.session, args.workspace, args.cmd
        )
        result = WorkspaceFlow.from_session(args.repo_root, session_id).results(
            preview_rows=args.preview_rows,
        )
    elif args.cmd == "review":
        per_kpi = json.loads(args.kpi_notes) if args.kpi_notes else []
        session_id = _resolve_session_id(
            Path(args.repo_root).resolve(), args.session, args.workspace, args.cmd
        )
        result = WorkspaceFlow.from_session(args.repo_root, session_id).review(
            verdict=args.verdict,
            summary=args.summary,
            per_kpi=per_kpi,
            confirmed_by=getattr(args, "confirmed_by", "") or "",
        )
        # BUG-014 (enforcement): --require-human-gates blocks completion when any
        # required gate is agent-asserted (non-breaking by default).
        if getattr(args, "require_human_gates", False) and result.status == "complete":
            panel_path = (Path(args.repo_root).resolve() / result.current_panel_path
                          if result.current_panel_path else None)
            panel_payload: dict[str, Any] = {}
            if panel_path and panel_path.exists():
                try:
                    panel_payload = json.loads(panel_path.read_text(encoding="utf-8"))
                except (json.JSONDecodeError, OSError):
                    pass
            provenance = (panel_payload.get("summary") or {}).get("gate_provenance") or []
            agent_gates = [g for g in provenance if g.get("source") != "human"]
            if agent_gates:
                print("[blocked] --require-human-gates: completion blocked — the following gates are agent-asserted:")
                for g in agent_gates:
                    label = g.get("label", g.get("gate", ""))
                    print(f"  [~] {label}")
                print("")
                print("Confirm each gate with the appropriate --confirmed-by flag, for example:")
                print(f"  workspace-flow review --session {result.session_id} --verdict ok --confirmed-by <reviewer>")
                print("")
                return 2
    elif args.cmd == "artifacts":
        repo_root = Path(args.repo_root).resolve()
        layout = WorkspaceLayout(project_root=(repo_root / args.workspace).resolve())
        inv = inventory_artifacts(layout)
        if args.write_manifest:
            inv["manifest_paths"] = write_artifact_manifest(layout)
        if args.print_gitignore:
            patterns = artifact_gitignore_patterns(layout)
            inv["suggested_gitignore_patterns"] = patterns
            if not args.json:
                print(render_artifact_inventory_markdown(inv))
                print("\n## Suggested .gitignore patterns (non-source-of-truth)\n")
                for pattern in patterns:
                    print(pattern)
                return 0
        if args.json:
            print(json.dumps(inv, indent=2))
        else:
            print(render_artifact_inventory_markdown(inv))
        return 0
    elif args.cmd == "handoff":
        repo_root = Path(args.repo_root).resolve()
        session_id = args.session or latest_open_session(repo_root, args.workspace)
        if not session_id:
            print(
                json.dumps(
                    {
                        "status": "no_open_session",
                        "workspace": args.workspace,
                        "note": (
                            "No open session found for this workspace. "
                            "Nothing to hand off. Start a fresh session with "
                            "`workspace-flow start --workspace ... --new-session`."
                        ),
                    },
                    indent=2,
                )
            )
            return 0
        handoff_path = write_session_handoff(repo_root, args.workspace, session_id)
        print(
            json.dumps(
                {
                    "status": "handoff_written",
                    "session_id": session_id,
                    "handoff_path": handoff_path,
                    "note": (
                        "Compact handoff written. Read it, then start a fresh session with "
                        "`workspace-flow start --workspace <ws> --new-session`. The new session "
                        "auto-loads this handoff at startup."
                    ),
                },
                indent=2,
            )
        )
        return 0
    elif args.cmd == "context-status":
        from tools.context_status import estimate_context

        repo_root = Path(args.repo_root).resolve()
        layout = WorkspaceLayout(project_root=(repo_root / args.workspace).resolve())
        session_id = args.session or (latest_open_session(repo_root, args.workspace) or "")
        status = estimate_context(layout, session_id=session_id, budget_kb=args.budget_kb)
        print(json.dumps(status.to_dict(), indent=2))
        return 0
    elif args.cmd == "skill-excerpt":
        from tools.skill_excerpt import get_skill_excerpt

        try:
            excerpt = get_skill_excerpt(args.skill, args.section)
        except FileNotFoundError as exc:
            print(json.dumps({"error": "skill_not_found", "detail": str(exc)}, indent=2))
            return 2
        if args.json:
            print(json.dumps(excerpt.to_dict(), indent=2))
        else:
            print(excerpt.frontmatter)
            print()
            if excerpt.matched_section:
                print(f"<!-- excerpt: {excerpt.skill} :: {excerpt.matched_section} "
                      f"({excerpt.excerpt_size_bytes}/{excerpt.full_size_bytes} bytes) -->")
                print()
            print(excerpt.body)
        return 0
    elif args.cmd == "gc":
        repo_root = Path(args.repo_root).resolve()
        layout = WorkspaceLayout(project_root=(repo_root / args.workspace).resolve())
        consolidation = consolidate_state_all(layout)
        report = gc_workspace(
            layout,
            apply=args.apply,
            max_session_age_hours=args.max_session_age_hours,
            max_log_mb=args.max_log_mb,
        )
        out = report.summary()
        out["consolidation"] = consolidation
        print(json.dumps(out, indent=2))
        return 0
    else:
        raise SystemExit(2)
    if args.json:
        print(json.dumps(result.summary(), indent=2))
    else:
        _print_cli_panel(
            Path(args.repo_root).resolve(),
            result,
            kpi_filter=getattr(args, "kpi", ""),
            full=getattr(args, "full", False),
        )
    return 0


def pipeline_main(argv: list[str] | None = None) -> int:
    """Entry point for ``run-kpi-pipeline``.

    Runs the deterministic post-route-selection KPI chain end-to-end:

        onboard-workspace
        -> prepare-kpi-blocker-panel
        -> [human KPI-blocker gate]
        -> build-relationship-contracts
        -> [human relationship-approval gate]
        -> workspace-flow start --intent full_kpi_sql
        -> [human kpi-analyst review gate]
        -> results

    HUMAN GATES are enforced: the command stops and surfaces exactly what
    needs a human decision plus the exact command to resolve it, then exits
    non-zero.  It never auto-approves relationships or auto-records review
    verdicts (that would re-introduce BUG-014).

    Idempotent/resumable: re-invoking after the human resolves a gate picks
    up from where the last invocation stopped (existing open session is
    reused).

    ``--quiet`` suppresses step-by-step commentary; only gate stops and the
    final result packet are printed.

    Usage::

        run-kpi-pipeline --workspace workspaces/<project> --domain <domain>
        run-kpi-pipeline --workspace workspaces/<project> --domain <domain> --quiet
        run-kpi-pipeline --workspace workspaces/<project> --domain <domain> --new-session
    """
    import sys

    _utf8_safe_stdio()

    parser = argparse.ArgumentParser(prog="run-kpi-pipeline")
    parser.add_argument("--repo-root", default=str(PROJECT_ROOT))
    parser.add_argument("--workspace", required=True, help="Workspace path relative to repo root.")
    parser.add_argument("--domain", default="generic", help="Domain label (e.g. healthcare, retail).")
    parser.add_argument(
        "--new-session",
        action="store_true",
        help="Force a new workflow session even if an in-progress one exists.",
    )
    parser.add_argument(
        "--quiet",
        action="store_true",
        help="Print only gate stops and the final KPI result packet.",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="Print the final result as machine-readable JSON.",
    )
    # BUG-014 (enforcement): strict opt-in to block completion when any required
    # gate is agent-asserted rather than human-confirmed.
    parser.add_argument(
        "--require-human-gates",
        action="store_true",
        help=(
            "Block completion (exit non-zero) if any required gate is agent-asserted. "
            "By default the pipeline completes with a visible [~] warning. "
            "Use --confirmed-by on workspace-flow review to record human confirmation."
        ),
    )

    args = parser.parse_args(argv)
    repo_root = Path(args.repo_root).resolve()
    workspace_rel: str = args.workspace
    domain: str = args.domain
    quiet: bool = args.quiet
    emit_json: bool = args.json
    require_human_gates: bool = getattr(args, "require_human_gates", False)

    def _log(msg: str) -> None:
        if not quiet:
            print(msg)

    def _gate_stop(headline: str, details: list[str], commands: list[str]) -> int:
        """Print a human-gate blocker and return exit code 1."""
        print(f"[blocked] {headline}")
        for line in details:
            print(f"  {line}")
        if commands:
            print("")
            print("Resolve with:")
            for cmd in commands:
                print(f"  {cmd}")
        print("")
        print("Re-run `run-kpi-pipeline` after resolving the gate.")
        return 1

    # ------------------------------------------------------------------ #
    # STEP 1: onboarding (idempotent -- skips if registry already exists) #
    # ------------------------------------------------------------------ #
    _log("[ok] Step 1/5: onboard-workspace")
    try:
        onboarding = WorkspaceOnboarder(repo_root, workspace_rel).run()
        _log(f"  [ok] onboarding complete (version={onboarding.summary().get('version', '')})")
    except Exception as exc:
        print(f"[x] onboard-workspace failed: {exc}")
        return 1

    # ------------------------------------------------------------------ #
    # STEP 2: KPI blocker panel + HUMAN GATE                              #
    # ------------------------------------------------------------------ #
    _log("[ok] Step 2/5: prepare-kpi-blocker-panel")
    try:
        prepared = prepare_kpi_blocker_panel(
            repo_root,
            workspace_rel,
            domain=domain,
            onboard_if_missing=False,
        )
        panel = _read_json(repo_root / prepared.question_panel_path)
    except Exception as exc:
        print(f"[x] prepare-kpi-blocker-panel failed: {exc}")
        return 1

    if panel.get("status") == "needs_user_answer":
        feature = str(panel.get("feature") or "")
        return _gate_stop(
            headline="Open KPI blocker — human decision required before pipeline can continue.",
            details=[
                f"  Feature: {feature}" if feature else "  (see panel for details)",
                f"  Panel:   {prepared.question_panel_markdown_path}",
            ],
            commands=[
                f"uv run apply-kpi-panel-answer --workspace {workspace_rel} --domain {domain} --answer <option_id>",
            ],
        )
    _log(f"  [ok] no open KPI blockers (question_count={prepared.question_count})")

    # ------------------------------------------------------------------ #
    # STEP 3: build relationship contracts + HUMAN GATE                   #
    # ------------------------------------------------------------------ #
    _log("[ok] Step 3/5: build-relationship-contracts")
    try:
        relationships = RelationshipContractBuilder(repo_root, workspace_rel).build()
        rel_summary = relationships.summary()
    except Exception as exc:
        print(f"[x] build-relationship-contracts failed: {exc}")
        return 1

    candidate_count: int = int(rel_summary.get("candidate_relationship_count") or 0)
    executable_count: int = int(rel_summary.get("executable_relationship_count") or 0)
    _log(f"  [ok] relationships: {executable_count} executable, {candidate_count} candidate")

    # NOTE: do NOT hard-block here merely because candidate relationships remain.
    # A candidate join only matters if an in-scope KPI actually needs it. The
    # per-KPI join-proof check runs inside `start` (below) and marks the session
    # `source_to_target_blocked` only when a KPI requires an unapproved join --
    # that post-start gate (and not this count) is the correct place to stop.
    # Diagram-proven joins (proven_data_model) already satisfy the KPIs that use
    # them, so leftover candidates the KPIs do not use must not block the run.
    if candidate_count > 0:
        _log(
            f"  [~] {candidate_count} candidate relationship(s) not auto-proven from the "
            "data-model diagram; proceeding -- only a join an in-scope KPI needs will gate."
        )

    # ------------------------------------------------------------------ #
    # Completion planning: build the dependency-aware plan and record the  #
    # parallel-vs-sequential dispatch decision (instead of only emitting a #
    # plan artifact on demand). Advisory + deterministic: it always writes  #
    # the plan so the decision is auditable, and above the ready-KPI        #
    # threshold it recommends the `parallel_kpi_completion` delegation route #
    # for the orchestrator to fan out. Spawning workers stays with the      #
    # delegation layer; below threshold the pipeline runs sequentially as    #
    # before. Never breaks the run -- planning failures degrade to a note.   #
    # ------------------------------------------------------------------ #
    try:
        from core.onboarding.kpi.parallel_completion import (
            dispatch_parallel_completion,
        )

        dispatch = dispatch_parallel_completion(repo_root, workspace_rel)
        if dispatch.fan_out:
            _log(
                f"  [~] completion plan: {dispatch.ready_kpi_count} ready KPIs "
                f"> threshold {dispatch.threshold} -> parallel route "
                f"`parallel_kpi_completion` recommended ({dispatch.reason})."
            )
        else:
            _log(
                f"  [ok] completion plan: sequential "
                f"({dispatch.ready_kpi_count} ready KPIs at/under threshold "
                f"{dispatch.threshold})."
            )
    except Exception as exc:  # planner is advisory; never break the pipeline
        _log(f"  [~] completion planning skipped: {exc}")

    # ------------------------------------------------------------------ #
    # STEP 4: workspace-flow start / resume (full_kpi_sql)                #
    # ------------------------------------------------------------------ #
    _log("[ok] Step 4/5: workspace-flow start --intent full_kpi_sql")

    # Attempt to resume an existing open session first (idempotent).
    resume_id: str | None = None
    if not args.new_session:
        resume_id = latest_open_session(repo_root, workspace_rel)

    flow: WorkspaceFlow
    if resume_id:
        _log(f"  [~] resuming existing session {resume_id}")
        flow = WorkspaceFlow.from_session(repo_root, resume_id)
        state = flow._load_state()
        current_stage = str(state.get("stage") or "")
        current_status = str(state.get("status") or "")

        # BUG-021: if the session is blocked on source_to_target (join_proof_missing),
        # re-evaluate now that relationships have been approved out-of-band.
        if current_status == "blocked" and (current_stage == "source_to_target_blocked" or
                state.get("current_panel", {}).get("source") == "source_to_target"):
            _log("  [~] BUG-021: re-evaluating source-to-target blocker state after relationship approvals")
            try:
                result = flow.answer(answer="continue")
            except Exception as exc:
                print(f"[x] Failed to resume blocked session: {exc}")
                return 1
        elif current_status in ("complete", "needs_specialist_review"):
            # Let the code below handle it.
            result = flow.status()
        else:
            result = flow.status()
    else:
        try:
            result = WorkspaceFlow(
                repo_root,
                workspace_rel,
                domain=domain,
                orchestration_mode="local-safe",
            ).start(intent="full_kpi_sql")
        except Exception as exc:
            print(f"[x] workspace-flow start failed: {exc}")
            return 1

    # ------------------------------------------------------------------ #
    # Post-start: check for HUMAN GATES                                   #
    # ------------------------------------------------------------------ #
    # Reload flow reference for any needed review calls.
    flow = WorkspaceFlow.from_session(repo_root, result.session_id)

    # Open KPI blocker inside the session (can surface again after relationship build).
    if result.status == "needs_user_answer":
        panel_path = result.current_markdown_path
        panel_data = _read_json(repo_root / result.current_panel_path) if result.current_panel_path else {}
        source = panel_data.get("source", result.stage)
        if source == "kpi_blocker":
            feature = str(panel_data.get("feature") or panel_data.get("source_panel_summary", {}).get("feature") or "")
            return _gate_stop(
                headline="Open KPI blocker (emerged during source-to-target planning) — human decision required.",
                details=[
                    f"  Feature: {feature}" if feature else "  (see panel for details)",
                    f"  Panel:   {panel_path}",
                ],
                commands=[
                    f"uv run apply-kpi-panel-answer --workspace {workspace_rel} --domain {domain} --answer <option_id>",
                    f"run-kpi-pipeline --workspace {workspace_rel} --domain {domain}",
                ],
            )
        # Generic needs_user_answer gate.
        return _gate_stop(
            headline=f"Workflow stopped at stage `{result.stage}` — human answer required.",
            details=[f"  Panel: {panel_path}"],
            commands=[
                f"uv run workspace-flow answer --session {result.session_id} --answer <option_id>",
                f"run-kpi-pipeline --workspace {workspace_rel} --domain {domain}",
            ],
        )

    # Source-to-target blocked (join_proof_missing) — relationships were not all approved.
    if result.status == "blocked" and result.stage == "source_to_target_blocked":
        panel_data = _read_json(repo_root / result.current_panel_path) if result.current_panel_path else {}
        recovery = (panel_data.get("summary") or {}).get("recovery_commands") or panel_data.get("recovery_commands") or []
        cmds = [str(r.get("command", "")) for r in recovery if r.get("command")]
        return _gate_stop(
            headline="Source-to-target blocked on join_proof_missing — approve pending relationships.",
            details=[
                "  Run the recovery commands below, then re-run `run-kpi-pipeline`.",
                f"  Panel: {result.current_markdown_path}",
            ],
            commands=cmds or [
                f"uv run apply-relationship-answer --workspace {workspace_rel} --relationship-id <id> --answer approve",
            ],
        )

    # Any other blocked status.
    if result.status == "blocked":
        return _gate_stop(
            headline=f"Workflow blocked at stage `{result.stage}`.",
            details=[f"  Panel: {result.current_markdown_path}"],
            commands=[
                f"uv run workspace-flow status --workspace {workspace_rel} --diff",
            ],
        )

    # kpi-analyst review gate (enforced hard gate from BUG-014).
    if result.status == "needs_specialist_review" and result.stage == "kpi_analyst_review":
        panel_data = _read_json(repo_root / result.current_panel_path) if result.current_panel_path else {}
        session_id = result.session_id
        # Surface the result rows AT the gate (results are already executed by now):
        # the reviewer needs them to judge intent, and the operator never has to type
        # "show results". Compact (SQL linked, not inlined) so the gate output does not
        # UI-truncate; full SQL stays in the .sql files / runs snapshot.
        _emit_result_packet(repo_root, workspace_rel, compact=True)
        return _gate_stop(
            headline="KPI-analyst semantic review required before the workflow can complete.",
            details=[
                "  Invoke the `kpi-analyst` skill/agent: review each KPI's SQL + result rows against its intent.",
                "  Then post the verdict back with the command below.",
                f"  Brief: {result.current_markdown_path}",
            ],
            commands=[
                f'workspace-flow review --session {session_id} --verdict <ok|blocked> --summary "<one line>"',
            ],
        )

    # ------------------------------------------------------------------ #
    # STEP 5: emit result packet (exactly once)                           #
    # ------------------------------------------------------------------ #
    if result.status == "complete":
        _log("[ok] Step 5/5: emitting KPI result packet")
        kpi_results_md_path = repo_root / workspace_rel / "interns" / "reports" / "kpi_results" / "current.md"

        # BUG-014 (enforcement): check gate provenance before emitting completion.
        panel_data = _read_json(repo_root / result.current_panel_path) if result.current_panel_path else {}
        provenance = (panel_data.get("summary") or {}).get("gate_provenance") or []
        agent_gates = [g for g in provenance if g.get("source") != "human"]
        headline = (panel_data.get("summary") or {}).get("completion_headline") or ""

        if require_human_gates and agent_gates:
            print("[blocked] --require-human-gates: pipeline completion blocked — the following gates are agent-asserted:")
            for g in agent_gates:
                label = g.get("label", g.get("gate", ""))
                print(f"  [~] {label}")
            print("")
            print("Confirm each gate with the appropriate --confirmed-by flag, for example:")
            print(f"  workspace-flow review --session {result.session_id} --verdict ok --confirmed-by <reviewer>")
            print("")
            print("Re-run `run-kpi-pipeline` after resolving all agent-asserted gates.")
            return 2

        # Packet integrity (BUG-015/BUG-024 follow-on): never present a result
        # packet whose on-disk SQL has changed since the packet was generated
        # (e.g. a hand-edit). Block with a clear stale banner instead of showing
        # results that no longer match the SQL.
        stale_kpis = _result_packet_stale_kpis(repo_root, workspace_rel)
        if stale_kpis:
            print("[blocked] stale KPI result packet: the on-disk SQL changed after the")
            print("          packet was generated, so its results no longer match the SQL:")
            for kpi_id in stale_kpis:
                print(f"  [x] {kpi_id}: SQL hash changed since the packet was built")
            print("")
            print("Regenerate results before completion (do not hand-edit generated SQL):")
            print(f"  workspace-flow results --session {result.session_id}")
            print("Then re-run `run-kpi-pipeline`. If the SQL is wrong, fix the generator,")
            print("not the .sql file (hand-edits are overwritten on regeneration).")
            return 2

        if emit_json:
            print(json.dumps(panel_data.get("summary") or result.summary(), indent=2, default=str))
        else:
            # Print the markdown result packet (same path as BUG-013/BUG-016 fix).
            if kpi_results_md_path.exists():
                print("")
                print("## KPI Result Packet")
                print("")
                print(kpi_results_md_path.read_text(encoding="utf-8").rstrip())
                print("")
            else:
                print("[~] KPI result packet not found at expected path.")
                print(f"    Expected: {kpi_results_md_path}")
            # BUG-014 (enforcement): surface gate provenance at completion.
            if headline:
                print(headline)
            print("")
            for line in _render_gate_provenance_lines(provenance):
                print(line)
            print("## Pipeline Complete")
            print(f"  Session: {result.session_id}")
            print(f"  Workspace: {workspace_rel}")
            print(f"  Panel: {result.current_panel_path}")
        return 0

    # Unexpected status — surface it.
    print(f"[~] Pipeline reached status `{result.status}` at stage `{result.stage}` — inspect manually.")
    print(f"    Panel: {result.current_markdown_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
