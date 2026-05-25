"""Workspace-level governed workflow checkpoint orchestration."""
from __future__ import annotations

import argparse
import json
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from core.onboarding.data_model.generation_workflow import DataModelGenerationWorkflow
from core.onboarding.kpi.blocker_workflow import apply_kpi_panel_answer, prepare_kpi_blocker_panel
from core.onboarding.kpi.generation_workflow import KPIGenerationWorkflow
from core.onboarding.memory.wiki_memory import WorkspaceWikiMemoryBuilder
from core.onboarding.workspace.onboarding import WorkspaceOnboarder
from core.onboarding.workspace.validation import WorkspaceArtifactValidator
from core.presentation.exports import WorkspacePresentationExporter
from core.storage.workspace_layout import WorkspaceLayout
from tools.list_workspace_files import list_workspace_files


WORKFLOW_VERSION = 1
MODES = {"plan", "local-safe", "autopilot"}


@dataclass(frozen=True)
class WorkspaceWorkflowResult:
    workspace: str
    current_json_path: str
    current_markdown_path: str
    mode: str
    stage: str
    status: str
    action_count: int
    warning_count: int

    def summary(self) -> dict[str, Any]:
        return asdict(self)


class WorkspaceWorkflowOrchestrator:
    def __init__(
        self,
        repo_root: str | Path,
        workspace: str | Path,
        *,
        domain: str = "healthcare",
        mode: str = "local-safe",
    ) -> None:
        if mode not in MODES:
            raise ValueError(f"unsupported workflow mode: {mode}")
        self.repo_root = Path(repo_root).resolve()
        self.workspace = (self.repo_root / workspace).resolve()
        self.workspace_rel = _rel(self.workspace, self.repo_root)
        self.layout = WorkspaceLayout(project_root=self.workspace)
        self.domain = domain
        self.mode = mode
        self.actions: list[dict[str, Any]] = []
        self.warnings: list[str] = []

    def prepare(self) -> WorkspaceWorkflowResult:
        self._validate_workspace()
        self.layout.ensure_runtime_dirs()
        listing = list_workspace_files(self.repo_root, self.workspace_rel).to_dict()
        if self.mode in {"local-safe", "autopilot"}:
            self._ensure_onboarded()
            self._prepare_kpi_generation()
            self._prepare_data_model_generation()
            if self.mode == "autopilot":
                self._run_bounded_autopilot()
            self._prepare_kpi_blockers()
            self._prepare_data_model_blockers()
            validation = self._validate_artifacts()
            presentation = self._export_presentation()
            wiki_memory = self._prepare_wiki_memory()
        else:
            validation = self._validation_summary_if_available()
            presentation = {}
            wiki_memory = self._wiki_memory_summary_if_available()
        panel = self._panel(
            listing=listing,
            validation=validation,
            presentation=presentation,
            wiki_memory=wiki_memory,
        )
        current_json = self.layout.reports_dir / "workflow" / "current.json"
        current_md = self.layout.reports_dir / "workflow" / "current.md"
        current_json.parent.mkdir(parents=True, exist_ok=True)
        current_json.write_text(json.dumps(panel, indent=2) + "\n", encoding="utf-8")
        current_md.write_text(_render_workflow_markdown(panel), encoding="utf-8")
        return WorkspaceWorkflowResult(
            workspace=self.workspace_rel,
            current_json_path=_rel(current_json, self.repo_root),
            current_markdown_path=_rel(current_md, self.repo_root),
            mode=self.mode,
            stage=str(panel.get("stage", "")),
            status=str(panel.get("status", "")),
            action_count=len(self.actions),
            warning_count=len(self.warnings),
        )

    def _ensure_onboarded(self) -> None:
        required = [
            self.layout.requirements_dir / "input_inventory.json",
            self.layout.contracts_dir / "kpi_registry.json",
            self.layout.contracts_dir / "domain_model.json",
            self.layout.profiles_dir / "profile_index.json",
        ]
        if all(path.exists() for path in required):
            self._record("onboarding", "skipped", "Required onboarding artifacts already exist.")
            return
        result = WorkspaceOnboarder(self.repo_root, self.workspace_rel).run()
        self._record("onboarding", "ran", "Generated missing onboarding artifacts.", result.summary())

    def _prepare_kpi_generation(self) -> None:
        try:
            result = KPIGenerationWorkflow(self.repo_root, self.workspace_rel).prepare()
            self._record("kpi_generation", "prepared", "Prepared KPI generation route panel.", result.summary())
        except Exception as exc:
            self._warn("kpi_generation", exc)

    def _prepare_data_model_generation(self) -> None:
        try:
            result = DataModelGenerationWorkflow(self.repo_root, self.workspace_rel).prepare()
            self._record("data_model_generation", "prepared", "Prepared data-model generation route panel.", result.summary())
        except Exception as exc:
            self._warn("data_model_generation", exc)

    def _run_bounded_autopilot(self) -> None:
        self._autopilot_kpi_generation()
        self._autopilot_data_model_generation()

    def _autopilot_kpi_generation(self) -> None:
        workflow = KPIGenerationWorkflow(self.repo_root, self.workspace_rel)
        for _ in range(4):
            panel = _load_json(self.layout.reports_dir / "kpi_generation" / "current.json")
            stage = str(panel.get("stage") or "")
            if stage in {"", "final_preview", "usual_workflow_selected"}:
                return
            answer = str(panel.get("recommended_option_id") or "")
            if not answer:
                return
            if not _safe_kpi_generation_stage(stage):
                return
            try:
                result = workflow.apply_answer(answer=answer, custom_note="workspace workflow autopilot")
                self._record("kpi_generation_autopilot", "applied", f"Applied recommended {answer} at {stage}.", result.summary())
            except Exception as exc:
                self._warn("kpi_generation_autopilot", exc)
                return

    def _autopilot_data_model_generation(self) -> None:
        workflow = DataModelGenerationWorkflow(self.repo_root, self.workspace_rel)
        for _ in range(3):
            panel = _load_json(self.layout.reports_dir / "data_model_generation" / "current.json")
            stage = str(panel.get("stage") or "")
            if stage in {"", "risk_review", "final_preview"}:
                return
            answer = str(panel.get("recommended_option_id") or "")
            if not answer or not _safe_data_model_stage(stage):
                return
            try:
                result = workflow.apply_answer(answer=answer, custom_note="workspace workflow autopilot")
                self._record("data_model_generation_autopilot", "applied", f"Applied recommended {answer} at {stage}.", result.summary())
            except Exception as exc:
                self._warn("data_model_generation_autopilot", exc)
                return
        self._autopilot_data_model_blockers()

    def _autopilot_data_model_blockers(self) -> None:
        workflow = DataModelGenerationWorkflow(self.repo_root, self.workspace_rel)
        try:
            panel_result = workflow.prepare_blocker_panel()
            self._record("data_model_blocker", "prepared", "Prepared data-model blocker panel.", panel_result.summary())
            panel = _load_json(self.repo_root / panel_result.current_json_path)
            if panel.get("status") != "needs_user_answer":
                return
            option = _recommended_option(panel)
            if not option or not _safe_model_blocker_option(option):
                return
            result = workflow.apply_blocker_answer(
                answer=str(option.get("option_id")),
                custom_note="workspace workflow autopilot",
            )
            self._record("data_model_blocker_autopilot", "applied", f"Applied {option.get('option_id')}.", result.summary())
        except Exception as exc:
            self._warn("data_model_blocker_autopilot", exc)

    def _prepare_kpi_blockers(self) -> None:
        if not (self.layout.contracts_dir / "kpi_registry.json").exists():
            self._record("kpi_blocker", "skipped", "No onboarded KPI registry exists.")
            return
        try:
            result = prepare_kpi_blocker_panel(self.repo_root, self.workspace_rel, domain=self.domain, onboard_if_missing=False)
            self._record("kpi_blocker", "prepared", "Prepared KPI blocker panel.", result.summary())
            if self.mode == "autopilot":
                self._autopilot_kpi_blocker_once()
        except Exception as exc:
            self._warn("kpi_blocker", exc)

    def _autopilot_kpi_blocker_once(self) -> None:
        panel_path = self.layout.reports_dir / "blocker_question_panel" / "current.json"
        panel = _load_json(panel_path)
        if panel.get("status") != "needs_user_answer":
            return
        option = _recommended_option(panel)
        if not option or not _safe_kpi_blocker_option(option):
            return
        try:
            result = apply_kpi_panel_answer(
                self.repo_root,
                self.workspace_rel,
                answer=str(option.get("option_id")),
                domain=self.domain,
            )
            self._record("kpi_blocker_autopilot", "applied", f"Applied {option.get('option_id')}.", result.summary())
        except Exception as exc:
            self._warn("kpi_blocker_autopilot", exc)

    def _prepare_data_model_blockers(self) -> None:
        if not (self.layout.requirements_dir / "data_model_draft.json").exists():
            self._record("data_model_blocker", "skipped", "No data-model draft exists yet.")
            return
        try:
            result = DataModelGenerationWorkflow(self.repo_root, self.workspace_rel).prepare_blocker_panel()
            self._record("data_model_blocker", "prepared", "Prepared data-model blocker panel.", result.summary())
        except Exception as exc:
            self._warn("data_model_blocker", exc)

    def _validate_artifacts(self) -> dict[str, Any]:
        try:
            result = WorkspaceArtifactValidator(self.repo_root, self.workspace_rel).run()
            self._record("validation", "ran", "Validated generated workspace artifacts.", result.summary())
            return result.summary()
        except Exception as exc:
            self._warn("validation", exc)
            return {"ok": False, "errors": [str(exc)]}

    def _validation_summary_if_available(self) -> dict[str, Any]:
        if not self.layout.interns_dir.exists():
            return {"ok": False, "warnings": ["interns artifacts do not exist yet"]}
        return self._validate_artifacts()

    def _export_presentation(self) -> dict[str, Any]:
        try:
            result = WorkspacePresentationExporter(self.repo_root, self.workspace_rel).export_workspace_presentation()
            self._record("presentation", "exported", "Exported presentation SVG/XLSX artifacts.", result.summary())
            return result.summary()
        except Exception as exc:
            self._warn("presentation", exc)
            return {"warnings": [str(exc)]}

    def _prepare_wiki_memory(self) -> dict[str, Any]:
        try:
            result = WorkspaceWikiMemoryBuilder(
                self.repo_root,
                self.workspace_rel,
                domain=self.domain,
            ).prepare()
            self._record("wiki_memory", "prepared", "Prepared governed wiki memory reuse cards.", result.summary())
            return result.summary()
        except Exception as exc:
            self._warn("wiki_memory", exc)
            return {"warnings": [str(exc)]}

    def _wiki_memory_summary_if_available(self) -> dict[str, Any]:
        path = self.layout.reports_dir / "wiki_memory" / "current.json"
        if not path.exists():
            return {"status": "missing"}
        return _load_json(path)

    def _panel(
        self,
        *,
        listing: dict[str, Any],
        validation: dict[str, Any],
        presentation: dict[str, Any],
        wiki_memory: dict[str, Any],
    ) -> dict[str, Any]:
        next_commands = _next_commands(self.workspace_rel, self.domain)
        return {
            "artifact_type": "workflow/current.json",
            "version": WORKFLOW_VERSION,
            "generated_by": "prepare-workspace-workflow",
            "workspace": self.workspace_rel,
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "stage": "checkpoint",
            "status": "needs_user_choice",
            "mode": self.mode,
            "autopilot_boundaries": {
                "can_apply": [
                    "low-risk route/setup answers",
                    "JSON-backed blocker options that do not approve relationships",
                    "local-safe preparation and presentation exports",
                ],
                "must_stop_before": [
                    "final approval",
                    "delete/cleanup",
                    "remote execution",
                    "relationship approval",
                    "executable DDL/dbt/SQL generation",
                    "docs promotion",
                ],
            },
            "detected_files": {
                "file_count": len(listing.get("files", [])),
                "possible_kpi_files": listing.get("possible_kpi_files", []),
                "possible_data_model_files": listing.get("possible_data_model_files", []),
                "dataset_roots": listing.get("dataset_roots", []),
            },
            "actions": self.actions,
            "validation": validation,
            "presentation": presentation,
            "wiki_memory": wiki_memory,
            "warnings": self.warnings,
            "options": [
                {
                    "option_id": "option_a",
                    "label": "Continue manually",
                    "description": "Review current panels and answer the next blocker yourself.",
                    "next_commands": next_commands["manual"],
                },
                {
                    "option_id": "option_b",
                    "label": "Continue local-safe",
                    "description": "Run local-safe preparation again after adding or changing inputs.",
                    "next_commands": next_commands["local_safe"],
                },
                {
                    "option_id": "option_c",
                    "label": "Enter bounded autopilot",
                    "description": "Allow recommended low-risk answers, still stopping at approval/code/remote/delete gates.",
                    "next_commands": next_commands["autopilot"],
                },
            ],
            "recommended_option_id": "option_a" if validation.get("ok") is False else "option_b",
            "recommended_answer": (
                "Review validation warnings/blockers manually."
                if validation.get("ok") is False
                else "Continue local-safe preparation as inputs evolve."
            ),
            "next_step": "Choose an option from this workflow checkpoint panel.",
        }

    def _record(self, stage: str, status: str, message: str, detail: dict[str, Any] | None = None) -> None:
        self.actions.append({"stage": stage, "status": status, "message": message, "detail": detail or {}})

    def _warn(self, stage: str, exc: Exception) -> None:
        self.warnings.append(f"{stage}: {type(exc).__name__}: {exc}")
        self._record(stage, "warning", f"{type(exc).__name__}: {exc}")

    def _validate_workspace(self) -> None:
        if not self.workspace.exists():
            raise FileNotFoundError(f"workspace not found: {self.workspace}")
        if self.workspace == self.repo_root or not self.workspace.is_relative_to(self.repo_root):
            raise ValueError(f"workspace must be inside repo root: {self.workspace}")


def _safe_kpi_generation_stage(stage: str) -> bool:
    return stage in {"route_selection", "context_intake", "discovery_orientation", "format_selection"}


def _safe_data_model_stage(stage: str) -> bool:
    return stage in {"route_selection", "entity_inventory"}


def _safe_model_blocker_option(option: dict[str, Any]) -> bool:
    for operation in option.get("operations") or []:
        if operation.get("operation") in {"approve_relationship", "approve_for_execution"}:
            return False
    return bool(option.get("json_backed"))


def _safe_kpi_blocker_option(option: dict[str, Any]) -> bool:
    if not option.get("json_backed"):
        return False
    if "derived_feature_option" in option:
        return True
    if "physical_column_option" in option:
        return True
    return False


def _recommended_option(panel: dict[str, Any]) -> dict[str, Any] | None:
    recommended = str(panel.get("recommended_option_id") or "")
    for option in panel.get("options") or []:
        if option.get("option_id") == recommended:
            return option
    return None


def _next_commands(workspace: str, domain: str) -> dict[str, list[str]]:
    return {
        "manual": [
            f"uv run prepare-wiki-memory --workspace {workspace} --domain {domain}",
            f"uv run prepare-kpi-blocker-panel --workspace {workspace} --domain {domain}",
            f"uv run prepare-data-model-blocker-panel --workspace {workspace}",
        ],
        "local_safe": [
            f"uv run prepare-workspace-workflow --workspace {workspace} --mode local-safe --domain {domain}",
        ],
        "autopilot": [
            f"uv run prepare-workspace-workflow --workspace {workspace} --mode autopilot --domain {domain}",
        ],
    }


def _render_workflow_markdown(panel: dict[str, Any]) -> str:
    lines = [
        "# Workspace Workflow Checkpoint",
        "",
        f"- Workspace: `{panel.get('workspace', '')}`",
        f"- Mode: `{panel.get('mode', '')}`",
        f"- Status: `{panel.get('status', '')}`",
        f"- Recommended option: `{panel.get('recommended_option_id', '')}`",
        "",
        "## Recommended Answer",
        "",
        str(panel.get("recommended_answer", "")),
        "",
        "## Actions",
        "",
    ]
    for action in panel.get("actions", []):
        lines.append(f"- `{action.get('stage')}`: {action.get('status')} - {action.get('message')}")
    if panel.get("warnings"):
        lines.extend(["", "## Warnings", ""])
        lines.extend(f"- {warning}" for warning in panel["warnings"])
    lines.extend(["", "## Options", ""])
    for option in panel.get("options", []):
        lines.extend([f"### {option.get('option_id')}: {option.get('label')}", "", str(option.get("description", "")), ""])
        for command in option.get("next_commands", []):
            lines.append(f"- `{command}`")
        lines.append("")
    lines.extend(["## Autopilot Boundaries", ""])
    boundaries = panel.get("autopilot_boundaries", {})
    lines.append("Can apply:")
    for item in boundaries.get("can_apply", []):
        lines.append(f"- {item}")
    lines.append("")
    lines.append("Must stop before:")
    for item in boundaries.get("must_stop_before", []):
        lines.append(f"- {item}")
    lines.append("")
    return "\n".join(lines)


def _load_json(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return {}
    return data if isinstance(data, dict) else {}


def _rel(path: Path, root: Path) -> str:
    try:
        return path.resolve().relative_to(root.resolve()).as_posix()
    except ValueError:
        return path.as_posix()


def prepare_main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Prepare a governed workspace workflow checkpoint.")
    parser.add_argument("--workspace", required=True)
    parser.add_argument("--repo-root", default=".")
    parser.add_argument("--domain", default="healthcare")
    parser.add_argument("--mode", choices=sorted(MODES), default="local-safe")
    args = parser.parse_args(argv)
    result = WorkspaceWorkflowOrchestrator(
        args.repo_root,
        args.workspace,
        domain=args.domain,
        mode=args.mode,
    ).prepare()
    print(json.dumps(result.summary(), indent=2))
    return 0
