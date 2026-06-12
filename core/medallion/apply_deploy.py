"""`medallion apply-deploy`: evaluate the five deployment gates and, when ALL
pass, record an approved deployment intent — and stop.

This slice deliberately ends at the approval boundary: no Databricks call is
made under any outcome. A fully-approved run writes
``interns/state/medallion/deploy_approval.json`` (gate evidence + provenance
+ the approved plan's hash) and prints what WOULD be executed; the actual
workspace mutation is the next slice's seam (workspace_deployer), which must
require this approval artifact as its input.

Default behavior is gate evaluation + verdict table; any failing gate exits
nonzero. ``--dry-run`` is the same evaluation, explicitly never recording the
approval artifact even on all-green.
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

from core.paths import PROJECT_ROOT
from core.onboarding.databricks.deploy_gates import GateVerdict, run_deploy_gates

APPROVAL_FILENAME = "deploy_approval.json"


def _verdict_table(verdicts: list[GateVerdict]) -> str:
    lines = [
        "gate                  status   detail",
        "--------------------  -------  ------------------------------------------",
    ]
    for v in verdicts:
        marker = "[ok]" if v.ok else "[x]"
        detail = v.blocking_reason if not v.ok else json.dumps(v.evidence, default=str)[:60]
        lines.append(f"{v.gate:<20}  {marker:<7}  {detail}")
    return "\n".join(lines)


def _write_approval(
    repo_root: Path, workspace_rel: str, verdicts: list[GateVerdict], confirmed_by: str
) -> Path:
    workspace = (repo_root / workspace_rel).resolve()
    plan_path = workspace / "interns" / "generated" / "medallion" / "deploy_plan.json"
    plan = json.loads(plan_path.read_text(encoding="utf-8"))
    approval = {
        "artifact_type": "medallion/deploy_approval.json",
        "version": 1,
        "workspace": workspace_rel,
        "approved_at": datetime.now(timezone.utc).isoformat(),
        "confirmed_by": confirmed_by,
        "source": "human",
        "plan_inputs_hash": (plan.get("source_manifest") or {}).get("inputs_hash"),
        "plan_path": str(plan_path.relative_to(repo_root).as_posix()),
        "gates": [v.to_dict() for v in verdicts],
        "next_step": (
            "run `medallion deploy --workspace <ws>` (workspace_deployer) to "
            "consume this approval and perform the Unity Catalog deployment "
            "described in the plan; no remote call has been made yet"
        ),
    }
    out_dir = workspace / "interns" / "state" / "medallion"
    out_dir.mkdir(parents=True, exist_ok=True)
    out = out_dir / APPROVAL_FILENAME
    out.write_text(json.dumps(approval, indent=2) + "\n", encoding="utf-8")
    return out


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="medallion apply-deploy",
        description="Evaluate the 5 deployment gates; record approval when all pass (no remote calls).",
    )
    parser.add_argument("--workspace", required=True)
    parser.add_argument("--repo-root", default=str(PROJECT_ROOT))
    parser.add_argument("--confirmed-by", default="", help="Human approver name (required to pass G3).")
    parser.add_argument(
        "--dry-run", action="store_true",
        help="Evaluate and report only; never record the approval artifact.",
    )
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)

    repo_root = Path(args.repo_root).resolve()
    verdicts = run_deploy_gates(repo_root, args.workspace, confirmed_by=args.confirmed_by)
    all_ok = all(v.ok for v in verdicts)

    approval_path = ""
    if all_ok and not args.dry_run:
        approval_path = str(_write_approval(repo_root, args.workspace, verdicts, args.confirmed_by))

    if args.json:
        print(json.dumps({
            "action": "apply-deploy",
            "dry_run": args.dry_run,
            "all_gates_ok": all_ok,
            "gates": [v.to_dict() for v in verdicts],
            "approval_path": approval_path,
            "remote_mutation": "none (approval boundary; deployer slice pending)",
        }, indent=2, default=str))
    else:
        print(_verdict_table(verdicts))
        if all_ok and approval_path:
            print(f"\n[ok] all gates passed - approval recorded: {approval_path}")
            print("[ok] no remote call made; the deployer slice consumes this approval next")
        elif all_ok:
            print("\n[ok] all gates passed (dry run - no approval recorded)")
        else:
            failed = [v.gate for v in verdicts if not v.ok]
            print(f"\n[x] blocked by: {', '.join(failed)}")
            print("[x] no approval recorded; resolve the blockers above and re-run")
    if not all_ok:
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
