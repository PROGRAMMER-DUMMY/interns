"""Workspace vocabulary loader with universal-seed fallback.

Single helper every call site that previously held a curated domain word
list should use instead. Returns workspace-derived terms first; falls
back to a tiny truly-generic seed when the workspace hasn't been researched
yet.

Workspace-agnostic. No domain vocabulary baked in.
"""
from __future__ import annotations

import json
from functools import lru_cache
from pathlib import Path
from typing import Any

from core.onboarding.workspace.research import (
    GENERIC_FINANCIAL_SEED,
    GENERIC_IDENTIFIER_SUFFIXES,
    GENERIC_TEMPORAL_SEED,
)
from core.storage.workspace_layout import WorkspaceLayout


def _read_vocabulary(layout: WorkspaceLayout) -> dict[str, Any]:
    path = layout.contracts_dir / "workspace_vocabulary.json"
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return {}
    return data if isinstance(data, dict) else {}


def terms_for(layout: WorkspaceLayout, category: str) -> list[str]:
    """Return the workspace-derived terms for a category, or a generic seed fallback.

    `category` is one of: "financial_terms", "temporal_terms", "entity_terms",
    "filter_terms", "identifier_terms".
    """
    data = _read_vocabulary(layout)
    items = (data.get("categories") or {}).get(category) or []
    if items:
        return [str(item.get("term")) for item in items if isinstance(item, dict) and item.get("term")]

    if category == "financial_terms":
        return list(GENERIC_FINANCIAL_SEED)
    if category == "temporal_terms":
        return list(GENERIC_TEMPORAL_SEED)
    if category == "identifier_terms":
        return list(GENERIC_IDENTIFIER_SUFFIXES)
    return []


def vocabulary_confidence(layout: WorkspaceLayout) -> tuple[float, bool]:
    """Return (overall_confidence, needs_user_confirmation) from the latest research."""
    data = _read_vocabulary(layout)
    confidence = float(data.get("overall_confidence") or 0.0)
    needs = bool(data.get("needs_user_confirmation", False))
    return confidence, needs


def vocabulary_present(layout: WorkspaceLayout) -> bool:
    """True if research has been run for this workspace at least once."""
    path = layout.contracts_dir / "workspace_vocabulary.json"
    return path.exists()


__all__ = [
    "GENERIC_FINANCIAL_SEED",
    "GENERIC_IDENTIFIER_SUFFIXES",
    "GENERIC_TEMPORAL_SEED",
    "terms_for",
    "vocabulary_confidence",
    "vocabulary_present",
]
