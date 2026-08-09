"""Publish dbt run state (`manifest.json` + `run_results.json`) to a UC volume.

Neither slim CI (`dbt build --state ... --select state:modified+`) nor
`dbt retry`/`dbt clone` have anywhere to read a prior build's state FROM
today -- a repo-wide grep for `publish_dbt_state|dbt_state` returned 0
matches before this module. This is that publish step, run after
`publish_gold` (state should reflect a build that was actually promoted to
live gold, not a WAP-staged one a failed test might still roll back).

CLI, not SDK, deliberately (same reasoning as `core.provisioning.sync_code`):
`databricks fs cp` needs no extra dependency and is what the CLI-first
handoff plan already standardized on for moving files into a UC volume.

Two `databricks fs cp` calls per artifact -- one to a UTC-timestamped folder
(history, for `dbt clone`-style point-in-time reads) and one to `latest/`
(what CI's slim-CI download and `dbt retry` want). Injectable `runner`
(default `subprocess.run`) so tests never touch the network, same shape as
`core.provisioning.sync_code.sync_workspace_code`. Never echoes profile or
host values -- stderr is redacted before it reaches the returned dict.
"""
from __future__ import annotations

import argparse
import json
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from core.observability.cost_ledger import anchored
from core.observability.log_redaction import redact
from core.paths import PROJECT_ROOT

#: `target/` files worth publishing -- exactly what `dbt --state`, `dbt
#: retry`, and `dbt clone` read; nothing else in `target/` is state.
STATE_ARTIFACTS: tuple[str, ...] = ("manifest.json", "run_results.json")

#: Enough of a `databricks fs cp` error to diagnose it; short enough that a
#: verbose auth dump cannot ride along (same bound as sync_code.py).
STDERR_TAIL_CHARS = 600


def _read_catalog(project_dir: Path) -> str:
    """dbt_project.yml's `vars.catalog` -- the same read
    `cosmos_dag._project_layer_schema_tables` does, kept local here (rather
    than imported) so this module -- parsed by the Airflow scheduler at
    DAG-load time via `cosmos_dag.build_publish_dbt_state_task` -- does not
    pull in the whole KPI SQL-generation stack for one field read.
    """
    import yaml

    project_yml = Path(project_dir) / "dbt_project.yml"
    if not project_yml.exists():
        return ""
    data = yaml.safe_load(project_yml.read_text(encoding="utf-8")) or {}
    return str((data.get("vars") or {}).get("catalog") or "")


def state_remote_root(project_dir: str | Path, workspace: str) -> str:
    """`/Volumes/<catalog>/_state/dbt/<workspace-basename>` -- the parent
    both `publish_state` and `state_download_command` write/read under.
    `catalog` falls back to `main` when the project has no declared one
    (offline/local-only workspace never generated a real dbt project)."""
    catalog = _read_catalog(Path(project_dir)) or "main"
    ws_name = Path(str(workspace)).name
    return f"/Volumes/{catalog}/_state/dbt/{ws_name}"


def publish_state(
    project_dir: str | Path,
    workspace: str,
    *,
    runner: Callable[..., Any] = subprocess.run,
) -> dict[str, Any]:
    """Ship `target/manifest.json` + `target/run_results.json` to a UC
    volume, both timestamped AND `latest/`. No `target/` (or an empty one,
    e.g. before the first-ever `dbt build`) makes no `databricks` call at
    all. `runner` is injected so tests never touch the network.
    """
    project_dir = Path(project_dir)
    target_dir = project_dir / "target"
    present = [n for n in STATE_ARTIFACTS if (target_dir / n).is_file()]
    if not present:
        return {"ok": False, "reason": "no target/ artifacts"}

    remote_root = state_remote_root(project_dir, workspace)
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    published: list[dict[str, Any]] = []
    for name in present:
        local = str(target_dir / name)
        for tag in (timestamp, "latest"):
            remote_path = f"{remote_root}/{tag}/{name}"
            argv = ["databricks", "fs", "cp", "--overwrite", local, remote_path]
            entry: dict[str, Any] = {
                "artifact": name, "tag": tag, "remote_path": remote_path,
            }
            try:
                proc = runner(argv, capture_output=True, text=True)
            except Exception as exc:  # CLI missing / not on PATH
                entry |= {
                    "status": "failed",
                    "detail": f"{type(exc).__name__}: {redact(str(exc))}",
                }
                published.append(entry)
                return {
                    "ok": False, "reason": "databricks fs cp failed",
                    "published": published, "remote_root": remote_root,
                    "timestamp": timestamp,
                }
            code = int(getattr(proc, "returncode", 1) or 0)
            entry["exit_code"] = code
            if code != 0:
                # stderr TAIL only -- `databricks` can echo the resolved
                # host/profile on failure, and redact() is a second layer,
                # not the only one.
                tail = str(getattr(proc, "stderr", "") or "")[-STDERR_TAIL_CHARS:]
                entry |= {"status": "failed", "stderr_tail": redact(tail)}
                published.append(entry)
                # A failed push is almost always auth or path, not this
                # artifact -- the next call would fail identically.
                return {
                    "ok": False, "reason": "databricks fs cp failed",
                    "published": published, "remote_root": remote_root,
                    "timestamp": timestamp,
                }
            entry["status"] = "published"
            published.append(entry)

    return {
        "ok": True,
        "published": published,
        "remote_root": remote_root,
        "timestamp": timestamp,
        "artifacts": present,
    }


def state_download_command(
    workspace: str, *, repo_root: str | Path = PROJECT_ROOT,
) -> list[str]:
    """The `databricks fs cp` argv CI (or a human running slim CI locally)
    uses to pull the last published state down before
    `dbt build --state target --select state:modified+` -- the other half
    of what `publish_state` uploads."""
    project_dir = Path(repo_root) / workspace / "dbt"
    remote_root = state_remote_root(project_dir, workspace)
    return ["databricks", "fs", "cp", "--recursive", f"{remote_root}/latest", "target"]


@anchored("publish-dbt-state")
def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Publish target/manifest.json + target/run_results.json to a UC volume."
    )
    parser.add_argument("--workspace", required=True)
    parser.add_argument("--repo-root", default=str(PROJECT_ROOT))
    args = parser.parse_args(argv)

    project_dir = Path(args.repo_root) / args.workspace / "dbt"
    result = publish_state(project_dir, args.workspace)
    print(json.dumps(result, indent=2))
    return 0 if result.get("ok") else 1


if __name__ == "__main__":
    raise SystemExit(main())


__all__ = [
    "STATE_ARTIFACTS",
    "publish_state",
    "state_download_command",
    "state_remote_root",
    "main",
]
