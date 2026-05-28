import argparse
import json
import re
import shutil
import sys
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any


RUNTIME_STATE_FILES = (
    "state/run.log",
    "state/workspace.db",
    "state/dashboard_live.log",
    "state/dashboard_stdout.log",
    "state/dashboard_stderr.log",
)


@dataclass
class CleanupAction:
    action: str
    target: str
    reason: str
    status: str


@dataclass
class WorkspaceCleanupResult:
    workspace: str
    applied: bool
    actions: list[CleanupAction]

    def to_dict(self) -> dict[str, Any]:
        return {
            "workspace": self.workspace,
            "applied": self.applied,
            "actions": [asdict(action) for action in self.actions],
        }


class WorkspaceReferenceCleaner:
    """Plan and apply a workspace reset without touching source docs or datasets."""

    def __init__(
        self,
        repo_root: Path | str,
        workspace: str,
        *,
        remove_workspace_interns: bool = True,
        remove_repo_state: bool = False,
        remove_task_config: bool = False,
        delete_confirmed: bool = False,
    ) -> None:
        self.repo_root = Path(repo_root).resolve()
        self.workspace = self._normalize_workspace(workspace)
        self.workspace_path = (self.repo_root / self.workspace).resolve()
        self.remove_workspace_interns = remove_workspace_interns
        self.remove_repo_state = remove_repo_state
        self.remove_task_config = remove_task_config
        self.delete_confirmed = delete_confirmed

    def plan(self) -> WorkspaceCleanupResult:
        actions: list[CleanupAction] = []

        if self.remove_workspace_interns:
            interns_dir = self.workspace_path / "interns"
            if interns_dir.exists():
                actions.append(
                    CleanupAction(
                        action="delete_tree",
                        target=self._display_path(interns_dir),
                        reason="Remove generated workspace artifacts only; docs and datasets stay intact.",
                        status="planned",
                    )
                )
            else:
                actions.append(
                    CleanupAction(
                        action="delete_tree",
                        target=self._display_path(interns_dir),
                        reason="Generated workspace artifacts are already absent.",
                        status="missing",
                    )
                )
            
            wiki_dir = self.workspace_path / "wiki"
            if wiki_dir.exists():
                actions.append(
                    CleanupAction(
                        action="delete_tree",
                        target=self._display_path(wiki_dir),
                        reason="Remove generated workspace wiki notes.",
                        status="planned",
                    )
                )
            else:
                actions.append(
                    CleanupAction(
                        action="delete_tree",
                        target=self._display_path(wiki_dir),
                        reason="Workspace wiki directory is already absent.",
                        status="missing",
                    )
                )

        if self.remove_repo_state:
            actions.extend(self._repo_state_actions())

        if self.remove_task_config:
            task_config = self.repo_root / "config" / "tasks.json"
            if self._task_config_has_workspace(task_config):
                actions.append(
                    CleanupAction(
                        action="rewrite_task_config",
                        target=self._display_path(task_config),
                        reason="Remove task entries and active_task references for this workspace.",
                        status="planned",
                    )
                )
            else:
                actions.append(
                    CleanupAction(
                        action="rewrite_task_config",
                        target=self._display_path(task_config),
                        reason="No task entry references this workspace.",
                        status="missing",
                    )
                )

        return WorkspaceCleanupResult(workspace=self.workspace, applied=False, actions=actions)

    def apply(self) -> WorkspaceCleanupResult:
        planned = self.plan()
        self._require_delete_confirmation(planned.actions)
        applied_actions: list[CleanupAction] = []
        for action in planned.actions:
            if action.status != "planned":
                applied_actions.append(action)
                continue

            target = (self.repo_root / action.target).resolve()
            if action.action == "delete_tree":
                self._ensure_deletable(target)
                self._preserve_workspace_settings(target)
                shutil.rmtree(target)
            elif action.action == "delete_file":
                self._ensure_deletable(target)
                target.unlink()
            elif action.action == "rewrite_task_config":
                self._rewrite_task_config(target)
            elif action.action == "rewrite_deployment_index":
                self._rewrite_deployment_index(target)
            elif action.action == "rewrite_wiki_memory_index":
                self._rewrite_wiki_memory_index(target)
            else:
                raise ValueError(f"Unsupported cleanup action: {action.action}")

            applied_actions.append(
                CleanupAction(
                    action=action.action,
                    target=action.target,
                    reason=action.reason,
                    status="applied",
                )
            )

        return WorkspaceCleanupResult(
            workspace=self.workspace,
            applied=True,
            actions=applied_actions,
        )

    def _require_delete_confirmation(self, actions: list[CleanupAction]) -> None:
        # Deletion is always a hard permission boundary. Callers must opt in explicitly.
        if self.delete_confirmed:
            return
        if any(action.status == "planned" and action.action in {"delete_tree", "delete_file"} for action in actions):
            raise PermissionError(
                "Refusing to delete without hard confirmation. "
                f"Pass confirm_delete='{self.workspace}' after reviewing the dry run."
            )

    def _repo_state_actions(self) -> list[CleanupAction]:
        actions: list[CleanupAction] = []
        for rel_path in RUNTIME_STATE_FILES:
            path = self.repo_root / rel_path
            if path.exists():
                actions.append(
                    CleanupAction(
                        action="delete_file",
                        target=rel_path,
                        reason="Remove repo-level runtime cache/log from previous sessions.",
                        status="planned",
                    )
                )

        deployment_root = self.repo_root / "state" / "databricks" / "deployments"
        deployment_dir = deployment_root / self._workspace_slug()
        if deployment_dir.exists():
            actions.append(
                CleanupAction(
                    action="delete_tree",
                    target=self._display_path(deployment_dir),
                    reason="Remove central Databricks deployment artifacts for this workspace.",
                    status="planned",
                )
            )

        deployment_index = deployment_root / "deployment_index.json"
        if self._file_contains_workspace(deployment_index):
            actions.append(
                CleanupAction(
                    action="rewrite_deployment_index",
                    target=self._display_path(deployment_index),
                    reason="Prune deployment index entries for this workspace.",
                    status="planned",
                )
            )

        latest_report = deployment_root / "latest.md"
        if self._file_contains_workspace(latest_report):
            actions.append(
                CleanupAction(
                    action="delete_file",
                    target=self._display_path(latest_report),
                    reason="Remove stale latest deployment report for this workspace.",
                    status="planned",
                )
            )

        team_memory_root = self.repo_root / "state" / "team_memory"
        wiki_index = team_memory_root / "wiki_memory_index.json"
        if self._file_contains_workspace(wiki_index):
            actions.append(
                CleanupAction(
                    action="rewrite_wiki_memory_index",
                    target=self._display_path(wiki_index),
                    reason="Prune wiki memory index entries for this workspace.",
                    status="planned",
                )
            )
        else:
            actions.append(
                CleanupAction(
                    action="rewrite_wiki_memory_index",
                    target=self._display_path(wiki_index),
                    reason="No wiki memory index entries reference this workspace.",
                    status="missing",
                )
            )

        return actions

    def _rewrite_task_config(self, path: Path) -> None:
        if not path.exists():
            return
        payload = json.loads(path.read_text(encoding="utf-8"))
        tasks = payload.get("tasks", [])
        kept_tasks = [task for task in tasks if not self._object_references_workspace(task)]
        removed_active = self._object_references_workspace(payload.get("active_task"))
        if not removed_active:
            for task in tasks:
                if task.get("id") == payload.get("active_task") and self._object_references_workspace(task):
                    removed_active = True
                    break
        payload["tasks"] = kept_tasks
        if removed_active:
            payload["active_task"] = kept_tasks[0].get("id", "") if kept_tasks else ""
        path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")

    def _rewrite_deployment_index(self, path: Path) -> None:
        if not path.exists():
            return
        payload = json.loads(path.read_text(encoding="utf-8"))
        deployments = payload.get("deployments", [])
        payload["deployments"] = [
            deployment
            for deployment in deployments
            if not self._object_references_workspace(deployment)
        ]
        if self._object_references_workspace(payload.get("latest")):
            payload["latest"] = payload["deployments"][-1] if payload["deployments"] else None
        path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")

    def _rewrite_wiki_memory_index(self, path: Path) -> None:
        if not path.exists():
            return
        payload = json.loads(path.read_text(encoding="utf-8"))
        definitions = payload.get("definitions", [])
        kept_definitions = []
        for definition in definitions:
            workspaces = definition.get("workspaces", [])
            kept_workspaces = [
                ws for ws in workspaces
                if not self._object_references_workspace(ws)
            ]
            if kept_workspaces:
                definition["workspaces"] = kept_workspaces
                definition["repeat_count"] = len(kept_workspaces)
                kept_definitions.append(definition)
        payload["definitions"] = kept_definitions
        path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")

    def _task_config_has_workspace(self, path: Path) -> bool:
        if not path.exists():
            return False
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            return False
        return self._object_references_workspace(payload)

    def _file_contains_workspace(self, path: Path) -> bool:
        if not path.exists() or not path.is_file():
            return False
        try:
            return self._workspace_key() in path.read_text(encoding="utf-8").replace("\\", "/").lower()
        except UnicodeDecodeError:
            return False

    def _object_references_workspace(self, value: Any) -> bool:
        if isinstance(value, str):
            normalized = value.replace("\\", "/").lower()
            return self._workspace_key() in normalized or self._workspace_id_key() == normalized.lower()
        if isinstance(value, list):
            return any(self._object_references_workspace(item) for item in value)
        if isinstance(value, dict):
            return any(self._object_references_workspace(item) for item in value.values())
        return False

    def _ensure_deletable(self, path: Path) -> None:
        path = path.resolve()
        state_root = (self.repo_root / "state").resolve()
        interns_dir = (self.workspace_path / "interns").resolve()
        wiki_dir = (self.workspace_path / "wiki").resolve()
        if path in {interns_dir, wiki_dir} or self._is_relative_to(path, state_root):
            return
        raise ValueError(f"Refusing to delete outside workspace interns, wiki, or repo state: {path}")

    def _preserve_workspace_settings(self, target: Path) -> None:
        interns_dir = (self.workspace_path / "interns").resolve()
        if target.resolve() != interns_dir:
            return
        settings = interns_dir / "state" / "workspace_settings.json"
        if not settings.exists():
            return
        durable = self.workspace_path / "workspace_settings.json"
        durable.write_text(settings.read_text(encoding="utf-8"), encoding="utf-8")

    def _display_path(self, path: Path) -> str:
        try:
            return path.resolve().relative_to(self.repo_root).as_posix()
        except ValueError:
            return path.as_posix()

    def _workspace_key(self) -> str:
        return self.workspace.replace("\\", "/").lower().rstrip("/")

    def _workspace_id_key(self) -> str:
        return Path(self.workspace).name.lower()

    def _workspace_slug(self) -> str:
        return re.sub(r"[^a-z0-9]+", "-", self._workspace_key()).strip("-")

    @staticmethod
    def _normalize_workspace(workspace: str) -> str:
        return workspace.replace("\\", "/").strip().rstrip("/")

    @staticmethod
    def _is_relative_to(path: Path, parent: Path) -> bool:
        try:
            path.relative_to(parent)
            return True
        except ValueError:
            return False


def run_cleanup(
    repo_root: Path | str,
    workspace: str,
    *,
    apply: bool = False,
    confirm_delete: str | None = None,
    remove_workspace_interns: bool = True,
    remove_repo_state: bool = False,
    remove_task_config: bool = False,
) -> WorkspaceCleanupResult:
    expected = WorkspaceReferenceCleaner._normalize_workspace(workspace)
    confirmed = (
        WorkspaceReferenceCleaner._normalize_workspace(confirm_delete)
        if confirm_delete is not None
        else None
    )
    cleaner = WorkspaceReferenceCleaner(
        repo_root,
        workspace,
        remove_workspace_interns=remove_workspace_interns,
        remove_repo_state=remove_repo_state,
        remove_task_config=remove_task_config,
        delete_confirmed=confirmed == expected,
    )
    if apply:
        if confirmed != expected:
            raise PermissionError(
                "Refusing to apply cleanup without exact delete confirmation. "
                f"Use confirm_delete='{expected}'."
            )
        return cleaner.apply()
    return cleaner.plan()


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Remove generated workspace artifacts and stale repo-level references."
    )
    parser.add_argument("--workspace", required=True, help="Workspace path, for example workspaces/demo")
    parser.add_argument("--repo-root", default=".", help="Repository root. Defaults to current directory.")
    parser.add_argument("--apply", action="store_true", help="Apply the cleanup. Default is dry-run.")
    parser.add_argument(
        "--confirm-delete",
        help="Required with --apply when deletion is planned. Must exactly equal --workspace.",
    )
    parser.add_argument(
        "--all-references",
        action="store_true",
        help="Remove workspace interns, repo runtime state, Databricks deployment refs, and task config refs.",
    )
    parser.add_argument(
        "--keep-workspace-interns",
        action="store_true",
        help="Do not remove workspaces/<project>/interns.",
    )
    parser.add_argument(
        "--repo-state",
        action="store_true",
        help="Remove repo-level runtime state/logs and Databricks deployment refs.",
    )
    parser.add_argument(
        "--task-config",
        action="store_true",
        help="Remove matching entries from config/tasks.json.",
    )
    parser.add_argument("--json", action="store_true", help="Print machine-readable JSON output.")
    args = parser.parse_args()

    try:
        result = run_cleanup(
            Path(args.repo_root),
            args.workspace,
            apply=args.apply,
            confirm_delete=args.confirm_delete,
            remove_workspace_interns=not args.keep_workspace_interns,
            remove_repo_state=args.repo_state or args.all_references,
            remove_task_config=args.task_config or args.all_references,
        )
    except PermissionError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        raise SystemExit(2) from exc
    if args.json:
        print(json.dumps(result.to_dict(), indent=2))
        return

    mode = "APPLIED" if result.applied else "DRY RUN"
    print(f"{mode}: cleanup plan for {result.workspace}")
    for action in result.actions:
        print(f"- [{action.status}] {action.action}: {action.target}")
        print(f"  {action.reason}")


if __name__ == "__main__":
    main()
