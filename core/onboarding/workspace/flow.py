"""User-facing workspace workflow sessions.

This module is the quiet front door for agent UIs.  It runs the existing
governed tools in-process, persists a small session record, and returns compact
panels instead of streaming every lower-level command to the main chat.
"""
from __future__ import annotations

import argparse
import json
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from core.paths import PROJECT_ROOT
from core.onboarding.bronze_silver_standards import BronzeSilverStandardsBuilder
from core.onboarding.data_quality import DataQualityHarness, DuplicateDecisionRecorder, DuplicateReviewPanel
from core.onboarding.harness.trajectory_recorder import record_trajectory_event_safe
from core.onboarding.kpi.blocker_workflow import apply_kpi_panel_answer, prepare_kpi_blocker_panel
from core.onboarding.kpi.execution_harness import KPIExecutionHarness
from core.onboarding.kpi.generation_workflow import KPIGenerationWorkflow
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
from tools.list_workspace_files import list_workspace_files


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
        domain: str = "healthcare",
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

    def status(self) -> WorkspaceFlowResult:
        state = self._load_state()
        return self._result_from_state(state)

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
        plan = SourceToTargetPlanner(self.repo_root, self.workspace_rel, target_engine="sql").build()
        self._record_step(state, "plan_source_to_target", "ok", plan.summary())
        if plan.blocked_kpi_count:
            return self._save_panel(
                state,
                panel={
                    "stage": "source_to_target_blocked",
                    "status": "blocked",
                    "instruction": "Source-to-target planning found blockers. Review the plan report.",
                    "question": "Resolve the blockers in the source-to-target plan before generating SQL.",
                    "options": [],
                    "recommended_option_id": "",
                    "artifact_paths": [plan.json_path, plan.markdown_path],
                    "summary": plan.summary(),
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
        return self._save_panel(
            state,
            panel={
                "stage": "complete",
                "status": "complete",
                "instruction": "KPI SQL generation and previews are complete.",
                "question": "",
                "options": [],
                "recommended_option_id": "",
                "artifact_paths": [
                    self.workspace_rel + "/interns/generated/solutions",
                    preview["json_path"],
                    preview["markdown_path"],
                ],
                "summary": {
                    "generated_kpi_count": len(generated),
                    "orchestration": orchestration_context,
                    "validation": validation.summary(),
                    "results": preview,
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
        md_path.write_text(_render_results_markdown(payload), encoding="utf-8")
        return {
            "json_path": _rel(json_path, self.repo_root),
            "markdown_path": _rel(md_path, self.repo_root),
            "kpi_count": len(entries),
            "error_count": sum(1 for item in entries if item.get("status") != "ok"),
        }

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
    filters = []
    for part in str(cuts).split(","):
        cleaned = part.strip()
        if any(token in cleaned for token in ("=", ">", "<")):
            filters.append(cleaned)
    lowered = str(question).lower()
    if "medicare" in lowered and not any("medicare" in item.lower() for item in filters):
        filters.append("LOB = Medicare")
    if "commercial" in lowered and not any("commercial" in item.lower() for item in filters):
        filters.append("LOB = Commercial")
    if "above 50" in lowered and not any("age" in item.lower() and "50" in item for item in filters):
        filters.append("Age > 50")
    if "top 10" in lowered and not any("top 10" in item.lower() for item in filters):
        filters.append("Top 10")
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


def _render_results_markdown(payload: dict[str, Any]) -> str:
    lines = [
        "# KPI Query Results",
        "",
        f"- Workspace: `{payload.get('workspace', '')}`",
        f"- KPI count: {len(payload.get('kpis', []))}",
        "",
    ]
    for entry in payload.get("kpis", []):
        lines.extend(
            [
                f"## {entry.get('kpi_id')}",
                "",
                f"- SQL: `{entry.get('sql_path', '')}`",
                f"- Status: `{entry.get('status', '')}`",
                "",
            ]
        )
        if entry.get("error"):
            lines.extend(["```text", str(entry["error"]), "```", ""])
        else:
            lines.extend([str(entry.get("preview_markdown", "")), ""])
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


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _rel(path: Path, root: Path) -> str:
    try:
        return path.resolve().relative_to(root.resolve()).as_posix()
    except ValueError:
        return path.as_posix()


def _print_cli_panel(repo_root: Path, result: WorkspaceFlowResult) -> None:
    markdown_path = repo_root / result.current_markdown_path
    if markdown_path.exists():
        print(markdown_path.read_text(encoding="utf-8").rstrip())
    else:
        print(f"# Workspace Flow: {result.stage}")
        print("")
        print(f"- Workspace: `{result.workspace}`")
        print(f"- Status: `{result.status}`")
    print("")
    print("## Next Step")
    print("")
    print(result.next_step)
    print("")
    print("## Panel Artifacts")
    print("")
    print(f"- JSON: `{result.current_panel_path}`")
    print(f"- Markdown: `{result.current_markdown_path}`")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="workspace-flow")
    parser.add_argument("--repo-root", default=str(PROJECT_ROOT))
    parser.add_argument("--json", action="store_true", help="Print the machine-readable result summary only.")
    sub = parser.add_subparsers(dest="cmd", required=True)

    start = sub.add_parser("start")
    start.add_argument("--workspace", required=True)
    start.add_argument("--domain", default="healthcare")
    start.add_argument("--intent", choices=sorted(INTENTS), default="kpi_generation")
    start.add_argument("--mode", choices=sorted(ORCHESTRATION_MODES), default="local-safe")

    status = sub.add_parser("status")
    status.add_argument("--session", required=True)

    answer = sub.add_parser("answer")
    answer.add_argument("--session", required=True)
    answer.add_argument("--answer", required=True)
    answer.add_argument("--custom-definition", default="")
    answer.add_argument("--evidence-note", default="")

    results = sub.add_parser("results")
    results.add_argument("--session", required=True)
    results.add_argument("--preview-rows", type=int, default=20)

    args = parser.parse_args(argv)
    if args.cmd == "start":
        result = WorkspaceFlow(
            args.repo_root,
            args.workspace,
            domain=args.domain,
            orchestration_mode=args.mode,
        ).start(intent=args.intent)
    elif args.cmd == "status":
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
    else:
        raise SystemExit(2)
    if args.json:
        print(json.dumps(result.summary(), indent=2))
    else:
        _print_cli_panel(Path(args.repo_root).resolve(), result)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
