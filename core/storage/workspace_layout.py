"""
Project workspace output layout.

Project inputs can live anywhere under ``workspaces/<project>``, but platform
outputs should be grouped under ``workspaces/<project>/interns`` so the project
root does not become cluttered.
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any


@dataclass(frozen=True)
class WorkspaceLayout:
    project_root: Path

    @classmethod
    def from_task(cls, task: dict[str, Any], repo_root: Path) -> "WorkspaceLayout":
        workspace_value = task.get("workspace")
        if workspace_value:
            return cls(project_root=(repo_root / workspace_value).resolve())

        editable = Path(task.get("editable_file", ""))
        parts = editable.parts
        if len(parts) >= 2 and parts[0] == "workspaces":
            return cls(project_root=(repo_root / parts[0] / parts[1]).resolve())

        return cls(project_root=repo_root.resolve())

    @property
    def interns_dir(self) -> Path:
        return self.project_root / "interns"

    @property
    def state_dir(self) -> Path:
        return self.interns_dir / "state"

    @property
    def workspace_db(self) -> Path:
        return self.state_dir / "workspace.db"

    @property
    def workspace_settings(self) -> Path:
        return self.state_dir / "workspace_settings.json"

    @property
    def run_log(self) -> Path:
        return self.state_dir / "run.log"

    @property
    def runs_dir(self) -> Path:
        return self.interns_dir / "runs"

    @property
    def generated_dir(self) -> Path:
        return self.interns_dir / "generated"

    @property
    def contracts_dir(self) -> Path:
        return self.generated_dir / "contracts"

    @property
    def profiles_dir(self) -> Path:
        return self.generated_dir / "profiles"

    @property
    def evidence_dir(self) -> Path:
        return self.generated_dir / "evidence"

    @property
    def solutions_dir(self) -> Path:
        return self.generated_dir / "solutions"

    @property
    def requirements_dir(self) -> Path:
        return self.generated_dir / "requirements"

    @property
    def memory_dir(self) -> Path:
        return self.generated_dir / "memory"

    @property
    def evaluation_dir(self) -> Path:
        return self.interns_dir / "evaluation"

    @property
    def reports_dir(self) -> Path:
        return self.interns_dir / "reports"

    def ensure_runtime_dirs(self) -> None:
        for path in [
            self.state_dir,
            self.runs_dir,
            self.contracts_dir,
            self.profiles_dir,
            self.evidence_dir,
            self.solutions_dir,
            self.requirements_dir,
            self.memory_dir,
            self.evaluation_dir,
            self.reports_dir,
        ]:
            path.mkdir(parents=True, exist_ok=True)

    def load_settings(self) -> dict[str, Any]:
        if self.workspace_settings.exists():
            try:
                return json.loads(self.workspace_settings.read_text(encoding="utf-8"))
            except Exception:
                pass
        return {}

    def is_dataset_allowed(self, path: Path) -> bool:
        settings = self.load_settings()
        allowlist = settings.get("dataset_allowlist")
        if not allowlist:
            return True
            
        try:
            rel_path = path.relative_to(self.project_root)
        except ValueError:
            return True # Not inside the project root, cannot be restricted by workspace-level allowlist
            
        rel_str = str(rel_path).replace("\\", "/")
        
        # A file is allowed if it's within any of the allowlisted directory prefixes
        return any(rel_str.startswith(allowed.replace("\\", "/")) for allowed in allowlist)

    def summary(self) -> dict[str, str]:
        return {
            "project_root": str(self.project_root),
            "interns_dir": str(self.interns_dir),
            "state_dir": str(self.state_dir),
            "workspace_db": str(self.workspace_db),
            "run_log": str(self.run_log),
            "runs_dir": str(self.runs_dir),
            "generated_dir": str(self.generated_dir),
            "contracts_dir": str(self.contracts_dir),
            "profiles_dir": str(self.profiles_dir),
            "evidence_dir": str(self.evidence_dir),
            "solutions_dir": str(self.solutions_dir),
            "requirements_dir": str(self.requirements_dir),
            "memory_dir": str(self.memory_dir),
            "evaluation_dir": str(self.evaluation_dir),
            "reports_dir": str(self.reports_dir),
        }
