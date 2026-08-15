"""Selection-turn write guard -- harness-agnostic enforcement of AGENTS.md Step 0.

AGENTS.md > "Step 0: Active Workflow Setup" is a HARD STOP: during a `set <workspace>`
turn the agent must not create, edit, or delete ANY file (including ``.gitignore``,
settings files, and generated artifacts) until the user has confirmed the file set. That
rule was prose only -- repeated in AGENTS.md and CLAUDE.md precisely because a single
statement of it did not hold. This module makes it mechanism.

Agent-agnostic by construction. AGENTS.md ("Multi-Phase Plan Persistence") states this
repo is driven by interchangeable CLIs (`claude-code`, `codex`, `gemini-cli`), so the
decision logic lives here in repo code rather than inside any one harness's hook
directory. Harnesses differ in *how* they invoke a guard, not in what the rule is:

* ``claude-code`` -- ``.claude/settings.json`` PreToolUse + UserPromptSubmit hooks.
* ``codex``       -- ``.codex/hooks.json``, same event/matcher shape.
* ``gemini-cli``  -- no hook surface; it enforces via the ``tools.allowed`` allowlist in
  ``.gemini/settings.json``, which today contains no file-write tool at all, so writes
  already require an explicit prompt there. Nothing to register; see CONTEXT-governance.

Event shape is normalized rather than assumed, so a harness that names its fields
differently still works: a payload carrying a prompt is treated as a turn-start, and a
payload carrying a tool name is treated as a tool gate.

Invoke directly by path -- no package import required::

    python core/governance/selection_guard.py   # reads the hook event JSON on stdin

Contract: exit 0 = allow, exit 2 = block (stderr is shown to the agent). Any internal
error fails open (exit 0) so the guard can never wedge a session.
"""

from __future__ import annotations

import json
import re
import sys
import tempfile
from pathlib import Path
from typing import Any

# `set <ws>` / `set current workspace to <ws>` -- the explicit forms from CLAUDE.md.
_SET_RE = re.compile(r"^\s*set\s+(?:current\s+workspace\s+to\s+)?([^\s,.;!?]+)", re.I)

# Tools that create/modify/delete files, across harness naming conventions.
MUTATING_TOOLS = {
    "edit",
    "write",
    "notebookedit",
    "apply_patch",
    "writefile",
    "replace",
    "create_file",
}

MESSAGE = (
    "Blocked: file mutation during a workspace SELECTION turn.\n"
    "AGENTS.md > Step 0 is a HARD STOP -- no Edit/Write/delete on ANY file (including "
    ".gitignore, settings.json, and generated artifacts) until the user has confirmed "
    "the workspace AND authorized continuing.\n"
    "Do this instead: run `uv run list-workspace-files --workspace workspaces/<project>`, "
    "summarize the file set, and ask the confirmation question. The guard clears on the "
    "user's next message."
)


def marker_path(session_id: str) -> Path:
    """Per-session marker. Temp dir, so nothing lands in the repo and the OS reaps it."""
    safe = re.sub(r"[^A-Za-z0-9_-]", "", str(session_id or ""))[:64] or "nosession"
    return Path(tempfile.gettempdir()) / f"selection_pending_{safe}"


def workspace_names(cwd: str | Path) -> set[str]:
    try:
        return {p.name.lower() for p in (Path(cwd) / "workspaces").iterdir() if p.is_dir()}
    except Exception:
        return set()


def is_selection_prompt(prompt: str, cwd: str | Path = ".") -> bool:
    """True when the user's message is a workspace-selection command."""
    if _SET_RE.match(prompt or ""):
        return True
    # CLAUDE.md also treats a bare project name as selection. Only match a lone token
    # that is really a workspace folder, so ordinary prose cannot arm the guard.
    bare = (prompt or "").strip().strip("\"'`")
    if bare and len(bare.split()) == 1:
        return bare.lower() in workspace_names(cwd)
    return False


def _first(event: dict[str, Any], *keys: str) -> str:
    for key in keys:
        value = event.get(key)
        if isinstance(value, str) and value:
            return value
    return ""


def handle_event(event: dict[str, Any]) -> int:
    """Normalized, harness-agnostic dispatch. Returns the process exit code."""
    session = _first(event, "session_id", "sessionId", "session")
    marker = marker_path(session)

    tool = _first(event, "tool_name", "toolName", "tool").strip().lower()
    prompt = _first(event, "prompt", "user_prompt", "userPrompt", "message")

    # A payload carrying a prompt and no tool is a turn-start: arm or disarm. The user's
    # next message after a selection IS the confirmation, so disarm-on-anything-else is
    # the rule, not a heuristic.
    if prompt and not tool:
        try:
            if is_selection_prompt(prompt, event.get("cwd") or "."):
                marker.write_text("pending", encoding="utf-8")
            else:
                marker.unlink(missing_ok=True)
        except Exception:
            pass  # fail open
        return 0

    if tool not in MUTATING_TOOLS:
        return 0
    try:
        armed = marker.exists()
    except Exception:
        return 0  # fail open
    if armed:
        sys.stderr.write(MESSAGE + "\n")
        return 2
    return 0


def main() -> int:
    try:
        event = json.load(sys.stdin)
    except Exception:
        return 0  # fail open
    if not isinstance(event, dict):
        return 0
    try:
        return handle_event(event)
    except Exception:
        return 0  # fail open


if __name__ == "__main__":
    raise SystemExit(main())
