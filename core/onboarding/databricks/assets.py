"""Databricks asset manifest generation for workspace datasets."""
from __future__ import annotations

import argparse
import hashlib
import json
import re
from collections import Counter
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from core.storage.workspace_layout import WorkspaceLayout


@dataclass(frozen=True)
class DatabricksAssetManifestResult:
    path: str
    dataset_count: int
    workspace_asset_count: int
    environment: str
    domain: str
    workspace_path: str
    catalog: str
    schema: str

    def summary(self) -> dict[str, Any]:
        from core.onboarding.workspace.delegation import routing_for

        _routing = routing_for("remote_execution")
        return {
            "path": self.path,
            "dataset_count": self.dataset_count,
            "workspace_asset_count": self.workspace_asset_count,
            "environment": self.environment,
            "domain": self.domain,
            "workspace_path": self.workspace_path,
            "catalog": self.catalog,
            "schema": self.schema,
            "required_specialists": _routing["agents"],
            "suggested_skills": _routing["skills"],
        }


class DatabricksAssetManifestBuilder:
    """Builds a governed upload/register plan without moving raw data."""

    def __init__(
        self,
        repo_root: str | Path,
        workspace: str | Path,
        *,
        environment: str = "dev",
        domain: str = "rcm",
        workspace_root: str = "/Workspace/Autoresearch",
        catalog: str = "dev",
        schema: str | None = None,
    ):
        self.repo_root = Path(repo_root).resolve()
        self.workspace = (self.repo_root / workspace).resolve()
        self.layout = WorkspaceLayout(project_root=self.workspace)
        self.environment = _safe_name(environment).lower()
        self.domain = _safe_name(domain).lower()
        self.workspace_root = workspace_root.rstrip("/") or "/Workspace/Autoresearch"
        self.catalog = catalog
        self.schema = schema or self.domain

    def build(self) -> DatabricksAssetManifestResult:
        profile_index = self.layout.profiles_dir / "profile_index.json"
        profiles = []
        if profile_index.exists():
            profiles = json.loads(profile_index.read_text(encoding="utf-8")).get("profiles", [])
        stem_counts = Counter(
            _safe_name(Path(profile.get("path", "")).stem)
            for profile in profiles
            if profile.get("path")
        )
        assets = []
        for profile in profiles:
            path = profile.get("path")
            if not path:
                continue
            table = _table_name(path, stem_counts)
            assets.append({
                "source_path": path,
                "source_format": profile.get("format", "unknown"),
                "target_catalog": self.catalog,
                "target_schema": self.schema,
                "target_table": table,
                "target_fqn": f"{self.catalog}.{self.schema}.{table}",
                "environment": self.environment,
                "domain": self.domain,
                "registration_state": "requires_user_approved_upload_or_table_registration",
                "inspection_mode": "metadata_schema_profiler_first",
                "source_of_truth": "unity_catalog_after_registration",
            })
        workspace_assets = self._workspace_assets()
        databricks_workspace_path = (
            f"{self.workspace_root}/{self.environment}/{self.domain}"
        )

        payload = {
            "workspace": _rel(self.workspace, self.repo_root),
            "operating_model": "federated_enterprise",
            "environment_model": "shared_non_prod_workspace_with_isolated_prod_workspace",
            "environment": self.environment,
            "domain": self.domain,
            "databricks_workspace_path": databricks_workspace_path,
            "catalog": self.catalog,
            "schema": self.schema,
            "source_of_truth": {
                "git": [
                    "engine_code",
                    "tests",
                    "policies",
                    "skills",
                    "deployment_manifests",
                ],
                "databricks": [
                    "unity_catalog_tables",
                    "jobs",
                    "notebooks",
                    "dashboards",
                    "genie_spaces",
                    "mlflow_traces",
                    "delta_evidence_tables",
                ],
                "rule": (
                    "Databricks operational assets must be registered in this manifest; "
                    "repo-managed assets use source hashes for drift detection."
                ),
            },
            "execution_truth": {
                "enterprise_backend": "databricks",
                "local_backend_role": "syntax_layout_and_tiny_fixture_smoke_tests_only",
                "promotion_from_local_execution_allowed": False,
            },
            "assets": assets,
            "workspace_assets": workspace_assets,
            "promotion_path": [
                {
                    "from": "dev",
                    "to": "stage",
                    "required_gates": [
                        "semantic_contract",
                        "correctness",
                        "lineage_impact",
                        "performance",
                        "cost",
                    ],
                },
                {
                    "from": "stage",
                    "to": "prod",
                    "required_gates": [
                        "semantic_contract",
                        "correctness",
                        "lineage_impact",
                        "performance",
                        "cost",
                        "access_policy",
                        "human_approval",
                    ],
                },
            ],
            "genie_role": {
                "mode": "interactive_workspace_operator",
                "allowed": [
                    "search_assets",
                    "inspect_metadata",
                    "open_or_create_workspace_assets",
                    "debug_jobs_queries_and_pipelines",
                    "summarize_lineage_and_results",
                ],
                "not_source_of_truth": True,
            },
            "non_genie_fallback": {
                "mode": "api_agent_or_ci",
                "required_surfaces": [
                    "workspace_files_api",
                    "jobs_api",
                    "statement_execution_api",
                    "unity_catalog_api",
                    "mlflow_tracking",
                    "delta_metadata_tables",
                ],
            },
            "approval_required": True,
            "notes": [
                "This manifest does not upload raw data or workspace files.",
                "Remote registration and workspace deployment require explicit approval.",
                "DuckDB/local execution is a developer smoke-test path only, not enterprise evidence.",
            ],
        }
        output = self.layout.requirements_dir / "databricks_asset_manifest.json"
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
        return DatabricksAssetManifestResult(
            path=_rel(output, self.repo_root),
            dataset_count=len(assets),
            workspace_asset_count=len(workspace_assets),
            environment=self.environment,
            domain=self.domain,
            workspace_path=databricks_workspace_path,
            catalog=self.catalog,
            schema=self.schema,
        )

    def _workspace_assets(self) -> list[dict[str, Any]]:
        roots = [
            (self.layout.solutions_dir, "solutions"),
            (self.layout.evaluation_dir, "evaluation"),
            (self.layout.contracts_dir, "contracts"),
            (self.layout.requirements_dir, "requirements"),
            (self.layout.reports_dir, "reports"),
        ]
        assets: list[dict[str, Any]] = []
        base_path = f"{self.workspace_root}/{self.environment}/{self.domain}"
        for root, kind in roots:
            if not root.exists():
                continue
            for path in sorted(item for item in root.rglob("*") if item.is_file()):
                rel_to_root = path.relative_to(root).as_posix()
                target_path = f"{base_path}/{kind}/{rel_to_root}"
                assets.append(
                    {
                        "source_path": _rel(path, self.repo_root),
                        "target_path": target_path,
                        "asset_type": _asset_type(path, kind),
                        "environment": self.environment,
                        "domain": self.domain,
                        "source_sha256": _file_hash(path),
                        "source_of_truth": "repo_generated_artifact",
                        "edit_policy": _edit_policy(kind),
                        "deployment_state": "requires_user_approved_workspace_import",
                    }
                )
        return assets


def _safe_name(value: str) -> str:
    return re.sub(r"[^A-Za-z0-9_]+", "_", value).strip("_") or "table"


def _table_name(path: str, stem_counts: Counter[str]) -> str:
    stem = _safe_name(Path(path).stem)
    if stem_counts[stem] <= 1:
        return stem.lower()
    without_suffix = Path(path).with_suffix("")
    parts = [_safe_name(part) for part in without_suffix.parts[-3:]]
    return _safe_name("_".join(part for part in parts if part)).lower()


def _asset_type(path: Path, kind: str) -> str:
    if path.suffix == ".sql":
        return "sql"
    if path.suffix == ".py":
        return "python"
    if path.suffix == ".json":
        return kind.rstrip("s") + "_json"
    if path.suffix in {".md", ".txt"}:
        return kind.rstrip("s") + "_document"
    return kind.rstrip("s") + "_file"


def _edit_policy(kind: str) -> str:
    if kind in {"solutions", "evaluation", "contracts"}:
        return "repo_managed_drift_detection_required"
    return "databricks_operational_edits_allowed_with_manifest_update"


def _file_hash(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _rel(path: Path, root: Path) -> str:
    try:
        return str(path.resolve().relative_to(root.resolve())).replace("\\", "/")
    except ValueError:
        return str(path)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Create a Databricks asset registration manifest.")
    parser.add_argument("--workspace", required=True, help="Workspace path relative to repo root.")
    parser.add_argument("--repo-root", default=".", help="Repository root. Defaults to current directory.")
    parser.add_argument("--environment", default="dev", help="Target environment: dev, stage, or prod.")
    parser.add_argument("--domain", default="rcm", help="Domain/team boundary, for example rcm.")
    parser.add_argument("--workspace-root", default="/Workspace/Autoresearch")
    parser.add_argument("--catalog", default="dev")
    parser.add_argument("--schema", help="Target schema. Defaults to --domain.")
    args = parser.parse_args(argv)
    result = DatabricksAssetManifestBuilder(
        args.repo_root,
        args.workspace,
        environment=args.environment,
        domain=args.domain,
        workspace_root=args.workspace_root,
        catalog=args.catalog,
        schema=args.schema,
    ).build()
    print(json.dumps(result.summary(), indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
