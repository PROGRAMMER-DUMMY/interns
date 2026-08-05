"""CLI entry points for the blueprint slice.

Two commands, both through the standard ``run_workspace_command`` envelope
(workspace lock, timing, trajectory events):

* ``prepare-blueprint``  -- evaluate the decision tables and render the blueprint.
* ``confirm-blueprint``  -- the ONE human gate. Requires ``--confirmed-by`` and
  records human provenance per the Human-Gate Provenance Rule; an agent identity
  is refused rather than silently stamped as human approval.

Confirmation is refused while any decision is blocked: a blueprint with a hole in
it is not a plan, and approving one would turn "we never measured that" into
"the human accepted it".
"""
from __future__ import annotations

import argparse
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from core.blueprint.decisions import STATUS_BLOCKED
from core.blueprint.renderer import blueprint_dir, prepare_blueprint
from core.governance.provenance import decision_source
from core.observability.cost_ledger import anchored
from core.paths import PROJECT_ROOT
from core.storage.workspace_layout import WorkspaceLayout


def confirm_blueprint(
    repo_root: str | Path, workspace: str | Path, *, confirmed_by: str
) -> dict[str, Any]:
    repo_root = Path(repo_root).resolve()
    layout = WorkspaceLayout(project_root=(repo_root / workspace).resolve())
    current = blueprint_dir(layout) / "current.json"
    if not current.exists():
        raise FileNotFoundError(
            f"no blueprint at {current}. Run `prepare-blueprint --workspace {workspace}` first."
        )
    payload = json.loads(current.read_text(encoding="utf-8"))
    if str(payload.get("generated_by") or "") != "prepare-blueprint":
        raise ValueError(
            f"{current} was written by `{payload.get('generated_by')}`, not `prepare-blueprint`. "
            "Re-run `prepare-blueprint` before confirming so the confirmation covers this "
            "blueprint's decisions."
        )

    source = decision_source(confirmed_by)
    if source != "human":
        raise PermissionError(
            f"--confirmed-by {confirmed_by!r} resolves to `{source}`, not a human. This "
            "confirmation authorises creating catalogs, schemas, external locations, tables "
            "and DAGs; that needs a real name (Human-Gate Provenance Rule)."
        )

    blocked = [
        d
        for d in (payload.get("decisions") or {}).get("decisions", [])
        if d.get("status") == STATUS_BLOCKED
    ]
    if blocked:
        missing = sorted({m for d in blocked for m in d.get("missing_facts", [])})
        raise ValueError(
            f"{len(blocked)} decision(s) are blocked on missing fact(s): {', '.join(missing)}. "
            "Measure them in discovery or answer them in the intake interview, re-run "
            "`prepare-blueprint`, then confirm."
        )

    payload["status"] = "confirmed"
    payload["confirmation"] = {
        "confirmed_by": confirmed_by,
        "source": source,
        "confirmed_at": datetime.now(timezone.utc).isoformat(),
    }
    confirmed_path = blueprint_dir(layout) / "current.confirmed.json"
    confirmed_path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    current.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    return {
        "confirmed_path": _rel(confirmed_path, repo_root),
        "confirmed_by": confirmed_by,
        "source": source,
        "status": "confirmed",
        "ok": True,
    }


def _rel(path: Path, root: Path) -> str:
    try:
        return path.resolve().relative_to(root.resolve()).as_posix()
    except ValueError:
        return path.as_posix()


@anchored("prepare-blueprint")
def prepare_blueprint_main(argv: list[str] | None = None) -> int:
    from core.onboarding.workspace.cli_runner import run_workspace_command

    parser = argparse.ArgumentParser(
        description="Evaluate the blueprint decision tables and render the pipeline blueprint."
    )
    parser.add_argument("--workspace", required=True)
    parser.add_argument("--repo-root", default=str(PROJECT_ROOT))
    parser.add_argument("--catalog", default="", help="Target catalog; defaults to the workspace name.")
    parser.add_argument("--bronze-schema", default="bronze")
    parser.add_argument("--silver-schema", default="silver")
    parser.add_argument("--gold-schema", default="gold")
    args = parser.parse_args(argv)

    holder: dict[str, Any] = {}

    def _run() -> Any:
        # The alignment gate refusing is an expected outcome, like confirm's:
        # a clean structured payload, not a traceback -- but not exit 0 either.
        try:
            holder["result"] = prepare_blueprint(
                args.repo_root,
                args.workspace,
                catalog=args.catalog,
                bronze_schema=args.bronze_schema,
                silver_schema=args.silver_schema,
                gold_schema=args.gold_schema,
            )
        except ValueError as exc:
            holder["result"] = {"ok": False, "status": "refused", "reason": str(exc)}
            print(f"[x] prepare-blueprint refused: {exc}")
        return holder["result"]

    code = run_workspace_command(
        command="prepare-blueprint",
        workspace=args.workspace,
        repo_root=args.repo_root,
        fn=_run,
        metadata={"catalog": args.catalog},
    )
    if code != 0:
        return code
    result = holder.get("result")
    return 0 if not isinstance(result, dict) or result.get("ok") else 1


@anchored("confirm-blueprint")
def confirm_blueprint_main(argv: list[str] | None = None) -> int:
    from core.onboarding.workspace.cli_runner import run_workspace_command

    parser = argparse.ArgumentParser(
        description="Record the ONE human confirmation of the rendered blueprint."
    )
    parser.add_argument("--workspace", required=True)
    parser.add_argument("--repo-root", default=str(PROJECT_ROOT))
    parser.add_argument("--confirmed-by", required=True, help="Real human name; agent identities are refused.")
    args = parser.parse_args(argv)

    holder: dict[str, Any] = {}

    def _run() -> Any:
        # A refusal (blocked decisions, agent identity, stale/foreign blueprint)
        # is an expected outcome: a clean structured payload, not a traceback --
        # but it must not exit 0.
        try:
            holder["result"] = confirm_blueprint(
                args.repo_root, args.workspace, confirmed_by=args.confirmed_by
            )
        except (ValueError, PermissionError, FileNotFoundError) as exc:
            holder["result"] = {"ok": False, "status": "refused", "reason": str(exc)}
            print(f"[x] confirm-blueprint refused: {exc}")
        return holder["result"]

    code = run_workspace_command(
        command="confirm-blueprint",
        workspace=args.workspace,
        repo_root=args.repo_root,
        fn=_run,
        metadata={"confirmed_by": args.confirmed_by},
        record_idempotent=True,
    )
    if code != 0:
        return code
    result = holder.get("result") or {}
    return 0 if result.get("ok") else 1


__all__ = ["confirm_blueprint", "confirm_blueprint_main", "prepare_blueprint_main"]


if __name__ == "__main__":
    raise SystemExit(prepare_blueprint_main())
