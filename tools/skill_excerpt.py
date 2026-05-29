"""Skill-body excerpting.

Skills live at `skills/<name>/SKILL.md` and are typically 5-15 KB of
markdown with multiple sections. When an orchestrator activates a skill,
loading the full body burns context that often isn't needed — the agent
usually only needs the "When to invoke" section, or the "Output format"
section, depending on the task.

This module returns just the requested section (plus the frontmatter,
always) so the orchestrator can request a precise excerpt and keep its
main chat context lean.

Generic across skills. No skill names hardcoded.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from core.paths import PROJECT_ROOT


SKILLS_DIR = PROJECT_ROOT / "skills"
_HEADING_RE = re.compile(r"^(#{1,6})\s+(.+?)\s*$")


@dataclass(frozen=True)
class SkillExcerpt:
    skill: str
    requested_section: str
    matched_section: str
    frontmatter: str
    body: str
    full_size_bytes: int
    excerpt_size_bytes: int

    def to_dict(self) -> dict[str, Any]:
        return {
            "skill": self.skill,
            "requested_section": self.requested_section,
            "matched_section": self.matched_section,
            "frontmatter": self.frontmatter,
            "body": self.body,
            "full_size_bytes": self.full_size_bytes,
            "excerpt_size_bytes": self.excerpt_size_bytes,
            "context_savings_pct": (
                round((1 - (self.excerpt_size_bytes / max(1, self.full_size_bytes))) * 100, 1)
                if self.full_size_bytes
                else 0.0
            ),
        }


def _skill_path(skill_name: str) -> Path:
    return SKILLS_DIR / skill_name / "SKILL.md"


def _split_frontmatter(text: str) -> tuple[str, str]:
    """Return (frontmatter_block, body) — frontmatter includes the --- delimiters."""
    if not text.startswith("---"):
        return "", text
    lines = text.split("\n")
    if len(lines) < 2:
        return "", text
    end_idx = None
    for i in range(1, len(lines)):
        if lines[i].strip() == "---":
            end_idx = i
            break
    if end_idx is None:
        return "", text
    front = "\n".join(lines[: end_idx + 1])
    body = "\n".join(lines[end_idx + 1 :]).lstrip("\n")
    return front, body


def _section_index(body: str) -> list[tuple[int, int, str]]:
    """Return list of (level, line_index, heading_text) for every markdown heading."""
    out: list[tuple[int, int, str]] = []
    for idx, line in enumerate(body.split("\n")):
        match = _HEADING_RE.match(line)
        if match:
            level = len(match.group(1))
            heading = match.group(2).strip()
            out.append((level, idx, heading))
    return out


def _normalize(s: str) -> str:
    return re.sub(r"[^a-z0-9]+", " ", s.lower()).strip()


def _find_section(body: str, requested: str) -> tuple[str, str]:
    """Return (matched_heading_text, section_body_including_subsections).

    Matches loosely on normalized heading text. Empty `requested` returns
    the whole body. Unmatched returns ('', '').
    """
    sections = _section_index(body)
    if not requested or not sections:
        return ("", body)
    target_norm = _normalize(requested)
    lines = body.split("\n")
    best_idx: int | None = None
    best_level = 7
    for i, (level, line_idx, heading) in enumerate(sections):
        heading_norm = _normalize(heading)
        if target_norm in heading_norm or heading_norm in target_norm:
            if level < best_level:
                best_idx = i
                best_level = level
    if best_idx is None:
        return ("", "")
    start_line = sections[best_idx][1]
    end_line = len(lines)
    for j in range(best_idx + 1, len(sections)):
        if sections[j][0] <= sections[best_idx][0]:
            end_line = sections[j][1]
            break
    return (sections[best_idx][2], "\n".join(lines[start_line:end_line]).rstrip())


def get_skill_excerpt(skill_name: str, section: str = "") -> SkillExcerpt:
    """Return frontmatter + requested section of `skills/<skill_name>/SKILL.md`.

    `section` is matched loosely (case-insensitive, punctuation-ignoring).
    Empty section returns the full body. Unknown skill raises.
    Unmatched section returns frontmatter + empty body (no error).
    """
    path = _skill_path(skill_name)
    if not path.exists():
        raise FileNotFoundError(f"skill not found: {path}")
    full = path.read_text(encoding="utf-8")
    frontmatter, body = _split_frontmatter(full)
    matched_heading, section_body = _find_section(body, section)
    excerpt = section_body if section else body
    return SkillExcerpt(
        skill=skill_name,
        requested_section=section,
        matched_section=matched_heading,
        frontmatter=frontmatter,
        body=excerpt,
        full_size_bytes=len(full.encode("utf-8")),
        excerpt_size_bytes=len((frontmatter + "\n\n" + excerpt).encode("utf-8")),
    )


__all__ = ["SkillExcerpt", "get_skill_excerpt"]
