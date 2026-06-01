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
    verdict_from_kpi_completion,
    verdict_from_relationship_summary,
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
from core.storage.workspace_layout import WorkspaceLayout
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
        # verdicts from human-confirmed ones.
        source = "human" if confirmed_by else "agent"
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

    def results(self, *, preview_rows: int = 20) -> WorkspaceFlowResult:
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
        for idx in range(1, plan.kpi_count + 1):
            kpi_id = f"kpi_{idx:03d}"
            generated.append(DuckDBKPISQLGenerator(self.repo_root, self.workspace_rel).generate(kpi_id).summary())
        self._record_step(state, "generate_kpi_sql", "ok", {"generated": generated})
        harness = KPIExecutionHarness(self.repo_root, self.workspace_rel).run()
        self._record_step(
            state,
            "run_kpi_execution_harness",
            "ok" if harness.ok else "failed",
            harness.summary(),
            validation="run-kpi-execution-harness",
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
        preview = self._write_result_preview(preview_rows=20)
        self._record_step(state, "preview_kpi_results", "ok", preview)
        kpi_entries = preview.get("kpis") or []
        wiki_paths: list[str] = []
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
            self._record_step(
                state,
                "refresh_workspace_dashboard",
                "ok",
                dash_summary,
            )
            self._delegate_and_record(
                state,
                agent="dashboard-engineer",
                stage="dashboard_refresh",
                reason="every workflow completion must refresh dashboard specs (preserving user_overrides)",
                verdict_fn=lambda: verdict_from_dashboard_summary(dash_summary),
            )
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
                    "suggested_skills": [
                        {"name": "kpi-analyst", "why": "Validate generated SQL and result samples against KPI intent."},
                        {"name": "self-grill", "why": "EXECUTED — see summary.self_grill and the delegation brief for kpi_output_verification."},
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
        conn = duckdb.connect(":memory:")
        old_cwd = Path.cwd()
        try:
            import os

            os.chdir(self.repo_root)
            for sql_file in sql_files:
                kpi_id = sql_file.stem
                sql_text = sql_file.read_text(encoding="utf-8")
                entry: dict[str, Any] = {
                    "kpi_id": kpi_id,
                    "sql_path": _rel(sql_file, self.repo_root),
                    "sql_text": sql_text,
                    "definition": kpi_definitions.get(kpi_id, {}),
                    "status": "ok",
                }
                try:
                    conn.execute(sql_text)
                    view = _result_view(conn, kpi_id)
                    if view:
                        cursor = conn.execute(f'SELECT * FROM "{view}" LIMIT {int(preview_rows)}')
                        entry["preview_markdown"] = render_query_result_table(cursor)
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
            import os

            os.chdir(old_cwd)
            conn.close()

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
        results_markdown = _render_results_markdown(payload)
        md_path.write_text(results_markdown, encoding="utf-8")
        self._write_runs_snapshot(payload, results_markdown)
        return {
            "json_path": _rel(json_path, self.repo_root),
            "markdown_path": _rel(md_path, self.repo_root),
            "kpi_count": len(entries),
            "error_count": sum(1 for item in entries if item.get("status") != "ok"),
            "kpis": entries,
        }

    def _write_runs_snapshot(self, payload: dict[str, Any], results_markdown: str) -> None:
        """Mirror the just-executed results into a dated runs/<date>/ snapshot.

        The snapshot is written here — by the executor that re-runs the on-disk
        SQL — so the dated record always reflects what was actually executed,
        not what the generator first emitted. Per-KPI files plus a combined
        results.md, both overwritten on each run (no append graveyard).
        """
        run_dir = self.layout.runs_dir / date.today().isoformat()
        run_dir.mkdir(parents=True, exist_ok=True)
        for entry in payload.get("kpis", []):
            kpi_id = entry.get("kpi_id")
            if not kpi_id:
                continue
            section = "\n".join(render_kpi_block(entry, heading_level=2)).rstrip() + "\n"
            (run_dir / f"{kpi_id}.md").write_text(section, encoding="utf-8")
        (run_dir / "results.md").write_text(results_markdown, encoding="utf-8")

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
            {"name": "clarify-ambiguity", "why": "Flag missing context before applying an answer."},
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
    completed_kpis = (panel.get("summary") or {}).get("completed_kpis") or []
    if completed_kpis:
        lines.extend(["## Completed KPIs", ""])
        for entry in completed_kpis:
            lines.extend(render_kpi_block(entry, heading_level=3))
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


def _render_results_markdown(payload: dict[str, Any]) -> str:
    lines = [
        "# KPI Query Results",
        "",
        f"- Workspace: `{payload.get('workspace', '')}`",
        f"- KPI count: {len(payload.get('kpis', []))}",
        "",
    ]
    for entry in payload.get("kpis", []):
        lines.extend(render_kpi_block(entry, heading_level=2))
    return "\n".join(lines).rstrip() + "\n"


def _result_view(conn: Any, kpi_id: str) -> str:
    rows = conn.execute(
        "SELECT table_name FROM information_schema.views WHERE lower(table_name) = lower(?)",
        [f"{kpi_id}_results"],
    ).fetchall()
    return str(rows[0][0]) if rows else ""


def _find_session(repo_root: Path, session_id: str) -> dict[str, Any]:
    matches = list(repo_root.glob(f"workspaces/*/interns/state/workflow_sessions/{session_id}/session.json"))
    if not matches:
        raise FileNotFoundError(f"workspace-flow session not found: {session_id}")
    return json.loads(matches[0].read_text(encoding="utf-8"))


def _read_json(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return {}
    return data if isinstance(data, dict) else {}


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

    return {
        "workspace": workspace_rel,
        "plan_present": bool(plan),
        "kpi_count": len(plan.get("kpis") or []),
        "executable_relationship_ids": executable_rel_ids,
        "pending_relationship_ids": pending_rel_ids,
        "kpi_gaps": kpi_gaps,
        "ready_kpi_count": sum(1 for g in kpi_gaps if g.get("status") == "ready_for_generation"),
        "blocked_kpi_count": sum(1 for g in kpi_gaps if g.get("status") == "blocked"),
    }


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _rel(path: Path, root: Path) -> str:
    try:
        return path.resolve().relative_to(root.resolve()).as_posix()
    except ValueError:
        return path.as_posix()


def _print_cli_panel(repo_root: Path, result: WorkspaceFlowResult) -> None:
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
    # BUG-013 / BUG-016: on completion OR explicit `results` call, emit the full
    # kpi_results packet so any driving agent sees KPI + SQL + result rows without
    # needing a separate call.  For `results` stage the packet IS the point of the
    # command; for `complete` it surfaces automatically so agents driving via the
    # CLI see rows immediately after the workflow finishes.
    if result.stage in ("complete", "results") and result.workspace:
        kpi_results_paths = (panel_payload.get("artifact_paths") or [])
        kpi_results_md: str | None = None
        # Look for the kpi_results current.md among the artifact paths first.
        for ap in kpi_results_paths:
            if ap.endswith("kpi_results/current.md"):
                candidate = repo_root / ap
                if candidate.exists():
                    kpi_results_md = candidate.read_text(encoding="utf-8")
                    break
        if kpi_results_md is None:
            # Fallback: derive path from workspace.
            fallback = repo_root / result.workspace / "interns" / "reports" / "kpi_results" / "current.md"
            if fallback.exists():
                kpi_results_md = fallback.read_text(encoding="utf-8")
        if kpi_results_md:
            print("")
            print("## KPI Result Packet")
            print("")
            print(kpi_results_md.rstrip())
            print("")
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
    # BUG-019: --quiet must be accepted both at the top level
    # (workspace-flow --quiet status --diff) AND after the subcommand
    # (workspace-flow status --diff --quiet).
    # Strategy: parse the raw argv for a pre-subcommand --quiet first, then
    # add --quiet to every subparser.  After parse_args we OR the two values
    # so either placement is honoured without either overriding the other.
    _toplevel_quiet: bool = bool(argv and "--quiet" in _args_before_subcommand(argv))

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
    status.add_argument("--session", default="")
    status.add_argument("--workspace", default="")
    status.add_argument(
        "--diff",
        action="store_true",
        help="Read existing artifacts and report per-KPI gaps + recovery commands without re-running the pipeline.",
    )

    answer = sub.add_parser("answer")
    _add_quiet(answer)
    answer.add_argument("--session", required=True)
    answer.add_argument("--answer", required=True)
    answer.add_argument("--custom-definition", default="")
    answer.add_argument("--evidence-note", default="")

    results = sub.add_parser("results")
    _add_quiet(results)
    results.add_argument("--session", required=True)
    results.add_argument("--preview-rows", type=int, default=20)

    review_p = sub.add_parser("review")
    _add_quiet(review_p)
    review_p.add_argument("--session", required=True)
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

    args = parser.parse_args(argv)
    # BUG-019: merge top-level --quiet (pre-subcommand) with subparser --quiet
    # (post-subcommand) so both orderings are honoured.
    args.quiet = bool(args.quiet) or _toplevel_quiet
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
        if not args.session:
            raise SystemExit("workspace-flow status requires --session (or use --diff with --workspace)")
        result = WorkspaceFlow.from_session(args.repo_root, args.session).status()
    elif args.cmd == "answer":
        result = WorkspaceFlow.from_session(args.repo_root, args.session).answer(
            answer=args.answer,
            custom_definition=args.custom_definition,
            evidence_note=args.evidence_note,
        )
    elif args.cmd == "results":
        result = WorkspaceFlow.from_session(args.repo_root, args.session).results(
            preview_rows=args.preview_rows,
        )
    elif args.cmd == "review":
        per_kpi = json.loads(args.kpi_notes) if args.kpi_notes else []
        result = WorkspaceFlow.from_session(args.repo_root, args.session).review(
            verdict=args.verdict,
            summary=args.summary,
            per_kpi=per_kpi,
            confirmed_by=getattr(args, "confirmed_by", "") or "",
        )
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
        _print_cli_panel(Path(args.repo_root).resolve(), result)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
