"""Dry-run and guarded deployment for Databricks workspace specs.

Two deployment paths live here:

1. Genie workspace-spec deployment (``main`` / ``run_deployment``): folders,
   workspace files, and evidence tables from a Genie workspace spec.
2. Medallion Unity Catalog deployment (``medallion_deploy_main`` /
   ``deploy_medallion_from_approval``): the slice after the
   ``medallion apply-deploy`` approval boundary (PRD
   docs/prd/databricks_deployment.md section 9). It consumes
   ``interns/state/medallion/deploy_approval.json``, REFUSES to run without a
   fresh approval (re-verifying gate G4 plan freshness and gate G5 remote
   approval at execution time), and performs the Unity Catalog deployment
   described in the approved ``deploy_plan.json``.

Both paths keep every live Databricks call behind
``AUTORESEARCH_ALLOW_REMOTE_EXECUTION=1`` (human-set only; this module only
VERIFIES the variable, never sets it). Without it the deployer runs in
dry-run / plan-echo mode and says so.
"""
from __future__ import annotations
from core.observability.cost_ledger import anchored

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
from core.failures import WorkflowBlockedError
from core.onboarding.databricks.deploy_gates import (
    GATE_NAMES,
    REMOTE_ENV,
    check_plan_freshness,
    check_remote_approval,
    run_deploy_gates,
)
from core.failures import remote_denied
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

    def upload_volume_file(self, source: Path, volume_path: str) -> None:
        with source.open("rb") as fh:
            self.client.files.upload(volume_path, fh, overwrite=True)

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
        not_attempted = []
        operations = plan.get("operations", [])
        for index, operation in enumerate(operations):
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
                # Stop on first failure rather than silently continuing past it.
                # Every operation above is idempotent (mkdirs/upload-overwrite/
                # get-then-create/CREATE TABLE IF NOT EXISTS), so retrying the
                # whole plan after a fix is always safe -- there is no partial
                # state a retry can't recover from.
                not_attempted = [
                    {**op, "result": "not_attempted"} for op in operations[index + 1 :]
                ]
                break
        plan["apply_result"] = {
            "applied": applied,
            "skipped": skipped,
            "failed": failed,
            "not_attempted": not_attempted,
            "applied_count": len(applied),
            "skipped_count": len(skipped),
            "failed_count": len(failed),
            "not_attempted_count": len(not_attempted),
        }
        return plan


def run_deployment(
    repo_root: str | Path,
    workspace: str | Path,
    *,
    spec_path: str | Path | None = None,
    apply: bool = False,
    confirm_remote_mutation: bool = False,
    confirmed_by: str = "",
) -> DatabricksWorkspaceDeploymentResult:
    planner = DatabricksWorkspaceDeploymentPlanner(repo_root, workspace, spec_path=spec_path)
    plan = planner.build_plan()
    if apply:
        _require_remote_approval(confirm_remote_mutation)
        # P2 (T7): the Genie lane previously mutated Databricks with ONLY the
        # confirm flag + env + PHI gate, bypassing the deploy gates entirely —
        # so G3 human-provenance never ran and a remote apply could happen with
        # no human-attributed approval (Human-Gate Provenance Rule violation).
        # Run the gate set, record all verdicts on the plan, and HARD-BLOCK on
        # G3 human-provenance. (G4 plan-freshness binds to the medallion
        # deploy_plan, not the Genie spec, so it is recorded but not enforced
        # here.) Ref: core-audit ob-databricks.md.
        gate_verdicts = run_deploy_gates(
            planner.repo_root,
            _rel(planner.layout.project_root, planner.repo_root),
            confirmed_by=confirmed_by,
        )
        plan["deploy_gates"] = [v.to_dict() for v in gate_verdicts]
        g3 = next((v for v in gate_verdicts if v.gate == "G3_human_provenance"), None)
        if g3 is None or not g3.ok:
            raise WorkflowBlockedError(
                remote_denied(
                    "databricks_workspace_deploy.human_provenance",
                    "Genie-lane apply refused: deployment approval is not "
                    "human-attributed (G3 human-provenance failed). "
                    + (g3.blocking_reason if g3 else "G3 gate did not run"),
                    next_command=(
                        "Re-run with --confirmed-by \"<reviewer name>\" so the "
                        "remote apply is recorded as source: human."
                    ),
                )
            )
        cfg = load_config()
        # Sensitive-data gate: refuse to upload identifiable PHI to a
        # non-HIPAA-covered target or cardholder data to a non-PCI target.
        from core.governance.phi_gate import enforce_remote_sensitive_gate

        phi_failure = enforce_remote_sensitive_gate(
            planner.layout, cfg, operation="databricks_workspace_deploy"
        )
        if phi_failure is not None:
            raise WorkflowBlockedError(phi_failure)
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


# ─────────────────────────────────────────────────────────────────────────────
# Medallion Unity Catalog deployment — the slice after the apply-deploy
# approval boundary (docs/prd/databricks_deployment.md section 9).
#
# Refusal contract (the refusal IS the feature):
#   - no approval artifact                -> refuse (nothing was approved)
#   - consumed / agent-asserted approval  -> refuse
#   - approval gates not all recorded ok  -> refuse
#   - approval-plan hash binding broken   -> refuse
#   - G4 re-verified at EXECUTION time (plan hash vs manifest on disk) ->
#     refuse on stale plan
#   - live mutation additionally re-verifies G5 (AUTORESEARCH_ALLOW_REMOTE_
#     EXECUTION=1, human-set) plus --confirm-remote-mutation; when live is not
#     fully authorized the deployer either refuses (--apply requested) or runs
#     in dry-run / plan-echo mode and says so. This code NEVER sets the env var.
# ─────────────────────────────────────────────────────────────────────────────

MEDALLION_APPROVAL_TYPE = "medallion/deploy_approval.json"
MEDALLION_APPROVAL_REL = Path("interns") / "state" / "medallion" / "deploy_approval.json"
MEDALLION_PLAN_REL = Path("interns") / "generated" / "medallion" / "deploy_plan.json"
EXECUTION_STATE_JSON = "deploy_execution.json"
EXECUTION_STATE_MD = "deploy_execution.md"

_SCHEMA_LAYER_ORDER = ("bronze", "silver", "gold", "ops")

# Source formats Databricks `read_files` can ingest directly; anything else is
# deferred to the medallion refresh job (orchestration slice).
_READ_FILES_FORMATS = {
    ".csv": "csv",
    ".json": "json",
    ".parquet": "parquet",
    ".avro": "avro",
    ".orc": "orc",
    ".txt": "text",
    ".xml": "xml",
}


class UnityCatalogDeploymentApi(Protocol):
    """The two remote primitives the medallion deployer needs. Tests inject a
    fake; the live path uses :class:`DatabricksWorkspaceApi`."""

    def execute_sql(self, statement: str) -> None: ...
    def upload_volume_file(self, source: Path, volume_path: str) -> None: ...


@dataclass(frozen=True)
class UnityCatalogAction:
    seq: int
    action: str
    target: str
    execute: bool                 # executed on live apply; False = deferred
    kind: str = "sql"             # "sql" | "upload" | "deferred"
    statement: str = ""
    source_file: str = ""
    volume_path: str = ""
    reason: str = ""

    def as_dict(self) -> dict[str, Any]:
        return {
            "seq": self.seq,
            "action": self.action,
            "target": self.target,
            "execute": self.execute,
            "kind": self.kind,
            "statement": self.statement,
            "source_file": self.source_file,
            "volume_path": self.volume_path,
            "reason": self.reason,
        }


@dataclass
class MedallionDeploymentOutcome:
    mode: str  # "refused" | "dry_run" | "applied"
    refusal_reasons: list[str] = field(default_factory=list)
    actions: list[dict[str, Any]] = field(default_factory=list)
    executed_count: int = 0
    deferred_count: int = 0
    failed_count: int = 0
    note: str = ""
    state_json_path: str = ""
    state_md_path: str = ""

    @property
    def refused(self) -> bool:
        return self.mode == "refused"

    def summary(self) -> dict[str, Any]:
        return {
            "mode": self.mode,
            "refusal_reasons": self.refusal_reasons,
            "action_count": len(self.actions),
            "executed_count": self.executed_count,
            "deferred_count": self.deferred_count,
            "failed_count": self.failed_count,
            "note": self.note,
            "state_json_path": self.state_json_path,
            "state_md_path": self.state_md_path,
        }


def _load_json_dict(path: Path) -> dict[str, Any] | None:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
        return payload if isinstance(payload, dict) else None
    except (OSError, json.JSONDecodeError):
        return None


def _sql_quote(value: str) -> str:
    return value.replace("'", "''")


def verify_deploy_approval(
    repo_root: Path, workspace: Path
) -> tuple[dict[str, Any] | None, dict[str, Any] | None, list[str]]:
    """Verify the approval artifact is fresh and binding. Returns
    ``(approval, plan, refusal_reasons)``; any nonempty reasons list means the
    deployer must refuse (both dry-run echo and live apply)."""
    reasons: list[str] = []
    approval_path = workspace / MEDALLION_APPROVAL_REL
    approval = _load_json_dict(approval_path)
    if approval is None:
        return None, None, [
            f"no deployment approval artifact at {_rel(approval_path, repo_root)}; "
            "run `medallion apply-deploy --workspace <ws> --confirmed-by <name>` first "
            "(the refusal IS the feature)"
        ]

    if approval.get("artifact_type") != MEDALLION_APPROVAL_TYPE:
        reasons.append(
            f"approval artifact_type is {approval.get('artifact_type')!r}, "
            f"expected {MEDALLION_APPROVAL_TYPE!r}"
        )
    try:
        version = int(approval.get("version") or 0)
    except (TypeError, ValueError):
        version = 0
    if version != 1:
        reasons.append(f"unsupported approval version: {approval.get('version')!r}")
    if approval.get("consumed_at"):
        reasons.append(
            f"approval was already consumed at {approval.get('consumed_at')}; "
            "re-run `medallion apply-deploy` for a fresh approval"
        )
    if approval.get("source") != "human" or not str(approval.get("confirmed_by") or "").strip():
        reasons.append(
            "approval is not human-attributed: source must be `human` with a "
            "nonempty confirmed_by (Human-Gate Provenance Rule)"
        )
    recorded = {
        str(g.get("gate")): bool(g.get("ok"))
        for g in (approval.get("gates") or [])
        if isinstance(g, dict)
    }
    missing_gates = [name for name in GATE_NAMES if name not in recorded]
    failed_gates = sorted(name for name, ok in recorded.items() if not ok)
    if missing_gates:
        reasons.append(f"approval lacks recorded verdicts for gates: {missing_gates}")
    if failed_gates:
        reasons.append(f"approval records failing gates: {failed_gates}")

    plan_rel = str(approval.get("plan_path") or "")
    plan = _load_json_dict(repo_root / plan_rel) if plan_rel else None
    if plan is None:
        reasons.append(
            f"approval references a deploy plan that cannot be read: {plan_rel or '<missing plan_path>'}"
        )
        return approval, None, reasons

    canonical = (workspace / MEDALLION_PLAN_REL).resolve()
    if (repo_root / plan_rel).resolve() != canonical:
        reasons.append(
            f"approval plan_path {plan_rel!r} is not the canonical "
            f"{_rel(canonical, repo_root)!r}"
        )
    validation = plan.get("validation") or {}
    if not validation.get("valid"):
        reasons.append(f"deploy plan is not valid: {validation.get('errors')}")
    plan_hash = str((plan.get("source_manifest") or {}).get("inputs_hash") or "")
    if str(approval.get("plan_inputs_hash") or "") != plan_hash:
        reasons.append(
            "approval plan_inputs_hash does not match the plan on disk "
            "(approval-plan binding broken); regenerate the plan and re-approve"
        )

    # G4 re-verified at execution time: the plan's recorded manifest hash must
    # still match a re-hash of the manifest on disk RIGHT NOW.
    g4 = check_plan_freshness(workspace, repo_root)
    if not g4.ok:
        reasons.append(f"G4 re-check failed at execution time: {g4.blocking_reason}")
    return approval, plan, reasons


def build_unity_catalog_actions(plan: dict[str, Any]) -> list[UnityCatalogAction]:
    """Deterministic, ordered UC action sequence from a validated deploy plan:
    catalog -> schemas (bronze/silver/gold/ops) -> volumes -> raw uploads ->
    tables (plan order) -> ops refresh-manifest table."""
    uc = plan.get("unity_catalog") or {}
    catalog = str(uc.get("catalog") or "")
    schemas = uc.get("schemas") or {}
    actions: list[UnityCatalogAction] = []

    def _add(action: str, target: str, **kwargs: Any) -> None:
        actions.append(UnityCatalogAction(seq=len(actions) + 1, action=action, target=target, **kwargs))

    _add(
        "ensure_catalog", catalog,
        execute=True,
        statement=f"CREATE CATALOG IF NOT EXISTS {catalog}",
        reason="Catalog from the approved plan (deployment parameter, never hardcoded).",
    )
    for layer in _SCHEMA_LAYER_ORDER:
        schema = schemas.get(layer)
        if not schema:
            continue
        _add(
            "ensure_schema", f"{catalog}.{schema}",
            execute=True,
            statement=f"CREATE SCHEMA IF NOT EXISTS {catalog}.{schema}",
            reason=f"{layer} layer schema from the approved plan.",
        )

    upload_targets: dict[str, str] = {}
    for volume in uc.get("volumes") or []:
        vschema = str(volume.get("schema") or schemas.get("bronze") or "bronze")
        vname = str(volume.get("name") or "raw_sources")
        _add(
            "ensure_volume", f"{catalog}.{vschema}.{vname}",
            execute=True,
            statement=f"CREATE VOLUME IF NOT EXISTS {catalog}.{vschema}.{vname}",
            reason="UC volume for raw source files (append-only; rollback never deletes).",
        )
        for upload in volume.get("uploads") or []:
            source_file = str(upload.get("source_file") or "")
            target_path = str(upload.get("target_path") or "")
            if not source_file or not target_path:
                continue
            upload_targets[source_file] = target_path
            _add(
                "upload_raw_source", target_path,
                execute=True,
                kind="upload",
                source_file=source_file,
                volume_path=target_path,
                reason="Raw source upload into the bronze volume (repo-relative source).",
            )

    for table in uc.get("tables") or []:
        layer = str(table.get("layer") or "")
        fqn = str(table.get("fqn") or "")
        if layer == "bronze":
            source_file = str(table.get("source_file") or "")
            volume_target = upload_targets.get(source_file, "")
            fmt = _READ_FILES_FORMATS.get(Path(source_file).suffix.lower(), "")
            if volume_target and fmt:
                statement = (
                    f"CREATE TABLE IF NOT EXISTS {fqn} AS\n"
                    "SELECT\n"
                    "  *,\n"
                    f"  '{_sql_quote(source_file)}' AS _source_file,\n"
                    "  current_timestamp() AS _load_ts\n"
                    f"FROM read_files('{_sql_quote(volume_target)}', format => '{fmt}')"
                )
                _add(
                    "create_bronze_table", fqn,
                    execute=True,
                    statement=statement,
                    source_file=source_file,
                    reason="Bronze ingest from the uploaded raw volume file.",
                )
            else:
                _add(
                    "create_bronze_table", fqn,
                    execute=False,
                    kind="deferred",
                    source_file=source_file,
                    reason=(
                        f"source format {Path(source_file).suffix or '<none>'} is not "
                        "ingestible via read_files; the medallion refresh job "
                        "(orchestration slice) materializes this table"
                        if volume_target
                        else "no volume upload recorded for the bronze source file"
                    ),
                )
        else:
            _add(
                f"materialize_{layer}_table", fqn,
                execute=False,
                kind="deferred",
                reason=(
                    "transform SQL is emitted locally in DuckDB dialect; the table is "
                    "materialized by the medallion refresh job (orchestration slice). "
                    "Its schema exists after this deployment."
                ),
            )

    manifest_table = str(
        ((plan.get("orchestration") or {}).get("incremental") or {}).get("remote_manifest_table") or ""
    )
    if manifest_table:
        _add(
            "ensure_ops_manifest_table", manifest_table,
            execute=True,
            statement=(
                f"CREATE TABLE IF NOT EXISTS {manifest_table} (\n"
                "  table_key STRING,\n"
                "  fingerprint STRING,\n"
                "  run_id STRING,\n"
                "  updated_at TIMESTAMP\n"
                ") USING DELTA"
            ),
            reason="Remote mirror of the local refresh manifest (incremental skip semantics).",
        )
    return actions


def _execute_uc_actions(
    actions: list[UnityCatalogAction],
    api: UnityCatalogDeploymentApi,
    repo_root: Path,
) -> tuple[list[dict[str, Any]], int]:
    results: list[dict[str, Any]] = []
    failed = 0
    for action in actions:
        record = action.as_dict()
        if not action.execute:
            record["result"] = "deferred"
        else:
            try:
                if action.kind == "upload":
                    source = repo_root / action.source_file
                    if not source.exists():
                        raise FileNotFoundError(f"missing raw source: {action.source_file}")
                    api.upload_volume_file(source, action.volume_path)
                else:
                    api.execute_sql(action.statement)
                record["result"] = "applied"
            except Exception as exc:
                record["result"] = "failed"
                record["error"] = str(exc)
                failed += 1
        results.append(record)
    return results, failed


def _build_live_medallion_api(workspace: Path) -> DatabricksWorkspaceApi:
    """Live-path construction only: PHI/PCI gate, then client health check.
    Never reached unless --apply, --confirm-remote-mutation, and the human-set
    AUTORESEARCH_ALLOW_REMOTE_EXECUTION=1 all hold."""
    cfg = load_config()
    from core.governance.phi_gate import enforce_remote_sensitive_gate

    layout = WorkspaceLayout(project_root=workspace)
    phi_failure = enforce_remote_sensitive_gate(layout, cfg, operation="medallion_uc_deploy")
    if phi_failure is not None:
        raise WorkflowBlockedError(phi_failure)
    db_client = DatabricksClient(cfg.databricks)
    ok, msg = db_client.health_check()
    if not ok:
        raise RuntimeError(f"Databricks unavailable: {msg}")
    return DatabricksWorkspaceApi(db_client)


def _stamp_approval_consumed(workspace: Path) -> None:
    path = workspace / MEDALLION_APPROVAL_REL
    approval = _load_json_dict(path)
    if approval is None:
        return
    approval["consumed_at"] = datetime.now(timezone.utc).isoformat()
    approval["consumed_by"] = "core.onboarding.databricks.workspace_deployer"
    path.write_text(json.dumps(approval, indent=2) + "\n", encoding="utf-8")


def _write_execution_state(
    workspace: Path,
    repo_root: Path,
    outcome: MedallionDeploymentOutcome,
    approval: dict[str, Any],
) -> None:
    out_dir = workspace / "interns" / "state" / "medallion"
    out_dir.mkdir(parents=True, exist_ok=True)
    payload = {
        "artifact_type": "medallion/deploy_execution.json",
        "version": 1,
        "mode": outcome.mode,
        "executed_at": datetime.now(timezone.utc).isoformat(),
        "confirmed_by": approval.get("confirmed_by"),
        "plan_inputs_hash": approval.get("plan_inputs_hash"),
        "executed_count": outcome.executed_count,
        "deferred_count": outcome.deferred_count,
        "failed_count": outcome.failed_count,
        "note": outcome.note,
        "actions": outcome.actions,
    }
    json_path = out_dir / EXECUTION_STATE_JSON
    md_path = out_dir / EXECUTION_STATE_MD
    json_path.write_text(json.dumps(payload, indent=2, default=str) + "\n", encoding="utf-8")

    marker = {"applied": "[ok]", "planned": "[~]", "deferred": "[~]", "failed": "[x]"}
    lines = [
        "# Medallion Unity Catalog Deployment "
        + ("Apply Report" if outcome.mode == "applied" else "Dry Run (plan echo)"),
        "",
        f"- Mode: **{outcome.mode}**",
        f"- Confirmed by: {approval.get('confirmed_by') or '(none)'}",
        f"- Plan hash: `{approval.get('plan_inputs_hash')}`",
        f"- Executed: {outcome.executed_count}  Deferred: {outcome.deferred_count}  "
        f"Failed: {outcome.failed_count}",
        "",
        "## Actions",
        "",
    ]
    for record in outcome.actions:
        mark = marker.get(str(record.get("result")), "[~]")
        lines.append(
            f"- {mark} `{record['seq']:03d}` {record['action']} -> `{record['target']}` "
            f"({record.get('result')})"
        )
    lines += ["", "## Remote Mutation", "", outcome.note]
    md_path.write_text("\n".join(lines).rstrip() + "\n", encoding="utf-8")
    outcome.state_json_path = _rel(json_path, repo_root)
    outcome.state_md_path = _rel(md_path, repo_root)


def deploy_medallion_from_approval(
    repo_root: str | Path,
    workspace: str | Path,
    *,
    apply: bool = False,
    confirm_remote_mutation: bool = False,
    api: UnityCatalogDeploymentApi | None = None,
) -> MedallionDeploymentOutcome:
    """Consume the apply-deploy approval artifact and perform (or echo) the UC
    deployment. ``api`` is the test seam — injecting it skips client
    construction but NEVER skips the refusal gates."""
    repo_root = Path(repo_root).resolve()
    workspace_path = (repo_root / workspace).resolve()

    approval, plan, reasons = verify_deploy_approval(repo_root, workspace_path)

    if apply and not reasons:
        # G5 re-verified at execution time (PRD section 7 gate 5 + section 9
        # seam). The check only VERIFIES the env var; nothing here sets it.
        if not confirm_remote_mutation:
            reasons.append(
                "live apply refused: --apply requires --confirm-remote-mutation"
            )
        g5 = check_remote_approval()
        if not g5.ok:
            reasons.append(
                f"G5 re-check failed at execution time: {g5.blocking_reason}; "
                "drop --apply to run the dry-run plan echo instead"
            )

    if reasons:
        return MedallionDeploymentOutcome(mode="refused", refusal_reasons=reasons)

    assert approval is not None and plan is not None  # guaranteed by empty reasons
    actions = build_unity_catalog_actions(plan)

    if not apply:
        note = (
            "[dry-run] no Databricks call was made. Live apply requires --apply "
            f"--confirm-remote-mutation and {REMOTE_ENV}=1 set by a human in the "
            "executing shell (agents must never set it)."
        )
        outcome = MedallionDeploymentOutcome(
            mode="dry_run",
            actions=[
                {**a.as_dict(), "result": "planned" if a.execute else "deferred"}
                for a in actions
            ],
            executed_count=0,
            deferred_count=sum(1 for a in actions if not a.execute),
            note=note,
        )
        _write_execution_state(workspace_path, repo_root, outcome, approval)
        return outcome

    if api is None:
        try:
            api = _build_live_medallion_api(workspace_path)
        except Exception as exc:
            return MedallionDeploymentOutcome(
                mode="refused",
                refusal_reasons=[f"live apply blocked: {exc}"],
            )

    results, failed = _execute_uc_actions(actions, api, repo_root)
    outcome = MedallionDeploymentOutcome(
        mode="applied",
        actions=results,
        executed_count=sum(1 for r in results if r.get("result") == "applied"),
        deferred_count=sum(1 for r in results if r.get("result") == "deferred"),
        failed_count=failed,
        note=(
            "applied via the approved plan; deferred actions are materialized by "
            "the medallion refresh job (orchestration slice)."
            if failed == 0
            else "apply finished with failures; the approval was NOT consumed - fix and re-run."
        ),
    )
    if failed == 0:
        # Consume-once: a successful apply uses up the approval; the next
        # deployment needs a fresh `medallion apply-deploy` run.
        _stamp_approval_consumed(workspace_path)
    _write_execution_state(workspace_path, repo_root, outcome, approval)
    return outcome


def medallion_deploy_main(argv: list[str] | None = None) -> int:
    """CLI: `medallion deploy` — execute the approved UC deployment (dry-run
    plan echo unless --apply --confirm-remote-mutation + human-set env)."""
    from core.paths import PROJECT_ROOT

    parser = argparse.ArgumentParser(
        prog="medallion deploy",
        description=(
            "Consume interns/state/medallion/deploy_approval.json and perform the "
            "Unity Catalog deployment from the approved deploy_plan.json. Refuses "
            "without a fresh approval (G4 + G5 re-verified at execution time). "
            "Dry-run plan echo unless --apply --confirm-remote-mutation and "
            f"{REMOTE_ENV}=1 (human-set) all hold."
        ),
    )
    parser.add_argument("--workspace", required=True)
    parser.add_argument("--repo-root", default=str(PROJECT_ROOT))
    parser.add_argument("--apply", action="store_true", help="Request live remote mutation.")
    parser.add_argument(
        "--confirm-remote-mutation", action="store_true",
        help="Required with --apply to confirm remote Databricks mutation.",
    )
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)

    outcome = deploy_medallion_from_approval(
        args.repo_root,
        args.workspace,
        apply=args.apply,
        confirm_remote_mutation=args.confirm_remote_mutation,
    )

    if args.json:
        print(json.dumps(outcome.summary(), indent=2, default=str))
    elif outcome.refused:
        print("[x] medallion deploy refused:")
        for reason in outcome.refusal_reasons:
            print(f"  [x] {reason}")
    else:
        for record in outcome.actions:
            mark = "[ok]" if record.get("result") == "applied" else (
                "[x]" if record.get("result") == "failed" else "[~]"
            )
            print(f"{mark} {record['seq']:03d} {record['action']:<26} {record['target']} "
                  f"({record.get('result')})")
        print(f"\n{outcome.note}")
        if outcome.state_json_path:
            print(f"[ok] execution state: {outcome.state_json_path}")
    if outcome.refused:
        return 2
    return 1 if outcome.failed_count else 0



@anchored("deploy-databricks-workspace")
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
    parser.add_argument(
        "--confirmed-by",
        default="",
        help="Human reviewer name; required for --apply (G3 human-provenance).",
    )
    args = parser.parse_args(argv)
    result = run_deployment(
        args.repo_root,
        args.workspace,
        spec_path=args.spec_path,
        apply=args.apply,
        confirm_remote_mutation=args.confirm_remote_mutation,
        confirmed_by=args.confirmed_by,
    )
    print(json.dumps(result.summary(), indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
