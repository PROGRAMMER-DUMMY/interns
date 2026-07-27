"""Execute an approved solution blueprint against Unity Catalog.

Phase C. Until now the honest answer to "my data is in S3" was "not our
problem" -- the platform assumed tables already existed in Unity Catalog and
left the user at a dead end. This closes it without becoming an ingestion tool:
it creates the GOVERNANCE objects (storage credential, external location,
catalog, schemas, volumes, external tables) and moves no bytes.

Three refusals, in this order, before anything is created:

1. **No approved blueprint -> refuse.** The blueprint is the human decision that
   authorises creating catalogs and tables. A draft is not an approval, and an
   approval invalidated by a later edit is not an approval either.
2. **No remote-execution approval -> refuse.** Same G5 gate as every other
   remote path; `AUTORESEARCH_ALLOW_REMOTE_EXECUTION` must be set by a human's
   own shell and nothing here ever sets it.
3. **Dry run by default.** `--apply` is required to mutate anything, so the
   normal path prints exactly what would happen and creates nothing.

Every operation is idempotent by construction: existence is checked first and an
already-present object is `skipped`, never recreated. Re-running after a partial
failure resumes rather than starting over.

Zero copy is the default the blueprint already chose -- an external table
registers data where it lives. A `managed_table` disposition is reported as
REQUIRING A COPY and is deliberately NOT executed here: `COPY INTO`/Auto Loader
move real bytes and belong behind their own decision, not smuggled in under a
governance step.
"""
from __future__ import annotations
from core.observability.cost_ledger import anchored

import argparse
import json
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional, Protocol

from core.contracts.versioning import register_contract
from core.onboarding.blueprint import (
    DISPOSITION_EXTERNAL_TABLE,
    DISPOSITION_MANAGED_TABLE,
    DISPOSITION_VOLUME,
    load_blueprint,
)
from core.onboarding.databricks.deploy_gates import check_remote_approval
from core.paths import PROJECT_ROOT
from core.storage.workspace_layout import WorkspaceLayout

REPORT_VERSION = 1
register_contract("uc_intake/current.json", current_version=REPORT_VERSION)


@dataclass
class IntakeOperation:
    kind: str          # storage_credential | external_location | catalog | schema | volume | table
    name: str
    status: str        # planned | created | skipped | failed | requires_copy
    detail: str = ""

    def to_dict(self) -> dict[str, Any]:
        return self.__dict__.copy()


@dataclass
class IntakeResult:
    current_json_path: str
    current_markdown_path: str
    status: str        # dry_run | applied | failed
    created: int = 0
    skipped: int = 0
    failed: int = 0
    operations: list[dict[str, Any]] = field(default_factory=list)
    ok: bool = True

    def summary(self) -> dict[str, Any]:
        return self.__dict__.copy()


class UnityCatalogApi(Protocol):
    """The narrow surface intake needs. A Protocol so tests substitute a fake
    without a Databricks account, and so the SDK stays behind one seam."""

    def storage_credential_exists(self, name: str) -> bool: ...
    def create_storage_credential(self, name: str, role_arn: str) -> None: ...
    def external_location_exists(self, name: str) -> bool: ...
    def create_external_location(self, name: str, url: str, credential: str) -> None: ...
    def catalog_exists(self, name: str) -> bool: ...
    def create_catalog(self, name: str) -> None: ...
    def schema_exists(self, catalog: str, schema: str) -> bool: ...
    def create_schema(self, catalog: str, schema: str) -> None: ...
    def volume_exists(self, catalog: str, schema: str, name: str) -> bool: ...
    def create_external_volume(self, catalog: str, schema: str, name: str, location: str) -> None: ...
    def table_exists(self, fqn: str) -> bool: ...
    def create_external_table(self, fqn: str, location: str) -> None: ...


class SdkUnityCatalogApi:
    """databricks-sdk implementation. Constructed only when actually applying."""

    def __init__(self, db_client: Any) -> None:
        self.db_client = db_client
        self.client = db_client.get_client()

    @staticmethod
    def _exists(fn: Any, *args: Any) -> bool:
        try:
            fn(*args)
            return True
        except Exception:
            return False

    def storage_credential_exists(self, name: str) -> bool:
        return self._exists(self.client.storage_credentials.get, name)

    def create_storage_credential(self, name: str, role_arn: str) -> None:
        from databricks.sdk.service.catalog import AwsIamRoleRequest

        self.client.storage_credentials.create(
            name=name, aws_iam_role=AwsIamRoleRequest(role_arn=role_arn)
        )

    def external_location_exists(self, name: str) -> bool:
        return self._exists(self.client.external_locations.get, name)

    def create_external_location(self, name: str, url: str, credential: str) -> None:
        self.client.external_locations.create(
            name=name, url=url, credential_name=credential
        )

    def catalog_exists(self, name: str) -> bool:
        return self._exists(self.client.catalogs.get, name)

    def create_catalog(self, name: str) -> None:
        self.client.catalogs.create(name=name)

    def schema_exists(self, catalog: str, schema: str) -> bool:
        return self._exists(self.client.schemas.get, f"{catalog}.{schema}")

    def create_schema(self, catalog: str, schema: str) -> None:
        self.client.schemas.create(name=schema, catalog_name=catalog)

    def volume_exists(self, catalog: str, schema: str, name: str) -> bool:
        return self._exists(self.client.volumes.read, f"{catalog}.{schema}.{name}")

    def create_external_volume(self, catalog: str, schema: str, name: str, location: str) -> None:
        from databricks.sdk.service.catalog import VolumeType

        self.client.volumes.create(
            catalog_name=catalog, schema_name=schema, name=name,
            volume_type=VolumeType.EXTERNAL, storage_location=location,
        )

    def table_exists(self, fqn: str) -> bool:
        return self._exists(self.client.tables.get, fqn)

    def create_external_table(self, fqn: str, location: str) -> None:
        warehouse_id = self.db_client._extract_warehouse_id()
        if not warehouse_id:
            raise RuntimeError(
                "DATABRICKS_HTTP_PATH is required to create tables (needs a SQL warehouse)"
            )
        self.client.statement_execution.execute_statement(
            warehouse_id=warehouse_id,
            statement=f"CREATE TABLE IF NOT EXISTS {fqn} LOCATION '{location}'",
            wait_timeout="50s",
        )


def _plan_operations(payload: dict[str, Any], role_arn: str) -> list[IntakeOperation]:
    catalog = str(payload.get("catalog") or "")
    schemas = payload.get("schemas") or {}
    bronze = str(schemas.get("bronze") or "bronze")
    root = str(payload.get("source_root") or "").rstrip("/") + "/"
    credential = f"{catalog}_src"
    location = f"{catalog}_root"

    ops = [
        IntakeOperation("storage_credential", credential, "planned", role_arn or "<MISSING ROLE ARN>"),
        IntakeOperation("external_location", location, "planned", root),
        IntakeOperation("catalog", catalog, "planned"),
    ]
    for key in ("bronze", "silver", "gold"):
        ops.append(IntakeOperation("schema", f"{catalog}.{schemas.get(key, key)}", "planned"))

    for group in payload.get("groups") or []:
        disposition = str(group.get("disposition") or "")
        name = str(group.get("group") or "")
        target = str(group.get("target") or "")
        if disposition == DISPOSITION_VOLUME:
            ops.append(IntakeOperation(
                "volume", target, "planned", f"{root}{name.split('/')[0]}/"
            ))
        elif disposition == DISPOSITION_EXTERNAL_TABLE:
            ops.append(IntakeOperation("table", target, "planned", f"{root}{name}/"))
        elif disposition == DISPOSITION_MANAGED_TABLE:
            # Deliberately not executed here -- see the module docstring.
            ops.append(IntakeOperation(
                "table", target, "requires_copy",
                f"managed table: run COPY INTO / Auto Loader from {root}{name}/ separately",
            ))
    # A volume group per FILE would create one volume per document; collapse to
    # the distinct volume path the blueprint targeted.
    seen: set[tuple[str, str]] = set()
    deduped: list[IntakeOperation] = []
    for op in ops:
        key = (op.kind, op.name)
        if key in seen:
            continue
        seen.add(key)
        deduped.append(op)
    return deduped


def run_intake(
    repo_root: str | Path,
    workspace: str | Path,
    *,
    role_arn: str = "",
    apply: bool = False,
    api: Optional[UnityCatalogApi] = None,
) -> IntakeResult:
    repo_root = Path(repo_root).resolve()
    layout = WorkspaceLayout(project_root=(repo_root / workspace).resolve())
    payload = load_blueprint(layout)

    if not payload:
        raise FileNotFoundError(
            "no solution blueprint; run `prepare-solution-blueprint` first."
        )
    if payload.get("status") != "approved":
        raise PermissionError(
            "the solution blueprint is not approved. Intake creates catalogs, "
            "schemas, volumes and tables -- that needs a recorded human decision: "
            "`apply-blueprint-answer --workspace <ws> --approve --confirmed-by \"<name>\"`."
        )

    remote_gate = check_remote_approval()
    ops = _plan_operations(payload, role_arn)

    if not apply:
        status = "dry_run"
    elif not remote_gate.ok:
        status = "refused_no_remote_approval"
    elif not role_arn:
        status = "refused_no_role_arn"
    else:
        status = _execute(ops, payload, role_arn, api)

    report = {
        "artifact_type": "uc_intake/current.json",
        "version": REPORT_VERSION,
        "generated_by": "apply-uc-intake",
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "status": status,
        "applied": apply,
        "source_root": payload.get("source_root"),
        "catalog": payload.get("catalog"),
        "role_arn_provided": bool(role_arn),
        "remote_approval_ok": remote_gate.ok,
        "remote_approval_blocking_reason": remote_gate.blocking_reason,
        "approved_by": (payload.get("approval") or {}).get("confirmed_by", ""),
        "operations": [op.to_dict() for op in ops],
    }
    out_dir = layout.reports_dir / "uc_intake"
    out_dir.mkdir(parents=True, exist_ok=True)
    current_json = out_dir / "current.json"
    current_md = out_dir / "current.md"
    current_json.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    current_md.write_text(_render_markdown(report), encoding="utf-8")

    if status == "refused_no_remote_approval":
        raise PermissionError(
            f"{remote_gate.blocking_reason} Nothing was created. Plan written to "
            f"{_rel(current_md, repo_root)}."
        )
    if status == "refused_no_role_arn":
        raise ValueError(
            "--role-arn is required to create the storage credential. It is the "
            "AWS IAM role Unity Catalog assumes to reach your bucket; there is no "
            "safe default. Nothing was created."
        )

    counts = {s: sum(1 for op in ops if op.status == s) for s in ("created", "skipped", "failed")}
    return IntakeResult(
        _rel(current_json, repo_root),
        _rel(current_md, repo_root),
        status=status,
        created=counts["created"],
        skipped=counts["skipped"],
        failed=counts["failed"],
        operations=[op.to_dict() for op in ops],
        ok=counts["failed"] == 0,
    )


def _execute(
    ops: list[IntakeOperation], payload: dict[str, Any], role_arn: str,
    api: Optional[UnityCatalogApi],
) -> str:
    if api is None:
        from core.config import resolve_databricks_config
        from core.execution.databricks_client import DatabricksClient

        layout_enterprise = str(payload.get("catalog") or "")
        api = SdkUnityCatalogApi(DatabricksClient(resolve_databricks_config(layout_enterprise)))

    catalog = str(payload.get("catalog") or "")
    for op in ops:
        if op.status == "requires_copy":
            continue
        try:
            _apply_one(op, api, catalog, role_arn)
        except Exception as exc:
            op.status = "failed"
            op.detail = f"{type(exc).__name__}: {exc}"
            # Stop on first failure: later objects depend on earlier ones, and
            # pressing on would produce a cascade of confusing secondary errors.
            break
    return "failed" if any(op.status == "failed" for op in ops) else "applied"


def _apply_one(op: IntakeOperation, api: UnityCatalogApi, catalog: str, role_arn: str) -> None:
    if op.kind == "storage_credential":
        if api.storage_credential_exists(op.name):
            op.status = "skipped"; return
        api.create_storage_credential(op.name, role_arn)
    elif op.kind == "external_location":
        if api.external_location_exists(op.name):
            op.status = "skipped"; return
        api.create_external_location(op.name, op.detail, f"{catalog}_src")
    elif op.kind == "catalog":
        if api.catalog_exists(op.name):
            op.status = "skipped"; return
        api.create_catalog(op.name)
    elif op.kind == "schema":
        cat, _, schema = op.name.partition(".")
        if api.schema_exists(cat, schema):
            op.status = "skipped"; return
        api.create_schema(cat, schema)
    elif op.kind == "volume":
        parts = op.name.strip("/").split("/")          # Volumes/<cat>/<schema>/<name>
        cat, schema, name = parts[1], parts[2], parts[3]
        if api.volume_exists(cat, schema, name):
            op.status = "skipped"; return
        api.create_external_volume(cat, schema, name, op.detail)
    elif op.kind == "table":
        if api.table_exists(op.name):
            op.status = "skipped"; return
        api.create_external_table(op.name, op.detail)
    else:  # pragma: no cover - guarded by _plan_operations
        raise ValueError(f"unknown operation kind {op.kind!r}")
    op.status = "created"


def _render_markdown(report: dict[str, Any]) -> str:
    status = report["status"]
    lines = [
        "# Unity Catalog intake",
        "",
        f"Source: `{report.get('source_root','')}` -> catalog `{report.get('catalog','')}`",
        f"Blueprint approved by: `{report.get('approved_by','')}`",
        f"Status: `{status}`",
        "",
    ]
    if status == "dry_run":
        lines += ["**DRY RUN -- nothing was created.** Re-run with `--apply` "
                  "(needs `--role-arn` and a human-set AUTORESEARCH_ALLOW_REMOTE_EXECUTION).", ""]
    elif status == "refused_no_remote_approval":
        lines += [f"**REFUSED** -- {report['remote_approval_blocking_reason']}. Nothing created.", ""]
    elif status == "refused_no_role_arn":
        lines += ["**REFUSED** -- no `--role-arn`. Nothing created.", ""]

    lines += ["| Object | Name | Status | Detail |", "|---|---|---|---|"]
    for op in report["operations"]:
        mark = {"created": "[ok]", "skipped": "[~]", "failed": "[x]",
                "requires_copy": "[!]", "planned": "[ ]"}.get(op["status"], "[ ]")
        lines.append(f"| {op['kind']} | `{op['name']}` | {mark} {op['status']} | {op['detail']} |")
    lines.append("")
    if any(op["status"] == "requires_copy" for op in report["operations"]):
        lines += [
            "`requires_copy` means you chose a MANAGED table for that group. Intake "
            "creates governance objects and moves no bytes, so the copy "
            "(`COPY INTO` for thousands of files, Auto Loader for millions) is a "
            "separate, deliberate step.",
            "",
        ]
    return "\n".join(lines)


def _rel(path: Path, root: Path) -> str:
    try:
        return path.resolve().relative_to(root.resolve()).as_posix()
    except ValueError:
        return path.as_posix()


@anchored("apply-uc-intake")
def main(argv: list[str] | None = None) -> int:
    from core.onboarding.workspace.cli_runner import run_workspace_command

    parser = argparse.ArgumentParser()
    parser.add_argument("--workspace", required=True)
    parser.add_argument("--repo-root", default=str(PROJECT_ROOT))
    parser.add_argument("--role-arn", default="", help="AWS IAM role Unity Catalog assumes")
    parser.add_argument("--apply", action="store_true", help="actually create (default: dry run)")
    args = parser.parse_args(argv)
    return run_workspace_command(
        command="apply-uc-intake",
        workspace=args.workspace,
        repo_root=args.repo_root,
        fn=lambda: run_intake(
            args.repo_root, args.workspace, role_arn=args.role_arn, apply=args.apply
        ),
        metadata={"apply": args.apply, "role_arn_provided": bool(args.role_arn)},
    )


if __name__ == "__main__":
    raise SystemExit(main())
