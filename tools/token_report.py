"""Token-cost reporter for development tracing.

Measures the token cost of the two things that actually drive per-session spend:

1. **CLI fixed context** — the instruction/registry files each CLI *pins into
   context on every turn*. Discovered generically from each CLI's own config
   (e.g. Gemini's ``.gemini/settings.json`` ``context.fileName`` array, Claude's
   auto-loaded ``CLAUDE.md``). This is the ~56k->17k base we cut by un-pinning
   ``TOOLS.md`` / ``.agents/tools.json``.
2. **Workspace run outputs** (optional) — the result packet / dated run files /
   open blocker panel an agent must read+forward per run.

It complements ``tools/context_status.py`` (which estimates *workspace-state*
context in bytes for an active session): this tool reports **tokens**, covers the
**CLI fixed context** that ``context_status`` does not, and supports a
**before/after** snapshot + compare so a change's token impact is measured, not
eyeballed.

Generic by construction: no CLI is hardcoded into the logic — providers read each
CLI's actual config file and are skipped when that config is absent. No workspace
or domain vocabulary is baked in.

Token counting uses ``tiktoken`` (``cl100k_base``) when installed; otherwise a
deterministic ``utf8-bytes / 4`` heuristic. The method is recorded in every
snapshot so before/after comparisons stay apples-to-apples.

Usage::

    # Current fixed CLI context (all discovered CLIs):
    uv run token-report

    # Add a workspace's run outputs, and save a labelled baseline:
    uv run token-report --workspace workspaces/<project> --label before --save before.json

    # After a change, compare against the saved baseline:
    uv run token-report --workspace workspaces/<project> --label after --baseline before.json
"""
from __future__ import annotations

import argparse
import json
import math
from collections.abc import Callable
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from core.paths import PROJECT_ROOT
from core.storage.workspace_layout import WorkspaceLayout


# --------------------------------------------------------------------------- #
# Token counting (tiktoken if available, else a deterministic heuristic).      #
# --------------------------------------------------------------------------- #
def resolve_token_counter() -> tuple[str, Callable[[str], int]]:
    """Return (method_label, counter). Prefers tiktoken; falls back to bytes/4."""
    try:
        import tiktoken  # type: ignore

        enc = tiktoken.get_encoding("cl100k_base")
        return "tiktoken:cl100k_base", lambda s: len(enc.encode(s))
    except Exception:
        # ceil(utf-8 bytes / 4) — a stable, well-known rough estimate.
        return "heuristic:utf8-bytes/4", lambda s: math.ceil(len(s.encode("utf-8")) / 4)


# --------------------------------------------------------------------------- #
# Data model                                                                  #
# --------------------------------------------------------------------------- #
@dataclass
class FileTokens:
    path: str
    exists: bool
    bytes: int
    tokens: int
    note: str = ""

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class Section:
    name: str
    items: list[FileTokens] = field(default_factory=list)

    @property
    def total_bytes(self) -> int:
        return sum(i.bytes for i in self.items)

    @property
    def total_tokens(self) -> int:
        return sum(i.tokens for i in self.items)

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "total_bytes": self.total_bytes,
            "total_tokens": self.total_tokens,
            "items": [i.to_dict() for i in self.items],
        }


@dataclass
class Snapshot:
    label: str
    generated_at: str
    token_method: str
    repo_root: str
    workspace: str
    sections: list[Section] = field(default_factory=list)

    @property
    def grand_total_tokens(self) -> int:
        return sum(s.total_tokens for s in self.sections)

    @property
    def grand_total_bytes(self) -> int:
        return sum(s.total_bytes for s in self.sections)

    def to_dict(self) -> dict[str, Any]:
        return {
            "artifact_type": "token_report.snapshot",
            "label": self.label,
            "generated_at": self.generated_at,
            "token_method": self.token_method,
            "repo_root": self.repo_root,
            "workspace": self.workspace,
            "grand_total_bytes": self.grand_total_bytes,
            "grand_total_tokens": self.grand_total_tokens,
            "sections": [s.to_dict() for s in self.sections],
        }


# --------------------------------------------------------------------------- #
# Measurement helpers                                                         #
# --------------------------------------------------------------------------- #
def _rel(path: Path, root: Path) -> str:
    try:
        return path.resolve().relative_to(root.resolve()).as_posix()
    except ValueError:
        return path.as_posix()


def measure(path: Path, root: Path, counter: Callable[[str], int], *, note: str = "") -> FileTokens:
    rel = _rel(path, root)
    if not path.exists() or not path.is_file():
        return FileTokens(path=rel, exists=False, bytes=0, tokens=0, note=note or "missing")
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return FileTokens(path=rel, exists=False, bytes=0, tokens=0, note="unreadable")
    return FileTokens(
        path=rel,
        exists=True,
        bytes=len(text.encode("utf-8")),
        tokens=counter(text),
        note=note,
    )


# --------------------------------------------------------------------------- #
# CLI fixed-context providers (generic: read each CLI's OWN config).          #
# Each returns (list_of_pinned_files, note) or None when the CLI's config is  #
# absent. Adding a CLI is adding a config reader here — never domain logic.    #
# --------------------------------------------------------------------------- #
def _gemini_pinned(repo_root: Path) -> tuple[list[Path], str] | None:
    cfg = repo_root / ".gemini" / "settings.json"
    if not cfg.exists():
        return None
    try:
        data = json.loads(cfg.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    context = data.get("context") or {}
    names = context.get("fileName") or []
    note = "includeDirectoryTree=on (adds untracked tree cost)" if context.get("includeDirectoryTree") else ""
    return [repo_root / n for n in names], note


def _claude_pinned(repo_root: Path) -> tuple[list[Path], str] | None:
    f = repo_root / "CLAUDE.md"
    if not f.exists():
        return None
    return [f], "auto-loaded project memory"


_CLI_PROVIDERS: dict[str, Callable[[Path], "tuple[list[Path], str] | None"]] = {
    "gemini": _gemini_pinned,
    "claude": _claude_pinned,
}


def cli_context_sections(repo_root: Path, counter: Callable[[str], int]) -> list[Section]:
    sections: list[Section] = []
    for cli_name, provider in sorted(_CLI_PROVIDERS.items()):
        result = provider(repo_root)
        if result is None:
            continue
        files, note = result
        section = Section(name=f"cli_context:{cli_name}")
        for path in files:
            section.items.append(measure(path, repo_root, counter))
        if note and section.items:
            section.items[0].note = (section.items[0].note + f"; {note}").lstrip("; ")
        sections.append(section)
    return sections


# --------------------------------------------------------------------------- #
# Workspace run-output section (optional).                                    #
# --------------------------------------------------------------------------- #
def workspace_output_section(repo_root: Path, workspace_rel: str, counter: Callable[[str], int]) -> Section:
    layout = WorkspaceLayout(project_root=(repo_root / workspace_rel).resolve())
    section = Section(name=f"workspace_outputs:{Path(workspace_rel).name}")
    candidates = [
        (layout.reports_dir / "kpi_results" / "current.md", "result packet (forwarded verbatim)"),
        (layout.reports_dir / "blocker_question_panel" / "current.md", "open blocker panel"),
    ]
    # Latest dated run results.md, if any.
    runs_base = layout.runs_dir
    if runs_base.is_dir():
        dated = sorted(
            (p for p in runs_base.iterdir() if p.is_dir() and (p / "results.md").exists()),
            key=lambda p: p.name,
        )
        if dated:
            candidates.append((dated[-1] / "results.md", "latest dated run results"))
    for path, note in candidates:
        section.items.append(measure(path, repo_root, counter, note=note))
    return section


# --------------------------------------------------------------------------- #
# Snapshot build + render + compare                                           #
# --------------------------------------------------------------------------- #
def build_snapshot(repo_root: Path, *, workspace: str = "", label: str = "") -> Snapshot:
    method, counter = resolve_token_counter()
    sections = cli_context_sections(repo_root, counter)
    if workspace:
        sections.append(workspace_output_section(repo_root, workspace, counter))
    return Snapshot(
        label=label or "current",
        generated_at=datetime.now(timezone.utc).isoformat(),
        token_method=method,
        repo_root=str(repo_root),
        workspace=workspace,
        sections=sections,
    )


def _fmt(n: int) -> str:
    return f"{n:,}"


def render_text(snapshot: Snapshot) -> str:
    lines: list[str] = []
    lines.append(f"# Token Report — {snapshot.label}")
    lines.append(f"- method: {snapshot.token_method}")
    lines.append(f"- generated_at: {snapshot.generated_at}")
    if snapshot.workspace:
        lines.append(f"- workspace: {snapshot.workspace}")
    lines.append("")
    for section in snapshot.sections:
        lines.append(f"## {section.name} — {_fmt(section.total_tokens)} tok ({_fmt(section.total_bytes)} bytes)")
        for item in section.items:
            marker = "[ok]" if item.exists else "[x]"
            extra = f"  ({item.note})" if item.note else ""
            lines.append(f"  {marker} {_fmt(item.tokens):>10} tok  {item.path}{extra}")
        lines.append("")
    lines.append(f"GRAND TOTAL: {_fmt(snapshot.grand_total_tokens)} tok ({_fmt(snapshot.grand_total_bytes)} bytes)")
    return "\n".join(lines)


def render_compare(baseline: dict[str, Any], current: Snapshot) -> str:
    """Before/after table keyed by section name (plus grand total)."""
    before_sections = {s["name"]: s["total_tokens"] for s in baseline.get("sections", [])}
    after_sections = {s.name: s.total_tokens for s in current.sections}
    names = sorted(set(before_sections) | set(after_sections))

    lines: list[str] = []
    lines.append("# Token Report — before/after")
    lines.append(f"- before: `{baseline.get('label', '?')}` ({baseline.get('token_method', '?')})")
    lines.append(f"- after:  `{current.label}` ({current.token_method})")
    if baseline.get("token_method") != current.token_method:
        lines.append("  [~] token methods differ — deltas are approximate.")
    lines.append("")
    header = f"{'section':<38}{'before':>12}{'after':>12}{'delta':>12}"
    lines.append(header)
    lines.append("-" * len(header))
    for name in names:
        b = before_sections.get(name, 0)
        a = after_sections.get(name, 0)
        d = a - b
        sign = "+" if d > 0 else ""
        lines.append(f"{name:<38}{_fmt(b):>12}{_fmt(a):>12}{(sign + _fmt(d)):>12}")
    b_total = baseline.get("grand_total_tokens", sum(before_sections.values()))
    a_total = current.grand_total_tokens
    d_total = a_total - b_total
    sign = "+" if d_total > 0 else ""
    lines.append("-" * len(header))
    lines.append(f"{'GRAND TOTAL':<38}{_fmt(b_total):>12}{_fmt(a_total):>12}{(sign + _fmt(d_total)):>12}")
    if b_total:
        pct = round(d_total / b_total * 100, 1)
        lines.append("")
        lines.append(f"net change: {sign}{_fmt(d_total)} tok ({sign}{pct}%)")
    return "\n".join(lines)


def main() -> None:
    parser = argparse.ArgumentParser(description="Report per-turn token cost (CLI fixed context + workspace run outputs).")
    parser.add_argument("--repo-root", default=str(PROJECT_ROOT), help="Repo root (default: detected PROJECT_ROOT).")
    parser.add_argument("--workspace", default="", help="Workspace path (e.g. workspaces/<project>) to add run-output tokens.")
    parser.add_argument("--label", default="", help="Label for this snapshot (e.g. before/after).")
    parser.add_argument("--save", default="", help="Write the snapshot JSON to this path.")
    parser.add_argument("--baseline", default="", help="Compare current snapshot against this saved baseline JSON.")
    parser.add_argument("--json", action="store_true", help="Print snapshot JSON instead of the text table.")
    args = parser.parse_args()

    repo_root = Path(args.repo_root).resolve()
    snapshot = build_snapshot(repo_root, workspace=args.workspace, label=args.label)

    if args.save:
        save_path = Path(args.save)
        save_path.parent.mkdir(parents=True, exist_ok=True)
        save_path.write_text(json.dumps(snapshot.to_dict(), indent=2) + "\n", encoding="utf-8")

    if args.baseline:
        baseline = json.loads(Path(args.baseline).read_text(encoding="utf-8"))
        print(render_compare(baseline, snapshot))
        return

    if args.json:
        print(json.dumps(snapshot.to_dict(), indent=2))
    else:
        print(render_text(snapshot))


if __name__ == "__main__":
    main()
