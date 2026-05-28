"""Workspace lexicon: per-workspace vocabulary derived from existing evidence.

The lexicon replaces hardcoded keyword ladders. Every metric phrase, cut
phrase, and column alias is derived from workspace artifacts (authored KPI
registry cells, profile schemas, accepted user definitions, prior resolution
history) — never from a checked-in domain dictionary.

Public surface:

- ``build_workspace_lexicon(layout, repo_root)`` — derive the lexicon from
  current workspace artifacts and write it as a generated contract.
- ``load_workspace_lexicon(layout)`` — read the lexicon back from its
  generated path. Returns ``None`` if it has not been built yet.
- ``WorkspaceLexicon`` — the in-memory matcher with
  ``infer_metric_and_cuts(name, description)`` and
  ``aliases_for_column(column)`` methods.
"""
from __future__ import annotations

from .builder import (
    WorkspaceLexicon,
    build_workspace_lexicon,
    load_workspace_lexicon,
)

__all__ = [
    "WorkspaceLexicon",
    "build_workspace_lexicon",
    "load_workspace_lexicon",
]
