from __future__ import annotations
from core.observability.cost_ledger import anchored

import argparse
import json
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from core.paths import PROJECT_ROOT
from tools.list_workspace_files import WorkspaceFileListing, list_workspace_files


@dataclass(frozen=True)
class WorkspaceSummary:
    name: str
    path: str
    file_count: int
    has_docs: bool
    has_datasets: bool
    interns_state: str


@dataclass(frozen=True)
class WorkspaceSelectionResult:
    requested_workspace: str
    resolved_workspace: str
    status: str
    message: str
    listing: dict[str, Any]
    available_workspaces: list[dict[str, Any]]
    recommended_next_step: str
    external_data_setup: dict[str, Any]

    def summary(self) -> dict[str, Any]:
        return asdict(self)


def prepare_workspace_selection(
    repo_root: str | Path,
    workspace: str,
    *,
    max_files: int = 200,
    max_available: int = 20,
) -> WorkspaceSelectionResult:
    root = Path(repo_root).resolve()
    listing = list_workspace_files(root, workspace, max_files=max_files)
    available = _available_workspaces(root, max_available=max_available)
    status, message, next_step = _status_message(listing)
    return WorkspaceSelectionResult(
        requested_workspace=workspace,
        resolved_workspace=listing.workspace,
        status=status,
        message=message,
        listing=listing.to_dict(),
        available_workspaces=[asdict(item) for item in available],
        recommended_next_step=next_step,
        external_data_setup=_external_data_setup(listing.workspace),
    )


def _status_message(listing: WorkspaceFileListing) -> tuple[str, str, str]:
    if not listing.exists:
        return (
            "blocked_missing_workspace",
            (
                f"I could not find workspace `{listing.workspace}`. Confirm the workspace name, "
                "choose one of the available workspaces, or create a lightweight workspace that "
                "points to external raw data."
            ),
            "Ask the user to confirm the correct workspace name or approve creating a new external-data workspace.",
        )
    if listing.file_count == 0:
        return (
            "blocked_empty_workspace",
            (
                f"Workspace `{listing.workspace}` exists but contains no files. I should not scan "
                "unrelated folders or borrow files from another workspace."
            ),
            (
                "Ask whether this is the intended empty workspace. If yes, create lightweight docs "
                "and configure `interns/state/workspace_settings.json` with a dataset_allowlist "
                "pointing to the external raw data folder."
            ),
        )
    if not listing.possible_kpi_files and not listing.dataset_roots and not listing.docs:
        return (
            "needs_confirmation_no_obvious_inputs",
            (
                f"Workspace `{listing.workspace}` has files, but no obvious KPI, dataset, data model, "
                "or docs were classified."
            ),
            "Ask the user to confirm whether this is the right file set before onboarding.",
        )
    return (
        "ready_for_confirmation",
        f"Workspace `{listing.workspace}` has classified inputs and is ready for file-set confirmation.",
        "Present the listed file set and ask the user to confirm before onboarding.",
    )


def _available_workspaces(root: Path, *, max_available: int) -> list[WorkspaceSummary]:
    workspaces_root = root / "workspaces"
    if not workspaces_root.exists():
        return []
    summaries: list[WorkspaceSummary] = []
    for path in sorted(item for item in workspaces_root.iterdir() if item.is_dir()):
        files = [file for file in path.rglob("*") if file.is_file()]
        rel_files = [
            file.resolve().relative_to(root).as_posix()
            for file in files[:50]
            if _is_relative_to(file.resolve(), root)
        ]
        summaries.append(
            WorkspaceSummary(
                name=path.name,
                path=path.relative_to(root).as_posix(),
                file_count=len(files),
                has_docs=any("/docs/" in file for file in rel_files),
                has_datasets=any("/datasets/" in file for file in rel_files),
                interns_state="present" if (path / "interns").exists() else "missing",
            )
        )
        if len(summaries) >= max_available:
            break
    return summaries


def _external_data_setup(workspace: str) -> dict[str, Any]:
    return {
        "when_to_use": "Use this when raw data is large, messy, or must stay outside the workspace.",
        "workspace_shape": [
            f"{workspace}/docs/",
            f"{workspace}/interns/state/workspace_settings.json",
        ],
        "settings_template": {
            "dataset_allowlist": [
                {
                    "path": "<external-data-root>/cold_storage/raw",
                    "reason": "External raw data folder; keep generated artifacts in workspace only.",
                }
            ]
        },
        "generated_outputs_stay_under": f"{workspace}/interns/",
        "do_not_copy_raw_data_into_workspace": True,
    }


def _is_relative_to(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
        return True
    except ValueError:
        return False


def render_text(result: WorkspaceSelectionResult) -> str:
    lines = [
        "# Workspace Selection Harness",
        "",
        f"- Requested: `{result.requested_workspace}`",
        f"- Resolved: `{result.resolved_workspace}`",
        f"- Status: `{result.status}`",
        "",
        result.message,
        "",
    ]
    listing = result.listing
    lines.extend(
        [
            "## Active Workspace Listing",
            "",
            f"- Exists: `{listing.get('exists')}`",
            f"- Files listed: `{listing.get('file_count')}`",
            f"- Dataset roots: {', '.join(listing.get('dataset_roots') or []) or '(none)'}",
            f"- KPI files: {', '.join(listing.get('possible_kpi_files') or []) or '(none)'}",
            f"- Data model files: {', '.join(listing.get('possible_data_model_files') or []) or '(none)'}",
            f"- Docs: {', '.join(listing.get('docs') or []) or '(none)'}",
            "",
            "## Available Workspaces",
            "",
        ]
    )
    if result.available_workspaces:
        for item in result.available_workspaces:
            lines.append(
                f"- `{item['path']}` ({item['file_count']} files, "
                f"docs={item['has_docs']}, datasets={item['has_datasets']})"
            )
    else:
        lines.append("- (none)")
    setup = result.external_data_setup
    lines.extend(
        [
            "",
            "## Recommended Next Step",
            "",
            result.recommended_next_step,
            "",
            "## External Data Workspace Option",
            "",
            setup["when_to_use"],
            "",
            "Settings template:",
            "",
            "```json",
            json.dumps(setup["settings_template"], indent=2),
            "```",
            "",
            f"Generated outputs stay under: `{setup['generated_outputs_stay_under']}`",
        ]
    )
    return "\n".join(lines)



@anchored("prepare-workspace-selection")
def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description="Prepare a guarded workspace-selection panel.")
    parser.add_argument("--workspace", required=True, nargs="+")
    parser.add_argument("--repo-root", default=str(PROJECT_ROOT))
    parser.add_argument("--max-files", type=int, default=200)
    parser.add_argument("--max-available", type=int, default=20)
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)

    workspace = " ".join(args.workspace)
    result = prepare_workspace_selection(
        args.repo_root,
        workspace,
        max_files=args.max_files,
        max_available=args.max_available,
    )
    if args.json:
        print(json.dumps(result.summary(), indent=2))
        return
    print(render_text(result))


if __name__ == "__main__":
    main()
