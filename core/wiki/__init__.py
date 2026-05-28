"""Per-workspace LLM-wiki: human-readable Markdown annotations of JSON contracts.

The wiki is additive to the existing JSON contract pipeline. JSON contracts remain
the source of truth that the validator enforces; wiki notes carry the *why*,
evidence, decision history, and business context that prose captures better than
structured fields.

Machine-owned sections are auto-refreshed on every writer pass; human-owned
sections are preserved verbatim across upserts.
"""
from core.wiki.layout import WikiLayout
from core.wiki.reader import WikiNote, read_feature_note
from core.wiki.writer import (
    build_feature_scaffold,
    build_kpi_completion_scaffold,
    upsert_feature_note,
    upsert_kpi_note,
)

__all__ = [
    "WikiLayout",
    "WikiNote",
    "build_feature_scaffold",
    "build_kpi_completion_scaffold",
    "read_feature_note",
    "upsert_feature_note",
    "upsert_kpi_note",
]
