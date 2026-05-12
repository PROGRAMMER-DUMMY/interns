"""Automatic local-safe workspace bootstrap.

This module decides whether a workspace already has current generated artifacts
under ``interns/`` or whether onboarding should be rerun.
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from core.config import Config
from core.onboarding.workspace_onboarding import WorkspaceOnboarder
from core.storage.metadata_store import build_metadata_store
from core.storage.workspace_layout import WorkspaceLayout


@dataclass(frozen=True)
class DatabricksReadiness:
    configured: bool
    active: bool
    execution: str
    message: str
    approval_required: bool = True


@dataclass(frozen=True)
class BootstrapResult:
    action: str
    reason: str
    manifest_path: str
    fingerprint: str
    workspace: str
    artifacts: dict[str, str] = field(default_factory=dict)
    databricks: DatabricksReadiness | None = None

    def summary(self) -> dict[str, Any]:
        return {
            "action": self.action,
            "reason": self.reason,
            "manifest_path": self.manifest_path,
            "fingerprint": self.fingerprint,
            "workspace": self.workspace,
            "artifacts": self.artifacts,
            "databricks": (
                {
                    "configured": self.databricks.configured,
                    "active": self.databricks.active,
                    "execution": self.databricks.execution,
                    "message": self.databricks.message,
                    "approval_required": self.databricks.approval_required,
                }
                if self.databricks
                else None
            ),
        }


class AutoBootstrap:
    def __init__(
        self,
        repo_root: str | Path,
        task: dict[str, Any],
        cfg: Config | None = None,
        *,
        sample_rows: int = 100_000,
    ):
        self.repo_root = Path(repo_root).resolve()
        self.task = task
        self.cfg = cfg
        self.sample_rows = sample_rows
        self.layout = WorkspaceLayout.from_task(task, self.repo_root)
        self.metadata_store = build_metadata_store(self.layout, repo_root=self.repo_root)

    def ensure_ready(self) -> BootstrapResult:
        self.layout.ensure_runtime_dirs()
        fingerprint = self.compute_fingerprint()
        manifest_path = self.layout.state_dir / "bootstrap_manifest.json"
        databricks = self.check_databricks_readiness() if self.cfg else None
        current = self._read_manifest(manifest_path)
        ready, reason = self._is_current(current, fingerprint)
        if ready:
            result = BootstrapResult(
                action="reuse",
                reason=reason,
                manifest_path=str(manifest_path),
                fingerprint=fingerprint,
                workspace=str(self.layout.project_root),
                artifacts=current.get("artifacts", {}),
                databricks=databricks,
            )
            self._write_status(result)
            return result

        onboarding = WorkspaceOnboarder(
            self.repo_root,
            self.layout.project_root,
            sample_rows=self.sample_rows,
        ).run()
        manifest = {
            "fingerprint": fingerprint,
            "workspace": str(self.layout.project_root),
            "artifacts": onboarding.artifacts,
            "kpi_count": onboarding.kpi_count,
            "profile_count": onboarding.profile_count,
        }
        manifest_path.write_text(json.dumps(manifest, indent=2), encoding="utf-8")
        self.metadata_store.upsert(
            "bootstrap",
            "bootstrap_manifest",
            manifest,
            workspace=str(self.layout.project_root),
        )
        result = BootstrapResult(
            action="generated",
            reason=reason,
            manifest_path=str(manifest_path),
            fingerprint=fingerprint,
            workspace=str(self.layout.project_root),
            artifacts=onboarding.artifacts,
            databricks=databricks,
        )
        self._write_status(result)
        return result

    def compute_fingerprint(self) -> str:
        digest = hashlib.sha256()
        for path in self._fingerprint_inputs():
            rel = _rel(path, self.repo_root)
            stat = path.stat()
            digest.update(rel.encode("utf-8"))
            digest.update(str(stat.st_size).encode("utf-8"))
            digest.update(_file_hash(path).encode("utf-8"))
        return digest.hexdigest()

    def check_databricks_readiness(self) -> DatabricksReadiness:
        if not self.cfg:
            return DatabricksReadiness(False, False, "duckdb", "config_unavailable")
        db_cfg = self.cfg.databricks
        if not db_cfg.enabled:
            return DatabricksReadiness(False, False, "duckdb", "databricks_disabled")
        if not db_cfg.is_active():
            return DatabricksReadiness(
                configured=True,
                active=False,
                execution=db_cfg.execution,
                message="databricks_enabled_but_credentials_missing",
            )
        try:
            from core.execution.databricks_client import DatabricksClient

            ok, msg = DatabricksClient(db_cfg).health_check()
            return DatabricksReadiness(
                configured=True,
                active=ok,
                execution=db_cfg.execution,
                message=msg,
            )
        except Exception as exc:
            return DatabricksReadiness(
                configured=True,
                active=False,
                execution=db_cfg.execution,
                message=f"databricks_health_check_failed:{type(exc).__name__}:{exc}",
            )

    def _fingerprint_inputs(self) -> list[Path]:
        workspace = self.layout.project_root
        candidates: list[Path] = []
        for folder in ["docs", "datasets"]:
            root = workspace / folder
            if root.exists():
                candidates.extend(path for path in root.rglob("*") if path.is_file())
        for key in ["editable_file", "sql_file"]:
            value = self.task.get(key)
            if value:
                path = self.repo_root / value
                if path.exists() and self.layout.interns_dir not in path.parents:
                    candidates.append(path)
        return sorted(set(candidates))

    def _is_current(self, manifest: dict[str, Any], fingerprint: str) -> tuple[bool, str]:
        if not manifest:
            return False, "bootstrap_manifest_missing"
        if manifest.get("fingerprint") != fingerprint:
            return False, "input_fingerprint_changed"
        missing = [
            name
            for name, path in self.required_artifacts().items()
            if not path.exists()
        ]
        if missing:
            return False, f"required_artifacts_missing:{','.join(missing)}"
        return True, "artifacts_current"

    def required_artifacts(self) -> dict[str, Path]:
        return {
            "solution": self.layout.solutions_dir / "kpi_metrics.sql",
            "experiment": self.layout.evaluation_dir / "experiment.py",
            "evaluator": self.layout.evaluation_dir / "evaluator.py",
            "semantic_contract": self.layout.contracts_dir / "semantic_contract.json",
            "domain_model": self.layout.contracts_dir / "domain_model.json",
            "profile_index": self.layout.profiles_dir / "profile_index.json",
        }

    def _read_manifest(self, path: Path) -> dict[str, Any]:
        if not path.exists():
            return {}
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            return {}

    def _write_status(self, result: BootstrapResult) -> None:
        status_path = self.layout.reports_dir / "bootstrap_status.json"
        status = result.summary()
        status_path.write_text(json.dumps(status, indent=2), encoding="utf-8")
        self.metadata_store.upsert(
            "bootstrap",
            "bootstrap_status",
            status,
            workspace=str(self.layout.project_root),
        )


def _file_hash(path: Path, chunk_size: int = 1024 * 1024) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(chunk_size), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _rel(path: Path, root: Path) -> str:
    try:
        return str(path.resolve().relative_to(root.resolve())).replace("\\", "/")
    except ValueError:
        return str(path)
