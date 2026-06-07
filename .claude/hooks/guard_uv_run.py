#!/usr/bin/env python
"""PreToolUse guard: block `uv run` for test runners and pyspark/engine generation.

Why: `uv run` resyncs and reinstalls pre-release pyspark 4.1.1 (no matching Delta
build), which silently breaks the pyspark-backed tests and engine generation. The
repo rule is to run these under `.venv\\Scripts\\python.exe`. Governed `uv run`
wrappers (onboard-workspace, resolve-kpi-features, ...) are NOT blocked -- only the
commands HANDOFF documents as breaking.

Contract: reads the PreToolUse JSON event on stdin. Exit 0 = allow; exit 2 = block
(stderr is shown to the agent). Any internal error fails open (exit 0) so the guard
can never wedge the session.
"""
from __future__ import annotations

import json
import re
import sys

# Commands that MUST use the venv interpreter, never `uv run`.
BLOCKED_AFTER_UV_RUN = (
    r"-m\s+unittest",
    r"-m\s+pytest",
    r"\bpytest\b",
    r"\bgenerate-kpi-pyspark\b",
    r"\bgenerate-kpi-engines\b",
    r"\btest_ci_pyspark_parity\b",
    r"verify-kpi-output\b.*--cross-engine",
)

MESSAGE = (
    "Blocked: `uv run` for tests / pyspark / engine generation reinstalls pre-release "
    "pyspark 4.1.1 (no Delta) and breaks them.\n"
    "Use the project venv interpreter instead, e.g.:\n"
    "  .venv\\Scripts\\python.exe -m unittest <modules>\n"
    "  .venv\\Scripts\\python.exe -m core.onboarding.kpi.generate_kpi_engines ...\n"
    "Or run the portable gate: green-gate  (installed against the venv interpreter).\n"
    "Note: governed `uv run` wrappers (onboard-workspace, resolve-kpi-features, ...) are fine."
)


def main() -> int:
    try:
        event = json.load(sys.stdin)
    except Exception:
        return 0  # fail open
    if event.get("tool_name") != "Bash":
        return 0
    command = str((event.get("tool_input") or {}).get("command") or "")
    if not re.search(r"\buv\s+run\b", command):
        return 0
    for pattern in BLOCKED_AFTER_UV_RUN:
        if re.search(pattern, command):
            sys.stderr.write(MESSAGE + "\n")
            return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
