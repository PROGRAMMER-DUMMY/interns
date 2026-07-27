"""Policy helpers for huge external data roots."""
from __future__ import annotations

import json
import os
import re
import time
from collections import deque
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable


# A storage URI, as opposed to a local filesystem path. Anything fsspec can
# mount: s3://, gs://, abfs://, az://, http(s)://, sftp://, memory://.
# Detected by scheme rather than by a hardcoded protocol list so a backend this
# module has never heard of still routes down the URI path instead of being
# silently mangled into a local path -- `Path("s3://b/k")` collapses the double
# slash to `s3:/b/k`, the same identifier-collapse class that made the
# self-ingestion guard blind to Unity Catalog names.
_URI_SCHEME = re.compile(r"^[A-Za-z][A-Za-z0-9+.-]*://")


def is_storage_uri(value: Any) -> bool:
    """True when ``value`` names a remote storage location rather than a path."""
    return bool(_URI_SCHEME.match(str(value)))


def _normalize_uri(value: Any) -> str:
    """A URI with a single trailing slash, for prefix comparison."""
    return str(value).rstrip("/") + "/"


@dataclass(frozen=True)
class ExternalDataPolicy:
    max_paths: int = 200
    max_seconds: float = 30.0
    configured_roots: tuple[Path, ...] = ()
    # Remote roots kept SEPARATE from `configured_roots` on purpose: that field
    # is `Path` and every local caller's `.resolve()`/`relative_to` semantics
    # depend on it. Mixing URIs in would either mangle them or force every
    # consumer to re-learn the type. Additive, so the local path is untouched.
    configured_uri_roots: tuple[str, ...] = ()


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

    uri_roots: list[str] = []
    for profile in (profiles.values() if isinstance(profiles, dict) else []):
        if isinstance(profile, dict):
            env_name = str(profile.get("uri_root_env") or "")
            if env_name and os.environ.get(env_name):
                uri_roots.append(_normalize_uri(os.environ[env_name]))

    local_paths, local_uris = _split_roots(_raw_roots_from_local_config(local))
    roots.extend(local_paths)
    uri_roots.extend(local_uris)
    return ExternalDataPolicy(
        max_paths=max_paths,
        max_seconds=max_seconds,
        configured_roots=tuple(roots),
        configured_uri_roots=tuple(dict.fromkeys(uri_roots)),
    )


def _split_roots(raw: Iterable[Any]) -> tuple[list[Path], list[str]]:
    """Partition configured roots into local paths and storage URIs."""
    paths: list[Path] = []
    uris: list[str] = []
    for item in raw:
        text = str(item or "").strip()
        if not text:
            continue
        if is_storage_uri(text):
            uris.append(_normalize_uri(text))
        else:
            paths.append(Path(text).expanduser().resolve())
    return paths, uris


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
    # A storage URI is never "inside the repo" and cannot be `.resolve()`d, so it
    # is checked against the URI allowlist by normalised prefix. The trailing
    # slash on both sides is what stops `s3://b/data` from allow-listing
    # `s3://b/database` -- prefix matching without a segment boundary is how
    # allowlists leak.
    if is_storage_uri(path):
        candidate = _normalize_uri(path)
        return any(
            candidate.startswith(root)
            for root in (policy.configured_uri_roots if policy else ())
        )
    resolved = path.expanduser().resolve()
    if _is_relative_to(resolved, repo_root.resolve()):
        return True
    roots = policy.configured_roots if policy else ()
    return any(_is_relative_to(resolved, external_root) for external_root in roots)


def bounded_external_files(root: Any, *, max_paths: int, max_seconds: float) -> tuple[list[Any], bool]:
    """Return file paths from an external root without reading contents.

    Two walk strategies behind one signature, because they are genuinely
    different problems:

    * **Local filesystem** -- breadth-first over `iterdir()`, bounded by wall
      clock. Unchanged; every existing caller behaves exactly as before.
    * **Object storage** (`s3://`, `gs://`, `abfs://`, ...) -- a BFS here is
      wrong, not merely slow: object stores have no directories, so one
      `iterdir()` per pseudo-folder is one billable `LIST` per prefix. A single
      recursive prefix listing is the native operation.

    Returns `UPath` objects for remote roots. Callers only use `.suffix`,
    `.name`, `.stem` and `.relative_to()`, all of which `UPath` provides -- which
    is why classification needed no changes to work on any backend.
    """
    if is_storage_uri(root):
        return _bounded_remote_files(root, max_paths=max_paths, max_seconds=max_seconds)
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


def _bounded_remote_files(
    root: Any, *, max_paths: int, max_seconds: float
) -> tuple[list[Any], bool]:
    """List an object-storage prefix with one recursive call, then bound.

    ponytail: `fs.find()` materialises the whole prefix listing before we
    truncate, so the ceiling is the key count under the root, not `max_paths`.
    Fine for a discovery prefix; if someone points this at a bucket root with
    tens of millions of keys, switch to a paginated `ListObjectsV2` loop that
    stops at `max_paths`. `max_seconds` is accepted for signature parity and
    applied around the call, not inside it -- a single LIST is not interruptible.
    """
    try:
        from upath import UPath
    except ImportError as exc:  # pragma: no cover - upath is a base dependency
        raise RuntimeError(
            "universal_pathlib is required to read a storage URI. "
            "Install it, or pass a local path instead."
        ) from exc

    base = UPath(root)
    try:
        fs = base.fs
    except Exception as exc:
        raise RuntimeError(_missing_backend_hint(root, exc)) from exc

    start = time.monotonic()
    try:
        found = fs.find(str(base), withdirs=False)
    except ImportError as exc:
        # fsspec raises ImportError when the backend package is absent
        # (s3fs / gcsfs / adlfs). Say which one, rather than a bare traceback.
        raise RuntimeError(_missing_backend_hint(root, exc)) from exc

    protocol = base.protocol or ""
    files: list[Any] = []
    truncated = False
    for key in sorted(found):
        if len(files) >= max_paths or time.monotonic() - start > max_seconds:
            truncated = True
            break
        # `fs.find` returns bare keys (`bucket/prefix/file`); re-attach the
        # protocol so `.relative_to(root)` and `.suffix` behave.
        files.append(UPath(f"{protocol}://{key}") if protocol else UPath(key))
    return files, truncated


def _missing_backend_hint(root: Any, exc: Exception) -> str:
    scheme = str(root).split("://", 1)[0].lower()
    package = {
        "s3": "s3fs", "s3a": "s3fs",
        "gs": "gcsfs", "gcs": "gcsfs",
        "abfs": "adlfs", "abfss": "adlfs", "az": "adlfs",
    }.get(scheme)
    if package:
        return (
            f"reading {scheme}:// needs the `{package}` package "
            f"(pip install '.[storage]'). Original error: {exc}"
        )
    return f"no fsspec backend available for {scheme}://. Original error: {exc}"


def _raw_roots_from_local_config(data: dict[str, Any]) -> Iterable[str]:
    """Configured roots as WRITTEN, before local-vs-URI classification.

    Deliberately does not resolve: `Path("s3://b/k").resolve()` collapses the
    double slash and produces a nonsense local path. `_split_roots` decides
    which ones are paths and resolves only those.
    """
    roots = data.get("external_roots") or data.get("roots") or []
    if not isinstance(roots, list):
        return []
    out: list[str] = []
    for item in roots:
        if isinstance(item, str):
            out.append(item)
        elif isinstance(item, dict):
            value = item.get("path") or item.get("uri") or item.get("url")
            if value:
                out.append(str(value))
    return out


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
