"""Offline verification of the orchestration graph.

Airflow/Dagster DAGs here are STATIC modules an operator points a scheduler at
(`airflow_dag.py`, `dagster_defs.py`), both rendered from the single stage graph
in `pipeline_stages.py`. There is no per-workspace DAG codegen, so there is no
generated DAG directory to parse -- which means `astro dev parse` has nothing
workspace-specific to check and is not the useful gate here.

What DOES break in production: every stage shells out to a governed CLI
(`uv run onboard-workspace`, `uv run generate-dbt-project`, ...). Rename or
retire one of those commands and nothing fails until a scheduled task errors at
2am, because the command name lives in a string. This module closes that gap the
same way `tests/test_instruction_drift.py` closes the AGENTS.md one: the
reference is checked against `pyproject.toml`'s registered scripts.

It also checks the topology a scheduler would otherwise reject at parse time:
duplicate task ids, dependencies on stages that do not exist, and cycles.

Deliberately offline -- no Airflow, no Dagster, no scheduler, no credentials.
Airflow is intentionally absent from this repo's venv (installing it downgrades
shared dependencies), so a check that required it would never run here.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from core.dev.instruction_drift import registered_commands
from core.observability.cost_ledger import anchored
from core.orchestration.pipeline_stages import STAGES
from core.paths import PROJECT_ROOT

# `uv run <command>` as emitted into task command strings. `python` is the
# interpreter passthrough (`uv run python -m ...`), not a project script.
_UV_RUN = re.compile(r"uv run ([a-z][a-z0-9-]*)")
_INTERPRETER_PASSTHROUGH = frozenset({"python"})

_ORCHESTRATION_MODULES = (
    "pipeline_stages.py",
    "airflow_dag.py",
    "cosmos_dag.py",
    "dagster_defs.py",
)


@dataclass(frozen=True)
class DagVerifyResult:
    """Offline verdict on the orchestration graph."""

    ok: bool
    status: str  # "verified" | "failed"
    stage_count: int
    command_count: int
    unknown_commands: list[str] = field(default_factory=list)
    deprecated_commands: list[str] = field(default_factory=list)
    topology_errors: list[str] = field(default_factory=list)

    def summary(self) -> dict[str, Any]:
        return self.__dict__.copy()


def emitted_commands(repo_root: Path | None = None) -> dict[str, list[str]]:
    """Every `uv run <command>` an orchestration module emits -> the modules using it."""
    root = Path(repo_root or PROJECT_ROOT) / "core" / "orchestration"
    found: dict[str, list[str]] = {}
    for name in _ORCHESTRATION_MODULES:
        path = root / name
        if not path.exists():
            continue
        for command in _UV_RUN.findall(path.read_text(encoding="utf-8")):
            if command in _INTERPRETER_PASSTHROUGH:
                continue
            found.setdefault(command, []).append(name)
    return found


def topology_errors(stages: Any = None) -> list[str]:
    """Duplicate ids, dangling dependencies and cycles in the stage graph."""
    graph = list(stages if stages is not None else STAGES)
    errors: list[str] = []

    ids = [getattr(s, "key", "") for s in graph]
    duplicates = sorted({i for i in ids if ids.count(i) > 1})
    errors += [f"duplicate stage id: {d}" for d in duplicates]

    known = set(ids)
    deps: dict[str, list[str]] = {}
    for stage, sid in zip(graph, ids):
        upstream = list(getattr(stage, "upstream", ()) or ())
        deps[sid] = upstream
        errors += [
            f"stage `{sid}` depends on `{u}`, which is not a stage" for u in upstream if u not in known
        ]

    # Iterative DFS: a scheduler rejects a cyclic graph at parse time, so catch
    # it here rather than letting the DAG fail to load in production.
    WHITE, GREY, BLACK = 0, 1, 2
    colour = dict.fromkeys(deps, WHITE)
    for start in deps:
        if colour[start] != WHITE:
            continue
        stack: list[tuple[str, bool]] = [(start, False)]
        while stack:
            node, leaving = stack.pop()
            if leaving:
                colour[node] = BLACK
                continue
            if colour.get(node) == GREY:
                errors.append(f"dependency cycle reaches `{node}`")
                continue
            if colour.get(node) == BLACK:
                continue
            colour[node] = GREY
            stack.append((node, True))
            for nxt in deps.get(node, []):
                if colour.get(nxt, BLACK) != BLACK:
                    stack.append((nxt, False))
    return errors


def verify_dags(repo_root: Path | None = None) -> DagVerifyResult:
    """Check the orchestration graph offline. No scheduler, no credentials."""
    root = Path(repo_root or PROJECT_ROOT)
    registered = registered_commands(root / "pyproject.toml")
    emitted = emitted_commands(root)

    unknown = sorted(
        f"{cmd} (in {', '.join(sorted(set(mods)))})"
        for cmd, mods in emitted.items()
        if cmd not in registered
    )
    # A deprecated command still resolves, so it cannot fail the check -- but a
    # scheduled task pinned to one is a slow-moving break worth naming.
    deprecated = sorted(
        cmd for cmd in emitted if cmd in {"resolve-kpi-features", "blocker-question-panel",
                                          "derived-feature-markdown", "prepare-solution-blueprint"}
    )
    topo = topology_errors()

    ok = not unknown and not topo
    return DagVerifyResult(
        ok=ok,
        status="verified" if ok else "failed",
        stage_count=len(list(STAGES)),
        command_count=len(emitted),
        unknown_commands=unknown,
        deprecated_commands=deprecated,
        topology_errors=topo,
    )


@anchored("verify-orchestration")
def main(argv: list[str] | None = None) -> int:
    import argparse

    parser = argparse.ArgumentParser(
        description="Verify the orchestration graph offline: emitted CLI commands "
        "resolve to registered scripts, and the stage topology is loadable."
    )
    parser.add_argument("--repo-root", default=str(PROJECT_ROOT))
    args = parser.parse_args(argv)

    result = verify_dags(Path(args.repo_root))
    marker = "[ok]" if result.ok else "[x]"
    print(
        f"{marker} orchestration: {result.stage_count} stages, "
        f"{result.command_count} emitted commands"
    )
    for label, items in (
        ("unresolved command", result.unknown_commands),
        ("topology", result.topology_errors),
    ):
        for item in items:
            print(f"  [x] {label}: {item}")
    for item in result.deprecated_commands:
        print(f"  [~] deprecated command still scheduled: {item}")
    return 0 if result.ok else 1


__all__ = [
    "DagVerifyResult",
    "emitted_commands",
    "main",
    "topology_errors",
    "verify_dags",
]


if __name__ == "__main__":
    raise SystemExit(main())
