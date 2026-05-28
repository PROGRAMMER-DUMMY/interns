"""On-disk cache for executed DuckDB sample-result previews used by the KPI panel.

Keyed on the SHA256 of the normalized SQL text plus each source CSV's modification
time, the cache lets repeated panel rebuilds skip redundant DuckDB execution.
Entries are JSON files under ``<workspace>/interns/state/panel_previews/``;
writes are atomic via ``os.replace`` and corrupt entries are treated as misses
so the next successful run can overwrite them. Dependency-free by design:
stdlib only, no imports from ``core.*``.
"""

from __future__ import annotations

import hashlib
import json
import os
import time
from pathlib import Path


_INTERNS_DIRNAME = "interns"
_STATE_DIRNAME = "state"
_CACHE_DIRNAME = "panel_previews"
_KEY_HEX_LEN = 32


def _cache_dir(workspace_path: Path) -> Path:
    return Path(workspace_path) / _INTERNS_DIRNAME / _STATE_DIRNAME / _CACHE_DIRNAME


def compute_preview_cache_key(sql: str, dataset_paths: list[Path]) -> str:
    """Return a 32-char hex key derived from SQL text and dataset (path, mtime_ns) pairs.

    Missing dataset files contribute ``mtime_ns=0`` so the key changes once the
    file appears on disk.
    """

    entries: list[str] = []
    for raw_path in dataset_paths:
        path = Path(raw_path)
        try:
            mtime_ns = path.stat().st_mtime_ns
        except (FileNotFoundError, OSError):
            mtime_ns = 0
        entries.append(f"{path}::{mtime_ns}")
    entries.sort()

    payload = sql.strip() + "\n" + "\n".join(entries)
    digest = hashlib.sha256(payload.encode("utf-8")).hexdigest()
    return digest[:_KEY_HEX_LEN]


def cache_path(workspace_path: Path, cache_key: str) -> Path:
    """Return the JSON file path for ``cache_key`` under the cache directory."""

    return _cache_dir(workspace_path) / f"{cache_key}.json"


def load_cached_preview(workspace_path: Path, cache_key: str) -> dict | None:
    """Return the cached payload dict or ``None`` when no usable entry exists.

    Corrupt cache files (JSON decode error, non-dict top level) return ``None``
    without raising; the file is left in place so the next ``save`` overwrites
    it atomically.
    """

    path = cache_path(workspace_path, cache_key)
    if not path.exists():
        return None
    try:
        with path.open("r", encoding="utf-8") as handle:
            data = json.load(handle)
    except (json.JSONDecodeError, OSError):
        return None
    if not isinstance(data, dict):
        return None
    return data


def save_cached_preview(
    workspace_path: Path,
    cache_key: str,
    payload: dict,
) -> Path:
    """Write ``payload`` atomically to the cache path and return the final path.

    The payload must be JSON-serializable; ``default=str`` is used as a
    resilience fallback for stray non-serializable types (e.g. ``Decimal``,
    ``datetime``).
    """

    final_path = cache_path(workspace_path, cache_key)
    final_path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = final_path.with_suffix(final_path.suffix + ".tmp")

    with tmp_path.open("w", encoding="utf-8") as handle:
        json.dump(payload, handle, indent=2, default=str)
    os.replace(tmp_path, final_path)
    return final_path


def evict_stale_entries(
    workspace_path: Path,
    *,
    max_age_seconds: float = 86_400.0,
    keep_at_most: int = 200,
) -> int:
    """Remove cache files older than ``max_age_seconds`` and cap directory size.

    Best-effort housekeeping: never raises. Returns the count of files removed.
    """

    removed = 0
    directory = _cache_dir(workspace_path)
    if not directory.exists():
        return 0

    try:
        entries = [entry for entry in directory.iterdir() if entry.is_file() and entry.suffix == ".json"]
    except OSError:
        return 0

    now = time.time()
    survivors: list[tuple[float, Path]] = []
    for entry in entries:
        try:
            mtime = entry.stat().st_mtime
        except OSError:
            continue
        if (now - mtime) > max_age_seconds:
            try:
                entry.unlink()
                removed += 1
            except OSError:
                pass
            continue
        survivors.append((mtime, entry))

    if len(survivors) > keep_at_most:
        survivors.sort(key=lambda item: item[0])  # oldest first
        excess = len(survivors) - keep_at_most
        for _mtime, entry in survivors[:excess]:
            try:
                entry.unlink()
                removed += 1
            except OSError:
                pass

    return removed


__all__ = [
    "cache_path",
    "compute_preview_cache_key",
    "evict_stale_entries",
    "load_cached_preview",
    "save_cached_preview",
]
