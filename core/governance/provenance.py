"""Decision provenance: distinguish a human confirmer from an agent-asserted one.

The Human-Gate Provenance Rule (CLAUDE.md / AGENTS.md) says an agent-asserted
decision must be ``source: agent`` and only a real human's confirmation is
``source: human``. The original logic was ``"human" if confirmed_by else
"agent"`` -- so an agent passing ``--confirmed-by agent`` (or any agent
identity) got stamped ``source: human``, silently turning an agent guess into
"human-approved" evidence. That is the exact integrity hole the rule guards.

This module centralizes the check: a ``confirmed_by`` value that is empty OR
denotes an agent/automation identity is agent-asserted; anything else is treated
as a human name. Generic -- the agent-token set is automation vocabulary plus the
CLI/orchestrator names, never a workspace/domain term.
"""
from __future__ import annotations

import re

# Identities that denote an agent/automation rather than a human confirmer.
_AGENT_TOKENS = frozenset(
    {
        "",
        "agent",
        "agents",
        "ai",
        "assistant",
        "auto",
        "automated",
        "automation",
        "bot",
        "cli",
        "cliagent",
        "claude",
        "codex",
        "gemini",
        "llm",
        "model",
        "orchestrator",
        "subagent",
        "system",
        "tool",
    }
)


def _norm(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", "", str(value or "").lower())


def is_agent_confirmer(confirmed_by: str) -> bool:
    """True when ``confirmed_by`` is empty or an agent/automation identity.

    A human name (e.g. ``"Dr. Smith"``, ``"alice"``) returns False. The literal
    ``"agent"`` (or ``"claude"``/``"gemini"``/etc.) returns True, so an agent's
    self-confirmation is never recorded as human.
    """
    return _norm(confirmed_by) in _AGENT_TOKENS


def decision_source(confirmed_by: str) -> str:
    """``"human"`` only for a real human confirmer; ``"agent"`` otherwise."""
    return "agent" if is_agent_confirmer(confirmed_by) else "human"


__all__ = ["is_agent_confirmer", "decision_source"]
