"""Deep-knowledge registry for MinusDataOps.

Each of the six framework steps has a depth module exporting ``DEEP: DeepStep``.
This registry collects whatever is present -- tolerant of a module that is missing
or not yet written -- so the system degrades gracefully to the base framework.
"""

from __future__ import annotations

import importlib

from minus_dataops.depth._schema import (
    Comparison, DeepStep, Heuristic, Option, Topic,
)

# step_key -> module name (module lives in this package)
_MODULES = {
    "requirements": "requirements",
    "pipeline": "pipeline",
    "modeling": "modeling",
    "storage": "storage",
    "quality": "quality",
    "scalability": "scalability",
}


def _load() -> dict[str, DeepStep]:
    out: dict[str, DeepStep] = {}
    for key, modname in _MODULES.items():
        try:
            mod = importlib.import_module(f"minus_dataops.depth.{modname}")
        except Exception:
            continue
        deep = getattr(mod, "DEEP", None)
        if isinstance(deep, DeepStep):
            out[deep.step_key] = deep
    return out


_CACHE: dict[str, DeepStep] | None = None


def deep_steps() -> dict[str, DeepStep]:
    """All available deep dossiers, keyed by framework step key."""
    global _CACHE
    if _CACHE is None:
        _CACHE = _load()
    return dict(_CACHE)


def get_deep(key: str) -> DeepStep | None:
    """Deep dossier for one step (by framework key), or None if not available."""
    return deep_steps().get(key)


def available() -> tuple[str, ...]:
    return tuple(deep_steps().keys())


__all__ = [
    "Option", "Comparison", "Heuristic", "Topic", "DeepStep",
    "deep_steps", "get_deep", "available",
]
