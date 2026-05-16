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
TEXT_MODEL_SUFFIXES = {".md", ".txt", ".json", ".yaml", ".yml"}
IMAGE_MODEL_SUFFIXES = {".png", ".jpg", ".jpeg", ".svg"}
MODEL_NAME_TOKENS = ("model", "schema", "diagram", "erd", "dictionary", "contract")
PREFERRED_JOIN_KEYS = ("patientid", "encounterid", "transactionid", "claimid", "deptid", "departmentid", "providerid")


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
            "decisions": [],
            "draft_contract_path": "",
        }
        self._write_session(session)
        return self._write_panel(session, _route_panel(session, recommended))

    def apply_answer(self, *, answer: str, custom_note: str = "") -> DataModelGenerationResult:
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
        session["updated_at"] = now

        stage = str(panel.get("stage") or session.get("current_stage") or "")
        if stage == "route_selection":
            mode = str(option.get("value") or "generate_draft")
            draft = self._build_draft(mode=mode)
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
        tables = [_table_from_profile(profile) for profile in profiles.values()]
        relationships = _candidate_relationships(tables)
        relationships = _promote_from_text_models(relationships, text_models, self.repo_root)
        return {
            "version": 1,
            "generated_by": "data-model-generation",
            "workspace": _rel(self.workspace, self.repo_root),
            "mode": mode,
            "status": "draft_requires_final_preview_approval",
            "generated_at": _now(),
            "evidence_order": ["profile_index", "text_data_model_docs", "reviewed_diagram_sidecars", "user_approval"],
            "image_model_policy": {
                "state": "review_gated",
                "rule": "Images require a reviewed text/JSON/Markdown sidecar before relationships become executable.",
                "image_files": image_models,
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

    def _write_panel(self, session: dict[str, Any], panel: dict[str, Any]) -> DataModelGenerationResult:
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
        "next_step": "Apply the chosen path with apply-data-model-answer.",
    }


def _final_preview_panel(session: dict[str, Any], draft: dict[str, Any]) -> dict[str, Any]:
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
        "recommended_option_id": "option_a",
        "recommended_answer": "Finalize after review",
        "why": "The final write updates user-facing docs and relationship contracts only after explicit approval.",
        "draft_contract_path": session.get("draft_contract_path", ""),
        "summary": draft.get("summary", {}),
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


def _table_from_profile(profile: dict[str, Any]) -> dict[str, Any]:
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
    return {
        "name": name,
        "source_dataset": source,
        "description": f"Source-backed entity for `{Path(source).name}`.",
        "primary_key": primary_key,
        "columns": columns,
        "approval": {"state": "draft"},
    }


def _candidate_relationships(tables: list[dict[str, Any]]) -> list[dict[str, Any]]:
    relationships = []
    for idx, left in enumerate(tables):
        for right in tables[idx + 1:]:
            match = _shared_key(left, right)
            if not match:
                continue
            left_col, right_col = match
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
                    "state": "candidate_profile",
                    "confidence": 0.62,
                    "approval": {"state": "needs_review"},
                    "evidence_sources": [
                        {
                            "type": "profile_shared_key",
                            "left_dataset": left["source_dataset"],
                            "right_dataset": right["source_dataset"],
                            "normalized_key": _norm(left_col),
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
    for table in contract.get("tables", []):
        lines.extend([
            f"## {table.get('name', '')}",
            "",
            f"> {table.get('description', '')}",
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


def _shared_key(left: dict[str, Any], right: dict[str, Any]) -> tuple[str, str] | None:
    left_by_norm = {_norm(column.get("name", "")): column.get("name", "") for column in left.get("columns", [])}
    right_by_norm = {_norm(column.get("name", "")): column.get("name", "") for column in right.get("columns", [])}
    for key in PREFERRED_JOIN_KEYS:
        if key in left_by_norm and key in right_by_norm:
            return left_by_norm[key], right_by_norm[key]
    for key in sorted(set(left_by_norm).intersection(right_by_norm)):
        if key.endswith("id") or key.endswith("code"):
            return left_by_norm[key], right_by_norm[key]
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
    from core.onboarding.data_model_generation_cli import prepare_main as cli_prepare_main

    return cli_prepare_main(argv)


def apply_main(argv: list[str] | None = None) -> int:
    from core.onboarding.data_model_generation_cli import apply_main as cli_apply_main

    return cli_apply_main(argv)


def finalize_main(argv: list[str] | None = None) -> int:
    from core.onboarding.data_model_generation_cli import finalize_main as cli_finalize_main

    return cli_finalize_main(argv)
