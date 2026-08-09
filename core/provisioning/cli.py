"""Governed CLI entry points for provisioning + ingestion generation."""
from __future__ import annotations
from core.observability.cost_ledger import anchored

import argparse
from pathlib import Path
from typing import Any

from core.onboarding.workspace.cli_runner import run_workspace_command
from core.onboarding.workspace.idempotency import fingerprint_paths
from core.paths import PROJECT_ROOT
from core.provisioning.apply import apply_provision_plan
from core.provisioning.ingestion import generate_ingestion
from core.provisioning.ingestion_run import run_ingestion_jobs
from core.provisioning.plan import DEFAULT_SCHEMAS, build_provision_plan
from core.provisioning.sync_code import sync_workspace_code


@anchored("plan-provisioning")
def plan_provisioning_main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Plan additive-only Unity Catalog provisioning for a workspace."
    )
    parser.add_argument("--workspace", required=True)
    parser.add_argument("--repo-root", default=str(PROJECT_ROOT))
    parser.add_argument("--catalog", default="", help="catalog base name (env suffix is added)")
    parser.add_argument("--env", default="dev", help="dev | prod (catalog-per-env naming)")
    parser.add_argument(
        "--schema", action="append", default=[],
        help="medallion schema to provision; repeatable (default bronze/silver/gold)",
    )
    parser.add_argument(
        "--grant-principal", action="append", default=[],
        help="principal to grant read access on a NEWLY created catalog; repeatable",
    )
    parser.add_argument(
        "--storage-root", default="",
        help=(
            "catalog MANAGED LOCATION (e.g. s3://bucket/). Required on a metastore "
            "with no storage root of its own; omit to inherit the metastore's. "
            "Where managed data physically lives is a residency decision, so it is "
            "never derived from the source location"
        ),
    )
    args = parser.parse_args(argv)
    schemas = tuple(args.schema) or DEFAULT_SCHEMAS
    return run_workspace_command(
        command="plan-provisioning",
        workspace=args.workspace,
        repo_root=args.repo_root,
        fn=lambda: build_provision_plan(
            args.repo_root,
            args.workspace,
            catalog=args.catalog,
            env=args.env,
            schemas=schemas,
            grant_principals=tuple(args.grant_principal),
            storage_root=args.storage_root,
        ),
        metadata={"env": args.env, "catalog": args.catalog},
        validation="validate-workspace-artifacts",
    )


@anchored("apply-provisioning")
def apply_provisioning_main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Apply the provision plan. Requires the confirmed solution blueprint; "
            "without it this is a dry run."
        )
    )
    parser.add_argument("--workspace", required=True)
    parser.add_argument("--repo-root", default=str(PROJECT_ROOT))
    group = parser.add_mutually_exclusive_group()
    group.add_argument(
        "--dry-run", dest="dry_run", action="store_true", default=None,
        help="plan-only; default is OFF once the blueprint is confirmed, ON otherwise",
    )
    group.add_argument("--no-dry-run", dest="dry_run", action="store_false")
    parser.add_argument("--allow-replay", action="store_true")
    args = parser.parse_args(argv)

    holder: dict[str, Any] = {}

    def _run() -> Any:
        holder["result"] = apply_provision_plan(
            args.repo_root, args.workspace, dry_run=args.dry_run
        )
        return holder["result"]

    # The op-id must cover the ARTIFACT this command consumes, not just its
    # flags. Without the plan's fingerprint, three runs against three
    # materially different plans shared one op_id and the envelope reported
    # "this exact call was already applied" about a call whose plan had
    # changed. (F16)
    code = run_workspace_command(
        command="apply-provisioning",
        workspace=args.workspace,
        repo_root=args.repo_root,
        fn=_run,
        op_args={
            "workspace": args.workspace,
            "dry_run": args.dry_run,
            "plan_fingerprint": fingerprint_paths(
                Path(args.repo_root) / args.workspace
                / "interns" / "generated" / "contracts" / "provision_plan.json"
            ),
        },
        allow_replay=args.allow_replay,
        metadata={"dry_run": args.dry_run},
        record_idempotent=True,
    )
    if code != 0:
        return code
    result = holder.get("result")
    # A refusal (no confirmation / kill-switch / Databricks unreachable) is a
    # clean structured payload, not a traceback -- but it must not exit 0.
    return 0 if result is None or result.ok else 1


@anchored("generate-ingestion")
def generate_ingestion_main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Generate Databricks-native ingestion code per discovered table."
    )
    parser.add_argument("--workspace", required=True)
    parser.add_argument("--repo-root", default=str(PROJECT_ROOT))
    parser.add_argument("--allow-replay", action="store_true")
    args = parser.parse_args(argv)
    return run_workspace_command(
        command="generate-ingestion",
        workspace=args.workspace,
        repo_root=args.repo_root,
        fn=lambda: generate_ingestion(args.repo_root, args.workspace),
        op_args={"workspace": args.workspace},
        allow_replay=args.allow_replay,
        record_idempotent=True,
    )


@anchored("run-ingestion")
def run_ingestion_main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Run the generated ingestion jobs against the SQL warehouse. Requires "
            "the confirmed solution blueprint; without it this is a dry run. "
            "COPY INTO is idempotent by file bookkeeping, so re-running is safe."
        )
    )
    parser.add_argument("--workspace", required=True)
    parser.add_argument("--repo-root", default=str(PROJECT_ROOT))
    group = parser.add_mutually_exclusive_group()
    group.add_argument(
        "--dry-run", dest="dry_run", action="store_true", default=None,
        help="list-only; default is OFF once the blueprint is confirmed, ON otherwise",
    )
    group.add_argument("--no-dry-run", dest="dry_run", action="store_false")
    parser.add_argument("--allow-replay", action="store_true")
    args = parser.parse_args(argv)

    holder: dict[str, Any] = {}

    def _run() -> Any:
        holder["result"] = run_ingestion_jobs(
            args.repo_root, args.workspace, dry_run=args.dry_run
        )
        return holder["result"]

    # Same contract as apply-provisioning: the emitted jobs manifest IS the
    # input, so a regenerated manifest must not replay as the prior run. (F16)
    code = run_workspace_command(
        command="run-ingestion",
        workspace=args.workspace,
        repo_root=args.repo_root,
        fn=_run,
        op_args={
            "workspace": args.workspace,
            "dry_run": args.dry_run,
            "manifest_fingerprint": fingerprint_paths(
                Path(args.repo_root) / args.workspace
                / "ingestion" / "jobs_manifest.json"
            ),
        },
        allow_replay=args.allow_replay,
        metadata={"dry_run": args.dry_run},
        record_idempotent=True,
    )
    if code != 0:
        return code
    result = holder.get("result")
    return 0 if result is None or result.ok else 1


@anchored("sync-workspace-code")
def sync_workspace_code_main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Ship generated ingestion/ and dbt/ code to the Databricks workspace "
            "with `databricks sync`. Requires the confirmed solution blueprint; "
            "without it this is a dry run."
        )
    )
    parser.add_argument("--workspace", required=True)
    parser.add_argument("--repo-root", default=str(PROJECT_ROOT))
    parser.add_argument(
        "--remote-root", default="",
        help="remote target dir (default /Workspace/Shared/<workspace-name>)",
    )
    group = parser.add_mutually_exclusive_group()
    group.add_argument(
        "--dry-run", dest="dry_run", action="store_true", default=None,
        help="push nothing; default is OFF once the blueprint is confirmed, ON otherwise",
    )
    group.add_argument("--no-dry-run", dest="dry_run", action="store_false")
    parser.add_argument("--allow-replay", action="store_true")
    args = parser.parse_args(argv)

    holder: dict[str, Any] = {}

    def _run() -> Any:
        holder["result"] = sync_workspace_code(
            args.repo_root,
            args.workspace,
            dry_run=args.dry_run,
            remote_root=args.remote_root or None,
        )
        return holder["result"]

    code = run_workspace_command(
        command="sync-workspace-code",
        workspace=args.workspace,
        repo_root=args.repo_root,
        fn=_run,
        op_args={
            "workspace": args.workspace,
            "dry_run": args.dry_run,
            "remote_root": args.remote_root,
        },
        allow_replay=args.allow_replay,
        metadata={"dry_run": args.dry_run},
        record_idempotent=True,
    )
    if code != 0:
        return code
    result = holder.get("result")
    # A refusal (no confirmation / kill-switch / non-zero sync) is a clean
    # structured payload, not a traceback -- but it must not exit 0.
    return 0 if result is None or result.get("ok") else 1


__all__ = [
    "apply_provisioning_main",
    "generate_ingestion_main",
    "plan_provisioning_main",
    "sync_workspace_code_main",
]
