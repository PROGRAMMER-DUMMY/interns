"""Execute a provision plan against Unity Catalog -- additively, idempotently.

Refusals, in order, before anything is created:

1. **No confirmed blueprint -> refuse.** ``interns/reports/solution_blueprint/
   current.confirmed.json`` is the ONE human confirmation of the cloud-first
   flow (spec Section 4). Without it this command is a dry run and says so.
2. **Kill-switch -> refuse.** For a confirmed workspace the confirmation IS the
   remote-execution approval (spec Section 10); ``AUTORESEARCH_ALLOW_REMOTE_
   EXECUTION=0`` remains the human override that stops it anyway.
3. **Databricks unreachable -> structured failure**, pointing at
   ``check-platform-readiness``. Never a traceback, never a partial guess.

Every step checks existence first: an object that is already there is recorded
``existing`` and skipped, so re-running after a partial failure resumes.
``blocked_destructive`` steps are never executed -- the planner only emits them
to say "a human must decide", and this module refuses them by kind.
"""
from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Protocol

from core.observability.log_redaction import redact
from core.onboarding.databricks.deploy_gates import REMOTE_ENV
from core.onboarding.panel_contract import attach_stage_routing
from core.provisioning.plan import (
    KIND_BLOCKED,
    KIND_CATALOG,
    KIND_EXTERNAL_LOCATION,
    KIND_GRANT,
    KIND_SCHEMA,
    KIND_VOLUME,
    ROUTING_STAGE,
    load_provision_plan,
)
from core.storage.workspace_layout import WorkspaceLayout

APPLY_LOG_VERSION = 1

STATUS_APPLIED = "applied"
STATUS_DRY_RUN = "dry_run"
STATUS_REFUSED_NO_CONFIRMATION = "refused_no_confirmation"
STATUS_REFUSED_KILL_SWITCH = "refused_remote_execution_kill_switch"
STATUS_REFUSED_UNAVAILABLE = "refused_databricks_unavailable"
STATUS_FAILED = "failed"

READINESS_COMMAND = "uv run check-platform-readiness"


class ProvisioningApi(Protocol):
    """Narrow surface, so tests substitute a fake without a Databricks account.
    The create/exists half is already implemented by
    ``core.onboarding.databricks.uc_intake.SdkUnityCatalogApi``."""

    def catalog_exists(self, name: str) -> bool: ...
    def create_catalog(self, name: str) -> None: ...
    def schema_exists(self, catalog: str, schema: str) -> bool: ...
    def create_schema(self, catalog: str, schema: str) -> None: ...
    def external_location_exists(self, name: str) -> bool: ...
    def create_external_location(self, name: str, url: str, credential: str) -> None: ...
    def volume_exists(self, catalog: str, schema: str, name: str) -> bool: ...
    def create_managed_volume(self, catalog: str, schema: str, name: str) -> None: ...
    def create_external_volume(self, catalog: str, schema: str, name: str, location: str) -> None: ...
    def grant(self, securable_type: str, securable: str, principal: str, privileges: list[str]) -> None: ...


@dataclass
class ApplyResult:
    status: str
    ok: bool
    dry_run: bool
    confirmed_blueprint: bool
    apply_log_path: str = ""
    plan_path: str = ""
    catalog: str = ""
    created: int = 0
    existing: int = 0
    blocked: int = 0
    failed: int = 0
    steps: list[dict[str, Any]] = field(default_factory=list)
    detail: str = ""
    next_command: str = ""

    def summary(self) -> dict[str, Any]:
        return attach_stage_routing(self.__dict__.copy(), ROUTING_STAGE)


def confirmed_blueprint_path(layout: WorkspaceLayout) -> Path:
    return layout.reports_dir / "solution_blueprint" / "current.confirmed.json"


def _sdk_api(catalog: str) -> ProvisioningApi:
    """Reuse the UC intake SDK seam and add the two calls it lacks."""
    from core.config import resolve_databricks_config
    from core.execution.databricks_client import DatabricksClient
    from core.onboarding.databricks.uc_intake import SdkUnityCatalogApi

    db_client = DatabricksClient(resolve_databricks_config(catalog))
    ok, message = db_client.health_check()
    if not ok:
        raise ConnectionError(message)

    class _Api(SdkUnityCatalogApi):
        def create_managed_volume(self, catalog: str, schema: str, name: str) -> None:
            from databricks.sdk.service.catalog import VolumeType

            self.client.volumes.create(
                catalog_name=catalog, schema_name=schema, name=name,
                volume_type=VolumeType.MANAGED,
            )

        def grant(self, securable_type: str, securable: str, principal: str,
                  privileges: list[str]) -> None:
            from databricks.sdk.service.catalog import (
                PermissionsChange,
                SecurableType,
            )

            self.client.grants.update(
                securable_type=SecurableType(securable_type.upper()),
                full_name=securable,
                changes=[PermissionsChange(add=list(privileges), principal=principal)],
            )

    return _Api(db_client)


def apply_provision_plan(
    repo_root: str | Path,
    workspace: str | Path,
    *,
    dry_run: bool | None = None,
    api: ProvisioningApi | None = None,
) -> ApplyResult:
    repo_root = Path(repo_root).resolve()
    layout = WorkspaceLayout(project_root=(repo_root / workspace).resolve())

    plan = load_provision_plan(layout)
    if not plan:
        raise FileNotFoundError(
            "no provision_plan.json; run `plan-provisioning --workspace <ws>` first."
        )
    steps = list(plan.get("steps") or [])
    catalog = str(plan.get("catalog") or "")
    plan_path = _rel(layout.contracts_dir / "provision_plan.json", repo_root)

    confirmed = confirmed_blueprint_path(layout).exists()
    # --dry-run defaults OFF only when the confirmed blueprint is present.
    effective_dry_run = (not confirmed) if dry_run is None else bool(dry_run)

    def _finish(status: str, ok: bool, detail: str = "", next_command: str = "") -> ApplyResult:
        result = ApplyResult(
            status=status, ok=ok, dry_run=effective_dry_run,
            confirmed_blueprint=confirmed, plan_path=plan_path, catalog=catalog,
            steps=step_records, detail=detail, next_command=next_command,
        )
        result.created = sum(1 for s in step_records if s["status"] == "created")
        result.existing = sum(1 for s in step_records if s["status"] == "existing")
        result.blocked = sum(1 for s in step_records if s["status"] == "blocked")
        result.failed = sum(1 for s in step_records if s["status"] == "failed")
        result.apply_log_path = _write_log(layout, repo_root, plan, result)
        return result

    step_records: list[dict[str, Any]] = []

    if not confirmed and not effective_dry_run:  # explicit --no-dry-run without the gate
        step_records = [_record(step, "blocked", "no confirmed blueprint") for step in steps]
        return _finish(
            STATUS_REFUSED_NO_CONFIRMATION, False,
            "the solution blueprint has not been confirmed; provisioning creates "
            "catalogs, schemas, external locations and volumes and needs the one "
            "recorded human confirmation "
            f"({_rel(confirmed_blueprint_path(layout), repo_root)}).",
            "uv run apply-blueprint-answer --workspace <ws> --confirmed-by \"<name>\"",
        )

    if effective_dry_run:
        step_records = [
            _record(step, "blocked" if step["kind"] == KIND_BLOCKED else "would_create",
                    step.get("reason", ""))
            for step in steps
        ]
        detail = "" if confirmed else (
            "no confirmed blueprint yet, so this is a dry run. Nothing was created."
        )
        return _finish(
            STATUS_REFUSED_NO_CONFIRMATION if not confirmed else STATUS_DRY_RUN,
            confirmed, detail,
            "" if confirmed else
            "uv run apply-blueprint-answer --workspace <ws> --confirmed-by \"<name>\"",
        )

    if os.environ.get(REMOTE_ENV, "") == "0":
        step_records = [_record(step, "blocked", "remote execution kill-switch set") for step in steps]
        return _finish(
            STATUS_REFUSED_KILL_SWITCH, False,
            f"{REMOTE_ENV}=0 is set in this shell; remote execution is disabled by "
            "a human override. Nothing was created.",
        )

    if api is None:
        try:
            api = _sdk_api(catalog)
        except Exception as exc:
            step_records = [_record(step, "blocked", "databricks unavailable") for step in steps]
            return _finish(
                STATUS_REFUSED_UNAVAILABLE, False,
                f"Databricks is not reachable: {redact(str(exc))}. Nothing was created.",
                READINESS_COMMAND,
            )

    for step in steps:
        if step["kind"] == KIND_BLOCKED:
            step_records.append(_record(step, "blocked", step.get("reason", "")))
            continue
        try:
            status, detail = _apply_one(api, step)
        except Exception as exc:
            step_records.append(_record(step, "failed", f"{type(exc).__name__}: {redact(str(exc))}"))
            # Later objects depend on earlier ones; pressing on only produces
            # cascading secondary errors.
            break
        step_records.append(_record(step, status, detail))

    failed = any(record["status"] == "failed" for record in step_records)
    return _finish(STATUS_FAILED if failed else STATUS_APPLIED, not failed)


def _apply_one(api: ProvisioningApi, step: dict[str, Any]) -> tuple[str, str]:
    kind = step["kind"]
    params = step.get("params") or {}

    if kind == KIND_EXTERNAL_LOCATION:
        name = params["name"]
        if api.external_location_exists(name):
            return "existing", f"[ok] existing external location {name}"
        api.create_external_location(name, params["url"], params.get("credential_ref", ""))
        return "created", f"[ok] created external location {name}"

    if kind == KIND_CATALOG:
        name = params["name"]
        if api.catalog_exists(name):
            return "existing", f"[ok] existing catalog {name}"
        api.create_catalog(name)
        return "created", f"[ok] created catalog {name}"

    if kind == KIND_SCHEMA:
        catalog, schema = params["catalog"], params["schema"]
        if api.schema_exists(catalog, schema):
            return "existing", f"[ok] existing schema {catalog}.{schema}"
        api.create_schema(catalog, schema)
        return "created", f"[ok] created schema {catalog}.{schema}"

    if kind == KIND_VOLUME:
        catalog, schema, name = params["catalog"], params["schema"], params["name"]
        if api.volume_exists(catalog, schema, name):
            return "existing", f"[ok] existing volume {catalog}.{schema}.{name}"
        if str(params.get("volume_type", "MANAGED")).upper() == "MANAGED":
            api.create_managed_volume(catalog, schema, name)
        else:
            api.create_external_volume(catalog, schema, name, params["location"])
        return "created", f"[ok] created volume {catalog}.{schema}.{name}"

    if kind == KIND_GRANT:
        api.grant(
            params["securable_type"], params["securable"], params["principal"],
            list(params.get("privileges") or []),
        )
        return "created", (
            f"[ok] granted {','.join(params.get('privileges') or [])} on "
            f"{params['securable']} to {params['principal']}"
        )

    raise ValueError(f"unknown provisioning step kind {kind!r}")


def _record(step: dict[str, Any], status: str, detail: str = "") -> dict[str, Any]:
    return {
        "step_id": step.get("step_id", ""),
        "kind": step.get("kind", ""),
        "status": status,
        "detail": detail,
        "at": datetime.now(timezone.utc).isoformat(),
    }


def _write_log(
    layout: WorkspaceLayout, repo_root: Path, plan: dict[str, Any], result: ApplyResult
) -> str:
    out_dir = layout.evidence_dir / "provisioning"
    out_dir.mkdir(parents=True, exist_ok=True)
    path = out_dir / "apply_log.json"
    payload = {
        "artifact_type": "evidence/provisioning/apply_log.json",
        "version": APPLY_LOG_VERSION,
        "generated_by": "apply-provisioning",
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "workspace": layout.project_root.name,
        "plan_path": result.plan_path,
        "catalog": plan.get("catalog"),
        "env": plan.get("env"),
        "status": result.status,
        "ok": result.ok,
        "dry_run": result.dry_run,
        "confirmed_blueprint": result.confirmed_blueprint,
        "additive_only": True,
        "counts": {
            "created": result.created,
            "existing": result.existing,
            "blocked": result.blocked,
            "failed": result.failed,
        },
        "detail": result.detail,
        "next_command": result.next_command,
        "steps": result.steps,
    }
    path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    return _rel(path, repo_root)


def _rel(path: Path, root: Path) -> str:
    try:
        return path.resolve().relative_to(root.resolve()).as_posix()
    except ValueError:
        return path.as_posix()


__all__ = [
    "ApplyResult",
    "ProvisioningApi",
    "apply_provision_plan",
    "confirmed_blueprint_path",
]
