#!/usr/bin/env python
"""PreToolUse guard: hard stop on displaying secret/credential files.

Enforces the repo's "Secret And Environment Display Guardrail" (AGENTS.md / CLAUDE.md):
never print `.env`, `.databrickscfg`, private keys, tokens, or credential files. This
guard blocks Read of such files and shell commands that would cat/type/Get-Content them.

Contract: reads the PreToolUse JSON event on stdin. Exit 0 = allow; exit 2 = block
(stderr shown to the agent). Fails open on any internal error.
"""
from __future__ import annotations

import json
import re
import sys

# Filename patterns that name a secret/credential artifact.
SECRET_PATTERNS = (
    r"(^|[\\/])\.env(\.[\w.-]+)?$",       # .env, .env.local, .env.prod
    r"(^|[\\/])\.env(\.[\w.-]+)?[\"'\s]",  # .env referenced mid-command
    r"\.databrickscfg\b",
    r"(^|[\\/])id_rsa\b",
    r"\.pem\b",
    r"\.key\b",
    r"\.p12\b",
    r"\.pfx\b",
    r"(^|[\\/])credentials(\.\w+)?\b",
    r"(^|[\\/])\.netrc\b",
    r"(^|[\\/])\.pgpass\b",
)

# Shell verbs that would print file contents.
DISPLAY_VERBS = (
    r"\bcat\b",
    r"\btype\b",
    r"\bGet-Content\b",
    r"\bgc\b",
    r"\bmore\b",
    r"\bless\b",
    r"\bhead\b",
    r"\btail\b",
    r"\bbat\b",
    r"\bSelect-String\b",
)

MESSAGE = (
    "Blocked: this would display a secret/credential file (.env, .databrickscfg, private "
    "key, token, credentials).\n"
    "Repo hard rule: report only existence/status or redacted key names "
    "(e.g. OPENAI_API_KEY=<redacted>), never the values."
)


def _matches_secret(text: str) -> bool:
    return any(re.search(p, text) for p in SECRET_PATTERNS)


def main() -> int:
    try:
        event = json.load(sys.stdin)
    except Exception:
        return 0  # fail open
    tool = event.get("tool_name")
    tool_input = event.get("tool_input") or {}

    if tool in ("Read", "Edit", "Write", "NotebookEdit"):
        path = str(tool_input.get("file_path") or tool_input.get("notebook_path") or "")
        if _matches_secret(path):
            sys.stderr.write(MESSAGE + "\n")
            return 2
        return 0

    if tool == "Bash":
        command = str(tool_input.get("command") or "")
        if _matches_secret(command) and any(re.search(v, command) for v in DISPLAY_VERBS):
            sys.stderr.write(MESSAGE + "\n")
            return 2
        # Block obvious environment dumps that would spill secret values.
        if re.search(r"\b(printenv|Get-ChildItem\s+Env:|gci\s+env:)\b", command) or re.search(
            r"\benv\b\s*(\||$)", command
        ):
            sys.stderr.write(MESSAGE + "\n")
            return 2
        return 0

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
