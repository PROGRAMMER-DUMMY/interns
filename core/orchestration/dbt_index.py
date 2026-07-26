"""Local dbt model lineage and blast radius -- no warehouse, no dbt install.

The audited failure that motivates this: an agent deleted a safety check with
no way to see what depended on it. `dbt ls`/`dbt docs` answer that question but
need a working connection and a compiled manifest, so in practice nobody asked.

This reads the generated project's `.sql` files directly. `{{ ref('x') }}` and
`{{ source('a','b') }}` are the whole graph -- dbt derives its own DAG from
exactly these -- so parsing them gives the same edges for free, offline, before
anything is built.

Reports three things the generator itself cannot tell you:
  - blast radius: every model downstream of one you are about to change,
  - unresolved refs: a `ref()` to a model that does not exist (dbt fails on
    this at parse time, after you have already pushed),
  - cycles: a model reachable from itself.
"""
from __future__ import annotations
from core.observability.cost_ledger import anchored

import argparse
import json
import re
from collections import deque
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from core.contracts.versioning import register_contract
from core.paths import PROJECT_ROOT
from core.storage.workspace_layout import WorkspaceLayout

REPORT_VERSION = 1
register_contract("dbt_index/current.json", current_version=REPORT_VERSION)

_REF = re.compile(r"\{\{\s*ref\(\s*['\"]([^'\"]+)['\"]\s*\)\s*\}\}")
_SOURCE = re.compile(
    r"\{\{\s*source\(\s*['\"]([^'\"]+)['\"]\s*,\s*['\"]([^'\"]+)['\"]\s*\)\s*\}\}"
)


@dataclass(frozen=True)
class IndexResult:
    current_json_path: str
    current_markdown_path: str
    model_count: int
    unresolved_ref_count: int
    cycle_count: int
    ok: bool = True

    def summary(self) -> dict[str, Any]:
        return self.__dict__.copy()


class DbtIndex:
    """The `ref`/`source` graph of a workspace's generated dbt project."""

    def __init__(self, dbt_dir: str | Path) -> None:
        self.dbt_dir = Path(dbt_dir)
        self.parents: dict[str, list[str]] = {}
        self.sources: dict[str, list[str]] = {}
        for path in sorted((self.dbt_dir / "models").rglob("*.sql")):
            sql = path.read_text(encoding="utf-8")
            self.parents[path.stem] = sorted(set(_REF.findall(sql)))
            self.sources[path.stem] = sorted(
                {f"{a}.{b}" for a, b in _SOURCE.findall(sql)}
            )
        self.children: dict[str, list[str]] = {m: [] for m in self.parents}
        for model, refs in self.parents.items():
            for ref in refs:
                if ref in self.children:
                    self.children[ref].append(model)
        for model in self.children:
            self.children[model].sort()

    def downstream(self, model: str) -> list[str]:
        """Every model reachable from `model`, excluding itself unless cyclic."""
        seen: set[str] = set()
        queue = deque(self.children.get(model, []))
        while queue:
            node = queue.popleft()
            if node in seen:
                continue
            seen.add(node)
            queue.extend(self.children.get(node, []))
        return sorted(seen)

    def unresolved_refs(self) -> list[dict[str, str]]:
        return [
            {"model": model, "ref": ref}
            for model, refs in sorted(self.parents.items())
            for ref in refs
            if ref not in self.parents
        ]

    def cycles(self) -> list[str]:
        return sorted(m for m in self.parents if m in self.downstream(m))

    def report(self) -> dict[str, Any]:
        models = []
        for model in sorted(self.parents):
            downstream = self.downstream(model)
            models.append({
                "model": model,
                "parents": self.parents[model],
                "sources": self.sources[model],
                "children": self.children[model],
                "downstream": downstream,
                "blast_radius": len(downstream),
            })
        return {
            "artifact_type": "dbt_index/current.json",
            "version": REPORT_VERSION,
            "generated_by": "dbt-index",
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "model_count": len(models),
            "unresolved_refs": self.unresolved_refs(),
            "cycles": self.cycles(),
            "models": models,
        }


def build_index(repo_root: str | Path, workspace: str | Path, model: str = "") -> IndexResult:
    repo_root = Path(repo_root).resolve()
    layout = WorkspaceLayout(project_root=(repo_root / workspace).resolve())
    dbt_dir = layout.project_root / "dbt"
    if not (dbt_dir / "dbt_project.yml").exists():
        raise FileNotFoundError(
            f"{dbt_dir}/dbt_project.yml not found -- run "
            "`generate-dbt-project --workspace <ws>` first."
        )
    index = DbtIndex(dbt_dir)
    if model and model not in index.parents:
        raise ValueError(
            f"no model named {model!r} in {dbt_dir}/models. "
            f"Known: {', '.join(sorted(index.parents)) or '(none)'}"
        )
    report = index.report()
    report["focus_model"] = model
    panel_dir = layout.reports_dir / "dbt_index"
    panel_dir.mkdir(parents=True, exist_ok=True)
    current_json = panel_dir / "current.json"
    current_md = panel_dir / "current.md"
    current_json.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    current_md.write_text(_render_markdown(report), encoding="utf-8")
    # An unresolved ref or a cycle is a project dbt refuses to parse -- a
    # generation defect, not a lookup result. Raised AFTER the report is
    # written, so the detail is on disk either way. Returning ok=False and
    # exiting 0 would make this exactly the kind of gate-written-as-prose the
    # audit found every instance of failing.
    if report["unresolved_refs"] or report["cycles"]:
        raise RuntimeError(
            f"{len(report['unresolved_refs'])} unresolved ref(s) and "
            f"{len(report['cycles'])} cycle(s) in {_rel(dbt_dir, repo_root)}; dbt "
            f"cannot parse this project. See {_rel(current_md, repo_root)}."
        )
    return IndexResult(
        _rel(current_json, repo_root),
        _rel(current_md, repo_root),
        model_count=report["model_count"],
        unresolved_ref_count=len(report["unresolved_refs"]),
        cycle_count=len(report["cycles"]),
    )


def _render_markdown(report: dict[str, Any]) -> str:
    focus = report.get("focus_model") or ""
    lines = ["# dbt Index", "", f"Models: {report['model_count']}", ""]
    if report["unresolved_refs"]:
        lines += ["## [x] Unresolved refs", ""]
        lines += [
            f"- `{u['model']}` refs `{u['ref']}`, which does not exist"
            for u in report["unresolved_refs"]
        ]
        lines.append("")
    if report["cycles"]:
        lines += ["## [x] Cycles", ""]
        lines += [f"- `{m}` is reachable from itself" for m in report["cycles"]]
        lines.append("")
    entries = [m for m in report["models"] if not focus or m["model"] == focus]
    if focus:
        lines += [f"## Blast radius: `{focus}`", ""]
    else:
        lines += ["## Blast radius (models broken by a change to each)", ""]
    lines += ["| Model | Depends on | Breaks if changed |", "|---|---|---|"]
    for entry in sorted(entries, key=lambda m: (-m["blast_radius"], m["model"])):
        depends = ", ".join(f"`{p}`" for p in entry["parents"] + entry["sources"]) or "-"
        breaks = ", ".join(f"`{d}`" for d in entry["downstream"]) or "-"
        lines.append(f"| `{entry['model']}` | {depends} | {entry['blast_radius']} -- {breaks} |")
    lines.append("")
    return "\n".join(lines)


def _rel(path: Path, root: Path) -> str:
    try:
        return path.resolve().relative_to(root.resolve()).as_posix()
    except ValueError:
        return path.as_posix()


@anchored("dbt-index")
def main(argv: list[str] | None = None) -> int:
    from core.onboarding.workspace.cli_runner import run_workspace_command

    parser = argparse.ArgumentParser()
    parser.add_argument("--workspace", required=True)
    parser.add_argument("--repo-root", default=str(PROJECT_ROOT))
    parser.add_argument("--model", default="", help="report the blast radius of one model")
    args = parser.parse_args(argv)
    return run_workspace_command(
        command="dbt-index",
        workspace=args.workspace,
        repo_root=args.repo_root,
        fn=lambda: build_index(args.repo_root, args.workspace, args.model),
        metadata={"model": args.model},
    )


if __name__ == "__main__":
    raise SystemExit(main())
