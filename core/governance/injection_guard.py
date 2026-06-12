"""Prompt-injection guard for untrusted workspace content.

Workspace artifacts (uploaded documents, data dictionaries, sample data
values) flow into LLM-facing surfaces: blocker question panels, document
sidecars, derived-feature evidence. A hostile workspace can plant
instruction-like text in any of them ("ignore previous instructions",
chat-template markers, exfiltration requests) hoping the orchestrating agent
treats data as commands.

This module is the display/ingest-side counterpart to the PII/PHI redaction
helpers:

  * ``scan_text`` -- detect instruction-like spans and return findings
    (pattern id + neutralized excerpt) for reports and panels.
  * ``neutralize_text`` -- replace matched spans with a visible
    ``[blocked-instruction]`` marker so downstream LLM consumers never see
    the live instruction text.
  * ``neutralize_rows`` -- apply ``neutralize_text`` to every string value of
    sample-row dicts.

Like ``pii_redaction`` it never mutates source data or rewrites SQL — only
the rendered/ingested surface is sanitized. Patterns are intentionally
conservative: a false positive costs one ``[blocked-instruction]`` marker in
display text, a false negative hands the agent a live injected instruction.

Dependency-free by design: stdlib only, no imports from ``core.*``.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Pattern


# Pattern id -> regexes (case-insensitive; ^ anchors are multiline).
INJECTION_PATTERNS: dict[str, tuple[str, ...]] = {
    "override_instructions": (
        r"\b(ignore|disregard|forget)\s+(all\s+|any\s+|the\s+|your\s+)?"
        r"(previous|prior|above|earlier|system)\s+"
        r"(instructions?|prompts?|rules?|context|messages?)\b",
        r"\byour\s+new\s+(role|task|instructions?|objective)\s+(is|are)\b",
        r"\bnew\s+instructions?\s*:",
    ),
    "role_hijack": (
        r"(?m)^\s*(system|assistant|developer)\s*:",
        r"\byou\s+are\s+now\b",
        r"\bpretend\s+(to\s+be|you\s+are)\b",
    ),
    "chat_template_markers": (
        r"<\|im_(start|end)\|>",
        r"\[/?INST\]",
        r"<<SYS>>",
        r"(?m)^\s*###\s*(system|instruction|assistant)\b",
        r"\bBEGIN\s+(SYSTEM|PROMPT)\b",
    ),
    "exfiltration_request": (
        r"\b(reveal|print|show|output|leak|send)\s+(your|the)\s+"
        r"(system\s+prompt|hidden\s+instructions?|secrets?|api\s+keys?|"
        r"credentials?|tokens?|environment\s+variables?)\b",
    ),
    "jailbreak_marker": (
        r"\bjailbreak\b",
        r"\bdo\s+anything\s+now\b",
        r"\bDAN\s+mode\b",
    ),
    "tool_abuse": (
        r"\b(run|execute)\s+(the\s+following\s+|this\s+)?"
        r"(shell|bash|powershell|cmd)\s+command\b",
    ),
}

NEUTRALIZED_MARKER: str = "[blocked-instruction]"

_EXCERPT_MAX_CHARS: int = 80

_COMPILED: tuple[tuple[str, Pattern[str]], ...] = tuple(
    (pattern_id, re.compile(p, re.IGNORECASE))
    for pattern_id, patterns in INJECTION_PATTERNS.items()
    for p in patterns
)


@dataclass(frozen=True)
class InjectionFinding:
    pattern_id: str
    excerpt: str  # the matched text, truncated; safe to render in reports

    def to_dict(self) -> dict[str, str]:
        return {"pattern_id": self.pattern_id, "excerpt": self.excerpt}


def scan_text(text: str) -> list[InjectionFinding]:
    """Return findings for every injection-pattern match in ``text``."""
    if not isinstance(text, str) or not text:
        return []
    findings: list[InjectionFinding] = []
    for pattern_id, regex in _COMPILED:
        for match in regex.finditer(text):
            excerpt = match.group(0)[:_EXCERPT_MAX_CHARS]
            findings.append(InjectionFinding(pattern_id=pattern_id, excerpt=excerpt))
    return findings


def neutralize_text(text: str, *, marker: str = NEUTRALIZED_MARKER) -> str:
    """Replace every injection-pattern match in ``text`` with ``marker``.

    Returns the input unchanged (same object) when nothing matches, so
    callers can cheaply detect "was anything neutralized" via identity.
    """
    if not isinstance(text, str) or not text:
        return text
    result = text
    for _pattern_id, regex in _COMPILED:
        result = regex.sub(marker, result)
    return result


def neutralize_rows(
    rows: list[dict[str, object]],
    *,
    marker: str = NEUTRALIZED_MARKER,
) -> list[dict[str, object]]:
    """Apply :func:`neutralize_text` to every string value. Returns a new list;
    never mutates the input rows."""
    sanitized: list[dict[str, object]] = []
    for row in rows:
        new_row: dict[str, object] = {}
        for key, value in row.items():
            new_row[key] = neutralize_text(value, marker=marker) if isinstance(value, str) else value
        sanitized.append(new_row)
    return sanitized


__all__ = [
    "INJECTION_PATTERNS",
    "NEUTRALIZED_MARKER",
    "InjectionFinding",
    "neutralize_rows",
    "neutralize_text",
    "scan_text",
]
