from __future__ import annotations

import argparse
import json
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from core.paths import PROJECT_ROOT
from core.storage.workspace_layout import WorkspaceLayout

REMOTE_APPROVAL_ENV = "AUTORESEARCH_ALLOW_REMOTE_EXECUTION"


@dataclass(frozen=True)
class PipelineDeploymentPlanResult:
    json_path: str
    markdown_path: str
    status: str
    target: str
    mode: str

    def summary(self) -> dict[str, Any]:
        return self.__dict__.copy()


class PipelineDeploymentPlanner:
    def __init__(self, repo_root: str | Path, workspace: str | Path, *, target: str = "local", mode: str = "dry-run") -> None:
        self.repo_root = Path(repo_root).resolve()
        self.workspace = (self.repo_root / workspace).resolve()
        self.target = target
        self.mode = mode
        self.layout = WorkspaceLayout(project_root=self.workspace)

    def build(self) -> PipelineDeploymentPlanResult:
        if self.mode == "apply" and self.target in {"external", "warehouse"} and os.environ.get(REMOTE_APPROVAL_ENV) != "1":
            raise PermissionError(f"{REMOTE_APPROVAL_ENV}=1 is required for remote apply")
        self.layout.ensure_runtime_dirs()
        status = "planned_apply" if self.mode == "apply" else "dry_run_ready"
        payload = {
            "artifact_type": "pipeline_deployment_plan.json",
            "target": self.target,
            "mode": self.mode,
            "status": status,
            "remote_writes_require_explicit_approval": True,
            "execution_policy": {
                "no_actual_remote_mutation": self.mode == "dry-run",
                "remote_mutation_performed": False,
            },
            "remote_approval_env": REMOTE_APPROVAL_ENV,
        }
        json_path = self.layout.contracts_dir / "pipeline_deployment_plan.json"
        md_path = self.layout.reports_dir / "pipeline_deployment_plan.md"
        json_path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
        md_path.write_text(f"# Pipeline Deployment Plan\n\nStatus: `{status}`\n", encoding="utf-8")
        return PipelineDeploymentPlanResult(_rel(json_path, self.repo_root), _rel(md_path, self.repo_root), status, self.target, self.mode)


def _rel(path: Path, root: Path) -> str:
    try:
        return path.resolve().relative_to(root.resolve()).as_posix()
    except ValueError:
        return path.as_posix()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--workspace", required=True)
    parser.add_argument("--repo-root", default=str(PROJECT_ROOT))
    parser.add_argument("--target", default="local")
    parser.add_argument("--mode", default="dry-run")
    args = parser.parse_args(argv)
    print(json.dumps(PipelineDeploymentPlanner(args.repo_root, args.workspace, target=args.target, mode=args.mode).build().summary(), indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
