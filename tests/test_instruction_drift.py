"""Drift guard: instruction-file staleness must fail a test, not wait for a human.

47 of 130 registered CLIs were missing from `AGENTS.md` when this guard was written.
The Stage index is the designated tool-discovery path, so those commands did not
exist as far as any agent was concerned -- one improvised raw SDK calls because it
never learned the governed command was there. Nothing failed; a person had to notice.

EXPECTED STATE: these tests FAIL until the parallel doc-fix lands in
AGENTS.md / TOOLS.md. That is the guard working, not a broken test. The fix is to
document the listed commands (or exempt one with a reason in
`core.dev.instruction_drift.EXEMPT_REASONS`) -- never to loosen an assertion here.

Run: .venv\\Scripts\\python.exe -m unittest tests.test_instruction_drift
"""
from __future__ import annotations

import json
import unittest

from core.dev.instruction_drift import (
    EXEMPT_COMMANDS,
    EXEMPT_REASONS,
    REPO_ROOT,
    documented_sections,
    missing_from,
    registered_commands,
)

AGENTS_MD = REPO_ROOT / "AGENTS.md"
TOOLS_MD = REPO_ROOT / "TOOLS.md"
TOOL_INDEX = REPO_ROOT / ".agents" / "tools.json"

REMEDY = (
    "add it to the Stage index row for its stage in AGENTS.md, or exempt it in "
    "core.dev.instruction_drift.EXEMPT_COMMANDS with a reason"
)

# Commands that gate a human decision. If one of these has no TOOLS.md section, an
# agent cannot learn its flags or its answer contract without reading source.
GATED_PREFIXES = ("apply-", "prepare-", "finalize-")


class InstructionDriftTests(unittest.TestCase):
    def setUp(self):
        self.commands = registered_commands()

    def test_every_registered_command_is_documented_in_agents_md(self):
        missing = missing_from(AGENTS_MD, self.commands)
        self.assertEqual(
            missing, [],
            f"{len(missing)} of {len(self.commands)} registered CLIs are invisible to "
            f"agents -- absent from AGENTS.md, the designated tool-discovery path.\n"
            f"Missing: {missing}\n"
            f"For each one: {REMEDY}.",
        )

    def test_every_exemption_has_a_reason(self):
        unreasoned = sorted(c for c in EXEMPT_COMMANDS if not EXEMPT_REASONS.get(c, "").strip())
        self.assertEqual(
            unreasoned, [],
            f"exempted with no reason: {unreasoned}. An exemption without a written "
            f"reason is an undocumented command with extra steps; add one to "
            f"core.dev.instruction_drift.EXEMPT_REASONS.",
        )

    def test_exemptions_are_still_registered(self):
        stale = sorted(EXEMPT_COMMANDS - self.commands)
        self.assertEqual(
            stale, [],
            f"exemptions for commands that no longer exist in [project.scripts]: {stale}. "
            f"A rotting exemption list silently exempts the next command that reuses the "
            f"name; delete these from core.dev.instruction_drift.EXEMPT_REASONS.",
        )

    def test_tools_md_documents_the_governed_cli_surface(self):
        sections = documented_sections(TOOLS_MD, self.commands)

        orphaned = sorted(sections - self.commands)
        self.assertEqual(
            orphaned, [],
            f"TOOLS.md has a '### <command>' section for commands that are not "
            f"registered in pyproject.toml [project.scripts]: {orphaned}. Delete the "
            f"section, or restore the command -- docs for a deleted command send agents "
            f"to a CLI that does not exist.",
        )

        gated = {c for c in self.commands if c.startswith(GATED_PREFIXES)}
        undocumented = sorted(gated - sections - EXEMPT_COMMANDS)
        self.assertEqual(
            undocumented, [],
            f"{len(undocumented)} of {len(gated)} human-gate CLIs (apply-/prepare-/"
            f"finalize-) have no '### <command>' section in TOOLS.md: {undocumented}. "
            f"Without one an agent cannot learn the flags or the answer contract and "
            f"will invent them. Add the section, or {REMEDY}.",
        )

    def test_generated_tool_index_is_in_sync(self):
        self.assertTrue(TOOL_INDEX.exists(), f"{TOOL_INDEX} is missing; run: uv run build-tool-index")
        indexed = {e["name"] for e in json.loads(TOOL_INDEX.read_text(encoding="utf-8"))["tools"]}
        self.assertEqual(
            indexed ^ self.commands, set(),
            f"the checked-in .agents/tools.json and pyproject.toml [project.scripts] "
            f"disagree: {sorted(indexed ^ self.commands)}. Regenerate with: "
            f"uv run build-tool-index",
        )


if __name__ == "__main__":
    unittest.main()
