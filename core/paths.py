"""Portable project-root and path helpers.

All repo-owned runtime paths should be derived from ``PROJECT_ROOT`` instead of
the current working directory or machine-specific absolute paths.
"""
from __future__ import annotations

import os
from pathlib import Path

PROJECT_ROOT_ENV = "AUTORESEARCH_PROJECT_ROOT"
PROJECT_ROOT_MARKERS = ("pyproject.toml", ".git", "AGENTS.md")


def find_project_root(start: str | Path | None = None) -> Path:
    """Return the repository root using an env override or parent marker scan."""
    configured = os.environ.get(PROJECT_ROOT_ENV)
    if configured:
        return Path(configured).expanduser().resolve()

    current = Path(start).expanduser().resolve() if start else Path(__file__).resolve()
    if current.is_file():
        current = current.parent

    for candidate in (current, *current.parents):
        if any((candidate / marker).exists() for marker in PROJECT_ROOT_MARKERS):
            return candidate

    return Path(__file__).resolve().parents[1]


PROJECT_ROOT = find_project_root()


def project_path(*parts: str | os.PathLike[str]) -> Path:
    """Build an absolute path under the project root."""
    rel_parts = [Path(part) for part in parts]
    absolute_parts = [str(part) for part in rel_parts if part.is_absolute()]
    if absolute_parts:
        raise ValueError(f"project_path parts must be relative: {absolute_parts[0]}")
    return PROJECT_ROOT.joinpath(*rel_parts).resolve()


def resolve_project_path(
    value: str | Path,
    *,
    base: str | Path | None = None,
    allow_absolute: bool = True,
) -> Path:
    """Resolve a user/config path against the project root or a provided base."""
    path = Path(value).expanduser()
    if path.is_absolute():
        if not allow_absolute:
            raise ValueError(f"absolute paths are not allowed here: {value}")
        return path.resolve()
    root = Path(base).expanduser().resolve() if base else PROJECT_ROOT
    return (root / path).resolve()


def relative_to_project(path: str | Path) -> str:
    """Return a stable POSIX-style path relative to ``PROJECT_ROOT`` when possible."""
    return rel_to(path, PROJECT_ROOT)


def rel_to(path: str | Path, root: str | Path) -> str:
    """Return a stable POSIX-style path relative to ``root`` when possible.

    Parameterized sibling of :func:`relative_to_project` for callers whose
    root is not the global ``PROJECT_ROOT`` (e.g. a workspace/repo root passed
    as a function argument, or a test's temp-directory root). Consolidates a
    ``_rel(path, root)`` helper that had drifted into ~6 near-identical local
    copies across ``core/onboarding/**`` -- some normalized the not-relative
    fallback to POSIX, others returned the OS-native ``str(path)``. This
    always POSIX-normalizes, matching the most robust of the prior copies.
    """
    resolved = Path(path).expanduser().resolve()
    try:
        return resolved.relative_to(Path(root).expanduser().resolve()).as_posix()
    except ValueError:
        return resolved.as_posix()
