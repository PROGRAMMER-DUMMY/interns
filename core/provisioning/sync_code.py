"""Ship generated pipeline code to the Databricks workspace with `databricks sync`.

`generate-ingestion` writes runnable jobs to ``workspaces/<ws>/ingestion/`` and
the dbt generator writes ``workspaces/<ws>/dbt/``; nothing shipped either, so a
live replay stalled between codegen and execution. This module closes that gap.

CLI, not SDK, deliberately (`docs/plans/2026-08-06-cli-first-handoff.md` S1):
`databricks sync` is incremental, stateful and **preserves file extensions**.
``databricks workspace import-dir`` is never used -- it strips ``.py``/``.sql``
and would rename ``models/foo.sql`` to ``models/foo``, corrupting a dbt project
(`docs/reference/databricks_cli_reference.md` S5).

Refusals mirror [[core.provisioning.apply]], in the same order and shape:

1. **No confirmed blueprint -> refuse.** ``interns/reports/solution_blueprint/
   current.confirmed.json`` is the ONE human confirmation; without it nothing
   is pushed and the reason names ``confirm-blueprint``.
2. **Dry run -> executes nothing.** Not even ``databricks sync --dry-run``: a
   dry run must not need credentials.
3. **Kill-switch -> refuse.** ``AUTORESEARCH_ALLOW_REMOTE_EXECUTION=0`` is the
   human override that stops a confirmed workspace anyway.
4. **Non-zero exit -> structured failure** carrying the stderr TAIL only --
   never the environment, never a host or token.
"""
from __future__ import annotations

import json
import os
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from core.observability.log_redaction import redact
from core.onboarding.databricks.deploy_gates import REMOTE_ENV
from core.onboarding.panel_contract import attach_stage_routing
from core.provisioning.apply import confirmed_blueprint_path
from core.provisioning.plan import ROUTING_STAGE
from core.storage.workspace_layout import WorkspaceLayout

SYNC_LOG_VERSION = 1

#: Generated code trees, in push order. A workspace may have either or both.
# `context` is the curated, PHI-safe artifact set staged by stage_context().
CODE_DIRS = ("ingestion", "dbt", "context")
#: Build output and vendored deps: regenerated remotely, never worth pushing.
EXCLUDES = ("target/**", "dbt_packages/**", "logs/**", ".venv/**", "__pycache__/**")
DEFAULT_REMOTE_PARENT = "/Workspace/Shared"

STATUS_SYNCED = "synced"
STATUS_DRY_RUN = "dry_run"
STATUS_NOTHING_TO_SYNC = "nothing_to_sync"
STATUS_REFUSED_NO_CONFIRMATION = "refused_no_confirmation"
STATUS_REFUSED_KILL_SWITCH = "refused_remote_execution_kill_switch"
STATUS_FAILED = "failed"

CONFIRM_COMMAND = 'uv run confirm-blueprint --workspace <ws> --confirmed-by "<name>"'
READINESS_COMMAND = "uv run check-platform-readiness"
#: Enough of a `databricks sync` error to diagnose it; short enough that a
#: verbose auth dump cannot ride along.
STDERR_TAIL_CHARS = 600


# Artifacts worth publishing INTO the workspace so a teammate -- or a
# Databricks-native agent (Assistant/Genie) pointed at this folder -- has the
# context without cloning the repo: what the pipeline is, what the KPIs mean,
# what was decided and why.
#
# CURATED, not a directory dump. `interns/` also holds profile evidence with
# SAMPLED DATA VALUES, which on a regulated workspace can carry PHI/PII;
# publishing that wholesale would move person-derived samples into a broadly
# readable location. Anything sample-bearing stays out, and what does go is
# re-checked by the PHI gate before upload.
PUBLISHABLE_ARTIFACTS: tuple[str, ...] = (
    "interns/reports/solution_blueprint/current.md",
    "interns/reports/intake_playback/current.md",
    "interns/reports/onboarding_report.md",
    "interns/generated/contracts/kpi_registry.json",
    "interns/generated/contracts/domain_model.json",
    "interns/generated/contracts/semantic_contract.json",
    "interns/generated/contracts/provision_plan.json",
)

# Never publish, even if a future path glob would reach them: sampled values,
# machine audit trails, and anything holding credentials or session state.
NEVER_PUBLISH = ("profiles/", "evidence/", "state/", "trajectory", "session", "events.jsonl")


def collect_publishable(workspace_dir: Path) -> tuple[list[Path], list[dict[str, str]]]:
    """(files to publish, skipped-with-reason). Refuses sample-bearing paths."""
    keep: list[Path] = []
    skipped: list[dict[str, str]] = []
    for rel in PUBLISHABLE_ARTIFACTS:
        if any(token in rel for token in NEVER_PUBLISH):
            skipped.append({"path": rel, "reason": "may carry sampled values or session state"})
            continue
        path = workspace_dir / rel
        if path.is_file():
            keep.append(path)
        else:
            skipped.append({"path": rel, "reason": "not generated yet"})
    return keep, skipped


def default_remote_root(workspace: str | Path) -> str:
    return f"{DEFAULT_REMOTE_PARENT}/{Path(str(workspace)).name}"


def stage_context(workspace_dir: Path) -> tuple[int, list[dict[str, str]]]:
    """Copy the curated artifacts into `<workspace>/context/` for publication.

    A staged copy rather than syncing `interns/` directly: `interns/` is the
    machine audit trail (sampled values, session state, event logs) and must not
    be published wholesale. Staging makes the published set an explicit,
    reviewable list instead of whatever happens to be on disk.
    """
    keep, skipped = collect_publishable(workspace_dir)
    out = workspace_dir / "context"
    if keep:
        out.mkdir(parents=True, exist_ok=True)
        (out / "README.md").write_text(
            "\n".join(
                [
                    "# Workspace context",
                    "",
                    "Published by `sync-workspace-code` so a teammate -- or a",
                    "Databricks-native agent pointed at this folder -- has the pipeline's",
                    "meaning without cloning the repo: what it builds, what the KPIs are,",
                    "and what was decided and why.",
                    "",
                    "Generated. Edit the workspace and re-sync; do not edit here.",
                    "",
                    "Deliberately EXCLUDED: data profiles and evidence. Those carry",
                    "sampled values, which on a regulated workspace can contain personal",
                    "data, and this folder is broadly readable.",
                    "",
                ]
            ),
            encoding="utf-8",
        )
    for src in keep:
        (out / src.name).write_bytes(src.read_bytes())
    return len(keep), skipped


def build_sync_commands(
    workspace_root: Path, remote_root: str, *, full: bool = True
) -> list[list[str]]:
    """One ``databricks sync`` argv per EXISTING generated code directory."""
    remote = str(remote_root).rstrip("/")
    commands: list[list[str]] = []
    for name in CODE_DIRS:
        local = Path(workspace_root) / name
        if not local.is_dir():
            continue
        argv = ["databricks", "sync", str(local), f"{remote}/{name}"]
        if full:
            argv.append("--full")
        for pattern in EXCLUDES:
            argv += ["--exclude", pattern]
        commands.append(argv)
    return commands


def sync_workspace_code(
    repo_root: str | Path,
    workspace: str | Path,
    *,
    dry_run: bool | None = None,
    remote_root: str | None = None,
    runner: Callable[..., Any] = subprocess.run,
) -> dict[str, Any]:
    """Push ``ingestion/`` and ``dbt/`` to ``remote_root`` on the workspace.

    ``runner`` is injected so tests never touch the network.
    """
    repo_root = Path(repo_root).resolve()
    layout = WorkspaceLayout(project_root=(repo_root / workspace).resolve())
    remote = (remote_root or default_remote_root(layout.project_root)).rstrip("/")

    confirmed = confirmed_blueprint_path(layout).exists()
    # --dry-run defaults OFF only when the confirmed blueprint is present.
    effective_dry_run = (not confirmed) if dry_run is None else bool(dry_run)

    # Stage the curated context BEFORE deciding what is present, so a workspace
    # that has generated artifacts but no code still publishes its meaning.
    context_count, context_skipped = stage_context(layout.project_root)

    present = [n for n in CODE_DIRS if (layout.project_root / n).is_dir()]
    absent = [
        {"dir": n, "status": "skipped", "reason": "not generated"}
        for n in CODE_DIRS
        if n not in present
    ]
    commands = build_sync_commands(layout.project_root, remote)

    def _finish(
        status: str,
        ok: bool,
        synced: list[dict[str, Any]],
        skipped: list[dict[str, Any]],
        reason: str = "",
        next_command: str = "",
    ) -> dict[str, Any]:
        result: dict[str, Any] = {
            "ok": ok,
            "status": status,
            "dry_run": effective_dry_run,
            "confirmed_blueprint": confirmed,
            "remote_root": remote,
            "synced": synced,
            "skipped": skipped,
            "commands": commands,
        }
        if reason:
            result["reason"] = reason
        if next_command:
            result["next_command"] = next_command
        result["sync_log_path"] = _write_log(layout, repo_root, result)
        return attach_stage_routing(result, ROUTING_STAGE)

    def _refuse(status: str, reason: str, next_command: str = "") -> dict[str, Any]:
        blocked = [{"dir": n, "status": "skipped", "reason": status} for n in present]
        return _finish(status, False, [], absent + blocked, reason, next_command)

    if not confirmed:
        return _refuse(
            STATUS_REFUSED_NO_CONFIRMATION,
            "the solution blueprint has not been confirmed; shipping code to the "
            "Databricks workspace needs the one recorded human confirmation "
            f"({_rel(confirmed_blueprint_path(layout), repo_root)}). Nothing was "
            "pushed.",
            CONFIRM_COMMAND,
        )

    if effective_dry_run:
        would = [
            {"dir": n, "remote_path": f"{remote}/{n}", "status": "would_sync"}
            for n in present
        ]
        return _finish(STATUS_DRY_RUN, True, would, absent,
                       "dry run; nothing was pushed.")

    if os.environ.get(REMOTE_ENV, "") == "0":
        return _refuse(
            STATUS_REFUSED_KILL_SWITCH,
            f"{REMOTE_ENV}=0 is set in this shell; remote execution is disabled by "
            "a human override. Nothing was pushed.",
        )

    if not present:
        return _finish(
            STATUS_NOTHING_TO_SYNC, True, [], absent,
            "no generated code to ship; run `generate-ingestion` or "
            "`generate-dbt-project` first.",
        )

    synced: list[dict[str, Any]] = []
    failed = False
    for name, argv in zip(present, commands):
        entry: dict[str, Any] = {"dir": name, "remote_path": f"{remote}/{name}"}
        try:
            proc = runner(argv, capture_output=True, text=True)
        except Exception as exc:  # CLI missing / not on PATH
            entry |= {
                "status": "failed",
                "exit_code": None,
                "detail": f"{type(exc).__name__}: {redact(str(exc))}",
            }
            synced.append(entry)
            failed = True
            break
        code = int(getattr(proc, "returncode", 1) or 0)
        entry["exit_code"] = code
        if code != 0:
            # stderr TAIL only: `databricks` can echo the resolved host/profile
            # on failure, and the head of that output is exactly where it lands.
            tail = str(getattr(proc, "stderr", "") or "")[-STDERR_TAIL_CHARS:]
            entry |= {"status": "failed", "stderr_tail": redact(tail)}
            synced.append(entry)
            failed = True
            # A sync failure is almost always auth or path, not this directory;
            # the second push would fail identically.
            break
        entry["status"] = STATUS_SYNCED
        synced.append(entry)

    if failed:
        return _finish(
            STATUS_FAILED, False, synced, absent,
            "`databricks sync` exited non-zero; see stderr_tail.",
            READINESS_COMMAND,
        )
    return _finish(STATUS_SYNCED, True, synced, absent)


def _write_log(layout: WorkspaceLayout, repo_root: Path, result: dict[str, Any]) -> str:
    out_dir = layout.evidence_dir / "provisioning"
    out_dir.mkdir(parents=True, exist_ok=True)
    path = out_dir / "sync_log.json"
    payload = {
        "artifact_type": "evidence/provisioning/sync_log.json",
        "version": SYNC_LOG_VERSION,
        "generated_by": "sync-workspace-code",
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "workspace": layout.project_root.name,
        "tool": "databricks sync",
        "remote_root": result["remote_root"],
        "status": result["status"],
        "ok": result["ok"],
        "dry_run": result["dry_run"],
        "confirmed_blueprint": result["confirmed_blueprint"],
        "reason": result.get("reason", ""),
        "next_command": result.get("next_command", ""),
        "directories": list(result["synced"]) + list(result["skipped"]),
    }
    path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    return _rel(path, repo_root)


def _rel(path: Path, root: Path) -> str:
    try:
        return path.resolve().relative_to(root.resolve()).as_posix()
    except ValueError:
        return path.as_posix()


__all__ = [
    "CODE_DIRS",
    "STATUS_DRY_RUN",
    "STATUS_FAILED",
    "STATUS_NOTHING_TO_SYNC",
    "STATUS_REFUSED_KILL_SWITCH",
    "STATUS_REFUSED_NO_CONFIRMATION",
    "STATUS_SYNCED",
    "build_sync_commands",
    "default_remote_root",
    "sync_workspace_code",
]
