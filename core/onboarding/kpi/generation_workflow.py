"""Deterministic KPI generation interview workflow.

This module keeps KPI generation as an artifact-first workflow.  The CLI writes
question panels, decisions, draft registries, proof notes, and memory files
under the active workspace before any user-facing KPI registry is written.
"""
from __future__ import annotations

import json
import re
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from core.onboarding.kpi.generation_quality import (
    advisor_notes as quality_advisor_notes,
    column_like_token_overlap as quality_column_like_token_overlap,
    looks_like_business_question as quality_looks_like_business_question,
    merge_refinement as quality_merge_refinement,
    missing_discussion_points as quality_missing_discussion_points,
    score_kpis as quality_score_kpis,
    suggest_seed_kpi as quality_suggest_seed_kpi,
    unique_sorted as quality_unique_sorted,
)
from core.onboarding.workspace.onboarding import KpiDefinition, WorkspaceOnboarder
from core.storage.workspace_layout import WorkspaceLayout
from tools.list_workspace_files import list_workspace_files


SESSION_VERSION = 1
SIMPLE_KPI_COLUMNS = [
    "business_question",
    "description",
    "cuts",
    "metric",
    "refinement_required",
]
GOVERNED_KPI_COLUMNS = [
    "kpi_name",
    "business_question",
    "decision_supported",
    "owner",
    "metric_definition",
    "grain",
    "dimensions",
    "filters",
    "numerator",
    "denominator",
    "temporal_anchor",
    "source_preference",
    "acceptance_tests",
    "refinement_required",
]


@dataclass(frozen=True)
class KPIGenerationResult:
    workspace: str
    session_path: str
    current_json_path: str
    current_markdown_path: str
    stage: str
    status: str
    next_step: str

    def summary(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class KPIGenerationFinalizeResult:
    workspace: str
    session_path: str
    output_registry_path: str
    production_proof_path: str
    production_proof_markdown_path: str
    workspace_memory_path: str
    team_memory_path: str
    status: str

    def summary(self) -> dict[str, Any]:
        return asdict(self)


class KPIGenerationWorkflow:
    def __init__(self, repo_root: str | Path, workspace: str | Path) -> None:
        self.repo_root = Path(repo_root).resolve()
        self.workspace = (self.repo_root / workspace).resolve()
        self.layout = WorkspaceLayout(project_root=self.workspace)

    def prepare(self, *, context_files: list[str] | None = None) -> KPIGenerationResult:
        self._validate_workspace()
        self.layout.ensure_runtime_dirs()
        listing = list_workspace_files(self.repo_root, _rel(self.workspace, self.repo_root)).to_dict()
        kpis, warnings = self._load_existing_kpis()
        context = self._normalize_context_files(context_files or [])
        quality = _score_kpis(kpis, listing, context)
        recommended = "option_a" if not kpis or quality["overall_score"] < 75 else "option_b"
        session = {
            "version": SESSION_VERSION,
            "workspace": _rel(self.workspace, self.repo_root),
            "created_at": _now(),
            "updated_at": _now(),
            "status": "awaiting_user_answer",
            "current_stage": "route_selection",
            "detected_files": listing,
            "source_warnings": warnings,
            "context_files": context,
            "existing_kpis": [_kpi_to_dict(kpi) for kpi in kpis],
            "quality_score": quality,
            "preferences": _default_preferences(),
            "decisions": [],
            "draft_kpis": [],
            "proof_model": {
                "draft_kpi_proof": "evidence_proof",
                "final_kpi_set_proof": "production_readiness_proof",
            },
        }
        panel = _route_panel(session, recommended)
        self._write_session(session)
        return self._write_panel(session, panel)

    def apply_answer(
        self,
        *,
        answer: str,
        context_files: list[str] | None = None,
        custom_note: str = "",
    ) -> KPIGenerationResult:
        self._validate_workspace()
        session = self._read_session()
        panel = self._read_current_panel()
        option = _resolve_option(panel, answer)
        now = _now()
        session["updated_at"] = now
        if context_files:
            session["context_files"] = _unique_sorted(
                [*session.get("context_files", []), *self._normalize_context_files(context_files)]
            )
        session.setdefault("decisions", []).append(
            {
                "stage": panel.get("stage", ""),
                "accepted_option_id": option.get("option_id", ""),
                "accepted_label": option.get("label", ""),
                "custom_note": custom_note,
                "accepted_at": now,
            }
        )

        stage = str(panel.get("stage") or session.get("current_stage") or "")
        next_panel = self._advance(session, stage, option)
        self._write_draft_if_present(session)
        self._write_session(session)
        return self._write_panel(session, next_panel)

    def finalize(
        self,
        *,
        approve_final_preview: bool,
        output_registry: str = "",
        replace_existing: bool = False,
    ) -> KPIGenerationFinalizeResult:
        self._validate_workspace()
        session = self._read_session()
        if not approve_final_preview:
            raise PermissionError("--approve-final-preview is required before writing a KPI registry")
        draft_kpis = session.get("draft_kpis") or _build_draft_kpis(session, self.repo_root)
        if not draft_kpis:
            raise ValueError("No draft KPIs are available to finalize")
        if _is_placeholder_only_draft(draft_kpis):
            raise PermissionError(
                "Refusing to finalize a placeholder-only KPI draft. Add stakeholder context, "
                "extract concrete KPI questions, or revise the draft before final approval."
            )

        output_path = (
            (self.repo_root / output_registry).resolve()
            if output_registry
            else self.workspace / "docs" / "kpi_registry.generated.json"
        )
        if not output_path.is_relative_to(self.workspace):
            raise ValueError("output registry must stay inside the active workspace")
        if output_path.exists() and not replace_existing:
            raise FileExistsError(
                f"output registry already exists: {_rel(output_path, self.repo_root)}; "
                "pass --replace-existing to overwrite it"
            )
        output_path.parent.mkdir(parents=True, exist_ok=True)
        registry = {
            "version": 1,
            "generated_by": "finalize-kpi-generation",
            "workspace": _rel(self.workspace, self.repo_root),
            "approved_at": _now(),
            "format": session.get("preferences", {}).get("kpi_format", "simple_progressive"),
            "kpis": draft_kpis,
        }
        output_path.write_text(json.dumps(registry, indent=2) + "\n", encoding="utf-8")

        production_proof = _production_proof(session, draft_kpis, _rel(output_path, self.repo_root))
        proof_path = self.layout.requirements_dir / "kpi_generation_production_proof.json"
        proof_md_path = self.layout.reports_dir / "kpi_generation" / "production_proof.md"
        proof_md_path.parent.mkdir(parents=True, exist_ok=True)
        proof_path.write_text(json.dumps(production_proof, indent=2) + "\n", encoding="utf-8")
        proof_md_path.write_text(_render_production_proof_markdown(production_proof), encoding="utf-8")

        workspace_memory = self._write_workspace_memory(session, output_path, production_proof)
        team_memory = self._write_team_memory(session)
        session["status"] = "finalized"
        session["current_stage"] = "finalized"
        session["final_registry_path"] = _rel(output_path, self.repo_root)
        session["production_proof_path"] = _rel(proof_path, self.repo_root)
        session["updated_at"] = _now()
        self._write_session(session)
        return KPIGenerationFinalizeResult(
            workspace=_rel(self.workspace, self.repo_root),
            session_path=_rel(self._session_path(), self.repo_root),
            output_registry_path=_rel(output_path, self.repo_root),
            production_proof_path=_rel(proof_path, self.repo_root),
            production_proof_markdown_path=_rel(proof_md_path, self.repo_root),
            workspace_memory_path=_rel(workspace_memory, self.repo_root),
            team_memory_path=_rel(team_memory, self.repo_root),
            status="finalized",
        )

    def _advance(
        self,
        session: dict[str, Any],
        stage: str,
        option: dict[str, Any],
    ) -> dict[str, Any]:
        option_id = str(option.get("option_id") or "")
        if stage == "route_selection":
            if option_id == "option_b":
                session["status"] = "usual_workflow_selected"
                session["current_stage"] = "usual_workflow_selected"
                return _usual_workflow_panel(session)
            session["status"] = "awaiting_user_answer"
            session["current_stage"] = "context_intake"
            return _context_panel(session)
        if stage == "context_intake":
            session["current_stage"] = "discovery_orientation"
            if option_id == "option_c":
                session["context_files"] = _unique_sorted(
                    [
                        *session.get("context_files", []),
                        *session.get("detected_files", {}).get("docs", []),
                    ]
                )
            return _orientation_panel(session)
        if stage == "discovery_orientation":
            session["preferences"]["discovery_orientation"] = option.get("value", "hybrid")
            session["current_stage"] = "format_selection"
            return _format_panel(session)
        if stage == "format_selection":
            session["preferences"]["kpi_format"] = option.get("value", "simple_progressive")
            session["draft_kpis"] = _build_draft_kpis(session, self.repo_root)
            session["draft_proofs"] = _draft_proofs(session)
            session["competitive_review"] = _competitive_review(session)
            session["current_stage"] = "final_preview"
            session["status"] = "awaiting_final_preview_approval"
            return _final_preview_panel(session)
        if stage in {"final_preview", "usual_workflow_selected"}:
            return _terminal_panel(session)
        raise ValueError(f"Unsupported KPI generation stage: {stage}")

    def _load_existing_kpis(self) -> tuple[list[KpiDefinition], list[str]]:
        onboarder = WorkspaceOnboarder(self.repo_root, _rel(self.workspace, self.repo_root))
        inputs = onboarder.discover_inputs()
        return onboarder.load_kpis(inputs.kpi_registries)

    def _normalize_context_files(self, values: list[str]) -> list[str]:
        files: list[str] = []
        for value in values:
            path = (self.repo_root / value).resolve()
            if not path.exists():
                raise FileNotFoundError(f"context file not found: {value}")
            if not path.is_file():
                raise ValueError(f"context path is not a file: {value}")
            if not path.is_relative_to(self.workspace):
                raise ValueError(f"context file must stay inside workspace: {value}")
            files.append(_rel(path, self.repo_root))
        return _unique_sorted(files)

    def _write_session(self, session: dict[str, Any]) -> Path:
        path = self._session_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(session, indent=2) + "\n", encoding="utf-8")
        return path

    def _write_draft_if_present(self, session: dict[str, Any]) -> Path | None:
        if not session.get("draft_kpis"):
            return None
        path = self.layout.requirements_dir / "kpi_registry_draft.json"
        payload = {
            "version": 1,
            "generated_by": "kpi-generation-draft",
            "workspace": _rel(self.workspace, self.repo_root),
            "format": session.get("preferences", {}).get("kpi_format", "simple_progressive"),
            "draft_status": "requires_final_preview_approval",
            "kpis": session.get("draft_kpis", []),
            "proofs": session.get("draft_proofs", []),
            "competitive_review": session.get("competitive_review", {}),
        }
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
        session["draft_registry_path"] = _rel(path, self.repo_root)
        return path

    def _read_session(self) -> dict[str, Any]:
        path = self._session_path()
        if not path.exists():
            raise FileNotFoundError(f"KPI generation session not found: {_rel(path, self.repo_root)}")
        return json.loads(path.read_text(encoding="utf-8"))

    def _read_current_panel(self) -> dict[str, Any]:
        path = self._current_json_path()
        if not path.exists():
            raise FileNotFoundError(f"KPI generation panel not found: {_rel(path, self.repo_root)}")
        return json.loads(path.read_text(encoding="utf-8"))

    def _write_panel(self, session: dict[str, Any], panel: dict[str, Any]) -> KPIGenerationResult:
        current_json = self._current_json_path()
        current_md = self._current_markdown_path()
        current_json.parent.mkdir(parents=True, exist_ok=True)
        current_json.write_text(json.dumps(panel, indent=2) + "\n", encoding="utf-8")
        current_md.write_text(_render_panel_markdown(panel), encoding="utf-8")
        return KPIGenerationResult(
            workspace=_rel(self.workspace, self.repo_root),
            session_path=_rel(self._session_path(), self.repo_root),
            current_json_path=_rel(current_json, self.repo_root),
            current_markdown_path=_rel(current_md, self.repo_root),
            stage=str(panel.get("stage") or session.get("current_stage") or ""),
            status=str(session.get("status") or ""),
            next_step=str(panel.get("next_step") or ""),
        )

    def _write_workspace_memory(
        self,
        session: dict[str, Any],
        output_path: Path,
        production_proof: dict[str, Any],
    ) -> Path:
        path = self.layout.memory_dir / "kpi_generation_workspace_memory.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        memory = {
            "version": 1,
            "workspace": _rel(self.workspace, self.repo_root),
            "updated_at": _now(),
            "preferences": session.get("preferences", {}),
            "decisions": session.get("decisions", []),
            "accepted_context_files": session.get("context_files", []),
            "final_registry_path": _rel(output_path, self.repo_root),
            "production_readiness": production_proof.get("status", ""),
        }
        path.write_text(json.dumps(memory, indent=2) + "\n", encoding="utf-8")
        return path

    def _write_team_memory(self, session: dict[str, Any]) -> Path:
        path = self.repo_root / "state" / "team_memory" / "kpi_generation_preferences.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        existing = {}
        if path.exists():
            try:
                existing = json.loads(path.read_text(encoding="utf-8"))
            except json.JSONDecodeError:
                existing = {}
        preferences = {
            **existing.get("preferences", {}),
            "route_prompt": "always_show_two_options_with_recommendation",
            "kpi_format_policy": "simple_default_progressive_governed_upgrade",
            "proof_policy": "draft_evidence_proof_then_final_production_readiness",
            "advisor_style": "competitive_review_constructive_override_allowed",
            "final_write_policy": "draft_save_then_explicit_final_approval",
            **session.get("preferences", {}),
        }
        payload = {
            "version": 1,
            "updated_at": _now(),
            "preferences": preferences,
        }
        path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
        return path

    def _session_path(self) -> Path:
        return self.layout.requirements_dir / "kpi_generation_session.json"

    def _current_json_path(self) -> Path:
        return self.layout.reports_dir / "kpi_generation" / "current.json"

    def _current_markdown_path(self) -> Path:
        return self.layout.reports_dir / "kpi_generation" / "current.md"

    def _validate_workspace(self) -> None:
        if not self.workspace.exists():
            raise FileNotFoundError(f"workspace not found: {self.workspace}")
        if self.workspace == self.repo_root or not self.workspace.is_relative_to(self.repo_root):
            raise ValueError(f"workspace must be inside repo root: {self.workspace}")


def _route_panel(session: dict[str, Any], recommended_option_id: str) -> dict[str, Any]:
    quality = session["quality_score"]
    reason = (
        "Existing KPI quality/readiness is weak or missing, so KPI generation is recommended."
        if recommended_option_id == "option_a"
        else "Existing KPI quality/readiness is acceptable, so usual workflow is recommended."
    )
    return {
        "version": SESSION_VERSION,
        "workspace": session["workspace"],
        "stage": "route_selection",
        "question": "Which path should this workspace use now?",
        "options": [
            {
                "option_id": "option_a",
                "label": "KPI generation / BA-product interview",
                "value": "kpi_generation",
                "description": (
                    "Create, revise, challenge, score, prove, and draft KPIs before implementation."
                ),
            },
            {
                "option_id": "option_b",
                "label": "Usual workflow / onboard existing KPI + data model",
                "value": "usual_workflow",
                "description": "Use existing KPI artifacts and continue onboarding, mapping, and generation.",
            },
        ],
        "recommended_option_id": recommended_option_id,
        "recommended_answer": recommended_option_id,
        "why": reason,
        "quality_score": quality,
        "next_step": "Apply the chosen path with apply-kpi-generation-answer.",
    }


def _context_panel(session: dict[str, Any]) -> dict[str, Any]:
    return {
        "version": SESSION_VERSION,
        "workspace": session["workspace"],
        "stage": "context_intake",
        "question": "Do you want to include optional stakeholder context before KPI scoring/grilling?",
        "options": [
            {
                "option_id": "option_a",
                "label": "Add explicit context files",
                "value": "add_context_files",
                "description": "Use --context-file for transcripts, PRDs, notes, screenshots, or dictionaries.",
            },
            {
                "option_id": "option_b",
                "label": "Skip extra context",
                "value": "skip_context",
                "description": "Score and draft from KPI files, data model files, and dataset evidence only.",
            },
            {
                "option_id": "option_c",
                "label": "Use existing workspace docs",
                "value": "use_workspace_docs",
                "description": "Treat current docs as optional stakeholder context.",
            },
        ],
        "recommended_option_id": "option_c" if session.get("detected_files", {}).get("docs") else "option_b",
        "recommended_answer": "Use existing workspace docs when available; otherwise skip for now.",
        "why": "Context is optional, but transcripts and product notes improve missing-discussion detection.",
        "context_files": session.get("context_files", []),
        "next_step": "Apply an option, optionally with --context-file.",
    }


def _orientation_panel(session: dict[str, Any]) -> dict[str, Any]:
    return {
        "version": SESSION_VERSION,
        "workspace": session["workspace"],
        "stage": "discovery_orientation",
        "question": "Should KPI generation be data-first, goal-first, or hybrid for this workspace?",
        "options": [
            {
                "option_id": "option_a",
                "label": "Data-first",
                "value": "data_first",
                "description": "Start from selected datasets and recommend useful KPIs from available data.",
            },
            {
                "option_id": "option_b",
                "label": "Goal-first",
                "value": "goal_first",
                "description": "Start from product/business goals and map them to datasets later.",
            },
            {
                "option_id": "option_c",
                "label": "Hybrid",
                "value": "hybrid",
                "description": "Use business goals and available data together.",
            },
        ],
        "recommended_option_id": "option_c",
        "recommended_answer": "Hybrid",
        "why": "Hybrid avoids forcing product leads into data-first thinking or engineers into goal-only planning.",
        "next_step": "Apply an option to choose the KPI registry format policy.",
    }


def _format_panel(session: dict[str, Any]) -> dict[str, Any]:
    return {
        "version": SESSION_VERSION,
        "workspace": session["workspace"],
        "stage": "format_selection",
        "question": "Which KPI registry format policy should the draft use?",
        "options": [
            {
                "option_id": "option_a",
                "label": "Keep simple default format",
                "value": "simple",
                "columns": SIMPLE_KPI_COLUMNS,
            },
            {
                "option_id": "option_b",
                "label": "Use governed production-grade format",
                "value": "governed",
                "columns": GOVERNED_KPI_COLUMNS,
            },
            {
                "option_id": "option_c",
                "label": "Simple first, upgrade when weak",
                "value": "simple_progressive",
                "columns": SIMPLE_KPI_COLUMNS,
                "upgrade_columns": GOVERNED_KPI_COLUMNS,
            },
        ],
        "recommended_option_id": "option_c",
        "recommended_answer": "Simple first, upgrade when weak",
        "why": "This keeps adoption easy while making production gaps visible when quality is weak.",
        "next_step": "Apply an option to generate the draft KPI registry preview and per-KPI evidence proof.",
    }


def _final_preview_panel(session: dict[str, Any]) -> dict[str, Any]:
    draft_kpis = session.get("draft_kpis", [])
    placeholder_only = _is_placeholder_only_draft(draft_kpis)
    options = [
        {
            "option_id": "option_b",
            "label": "Revise before saving",
            "value": "revise",
            "description": "Continue grilling or improve the draft before final approval.",
        },
    ]
    if not placeholder_only:
        options.insert(
            0,
            {
                "option_id": "option_a",
                "label": "Finalize after review",
                "value": "finalize",
                "description": "Run finalize-kpi-generation with --approve-final-preview.",
            },
        )
    return {
        "version": SESSION_VERSION,
        "workspace": session["workspace"],
        "stage": "final_preview",
        "status": "blocked_placeholder_seed" if placeholder_only else "awaiting_final_preview_approval",
        "question": (
            "The draft contains only a placeholder seed KPI. Revise with concrete business "
            "questions or context before finalizing."
            if placeholder_only
            else "Review the draft KPI registry and proof notes before finalizing."
        ),
        "options": options,
        "recommended_option_id": "option_b"
        if placeholder_only or session.get("competitive_review", {}).get("missing_discussion_points")
        else "option_a",
        "recommended_answer": (
            "Revise before saving; placeholder-only KPI drafts cannot be finalized."
            if placeholder_only
            else "Revise if high-risk discussion points remain; otherwise finalize."
        ),
        "why": "Drafts are saved safely, but the user-facing KPI registry requires explicit final approval.",
        "draft_kpis": draft_kpis,
        "draft_proofs": session.get("draft_proofs", []),
        "competitive_review": session.get("competitive_review", {}),
        "next_step": (
            "Add context or revise KPI requirements, then regenerate the draft preview."
            if placeholder_only
            else "Run finalize-kpi-generation --approve-final-preview after reviewing the draft."
        ),
    }


def _usual_workflow_panel(session: dict[str, Any]) -> dict[str, Any]:
    return {
        "version": SESSION_VERSION,
        "workspace": session["workspace"],
        "stage": "usual_workflow_selected",
        "question": "Usual workflow selected.",
        "options": [],
        "recommended_option_id": "",
        "recommended_answer": (
            f"uv run onboard-workspace --workspace {session['workspace']} then "
            f"uv run prepare-kpi-blocker-panel --workspace {session['workspace']} --domain <domain>"
        ),
        "why": "The user chose to implement existing KPI artifacts instead of revising KPI requirements now.",
        "next_step": "Continue with onboarding and KPI feature resolution.",
    }


def _terminal_panel(session: dict[str, Any]) -> dict[str, Any]:
    return {
        "version": SESSION_VERSION,
        "workspace": session["workspace"],
        "stage": session.get("current_stage", "terminal"),
        "question": "No further KPI generation question is active.",
        "options": [],
        "recommended_option_id": "",
        "recommended_answer": "",
        "why": "The workflow is waiting for a deterministic command or final approval.",
        "next_step": "Run the command named in the previous panel.",
    }


def _score_kpis(
    kpis: list[KpiDefinition],
    listing: dict[str, Any],
    context_files: list[str],
) -> dict[str, Any]:
    return quality_score_kpis(kpis, listing, context_files)


def _build_draft_kpis(session: dict[str, Any], repo_root: Path) -> list[dict[str, Any]]:
    existing = session.get("existing_kpis") or []
    quality_by_name = {
        item.get("business_question", ""): item
        for item in session.get("quality_score", {}).get("kpis", [])
    }
    
    if not existing:
        extracted = []
        for rel_path in session.get("context_files", []):
            path = repo_root / rel_path
            if path.exists() and path.is_file() and path.suffix.lower() == ".sql":
                from core.onboarding.kpi.text_parser import extract_kpis_from_sql
                sql_kpis = extract_kpis_from_sql(path.read_text(encoding="utf-8"), str(rel_path))
                for sql_kpi in sql_kpis:
                    extracted.append({
                        "name": sql_kpi["name"],
                        "description": sql_kpi["description"],
                        "cuts": sql_kpi["cuts"],
                        "metric": sql_kpi["metric"],
                        "refinement_required": "Confirm business question, owner, metric, grain, and tests.",
                        "source": sql_kpi["source"],
                    })
        if extracted:
            existing = extracted
        else:
            existing = [_suggest_seed_kpi(session)]

    drafts = []
    for idx, kpi in enumerate(existing, start=1):
        quality = quality_by_name.get(kpi.get("name", ""), {})
        drafts.append(
            {
                "kpi_id": f"kpi_{idx:03d}",
                "business_question": kpi.get("name", ""),
                "description": kpi.get("description", ""),
                "cuts": kpi.get("cuts", ""),
                "metric": kpi.get("metric", ""),
                "refinement_required": _merge_refinement(
                    kpi.get("refinement_required", ""),
                    quality.get("missing_discussion_points", []),
                ),
                "source": kpi.get("source", ""),
                "status": "draft_needs_review"
                if quality.get("missing_discussion_points")
                else "draft_ready_for_evidence_mapping",
                "advisor_notes": _advisor_notes(quality),
            }
        )
    draft_path = Path(session["workspace"]) / "interns/generated/requirements/kpi_registry_draft.json"
    session["draft_registry_path"] = draft_path.as_posix()
    return drafts


def _is_placeholder_only_draft(draft_kpis: list[dict[str, Any]]) -> bool:
    if len(draft_kpis) != 1:
        return False
    kpi = draft_kpis[0]
    question = str(kpi.get("business_question") or kpi.get("name") or "").strip().lower()
    metric = str(kpi.get("metric") or "").strip().lower()
    source = str(kpi.get("source") or "").strip().lower()
    return (
        question == "what operational kpi should this dataset support?"
        and metric in {"confirm metric", ""}
        and source == "generated_from_workspace_data"
    )


def _draft_proofs(session: dict[str, Any]) -> list[dict[str, Any]]:
    files = session.get("detected_files", {}).get("files", [])
    datasets = [file for file in files if "/datasets/" in file]
    proofs = []
    for kpi in session.get("draft_kpis", []):
        text = " ".join([kpi.get("business_question", ""), kpi.get("metric", ""), kpi.get("cuts", "")])
        candidate_sources = [file for file in datasets if _column_like_token_overlap(text.lower(), [file])]
        if not candidate_sources:
            candidate_sources = datasets[:3]
        proofs.append(
            {
                "kpi_id": kpi.get("kpi_id", ""),
                "proof_level": "evidence_proof",
                "mapped_dataset_candidates": candidate_sources,
                "source_to_target_required": True,
                "query_proof_required": True,
                "basic_validation_tests": [
                    "required_source_columns_present",
                    "query_compiles_or_runs_locally",
                    "result_row_count_non_negative",
                    "null_and_duplicate_review_for_grouping_keys",
                ],
                "remaining_risk": [
                    "Business owner approval is not complete until final production proof.",
                    "Join, grain, and temporal anchor must be proven before executable generation.",
                ],
            }
        )
    return proofs


def _competitive_review(session: dict[str, Any]) -> dict[str, Any]:
    quality = session.get("quality_score", {})
    missing = quality.get("missing_discussion_points", [])
    files = session.get("detected_files", {}).get("files", [])
    dataset_text = " ".join(files).lower()
    suggestions = []
    if "paid" in dataset_text or "amount" in dataset_text:
        suggestions.append("Add paid/allowed/denied amount KPIs with payer, LOB, and time cuts.")
    if "patient" in dataset_text or "member" in dataset_text:
        suggestions.append("Add lives/member coverage KPIs with denominator and eligibility rules.")
    if "encounter" in dataset_text or "claim" in dataset_text:
        suggestions.append("Add volume and leakage KPIs by encounter/claim status and service period.")
    if not suggestions:
        suggestions.append("Add at least one decision-linked KPI with owner, grain, and acceptance tests.")
    return {
        "mode": "competitive_review",
        "missing_discussion_points": missing,
        "weaknesses": [
            "KPI lacks production ownership or decision linkage."
            for point in missing
            if point in {"owner", "decision_supported"}
        ],
        "ranked_suggestions": suggestions,
        "override_policy": "User may accept weaker KPIs with a saved rationale.",
    }


def _production_proof(
    session: dict[str, Any],
    draft_kpis: list[dict[str, Any]],
    output_registry_path: str,
) -> dict[str, Any]:
    missing = session.get("competitive_review", {}).get("missing_discussion_points", [])
    checks = [
        {"check": "data_quality_tests_defined", "status": "needs_review"},
        {"check": "edge_case_tests_defined", "status": "needs_review"},
        {"check": "reconciliation_checks_defined", "status": "needs_review"},
        {"check": "performance_baseline_defined", "status": "needs_review"},
        {"check": "owner_defined", "status": "passed" if "owner" not in missing else "needs_review"},
        {"check": "sla_defined", "status": "needs_review"},
        {"check": "governance_approval_recorded", "status": "needs_review"},
    ]
    status = "production_ready_with_review_items" if any(
        item["status"] == "needs_review" for item in checks
    ) else "production_ready"
    return {
        "version": 1,
        "workspace": session.get("workspace", ""),
        "generated_at": _now(),
        "output_registry_path": output_registry_path,
        "kpi_count": len(draft_kpis),
        "proof_level": "production_readiness_proof",
        "status": status,
        "checks": checks,
        "required_before_publish": [
            "Resolve every needs_review production proof check.",
            "Run source-to-target planning for the final KPI set.",
            "Generate and execute target-engine queries.",
            "Record product/business approval.",
        ],
    }


def _render_panel_markdown(panel: dict[str, Any]) -> str:
    lines = [
        f"# KPI Generation Panel: {panel.get('stage', '')}",
        "",
        f"- Workspace: `{panel.get('workspace', '')}`",
        f"- Recommended option: `{panel.get('recommended_option_id', '')}`",
        "",
        "## Question",
        "",
        str(panel.get("question", "")),
        "",
        "## Options",
        "",
    ]
    options = panel.get("options") or []
    if not options:
        lines.append("- (none)")
    for option in options:
        lines.extend(
            [
                f"### {option.get('option_id', '')}: {option.get('label', '')}",
                "",
                str(option.get("description", "")),
                "",
            ]
        )
    lines.extend(
        [
            "## Recommended Answer",
            "",
            str(panel.get("recommended_answer", "")),
            "",
            "## Why",
            "",
            str(panel.get("why", "")),
            "",
            "## Next Step",
            "",
            str(panel.get("next_step", "")),
            "",
        ]
    )
    if panel.get("quality_score"):
        lines.extend(["## KPI Quality Score", "", "```json"])
        lines.append(json.dumps(panel["quality_score"], indent=2))
        lines.extend(["```", ""])
    if panel.get("draft_kpis"):
        lines.extend(["## Draft KPI Registry Preview", "", "```json"])
        lines.append(json.dumps(panel["draft_kpis"], indent=2))
        lines.extend(["```", ""])
    if panel.get("competitive_review"):
        lines.extend(["## Competitive Review", "", "```json"])
        lines.append(json.dumps(panel["competitive_review"], indent=2))
        lines.extend(["```", ""])
    return "\n".join(lines)


def _render_production_proof_markdown(proof: dict[str, Any]) -> str:
    lines = [
        "# KPI Generation Production Proof",
        "",
        f"- Workspace: `{proof.get('workspace', '')}`",
        f"- Registry: `{proof.get('output_registry_path', '')}`",
        f"- Status: `{proof.get('status', '')}`",
        "",
        "## Checks",
        "",
    ]
    for check in proof.get("checks", []):
        lines.append(f"- `{check.get('check')}`: {check.get('status')}")
    lines.extend(["", "## Required Before Publish", ""])
    for item in proof.get("required_before_publish", []):
        lines.append(f"- {item}")
    lines.append("")
    return "\n".join(lines)


def _kpi_to_dict(kpi: KpiDefinition) -> dict[str, Any]:
    return {
        "name": kpi.name,
        "description": kpi.description,
        "cuts": kpi.cuts,
        "metric": kpi.metric,
        "refinement_required": kpi.refinement_required,
        "source": kpi.source,
        "status": kpi.status,
    }


def _default_preferences() -> dict[str, str]:
    return {
        "route_prompt": "always_show_two_options_with_recommendation",
        "discovery_orientation": "hybrid",
        "kpi_format": "simple_progressive",
        "advisor_style": "competitive_review",
        "draft_proof": "evidence_proof",
        "final_proof": "production_readiness_proof",
    }


def _missing_discussion_points(kpi: KpiDefinition, has_context: bool) -> list[str]:
    return quality_missing_discussion_points(kpi, has_context)


def _advisor_notes(quality: dict[str, Any]) -> list[str]:
    return quality_advisor_notes(quality)


def _merge_refinement(existing: str, missing: list[str]) -> str:
    return quality_merge_refinement(existing, missing)


def _suggest_seed_kpi(session: dict[str, Any]) -> dict[str, Any]:
    return quality_suggest_seed_kpi(session)


def _looks_like_business_question(value: str) -> bool:
    return quality_looks_like_business_question(value)


def _column_like_token_overlap(text: str, data_files: list[str]) -> bool:
    return quality_column_like_token_overlap(text, data_files)


def _resolve_option(panel: dict[str, Any], answer: str) -> dict[str, Any]:
    options = panel.get("options") or []
    if not options:
        raise ValueError("current KPI generation panel has no options")
    raw = str(answer or "").strip()
    normalized = _norm(raw)
    if normalized in {"recommendation", "recommended", "yourrecommendation"}:
        recommended = panel.get("recommended_option_id")
        return _one(options, lambda item: item.get("option_id") == recommended, raw)
    match = re.match(r"^option\s+([a-z])$", raw, re.I)
    if match:
        normalized = _norm(f"option_{match.group(1).lower()}")
    if len(raw) == 1 and raw.lower().isalpha():
        normalized = _norm(f"option_{raw.lower()}")
    return _one(
        options,
        lambda item: normalized
        in {
            _norm(item.get("option_id", "")),
            _norm(item.get("label", "")),
            _norm(item.get("value", "")),
        },
        raw,
    )


def _one(options: list[dict[str, Any]], predicate: Any, answer: str) -> dict[str, Any]:
    matches = [option for option in options if predicate(option)]
    if len(matches) == 1:
        return matches[0]
    if not matches:
        valid = ", ".join(str(option.get("option_id")) for option in options)
        raise ValueError(f"answer `{answer}` did not match a KPI generation option: {valid}")
    raise ValueError(f"answer `{answer}` is ambiguous across {len(matches)} options")


def _unique_sorted(values: Any) -> list[str]:
    return quality_unique_sorted(values)


def _norm(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", "", str(value).lower())


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _rel(path: Path, root: Path) -> str:
    try:
        return path.resolve().relative_to(root.resolve()).as_posix()
    except ValueError:
        return path.as_posix()


def prepare_main(argv: list[str] | None = None) -> int:
    from core.onboarding.kpi.generation_cli import prepare_main as cli_prepare_main

    return cli_prepare_main(argv)


def apply_main(argv: list[str] | None = None) -> int:
    from core.onboarding.kpi.generation_cli import apply_main as cli_apply_main

    return cli_apply_main(argv)


def finalize_main(argv: list[str] | None = None) -> int:
    from core.onboarding.kpi.generation_cli import finalize_main as cli_finalize_main

    return cli_finalize_main(argv)
