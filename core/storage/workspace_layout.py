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

from core.storage.external_data import (
    external_allowlist_paths,
    path_allowed_by_entries,
)


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
    def durable_workspace_settings(self) -> Path:
        return self.project_root / "workspace_settings.json"

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

    @property
    def kpi_registry_path(self) -> Path:
        return self.contracts_dir / "kpi_registry.json"

    @property
    def kpi_feature_mapping_path(self) -> Path:
        return self.contracts_dir / "kpi_feature_mapping.json"

    @property
    def relationship_contracts_path(self) -> Path:
        return self.contracts_dir / "relationship_contracts.json"

    @property
    def data_model_contract_path(self) -> Path:
        return self.contracts_dir / "data_model_contract.json"

    @property
    def source_to_target_plan_path(self) -> Path:
        return self.contracts_dir / "source_to_target_plan.json"

    @property
    def profile_index_path(self) -> Path:
        return self.profiles_dir / "profile_index.json"

    @property
    def workflow_sessions_dir(self) -> Path:
        return self.state_dir / "workflow_sessions"

    @property
    def handoffs_dir(self) -> Path:
        return self.state_dir / "handoffs"

    @property
    def dashboard_dir(self) -> Path:
        return self.project_root / "dashboard"

    @property
    def dashboard_exports_dir(self) -> Path:
        return self.dashboard_dir / "exports"

    @property
    def dashboard_index_path(self) -> Path:
        return self.dashboard_dir / "index.json"

    @property
    def medallion_dir(self) -> Path:
        return self.state_dir / "medallion"

    @property
    def bronze_dir(self) -> Path:
        return self.medallion_dir / "bronze"

    @property
    def silver_dir(self) -> Path:
        return self.medallion_dir / "silver"

    @property
    def gold_dir(self) -> Path:
        return self.medallion_dir / "gold"

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
        for path in (self.workspace_settings, self.durable_workspace_settings):
            if not path.exists():
                continue
            try:
                return json.loads(path.read_text(encoding="utf-8"))
            except Exception:
                pass
        return {}

    def databricks_source_mode(self) -> str:
        """The workspace's data-source mode for onboarding.

        Read from `workspace_settings.json`'s `databricks_source.mode`:
          "local_files" -- scan local `datasets/` only (the default).
          "additive"    -- scan local files AND profile the declared UC
                            catalog/schema, merging both into
                            profile_index.json.
          "exclusive"   -- UC catalog/schema only; local dataset discovery
                            is skipped entirely.

        No `databricks_source` key at all -> "local_files": every existing
        local-only workspace is unaffected. A `databricks_source` declared
        with a catalog/schema but no `mode` -> "additive": this is today's
        original silent-merge behavior, preserved as the default for a
        workspace whose owner has not yet been asked to make the choice
        explicit via the data_source_panel (see core.onboarding.data_source_panel).
        """
        settings = self.load_settings()
        source = settings.get("databricks_source")
        if not isinstance(source, dict):
            return "local_files"
        mode = str(source.get("mode") or "").strip().lower()
        if mode in {"local_files", "additive", "exclusive"}:
            return mode
        return "additive"

    def enterprise_id(self) -> str:
        """The tenant/enterprise a Databricks-sourced workspace belongs to,
        for core.config.resolve_databricks_config(). Explicit
        `databricks_source.enterprise_id` wins; falls back to
        `databricks_source.catalog` (the natural per-enterprise isolation
        boundary -- see the dbt+Airflow integration plan) when absent; empty
        string (global config, today's only real case) when neither is set.
        """
        settings = self.load_settings()
        source = settings.get("databricks_source")
        if not isinstance(source, dict):
            return ""
        explicit = str(source.get("enterprise_id") or "").strip()
        if explicit:
            return explicit
        return str(source.get("catalog") or "").strip()

    def is_dataset_allowed(self, path: Path) -> bool:
        settings = self.load_settings()
        return path_allowed_by_entries(path, project_root=self.project_root, settings=settings)

    def external_dataset_allowlist_paths(self) -> list[Path]:
        return external_allowlist_paths(self.load_settings())

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
