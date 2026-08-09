"""The five deployment gates from docs/prd/databricks_deployment.md section 7.

Each gate is a pure, individually-testable check returning a GateVerdict;
``run_deploy_gates`` evaluates all five and never short-circuits, so a dry
run reports the complete picture. Nothing here performs any remote action —
the gates only READ local artifacts and the environment.

G1 local-green      latest medallion run has no failed tables (no degraded
                    run), and the execution harness evidence is ok.
G2 design ratified  zero open medallion design-panel decisions.
G3 human provenance --confirmed-by nonempty -> source: human (Human-Gate
                    Provenance Rule; agent-asserted approval never passes).
G4 plan freshness   the deploy plan's source_manifest.inputs_hash matches the
                    manifest on disk right now.
G5 remote approval  AUTORESEARCH_ALLOW_REMOTE_EXECUTION=1 in the environment,
                    set by a human shell — this check only VERIFIES, never sets.
"""
from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

REMOTE_ENV = "AUTORESEARCH_ALLOW_REMOTE_EXECUTION"

GATE_NAMES = (
    "G1_local_green",
    "G2_design_ratified",
    "G3_human_provenance",
    "G4_plan_freshness",
    "G5_remote_approval",
)


@dataclass(frozen=True)
class GateVerdict:
    gate: str
    ok: bool
    evidence: dict[str, Any] = field(default_factory=dict)
    blocking_reason: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "gate": self.gate,
            "ok": self.ok,
            "evidence": self.evidence,
            "blocking_reason": self.blocking_reason,
        }


def _load_json(path: Path) -> dict[str, Any] | None:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else None
    except (json.JSONDecodeError, OSError):
        return None


def check_local_green(workspace: Path) -> GateVerdict:
    """G1: the latest medallion run and the execution harness are green."""
    gate = "G1_local_green"
    runs_dir = workspace / "interns" / "state" / "medallion" / "runs"
    try:
        run_dirs = sorted(d for d in runs_dir.iterdir() if d.is_dir()) if runs_dir.is_dir() else []
    except OSError as exc:
        return GateVerdict(gate, False, blocking_reason=f"cannot list medallion runs at {runs_dir}: {exc}")
    if not run_dirs:
        return GateVerdict(gate, False, blocking_reason="no medallion run recorded; run `medallion build` first")
    run = _load_json(run_dirs[-1] / "run.json")
    if run is None:
        return GateVerdict(gate, False, blocking_reason=f"unreadable run state: {run_dirs[-1].name}/run.json")
    statuses = {
        str(t): str(v.get("status") if isinstance(v, dict) else v)
        for t, v in (run.get("per_table_status") or {}).items()
    }
    failed = sorted(t for t, s in statuses.items() if s not in ("built", "skipped", "ok"))
    degraded = bool(run.get("degraded_run"))
    kpi_diff = run.get("kpi_diff") or {}
    unequal = [k for k, v in kpi_diff.items() if isinstance(v, dict) and not v.get("equal", True)]

    harness = _load_json(workspace / "interns" / "generated" / "evidence" / "kpi_execution_harness.json")
    harness_ok = bool(harness and harness.get("ok"))

    evidence = {
        "run_id": run.get("run_id"),
        "tables_failed": failed,
        "degraded_run": degraded,
        "kpi_diff_unequal": unequal,
        "execution_harness_ok": harness_ok,
        "harness_passed": (harness or {}).get("passed_count"),
        "harness_failed": (harness or {}).get("failed_count"),
    }
    reasons = []
    if failed:
        reasons.append(f"medallion tables failed: {failed}")
    if degraded:
        reasons.append("latest medallion run was degraded")
    if unequal:
        reasons.append(f"KPI row-equality diff has unequal KPIs: {unequal}")
    if not harness_ok:
        reasons.append("execution harness evidence missing or not ok")
    return GateVerdict(gate, not reasons, evidence=evidence, blocking_reason="; ".join(reasons))


def check_design_ratified(workspace: Path) -> GateVerdict:
    """G2: zero open medallion design-panel decisions. --force-with-blockers
    history does not help — the panel itself must be clean."""
    gate = "G2_design_ratified"
    panel = _load_json(workspace / "interns" / "reports" / "medallion_design_panel" / "current.json")
    if panel is None:
        return GateVerdict(
            gate, False,
            blocking_reason="no medallion design panel found; run `medallion design` first",
        )
    raw_open = panel.get("open_count") or 0
    try:
        open_count = int(raw_open)
    except (TypeError, ValueError):
        return GateVerdict(
            gate, False,
            evidence={"open_count": raw_open},
            blocking_reason=f"panel open_count is not a number: {raw_open!r}; regenerate the design panel",
        )
    items = [
        str(i.get("id") or "?") if isinstance(i, dict) else str(i)
        for i in (panel.get("items") or [])
    ]
    evidence = {"open_count": open_count, "open_items": items[:10]}
    if open_count:
        return GateVerdict(
            gate, False, evidence=evidence,
            blocking_reason=(
                f"{open_count} unratified design decision(s): {items[:5]}; a human "
                "must ratify them (never deploy past --force-with-blockers)"
            ),
        )
    return GateVerdict(gate, True, evidence=evidence)


def check_human_provenance(confirmed_by: str) -> GateVerdict:
    """G3: deployment approval must be human-attributed, never agent-asserted."""
    gate = "G3_human_provenance"
    name = (confirmed_by or "").strip()
    if not name:
        return GateVerdict(
            gate, False,
            evidence={"source": "agent"},
            blocking_reason=(
                "--confirmed-by is empty: approval would record source: agent; "
                "deployment requires a human name (Human-Gate Provenance Rule)"
            ),
        )
    return GateVerdict(gate, True, evidence={"source": "human", "confirmed_by": name})


def check_plan_freshness(workspace: Path, repo_root: Path) -> GateVerdict:
    """G4: the plan's recorded manifest hash matches the manifest NOW."""
    gate = "G4_plan_freshness"
    plan = _load_json(workspace / "interns" / "generated" / "medallion" / "deploy_plan.json")
    if plan is None:
        return GateVerdict(gate, False, blocking_reason="no deploy plan found; run `medallion plan-deploy` first")
    recorded = str((plan.get("source_manifest") or {}).get("inputs_hash") or "")
    manifest_rel = str((plan.get("source_manifest") or {}).get("path") or "")
    if not recorded or not manifest_rel:
        return GateVerdict(gate, False, blocking_reason="plan lacks source_manifest.inputs_hash/path")
    try:
        from core.medallion.deploy_plan import _load_manifest

        current = _load_manifest(Path(repo_root) / manifest_rel).inputs_hash
    except Exception as exc:
        return GateVerdict(gate, False, blocking_reason=f"cannot recompute manifest hash: {exc}")
    evidence = {"plan_hash": recorded, "current_hash": current}
    if recorded != current:
        return GateVerdict(
            gate, False, evidence=evidence,
            blocking_reason="plan is stale (manifest changed since plan-deploy); regenerate the plan",
        )
    return GateVerdict(gate, True, evidence=evidence)


def check_remote_approval() -> GateVerdict:
    """G5: human-set remote-execution env var present. VERIFY only."""
    gate = "G5_remote_approval"
    value = os.environ.get(REMOTE_ENV, "")
    if value != "1":
        return GateVerdict(
            gate, False,
            evidence={REMOTE_ENV: value or "<unset>"},
            blocking_reason=(
                f"{REMOTE_ENV} is not 1; a human must set it in the executing "
                "shell (agents must never set it)"
            ),
        )
    return GateVerdict(gate, True, evidence={REMOTE_ENV: "1"})


def run_deploy_gates(
    repo_root: Path, workspace_rel: str, *, confirmed_by: str = ""
) -> list[GateVerdict]:
    """Evaluate all five gates; never short-circuits."""
    repo_root = Path(repo_root).resolve()
    workspace = (repo_root / workspace_rel).resolve()
    return [
        check_local_green(workspace),
        check_design_ratified(workspace),
        check_human_provenance(confirmed_by),
        check_plan_freshness(workspace, repo_root),
        check_remote_approval(),
    ]


def main(argv: list[str] | None = None) -> int:
    """Standalone CLI: is `AUTORESEARCH_ALLOW_REMOTE_EXECUTION=1` set right now?

    Reuses `check_remote_approval()` -- the same G5 check the medallion deploy
    path gates on -- as a fast synchronous gate other execution surfaces that
    aren't Python call sites (a shell command chain, e.g. `pipeline_stages.py`'s
    `DBT_BUILD_STAGE`) can shell out to: `cmd1 && check-remote-execution-gate &&
    cmd2`. Prints the blocking reason and exits 1 when not set; silent exit 0
    when set. No `--workspace` -- this checks one process-global env var, not
    workspace state.
    """
    verdict = check_remote_approval()
    if not verdict.ok:
        print(f"[x] {verdict.blocking_reason}")
        return 1
    return 0


__all__ = [
    "GATE_NAMES",
    "GateVerdict",
    "REMOTE_ENV",
    "check_design_ratified",
    "check_human_provenance",
    "check_local_green",
    "check_plan_freshness",
    "check_remote_approval",
    "main",
    "run_deploy_gates",
]


if __name__ == "__main__":
    raise SystemExit(main())
