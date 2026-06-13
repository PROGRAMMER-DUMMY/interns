"""Policy helpers for huge external data roots."""
from __future__ import annotations

import json
import os
import time
from collections import deque
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable


@dataclass(frozen=True)
class ExternalDataPolicy:
    max_paths: int = 200
    max_seconds: float = 30.0
    configured_roots: tuple[Path, ...] = ()


def load_external_data_policy(repo_root: Path) -> ExternalDataPolicy:
    """Load tracked defaults plus ignored local external-root overrides."""
    example = _read_json(repo_root / "config" / "external_data_roots.example.json")
    local = _read_json(repo_root / "config" / "external_data_roots.local.json")
    default_policy = example.get("default_policy", {}) if isinstance(example, dict) else {}
    max_paths = int(default_policy.get("max_paths") or 200)
    max_seconds = float(default_policy.get("max_seconds") or 30)
    roots: list[Path] = []

    profiles = example.get("profiles", {}) if isinstance(example, dict) else {}
    if isinstance(profiles, dict):
        for profile in profiles.values():
            if isinstance(profile, dict):
                env_name = str(profile.get("local_root_env") or "")
                if env_name and os.environ.get(env_name):
                    roots.append(Path(os.environ[env_name]).expanduser().resolve())

    roots.extend(_roots_from_local_config(local))
    return ExternalDataPolicy(max_paths=max_paths, max_seconds=max_seconds, configured_roots=tuple(roots))


def is_external_path(path: Path, repo_root: Path, policy: ExternalDataPolicy | None = None) -> bool:
    resolved = path.expanduser().resolve()
    root = repo_root.resolve()
    try:
        resolved.relative_to(root)
        inside_repo = True
    except ValueError:
        inside_repo = False
    if not inside_repo:
        return True
    if policy:
        return any(_is_relative_to(resolved, external_root) for external_root in policy.configured_roots)
    return False


def is_within_allowed_roots(
    path: Path, repo_root: Path, policy: ExternalDataPolicy | None = None
) -> bool:
    """True if ``path`` is inside the repo OR inside a configured external root.

    This is the GOVERNANCE allowlist check (distinct from :func:`is_external_path`,
    which merely classifies in-repo vs out-of-repo and returns True for *any*
    out-of-repo path). Discovery/local-source ingestion must use THIS so an
    arbitrary absolute host path is refused unless an operator configured it as an
    external root. Ref: core-audit ob-sources.md (theme T8).
    """
    resolved = path.expanduser().resolve()
    if _is_relative_to(resolved, repo_root.resolve()):
        return True
    roots = policy.configured_roots if policy else ()
    return any(_is_relative_to(resolved, external_root) for external_root in roots)


def bounded_external_files(root: Path, *, max_paths: int, max_seconds: float) -> tuple[list[Path], bool]:
    """Return file paths from an external root without reading contents."""
    start = time.monotonic()
    files: list[Path] = []
    queue: deque[Path] = deque([root])
    truncated = False
    while queue:
        if time.monotonic() - start > max_seconds:
            truncated = True
            break
        current = queue.popleft()
        try:
            children = sorted(current.iterdir(), key=lambda item: item.name.lower())
        except OSError:
            continue
        for child in children:
            if time.monotonic() - start > max_seconds:
                truncated = True
                break
            if child.is_dir():
                queue.append(child)
            elif child.is_file():
                if len(files) >= max_paths:
                    truncated = True
                    break
                files.append(child)
        if truncated:
            break
    return files, truncated


def allowlist_entries(settings: dict[str, Any]) -> list[dict[str, str]]:
    entries = settings.get("dataset_allowlist") or []
    normalized: list[dict[str, str]] = []
    if not isinstance(entries, list):
        return normalized
    for entry in entries:
        if isinstance(entry, str):
            entry_type = "external_absolute" if Path(entry).is_absolute() else "workspace_relative"
            normalized.append({"type": entry_type, "path": entry, "reason": ""})
        elif isinstance(entry, dict):
            path = str(entry.get("path") or "")
            if not path:
                continue
            entry_type = str(entry.get("type") or "")
            if entry_type not in {"workspace_relative", "external_absolute"}:
                entry_type = "external_absolute" if Path(path).is_absolute() else "workspace_relative"
            normalized.append(
                {
                    "type": entry_type,
                    "path": path,
                    "reason": str(entry.get("reason") or ""),
                }
            )
    return normalized


def external_allowlist_paths(settings: dict[str, Any]) -> list[Path]:
    paths = []
    for entry in allowlist_entries(settings):
        if entry["type"] == "external_absolute":
            paths.append(Path(entry["path"]).expanduser().resolve())
    return paths


def path_allowed_by_entries(path: Path, *, project_root: Path, settings: dict[str, Any]) -> bool:
    entries = allowlist_entries(settings)
    resolved = path.expanduser().resolve()
    if not entries:
        return _is_relative_to(resolved, project_root.resolve())

    for entry in entries:
        allowed = Path(entry["path"]).expanduser()
        if entry["type"] == "workspace_relative":
            allowed_path = _resolve_workspace_allowlist_path(allowed, project_root)
        else:
            allowed_path = allowed.resolve()
        if _is_relative_to(resolved, allowed_path):
            return True
    return False


def _resolve_workspace_allowlist_path(path: Path, project_root: Path) -> Path:
    if path.is_absolute():
        return path.resolve()
    workspace = project_root.resolve()
    parts = path.parts
    if len(parts) >= 3 and parts[0] == "workspaces" and parts[1] == workspace.name:
        return (workspace / Path(*parts[2:])).resolve()
    return (workspace / path).resolve()


def _roots_from_local_config(data: dict[str, Any]) -> Iterable[Path]:
    roots = data.get("external_roots") or data.get("roots") or []
    if not isinstance(roots, list):
        return []
    paths: list[Path] = []
    for item in roots:
        if isinstance(item, str):
            paths.append(Path(item).expanduser().resolve())
        elif isinstance(item, dict) and item.get("path"):
            paths.append(Path(str(item["path"])).expanduser().resolve())
    return paths


def _read_json(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return data if isinstance(data, dict) else {}


def _is_relative_to(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
        return True
    except ValueError:
        return False
