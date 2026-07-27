"""Bounded, dry-run-capable backfill for a workspace's dbt project.

Backfill is never automatic -- every generated Airflow DAG sets
`catchup=False` (see `airflow_dag.build_dag()`) -- it is a deliberate,
separate trigger, per the dbt+Airflow integration plan's backfill-safety
section (plan section 4). This module is that trigger: it resolves a date
range into dbt's microbatch `--event-time-start`/`--event-time-end` flags
(dbt-core 1.9+) and refuses to actually run a span wider than a bounded
default without an explicit human confirmation -- the same Human-Gate
Provenance Rule pattern (`core.governance.provenance`) used everywhere else
a decision needs a real name, not an agent guess.

`--dry-run` is NOT the safety mechanism -- the bound is. Dry-run always
succeeds and always writes the report (including a wide span that WOULD be
refused), so a human can review the resolved command and the bound check
before deciding whether to add `--confirmed-by`.

`--defer` narrows the same invocation to `state:modified+` and resolves every
unchanged `ref()` against the production artifacts at `DBT_STATE_PATH`, so a
backfill rebuilds what actually changed instead of the whole graph. The state
path must be object storage (or at least outside the working tree) -- see
`_resolve_state_path`.

This CLI performs one synchronous `dbt build` invocation -- no internal
concurrency, so a Databricks job-level `max_concurrent_runs: 1` guard
(coordinating with bronze ingestion checkpoints, per plan section 4) is the
caller's responsibility if this is wired into a scheduled job, not something
this script manages itself.
"""
from __future__ import annotations
from core.observability.cost_ledger import anchored

import argparse
import json
import os
import subprocess
from dataclasses import dataclass
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any

from core.contracts.versioning import register_contract
from core.governance.provenance import decision_source as decision_source_for
from core.observability.cost_ledger import RUN_ID_ENV, current_run_id
from core.onboarding.databricks.deploy_gates import check_remote_approval
from core.paths import PROJECT_ROOT
from core.storage.workspace_layout import WorkspaceLayout

REPORT_VERSION = 1
register_contract("dbt_backfill/current.json", current_version=REPORT_VERSION)

# A single backfill invocation cannot target an arbitrarily wide date range
# without explicit approval above this threshold -- plan section 4's own
# wording ("a defined threshold"). Not tuned per-workspace; widen via
# --max-span-days + --confirmed-by when a real case needs it.
DEFAULT_MAX_SPAN_DAYS = 31

# Where the PRODUCTION dbt artifacts (manifest.json) live, for `--defer` +
# `--select state:modified+`. Deliberately an env var pointing at object
# storage, never a manifest committed to this repo: a committed manifest is a
# snapshot of one machine's last run that goes stale the moment prod moves on,
# and every branch then merges conflicting binaries. Set it to the location the
# production job uploads its `target/` to.
DBT_STATE_PATH_ENV = "DBT_STATE_PATH"

# What `--defer` selects when the caller names no `--select`. `state:modified+`
# is the whole point: build only what changed plus its children, and resolve
# every unchanged `ref()` against prod instead of rebuilding it.
STATE_MODIFIED_SELECTOR = "state:modified+"


def _resolve_state_path() -> str:
    """The production-artifacts path for `--defer`, validated.

    Refuses a path inside this repo: that is the committed-manifest anti-pattern
    the plan rules out. A local path must exist -- otherwise dbt discovers the
    miss only after the span bound and remote gate have already passed. A remote
    URI (`s3://`, `abfss://`, ...) is taken on trust; only dbt can resolve it.
    """
    raw = (os.environ.get(DBT_STATE_PATH_ENV) or "").strip()
    if not raw:
        raise ValueError(
            f"--defer needs {DBT_STATE_PATH_ENV} set to the location the production "
            "job uploads its dbt `target/` to (e.g. s3://<bucket>/dbt-state/prod). "
            "It is intentionally not a manifest committed to this repo."
        )
    if "://" in raw:
        return raw
    resolved = Path(raw).expanduser().resolve()
    if resolved.is_relative_to(PROJECT_ROOT):
        raise ValueError(
            f"{DBT_STATE_PATH_ENV} points inside the repo ({resolved}). A committed "
            "manifest goes stale the moment production moves on. Point it at object "
            "storage, or at a path outside the working tree."
        )
    if not (resolved / "manifest.json").exists():
        raise FileNotFoundError(
            f"{DBT_STATE_PATH_ENV}={resolved} has no manifest.json -- nothing to "
            "compare against, so `state:modified` would select every model."
        )
    return str(resolved)


@dataclass(frozen=True)
class BackfillResult:
    current_json_path: str
    current_markdown_path: str
    status: str  # "dry_run" | "executed"
    ok: bool = True

    def summary(self) -> dict[str, Any]:
        return self.__dict__.copy()


class DbtBackfillRunner:
    def __init__(self, repo_root: str | Path, workspace: str | Path) -> None:
        self.repo_root = Path(repo_root).resolve()
        self.workspace = (self.repo_root / workspace).resolve()
        self.layout = WorkspaceLayout(project_root=self.workspace)

    def run(
        self,
        *,
        event_time_start: str,
        event_time_end: str,
        dry_run: bool = False,
        confirmed_by: str = "",
        max_span_days: int = DEFAULT_MAX_SPAN_DAYS,
        select: str = "",
        defer: bool = False,
    ) -> BackfillResult:
        start = _parse_date(event_time_start, "--event-time-start")
        end = _parse_date(event_time_end, "--event-time-end")
        if end < start:
            raise ValueError("--event-time-end must not be before --event-time-start")
        span_days = (end - start).days + 1

        dbt_dir = self.workspace / "dbt"
        if not (dbt_dir / "dbt_project.yml").exists():
            raise FileNotFoundError(
                f"{dbt_dir}/dbt_project.yml not found -- run "
                "`generate-dbt-project --workspace <ws>` first."
            )

        confirmed_by = (confirmed_by or "").strip()
        source = decision_source_for(confirmed_by)
        over_bound = span_days > max_span_days
        would_refuse = over_bound and source != "human"
        # Independent of the span-bound check above: a within-bound backfill
        # still executes real `dbt build` against the workspace's configured
        # target (often prod). Same G5 gate the medallion deploy path uses --
        # AUTORESEARCH_ALLOW_REMOTE_EXECUTION must be set by a human's own
        # shell; nothing in this codebase ever sets it programmatically.
        remote_gate = check_remote_approval()

        # Resolved BEFORE the gates so a misconfigured DBT_STATE_PATH fails on
        # the config, not halfway through a warehouse run.
        state_path = _resolve_state_path() if defer else ""
        run_id, run_id_source = current_run_id(_rel(self.workspace, self.repo_root))
        effective_select = select or (STATE_MODIFIED_SELECTOR if defer else "")

        cmd = [
            "dbt", "build",
            "--project-dir", str(dbt_dir),
            "--profiles-dir", str(dbt_dir),
            "--event-time-start", start.isoformat(),
            "--event-time-end", end.isoformat(),
        ]
        if defer:
            cmd += ["--defer", "--state", state_path]
        if effective_select:
            cmd += ["--select", effective_select]

        returncode: int | None = None
        stdout = ""
        stderr = ""
        if dry_run:
            status = "dry_run"
        elif would_refuse:
            status = "refused"
        elif not remote_gate.ok:
            status = "refused_no_remote_approval"
        else:
            # dbt's query_tags carry `run_id` as an env_var() -- resolved here,
            # in the child's environment, so every warehouse query this build
            # issues is attributable back to this run's ledger rows. Without it
            # the tag is tenant-scoped only and the cost cannot be reconciled.
            child_env = {**os.environ, RUN_ID_ENV: run_id}
            proc = subprocess.run(
                cmd, cwd=str(self.repo_root), capture_output=True, text=True, env=child_env,
            )
            returncode = proc.returncode
            stdout, stderr = proc.stdout, proc.stderr
            status = "executed" if returncode == 0 else "failed"

        report = {
            "artifact_type": "dbt_backfill/current.json",
            "version": REPORT_VERSION,
            "generated_by": "run-dbt-backfill",
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "event_time_start": start.isoformat(),
            "event_time_end": end.isoformat(),
            "span_days": span_days,
            "max_span_days": max_span_days,
            "over_bound": over_bound,
            "would_refuse": would_refuse,
            "remote_approval_ok": remote_gate.ok,
            "remote_approval_blocking_reason": remote_gate.blocking_reason,
            "confirmed_by": confirmed_by,
            "source": source,
            "dry_run": dry_run,
            "run_id": run_id,
            "run_id_source": run_id_source,
            "defer": defer,
            "state_path": state_path,
            "select": effective_select,
            "status": status,
            "command": cmd,
            "returncode": returncode,
            "stdout_tail": stdout[-4000:],
            "stderr_tail": stderr[-4000:],
        }
        panel_dir = self.layout.reports_dir / "dbt_backfill"
        panel_dir.mkdir(parents=True, exist_ok=True)
        current_json = panel_dir / "current.json"
        current_md = panel_dir / "current.md"
        current_json.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
        current_md.write_text(_render_markdown(report), encoding="utf-8")

        if status == "refused":
            raise PermissionError(
                f"Backfill span {span_days}d exceeds the bound ({max_span_days}d) and "
                "was not human-confirmed. Pass --confirmed-by \"<real name>\" to approve "
                "a wider span, or narrow --event-time-start/--event-time-end. "
                f"Report written to {_rel(current_json, self.repo_root)}."
            )
        if status == "refused_no_remote_approval":
            raise PermissionError(
                f"{remote_gate.blocking_reason} "
                f"Report written to {_rel(current_json, self.repo_root)}."
            )
        if status == "failed":
            raise RuntimeError(
                f"dbt build failed (exit {returncode}); see "
                f"{_rel(current_json, self.repo_root)} for stderr."
            )
        return BackfillResult(
            _rel(current_json, self.repo_root),
            _rel(current_md, self.repo_root),
            status,
        )


def _render_markdown(report: dict[str, Any]) -> str:
    lines = [
        "# dbt Backfill",
        "",
        f"Window: `{report['event_time_start']}` .. `{report['event_time_end']}` "
        f"({report['span_days']} days)",
        f"Bound: {report['max_span_days']} days -- "
        + ("OVER BOUND" if report["over_bound"] else "within bound"),
        f"Confirmed by: {report['confirmed_by'] or '(none -- agent-asserted)'} "
        f"(source: `{report['source']}`)",
        f"Remote execution approved: {'yes' if report['remote_approval_ok'] else 'NO -- ' + report['remote_approval_blocking_reason']}",
        f"Selection: `{report['select'] or '(all models)'}`"
        + (f" deferred to `{report['state_path']}`" if report["defer"] else ""),
        f"Status: `{report['status']}`"
        + (" -- WOULD REFUSE without human confirmation" if report["would_refuse"] and report["dry_run"] else ""),
        "",
        "## Command",
        "",
        "```",
        " ".join(report["command"]),
        "```",
        "",
    ]
    if report["status"] in ("executed", "failed"):
        lines += ["## dbt output (tail)", "", "```", report.get("stdout_tail", ""), "```", ""]
        if report.get("stderr_tail"):
            lines += ["## stderr (tail)", "", "```", report["stderr_tail"], "```", ""]
    return "\n".join(lines)


def _parse_date(value: str, flag: str) -> date:
    try:
        return date.fromisoformat(value)
    except ValueError as exc:
        raise ValueError(f"{flag} must be YYYY-MM-DD, got {value!r}") from exc


def _rel(path: Path, root: Path) -> str:
    try:
        return path.resolve().relative_to(root.resolve()).as_posix()
    except ValueError:
        return path.as_posix()


@anchored("run-dbt-backfill")
def main(argv: list[str] | None = None) -> int:
    from core.onboarding.workspace.cli_runner import run_workspace_command

    parser = argparse.ArgumentParser()
    parser.add_argument("--workspace", required=True)
    parser.add_argument("--repo-root", default=str(PROJECT_ROOT))
    parser.add_argument("--event-time-start", required=True)
    parser.add_argument("--event-time-end", required=True)
    parser.add_argument("--select", default="")
    parser.add_argument(
        "--defer", action="store_true",
        help=f"build only state:modified+ against {DBT_STATE_PATH_ENV} production artifacts",
    )
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--confirmed-by", default="")
    parser.add_argument("--max-span-days", type=int, default=DEFAULT_MAX_SPAN_DAYS)
    args = parser.parse_args(argv)
    return run_workspace_command(
        command="run-dbt-backfill",
        workspace=args.workspace,
        repo_root=args.repo_root,
        fn=lambda: DbtBackfillRunner(args.repo_root, args.workspace).run(
            event_time_start=args.event_time_start,
            event_time_end=args.event_time_end,
            dry_run=args.dry_run,
            confirmed_by=args.confirmed_by,
            max_span_days=args.max_span_days,
            select=args.select,
            defer=args.defer,
        ),
        metadata={
            "event_time_start": args.event_time_start,
            "event_time_end": args.event_time_end,
            "dry_run": args.dry_run,
            "defer": args.defer,
            "confirmed_by": args.confirmed_by,
        },
    )


if __name__ == "__main__":
    raise SystemExit(main())
