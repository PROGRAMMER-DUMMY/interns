"""CLI for local hardware/resource preflight."""
from __future__ import annotations
from core.observability.cost_ledger import anchored

import argparse
import json
from pathlib import Path

from core.paths import PROJECT_ROOT
from core.resource.manager import ResourceManager



@anchored("resource-preflight")
def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Write resource preflight evidence for a workspace.")
    parser.add_argument("--workspace", required=True, help="Workspace path, for example workspaces/demo")
    parser.add_argument("--repo-root", default=str(PROJECT_ROOT), help="Repository root.")
    parser.add_argument("--estimated-bytes", type=int, default=0)
    parser.add_argument("--workload", default="generic")
    parser.add_argument("--json", action="store_true", help="Print JSON.")
    args = parser.parse_args(argv)

    repo_root = Path(args.repo_root).resolve()
    workspace = (repo_root / args.workspace).resolve()
    manager = ResourceManager(workspace, repo_root=repo_root)
    artifacts = manager.write_report(estimated_bytes=args.estimated_bytes, workload=args.workload)
    decision = manager.decide(estimated_bytes=args.estimated_bytes, workload=args.workload)
    payload = {
        "workspace": args.workspace,
        "status": decision.status,
        "mode": decision.mode,
        "artifacts": artifacts,
        "warnings": decision.warnings,
        "blockers": decision.blockers,
    }
    if args.json:
        print(json.dumps(payload, indent=2))
    else:
        print(f"Resource preflight: {payload['status']} ({payload['mode']})")
        print(f"Report: {artifacts['report']}")
        if decision.blockers:
            print(f"Blockers: {len(decision.blockers)}")
        if decision.warnings:
            print(f"Warnings: {len(decision.warnings)}")
    return 1 if decision.blockers else 0


if __name__ == "__main__":
    raise SystemExit(main())
