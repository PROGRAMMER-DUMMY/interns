"""Governed data model creation and parsing workflow.

The workflow mirrors KPI generation: prepare a route panel, apply a choice to
create a draft model pack, and finalize only after explicit preview approval.
"""
from __future__ import annotations

import json
import re
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from core.storage.workspace_layout import WorkspaceLayout
from tools.list_workspace_files import list_workspace_files


SESSION_VERSION = 1
PATTERN_LIBRARY_PATH = Path(__file__).with_name("patterns.json")
TEXT_MODEL_SUFFIXES = {".md", ".txt", ".json", ".yaml", ".yml"}
IMAGE_MODEL_SUFFIXES = {".png", ".jpg", ".jpeg", ".svg"}
MODEL_NAME_TOKENS = ("model", "schema", "diagram", "erd", "dictionary", "contract")


@dataclass(frozen=True)
class DataModelGenerationResult:
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
class DataModelBlockerPanelResult:
    workspace: str
    session_path: str
    current_json_path: str
    current_markdown_path: str
    blocker_count: int
    current_blocker_id: str
    status: str

    def summary(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class DataModelFinalizeResult:
    workspace: str
    session_path: str
    contract_path: str
    data_model_markdown_path: str
    erd_markdown_path: str
    relationships_markdown_path: str
    status: str

    def summary(self) -> dict[str, Any]:
        return asdict(self)


class DataModelGenerationWorkflow:
    def __init__(self, repo_root: str | Path, workspace: str | Path) -> None:
        self.repo_root = Path(repo_root).resolve()
        self.workspace = (self.repo_root / workspace).resolve()
        self.layout = WorkspaceLayout(project_root=self.workspace)

    def prepare(self) -> DataModelGenerationResult:
        self._validate_workspace()
        self.layout.ensure_runtime_dirs()
        listing = list_workspace_files(self.repo_root, _rel(self.workspace, self.repo_root)).to_dict()
        profile_index = self._profile_index()
        model_files = self._model_files()
        text_models = [item for item in model_files if Path(item).suffix.lower() in TEXT_MODEL_SUFFIXES]
        image_models = [item for item in model_files if Path(item).suffix.lower() in IMAGE_MODEL_SUFFIXES]
        recommended = "option_a" if text_models else "option_b"
        if image_models and not text_models:
            recommended = "option_c"
        session = {
            "version": SESSION_VERSION,
            "workspace": _rel(self.workspace, self.repo_root),
            "created_at": _now(),
            "updated_at": _now(),
            "status": "awaiting_user_answer",
            "current_stage": "route_selection",
            "detected_files": listing,
            "profile_count": len(profile_index),
            "model_files": model_files,
            "text_model_files": text_models,
            "image_model_files": image_models,
            "pattern_library": _pattern_library_summary(),
            "decisions": [],
            "decision_operations": [],
            "draft_contract_path": "",
        }
        self._write_session(session)
        return self._write_panel(session, _route_panel(session, recommended))

    def apply_answer(
        self,
        *,
        answer: str,
        custom_note: str = "",
        operations: list[dict[str, Any]] | None = None,
    ) -> DataModelGenerationResult:
        self._validate_workspace()
        session = self._read_session()
        panel = self._read_current_panel()
        option = _resolve_option(panel, answer)
        now = _now()
        session.setdefault("decisions", []).append(
            {
                "stage": panel.get("stage", ""),
                "accepted_option_id": option.get("option_id", ""),
                "accepted_label": option.get("label", ""),
                "custom_note": custom_note,
                "accepted_at": now,
            }
        )
        session.setdefault("decision_operations", []).extend(
            _operations_from_option(option, operations or [], custom_note)
        )
        session["updated_at"] = now

        stage = str(panel.get("stage") or session.get("current_stage") or "")
        if stage == "route_selection":
            mode = str(option.get("value") or "generate_draft")
            draft = self._build_draft(mode=mode)
            draft_path = self._write_draft_contract(draft)
            session["draft_contract_path"] = _rel(draft_path, self.repo_root)
            session["current_stage"] = "entity_inventory"
            session["status"] = "awaiting_user_answer"
            self._write_draft_markdown_pack(draft, final=False)
            self._write_session(session)
            return self._write_panel(session, _entity_inventory_panel(session, draft))
        if stage == "entity_inventory":
            draft = self._load_draft(session)
            draft = _apply_model_operations(draft, session.get("decision_operations", []))
            draft["readiness"] = _readiness(draft)
            draft_path = self._write_draft_contract(draft)
            session["draft_contract_path"] = _rel(draft_path, self.repo_root)
            session["current_stage"] = "risk_review"
            session["status"] = "awaiting_user_answer"
            self._write_draft_markdown_pack(draft, final=False)
            self._write_session(session)
            return self._write_panel(session, _risk_review_panel(session, draft))
        if stage == "risk_review":
            draft = self._load_draft(session)
            draft = _apply_model_operations(draft, session.get("decision_operations", []))
            draft["readiness"] = _readiness(draft)
            draft_path = self._write_draft_contract(draft)
            session["draft_contract_path"] = _rel(draft_path, self.repo_root)
            session["current_stage"] = "final_preview"
            session["status"] = "awaiting_final_preview_approval"
            self._write_draft_markdown_pack(draft, final=False)
            self._write_session(session)
            return self._write_panel(session, _final_preview_panel(session, draft))
        if stage == "final_preview":
            return self._write_panel(session, _terminal_panel(session))
        raise ValueError(f"Unsupported data model generation stage: {stage}")

    def prepare_blocker_panel(self) -> DataModelBlockerPanelResult:
        self._validate_workspace()
        session = self._read_session()
        draft = self._load_draft(session)
        blockers = _model_blockers(draft)
        panel = blockers[0] if blockers else _empty_model_blocker_panel(session, draft)
        panel = {**panel, "understanding": _understanding_score(draft)}
        session["current_model_blocker_id"] = str(panel.get("blocker_id") or "")
        session["updated_at"] = _now()
        self._write_session(session)
        return self._write_model_blocker_panel(session, panel, len(blockers))

    def apply_blocker_answer(
        self,
        *,
        answer: str,
        custom_note: str = "",
    ) -> DataModelBlockerPanelResult:
        self._validate_workspace()
        session = self._read_session()
        panel = self._read_model_blocker_panel()
        if panel.get("status") != "needs_user_answer":
            return self._write_model_blocker_panel(session, panel, 0)
        option = _resolve_option(panel, answer)
        draft = self._load_draft(session)
        operations = _operations_from_option(option, [], custom_note)
        session.setdefault("decisions", []).append(
            {
                "stage": "model_blocker_panel",
                "blocker_id": panel.get("blocker_id", ""),
                "accepted_option_id": option.get("option_id", ""),
                "accepted_label": option.get("label", ""),
                "custom_note": custom_note,
                "accepted_at": _now(),
            }
        )
        session.setdefault("decision_operations", []).extend(operations)
        draft = _apply_model_operations(draft, operations)
        draft["readiness"] = _readiness(draft)
        draft_path = self._write_draft_contract(draft)
        session["draft_contract_path"] = _rel(draft_path, self.repo_root)
        session["updated_at"] = _now()
        self._write_session(session)
        self._write_draft_markdown_pack(draft, final=False)
        blockers = _model_blockers(draft)
        next_panel = blockers[0] if blockers else _empty_model_blocker_panel(session, draft)
        session["current_model_blocker_id"] = str(next_panel.get("blocker_id") or "")
        self._write_session(session)
        return self._write_model_blocker_panel(session, next_panel, len(blockers))

    def finalize(self, *, approve_final_preview: bool, replace_existing: bool = True) -> DataModelFinalizeResult:
        self._validate_workspace()
        if not approve_final_preview:
            raise PermissionError("--approve-final-preview is required before writing data model docs")
        session = self._read_session()
        draft_path_value = str(session.get("draft_contract_path") or "")
        if not draft_path_value:
            raise FileNotFoundError("No data model draft is available to finalize")
        draft_path = self.repo_root / draft_path_value
        draft = json.loads(draft_path.read_text(encoding="utf-8"))
        finalized = {
            **draft,
            "status": "finalized",
            "finalized_at": _now(),
            "relationships": [
                _approve_relationship(item)
                for item in draft.get("relationships", [])
            ],
        }
        contract_path = self.layout.contracts_dir / "data_model_contract.json"
        contract_path.parent.mkdir(parents=True, exist_ok=True)
        contract_path.write_text(json.dumps(finalized, indent=2) + "\n", encoding="utf-8")

        docs_dir = self.workspace / "docs"
        docs_dir.mkdir(parents=True, exist_ok=True)
        outputs = self._write_final_docs(finalized, docs_dir, replace_existing=replace_existing)
        session["status"] = "finalized"
        session["current_stage"] = "finalized"
        session["final_contract_path"] = _rel(contract_path, self.repo_root)
        session["updated_at"] = _now()
        self._write_session(session)
        return DataModelFinalizeResult(
            workspace=_rel(self.workspace, self.repo_root),
            session_path=_rel(self._session_path(), self.repo_root),
            contract_path=_rel(contract_path, self.repo_root),
            data_model_markdown_path=_rel(outputs["data_model"], self.repo_root),
            erd_markdown_path=_rel(outputs["erd"], self.repo_root),
            relationships_markdown_path=_rel(outputs["relationships"], self.repo_root),
            status="finalized",
        )

    def _build_draft(self, *, mode: str) -> dict[str, Any]:
        profiles = self._profile_index()
        model_files = self._model_files()
        text_models = [item for item in model_files if Path(item).suffix.lower() in TEXT_MODEL_SUFFIXES]
        image_models = [item for item in model_files if Path(item).suffix.lower() in IMAGE_MODEL_SUFFIXES]
        patterns = _load_pattern_library()
        tables = [_table_from_profile(profile, patterns) for profile in profiles.values()]
        relationships = _candidate_relationships(tables)
        relationships = _promote_from_text_models(relationships, text_models, self.repo_root)
        draft = {
            "version": 1,
            "generated_by": "data-model-generation",
            "workspace": _rel(self.workspace, self.repo_root),
            "mode": mode,
            "status": "draft_requires_final_preview_approval",
            "generated_at": _now(),
            "pattern_library": {
                "version": patterns.get("version", 1),
                "patterns": [
                    {
                        "pattern_id": pattern.get("pattern_id", ""),
                        "label": pattern.get("label", ""),
                        "downstream_unlocks": pattern.get("downstream_unlocks", []),
                    }
                    for pattern in patterns.get("patterns", [])
                ],
            },
            "architecture_reference": _architecture_reference(),
            "evidence_order": ["profile_index", "text_data_model_docs", "reviewed_diagram_sidecars", "user_approval"],
            "image_model_policy": {
                "state": "review_gated",
                "rule": "Images require a reviewed text/JSON/Markdown sidecar before relationships become executable.",
                "image_files": image_models,
                "parse_contract": _image_parse_contract(image_models),
            },
            "tables": tables,
            "relationships": relationships,
            "sidecar_requests": [
                {
                    "image_path": image,
                    "requested_sidecar": f"{Path(image).with_suffix('.model.md')}",
                    "status": "needed_for_image_relationship_proof",
                }
                for image in image_models
            ],
            "summary": {
                "table_count": len(tables),
                "relationship_count": len(relationships),
                "approved_relationship_count": sum(
                    1 for item in relationships if item.get("approval", {}).get("state") == "approved"
                ),
                "image_model_count": len(image_models),
                "text_model_count": len(text_models),
            },
        }
        draft["readiness"] = _readiness(draft)
        draft["understanding"] = _understanding_score(draft)
        return draft

    def _model_files(self) -> list[str]:
        docs_dir = self.workspace / "docs"
        if not docs_dir.exists():
            return []
        files = []
        for path in sorted(p for p in docs_dir.rglob("*") if p.is_file()):
            suffix = path.suffix.lower()
            if suffix not in TEXT_MODEL_SUFFIXES | IMAGE_MODEL_SUFFIXES:
                continue
            haystack = path.name.lower()
            if any(token in haystack for token in MODEL_NAME_TOKENS):
                files.append(_rel(path, self.repo_root))
        return files

    def _profile_index(self) -> dict[str, dict[str, Any]]:
        path = self.layout.profiles_dir / "profile_index.json"
        if not path.exists():
            return {}
        data = json.loads(path.read_text(encoding="utf-8"))
        profiles = {}
        for profile in data.get("profiles", []):
            source = _repo_path(str(profile.get("path") or ""), self.repo_root)
            if source:
                profiles[source] = {**profile, "path": source}
        return profiles

    def _write_draft_contract(self, draft: dict[str, Any]) -> Path:
        path = self.layout.requirements_dir / "data_model_draft.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(draft, indent=2) + "\n", encoding="utf-8")
        return path

    def _write_draft_markdown_pack(self, draft: dict[str, Any], *, final: bool) -> dict[str, Path]:
        base = self.layout.reports_dir / "data_model_generation"
        base.mkdir(parents=True, exist_ok=True)
        outputs = {
            "column_taxonomy": base / "column-taxonomy.md",
            "entity_map": base / "entity-map.md",
            "data_model": base / "data-model.md",
            "erd": base / "erd.md",
            "relationships": base / "relationships.md",
        }
        outputs["column_taxonomy"].write_text(_render_column_taxonomy(draft), encoding="utf-8")
        outputs["entity_map"].write_text(_render_entity_map(draft), encoding="utf-8")
        outputs["data_model"].write_text(_render_data_model_md(draft), encoding="utf-8")
        outputs["erd"].write_text(_render_erd_md(draft), encoding="utf-8")
        outputs["relationships"].write_text(_render_relationships_md(draft), encoding="utf-8")
        return outputs

    def _write_final_docs(self, contract: dict[str, Any], docs_dir: Path, *, replace_existing: bool) -> dict[str, Path]:
        outputs = {
            "data_model": docs_dir / "data-model.md",
            "erd": docs_dir / "erd.md",
            "relationships": docs_dir / "relationships.md",
        }
        for path in outputs.values():
            if path.exists() and not replace_existing:
                raise FileExistsError(f"output already exists: {_rel(path, self.repo_root)}")
        outputs["data_model"].write_text(_render_data_model_md(contract), encoding="utf-8")
        outputs["erd"].write_text(_render_erd_md(contract), encoding="utf-8")
        outputs["relationships"].write_text(_render_relationships_md(contract), encoding="utf-8")
        return outputs

    def _write_session(self, session: dict[str, Any]) -> Path:
        path = self._session_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(session, indent=2) + "\n", encoding="utf-8")
        return path

    def _read_session(self) -> dict[str, Any]:
        path = self._session_path()
        if not path.exists():
            raise FileNotFoundError(f"Data model generation session not found: {_rel(path, self.repo_root)}")
        return json.loads(path.read_text(encoding="utf-8"))

    def _read_current_panel(self) -> dict[str, Any]:
        path = self._current_json_path()
        if not path.exists():
            raise FileNotFoundError(f"Data model generation panel not found: {_rel(path, self.repo_root)}")
        return json.loads(path.read_text(encoding="utf-8"))

    def _read_model_blocker_panel(self) -> dict[str, Any]:
        path = self._model_blocker_json_path()
        if not path.exists():
            raise FileNotFoundError(f"Data model blocker panel not found: {_rel(path, self.repo_root)}")
        return json.loads(path.read_text(encoding="utf-8"))

    def _load_draft(self, session: dict[str, Any]) -> dict[str, Any]:
        draft_path_value = str(session.get("draft_contract_path") or "")
        if not draft_path_value:
            raise FileNotFoundError("No data model draft is available")
        return json.loads((self.repo_root / draft_path_value).read_text(encoding="utf-8"))

    def _write_panel(self, session: dict[str, Any], panel: dict[str, Any]) -> DataModelGenerationResult:
        # Route the orchestrating CLI agent to the right conversation skill for this
        # turn, driven by the data-model understanding score.
        panel.setdefault("suggested_skills", _dm_conversation_skills(panel))
        from core.onboarding.panel_contract import normalize_decision_panel
        normalize_decision_panel(
            panel, workspace=_rel(self.workspace, self.repo_root), routing_stage="data_model_design"
        )
        current_json = self._current_json_path()
        current_md = self._current_markdown_path()
        current_json.parent.mkdir(parents=True, exist_ok=True)
        current_json.write_text(json.dumps(panel, indent=2) + "\n", encoding="utf-8")
        current_md.write_text(_render_panel_markdown(panel), encoding="utf-8")
        return DataModelGenerationResult(
            workspace=_rel(self.workspace, self.repo_root),
            session_path=_rel(self._session_path(), self.repo_root),
            current_json_path=_rel(current_json, self.repo_root),
            current_markdown_path=_rel(current_md, self.repo_root),
            stage=str(panel.get("stage") or session.get("current_stage") or ""),
            status=str(session.get("status") or ""),
            next_step=str(panel.get("next_step") or ""),
        )

    def _session_path(self) -> Path:
        return self.layout.requirements_dir / "data_model_generation_session.json"

    def _current_json_path(self) -> Path:
        return self.layout.reports_dir / "data_model_generation" / "current.json"

    def _current_markdown_path(self) -> Path:
        return self.layout.reports_dir / "data_model_generation" / "current.md"

    def _model_blocker_json_path(self) -> Path:
        return self.layout.reports_dir / "data_model_blocker_panel" / "current.json"

    def _model_blocker_markdown_path(self) -> Path:
        return self.layout.reports_dir / "data_model_blocker_panel" / "current.md"

    def _write_model_blocker_panel(
        self,
        session: dict[str, Any],
        panel: dict[str, Any],
        blocker_count: int,
    ) -> DataModelBlockerPanelResult:
        current_json = self._model_blocker_json_path()
        current_md = self._model_blocker_markdown_path()
        panel = {**panel, "workspace": panel.get("workspace") or session.get("workspace", "")}
        current_json.parent.mkdir(parents=True, exist_ok=True)
        current_json.write_text(json.dumps(panel, indent=2) + "\n", encoding="utf-8")
        current_md.write_text(_render_model_blocker_markdown(panel), encoding="utf-8")
        return DataModelBlockerPanelResult(
            workspace=_rel(self.workspace, self.repo_root),
            session_path=_rel(self._session_path(), self.repo_root),
            current_json_path=_rel(current_json, self.repo_root),
            current_markdown_path=_rel(current_md, self.repo_root),
            blocker_count=blocker_count,
            current_blocker_id=str(panel.get("blocker_id") or ""),
            status=str(panel.get("status") or session.get("status") or ""),
        )

    def _validate_workspace(self) -> None:
        if not self.workspace.exists():
            raise FileNotFoundError(f"workspace not found: {self.workspace}")
        if self.workspace == self.repo_root or not self.workspace.is_relative_to(self.repo_root):
            raise ValueError(f"workspace must be inside repo root: {self.workspace}")


def _route_panel(session: dict[str, Any], recommended: str) -> dict[str, Any]:
    return {
        "version": SESSION_VERSION,
        "workspace": session["workspace"],
        "stage": "route_selection",
        "question": "Which data model workflow should this workspace use now?",
        "options": [
            {
                "option_id": "option_a",
                "label": "Parse existing text model",
                "value": "parse_existing",
                "description": "Use parseable Markdown/JSON/YAML model docs plus profile evidence.",
            },
            {
                "option_id": "option_b",
                "label": "Generate draft from profiles",
                "value": "generate_from_profiles",
                "description": "Create a draft data model from profiled CSV schemas and KPI needs.",
            },
            {
                "option_id": "option_c",
                "label": "Review diagram sidecar",
                "value": "diagram_sidecar",
                "description": "Treat image diagrams as review-gated inputs and request text sidecars.",
            },
        ],
        "recommended_option_id": recommended,
        "recommended_answer": recommended,
        "why": _route_reason(session, recommended),
        "model_files": session.get("model_files", []),
        "pattern_library": session.get("pattern_library", {}),
        "next_step": "Apply the chosen path with apply-data-model-answer.",
    }


def _entity_inventory_panel(session: dict[str, Any], draft: dict[str, Any]) -> dict[str, Any]:
    return {
        "version": SESSION_VERSION,
        "workspace": session["workspace"],
        "stage": "entity_inventory",
        "question": "Review the candidate entity inventory before modeling risk decisions.",
        "options": [
            {
                "option_id": "option_a",
                "label": "Accept entity inventory",
                "value": "accept_inventory",
                "description": "Keep the detected entities and continue to risk-ranked modeling decisions.",
                "operations": [],
            },
            {
                "option_id": "option_b",
                "label": "Revise with operations",
                "value": "revise_inventory",
                "description": (
                    "Apply structured add/remove/rename/change operations before risk review."
                ),
            },
            {
                "option_id": "option_c",
                "label": "Keep draft with notes",
                "value": "note_only",
                "description": "Record supporting context without changing the structured draft.",
                "operations": [],
            },
        ],
        "recommended_option_id": "option_a",
        "recommended_answer": "Accept entity inventory unless a source entity is missing or misclassified.",
        "why": "Profiles provide the safest first-pass entity inventory; high-risk decisions are handled next.",
        "entity_count": len(draft.get("tables", [])),
        "entities": [
            {
                "name": table.get("name", ""),
                "source_dataset": table.get("source_dataset", ""),
                "role": table.get("role", ""),
                "table_pattern": table.get("table_pattern", ""),
                "primary_key": table.get("primary_key", []),
            }
            for table in draft.get("tables", [])
        ],
        "supported_operations": _supported_operations(),
        "next_step": "Apply an option, optionally with --operation JSON for add/remove/change.",
    }


def _risk_review_panel(session: dict[str, Any], draft: dict[str, Any]) -> dict[str, Any]:
    readiness = draft.get("readiness", {})
    blockers = _risk_blockers(draft)
    return {
        "version": SESSION_VERSION,
        "workspace": session["workspace"],
        "stage": "risk_review",
        "question": "Resolve the highest-risk data-model decisions before final preview.",
        "options": [
            {
                "option_id": "option_a",
                "label": "Accept risk review",
                "value": "accept_risk_review",
                "description": "Continue to final preview with current readiness and blockers.",
                "operations": [{"operation": "approve_for_execution", "scope": "currently_proven_relationships"}],
            },
            {
                "option_id": "option_b",
                "label": "Apply model changes",
                "value": "apply_model_changes",
                "description": "Use structured operations for grain, keys, relationships, patterns, layers, and PII.",
            },
            {
                "option_id": "option_c",
                "label": "Hold for evidence",
                "value": "hold_for_evidence",
                "description": "Keep unresolved items blocked until dictionaries, diagrams, or owner approval are added.",
                "operations": [{"operation": "defer_unproven_items", "reason": "needs_more_evidence"}],
            },
        ],
        "recommended_option_id": "option_b" if blockers else "option_a",
        "recommended_answer": "Apply model changes when blockers remain; otherwise accept risk review.",
        "why": "Grain, keys, relationship proof, temporal anchors, and PII flags decide what later code generation can trust.",
        "readiness": readiness,
        "risk_blockers": blockers,
        "supported_operations": _supported_operations(),
        "next_step": "Apply an option to move to final preview.",
    }


def _final_preview_panel(session: dict[str, Any], draft: dict[str, Any]) -> dict[str, Any]:
    readiness = draft.get("readiness", {})
    recommended = "option_a" if readiness.get("overall", {}).get("score", 0) >= 70 else "option_b"
    return {
        "version": SESSION_VERSION,
        "workspace": session["workspace"],
        "stage": "final_preview",
        "question": "Review the draft data model pack before finalizing.",
        "options": [
            {
                "option_id": "option_a",
                "label": "Finalize after review",
                "value": "finalize",
                "description": "Run finalize-data-model-generation with --approve-final-preview.",
            },
            {
                "option_id": "option_b",
                "label": "Revise before saving",
                "value": "revise",
                "description": "Keep the draft under interns/ and resolve model issues first.",
            },
        ],
        "recommended_option_id": recommended,
        "recommended_answer": "Finalize only if readiness blockers are acceptable or resolved.",
        "why": "The final write updates user-facing docs and executable relationship evidence only after explicit approval.",
        "draft_contract_path": session.get("draft_contract_path", ""),
        "summary": draft.get("summary", {}),
        "readiness": readiness,
        "decision_operations": session.get("decision_operations", []),
        "sidecar_requests": draft.get("sidecar_requests", []),
        "next_step": "Run finalize-data-model-generation --approve-final-preview after reviewing the draft.",
    }


def _terminal_panel(session: dict[str, Any]) -> dict[str, Any]:
    return {
        "version": SESSION_VERSION,
        "workspace": session["workspace"],
        "stage": session.get("current_stage", "terminal"),
        "question": "No further data model generation question is active.",
        "options": [],
        "recommended_option_id": "",
        "recommended_answer": "",
        "why": "The workflow is waiting for final approval or a deterministic command.",
        "next_step": "Run the command named in the previous panel.",
    }


def _table_from_profile(profile: dict[str, Any], patterns: dict[str, Any]) -> dict[str, Any]:
    source = str(profile.get("path") or "")
    name = _table_name(source)
    schema = profile.get("schema") or {}
    columns = [
        {
            "name": column,
            "standard_name": _snake(column),
            "type": _sql_type(dtype),
            "source_type": str(dtype),
            "role": _column_role(column, dtype),
            "nullable": "unknown",
            "description": f"Profiled source column `{column}` from `{source}`.",
        }
        for column, dtype in schema.items()
    ]
    primary_key = _primary_key(columns)
    inference = _infer_table_pattern(name, columns, patterns)
    return {
        "name": name,
        "source_dataset": source,
        "description": f"Source-backed entity for `{Path(source).name}`.",
        "role": inference["role"],
        "table_pattern": inference["pattern_id"],
        "pattern_label": inference["label"],
        "pattern_evidence": inference["evidence"],
        "grain": {
            "description": _grain_description(name, columns, primary_key),
            "state": "inferred_needs_review",
        },
        "temporal_anchor": {
            "column": _first_role(columns, "timestamp"),
            "state": "inferred_needs_review" if _first_role(columns, "timestamp") else "not_found",
        },
        "scd_type": "not_applicable" if inference["role"] == "fact" else "needs_review",
        "medallion_layer": "silver",
        "dimensional_model_role": _dimensional_model_role(inference["role"]),
        "star_schema_candidate": _star_schema_candidate(inference["role"], columns, primary_key),
        "medallion_contract": _medallion_table_contract(inference["role"]),
        "load_pattern": _load_pattern_for(inference["pattern_id"]),
        "partitioning_policy": {
            "state": "candidate" if _first_role(columns, "timestamp") else "not_recommended_yet",
            "column": _first_role(columns, "timestamp"),
        },
        "indexing_policy": {
            "state": "candidate",
            "columns": primary_key,
        },
        "pii_columns": _pii_columns(columns),
        "primary_key": primary_key,
        "columns": columns,
        "approval": {"state": "draft"},
    }


def _architecture_reference() -> dict[str, Any]:
    return {
        "version": "1.0.0",
        "purpose": "Machine-parseable rules for data-model understanding, star schema design, medallion layers, and image sidecar review.",
        "star_schema": {
            "fact_table": {
                "required": ["grain", "foreign_keys", "numeric_measures", "temporal_anchor"],
                "grain_types": ["transaction", "periodic_snapshot", "accumulating_snapshot", "factless"],
                "anti_patterns": [
                    "descriptive_text_attributes_in_fact",
                    "missing_fact_grain",
                    "measures_in_dimension_tables",
                    "natural_keys_as_dimension_primary_keys",
                ],
            },
            "dimension_table": {
                "required": ["surrogate_key", "natural_key", "descriptive_attributes", "scd_policy"],
                "scd_types": [0, 1, 2, 3, 4, 6],
                "default_scd_type": 2,
                "anti_patterns": [
                    "scd2_without_validity_columns",
                    "dimension_to_dimension_joins_without_snowflake_label",
                    "nullable_fact_fk_without_unknown_member_policy",
                ],
            },
            "relationships": {
                "default_fact_to_dimension_cardinality": "many_to_one",
                "default_join_type": "left",
                "required_proof": ["fk_column", "referenced_pk", "cardinality", "grain_compatibility"],
            },
        },
        "medallion": {
            "bronze": {
                "purpose": "Raw append-only ingestion with source fidelity.",
                "allowed_transformations": ["add_ingestion_metadata", "capture_raw_payload"],
                "forbidden_transformations": ["deduplication", "business_logic", "semantic_renaming"],
                "required_metadata": ["_ingestion_timestamp", "_source_system", "_source_table", "_batch_id"],
            },
            "silver": {
                "purpose": "Cleaned, typed, conformed, reusable entities/events.",
                "allowed_transformations": [
                    "type_cast",
                    "null_handling",
                    "standardization",
                    "reference_validation",
                    "approved_semantic_conformance",
                    "approval_gated_deduplication",
                    "pii_masking",
                ],
                "forbidden_transformations": ["kpi_specific_aggregation", "final_metric_formula"],
                "required_metadata": ["_valid_from", "_valid_to", "_is_current", "_dq_score", "_is_deleted"],
            },
            "gold": {
                "purpose": "Business-ready KPI outputs, aggregations, and dimensional models.",
                "allowed_structures": ["star_schema", "snowflake_schema", "galaxy_schema", "certified_aggregate"],
                "required_checks": ["gold_reads_from_silver_or_approved_conformed_source", "business_friendly_names", "sensitive_columns_excluded_or_masked"],
                "anti_patterns": ["gold_directly_from_bronze", "bronze_metadata_leak", "raw_source_column_names_in_bi_output"],
            },
        },
        "canonical_outputs": {
            "table": ["name", "role", "grain", "primary_key", "columns", "medallion_layer", "dimensional_model_role"],
            "relationship": ["from_table", "from_column", "to_table", "to_column", "cardinality", "join_type", "confidence", "approval"],
            "image_parse": ["parse_metadata", "schema", "warnings", "confidence_by_element"],
        },
    }


def _image_parse_contract(image_models: list[str]) -> dict[str, Any]:
    return {
        "required_when_image_models_present": bool(image_models),
        "schema_type_detection": {
            "star_schema_signals": ["central_fact_box", "surrounding_dimension_boxes", "fact_to_dimension_lines", "FACT_or_DIM_prefixes"],
            "medallion_signals": ["bronze_silver_gold_layers", "left_to_right_pipeline_arrows", "source_and_consumer_icons"],
            "combined_signal": "medallion_pipeline_with_gold_star_schema",
        },
        "sidecar_required_fields": [
            "detected_schema_types",
            "tables",
            "columns",
            "relationships",
            "cardinality",
            "join_columns",
            "confidence_by_element",
            "warnings",
        ],
        "confidence_rules": {
            "explicit_fact_or_dim_label": 0.95,
            "position_inferred_table_role": 0.75,
            "explicit_pk_fk_marker": 0.95,
            "name_suffix_inferred_key": 0.70,
            "explicit_cardinality": 0.90,
            "topology_inferred_relationship": 0.75,
        },
        "execution_policy": "Image-derived relationships remain non-executable until reviewed sidecar text and user approval promote them.",
    }


def _dimensional_model_role(role: str) -> str:
    if role == "fact":
        return "fact_candidate"
    if role == "dimension":
        return "dimension_candidate"
    if role == "bridge":
        return "bridge_candidate"
    return "supporting_table_candidate"


def _star_schema_candidate(role: str, columns: list[dict[str, Any]], primary_key: list[str]) -> dict[str, Any]:
    measures = [column["name"] for column in columns if column.get("role") == "measure"]
    identifiers = [column["name"] for column in columns if column.get("role") == "identifier"]
    descriptive = [column["name"] for column in columns if column.get("role") == "descriptor"]
    is_fact = role == "fact"
    return {
        "role": _dimensional_model_role(role),
        "grain_required": is_fact,
        "surrogate_key_required": role == "dimension",
        "natural_key_candidates": identifiers[:5],
        "measure_candidates": measures[:8],
        "descriptive_attribute_candidates": descriptive[:12],
        "primary_key_candidates": primary_key,
        "validation_checks": [
            "grain_declared" if is_fact else "natural_key_declared",
            "measure_columns_present" if is_fact else "descriptive_attributes_present",
            "relationship_cardinality_reviewed",
        ],
    }


def _medallion_table_contract(role: str) -> dict[str, Any]:
    return {
        "bronze": {
            "state": "source_landing_required",
            "rule": "Preserve original source columns and add ingestion metadata only.",
        },
        "silver": {
            "state": "default_target_layer",
            "rule": "Apply technical normalization plus approved semantic conformance; keep KPI formulas out.",
        },
        "gold": {
            "state": "candidate_if_kpi_or_bi_output",
            "rule": "Use star/snowflake/aggregate output from conformed Silver data.",
            "dimensional_role": _dimensional_model_role(role),
        },
    }


def _candidate_relationships(tables: list[dict[str, Any]]) -> list[dict[str, Any]]:
    relationships = []
    for idx, left in enumerate(tables):
        for right in tables[idx + 1:]:
            match = _shared_key(left, right)
            if not match:
                continue
            left_col, right_col, match_basis = match
            name_only = match_basis == "name_only"
            relationships.append(
                {
                    "relationship_id": _relationship_id(left["name"], left_col, right["name"], right_col),
                    "from_table": left["name"],
                    "from_dataset": left["source_dataset"],
                    "from_column": left_col,
                    "to_table": right["name"],
                    "to_dataset": right["source_dataset"],
                    "to_column": right_col,
                    "cardinality": "many_to_one_or_many_to_many_pending_validation",
                    "join_type": "left",
                    # A join inferred ONLY from a shared column name is a candidate to confirm,
                    # never proof. It must ask the user (confirm/reject/relabel) and stay below
                    # the candidate confidence ceiling until evidence or approval backs it.
                    "state": "candidate_name_only" if name_only else "candidate_profile",
                    "match_basis": match_basis,
                    "needs_confirmation": name_only,
                    "confidence": 0.45 if name_only else 0.62,
                    "approval": {"state": "needs_review"},
                    "evidence_sources": [
                        {
                            "type": "name_only_shared_column" if name_only else "profile_shared_key",
                            "left_dataset": left["source_dataset"],
                            "right_dataset": right["source_dataset"],
                            "normalized_key": _norm(left_col),
                            "reason": (
                                "Shared column name only; no identifier role or profile/cardinality proof."
                                if name_only
                                else "Shared column carries an identifier role on at least one side."
                            ),
                        }
                    ],
                }
            )
    return relationships


def _promote_from_text_models(
    relationships: list[dict[str, Any]],
    text_models: list[str],
    repo_root: Path,
) -> list[dict[str, Any]]:
    docs = []
    for value in text_models:
        path = repo_root / value
        try:
            docs.append({"path": value, "text": path.read_text(encoding="utf-8", errors="ignore")})
        except OSError:
            continue
    promoted = []
    for relationship in relationships:
        evidence = _text_model_evidence(relationship, docs)
        if not evidence:
            promoted.append(relationship)
            continue
        promoted.append(
            {
                **relationship,
                "state": "proven_data_model",
                "match_basis": "text_data_model_doc",
                "needs_confirmation": False,
                "confidence": 0.9,
                "approval": {"state": "approved"},
                "evidence_sources": [*relationship.get("evidence_sources", []), evidence],
            }
        )
    return promoted


def _text_model_evidence(relationship: dict[str, Any], docs: list[dict[str, str]]) -> dict[str, Any] | None:
    left_terms = _dataset_terms(relationship.get("from_dataset", ""))
    right_terms = _dataset_terms(relationship.get("to_dataset", ""))
    left_col = _norm(relationship.get("from_column", ""))
    right_col = _norm(relationship.get("to_column", ""))
    for doc in docs:
        normalized = _norm(doc["text"])
        lowered = doc["text"].lower()
        if not any(token in lowered for token in ("join", "relationship", "foreign key", "dimension", "fact")):
            continue
        if not any(term in normalized for term in left_terms):
            continue
        if not any(term in normalized for term in right_terms):
            continue
        if left_col not in normalized and right_col not in normalized:
            continue
        return {
            "type": "text_data_model_doc",
            "path": doc["path"],
            "reason": "Text data model references both entities and the shared join key.",
        }
    return None


def _approve_relationship(relationship: dict[str, Any]) -> dict[str, Any]:
    return {
        **relationship,
        "state": "user_confirmed"
        if relationship.get("approval", {}).get("state") != "approved"
        else relationship.get("state", "proven_data_model"),
        "approval": {
            "state": "approved",
            "approved_at": _now(),
            "approval_source": "finalize-data-model-generation",
        },
    }


def _render_data_model_md(contract: dict[str, Any]) -> str:
    lines = ["# Data Model", "", f"- Workspace: `{contract.get('workspace', '')}`", ""]
    reference = contract.get("architecture_reference") or {}
    if reference:
        lines.extend(
            [
                "## Architecture Reference",
                "",
                f"- Version: `{reference.get('version', '')}`",
                "- Star schema: facts require grain, measures, temporal anchor, and proven relationships; dimensions require surrogate/natural keys and SCD policy.",
                "- Medallion: Bronze preserves raw source, Silver cleans/conforms reusable entities, Gold serves business/KPI star or aggregate outputs.",
                "- Image diagrams remain review-gated until a text sidecar and approval promote relationships.",
                "",
            ]
        )
    for table in contract.get("tables", []):
        lines.extend([
            f"## {table.get('name', '')}",
            "",
            f"> {table.get('description', '')}",
            "",
            f"- Dimensional role: `{table.get('dimensional_model_role', '')}`",
            f"- Medallion layer: `{table.get('medallion_layer', '')}`",
            f"- Table pattern: `{table.get('table_pattern', '')}`",
            "",
            "| column | type | role | nullable | description |",
            "|---|---|---|---|---|",
        ])
        for column in table.get("columns", []):
            lines.append(
                "| {name} | {type} | {role} | {nullable} | {description} |".format(
                    name=column.get("name", ""),
                    type=column.get("type", ""),
                    role=column.get("role", ""),
                    nullable=column.get("nullable", ""),
                    description=str(column.get("description", "")).replace("|", "\\|"),
                )
            )
        lines.extend(["", f"**Primary key:** `{', '.join(table.get('primary_key') or []) or 'needs_review'}`", ""])
    return "\n".join(lines).rstrip() + "\n"


def _render_erd_md(contract: dict[str, Any]) -> str:
    lines = ["# ERD", "", "```mermaid", "erDiagram"]
    for table in contract.get("tables", []):
        lines.append(f"  {table.get('name', '')} {{")
        for column in table.get("columns", []):
            lines.append(f"    {column.get('type', 'TEXT')} {column.get('name', '')}")
        lines.append("  }")
    for rel in contract.get("relationships", []):
        lines.append(
            f"  {rel.get('from_table')} }}o--|| {rel.get('to_table')} : "
            f"\"{rel.get('from_column')} to {rel.get('to_column')}\""
        )
    lines.extend(["```", ""])
    return "\n".join(lines)


def _render_relationships_md(contract: dict[str, Any]) -> str:
    lines = [
        "# Relationships",
        "",
        "| from_table | from_col | to_table | to_col | state | approval |",
        "|---|---|---|---|---|---|",
    ]
    for rel in contract.get("relationships", []):
        lines.append(
            f"| {rel.get('from_table', '')} | {rel.get('from_column', '')} | "
            f"{rel.get('to_table', '')} | {rel.get('to_column', '')} | "
            f"{rel.get('state', '')} | {rel.get('approval', {}).get('state', '')} |"
        )
    lines.append("")
    return "\n".join(lines)


def _render_column_taxonomy(contract: dict[str, Any]) -> str:
    lines = [
        "# Column Taxonomy",
        "",
        "| table | original_name | standard_name | role | data_type | notes |",
        "|---|---|---|---|---|---|",
    ]
    for table in contract.get("tables", []):
        for column in table.get("columns", []):
            lines.append(
                f"| {table.get('name', '')} | {column.get('name', '')} | "
                f"{column.get('standard_name', '')} | {column.get('role', '')} | "
                f"{column.get('type', '')} | source profile evidence |"
            )
    lines.append("")
    return "\n".join(lines)


def _render_entity_map(contract: dict[str, Any]) -> str:
    lines = ["# Entity Map", ""]
    for table in contract.get("tables", []):
        lines.extend([
            f"## {table.get('name', '')}",
            "",
            f"- Source dataset: `{table.get('source_dataset', '')}`",
            f"- Primary key: `{', '.join(table.get('primary_key') or []) or 'needs_review'}`",
            f"- Columns: {len(table.get('columns', []))}",
            "",
        ])
    return "\n".join(lines).rstrip() + "\n"


def _render_panel_markdown(panel: dict[str, Any]) -> str:
    lines = [
        f"# Data Model Generation Panel: {panel.get('stage', '')}",
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
    for option in panel.get("options", []):
        lines.extend([
            f"### {option.get('option_id')}: {option.get('label')}",
            "",
            str(option.get("description", "")),
            "",
        ])
    lines.extend([
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
    ])
    return "\n".join(lines)


def _route_reason(session: dict[str, Any], recommended: str) -> str:
    if recommended == "option_a":
        return "Parseable text data model docs are present, so use them as the strongest model evidence."
    if recommended == "option_c":
        return "Only diagram/image model artifacts were found; v1 requires reviewed sidecar interpretation."
    return "No strong parseable model exists, so generate a draft from profile evidence."


def _resolve_option(panel: dict[str, Any], answer: str) -> dict[str, Any]:
    normalized = _norm(answer)
    for option in panel.get("options", []):
        candidates = {
            _norm(str(option.get("option_id", ""))),
            _norm(str(option.get("label", ""))),
            _norm(str(option.get("value", ""))),
        }
        if normalized in candidates:
            return option
    short = {"a": "optiona", "b": "optionb", "c": "optionc"}.get(normalized)
    if short:
        for option in panel.get("options", []):
            if _norm(str(option.get("option_id", ""))) == short:
                return option
    raise ValueError(f"Answer does not match a current panel option: {answer}")


def _load_pattern_library() -> dict[str, Any]:
    data = json.loads(PATTERN_LIBRARY_PATH.read_text(encoding="utf-8"))
    if not isinstance(data.get("patterns"), list):
        raise ValueError("patterns.json must contain a patterns list")
    return data


def _pattern_library_summary() -> dict[str, Any]:
    patterns = _load_pattern_library()
    return {
        "version": patterns.get("version", 1),
        "pattern_count": len(patterns.get("patterns", [])),
        "patterns": [
            {"pattern_id": item.get("pattern_id", ""), "label": item.get("label", "")}
            for item in patterns.get("patterns", [])
        ],
    }


def _pattern_by_id(patterns: dict[str, Any], pattern_id: str) -> dict[str, Any]:
    for pattern in patterns.get("patterns", []):
        if pattern.get("pattern_id") == pattern_id:
            return pattern
    return {}


def _infer_table_pattern(name: str, columns: list[dict[str, Any]], patterns: dict[str, Any]) -> dict[str, Any]:
    norm_name = _norm(name)
    measure_count = sum(1 for column in columns if column.get("role") == "measure")
    id_count = sum(1 for column in columns if column.get("role") == "identifier")
    timestamp_count = sum(1 for column in columns if column.get("role") == "timestamp")
    if "bridge" in norm_name:
        pattern_id = "bridge_table"
        role = "bridge"
        evidence = ["table name contains bridge", "many-to-many relationship table candidate"]
    elif any(token in norm_name for token in ("audit", "event", "log")):
        pattern_id = "audit_event_log"
        role = "event_log"
        evidence = ["table name suggests event or audit log"]
    elif any(token in norm_name for token in ("snapshot", "balance", "inventory")):
        pattern_id = "periodic_snapshot_fact"
        role = "fact"
        evidence = ["table name suggests periodic snapshot"]
    elif norm_name.startswith("dim") or (id_count <= 2 and measure_count == 0):
        pattern_id = "dimension_scd2"
        role = "dimension"
        evidence = ["mostly descriptive columns", "no obvious additive measures"]
    elif measure_count > 0 or "fact" in norm_name or any(
        token in norm_name for token in ("transaction", "order", "event", "log", "record", "entry")
    ):
        pattern_id = "transaction_fact"
        role = "fact"
        evidence = ["measure columns detected" if measure_count else "table name suggests transactional fact"]
    elif timestamp_count and id_count:
        pattern_id = "audit_event_log"
        role = "event_log"
        evidence = ["timestamp and identifiers detected"]
    else:
        pattern_id = "dimension_scd2"
        role = "dimension"
        evidence = ["defaulted to dimension candidate pending review"]
    pattern = _pattern_by_id(patterns, pattern_id)
    return {
        "pattern_id": pattern_id,
        "label": pattern.get("label", pattern_id),
        "role": role,
        "evidence": evidence,
    }


def _first_role(columns: list[dict[str, Any]], role: str) -> str:
    for column in columns:
        if column.get("role") == role:
            return str(column.get("name") or "")
    return ""


def _grain_description(name: str, columns: list[dict[str, Any]], primary_key: list[str]) -> str:
    if primary_key:
        return f"one row per {name} record keyed by {', '.join(primary_key)}"
    identifiers = [str(column.get("name")) for column in columns if column.get("role") == "identifier"]
    if identifiers:
        return f"one row per candidate key combination: {', '.join(identifiers[:3])}"
    return "grain needs review"


def _load_pattern_for(pattern_id: str) -> str:
    if pattern_id in {"transaction_fact", "audit_event_log"}:
        return "append_only"
    if pattern_id == "periodic_snapshot_fact":
        return "snapshot_refresh"
    if pattern_id == "dimension_scd2":
        return "scd2_merge_pending_review"
    return "needs_review"


def _pii_columns(columns: list[dict[str, Any]]) -> list[str]:
    pii_tokens = ("email", "phone", "address", "dob", "birth", "ssn", "name")
    return [
        str(column.get("name") or "")
        for column in columns
        if any(token in _norm(column.get("name", "")) for token in pii_tokens)
    ]


def _supported_operations() -> list[dict[str, Any]]:
    return [
        {"operation": "add_entity", "required": ["name"], "optional": ["source_dataset", "role", "table_pattern"]},
        {"operation": "remove_entity", "required": ["name"]},
        {"operation": "rename_entity", "required": ["name", "new_name"]},
        {"operation": "set_entity_role", "required": ["name", "role"]},
        {"operation": "set_primary_key", "required": ["name", "columns"]},
        {"operation": "add_relationship", "required": ["from_table", "from_column", "to_table", "to_column"]},
        {"operation": "remove_relationship", "required": ["relationship_id"]},
        {"operation": "approve_relationship", "required": ["relationship_id"]},
        {"operation": "relabel_relationship", "required": ["relationship_id"], "optional": ["from_column", "to_column"]},
        {"operation": "set_grain", "required": ["name", "description"]},
        {"operation": "set_table_pattern", "required": ["name", "table_pattern"]},
        {"operation": "set_scd_type", "required": ["name", "scd_type"]},
        {"operation": "set_temporal_anchor", "required": ["name", "column"]},
        {"operation": "set_medallion_layer", "required": ["name", "layer"]},
        {"operation": "set_load_pattern", "required": ["name", "load_pattern"]},
        {"operation": "set_partitioning", "required": ["name", "column"]},
        {"operation": "set_indexing_policy", "required": ["name", "columns"]},
        {"operation": "mark_pii", "required": ["name", "columns"]},
        {"operation": "approve_for_execution", "required": ["scope"]},
    ]


def _operations_from_option(
    option: dict[str, Any],
    explicit_operations: list[dict[str, Any]],
    custom_note: str,
) -> list[dict[str, Any]]:
    now = _now()
    operations = []
    for operation in [*option.get("operations", []), *explicit_operations]:
        if not isinstance(operation, dict) or not operation.get("operation"):
            raise ValueError("data model operations must be JSON objects with an `operation` field")
        operations.append(
            {
                **operation,
                "source": "panel_or_cli",
                "note": custom_note,
                "accepted_at": now,
            }
        )
    return operations


def _apply_model_operations(draft: dict[str, Any], operations: list[dict[str, Any]]) -> dict[str, Any]:
    updated = json.loads(json.dumps(draft))
    applied_ids: set[str] = set()
    for operation in operations:
        operation_key = json.dumps(operation, sort_keys=True)
        if operation_key in applied_ids:
            continue
        applied_ids.add(operation_key)
        _apply_one_operation(updated, operation)
    updated["decision_operations"] = operations
    updated["understanding"] = _understanding_score(updated)
    updated["summary"] = {
        **updated.get("summary", {}),
        "table_count": len(updated.get("tables", [])),
        "relationship_count": len(updated.get("relationships", [])),
        "approved_relationship_count": sum(
            1 for item in updated.get("relationships", [])
            if item.get("approval", {}).get("state") == "approved"
        ),
    }
    return updated


def _apply_one_operation(draft: dict[str, Any], operation: dict[str, Any]) -> None:
    op = str(operation.get("operation") or "")
    if op == "add_entity":
        name = _snake(str(operation.get("name") or ""))
        if not name:
            raise ValueError("add_entity requires name")
        if _find_table(draft, name):
            return
        draft.setdefault("tables", []).append(
            {
                "name": name,
                "source_dataset": str(operation.get("source_dataset") or ""),
                "description": str(operation.get("description") or "User-added entity."),
                "role": str(operation.get("role") or "needs_review"),
                "table_pattern": str(operation.get("table_pattern") or "needs_review"),
                "pattern_label": str(operation.get("table_pattern") or "needs_review"),
                "pattern_evidence": ["user_added"],
                "grain": {"description": str(operation.get("grain") or "needs review"), "state": "user_added"},
                "temporal_anchor": {"column": "", "state": "needs_review"},
                "scd_type": "needs_review",
                "medallion_layer": str(operation.get("medallion_layer") or "silver"),
                "load_pattern": str(operation.get("load_pattern") or "needs_review"),
                "partitioning_policy": {"state": "needs_review", "column": ""},
                "indexing_policy": {"state": "needs_review", "columns": []},
                "pii_columns": [],
                "primary_key": [],
                "columns": [],
                "approval": {"state": "needs_review"},
            }
        )
        return
    if op == "remove_entity":
        name = _snake(str(operation.get("name") or ""))
        draft["tables"] = [table for table in draft.get("tables", []) if table.get("name") != name]
        draft["relationships"] = [
            rel for rel in draft.get("relationships", [])
            if rel.get("from_table") != name and rel.get("to_table") != name
        ]
        return
    if op == "rename_entity":
        table = _require_table(draft, str(operation.get("name") or ""))
        old = table["name"]
        new = _snake(str(operation.get("new_name") or ""))
        if not new:
            raise ValueError("rename_entity requires new_name")
        table["name"] = new
        for rel in draft.get("relationships", []):
            if rel.get("from_table") == old:
                rel["from_table"] = new
            if rel.get("to_table") == old:
                rel["to_table"] = new
        return
    if op in {
        "set_entity_role",
        "set_grain",
        "set_table_pattern",
        "set_scd_type",
        "set_temporal_anchor",
        "set_medallion_layer",
        "set_load_pattern",
        "set_partitioning",
        "set_indexing_policy",
        "mark_pii",
        "set_primary_key",
    }:
        table = _require_table(draft, str(operation.get("name") or ""))
        if op == "set_entity_role":
            table["role"] = str(operation.get("role") or "")
        elif op == "set_grain":
            table["grain"] = {"description": str(operation.get("description") or ""), "state": "user_confirmed"}
        elif op == "set_table_pattern":
            table["table_pattern"] = str(operation.get("table_pattern") or "")
            table["pattern_evidence"] = [*table.get("pattern_evidence", []), "user_confirmed_pattern"]
        elif op == "set_scd_type":
            table["scd_type"] = str(operation.get("scd_type") or "")
        elif op == "set_temporal_anchor":
            table["temporal_anchor"] = {"column": str(operation.get("column") or ""), "state": "user_confirmed"}
        elif op == "set_medallion_layer":
            table["medallion_layer"] = str(operation.get("layer") or "")
        elif op == "set_load_pattern":
            table["load_pattern"] = str(operation.get("load_pattern") or "")
        elif op == "set_partitioning":
            table["partitioning_policy"] = {"state": "user_confirmed", "column": str(operation.get("column") or "")}
        elif op == "set_indexing_policy":
            table["indexing_policy"] = {"state": "user_confirmed", "columns": _list_value(operation.get("columns"))}
        elif op == "mark_pii":
            table["pii_columns"] = _list_value(operation.get("columns"))
        elif op == "set_primary_key":
            table["primary_key"] = _list_value(operation.get("columns"))
        table.setdefault("approval", {})["state"] = "user_reviewed"
        return
    if op == "add_relationship":
        left_table = _snake(str(operation.get("from_table") or ""))
        right_table = _snake(str(operation.get("to_table") or ""))
        left_column = str(operation.get("from_column") or "")
        right_column = str(operation.get("to_column") or "")
        left = _require_table(draft, left_table)
        right = _require_table(draft, right_table)
        relationship = {
            "relationship_id": _relationship_id(left_table, left_column, right_table, right_column),
            "from_table": left_table,
            "from_dataset": left.get("source_dataset", ""),
            "from_column": left_column,
            "to_table": right_table,
            "to_dataset": right.get("source_dataset", ""),
            "to_column": right_column,
            "cardinality": str(operation.get("cardinality") or "needs_runtime_validation"),
            "join_type": str(operation.get("join_type") or "left"),
            "state": "user_confirmed",
            "confidence": float(operation.get("confidence") or 0.9),
            "approval": {"state": "approved", "approval_source": "data_model_generation_questionnaire"},
            "evidence_sources": [
                {
                    "type": "user_confirmed_relationship",
                    "reason": str(operation.get("reason") or "Confirmed through data model questionnaire."),
                }
            ],
        }
        draft.setdefault("relationships", []).append(relationship)
        return
    if op == "remove_relationship":
        relationship_id = str(operation.get("relationship_id") or "")
        draft["relationships"] = [
            rel for rel in draft.get("relationships", [])
            if rel.get("relationship_id") != relationship_id
        ]
        return
    if op == "approve_relationship":
        relationship_id = str(operation.get("relationship_id") or "")
        for rel in draft.get("relationships", []):
            if rel.get("relationship_id") != relationship_id:
                continue
            rel["state"] = "user_confirmed" if rel.get("state") != "proven_data_model" else rel["state"]
            rel["needs_confirmation"] = False
            rel["approval"] = {
                "state": "approved",
                "approval_source": "data_model_blocker_panel",
                "approved_at": _now(),
            }
            rel.setdefault("evidence_sources", []).append(
                {
                    "type": "user_confirmed_relationship",
                    "reason": str(operation.get("reason") or "Approved through data model blocker panel."),
                }
            )
            return
        raise ValueError(f"relationship not found: {relationship_id}")
    if op == "relabel_relationship":
        relationship_id = str(operation.get("relationship_id") or "")
        for rel in draft.get("relationships", []):
            if rel.get("relationship_id") != relationship_id:
                continue
            new_from = str(operation.get("from_column") or rel.get("from_column") or "")
            new_to = str(operation.get("to_column") or rel.get("to_column") or "")
            rel["from_column"] = new_from
            rel["to_column"] = new_to
            rel["relationship_id"] = _relationship_id(
                str(rel.get("from_table") or ""), new_from, str(rel.get("to_table") or ""), new_to
            )
            rel["state"] = "user_confirmed"
            rel["match_basis"] = "user_relabeled"
            rel["needs_confirmation"] = False
            rel["approval"] = {
                "state": "approved",
                "approval_source": "data_model_blocker_panel",
                "approved_at": _now(),
            }
            rel.setdefault("evidence_sources", []).append(
                {
                    "type": "user_relabeled_relationship",
                    "reason": str(operation.get("reason") or "User relabeled the join key via the blocker panel."),
                }
            )
            return
        raise ValueError(f"relationship not found: {relationship_id}")
    if op == "approve_for_execution":
        scope = str(operation.get("scope") or "")
        if scope == "currently_proven_relationships":
            for rel in draft.get("relationships", []):
                if rel.get("state") == "proven_data_model":
                    rel["approval"] = {"state": "approved", "approval_source": "risk_review"}
        return
    if op == "defer_unproven_items":
        draft.setdefault("deferred_items", []).append(operation)
        return
    raise ValueError(f"Unsupported data model operation: {op}")


def _find_table(draft: dict[str, Any], name: str) -> dict[str, Any] | None:
    target = _snake(name)
    return next((table for table in draft.get("tables", []) if table.get("name") == target), None)


def _require_table(draft: dict[str, Any], name: str) -> dict[str, Any]:
    table = _find_table(draft, name)
    if not table:
        raise ValueError(f"data model entity not found: {name}")
    return table


def _list_value(value: Any) -> list[str]:
    if isinstance(value, list):
        return [str(item) for item in value if str(item)]
    if isinstance(value, str):
        return [item.strip() for item in value.split(",") if item.strip()]
    return []


def _readiness(draft: dict[str, Any]) -> dict[str, Any]:
    tables = draft.get("tables", [])
    relationships = draft.get("relationships", [])
    entity_blockers = []
    for table in tables:
        name = table.get("name", "")
        if not table.get("primary_key"):
            entity_blockers.append(f"{name}: primary key needs review")
        if not table.get("grain", {}).get("description") or table.get("grain", {}).get("state") == "inferred_needs_review":
            entity_blockers.append(f"{name}: grain needs confirmation")
        if table.get("role") == "fact" and not table.get("temporal_anchor", {}).get("column"):
            entity_blockers.append(f"{name}: fact temporal anchor is missing")
        if table.get("table_pattern") in {"dimension_scd2", "needs_review"} and table.get("scd_type") == "needs_review":
            entity_blockers.append(f"{name}: SCD behavior needs review")
    relationship_blockers = [
        rel.get("relationship_id", "relationship")
        for rel in relationships
        if rel.get("approval", {}).get("state") not in {"approved", "approved_for_execution"}
    ]
    physical_blockers = [
        table.get("name", "")
        for table in tables
        if table.get("load_pattern") == "needs_review"
    ]
    scores = {
        "relationships": _score(len(relationships), len(relationship_blockers)),
        "physical_design": _score(len(tables), len(physical_blockers)),
        "medallion": _score(len(tables), sum(1 for table in tables if not table.get("medallion_layer"))),
        "ddl_dbt": _score(len(tables), len(entity_blockers) + len(physical_blockers)),
        "kpi_query": _score(len(tables) + len(relationships), len(entity_blockers) + len(relationship_blockers)),
    }
    overall = round(sum(item["score"] for item in scores.values()) / len(scores), 2)
    return {
        **scores,
        "overall": {"score": overall, "status": "ready_with_review" if overall >= 70 else "blocked"},
        "blockers": {
            "entities": entity_blockers,
            "relationships": relationship_blockers,
            "physical_design": physical_blockers,
        },
        "downstream_unlocks": {
            "relationship_contracts": scores["relationships"]["score"] >= 70,
            "source_to_target_plan": scores["relationships"]["score"] >= 70 and scores["kpi_query"]["score"] >= 70,
            "medallion_design": scores["medallion"]["score"] >= 70,
            "ddl_dbt_generation": overall >= 80,
        },
    }


def _dm_conversation_skills(panel: dict[str, Any]) -> list[dict[str, Any]]:
    """Skill routing for the orchestrating CLI agent during data-model creation,
    driven by the understanding score (the [[data-model-creation]] skill owns the
    overall design; grill until confident; clarify the few high-impact gaps)."""
    understanding = panel.get("understanding")
    raw = understanding.get("score") if isinstance(understanding, dict) else understanding
    score = int(raw or 0)
    return [
        {"name": "data-model-creation", "active": True,
         "why": "Design grain, keys, fact/dim, relationships, SCD; confirm joins instead of name-guessing; output ERD/SVG."},
        {"name": "grill-requirements", "active": True,
         "why": ("Interview the user to confirm entities, grain, and keys rather than infer from column names."
                 if score < 60 else
                 "Clarify-ambiguity mode: resolve the few high-impact modeling ambiguities (e.g. unconfirmed joins) before finalizing.")},
        {"name": "domain-model", "active": True,
         "why": "Align table/column terms with the KPI registry and CONTEXT.md."},
        {"name": "stakeholder-memory", "active": True,
         "why": "Save accepted grain/key/relationship decisions as durable workspace definitions."},
    ]


def _understanding_score(draft: dict[str, Any]) -> dict[str, Any]:
    """Compute the data-model understanding score (0-100) per data-model-creation skill.

    Score = mean of five dimension confidences, each 0-100:
      entities, grain, keys (confirmed not name-guessed), relationships
      (proven/confirmed vs candidate), temporal anchor + SCD decided.

    A relationship inferred only from a shared column name counts as a CANDIDATE
    (capped at <=50), not confirmed, until backed by identifier-role / profile /
    cardinality evidence or explicit user confirmation. The lowest-confidence
    dimension is named as the next decision. All evidence is workspace-derived.
    """
    tables = draft.get("tables", [])
    relationships = draft.get("relationships", [])

    entities = _ratio_score(len(tables), len(tables))
    grain = _ratio_score(len(tables), sum(1 for t in tables if _grain_confirmed(t)))
    keys = _ratio_score(len(tables), sum(1 for t in tables if _key_confirmed(t)))
    temporal = _ratio_score(len(tables), sum(1 for t in tables if _temporal_scd_decided(t)))

    if relationships:
        rel_confidences = [_relationship_confidence(rel) for rel in relationships]
        relationships_score = round(sum(rel_confidences) / len(rel_confidences), 2)
    else:
        relationships_score = 100.0

    dimensions = {
        "entities": _understanding_dimension(
            entities, "Entities confirmed", len(tables), len(tables)
        ),
        "grain": _understanding_dimension(
            grain, "Grain decided", sum(1 for t in tables if _grain_confirmed(t)), len(tables)
        ),
        "keys": _understanding_dimension(
            keys,
            "Keys confirmed (not name-guessed)",
            sum(1 for t in tables if _key_confirmed(t)),
            len(tables),
        ),
        "relationships": _understanding_dimension(
            relationships_score,
            "Relationships proven/confirmed",
            sum(1 for rel in relationships if _relationship_confidence(rel) > 50),
            len(relationships),
        ),
        "temporal_scd": _understanding_dimension(
            temporal,
            "Temporal anchor + SCD decided",
            sum(1 for t in tables if _temporal_scd_decided(t)),
            len(tables),
        ),
    }
    overall = round(sum(item["score"] for item in dimensions.values()) / len(dimensions), 2)
    lowest_key = min(dimensions, key=lambda key: dimensions[key]["score"])
    lowest = dimensions[lowest_key]
    next_decision = (
        f"{lowest['label']} is the lowest-confidence item ({lowest['score']}/100) -> resolve it next."
        if lowest["score"] < 100
        else "All understanding dimensions are confirmed."
    )
    return {
        "score": overall,
        "status": "high" if overall >= 80 else "moderate" if overall >= 50 else "low",
        "dimensions": dimensions,
        "lowest_dimension": lowest_key,
        "next_decision": next_decision,
    }


def _understanding_dimension(score: float, label: str, confirmed: int, total: int) -> dict[str, Any]:
    if score >= 100:
        icon = "[ok]"
    elif score >= 50:
        icon = "[~]"
    else:
        icon = "[x]"
    return {
        "label": label,
        "score": score,
        "icon": icon,
        "confirmed": confirmed,
        "total": total,
    }


def _ratio_score(total: int, confirmed: int) -> float:
    if total <= 0:
        return 100.0
    return round(100.0 * confirmed / total, 2)


def _grain_confirmed(table: dict[str, Any]) -> bool:
    grain = table.get("grain") or {}
    return bool(grain.get("description")) and grain.get("state") in {"user_confirmed", "user_added"}


def _key_confirmed(table: dict[str, Any]) -> bool:
    """A key counts as confirmed only when the user reviewed it, not when it was
    name-guessed by the profile heuristic (`approval.state == "draft"`)."""
    if not table.get("primary_key"):
        return False
    return table.get("approval", {}).get("state") in {"user_reviewed", "user_added", "approved"}


def _temporal_scd_decided(table: dict[str, Any]) -> bool:
    anchor = table.get("temporal_anchor") or {}
    role = table.get("role")
    if role == "fact":
        temporal_ok = anchor.get("state") == "user_confirmed" and bool(anchor.get("column"))
    else:
        temporal_ok = anchor.get("state") in {"user_confirmed", "not_found", "not_applicable"} or not anchor.get("column")
    scd = str(table.get("scd_type") or "")
    scd_ok = scd not in {"", "needs_review"}
    return temporal_ok and scd_ok


def _relationship_confidence(relationship: dict[str, Any]) -> float:
    """Confidence (0-100) for one relationship. A join inferred only from a shared
    column name is a candidate and is capped at 50 until confirmed or proven."""
    approval = relationship.get("approval", {}).get("state")
    if approval in {"approved", "approved_for_execution"}:
        return 100.0
    if relationship.get("needs_confirmation") or relationship.get("match_basis") == "name_only":
        return min(50.0, round(float(relationship.get("confidence") or 0.0) * 100.0, 2))
    return round(float(relationship.get("confidence") or 0.0) * 100.0, 2)


def _score(total: int, blockers: int) -> dict[str, Any]:
    if total <= 0:
        return {"score": 100.0, "status": "not_applicable", "blocker_count": 0}
    score = max(0.0, round(100.0 * (total - blockers) / total, 2))
    status = "ready" if blockers == 0 else "needs_review" if score >= 70 else "blocked"
    return {"score": score, "status": status, "blocker_count": blockers}


def _risk_blockers(draft: dict[str, Any]) -> list[dict[str, Any]]:
    readiness = draft.get("readiness") or _readiness(draft)
    blockers = []
    for category, values in readiness.get("blockers", {}).items():
        for value in values:
            blockers.append({"category": category, "blocker": value})
    return blockers[:10]


def _model_blockers(draft: dict[str, Any]) -> list[dict[str, Any]]:
    blockers: list[dict[str, Any]] = []
    for table in draft.get("tables", []):
        name = str(table.get("name") or "")
        if not name:
            continue
        if not table.get("primary_key"):
            blockers.append(_primary_key_blocker(table))
        grain = table.get("grain") or {}
        if grain.get("state") == "inferred_needs_review" or not grain.get("description"):
            blockers.append(_grain_blocker(table))
        anchor = table.get("temporal_anchor") or {}
        if table.get("role") == "fact" and not anchor.get("column"):
            blockers.append(_temporal_anchor_blocker(table))
        if table.get("table_pattern") == "dimension_scd2" and table.get("scd_type") == "needs_review":
            blockers.append(_scd_blocker(table))
    for relationship in draft.get("relationships", []):
        if relationship.get("approval", {}).get("state") in {"approved", "approved_for_execution"}:
            continue
        if _is_name_only_relationship(relationship):
            blockers.append(_name_only_relationship_blocker(relationship))
        else:
            blockers.append(_relationship_blocker(relationship))
    return sorted(blockers, key=lambda item: (-int(item.get("risk_rank", 0)), item.get("blocker_id", "")))


def _is_name_only_relationship(relationship: dict[str, Any]) -> bool:
    """A relationship inferred only from a shared column name (no identifier role,
    no profile/cardinality proof) that has not yet been confirmed or relabeled."""
    if not relationship.get("needs_confirmation"):
        return False
    return relationship.get("match_basis") == "name_only" or relationship.get("state") == "candidate_name_only"


def _primary_key_blocker(table: dict[str, Any]) -> dict[str, Any]:
    name = str(table.get("name") or "")
    candidates = _identifier_columns(table)
    options = []
    if candidates:
        options.append(
            {
                "option_id": "option_a",
                "label": f"Use `{candidates[0]}`",
                "business_summary": f"Set `{candidates[0]}` as the primary key for `{name}`.",
                "json_backed": True,
                "operations": [{"operation": "set_primary_key", "name": name, "columns": [candidates[0]]}],
            }
        )
    options.append(
        {
            "option_id": "option_b" if candidates else "option_a",
            "label": "Defer primary key",
            "business_summary": "Keep the entity blocked until a source dictionary or owner confirms the key.",
            "json_backed": True,
            "operations": [{"operation": "defer_unproven_items", "reason": f"{name}: primary key needs evidence"}],
        }
    )
    return {
        "version": SESSION_VERSION,
        "stage": "model_blocker",
        "blocker_id": f"primary_key__{name}",
        "status": "needs_user_answer",
        "blocker_type": "primary_key",
        "risk_rank": 95,
        "entity": name,
        "question": f"What is the primary key for `{name}`?",
        "recommended_option_id": "option_a",
        "recommended_answer": (
            f"Use `{candidates[0]}` as the primary key." if candidates else "Defer until key evidence is available."
        ),
        "why": "Primary keys drive grain, relationship contracts, deduplication, and executable DDL/dbt generation.",
        "options": options,
    }


def _grain_blocker(table: dict[str, Any]) -> dict[str, Any]:
    name = str(table.get("name") or "")
    pk = table.get("primary_key") or _identifier_columns(table)[:1]
    description = (
        f"one row per {name} record keyed by {', '.join(pk)}"
        if pk
        else f"one row per source row in {name}"
    )
    return {
        "version": SESSION_VERSION,
        "stage": "model_blocker",
        "blocker_id": f"grain__{name}",
        "status": "needs_user_answer",
        "blocker_type": "grain",
        "risk_rank": 90,
        "entity": name,
        "question": f"What grain should `{name}` use?",
        "recommended_option_id": "option_a",
        "recommended_answer": description,
        "why": "Grain must be explicit before facts, snapshots, relationships, and KPI aggregations are trusted.",
        "options": [
            {
                "option_id": "option_a",
                "label": "Accept inferred grain",
                "business_summary": description,
                "json_backed": True,
                "operations": [{"operation": "set_grain", "name": name, "description": description}],
            },
            {
                "option_id": "option_b",
                "label": "Defer grain",
                "business_summary": "Leave the table blocked until a source owner confirms the grain.",
                "json_backed": True,
                "operations": [{"operation": "defer_unproven_items", "reason": f"{name}: grain needs evidence"}],
            },
        ],
    }


def _temporal_anchor_blocker(table: dict[str, Any]) -> dict[str, Any]:
    name = str(table.get("name") or "")
    candidates = _role_columns(table, "timestamp")
    options = []
    if candidates:
        options.append(
            {
                "option_id": "option_a",
                "label": f"Use `{candidates[0]}`",
                "business_summary": f"Use `{candidates[0]}` as the temporal anchor for `{name}`.",
                "json_backed": True,
                "operations": [{"operation": "set_temporal_anchor", "name": name, "column": candidates[0]}],
            }
        )
    options.append(
        {
            "option_id": "option_b" if candidates else "option_a",
            "label": "No temporal anchor yet",
            "business_summary": "Keep time-based pruning and period logic blocked until a date column is confirmed.",
            "json_backed": True,
            "operations": [{"operation": "defer_unproven_items", "reason": f"{name}: temporal anchor missing"}],
        }
    )
    return {
        "version": SESSION_VERSION,
        "stage": "model_blocker",
        "blocker_id": f"temporal_anchor__{name}",
        "status": "needs_user_answer",
        "blocker_type": "temporal_anchor",
        "risk_rank": 82,
        "entity": name,
        "question": f"Which date/timestamp anchors `{name}` for partitions and point-in-time logic?",
        "recommended_option_id": "option_a",
        "recommended_answer": (
            f"Use `{candidates[0]}`." if candidates else "Defer until a date/timestamp column is identified."
        ),
        "why": "Facts and snapshots need a temporal anchor before partitioning, windows, and KPI periods are reliable.",
        "options": options,
    }


def _scd_blocker(table: dict[str, Any]) -> dict[str, Any]:
    name = str(table.get("name") or "")
    return {
        "version": SESSION_VERSION,
        "stage": "model_blocker",
        "blocker_id": f"scd_type__{name}",
        "status": "needs_user_answer",
        "blocker_type": "scd_type",
        "risk_rank": 78,
        "entity": name,
        "question": f"How should `{name}` track dimension changes?",
        "recommended_option_id": "option_a",
        "recommended_answer": "Use SCD Type 2 when historical attribute values matter; otherwise SCD Type 1.",
        "why": "The SCD choice changes keys, history columns, merge behavior, and point-in-time joins.",
        "options": [
            {
                "option_id": "option_a",
                "label": "SCD Type 2",
                "business_summary": "Keep full history with valid-from/valid-to/current-row control columns.",
                "json_backed": True,
                "operations": [{"operation": "set_scd_type", "name": name, "scd_type": "type_2"}],
            },
            {
                "option_id": "option_b",
                "label": "SCD Type 1",
                "business_summary": "Overwrite current attributes and do not preserve history.",
                "json_backed": True,
                "operations": [{"operation": "set_scd_type", "name": name, "scd_type": "type_1"}],
            },
            {
                "option_id": "option_c",
                "label": "Not applicable",
                "business_summary": "This table should not behave as a tracked dimension.",
                "json_backed": True,
                "operations": [{"operation": "set_scd_type", "name": name, "scd_type": "not_applicable"}],
            },
        ],
    }


def _relationship_blocker(relationship: dict[str, Any]) -> dict[str, Any]:
    relationship_id = str(relationship.get("relationship_id") or "")
    return {
        "version": SESSION_VERSION,
        "stage": "model_blocker",
        "blocker_id": f"relationship__{relationship_id}",
        "status": "needs_user_answer",
        "blocker_type": "relationship",
        "risk_rank": 88,
        "relationship_id": relationship_id,
        "question": (
            f"Can `{relationship.get('from_table')}.{relationship.get('from_column')}` join to "
            f"`{relationship.get('to_table')}.{relationship.get('to_column')}` for executable usage?"
        ),
        "recommended_option_id": "option_b",
        "recommended_answer": "Defer unless the relationship is proven by data model docs or explicitly owned.",
        "why": "Unproven relationships can duplicate facts, drop rows, or create invalid KPI SQL.",
        "options": [
            {
                "option_id": "option_a",
                "label": "Approve relationship",
                "business_summary": "Confirm this relationship for source-to-target and SQL generation.",
                "json_backed": True,
                "operations": [
                    {
                        "operation": "approve_relationship",
                        "relationship_id": relationship_id,
                        "reason": "Approved through data model blocker panel.",
                    }
                ],
            },
            {
                "option_id": "option_b",
                "label": "Keep blocked",
                "business_summary": "Require stronger model documentation, data dictionary evidence, or owner approval.",
                "json_backed": True,
                "operations": [
                    {
                        "operation": "defer_unproven_items",
                        "reason": f"{relationship_id}: relationship needs proof",
                    }
                ],
            },
        ],
    }


def _name_only_relationship_blocker(relationship: dict[str, Any]) -> dict[str, Any]:
    """Ask the user to confirm/reject/relabel a join inferred ONLY from a shared
    column name. It is never auto-accepted: confirmed joins join the model, rejected
    ones are dropped, relabeled ones are re-pointed to a user-supplied join key."""
    relationship_id = str(relationship.get("relationship_id") or "")
    from_table = str(relationship.get("from_table") or "")
    from_column = str(relationship.get("from_column") or "")
    to_table = str(relationship.get("to_table") or "")
    to_column = str(relationship.get("to_column") or "")
    return {
        "version": SESSION_VERSION,
        "stage": "model_blocker",
        "blocker_id": f"name_only_relationship__{relationship_id}",
        "status": "needs_user_answer",
        "blocker_type": "name_only_relationship",
        "risk_rank": 92,
        "relationship_id": relationship_id,
        "match_basis": "name_only",
        "question": (
            f"`{from_table}.{from_column}` and `{to_table}.{to_column}` share a column name only "
            "(no identifier role or profile/cardinality proof). Confirm, reject, or relabel this join?"
        ),
        "recommended_option_id": "option_b",
        "recommended_answer": (
            "Reject unless you can confirm the columns truly reference each other; a shared name is not proof."
        ),
        "why": (
            "A join inferred only from a matching column name can silently fan out or drop rows. "
            "It must be confirmed by the user (or by profile/cardinality evidence) before it is modeled."
        ),
        "options": [
            {
                "option_id": "option_a",
                "label": "Confirm the join",
                "business_summary": (
                    f"Confirm `{from_table}.{from_column}` joins `{to_table}.{to_column}` and add it to the model."
                ),
                "json_backed": True,
                "operations": [
                    {
                        "operation": "approve_relationship",
                        "relationship_id": relationship_id,
                        "reason": "User confirmed a name-inferred join via the data model blocker panel.",
                    }
                ],
            },
            {
                "option_id": "option_b",
                "label": "Reject the join",
                "business_summary": "Drop this name-inferred join; the shared column name is not enough evidence.",
                "json_backed": True,
                "operations": [
                    {"operation": "remove_relationship", "relationship_id": relationship_id}
                ],
            },
            {
                "option_id": "option_c",
                "label": "Relabel the join key",
                "business_summary": (
                    "Re-point this join to the correct columns "
                    "(supply from_column/to_column via --operation)."
                ),
                "json_backed": True,
                "operations": [
                    {
                        "operation": "relabel_relationship",
                        "relationship_id": relationship_id,
                        "from_column": from_column,
                        "to_column": to_column,
                    }
                ],
            },
        ],
    }


def _empty_model_blocker_panel(session: dict[str, Any], draft: dict[str, Any]) -> dict[str, Any]:
    return {
        "version": SESSION_VERSION,
        "workspace": session.get("workspace", ""),
        "stage": "model_blocker",
        "blocker_id": "none",
        "status": "no_blockers",
        "blocker_type": "none",
        "question": "",
        "recommended_option_id": "",
        "recommended_answer": "",
        "why": "No unresolved data-model blockers were found in the current draft.",
        "options": [],
        "readiness": draft.get("readiness", {}),
    }


def _render_model_blocker_markdown(panel: dict[str, Any]) -> str:
    lines = [
        f"# Data Model Blocker Panel: {panel.get('blocker_id', '')}",
        "",
        f"- Status: `{panel.get('status', '')}`",
        f"- Type: `{panel.get('blocker_type', '')}`",
        "",
        "## Question",
        "",
        str(panel.get("question", "")),
        "",
        "## Recommended Answer",
        "",
        str(panel.get("recommended_answer", "")),
        "",
        "## Why",
        "",
        str(panel.get("why", "")),
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
                str(option.get("business_summary", "")),
                "",
            ]
        )
        if option.get("json_backed"):
            lines.extend(["```json", json.dumps(option.get("operations", []), indent=2), "```", ""])
    return "\n".join(lines).rstrip() + "\n"


def _identifier_columns(table: dict[str, Any]) -> list[str]:
    return _role_columns(table, "identifier")


def _role_columns(table: dict[str, Any], role: str) -> list[str]:
    return [
        str(column.get("name") or "")
        for column in table.get("columns", [])
        if column.get("role") == role and column.get("name")
    ]


def _shared_key(left: dict[str, Any], right: dict[str, Any]) -> tuple[str, str, str] | None:
    """Pick the join key between two tables from workspace evidence, not a hardcoded list.

    Returns ``(left_column, right_column, match_basis)`` where ``match_basis`` is one of:
      * ``"identifier_role"`` — a shared column the model marks as an ``identifier`` (key)
        role in either table. This is evidence-backed and the join is trustworthy.
      * ``"name_only"`` — a shared column whose name merely ends in ``id``/``code`` with no
        identifier role on either side. This is a candidate inferred ONLY from a column name
        and must be confirmed by the user (or by profile/cardinality evidence) before use.

    All preference is derived from the model under construction, so it stays
    workspace-agnostic (no hardcoded domain entity/key names).
    """
    left_by_norm = {_norm(column.get("name", "")): column.get("name", "") for column in left.get("columns", [])}
    right_by_norm = {_norm(column.get("name", "")): column.get("name", "") for column in right.get("columns", [])}
    shared = sorted(set(left_by_norm).intersection(right_by_norm))
    identifier_norms = {
        _norm(name)
        for name in _role_columns(left, "identifier") + _role_columns(right, "identifier")
    }
    for key in shared:
        if key in identifier_norms:
            return left_by_norm[key], right_by_norm[key], "identifier_role"
    for key in shared:
        if key.endswith("id") or key.endswith("code"):
            return left_by_norm[key], right_by_norm[key], "name_only"
    return None


def _primary_key(columns: list[dict[str, Any]]) -> list[str]:
    for column in columns:
        if _norm(column.get("name", "")).endswith("id") and column.get("role") == "identifier":
            return [column["name"]]
    for column in columns:
        if _norm(column.get("name", "")).endswith("id"):
            return [column["name"]]
    return []


def _column_role(column: str, dtype: Any) -> str:
    norm = _norm(column)
    if norm.endswith("id") or norm in {"id", "uuid"}:
        return "identifier"
    if norm.endswith("date") or "timestamp" in norm or norm.endswith("time"):
        return "timestamp"
    if any(token in norm for token in ("amount", "cost", "price", "score", "quantity", "count")):
        return "measure"
    if str(dtype).lower() in {"bool", "boolean"} or norm.startswith(("is", "has")):
        return "flag"
    if any(token in norm for token in ("name", "description", "notes", "address")):
        return "text"
    return "dimension"


def _sql_type(dtype: Any) -> str:
    value = str(dtype).lower()
    if "int" in value:
        return "BIGINT"
    if any(token in value for token in ("float", "decimal", "double")):
        return "DECIMAL(18,4)"
    if "bool" in value:
        return "BOOLEAN"
    if "date" in value or "time" in value:
        return "TIMESTAMP"
    return "VARCHAR"


def _table_name(source: str) -> str:
    stem = Path(source.replace("\\", "/")).stem
    return _snake(stem)


def _relationship_id(left_table: str, left_col: str, right_table: str, right_col: str) -> str:
    return re.sub(r"[^a-z0-9_]+", "_", "__".join([left_table, left_col, right_table, right_col]).lower()).strip("_")


def _dataset_terms(source: str) -> set[str]:
    stem = Path(source.replace("\\", "/")).stem
    terms = {_norm(stem)}
    for token in re.split(r"[^A-Za-z0-9]+", stem):
        clean = _norm(token)
        if len(clean) > 2:
            terms.add(clean)
            if clean.endswith("s"):
                terms.add(clean[:-1])
    return terms


def _snake(value: str) -> str:
    value = re.sub(r"([a-z0-9])([A-Z])", r"\1_\2", str(value))
    return re.sub(r"[^a-zA-Z0-9]+", "_", value).strip("_").lower()


def _norm(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", "", str(value).lower())


def _repo_path(value: str, root: Path) -> str:
    if not value:
        return ""
    normalized = value.replace("\\", "/")
    marker = "workspaces/"
    if marker in normalized:
        return normalized[normalized.index(marker):]
    path = Path(value)
    if path.is_absolute():
        return _rel(path, root)
    return normalized


def _rel(path: Path, root: Path) -> str:
    try:
        return str(path.resolve().relative_to(root)).replace("\\", "/")
    except ValueError:
        return str(path)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def prepare_main(argv: list[str] | None = None) -> int:
    from core.onboarding.data_model.generation_cli import prepare_main as cli_prepare_main

    return cli_prepare_main(argv)


def apply_main(argv: list[str] | None = None) -> int:
    from core.onboarding.data_model.generation_cli import apply_main as cli_apply_main

    return cli_apply_main(argv)


def finalize_main(argv: list[str] | None = None) -> int:
    from core.onboarding.data_model.generation_cli import finalize_main as cli_finalize_main

    return cli_finalize_main(argv)


def prepare_blocker_main(argv: list[str] | None = None) -> int:
    from core.onboarding.data_model.generation_cli import prepare_blocker_main as cli_prepare_main

    return cli_prepare_main(argv)


def apply_blocker_main(argv: list[str] | None = None) -> int:
    from core.onboarding.data_model.generation_cli import apply_blocker_main as cli_apply_main

    return cli_apply_main(argv)
