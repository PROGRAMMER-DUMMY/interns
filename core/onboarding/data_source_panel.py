"""Workspace-level data-source panel: an explicit, human-confirmed, once-asked
choice of where a workspace's data lives, rather than the silent implicit
merge `workspace_settings.json`'s `databricks_source` key used to trigger.

This is workspace-level (asked once, not per-KPI), so it follows the same
precedent as `core.onboarding.data_quality`'s duplicate-review panel pair
(`prepare-duplicate-review-panel` / `apply-duplicate-review-answer`), not the
per-KPI `blocker_question_panel` flow: a `current.json`/`current.md` panel
artifact under `WorkspaceLayout.reports_dir`, and a CLI answer command that
writes the durable decision and rebuilds the panel so a re-run reflects it.

See `WorkspaceLayout.databricks_source_mode()` for what the recorded `mode`
actually controls downstream.
"""
from __future__ import annotations
from core.observability.cost_ledger import anchored

import argparse
import json
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from core.contracts.versioning import register_contract
from core.governance.provenance import decision_source as decision_source_for
from core.paths import PROJECT_ROOT
from core.storage.workspace_layout import WorkspaceLayout

PANEL_VERSION = 1
register_contract("data_source_panel/current.json", current_version=PANEL_VERSION)

_OPTIONS = [
    {
        "option_id": "local_files",
        "label": "Local files only",
        "description": "Scan and profile local workspace datasets/ only. No Databricks connection is used for data.",
    },
    {
        "option_id": "databricks_additive",
        "label": "Local files + Databricks (additive)",
        "description": (
            "Scan local datasets/ AND profile the declared Unity Catalog "
            "catalog/schema; both are merged into the same profile index."
        ),
    },
    {
        "option_id": "databricks_exclusive",
        "label": "Databricks only (exclusive)",
        "description": (
            "Profile the declared Unity Catalog catalog/schema only; local "
            "datasets/ discovery is skipped entirely."
        ),
    },
]

_ANSWER_TO_MODE = {
    "local_files": "local_files",
    "databricks_additive": "additive",
    "databricks_exclusive": "exclusive",
}


@dataclass(frozen=True)
class DataSourcePanelResult:
    current_json_path: str
    current_markdown_path: str
    status: str
    current_mode: str
    ok: bool = True

    def summary(self) -> dict[str, Any]:
        return self.__dict__.copy()


class DataSourcePanelBuilder:
    def __init__(self, repo_root: str | Path, workspace: str | Path) -> None:
        self.repo_root = Path(repo_root).resolve()
        self.workspace = (self.repo_root / workspace).resolve()
        self.layout = WorkspaceLayout(project_root=self.workspace)

    def prepare(self) -> DataSourcePanelResult:
        self.layout.ensure_runtime_dirs()
        settings = self.layout.load_settings()
        raw_source = settings.get("databricks_source")
        declared_source = raw_source if isinstance(raw_source, dict) else {}
        current_mode = self.layout.databricks_source_mode()
        mode_declared_explicitly = bool(str(declared_source.get("mode") or "").strip())

        recommended = current_mode if current_mode in _ANSWER_TO_MODE.values() else "local_files"
        recommended_option_id = next(
            (oid for oid, mode in _ANSWER_TO_MODE.items() if mode == recommended),
            "local_files",
        )
        panel = {
            "artifact_type": "data_source_panel/current.json",
            "version": PANEL_VERSION,
            "generated_by": "prepare-data-source-panel",
            "status": "needs_user_answer" if not mode_declared_explicitly else "answered",
            "current_mode": current_mode,
            "mode_declared_explicitly": mode_declared_explicitly,
            "current_databricks_source": {
                "catalog": str(declared_source.get("catalog") or ""),
                "schema": str(declared_source.get("schema") or ""),
            } if declared_source else None,
            "recommended_option_id": recommended_option_id,
            "options": _OPTIONS,
            "apply_command": (
                "uv run apply-data-source-answer --workspace <workspace> "
                "--answer <option_id> [--catalog ... --schema ...] --confirmed-by \"<name>\""
            ),
        }
        panel_dir = self.layout.reports_dir / "data_source_panel"
        panel_dir.mkdir(parents=True, exist_ok=True)
        current_json = panel_dir / "current.json"
        current_md = panel_dir / "current.md"
        current_json.write_text(json.dumps(panel, indent=2) + "\n", encoding="utf-8")
        current_md.write_text(_render_markdown(panel), encoding="utf-8")
        return DataSourcePanelResult(
            _rel(current_json, self.repo_root),
            _rel(current_md, self.repo_root),
            panel["status"],
            current_mode,
        )


class DataSourceAnswerRecorder:
    def __init__(self, repo_root: str | Path, workspace: str | Path) -> None:
        self.repo_root = Path(repo_root).resolve()
        self.workspace = (self.repo_root / workspace).resolve()
        self.layout = WorkspaceLayout(project_root=self.workspace)

    def apply(
        self,
        answer: str,
        *,
        catalog: str = "",
        schema: str = "",
        confirmed_by: str = "",
    ) -> dict[str, Any]:
        normalized = answer.strip().lower()
        if normalized not in _ANSWER_TO_MODE:
            raise ValueError(
                f"--answer must be one of {sorted(_ANSWER_TO_MODE)}, got {answer!r}"
            )
        mode = _ANSWER_TO_MODE[normalized]

        settings_path = self.layout.durable_workspace_settings
        settings: dict[str, Any] = {}
        if settings_path.exists():
            try:
                loaded = json.loads(settings_path.read_text(encoding="utf-8"))
                if isinstance(loaded, dict):
                    settings = loaded
            except (json.JSONDecodeError, OSError):
                settings = {}
        existing_source = settings.get("databricks_source")
        existing_source = existing_source if isinstance(existing_source, dict) else {}

        resolved_catalog = catalog.strip() or str(existing_source.get("catalog") or "")
        resolved_schema = schema.strip() or str(existing_source.get("schema") or "")
        if mode != "local_files" and not (resolved_catalog and resolved_schema):
            raise ValueError(
                f"--catalog and --schema are required for --answer {normalized} "
                "(pass them or declare databricks_source.catalog/schema in "
                "workspace_settings.json first)"
            )

        confirmed_by = (confirmed_by or "").strip()
        decision_source = decision_source_for(confirmed_by)
        now = datetime.now(timezone.utc).isoformat()
        settings["databricks_source"] = {
            "catalog": resolved_catalog,
            "schema": resolved_schema,
            "mode": mode,
            "source": decision_source,
            "confirmed_by": confirmed_by,
            "confirmed_at": now,
        }
        settings_path.parent.mkdir(parents=True, exist_ok=True)
        settings_path.write_text(json.dumps(settings, indent=2) + "\n", encoding="utf-8")

        panel = DataSourcePanelBuilder(self.repo_root, _rel(self.workspace, self.repo_root)).prepare()
        return {
            "settings_path": _rel(settings_path, self.repo_root),
            "mode": mode,
            "source": decision_source,
            "confirmed_by": confirmed_by,
            "next_panel": panel.summary(),
        }


def _render_markdown(panel: dict[str, Any]) -> str:
    lines = [
        "# Data Source",
        "",
        f"Current mode: `{panel['current_mode']}`"
        + ("" if panel["mode_declared_explicitly"] else " (implicit default -- not yet confirmed)"),
        "",
        "## Options",
        "",
    ]
    for option in panel["options"]:
        marker = " (recommended)" if option["option_id"] == panel["recommended_option_id"] else ""
        lines.append(f"- `{option['option_id']}`{marker}: {option['label']} -- {option['description']}")
    lines.extend(["", "## Apply", "", f"```powershell\n{panel['apply_command']}\n```", ""])
    return "\n".join(lines)


def _rel(path: Path, root: Path) -> str:
    try:
        return path.resolve().relative_to(root.resolve()).as_posix()
    except ValueError:
        return path.as_posix()


@anchored("prepare-data-source-panel")
def panel_main(argv: list[str] | None = None) -> int:
    from core.onboarding.workspace.cli_runner import run_workspace_command

    parser = argparse.ArgumentParser()
    parser.add_argument("--workspace", required=True)
    parser.add_argument("--repo-root", default=str(PROJECT_ROOT))
    args = parser.parse_args(argv)
    return run_workspace_command(
        command="prepare-data-source-panel",
        workspace=args.workspace,
        repo_root=args.repo_root,
        fn=lambda: DataSourcePanelBuilder(args.repo_root, args.workspace).prepare(),
        validation="validate-workspace-artifacts",
    )


@anchored("apply-data-source-answer")
def apply_main(argv: list[str] | None = None) -> int:
    from core.onboarding.workspace.cli_runner import run_workspace_command

    parser = argparse.ArgumentParser()
    parser.add_argument("--workspace", required=True)
    parser.add_argument("--repo-root", default=str(PROJECT_ROOT))
    parser.add_argument("--answer", required=True)
    parser.add_argument("--catalog", default="")
    parser.add_argument("--schema", default="")
    parser.add_argument("--confirmed-by", default="")
    parser.add_argument("--allow-replay", action="store_true")
    args = parser.parse_args(argv)
    return run_workspace_command(
        command="apply-data-source-answer",
        workspace=args.workspace,
        repo_root=args.repo_root,
        fn=lambda: DataSourceAnswerRecorder(args.repo_root, args.workspace).apply(
            args.answer,
            catalog=args.catalog,
            schema=args.schema,
            confirmed_by=args.confirmed_by,
        ),
        op_args={
            "workspace": args.workspace,
            "answer": args.answer,
            "catalog": args.catalog,
            "schema": args.schema,
        },
        allow_replay=args.allow_replay,
        decision=args.answer,
        metadata={"answer": args.answer, "confirmed_by": args.confirmed_by},
        record_idempotent=True,
    )


if __name__ == "__main__":
    raise SystemExit(panel_main())
