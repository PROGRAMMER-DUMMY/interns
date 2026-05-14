"""Generate reviewable Databricks Genie workspace setup specs.

This module does not mutate Databricks. It turns the Databricks asset manifest
into a deployment-ready specification, operator runbook, and evolution memory.
"""
from __future__ import annotations

import argparse
import json
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from core.onboarding.databricks_assets import DatabricksAssetManifestBuilder
from core.storage.workspace_layout import WorkspaceLayout


@dataclass(frozen=True)
class GenieWorkspaceSpecResult:
    spec_path: str
    runbook_path: str
    decisions_path: str
    evolution_path: str
    lessons_path: str
    folder_count: int
    genie_space_count: int
    prompt_count: int
    permission_count: int

    def summary(self) -> dict[str, Any]:
        return {
            "spec_path": self.spec_path,
            "runbook_path": self.runbook_path,
            "decisions_path": self.decisions_path,
            "evolution_path": self.evolution_path,
            "lessons_path": self.lessons_path,
            "folder_count": self.folder_count,
            "genie_space_count": self.genie_space_count,
            "prompt_count": self.prompt_count,
            "permission_count": self.permission_count,
        }


class GenieWorkspaceSpecBuilder:
    """Builds local Genie workspace setup artifacts from the asset manifest."""

    def __init__(
        self,
        repo_root: str | Path,
        workspace: str | Path,
        *,
        manifest_path: str | Path | None = None,
        environment: str = "dev",
        domain: str = "rcm",
        workspace_root: str = "/Workspace/Autoresearch",
        catalog: str = "dev",
        schema: str | None = None,
    ):
        self.repo_root = Path(repo_root).resolve()
        self.workspace = (self.repo_root / workspace).resolve()
        self.layout = WorkspaceLayout(project_root=self.workspace)
        self.manifest_path = (
            (self.repo_root / manifest_path).resolve()
            if manifest_path
            else self.layout.requirements_dir / "databricks_asset_manifest.json"
        )
        self.environment = environment
        self.domain = domain
        self.workspace_root = workspace_root
        self.catalog = catalog
        self.schema = schema

    def build(self) -> GenieWorkspaceSpecResult:
        self._validate_workspace()
        self.layout.ensure_runtime_dirs()
        manifest = self._load_or_create_manifest()
        spec = self._build_spec(manifest)
        spec_path = self.layout.requirements_dir / "genie_workspace_spec.json"
        runbook_path = self.layout.reports_dir / "genie_operator_runbook.md"
        decisions_path = self.layout.memory_dir / "genie_workspace_decisions.json"

        spec_path.write_text(json.dumps(spec, indent=2, default=str) + "\n", encoding="utf-8")
        runbook_path.write_text(self._runbook(spec), encoding="utf-8")
        decisions_path.write_text(
            json.dumps(self._accepted_decisions(spec), indent=2, default=str) + "\n",
            encoding="utf-8",
        )
        evolution_path, lessons_path = self._record_evolution(spec)

        return GenieWorkspaceSpecResult(
            spec_path=_rel(spec_path, self.repo_root),
            runbook_path=_rel(runbook_path, self.repo_root),
            decisions_path=_rel(decisions_path, self.repo_root),
            evolution_path=_rel(evolution_path, self.repo_root),
            lessons_path=_rel(lessons_path, self.repo_root),
            folder_count=len(spec["workspace_folders"]),
            genie_space_count=len(spec["genie_spaces"]),
            prompt_count=sum(len(space["starter_prompts"]) for space in spec["genie_spaces"]),
            permission_count=len(spec["permissions"]["role_action_matrix"]),
        )

    def _load_or_create_manifest(self) -> dict[str, Any]:
        if self.manifest_path.exists():
            return json.loads(self.manifest_path.read_text(encoding="utf-8"))
        DatabricksAssetManifestBuilder(
            self.repo_root,
            self.workspace,
            environment=self.environment,
            domain=self.domain,
            workspace_root=self.workspace_root,
            catalog=self.catalog,
            schema=self.schema,
        ).build()
        return json.loads(self.manifest_path.read_text(encoding="utf-8"))

    def _build_spec(self, manifest: dict[str, Any]) -> dict[str, Any]:
        base_path = manifest["databricks_workspace_path"]
        catalog = manifest["catalog"]
        schema = manifest["schema"]
        target_tables = [asset["target_fqn"] for asset in manifest.get("assets", [])]
        workspace_assets = manifest.get("workspace_assets", [])
        generated_at = datetime.now(timezone.utc).isoformat()

        folders = [
            base_path,
            f"{base_path}/sql",
            f"{base_path}/solutions",
            f"{base_path}/notebooks",
            f"{base_path}/evaluation",
            f"{base_path}/jobs",
            f"{base_path}/dashboards",
            f"{base_path}/genie",
            f"{base_path}/runbooks",
            f"{base_path}/evidence",
            f"{base_path}/contracts",
            f"{base_path}/requirements",
            f"{base_path}/reports",
        ]
        sql_assets = [
            {
                "name": Path(asset["target_path"]).stem,
                "source_path": asset["source_path"],
                "target_path": asset["target_path"],
                "source_sha256": asset.get("source_sha256"),
                "edit_policy": asset.get("edit_policy"),
            }
            for asset in workspace_assets
            if asset.get("asset_type") == "sql"
        ]
        jobs = [
            {
                "name": f"autoresearch_{manifest['environment']}_{manifest['domain']}_kpi_validation",
                "target_path": f"{base_path}/jobs/kpi_validation",
                "type": "databricks_workflow",
                "trigger": "manual_or_ci_after_approval",
                "execution_backend": "sql_warehouse_or_jobs_api",
                "inputs": {
                    "catalog": catalog,
                    "schema": schema,
                    "sql_assets": [asset["target_path"] for asset in sql_assets],
                },
                "evidence_outputs": [
                    f"{catalog}.{schema}.autoresearch_run_evidence",
                    f"{catalog}.{schema}.autoresearch_governance_decisions",
                    "mlflow_experiment:/Users/<owner>/autoresearch",
                ],
            }
        ]
        dashboards = [
            {
                "name": f"{manifest['domain'].upper()} KPI Governance",
                "target_path": f"{base_path}/dashboards/kpi_governance",
                "type": "lakeview_or_workspace_dashboard",
                "sources": [
                    f"{catalog}.{schema}.autoresearch_run_evidence",
                    f"{catalog}.{schema}.autoresearch_governance_decisions",
                ],
                "purpose": "Review KPI readiness, failed gates, run evidence, and promotion state.",
            }
        ]
        genie_spaces = [
            {
                "name": f"autoresearch-{manifest['environment']}-{manifest['domain']}-operator",
                "target_path": f"{base_path}/genie/kpi_operator",
                "description": (
                    "Interactive Genie space for searching RCM data assets, explaining KPI blockers, "
                    "and guiding Databricks validation workflows."
                ),
                "allowed_tables": target_tables,
                "allowed_workspace_paths": folders,
                "guardrails": [
                    "Do not treat Genie answers as source of truth.",
                    "Do not execute production writes without human approval.",
                    "Use Unity Catalog metadata and generated contracts before assumptions.",
                    "Record unresolved KPI semantics in open questions.",
                ],
                "starter_prompts": _starter_prompts(manifest, target_tables),
            }
        ]
        permissions = {
            "model": "least_privilege_role_action_matrix",
            "role_action_matrix": [
                {
                    "role": "platform_admin",
                    "allowed": [
                        "manage_workspace_paths",
                        "deploy_manifest_assets",
                        "approve_prod_promotion",
                        "manage_permissions",
                    ],
                    "approval_required": "prod_mutation",
                },
                {
                    "role": "domain_engineer",
                    "allowed": [
                        "edit_dev_stage_assets",
                        "trigger_validation_jobs",
                        "inspect_lineage",
                        "update_manifest_via_git",
                    ],
                    "approval_required": "stage_to_prod_promotion",
                },
                {
                    "role": "domain_analyst",
                    "allowed": [
                        "ask_genie",
                        "run_read_only_queries",
                        "view_dashboards",
                        "comment_on_open_questions",
                    ],
                    "approval_required": "write_actions",
                },
                {
                    "role": "governance_reviewer",
                    "allowed": [
                        "review_evidence_packs",
                        "approve_or_reject_promotion",
                        "inspect_access_policy",
                    ],
                    "approval_required": "none",
                },
                {
                    "role": "api_agent_ci",
                    "allowed": [
                        "deploy_approved_workspace_assets",
                        "run_jobs_api",
                        "run_statement_execution_api",
                        "write_delta_evidence",
                    ],
                    "approval_required": "remote_execution_flag_and_ci_policy",
                },
            ],
        }
        return {
            "version": 1,
            "generated_at": generated_at,
            "workspace": manifest["workspace"],
            "environment": manifest["environment"],
            "domain": manifest["domain"],
            "source_manifest": _rel(self.manifest_path, self.repo_root),
            "databricks_workspace_path": base_path,
            "catalog": catalog,
            "schema": schema,
            "workspace_folders": folders,
            "sql_assets": sql_assets,
            "jobs": jobs,
            "dashboards": dashboards,
            "genie_spaces": genie_spaces,
            "permissions": permissions,
            "non_genie_fallback": manifest["non_genie_fallback"],
            "drift_detection": {
                "strategy": "source_sha256_manifest_comparison",
                "tracked_workspace_assets": [
                    {
                        "source_path": asset["source_path"],
                        "target_path": asset["target_path"],
                        "source_sha256": asset.get("source_sha256"),
                        "edit_policy": asset.get("edit_policy"),
                    }
                    for asset in workspace_assets
                ],
            },
            "remote_mutation": {
                "enabled_by_this_generator": False,
                "next_step": "future_deployer_can_apply_this_spec_after_explicit_approval",
            },
        }

    def _runbook(self, spec: dict[str, Any]) -> str:
        space = spec["genie_spaces"][0]
        prompts = "\n".join(f"{idx}. {prompt}" for idx, prompt in enumerate(space["starter_prompts"], 1))
        folders = "\n".join(f"- `{folder}`" for folder in spec["workspace_folders"])
        tables = "\n".join(f"- `{table}`" for table in space["allowed_tables"][:20])
        return "\n".join(
            [
                "# Genie Operator Runbook",
                "",
                f"- Environment: `{spec['environment']}`",
                f"- Domain: `{spec['domain']}`",
                f"- Workspace path: `{spec['databricks_workspace_path']}`",
                f"- Genie space: `{space['target_path']}`",
                "",
                "## Workspace Folders",
                "",
                folders,
                "",
                "## Allowed Tables",
                "",
                tables or "- No Unity Catalog tables are registered in the manifest yet.",
                "",
                "## Starter Prompts",
                "",
                prompts,
                "",
                "## Operating Rules",
                "",
                "- Genie may search, inspect, explain, and guide workspace operations.",
                "- Genie is not the source of truth; Git manifests and Unity Catalog evidence are.",
                "- Production writes and remote deployment require explicit approval.",
                "- Non-Genie API/CI fallback must remain capable of deploying approved assets.",
                "",
            ]
        )

    def _accepted_decisions(self, spec: dict[str, Any]) -> dict[str, Any]:
        return {
            "topic": "genie_workspace_setup",
            "outcome": "accepted",
            "accepted_at": spec["generated_at"],
            "decisions": [
                "Generate reviewable Genie workspace deployment specs before remote mutation.",
                "Store specs and runbooks under workspace interns artifacts.",
                "Use environment/domain Databricks paths.",
                "Represent permissions as a least-privilege role-action matrix.",
                "Generate domain-specific starter prompts.",
                "Record accepted choices in structured and markdown evolution memory.",
                "Keep non-Genie API/CI fallback available for every Genie-assisted workflow.",
            ],
            "spec_path": "genie_workspace_spec.json",
            "runbook_path": "genie_operator_runbook.md",
        }

    def _record_evolution(self, spec: dict[str, Any]) -> tuple[Path, Path]:
        evolution_path = self.layout.memory_dir / "evolution.md"
        lessons_path = self.layout.memory_dir / "lessons.json"
        title = f"## [{datetime.now(timezone.utc).date()}] Genie Workspace Setup"
        entry = "\n".join(
            [
                title,
                "",
                "**Trigger**: User accepted Option B and requested implementation of Genie workspace setup.",
                "**Assumption**: Remote Databricks mutation should remain deferred until specs are reviewed.",
                "**Outcome**: Accepted",
                (
                    "**Lesson**: Generate deployment specs, runbooks, permissions, prompts, "
                    "fallback contracts, and drift evidence before calling Databricks APIs."
                ),
                "**Applies To**: governance / Databricks / Genie / deployment / workflow",
                "",
            ]
        )
        existing = evolution_path.read_text(encoding="utf-8") if evolution_path.exists() else ""
        if "Genie Workspace Setup" not in existing:
            evolution_path.parent.mkdir(parents=True, exist_ok=True)
            evolution_path.write_text(existing.rstrip() + "\n\n" + entry, encoding="utf-8")

        lessons = []
        if lessons_path.exists():
            try:
                lessons = json.loads(lessons_path.read_text(encoding="utf-8"))
            except json.JSONDecodeError:
                lessons = []
        lesson = {
            "topic": "genie_workspace_setup",
            "outcome": "accepted",
            "timestamp": spec["generated_at"],
            "lesson": (
                "Use local deployment specs and evolution memory before enabling remote "
                "Databricks/Genie mutation."
            ),
            "applies_to": ["governance", "databricks", "genie", "deployment"],
        }
        if not any(item.get("topic") == lesson["topic"] for item in lessons if isinstance(item, dict)):
            lessons.append(lesson)
            lessons_path.write_text(json.dumps(lessons, indent=2) + "\n", encoding="utf-8")
        return evolution_path, lessons_path

    def _validate_workspace(self) -> None:
        if not self.workspace.exists():
            raise FileNotFoundError(f"workspace not found: {self.workspace}")
        if self.workspace == self.repo_root or not self.workspace.is_relative_to(self.repo_root):
            raise ValueError(f"workspace must be inside repo root: {self.workspace}")


def _starter_prompts(manifest: dict[str, Any], target_tables: list[str]) -> list[str]:
    table_hint = target_tables[0] if target_tables else f"{manifest['catalog']}.{manifest['schema']}.*"
    return [
        (
            "Find the KPI feature mapping blockers for this workspace and summarize the highest-risk "
            "financial and temporal unresolved fields."
        ),
        (
            f"Search Unity Catalog under `{manifest['catalog']}.{manifest['schema']}` and identify "
            "which tables could support the unresolved RCM KPI features."
        ),
        (
            "Inspect the generated contracts and explain which KPIs are ready for Databricks SQL "
            "and which require stakeholder clarification."
        ),
        (
            f"Use `{table_hint}` as a starting point and propose a read-only validation query for "
            "one KPI without changing production data."
        ),
        (
            "Trace the workspace assets, jobs, SQL files, and evidence paths needed to promote a "
            "KPI from dev to stage."
        ),
        (
            "If a Databricks job or SQL query fails, summarize the likely cause, the relevant logs, "
            "and the next safe action without bypassing approval gates."
        ),
    ]


def _rel(path: Path, root: Path) -> str:
    try:
        return str(path.resolve().relative_to(root.resolve())).replace("\\", "/")
    except ValueError:
        return str(path)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Create a local Genie workspace setup spec.")
    parser.add_argument("--workspace", required=True, help="Workspace path relative to repo root.")
    parser.add_argument("--repo-root", default=".", help="Repository root. Defaults to current directory.")
    parser.add_argument("--manifest-path", help="Existing Databricks asset manifest path.")
    parser.add_argument("--environment", default="dev")
    parser.add_argument("--domain", default="rcm")
    parser.add_argument("--workspace-root", default="/Workspace/Autoresearch")
    parser.add_argument("--catalog", default="dev")
    parser.add_argument("--schema", help="Target schema. Defaults to --domain when manifest is generated.")
    args = parser.parse_args(argv)
    result = GenieWorkspaceSpecBuilder(
        args.repo_root,
        args.workspace,
        manifest_path=args.manifest_path,
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
