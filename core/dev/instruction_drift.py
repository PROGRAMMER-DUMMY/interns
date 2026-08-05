"""Detect drift between the CLI registry and the instruction files agents read.

An agentic CLI only exists, from an agent's point of view, if the instruction files
say it does. `AGENTS.md` > "Stage index" is the designated tool-discovery path; a
command registered in `[project.scripts]` but absent from it is invisible, and an
agent that cannot find the command improvises -- which is exactly how a raw-SDK
workaround shipped instead of the governed command that already existed.

Pure functions only: parse the registry, check containment in a markdown file,
report what is missing. The tests in `tests/test_instruction_drift.py` turn that
into a failing gate so staleness stops depending on a human noticing.

Mention rule: a command counts as documented only when it appears inside a code
span (inline backticks or a fenced block). Prose alone does not count -- it would
let English words that happen to be command names (`loop`, `doctor`, `harness`)
satisfy the guard by accident.
"""
from __future__ import annotations

import re
import tomllib

from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]

# Commands that are repo/CI plumbing for people working ON this platform, not steps
# an operator or agent runs against a workspace. Each entry needs a reason below;
# a test enforces that, and another enforces that the command still exists.
EXEMPT_REASONS: dict[str, str] = {
    "build-tool-index": (
        "repo maintenance: regenerates .agents/tools.json from the registry; "
        "coverage is already gated by tests/regressions/test_tool_index_coverage.py"
    ),
    "green-gate": "CI/dev test gate for repo contributors; runs no workspace stage",
    "resolver-accuracy": (
        "dev benchmark that scores the resolver against a checked-in corpus; "
        "produces no workspace artifact"
    ),
    "cost-ledger-ingest": (
        "dev telemetry: ingests the local CLI session transcript into the cost ledger; "
        "invoked by tooling, not by a workspace workflow"
    ),
    "token-report": (
        "dev token-cost tracing of instruction files and run outputs; "
        "measurement of the repo itself, not a workspace step"
    ),
}

EXEMPT_COMMANDS: frozenset[str] = frozenset(EXEMPT_REASONS)

_CODE_SPAN = re.compile(r"`([^`\n]+)`")
_FENCED = re.compile(r"```[^\n]*\n(.*?)```", re.S)
_TOKEN = re.compile(r"[A-Za-z0-9_.-]+")


def registered_commands(pyproject_path: Path | str = REPO_ROOT / "pyproject.toml") -> set[str]:
    """Every console script name in ``[project.scripts]``."""
    data = tomllib.loads(Path(pyproject_path).read_text(encoding="utf-8"))
    return set((data.get("project") or {}).get("scripts") or {})


def commands_mentioned(text: str, commands: set[str]) -> set[str]:
    """The subset of ``commands`` named inside a code span of ``text``.

    Containment check, not a parser: it never invents command names, so a doc can
    only satisfy it by naming the real thing.
    """
    blob = " ".join(_CODE_SPAN.findall(text)) + " " + " ".join(_FENCED.findall(text))
    return commands & set(_TOKEN.findall(blob))


def missing_from(
    doc_path: Path | str,
    commands: set[str],
    exempt: frozenset[str] | set[str] = EXEMPT_COMMANDS,
) -> list[str]:
    """Commands absent from ``doc_path``, minus exemptions, sorted."""
    text = Path(doc_path).read_text(encoding="utf-8")
    return sorted(set(commands) - exempt - commands_mentioned(text, set(commands)))


def documented_sections(doc_path: Path | str, commands: set[str]) -> set[str]:
    """Command names claimed by a ``### <command>`` heading in ``doc_path``.

    A heading claims a command when its first token is either already registered or
    shaped like a CLI name (lowercase, hyphenated). That skips the module-path,
    filename, and prose headings TOOLS.md also uses, while still catching a section
    left behind for a command that was deleted from the registry.
    """
    text = Path(doc_path).read_text(encoding="utf-8")
    heads = (h.split()[0] for h in re.findall(r"^### (.+)$", text, re.M) if h.split())
    return {
        h for h in heads
        if h in commands or re.fullmatch(r"[a-z][a-z0-9]*(-[a-z0-9]+)+", h)
    }
