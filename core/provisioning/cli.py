"""Governed CLI entry points for provisioning + ingestion generation."""
from __future__ import annotations
from core.observability.cost_ledger import anchored

import argparse
from typing import Any

from core.onboarding.workspace.cli_runner import run_workspace_command
from core.paths import PROJECT_ROOT
from core.provisioning.apply import apply_provision_plan
from core.provisioning.ingestion import generate_ingestion
from core.provisioning.plan import DEFAULT_SCHEMAS, build_provision_plan


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

    code = run_workspace_command(
        command="apply-provisioning",
        workspace=args.workspace,
        repo_root=args.repo_root,
        fn=_run,
        op_args={"workspace": args.workspace, "dry_run": args.dry_run},
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


__all__ = [
    "apply_provisioning_main",
    "generate_ingestion_main",
    "plan_provisioning_main",
]
