"""The no-API-key AI bridge.

The in-app chat panel and ``minus edit`` both call :func:`run_agent`, which
shells out to your configured CLI agent (default ``claude -p``). The agent runs
in the project directory with whatever auth it already has, edits the YAML
config, and the app hot-reloads. No LLM key lives here.
"""

from __future__ import annotations

import difflib
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path

from minus.config.loader import ConfigError, load_dashboards, load_project
from minus.config.models import Project


@dataclass
class AgentResult:
    ok: bool
    command: list[str]
    stdout: str = ""
    stderr: str = ""
    message: str = ""


# ---------------------------------------------------------------------------
# Prompt construction
# ---------------------------------------------------------------------------


def field_catalog(project: Project, model=None) -> str:
    """A compact catalog of tables, columns, measures the agent can reference."""
    lines = ["TABLES (use 'table.column' in field refs):"]
    for t in project.tables:
        cols = ""
        if model is not None:
            try:
                cols = " - columns: " + ", ".join(model.columns(t.name))
            except Exception:
                cols = ""
        pk = f" [pk={t.primary_key}]" if t.primary_key else ""
        lines.append(f"  - {t.name}{pk}{cols}")
    if project.measures:
        lines.append("MEASURES (reference by name in widgets):")
        for m in project.measures:
            kind = m.kind or m.agg
            lines.append(f"  - {m.name} ({kind}) {m.field or ''}".rstrip())
    if project.relationships:
        lines.append("RELATIONSHIPS:")
        for r in project.relationships:
            lines.append(f"  - {r.from_table}.{r.from_column} -> {r.to_table}.{r.to_column}")
    return "\n".join(lines)


def build_prompt(root: Path | str, project: Project, request: str,
                 page_id: str | None = None, model=None) -> str:
    root = Path(root)
    schema_rel = "minus/config/schema.json"
    pages_dir = project.dashboards_dir
    target = f"{pages_dir}/{page_id}.yaml" if page_id else f"a file in {pages_dir}/"
    return f"""You are editing a MinusAnalyst dashboard project (a config-driven Dash app).

USER REQUEST:
{request}

HOW TO APPLY IT:
- Edit the YAML config ONLY. Do not write Python. The app renders from YAML.
- Dashboard pages live in `{pages_dir}/*.yaml` (one page per file). The user is
  currently viewing `{target}`.
- Global datasources/tables/relationships/measures live in `project.yaml`. Add a
  new measure there if the request needs one, then reference it by name.
- The exact config schema (JSON Schema) is at `{schema_rel}`. Read it if unsure.
- After editing, the running app reloads automatically. Keep YAML valid.

{field_catalog(project, model)}

Make the smallest change that satisfies the request, then stop."""


# ---------------------------------------------------------------------------
# Running the agent
# ---------------------------------------------------------------------------


def run_agent(project: Project, root: Path | str, prompt: str,
              dry_run: bool = False) -> AgentResult:
    root = Path(root)
    argv = [a.replace("{prompt}", prompt) for a in project.agent.command]
    cwd = project.agent.cwd or str(root)

    if dry_run:
        shown = [a if a != prompt else "<prompt>" for a in argv]
        return AgentResult(ok=True, command=shown,
                           message="Dry run — command not executed:\n" + " ".join(shown))

    exe = project.agent.command[0]
    if shutil.which(exe) is None:
        return AgentResult(
            ok=False, command=argv,
            message=f"CLI agent {exe!r} not found on PATH. Install it (e.g. Claude "
                    f"Code) or set agent.command in project.yaml.")
    try:
        proc = subprocess.run(argv, cwd=cwd, capture_output=True, text=True,
                              timeout=project.agent.timeout)
    except subprocess.TimeoutExpired:
        return AgentResult(ok=False, command=argv,
                           message=f"Agent timed out after {project.agent.timeout}s.")
    ok = proc.returncode == 0
    msg = (proc.stdout or "").strip() or ("Done." if ok else "")
    if not ok and proc.stderr:
        msg = (msg + "\n" + proc.stderr.strip()).strip()
    return AgentResult(ok=ok, command=argv, stdout=proc.stdout, stderr=proc.stderr,
                       message=msg or ("Done." if ok else "Agent failed."))


# ---------------------------------------------------------------------------
# Safe apply: snapshot -> run -> validate -> rollback
# ---------------------------------------------------------------------------


def _config_paths(project: Project, root: Path) -> list[Path]:
    """All config files that make up the project: project.yaml + every page."""
    paths: list[Path] = []
    proj_yaml = root / "project.yaml"
    if proj_yaml.exists():
        paths.append(proj_yaml)
    pages_dir = root / project.dashboards_dir
    if pages_dir.exists():
        paths.extend(sorted(pages_dir.glob("*.y*ml")))
    return paths


def snapshot_config(project: Project, root: Path | str) -> dict[str, bytes]:
    """Read the raw bytes of every config file into a ``path -> bytes`` map.

    Paths are stored as POSIX-style strings relative to ``root`` so a snapshot
    is portable and easy to diff/restore.
    """
    root = Path(root)
    snap: dict[str, bytes] = {}
    for p in _config_paths(project, root):
        rel = p.relative_to(root).as_posix()
        snap[rel] = p.read_bytes()
    return snap


def _restore_snapshot(snapshot: dict[str, bytes], root: Path) -> list[str]:
    """Restore snapshotted bytes to disk; remove files created since snapshot.

    Returns the list of relative paths that were restored or deleted.
    """
    root = Path(root)
    touched: list[str] = []

    # Restore / recreate each snapshotted file.
    for rel, data in snapshot.items():
        dest = root / rel
        if not dest.exists() or dest.read_bytes() != data:
            dest.parent.mkdir(parents=True, exist_ok=True)
            dest.write_bytes(data)
            touched.append(rel)

    return touched


def config_diff(before: dict[str, bytes], after: dict[str, bytes]) -> str:
    """A readable unified diff of config files that changed between snapshots.

    Covers modified, added (only in ``after``) and removed (only in ``before``)
    files. Returns an empty string when the two snapshots are identical.
    """
    chunks: list[str] = []
    for rel in sorted(set(before) | set(after)):
        b = before.get(rel, b"")
        a = after.get(rel, b"")
        if b == a:
            continue
        b_lines = b.decode("utf-8", "replace").splitlines(keepends=True)
        a_lines = a.decode("utf-8", "replace").splitlines(keepends=True)
        diff = difflib.unified_diff(
            b_lines, a_lines,
            fromfile=f"a/{rel}", tofile=f"b/{rel}",
        )
        text = "".join(diff)
        if text and not text.endswith("\n"):
            text += "\n"
        chunks.append(text)
    return "".join(chunks)


def changed_files(before: dict[str, bytes], after: dict[str, bytes]) -> list[str]:
    """Relative paths whose bytes differ (modified, added, or removed)."""
    out: list[str] = []
    for rel in sorted(set(before) | set(after)):
        if before.get(rel) != after.get(rel):
            out.append(rel)
    return out


def validate_and_rollback(snapshot: dict[str, bytes], project: Project,
                          root: Path | str) -> str | None:
    """Re-validate the on-disk config; roll back to ``snapshot`` if invalid.

    Returns ``None`` when the current config loads cleanly. Otherwise restores
    the snapshotted bytes and returns the validation error message. Exposed
    separately so callers (and tests) can exercise the rollback logic without
    invoking an external CLI.
    """
    root = Path(root)
    try:
        reloaded = load_project(root)
        load_dashboards(root, reloaded)
    except ConfigError as exc:
        _restore_snapshot(snapshot, root)
        return str(exc)
    return None


def safe_run_agent(project: Project, root: Path | str, prompt: str,
                   dry_run: bool = False) -> AgentResult:
    """Run the agent with a snapshot/validate/rollback guard.

    1. Snapshot every config file's bytes.
    2. Run the agent (which edits the YAML on disk).
    3. Re-validate via ``load_project`` + ``load_dashboards``. If the agent
       produced invalid config, restore the snapshot and return a not-ok result
       whose message contains the validation error (so the caller knows it was
       reverted).
    4. Otherwise return ok with a summary of which files changed.

    ``dry_run`` short-circuits to :func:`run_agent`'s dry-run behavior — nothing
    is executed, snapshotted, or validated.
    """
    root = Path(root)
    if dry_run:
        return run_agent(project, root, prompt, dry_run=True)

    before = snapshot_config(project, root)
    result = run_agent(project, root, prompt, dry_run=False)

    if not result.ok:
        # Agent itself failed; nothing reliable to validate. Restore in case it
        # left partial edits behind.
        restored = _restore_snapshot(before, root)
        if restored:
            result.message = (result.message + "\n\nReverted partial edits to: "
                              + ", ".join(restored)).strip()
        return result

    error = validate_and_rollback(before, project, root)
    if error is not None:
        return AgentResult(
            ok=False, command=result.command,
            stdout=result.stdout, stderr=result.stderr,
            message="Agent produced invalid config — changes reverted.\n\n" + error,
        )

    after = snapshot_config(project, root)
    changed = changed_files(before, after)
    if changed:
        summary = "Applied. Changed files:\n  - " + "\n  - ".join(changed)
    else:
        summary = "Agent ran but made no config changes."
    base = result.message.strip()
    result.message = (base + "\n\n" + summary).strip() if base else summary
    return result
