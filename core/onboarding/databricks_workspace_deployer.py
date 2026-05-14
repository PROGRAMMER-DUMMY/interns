"""Dry-run and guarded deployment for Databricks workspace specs."""
from __future__ import annotations

import argparse
import base64
import json
import os
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Protocol

from core.config import load as load_config
from core.execution.databricks_client import DatabricksClient
from core.storage.workspace_layout import WorkspaceLayout


@dataclass(frozen=True)
class DeploymentOperation:
    op_id: str
    action: str
    target: str
    state: str
    apply_supported: bool
    approval_required: bool = True
    source: str | None = None
    payload: dict[str, Any] = field(default_factory=dict)
    reason: str = ""

    def as_dict(self) -> dict[str, Any]:
        return {
            "op_id": self.op_id,
            "action": self.action,
            "target": self.target,
            "state": self.state,
            "apply_supported": self.apply_supported,
            "approval_required": self.approval_required,
            "source": self.source,
            "payload": self.payload,
            "reason": self.reason,
        }


@dataclass(frozen=True)
class DatabricksWorkspaceDeploymentResult:
    plan_path: str
    report_path: str
    central_report_path: str
    deployment_index_path: str
    applied: bool
    operation_count: int
    apply_supported_count: int
    spec_only_count: int
    skipped_count: int = 0
    failed_count: int = 0

    def summary(self) -> dict[str, Any]:
        return {
            "plan_path": self.plan_path,
            "report_path": self.report_path,
            "central_report_path": self.central_report_path,
            "deployment_index_path": self.deployment_index_path,
            "applied": self.applied,
            "operation_count": self.operation_count,
            "apply_supported_count": self.apply_supported_count,
            "spec_only_count": self.spec_only_count,
            "skipped_count": self.skipped_count,
            "failed_count": self.failed_count,
        }


class WorkspaceApi(Protocol):
    def mkdirs(self, path: str) -> None: ...
    def upload_file(self, path: str, source: Path) -> None: ...
    def ensure_schema(self, catalog: str, schema: str) -> None: ...
    def execute_sql(self, statement: str) -> None: ...


class DatabricksWorkspaceApi:
    """Small adapter over databricks-sdk workspace APIs."""

    def __init__(self, db_client: DatabricksClient):
        self.db_client = db_client
        self.client = db_client.get_client()

    def mkdirs(self, path: str) -> None:
        self.client.workspace.mkdirs(path)

    def upload_file(self, path: str, source: Path) -> None:
        data = source.read_bytes()
        workspace = self.client.workspace
        if hasattr(workspace, "upload"):
            workspace.upload(path, data, overwrite=True)
            return
        try:
            from databricks.sdk.service.workspace import ImportFormat, Language

            kwargs: dict[str, Any] = {
                "path": path,
                "content": base64.b64encode(data).decode("ascii"),
                "overwrite": True,
            }
            suffix = source.suffix.lower()
            if suffix in {".py", ".sql"}:
                kwargs["format"] = ImportFormat.SOURCE
                kwargs["language"] = Language.PYTHON if suffix == ".py" else Language.SQL
            else:
                kwargs["format"] = ImportFormat.AUTO
            workspace.import_(**kwargs)
        except Exception as exc:
            raise RuntimeError(f"workspace_file_upload_failed:{path}:{exc}") from exc

    def ensure_schema(self, catalog: str, schema: str) -> None:
        self.db_client.ensure_delta_schema(catalog, schema)

    def execute_sql(self, statement: str) -> None:
        warehouse_id = self.db_client._extract_warehouse_id()
        if not warehouse_id:
            raise RuntimeError("DATABRICKS_HTTP_PATH is required for SQL deployment operations")
        self.client.statement_execution.execute_statement(
            warehouse_id=warehouse_id,
            statement=statement,
            wait_timeout="30s",
        )

    def post_api(self, path: str, payload: dict[str, Any]) -> dict[str, Any]:
        url = self.db_client.cfg.host.rstrip("/") + path
        request = urllib.request.Request(
            url,
            data=json.dumps(payload).encode("utf-8"),
            headers={
                "Authorization": f"Bearer {self.db_client.cfg.token}",
                "Content-Type": "application/json",
            },
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=self.db_client.cfg.http_timeout_sec) as response:
                body = response.read().decode("utf-8")
        except urllib.error.HTTPError as exc:
            body = exc.read().decode("utf-8", errors="replace")
            raise RuntimeError(f"databricks_api_failed:{path}:{exc.code}:{body}") from exc
        return json.loads(body) if body else {}


class DatabricksWorkspaceDeploymentPlanner:
    def __init__(
        self,
        repo_root: str | Path,
        workspace: str | Path,
        *,
        spec_path: str | Path | None = None,
    ):
        self.repo_root = Path(repo_root).resolve()
        self.workspace = (self.repo_root / workspace).resolve()
        self.layout = WorkspaceLayout(project_root=self.workspace)
        self.spec_path = (
            (self.repo_root / spec_path).resolve()
            if spec_path
            else self.layout.requirements_dir / "genie_workspace_spec.json"
        )

    def build_plan(self) -> dict[str, Any]:
        self._validate_workspace()
        spec = self._load_spec()
        operations: list[DeploymentOperation] = []
        operations.extend(self._folder_operations(spec))
        operations.extend(self._workspace_file_operations(spec))
        operations.extend(self._unity_catalog_operations(spec))
        operations.extend(self._job_operations(spec))
        operations.extend(self._dashboard_operations(spec))
        operations.extend(self._genie_operations(spec))
        return {
            "version": 1,
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "workspace": _rel(self.workspace, self.repo_root),
            "source_spec": _rel(self.spec_path, self.repo_root),
            "environment": spec["environment"],
            "domain": spec["domain"],
            "databricks_workspace_path": spec["databricks_workspace_path"],
            "catalog": spec["catalog"],
            "schema": spec["schema"],
            "remote_mutation_default": "disabled",
            "approval_required": True,
            "apply_requirements": [
                "Pass --apply",
                "Pass --confirm-remote-mutation",
                "Set AUTORESEARCH_ALLOW_REMOTE_EXECUTION=1",
                "Configure DATABRICKS_HOST and DATABRICKS_TOKEN",
            ],
            "operations": [operation.as_dict() for operation in operations],
            "summary": _summary(operations),
        }

    def write_plan(self, plan: dict[str, Any], *, applied: bool = False) -> tuple[Path, Path, Path, Path]:
        self.layout.ensure_runtime_dirs()
        plan_path = self.layout.requirements_dir / "databricks_workspace_deployment_plan.json"
        report_path = self.layout.reports_dir / (
            "databricks_workspace_deployment_apply.md"
            if applied
            else "databricks_workspace_deployment_dry_run.md"
        )
        plan_path.write_text(json.dumps(plan, indent=2, default=str) + "\n", encoding="utf-8")
        report = _report(plan, applied=applied)
        report_path.write_text(report, encoding="utf-8")
        central_report_path, deployment_index_path = self._write_central_deployment_record(
            plan,
            report,
            plan_path=plan_path,
            report_path=report_path,
            applied=applied,
        )
        self._record_evolution(plan, applied=applied)
        return plan_path, report_path, central_report_path, deployment_index_path

    def _write_central_deployment_record(
        self,
        plan: dict[str, Any],
        report: str,
        *,
        plan_path: Path,
        report_path: Path,
        applied: bool,
    ) -> tuple[Path, Path]:
        state_dir = self.repo_root / "state" / "databricks" / "deployments"
        workspace_slug = _slug(plan["workspace"])
        run_id = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        run_dir = state_dir / workspace_slug / run_id
        run_dir.mkdir(parents=True, exist_ok=True)

        central_plan_path = run_dir / "plan.json"
        central_report_path = run_dir / (
            "apply.md" if applied else "dry_run.md"
        )
        central_plan_path.write_text(json.dumps(plan, indent=2, default=str) + "\n", encoding="utf-8")
        central_report_path.write_text(report, encoding="utf-8")

        latest_report_path = state_dir / "latest.md"
        latest_report_path.write_text(report, encoding="utf-8")

        deployment_index_path = state_dir / "deployment_index.json"
        index = _read_json(deployment_index_path, default={"version": 1, "deployments": []})
        summary = plan["summary"]
        apply_result = plan.get("apply_result", {})
        index["latest"] = {
            "workspace": plan["workspace"],
            "environment": plan["environment"],
            "domain": plan["domain"],
            "catalog": plan["catalog"],
            "schema": plan["schema"],
            "applied": applied,
            "generated_at": plan["generated_at"],
            "recorded_at": datetime.now(timezone.utc).isoformat(),
            "operation_count": summary["operation_count"],
            "apply_supported_count": summary["apply_supported_count"],
            "spec_only_count": summary["spec_only_count"],
            "applied_count": apply_result.get("applied_count", 0),
            "failed_count": apply_result.get("failed_count", 0),
            "skipped_count": apply_result.get("skipped_count", 0),
            "workspace_plan_path": _rel(plan_path, self.repo_root),
            "workspace_report_path": _rel(report_path, self.repo_root),
            "central_plan_path": _rel(central_plan_path, self.repo_root),
            "central_report_path": _rel(central_report_path, self.repo_root),
        }
        deployments = index.setdefault("deployments", [])
        deployments.append(index["latest"])
        index["deployments"] = deployments[-50:]
        deployment_index_path.write_text(json.dumps(index, indent=2) + "\n", encoding="utf-8")
        return central_report_path, deployment_index_path

    def _folder_operations(self, spec: dict[str, Any]) -> list[DeploymentOperation]:
        return [
            DeploymentOperation(
                op_id=f"folder:{idx:03d}",
                action="create_workspace_folder",
                target=folder,
                state="planned",
                apply_supported=True,
                reason="Workspace path from Genie deployment spec.",
            )
            for idx, folder in enumerate(spec.get("workspace_folders", []), start=1)
        ]

    def _workspace_file_operations(self, spec: dict[str, Any]) -> list[DeploymentOperation]:
        operations = []
        for idx, asset in enumerate(spec.get("drift_detection", {}).get("tracked_workspace_assets", []), start=1):
            source_path = asset.get("source_path", "")
            target_path = asset.get("target_path", "")
            source = self.repo_root / source_path
            operations.append(
                DeploymentOperation(
                    op_id=f"file:{idx:03d}",
                    action="deploy_workspace_file",
                    target=target_path,
                    source=source_path,
                    state="planned" if source.exists() else "blocked_missing_source",
                    apply_supported=source.exists(),
                    payload={
                        "source_sha256": asset.get("source_sha256"),
                        "edit_policy": asset.get("edit_policy"),
                    },
                    reason="Repo-generated workspace asset with drift hash.",
                )
            )
        return operations

    def _unity_catalog_operations(self, spec: dict[str, Any]) -> list[DeploymentOperation]:
        catalog = spec["catalog"]
        schema = spec["schema"]
        evidence_tables = _evidence_tables(catalog, schema)
        operations = [
            DeploymentOperation(
                op_id="uc:001",
                action="ensure_unity_catalog_schema",
                target=f"{catalog}.{schema}",
                state="planned",
                apply_supported=True,
                payload={
                    "allowed_tables": spec["genie_spaces"][0].get("allowed_tables", []),
                    "reason": "Schema is required before evidence tables or registered datasets are deployed.",
                },
                reason="Ensure target Unity Catalog schema exists.",
            )
        ]
        for idx, table in enumerate(evidence_tables, start=2):
            operations.append(
                DeploymentOperation(
                    op_id=f"uc:{idx:03d}",
                    action="create_evidence_table",
                    target=table["target_fqn"],
                    state="planned",
                    apply_supported=True,
                    payload={"sql": table["sql"], "purpose": table["purpose"]},
                    reason="Create governed Delta evidence table used by deployment and review.",
                )
            )
        operations.append(
            DeploymentOperation(
                op_id=f"uc:{len(operations) + 1:03d}",
                action="prepare_dataset_registration",
                target=f"{catalog}.{schema}",
                state="spec_only",
                apply_supported=False,
                payload={
                    "allowed_tables": spec["genie_spaces"][0].get("allowed_tables", []),
                    "reason": (
                        "Raw dataset upload, external location use, or CREATE TABLE AS SELECT "
                        "requires data owner approval and storage-location policy."
                    ),
                },
                reason="Dataset registration remains spec-only until data movement policy is approved.",
            )
        )
        return operations

    def _job_operations(self, spec: dict[str, Any]) -> list[DeploymentOperation]:
        return [
            DeploymentOperation(
                op_id=f"job:{idx:03d}",
                action="prepare_job_payload",
                target=job["target_path"],
                state="spec_only",
                apply_supported=False,
                payload={**job, "api_payload": _job_api_payload(job, spec)},
                reason="Job API payload is prepared; compute policy must be reviewed before creation.",
            )
            for idx, job in enumerate(spec.get("jobs", []), start=1)
        ]

    def _dashboard_operations(self, spec: dict[str, Any]) -> list[DeploymentOperation]:
        return [
            DeploymentOperation(
                op_id=f"dashboard:{idx:03d}",
                action="prepare_dashboard_spec",
                target=dashboard["target_path"],
                state="spec_only",
                apply_supported=False,
                payload=dashboard,
                reason="Dashboard creation remains spec-only until dashboard API target is confirmed.",
            )
            for idx, dashboard in enumerate(spec.get("dashboards", []), start=1)
        ]

    def _genie_operations(self, spec: dict[str, Any]) -> list[DeploymentOperation]:
        return [
            DeploymentOperation(
                op_id=f"genie:{idx:03d}",
                action="prepare_genie_space_spec",
                target=space["target_path"],
                state="spec_only",
                apply_supported=False,
                payload=space,
                reason="Genie space setup remains manual/spec-only until workspace API support is confirmed.",
            )
            for idx, space in enumerate(spec.get("genie_spaces", []), start=1)
        ]

    def _load_spec(self) -> dict[str, Any]:
        if not self.spec_path.exists():
            raise FileNotFoundError(f"Genie workspace spec not found: {self.spec_path}")
        return json.loads(self.spec_path.read_text(encoding="utf-8"))

    def _validate_workspace(self) -> None:
        if not self.workspace.exists():
            raise FileNotFoundError(f"workspace not found: {self.workspace}")
        if self.workspace == self.repo_root or not self.workspace.is_relative_to(self.repo_root):
            raise ValueError(f"workspace must be inside repo root: {self.workspace}")

    def _record_evolution(self, plan: dict[str, Any], *, applied: bool) -> None:
        evolution_path = self.layout.memory_dir / "evolution.md"
        lesson = (
            "Remote Databricks deployment must stay approval-gated; dry-run plans are the "
            "review boundary before workspace mutation."
        )
        title = f"## [{datetime.now(timezone.utc).date()}] Databricks Workspace Deployer"
        entry = "\n".join(
            [
                title,
                "",
                "**Trigger**: User approved implementing the deployer boundary after Genie workspace specs.",
                "**Assumption**: Folder/file deployment may be automated after explicit approval; Genie, dashboards, jobs, and UC registration remain spec-gated.",
                f"**Outcome**: {'Applied' if applied else 'Planned'}",
                f"**Lesson**: {lesson}",
                "**Applies To**: Databricks / deployment / governance / Genie / CI",
                "",
            ]
        )
        existing = evolution_path.read_text(encoding="utf-8") if evolution_path.exists() else ""
        if "Databricks Workspace Deployer" not in existing:
            evolution_path.parent.mkdir(parents=True, exist_ok=True)
            evolution_path.write_text(existing.rstrip() + "\n\n" + entry, encoding="utf-8")


class DatabricksWorkspaceDeployer:
    def __init__(self, api: WorkspaceApi):
        self.api = api

    def apply(self, plan: dict[str, Any]) -> dict[str, Any]:
        applied = []
        failed = []
        skipped = []
        for operation in plan.get("operations", []):
            if not operation.get("apply_supported"):
                skipped.append({**operation, "result": "spec_only"})
                continue
            try:
                action = operation["action"]
                if action == "create_workspace_folder":
                    self.api.mkdirs(operation["target"])
                elif action == "deploy_workspace_file":
                    source = Path(operation["source"])
                    if not source.is_absolute():
                        source = Path.cwd() / source
                    self.api.upload_file(operation["target"], source)
                elif action == "ensure_unity_catalog_schema":
                    catalog, schema = operation["target"].split(".", 1)
                    self.api.ensure_schema(catalog, schema)
                elif action == "create_evidence_table":
                    self.api.execute_sql(operation["payload"]["sql"])
                else:
                    skipped.append({**operation, "result": "unsupported_action"})
                    continue
                applied.append({**operation, "result": "applied"})
            except Exception as exc:
                failed.append({**operation, "result": "failed", "error": str(exc)})
        plan["apply_result"] = {
            "applied": applied,
            "skipped": skipped,
            "failed": failed,
            "applied_count": len(applied),
            "skipped_count": len(skipped),
            "failed_count": len(failed),
        }
        return plan


def run_deployment(
    repo_root: str | Path,
    workspace: str | Path,
    *,
    spec_path: str | Path | None = None,
    apply: bool = False,
    confirm_remote_mutation: bool = False,
) -> DatabricksWorkspaceDeploymentResult:
    planner = DatabricksWorkspaceDeploymentPlanner(repo_root, workspace, spec_path=spec_path)
    plan = planner.build_plan()
    if apply:
        _require_remote_approval(confirm_remote_mutation)
        cfg = load_config()
        db_client = DatabricksClient(cfg.databricks)
        ok, msg = db_client.health_check()
        if not ok:
            raise RuntimeError(f"Databricks unavailable: {msg}")
        plan = DatabricksWorkspaceDeployer(DatabricksWorkspaceApi(db_client)).apply(plan)
    plan_path, report_path, central_report_path, deployment_index_path = planner.write_plan(plan, applied=apply)
    summary = plan["summary"]
    apply_result = plan.get("apply_result", {})
    return DatabricksWorkspaceDeploymentResult(
        plan_path=_rel(plan_path, planner.repo_root),
        report_path=_rel(report_path, planner.repo_root),
        central_report_path=_rel(central_report_path, planner.repo_root),
        deployment_index_path=_rel(deployment_index_path, planner.repo_root),
        applied=apply,
        operation_count=summary["operation_count"],
        apply_supported_count=summary["apply_supported_count"],
        spec_only_count=summary["spec_only_count"],
        skipped_count=apply_result.get("skipped_count", 0),
        failed_count=apply_result.get("failed_count", 0),
    )


def _require_remote_approval(confirm_remote_mutation: bool) -> None:
    if not confirm_remote_mutation:
        raise PermissionError("--apply requires --confirm-remote-mutation")
    if os.environ.get("AUTORESEARCH_ALLOW_REMOTE_EXECUTION") != "1":
        raise PermissionError("Set AUTORESEARCH_ALLOW_REMOTE_EXECUTION=1 before remote deployment")


def _summary(operations: list[DeploymentOperation]) -> dict[str, int]:
    spec_only = [operation for operation in operations if operation.state == "spec_only"]
    supported = [operation for operation in operations if operation.apply_supported]
    blocked = [operation for operation in operations if operation.state.startswith("blocked")]
    return {
        "operation_count": len(operations),
        "apply_supported_count": len(supported),
        "spec_only_count": len(spec_only),
        "blocked_count": len(blocked),
    }


def _evidence_tables(catalog: str, schema: str) -> list[dict[str, str]]:
    definitions = [
        {
            "name": "autoresearch_run_evidence",
            "purpose": "Run-level deployment and validation evidence.",
            "columns": [
                "run_id STRING",
                "task_id STRING",
                "environment STRING",
                "domain STRING",
                "metric DOUBLE",
                "status STRING",
                "evidence_json STRING",
                "created_at TIMESTAMP",
            ],
        },
        {
            "name": "autoresearch_governance_decisions",
            "purpose": "Promotion gate and approval decision evidence.",
            "columns": [
                "run_id STRING",
                "task_id STRING",
                "decision STRING",
                "approval_state STRING",
                "promotion_allowed BOOLEAN",
                "evidence_json STRING",
                "created_at TIMESTAMP",
            ],
        },
        {
            "name": "autoresearch_deployment_audit",
            "purpose": "Workspace deployment plans and apply results.",
            "columns": [
                "deployment_id STRING",
                "environment STRING",
                "domain STRING",
                "operation_count BIGINT",
                "applied_count BIGINT",
                "failed_count BIGINT",
                "plan_json STRING",
                "created_at TIMESTAMP",
            ],
        },
    ]
    tables = []
    for item in definitions:
        target = f"{catalog}.{schema}.{item['name']}"
        sql = "\n".join(
            [
                f"CREATE TABLE IF NOT EXISTS {target} (",
                ",\n".join(f"  {column}" for column in item["columns"]),
                ") USING DELTA;",
            ]
        )
        tables.append({"target_fqn": target, "purpose": item["purpose"], "sql": sql})
    return tables


def _job_api_payload(job: dict[str, Any], spec: dict[str, Any]) -> dict[str, Any]:
    python_file = f"{spec['databricks_workspace_path']}/evaluation/experiment.py"
    return {
        "name": job["name"],
        "tags": {
            "autoresearch_environment": spec["environment"],
            "autoresearch_domain": spec["domain"],
            "autoresearch_managed": "true",
        },
        "tasks": [
            {
                "task_key": "kpi_validation",
                "spark_python_task": {
                    "python_file": python_file,
                    "parameters": [],
                },
                "timeout_seconds": 3600,
            }
        ],
        "deployment_note": (
            "Compute selection is intentionally omitted. Add serverless/job cluster policy "
            "before remote creation."
        ),
    }


def _report(plan: dict[str, Any], *, applied: bool) -> str:
    summary = plan["summary"]
    lines = [
        "# Databricks Workspace Deployment " + ("Apply Report" if applied else "Dry Run"),
        "",
        f"- Workspace: `{plan['workspace']}`",
        f"- Environment: `{plan['environment']}`",
        f"- Domain: `{plan['domain']}`",
        f"- Databricks path: `{plan['databricks_workspace_path']}`",
        f"- Operations: {summary['operation_count']}",
        f"- Apply-supported: {summary['apply_supported_count']}",
        f"- Spec-only: {summary['spec_only_count']}",
        "",
        "## Operations",
        "",
    ]
    for operation in plan.get("operations", []):
        lines.append(
            f"- `{operation['op_id']}` {operation['action']} -> `{operation['target']}` "
            f"[{operation['state']}]"
        )
    if "apply_result" in plan:
        result = plan["apply_result"]
        lines.extend(
            [
                "",
                "## Apply Result",
                "",
                f"- Applied: {result['applied_count']}",
                f"- Skipped: {result['skipped_count']}",
                f"- Failed: {result['failed_count']}",
            ]
        )
    else:
        lines.extend(
            [
                "",
                "## Remote Mutation",
                "",
                "No Databricks mutation was performed. Use `--apply --confirm-remote-mutation` "
                "with `AUTORESEARCH_ALLOW_REMOTE_EXECUTION=1` after review.",
            ]
        )
    return "\n".join(lines).rstrip() + "\n"


def _rel(path: Path, root: Path) -> str:
    try:
        return str(path.resolve().relative_to(root.resolve())).replace("\\", "/")
    except ValueError:
        return str(path)


def _slug(value: str) -> str:
    chars = []
    for char in value.lower():
        chars.append(char if char.isalnum() else "-")
    slug = "-".join(part for part in "".join(chars).split("-") if part)
    return slug or "workspace"


def _read_json(path: Path, *, default: dict[str, Any]) -> dict[str, Any]:
    if not path.exists():
        return dict(default)
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return dict(default)
    return payload if isinstance(payload, dict) else dict(default)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Dry-run or apply Databricks workspace deployment.")
    parser.add_argument("--workspace", required=True, help="Workspace path relative to repo root.")
    parser.add_argument("--repo-root", default=".", help="Repository root. Defaults to current directory.")
    parser.add_argument("--spec-path", help="Genie workspace spec path.")
    parser.add_argument("--apply", action="store_true", help="Apply supported remote operations.")
    parser.add_argument(
        "--confirm-remote-mutation",
        action="store_true",
        help="Required with --apply to confirm remote Databricks mutation.",
    )
    args = parser.parse_args(argv)
    result = run_deployment(
        args.repo_root,
        args.workspace,
        spec_path=args.spec_path,
        apply=args.apply,
        confirm_remote_mutation=args.confirm_remote_mutation,
    )
    print(json.dumps(result.summary(), indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
