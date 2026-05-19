"""Panel-driven workflow for external source intake decisions."""
from __future__ import annotations

import json
import re
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from core.onboarding.external_source_discovery import ExternalSourceDiscoverer
from core.storage.workspace_layout import WorkspaceLayout


SESSION_VERSION = 1


@dataclass(frozen=True)
class ExternalSourceIntakeResult:
    workspace: str
    session_path: str
    current_json_path: str
    current_markdown_path: str
    stage: str
    status: str
    next_step: str

    def summary(self) -> dict[str, Any]:
        return asdict(self)


class ExternalSourceIntakeWorkflow:
    def __init__(
        self,
        repo_root: str | Path,
        *,
        external_root: str | Path,
        workspace: str | Path | None = None,
        proposed_workspace: str | Path | None = None,
    ) -> None:
        self.repo_root = Path(repo_root).resolve()
        self.external_root = Path(external_root).expanduser().resolve()
        self.workspace_arg = Path(workspace) if workspace else None
        derived_workspace = f"workspaces/{_safe_slug(self.external_root.name)}"
        normalized_proposed = _normalize_workspace(str(proposed_workspace or derived_workspace))
        proposed_path = (self.repo_root / normalized_proposed).resolve()
        proposed_session = (
            proposed_path
            / "interns"
            / "generated"
            / "requirements"
            / "external_source_intake_session.json"
        )
        # Some launchers pass the currently active workspace as --proposed-workspace.
        # If it already exists, keep it only as an attach-existing candidate and
        # derive the fresh workspace from the external source folder name.
        if proposed_workspace and proposed_path.exists() and not proposed_session.exists() and not self.workspace_arg:
            self.attach_existing_candidate = normalized_proposed
            self.proposed_workspace = _normalize_workspace(derived_workspace)
        else:
            self.attach_existing_candidate = ""
            self.proposed_workspace = normalized_proposed

    def prepare(self) -> ExternalSourceIntakeResult:
        if not self.external_root.exists() or not self.external_root.is_dir():
            raise FileNotFoundError(f"external root not found: {self.external_root}")
        preference = _read_json(_team_memory_path(self.repo_root)).get("preferences", {})
        workspace = self._initial_workspace(preference)
        layout = WorkspaceLayout(project_root=(self.repo_root / workspace).resolve())
        layout.ensure_runtime_dirs()
        session = {
            "version": SESSION_VERSION,
            "workflow": "external_source_intake",
            "status": "awaiting_user_answer",
            "current_stage": "route_selection",
            "workspace": workspace,
            "proposed_workspace": self.proposed_workspace,
            "attach_existing_candidate": self.attach_existing_candidate,
            "external_root": str(self.external_root),
            "created_at": _now(),
            "updated_at": _now(),
            "team_preference": preference,
            "decisions": [],
            "preferences": {},
            "discovery": {},
        }
        self._write_session(layout, session)
        return self._write_panel(layout, session, _route_panel(session))

    def apply_answer(
        self,
        *,
        answer: str,
        existing_workspace: str = "",
        workspace_name: str = "",
        change_reason: str = "",
        save_as_default: bool = False,
        max_files: int = 2000,
        max_seconds: float = 30.0,
    ) -> ExternalSourceIntakeResult:
        layout, session = self._load_active_session()
        panel = _read_json(self._current_json_path(layout))
        option = _resolve_option(panel, answer)
        stage = str(panel.get("stage") or session.get("current_stage") or "")
        session.setdefault("decisions", []).append(
            {
                "stage": stage,
                "answer": answer,
                "selected_option_id": option.get("option_id", ""),
                "selected_value": option.get("value", ""),
                "change_reason": change_reason,
                "save_as_default": save_as_default,
                "answered_at": _now(),
            }
        )
        session["updated_at"] = _now()

        if stage == "route_selection":
            value = str(option.get("value") or "")
            if value == "attach_existing" and not existing_workspace:
                existing_workspace = str(session.get("attach_existing_candidate") or "")
            default_route = session.get("team_preference", {}).get("default_route", "")
            if default_route and default_route != value and not change_reason:
                session["pending_route"] = value
                session["pending_existing_workspace"] = existing_workspace
                session["pending_workspace_name"] = workspace_name
                session["status"] = "awaiting_change_reason"
                session["current_stage"] = "route_change_reason"
                self._write_session(layout, session)
                return self._write_panel(layout, session, _change_reason_panel(session, value))
            return self._apply_route(
                session,
                value,
                existing_workspace=existing_workspace,
                workspace_name=workspace_name,
                change_reason=change_reason,
                save_as_default=save_as_default,
                max_files=max_files,
                max_seconds=max_seconds,
            )

        if stage == "route_change_reason":
            value = str(session.get("pending_route") or option.get("value") or "")
            if value == "attach_existing" and not existing_workspace:
                existing_workspace = str(session.get("attach_existing_candidate") or "")
            return self._apply_route(
                session,
                value,
                existing_workspace=existing_workspace or str(session.get("pending_existing_workspace") or ""),
                workspace_name=workspace_name or str(session.get("pending_workspace_name") or ""),
                change_reason=change_reason or option.get("description", ""),
                save_as_default=save_as_default,
                max_files=max_files,
                max_seconds=max_seconds,
            )

        if stage == "outcome_selection":
            session["preferences"]["intended_outcome"] = option.get("value", "")
            session["current_stage"] = "source_group_selection"
            session["status"] = "awaiting_user_answer"
            self._write_workspace_memory(session)
            self._write_session(layout, session)
            return self._write_panel(layout, session, _source_group_panel(session))

        if stage == "source_group_selection":
            session["preferences"]["source_group_policy"] = option.get("value", "")
            session["selected_groups"] = _selected_groups_for_option(session, str(option.get("value") or ""))
            session["current_stage"] = "approval_gate"
            session["status"] = "awaiting_source_selection_approval"
            self._write_workspace_memory(session)
            self._write_session(layout, session)
            return self._write_panel(layout, session, _approval_panel(session))

        if stage == "approval_gate":
            session["preferences"]["approval_decision"] = option.get("value", "")
            session["status"] = "ready_for_source_selection_review"
            session["current_stage"] = "terminal"
            self._write_workspace_memory(session)
            self._write_session(layout, session)
            return self._write_panel(layout, session, _terminal_panel(session))

        raise ValueError(f"No active external source intake stage: {stage}")

    def _apply_route(
        self,
        session: dict[str, Any],
        route: str,
        *,
        existing_workspace: str,
        workspace_name: str,
        change_reason: str,
        save_as_default: bool,
        max_files: int,
        max_seconds: float,
    ) -> ExternalSourceIntakeResult:
        workspace = _workspace_for_route(
            route,
            proposed=session["proposed_workspace"],
            existing_workspace=existing_workspace,
            workspace_name=workspace_name,
        )
        layout = WorkspaceLayout(project_root=(self.repo_root / workspace).resolve())
        layout.ensure_runtime_dirs()
        session["workspace"] = workspace
        session["route"] = route
        session["change_reason"] = change_reason
        session["status"] = "discovering_external_sources"
        session["current_stage"] = "metadata_discovery"
        discovery = ExternalSourceDiscoverer(
            self.repo_root,
            workspace,
            self.external_root,
            max_files=max_files,
            max_seconds=max_seconds,
        ).run()
        discovery_summary = discovery.summary()
        discovery_summary["_discovery_abs_path"] = str(self.repo_root / discovery.discovery_path)
        session["discovery"] = discovery_summary
        session["current_stage"] = "outcome_selection"
        session["status"] = "awaiting_user_answer"
        self._write_team_memory(session, save_as_default=save_as_default)
        self._write_workspace_memory(session)
        self._write_session(layout, session)
        return self._write_panel(layout, session, _outcome_panel(session))

    def _initial_workspace(self, preference: dict[str, Any]) -> str:
        if self.workspace_arg:
            return _normalize_workspace(str(self.workspace_arg))
        if preference.get("default_route") == "attach_existing" and preference.get("default_workspace"):
            return _normalize_workspace(str(preference["default_workspace"]))
        return self.proposed_workspace

    def _load_active_session(self) -> tuple[WorkspaceLayout, dict[str, Any]]:
        candidate_workspaces = []
        if self.workspace_arg:
            candidate_workspaces.append(_normalize_workspace(str(self.workspace_arg)))
        candidate_workspaces.append(self.proposed_workspace)
        memory = _read_json(_team_memory_path(self.repo_root)).get("preferences", {})
        if memory.get("default_workspace"):
            candidate_workspaces.append(_normalize_workspace(str(memory["default_workspace"])))
        for workspace in dict.fromkeys(candidate_workspaces):
            layout = WorkspaceLayout(project_root=(self.repo_root / workspace).resolve())
            path = self._session_path(layout)
            if path.exists():
                return layout, _read_json(path)
        raise FileNotFoundError("External source intake session not found. Run prepare-external-source-intake first.")

    def _write_session(self, layout: WorkspaceLayout, session: dict[str, Any]) -> Path:
        path = self._session_path(layout)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(session, indent=2, default=str) + "\n", encoding="utf-8")
        return path

    def _write_panel(
        self,
        layout: WorkspaceLayout,
        session: dict[str, Any],
        panel: dict[str, Any],
    ) -> ExternalSourceIntakeResult:
        current_json = self._current_json_path(layout)
        current_md = self._current_markdown_path(layout)
        current_json.parent.mkdir(parents=True, exist_ok=True)
        current_json.write_text(json.dumps(panel, indent=2, default=str) + "\n", encoding="utf-8")
        current_md.write_text(_render_panel_markdown(panel), encoding="utf-8")
        return ExternalSourceIntakeResult(
            workspace=session["workspace"],
            session_path=_rel(self._session_path(layout), self.repo_root),
            current_json_path=_rel(current_json, self.repo_root),
            current_markdown_path=_rel(current_md, self.repo_root),
            stage=str(panel.get("stage") or session.get("current_stage") or ""),
            status=str(session.get("status") or ""),
            next_step=str(panel.get("next_step") or ""),
        )

    def _write_team_memory(self, session: dict[str, Any], *, save_as_default: bool) -> Path:
        path = _team_memory_path(self.repo_root)
        path.parent.mkdir(parents=True, exist_ok=True)
        existing = _read_json(path)
        preferences = dict(existing.get("preferences") or {})
        preferences.setdefault("default_route", session.get("route", "create_new"))
        preferences.setdefault("default_discovery_policy", "metadata_only_then_review")
        preferences.setdefault("approval_policy", "review_source_selection_before_register_or_profile")
        if save_as_default or not existing.get("preferences"):
            preferences["default_route"] = session.get("route", "create_new")
            preferences["default_workspace"] = session.get("workspace", "")
            preferences["last_change_reason"] = session.get("change_reason", "")
        payload = {
            "version": 1,
            "updated_at": _now(),
            "preferences": preferences,
            "recent_decisions": (existing.get("recent_decisions") or [])[-20:]
            + [
                {
                    "external_root": session.get("external_root", ""),
                    "route": session.get("route", ""),
                    "workspace": session.get("workspace", ""),
                    "change_reason": session.get("change_reason", ""),
                    "saved_as_default": save_as_default,
                    "recorded_at": _now(),
                }
            ],
        }
        path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
        return path

    def _write_workspace_memory(self, session: dict[str, Any]) -> Path:
        layout = WorkspaceLayout(project_root=(self.repo_root / session["workspace"]).resolve())
        path = layout.memory_dir / "external_source_intake_memory.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "version": 1,
            "workspace": session.get("workspace", ""),
            "external_root": session.get("external_root", ""),
            "updated_at": _now(),
            "route": session.get("route", ""),
            "preferences": session.get("preferences", {}),
            "decisions": session.get("decisions", []),
            "discovery": session.get("discovery", {}),
            "selected_groups": session.get("selected_groups", []),
        }
        path.write_text(json.dumps(payload, indent=2, default=str) + "\n", encoding="utf-8")
        return path

    def _session_path(self, layout: WorkspaceLayout) -> Path:
        return layout.requirements_dir / "external_source_intake_session.json"

    def _current_json_path(self, layout: WorkspaceLayout) -> Path:
        return layout.reports_dir / "external_source_intake" / "current.json"

    def _current_markdown_path(self, layout: WorkspaceLayout) -> Path:
        return layout.reports_dir / "external_source_intake" / "current.md"


def _route_panel(session: dict[str, Any]) -> dict[str, Any]:
    preference = session.get("team_preference") or {}
    recommended = "option_b" if preference.get("default_route") == "attach_existing" else "option_a"
    attach_label = (
        f"Attach to `{session['attach_existing_candidate']}`"
        if session.get("attach_existing_candidate")
        else "Attach to existing workspace"
    )
    attach_description = (
        "Use this existing workspace candidate, or pass --existing-workspace for a different project."
        if session.get("attach_existing_candidate")
        else "Use --existing-workspace to attach this external source to an existing project."
    )
    return {
        "version": SESSION_VERSION,
        "workflow": "external_source_intake",
        "workspace": session["workspace"],
        "external_root": session["external_root"],
        "stage": "route_selection",
        "question": "This path is outside the repo workspace. How should it be handled?",
        "options": [
            {
                "option_id": "option_a",
                "label": f"Create `{session['proposed_workspace']}`",
                "value": "create_new",
                "description": "Create a fresh repo workspace, save all generated artifacts there, and run metadata-only discovery.",
            },
            {
                "option_id": "option_b",
                "label": attach_label,
                "value": "attach_existing",
                "description": attach_description,
            },
            {
                "option_id": "option_c",
                "label": "Create custom workspace name",
                "value": "create_custom",
                "description": "Use --workspace-name to create a different workspaces/<name> folder.",
            },
        ],
        "recommended_option_id": recommended,
        "recommended_answer": "Use saved default if available; otherwise create a fresh workspace and run metadata-only discovery.",
        "why": "External roots should not receive generated interns/ artifacts. The route decision must be saved before discovery.",
        "team_preference": preference,
        "next_step": "Run apply-external-source-intake with an option; use --existing-workspace or --workspace-name if needed.",
    }


def _change_reason_panel(session: dict[str, Any], route: str) -> dict[str, Any]:
    return {
        "version": SESSION_VERSION,
        "workflow": "external_source_intake",
        "workspace": session["workspace"],
        "external_root": session["external_root"],
        "stage": "route_change_reason",
        "question": "You changed the saved external-source routing default. Why?",
        "options": [
            {
                "option_id": "option_a",
                "label": "One-off exception",
                "value": route,
                "description": "Continue this way but keep the existing default.",
            },
            {
                "option_id": "option_b",
                "label": "This data belongs to project",
                "value": route,
                "description": "Route differs because this external source belongs with a specific existing project.",
            },
            {
                "option_id": "option_c",
                "label": "New default behavior",
                "value": route,
                "description": "Continue and save this route as the new default with --save-as-default.",
            },
        ],
        "recommended_option_id": "option_a",
        "recommended_answer": "Treat it as a one-off unless you explicitly want to update team preference.",
        "why": "One exception should not silently rewrite long-term routing memory.",
        "next_step": "Apply an option, optionally with --change-reason and --save-as-default.",
    }


def _outcome_panel(session: dict[str, Any]) -> dict[str, Any]:
    return {
        "version": SESSION_VERSION,
        "workflow": "external_source_intake",
        "workspace": session["workspace"],
        "external_root": session["external_root"],
        "stage": "outcome_selection",
        "question": "What is the intended outcome for this external source?",
        "options": [
            {
                "option_id": "option_a",
                "label": "Catalog/wiki only",
                "value": "catalog_wiki",
                "description": "Keep this as searchable requirements/catalog context without registering datasets yet.",
            },
            {
                "option_id": "option_b",
                "label": "Profile selected sources",
                "value": "profile_selected",
                "description": "Register approved sources and run resource-aware profiling before any transforms.",
            },
            {
                "option_id": "option_c",
                "label": "Medallion/ETL planning",
                "value": "medallion_etl",
                "description": "Use docs plus profiles to design bronze/silver/gold or ETL strategy.",
            },
        ],
        "recommended_option_id": "option_c",
        "recommended_answer": "Medallion/ETL planning for raw CMS-style folders with dictionaries and many CSVs.",
        "why": "Intent controls which source groups matter and how much processing is justified.",
        "discovery": session.get("discovery", {}),
        "next_step": "Apply an outcome to choose source-group scope.",
    }


def _source_group_panel(session: dict[str, Any]) -> dict[str, Any]:
    groups = _discovery_groups(session)
    top_groups = groups[:12]
    return {
        "version": SESSION_VERSION,
        "workflow": "external_source_intake",
        "workspace": session["workspace"],
        "external_root": session["external_root"],
        "stage": "source_group_selection",
        "question": "Which discovered source groups should be in scope first?",
        "options": [
            {
                "option_id": "option_a",
                "label": "Recommended data groups",
                "value": "recommended_data_groups",
                "description": "Select raw/delta/database groups and exclude specs/system/logs by default.",
            },
            {
                "option_id": "option_b",
                "label": "All discovered candidates",
                "value": "all_candidates",
                "description": "Keep every generated source candidate review-gated.",
            },
            {
                "option_id": "option_c",
                "label": "Docs/specs only",
                "value": "docs_only",
                "description": "Use discovered documents as context before touching datasets.",
            },
        ],
        "recommended_option_id": "option_a",
        "recommended_answer": "Start with recommended data groups; keep the source selection review-gated.",
        "why": "This avoids profiling logs/specs while preserving raw CMS data and table roots for review.",
        "top_groups": top_groups,
        "next_step": "Apply a group policy to reach the approval gate.",
    }


def _approval_panel(session: dict[str, Any]) -> dict[str, Any]:
    return {
        "version": SESSION_VERSION,
        "workflow": "external_source_intake",
        "workspace": session["workspace"],
        "external_root": session["external_root"],
        "stage": "approval_gate",
        "question": "Review the generated source selection before registering or profiling selected sources.",
        "options": [
            {
                "option_id": "option_a",
                "label": "Review only",
                "value": "review_only",
                "description": "Stop here after writing discovery and source selection artifacts.",
            },
            {
                "option_id": "option_b",
                "label": "Approve later",
                "value": "approve_later",
                "description": "Keep the draft selection and return later to finalize selected sources.",
            },
        ],
        "recommended_option_id": "option_a",
        "recommended_answer": "Review the generated source selection before any registration/profile work.",
        "why": "External source registration and profiling can be expensive or noisy; discovery is safe, but processing needs approval.",
        "selected_groups": session.get("selected_groups", []),
        "draft_selection_path": session.get("discovery", {}).get("draft_selection_path", ""),
        "next_step": "Review docs/source_selection.generated.json, then finalize or edit selection.",
    }


def _terminal_panel(session: dict[str, Any]) -> dict[str, Any]:
    return {
        "version": SESSION_VERSION,
        "workflow": "external_source_intake",
        "workspace": session["workspace"],
        "external_root": session["external_root"],
        "stage": "terminal",
        "question": "External source intake routing is complete.",
        "options": [],
        "recommended_option_id": "",
        "recommended_answer": "Review the generated discovery report and source selection draft.",
        "why": "Registration/profile work remains approval-gated.",
        "next_step": "Review the generated artifacts before source-catalog finalization or profiling.",
    }


def _discovery_groups(session: dict[str, Any]) -> list[dict[str, Any]]:
    discovery_path = session.get("discovery", {}).get("_discovery_abs_path") or session.get("discovery", {}).get("discovery_path", "")
    if not discovery_path:
        return []
    path = Path(discovery_path)
    if not path.is_absolute():
        path = Path.cwd() / path
    data = _read_json(path)
    return list(data.get("groups") or [])


def _selected_groups_for_option(session: dict[str, Any], value: str) -> list[str]:
    groups = _discovery_groups(session)
    if value == "docs_only":
        return [group["group"] for group in groups if group.get("strategy") == "requirements_context_only" or group.get("document_count", 0)]
    if value == "all_candidates":
        return [group["group"] for group in groups if group.get("strategy") != "exclude_system_state"]
    return [
        group["group"]
        for group in groups
        if group.get("strategy")
        in {
            "medallion_from_raw_csv_with_docs",
            "medallion_from_raw_csv_needs_docs",
            "delta_external_table_inspection",
            "database_metadata_first",
            "metadata_first_dataset_profile",
        }
    ]


def _workspace_for_route(
    route: str,
    *,
    proposed: str,
    existing_workspace: str,
    workspace_name: str,
) -> str:
    if route == "attach_existing":
        if not existing_workspace:
            raise ValueError("--existing-workspace is required when attaching to an existing workspace")
        return _normalize_workspace(existing_workspace)
    if route == "create_custom":
        if not workspace_name:
            raise ValueError("--workspace-name is required for a custom new workspace")
        return _normalize_workspace(workspace_name)
    return _normalize_workspace(proposed)


def _resolve_option(panel: dict[str, Any], answer: str) -> dict[str, Any]:
    normalized = answer.strip().lower().replace(" ", "_")
    options = panel.get("options") or []
    if normalized in {"recommended", "default"}:
        recommended = str(panel.get("recommended_option_id") or "")
        return _resolve_option(panel, recommended)
    for option in options:
        if normalized in {
            str(option.get("option_id", "")).lower(),
            str(option.get("label", "")).lower().replace(" ", "_"),
            str(option.get("value", "")).lower(),
        }:
            return option
    raise ValueError(f"answer did not match any option: {answer}")


def _render_panel_markdown(panel: dict[str, Any]) -> str:
    lines = [
        f"# External Source Intake: {panel.get('stage', '')}",
        "",
        f"- Workspace: `{panel.get('workspace', '')}`",
        f"- External root: `{panel.get('external_root', '')}`",
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
        ]
    )
    return "\n".join(lines).rstrip() + "\n"


def _team_memory_path(repo_root: Path) -> Path:
    return repo_root / "state" / "team_memory" / "external_source_intake_preferences.json"


def _read_json(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return {}
    return data if isinstance(data, dict) else {}


def _normalize_workspace(value: str) -> str:
    normalized = value.replace("\\", "/").strip().strip('"').strip("'").strip("/")
    if not normalized.startswith("workspaces/"):
        normalized = f"workspaces/{normalized}"
    return normalized


def _safe_slug(value: str) -> str:
    slug = re.sub(r"[^A-Za-z0-9_.-]+", "-", value).strip("-").lower()
    return slug or "external-source"


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _rel(path: Path, root: Path) -> str:
    try:
        return str(path.resolve().relative_to(root.resolve())).replace("\\", "/")
    except ValueError:
        return str(path).replace("\\", "/")
