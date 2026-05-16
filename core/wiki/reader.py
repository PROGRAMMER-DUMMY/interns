"""Parse and read entity notes from a workspace wiki."""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

from core.wiki.layout import WikiLayout


_SECTION_RE = re.compile(r"^##\s+(.+?)\s*$", re.MULTILINE)


@dataclass(frozen=True)
class WikiNote:
    """A parsed entity note: frontmatter + ordered sections."""

    path: Path
    frontmatter: dict[str, Any] = field(default_factory=dict)
    sections: dict[str, str] = field(default_factory=dict)
    body_lead: str = ""

    @property
    def why(self) -> str:
        return _strip_todo(self.sections.get("Why (user)", ""))

    @property
    def business_context(self) -> str:
        return _strip_todo(self.sections.get("Business context", ""))

    @property
    def has_user_why(self) -> bool:
        return bool(self.why)

    def summary_line(self) -> str:
        return str(self.frontmatter.get("summary") or "").strip()


def read_feature_note(layout: WikiLayout, feature_id: str) -> WikiNote | None:
    path = layout.feature_note_path(feature_id)
    return read_note(path)


def read_note(path: Path) -> WikiNote | None:
    if not path.exists():
        return None
    raw = path.read_text(encoding="utf-8")
    fm, body = _split_frontmatter(raw)
    lead, sections = _split_sections(body)
    return WikiNote(path=path, frontmatter=fm, sections=sections, body_lead=lead)


def _split_frontmatter(text: str) -> tuple[dict[str, Any], str]:
    if not text.startswith("---"):
        return {}, text
    lines = text.splitlines(keepends=True)
    if not lines or lines[0].rstrip("\r\n") != "---":
        return {}, text
    for idx in range(1, len(lines)):
        if lines[idx].rstrip("\r\n") == "---":
            fm_text = "".join(lines[1:idx])
            body = "".join(lines[idx + 1:])
            try:
                parsed = yaml.safe_load(fm_text) or {}
            except yaml.YAMLError:
                parsed = {}
            if not isinstance(parsed, dict):
                parsed = {}
            return parsed, body.lstrip("\n")
    return {}, text


def _split_sections(body: str) -> tuple[str, dict[str, str]]:
    matches = list(_SECTION_RE.finditer(body))
    if not matches:
        return body.rstrip() + "\n" if body.strip() else "", {}
    lead = body[: matches[0].start()].rstrip()
    sections: dict[str, str] = {}
    for idx, match in enumerate(matches):
        name = match.group(1).strip()
        start = match.end()
        end = matches[idx + 1].start() if idx + 1 < len(matches) else len(body)
        sections[name] = body[start:end].strip("\n").rstrip()
    return lead, sections


def _strip_todo(text: str) -> str:
    cleaned = re.sub(r"<!--.*?-->", "", text, flags=re.DOTALL).strip()
    return cleaned
