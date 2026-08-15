"""Contract test for the selection-turn write guard (`core/governance/selection_guard.py`).

Guards AGENTS.md > Step 0 (HARD STOP: no file mutation during a `set <workspace>` turn
until the user confirms). Covers the decision logic directly plus the stdin/exit-code
contract every harness invokes it through, and the cross-harness field-name and
tool-name variations that keep it agent-agnostic rather than Claude-Code-specific.
"""

from __future__ import annotations

import json
import subprocess
import sys
import unittest
import uuid
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
GUARD = REPO_ROOT / "core" / "governance" / "selection_guard.py"

sys.path.insert(0, str(REPO_ROOT))
from core.governance import selection_guard as sg  # noqa: E402

ALLOW, BLOCK = 0, 2


class SelectionGuardTest(unittest.TestCase):
    def setUp(self) -> None:
        self.session = f"test-{uuid.uuid4().hex[:12]}"

    def tearDown(self) -> None:
        sg.marker_path(self.session).unlink(missing_ok=True)

    def prompt(self, text: str) -> int:
        return sg.handle_event(
            {"session_id": self.session, "prompt": text, "cwd": str(REPO_ROOT)}
        )

    def write(self, tool: str = "Write", session: str | None = None) -> int:
        return sg.handle_event(
            {"session_id": session or self.session, "tool_name": tool, "tool_input": {}}
        )

    # -- core rule ---------------------------------------------------------------

    def test_writes_allowed_when_no_selection_in_progress(self) -> None:
        self.assertEqual(self.write(), ALLOW)

    def test_set_command_arms_guard_and_blocks_writes(self) -> None:
        self.prompt("set rcm")
        for tool in ("Write", "Edit", "NotebookEdit", "apply_patch", "WriteFile"):
            with self.subTest(tool=tool):
                self.assertEqual(self.write(tool), BLOCK)

    def test_long_form_set_command_arms_guard(self) -> None:
        self.prompt("set current workspace to rcm")
        self.assertEqual(self.write(), BLOCK)

    def test_confirmation_message_disarms_guard(self) -> None:
        self.prompt("set rcm")
        self.assertEqual(self.write(), BLOCK)
        self.prompt("yes, use those files and continue")
        self.assertEqual(self.write(), ALLOW)

    def test_bare_workspace_name_arms_guard(self) -> None:
        # CLAUDE.md treats a bare project name as a selection command.
        self.prompt("rcm")
        self.assertEqual(self.write(), BLOCK)

    def test_ordinary_prose_does_not_arm_guard(self) -> None:
        self.prompt("please add a settings toggle to the dashboard")
        self.assertEqual(self.write(), ALLOW)

    def test_unknown_bare_word_does_not_arm_guard(self) -> None:
        self.prompt("continue")
        self.assertEqual(self.write(), ALLOW)

    def test_read_only_tools_pass_while_armed(self) -> None:
        self.prompt("set rcm")
        for tool in ("Read", "Grep", "Glob", "Bash"):
            with self.subTest(tool=tool):
                self.assertEqual(self.write(tool), ALLOW)

    def test_sessions_are_isolated(self) -> None:
        self.prompt("set rcm")
        self.assertEqual(self.write(session=f"other-{uuid.uuid4().hex[:8]}"), ALLOW)

    # -- agent-agnostic normalization --------------------------------------------

    def test_camelcase_field_names_from_other_harnesses(self) -> None:
        sg.handle_event(
            {"sessionId": self.session, "userPrompt": "set rcm", "cwd": str(REPO_ROOT)}
        )
        self.assertEqual(
            sg.handle_event({"sessionId": self.session, "toolName": "apply_patch"}), BLOCK
        )

    def test_guard_does_not_depend_on_hook_event_name(self) -> None:
        # No `hook_event_name` key at all: shape alone must decide.
        self.prompt("set rcm")
        self.assertEqual(self.write(), BLOCK)

    # -- process contract ---------------------------------------------------------

    def _run(self, payload: str) -> int:
        return subprocess.run(
            [sys.executable, str(GUARD)], input=payload, capture_output=True, text=True
        ).returncode

    def test_stdin_contract_blocks_and_allows(self) -> None:
        session = f"proc-{uuid.uuid4().hex[:8]}"
        try:
            self._run(json.dumps({"session_id": session, "prompt": "set rcm",
                                  "cwd": str(REPO_ROOT)}))
            self.assertEqual(
                self._run(json.dumps({"session_id": session, "tool_name": "Write"})), BLOCK
            )
        finally:
            sg.marker_path(session).unlink(missing_ok=True)

    def test_malformed_event_fails_open(self) -> None:
        self.assertEqual(self._run("not json"), ALLOW)
        self.assertEqual(self._run("[]"), ALLOW)
        self.assertEqual(self._run("{}"), ALLOW)


if __name__ == "__main__":
    unittest.main()
