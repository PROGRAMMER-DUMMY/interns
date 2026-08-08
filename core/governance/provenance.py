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


def _tokens(value: str) -> list[str]:
    """Word tokens of an identity, lowercased, punctuation dropped."""
    return [t for t in re.split(r"[^a-z0-9]+", str(value or "").lower()) if t]


def is_agent_confirmer(confirmed_by: str) -> bool:
    """True when ``confirmed_by`` is empty or an agent/automation identity.

    A human name (e.g. ``"Dr. Smith"``, ``"alice"``) returns False. An agent
    identity returns True, so an agent's self-confirmation is never recorded as
    human.

    Matching is per WORD, not on the whole string. Exact whole-string matching
    let any agent bypass every human gate by appending a word:
    ``"agent (platform recommendation)"`` normalised to
    ``"agentplatformrecommendation"``, matched nothing, and was recorded as
    ``human`` -- defeating the refusal that `confirm-blueprint` and the review
    gates exist to enforce. Found live while driving a real replay.

    Per-word matching also fails CLOSED: a human genuinely called "Agent" is
    refused, which costs one flag rename; the inverse silently launders an
    agent decision into a human approval.
    """
    if not str(confirmed_by or "").strip():
        return True
    return any(token in _AGENT_TOKENS for token in _tokens(confirmed_by))


def decision_source(confirmed_by: str) -> str:
    """``"human"`` only for a real human confirmer; ``"agent"`` otherwise."""
    return "agent" if is_agent_confirmer(confirmed_by) else "human"


__all__ = ["is_agent_confirmer", "decision_source"]
