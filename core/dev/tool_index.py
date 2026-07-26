"""Generate `.agents/tools.json` from the real CLI registry.

The index an agent consults must be DERIVED, never curated. A hand-maintained
list drifts silently: as of 2026-07-26 the checked-in index covered 56 of 115
registered CLIs and omitted `run-kpi-pipeline` -- the one deterministic chain
AGENTS.md tells agents to prefer -- so agents fell back to grepping source. A
first-time "can I use my S3 data" question cost 20 tool calls for an answer the
index should have carried.

Source of truth is `[project.scripts]` in pyproject.toml; the one-line summary
comes from each entry point's own docstring, so the index cannot describe a
command that no longer behaves that way.

Regenerate:  uv run build-tool-index
Verify:      uv run build-tool-index --check
"""
from __future__ import annotations

import argparse
import importlib
import json

from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[2]
PYPROJECT = REPO_ROOT / "pyproject.toml"
INDEX_PATH = REPO_ROOT / ".agents" / "tools.json"

def registered_clis() -> dict[str, str]:
    """Every `[project.scripts]` entry: CLI name -> "module:function".

    Delegates to the cost ledger's tomllib-backed parser rather than re-parsing
    pyproject here -- two readers of the same registry would be exactly the kind
    of divergent duplication this module exists to remove.
    """
    from core.observability.cost_ledger import entry_point_scripts

    return entry_point_scripts()


def _entry_source(target: str) -> str:
    """Source text of the entry point's module, or "" when unreadable."""
    module_name = target.partition(":")[0]
    try:
        module = importlib.import_module(module_name)
        source_file = getattr(module, "__file__", None)
        if not source_file:
            return ""
        return Path(source_file).read_text(encoding="utf-8", errors="ignore")
    except Exception:
        # A CLI whose module needs an optional dependency must not break index
        # generation -- it still belongs in the index, just without a summary.
        return ""


def _summary_for(target: str) -> str:
    """First non-empty docstring line of the entry point, else its module."""
    module_name, _, func_name = target.partition(":")
    try:
        module = importlib.import_module(module_name)
    except Exception:
        return ""
    func = getattr(module, func_name, None)
    doc = (getattr(func, "__doc__", None) or getattr(module, "__doc__", None) or "")
    for line in doc.strip().splitlines():
        if line.strip():
            return line.strip()
    return ""


# Human-authored fields on the previous curated index. Coverage is generated;
# JUDGEMENT is not derivable from source, so it is preserved rather than
# overwritten. Add these by hand to the checked-in index and they survive
# regeneration.
_CURATED_FIELDS = ("use_when", "outputs", "safety", "required_skills", "quiet")


def _curated_overlay() -> dict[str, dict[str, Any]]:
    """Human-authored fields from the checked-in index, keyed by CLI name."""
    if not INDEX_PATH.exists():
        return {}
    try:
        existing = json.loads(INDEX_PATH.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return {}
    overlay: dict[str, dict[str, Any]] = {}
    for entry in existing.get("tools", []) or []:
        name = entry.get("name")
        if not name:
            continue
        kept = {k: entry[k] for k in _CURATED_FIELDS if entry.get(k)}
        if kept:
            overlay[name] = kept
    return overlay


def build_index() -> dict[str, Any]:
    """The full index document written to `.agents/tools.json`.

    Coverage is derived from the registry so it cannot drift; human-authored
    `use_when`/`outputs`/`safety` notes are carried forward.
    """
    overlay = _curated_overlay()
    tools: list[dict[str, Any]] = []
    for name, target in sorted(registered_clis().items()):
        source = _entry_source(target)
        command = f"uv run {name}"
        if '"--workspace"' in source:
            command += " --workspace <workspace>"
        entry: dict[str, Any] = {
            "name": name,
            "command": command,
            "entry_point": target,
            "summary": _summary_for(target),
        }
        if '"--quiet"' in source:
            entry["quiet"] = "Accepts --quiet to suppress full panel output."
        entry.update(overlay.get(name, {}))
        tools.append(entry)
    return {
        "version": 2,
        "source": "generated from pyproject.toml [project.scripts] by core.dev.tool_index",
        "evidence_policy": (
            "Derived, not curated. Do not hand-edit. Regenerate: uv run build-tool-index"
        ),
        "tools": tools,
    }


def _rendered() -> str:
    return json.dumps(build_index(), indent=2, ensure_ascii=True) + "\n"


def main(argv: list[str] | None = None) -> int:
    """Regenerate .agents/tools.json from the CLI registry."""
    parser = argparse.ArgumentParser(
        prog="build-tool-index",
        description="Regenerate .agents/tools.json from the CLI registry.",
    )
    parser.add_argument(
        "--check", action="store_true",
        help="Exit non-zero if the checked-in index is stale; write nothing.",
    )
    args = parser.parse_args(argv)

    rendered = _rendered()
    count = len(build_index()["tools"])

    if args.check:
        current = INDEX_PATH.read_text(encoding="utf-8") if INDEX_PATH.exists() else ""
        if current != rendered:
            print("[x] .agents/tools.json is stale; run: uv run build-tool-index")
            return 1
        print(f"[ok] tool index is current ({count} CLIs)")
        return 0

    INDEX_PATH.parent.mkdir(parents=True, exist_ok=True)
    INDEX_PATH.write_text(rendered, encoding="utf-8")
    print(f"[ok] wrote {count} CLIs to {INDEX_PATH.relative_to(REPO_ROOT)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
