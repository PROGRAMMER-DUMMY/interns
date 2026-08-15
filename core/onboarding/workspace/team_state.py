"""Mirror a workspace's DECISION state to a Unity Catalog volume.

The problem this closes: `workspaces/**/interns/` is gitignored, so when engineer A
answers ten blocker questions those answers exist only on A's laptop. Engineer B on
another machine gets none of them and re-answers the same questions -- and may answer
them differently, which is worse than re-doing the work.

Only two artifacts are decision state (everything else under `interns/` is derived and
regenerates from them):

* `interns/generated/contracts/workspace_feature_definitions.json` -- the reusable
  workspace-level business definitions accepted during blocker grilling.
* `interns/state/applied_ops.jsonl` -- the idempotency ledger of which `apply-*` /
  `finalize-*` decisions were actually recorded.

Shape deliberately mirrors :mod:`core.orchestration.dbt_state`: the same
`databricks fs cp --overwrite` argv, the same injected `runner` so tests never touch the
network, the same `redact()` over failure text so an auth dump cannot ride along in an
error string, and the same never-raise/structured-dict contract.

**This never blocks the local flow.** A workspace with no `databricks_source` makes no
subprocess call at all (one JSON read, then return), and an unreachable volume is
reported, not raised -- the local decision is already durably on disk, and losing the
mirror must never lose the answer.
"""

from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Any, Callable

from core.observability.log_redaction import redact
from core.storage.workspace_layout import WorkspaceLayout

#: (relative path under `interns/`, artifact label). Order is stable so a failure
#: report reads the same way every run.
TEAM_STATE_ARTIFACTS: tuple[tuple[str, str], ...] = (
    ("generated/contracts/workspace_feature_definitions.json", "feature_definitions"),
    ("state/applied_ops.jsonl", "applied_ops"),
)

#: Same bound as dbt_state/sync_code: enough to diagnose, too short to carry a dump.
STDERR_TAIL_CHARS = 600


def team_state_remote_root(catalog: str, schema: str, project: str) -> str:
    """`/Volumes/<catalog>/<schema>/_state/dbt/<project>`."""
    return f"/Volumes/{catalog}/{schema}/_state/dbt/{project}"


def _source_block(layout: WorkspaceLayout) -> dict[str, Any]:
    source = layout.load_settings().get("databricks_source")
    return source if isinstance(source, dict) else {}


def mirror_team_state(
    workspace: str | Path,
    *,
    runner: Callable[..., Any] = subprocess.run,
) -> dict[str, Any]:
    """Mirror decision state to the workspace's UC volume, best effort.

    Returns a structured result; never raises. ``skipped=True`` means no remote call
    was attempted (the overwhelmingly common local-only case), which is a normal
    outcome, not a failure.
    """
    workspace_path = Path(workspace).resolve()
    layout = WorkspaceLayout(project_root=workspace_path)

    source = _source_block(layout)
    catalog = str(source.get("catalog") or "").strip()
    schema = str(source.get("schema") or "").strip()
    if not catalog or not schema:
        return {
            "ok": True,
            "skipped": True,
            "reason": "workspace has no databricks_source catalog/schema; local-only",
            "mirrored": [],
        }

    present = [
        (workspace_path / "interns" / rel, label)
        for rel, label in TEAM_STATE_ARTIFACTS
        if (workspace_path / "interns" / rel).is_file()
    ]
    if not present:
        return {
            "ok": True,
            "skipped": True,
            "reason": "no decision-state artifacts on disk yet",
            "mirrored": [],
        }

    remote_root = team_state_remote_root(catalog, schema, workspace_path.name)
    mirrored: list[dict[str, Any]] = []
    for local_path, label in present:
        remote_path = f"{remote_root}/{local_path.name}"
        entry: dict[str, Any] = {
            "artifact": label,
            "remote_path": remote_path,
        }
        argv = ["databricks", "fs", "cp", "--overwrite", str(local_path), remote_path]
        try:
            proc = runner(argv, capture_output=True, text=True)
        except Exception as exc:  # CLI missing / not on PATH
            entry |= {"status": "failed", "detail": f"{type(exc).__name__}: {redact(str(exc))}"}
            mirrored.append(entry)
            return {
                "ok": False,
                "skipped": False,
                "reason": "databricks fs cp failed",
                "remote_root": remote_root,
                "mirrored": mirrored,
            }
        if getattr(proc, "returncode", 1) != 0:
            detail = redact(str(getattr(proc, "stderr", "") or ""))[-STDERR_TAIL_CHARS:]
            entry |= {"status": "failed", "detail": detail}
            mirrored.append(entry)
            return {
                "ok": False,
                "skipped": False,
                "reason": "databricks fs cp returned non-zero",
                "remote_root": remote_root,
                "mirrored": mirrored,
            }
        entry["status"] = "mirrored"
        mirrored.append(entry)

    return {
        "ok": True,
        "skipped": False,
        "remote_root": remote_root,
        "mirrored": mirrored,
    }


def mirror_team_state_safe(
    workspace: str | Path,
    *,
    runner: Callable[..., Any] = subprocess.run,
) -> dict[str, Any]:
    """:func:`mirror_team_state` with a blanket guard, for call sites inside the
    governed CLI envelope. A mirror failure must never turn a successfully recorded
    local decision into a failed command."""
    try:
        return mirror_team_state(workspace, runner=runner)
    except Exception as exc:  # pragma: no cover - defensive
        return {
            "ok": False,
            "skipped": False,
            "reason": f"{type(exc).__name__}: {redact(str(exc))}",
            "mirrored": [],
        }
