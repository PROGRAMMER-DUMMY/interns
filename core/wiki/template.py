"""Canonical note structure: section names, machine/human boundaries, default body."""
from __future__ import annotations


MACHINE_SECTIONS = (
    "Current state",
    "Decision history",
    "Evidence",
    "Rejected options",
)

HUMAN_SECTIONS = (
    "Why (user)",
    "Business context",
    "Related notes",
)

ALL_SECTIONS = MACHINE_SECTIONS + HUMAN_SECTIONS

WHY_TODO_MARKER = "<!-- TODO: explain why this option was the right business choice -->"
BUSINESS_TODO_MARKER = "<!-- Optional: business rules, vendor quirks, edge cases -->"
RELATED_TODO_MARKER = "<!-- [[wiki-links]] to other entity notes -->"


HUMAN_DEFAULTS = {
    "Why (user)": WHY_TODO_MARKER,
    "Business context": BUSINESS_TODO_MARKER,
    "Related notes": RELATED_TODO_MARKER,
}


def empty_section(name: str) -> str:
    """Default body for a human-owned section when a note is freshly scaffolded."""
    return HUMAN_DEFAULTS.get(name, "")


def is_machine_section(name: str) -> bool:
    return name in MACHINE_SECTIONS


def is_human_section(name: str) -> bool:
    return name in HUMAN_SECTIONS
