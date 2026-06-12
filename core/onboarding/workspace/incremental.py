"""Incremental onboarding manifest (Phase 3 track A).

Fingerprints every onboarding input (datasets, KPI registries, data-model
files, durable decision stores) so a re-onboarding run can skip work:

- unchanged dataset  -> reuse its existing ``*.profile.json`` + index summary;
- changed/new dataset -> re-profile only that dataset;
- deleted dataset     -> drop its profile artifact and flag downstream
  contracts as potentially stale;
- nothing changed     -> skip the whole run and replay the recorded result.

The manifest is machine-only JSON under ``interns/state/`` with a paired
human-readable ``.md`` summary (repo convention). Fingerprints are
``size + mtime_ns + sha256``; the hash is the source of truth (a ``git
checkout`` that restores content but bumps mtime stays "unchanged"), while
``size + mtime_ns`` is a shortcut that lets a re-run reuse the previously
computed hash without re-reading the file.

Workspace-agnostic: only file paths, sizes, and bytes are inspected.
Determinism: every mapping/list emitted here is sorted; no timestamps are
written, so identical inputs produce a byte-identical manifest.
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

MANIFEST_VERSION = 1
MANIFEST_NAME = "onboarding_manifest.json"
MANIFEST_MD_NAME = "onboarding_manifest.md"

# Files at or below this size are hashed in full; larger files get a cheap
# partial hash (head + tail chunks + size) so fingerprinting never costs a
# full read of a multi-GB dataset.
FULL_HASH_LIMIT_BYTES = 64 * 1024 * 1024
_PARTIAL_CHUNK_BYTES = 1024 * 1024
_READ_CHUNK_BYTES = 1024 * 1024

# Input categories carried in the manifest. ``data_files`` drive per-dataset
# skip/redo decisions; the others gate aggregate-contract rebuilds (KPI
# registry merge, lexicon, domain model) which must rerun when ANY input
# changed but not when nothing changed.
INPUT_CATEGORIES = ("data_files", "registry_files", "model_files", "decision_files")


def fingerprint_file(path: Path) -> dict[str, Any]:
    """``{size, mtime_ns, sha256}`` for one existing file."""
    stat = path.stat()
    return {
        "size": stat.st_size,
        "mtime_ns": stat.st_mtime_ns,
        "sha256": _content_hash(path, stat.st_size),
    }


def _content_hash(path: Path, size: int) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        if size <= FULL_HASH_LIMIT_BYTES:
            while True:
                chunk = handle.read(_READ_CHUNK_BYTES)
                if not chunk:
                    break
                digest.update(chunk)
        else:
            # Cheap partial hash for very large files: head + tail + size.
            digest.update(handle.read(_PARTIAL_CHUNK_BYTES))
            handle.seek(max(0, size - _PARTIAL_CHUNK_BYTES))
            digest.update(handle.read(_PARTIAL_CHUNK_BYTES))
            digest.update(str(size).encode("ascii"))
            return "partial:" + digest.hexdigest()
    return digest.hexdigest()


def fingerprint_inputs(
    repo_root: Path,
    rel_paths: list[str],
    previous: dict[str, dict[str, Any]] | None = None,
) -> dict[str, dict[str, Any]]:
    """Fingerprint a list of repo-relative files, sorted by path.

    When a previous fingerprint exists with the same ``size`` and
    ``mtime_ns``, its stored hash is reused instead of re-reading the file
    (the cheap path for the common nothing-changed re-run). Missing files are
    silently skipped; the diff reports them as removed.
    """
    previous = previous or {}
    out: dict[str, dict[str, Any]] = {}
    for rel in sorted(set(rel_paths)):
        path = repo_root / rel
        if not path.exists() or not path.is_file():
            continue
        stat = path.stat()
        old = previous.get(rel)
        if (
            isinstance(old, dict)
            and old.get("size") == stat.st_size
            and old.get("mtime_ns") == stat.st_mtime_ns
            and old.get("sha256")
        ):
            out[rel] = {
                "size": stat.st_size,
                "mtime_ns": stat.st_mtime_ns,
                "sha256": old["sha256"],
            }
        else:
            out[rel] = fingerprint_file(path)
    return out


@dataclass(frozen=True)
class ChangeSet:
    """Skip/redo decision for one input category."""

    unchanged: list[str] = field(default_factory=list)
    changed: list[str] = field(default_factory=list)
    added: list[str] = field(default_factory=list)
    removed: list[str] = field(default_factory=list)

    @property
    def nothing_changed(self) -> bool:
        return not (self.changed or self.added or self.removed)


def diff_fingerprints(
    previous: dict[str, dict[str, Any]] | None,
    current: dict[str, dict[str, Any]],
) -> ChangeSet:
    """Compare fingerprints by content (size + sha256), ignoring mtime.

    A file whose mtime moved but whose bytes are identical (e.g. restored via
    ``git checkout``) is UNCHANGED. All result lists are sorted.
    """
    previous = previous or {}
    unchanged: list[str] = []
    changed: list[str] = []
    added: list[str] = []
    for rel in sorted(current):
        old = previous.get(rel)
        if not isinstance(old, dict):
            added.append(rel)
        elif (
            old.get("size") == current[rel].get("size")
            and old.get("sha256") == current[rel].get("sha256")
        ):
            unchanged.append(rel)
        else:
            changed.append(rel)
    removed = sorted(set(previous) - set(current))
    return ChangeSet(unchanged=unchanged, changed=changed, added=added, removed=removed)


def manifest_path(state_dir: Path) -> Path:
    return state_dir / MANIFEST_NAME


def manifest_markdown_path(state_dir: Path) -> Path:
    return state_dir / MANIFEST_MD_NAME


def load_manifest(state_dir: Path) -> dict[str, Any] | None:
    """Read the manifest if present and structurally valid, else ``None``."""
    path = manifest_path(state_dir)
    if not path.exists():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return None
    if not isinstance(data, dict):
        return None
    if data.get("artifact_type") != MANIFEST_NAME or data.get("version") != MANIFEST_VERSION:
        return None
    if not isinstance(data.get("inputs"), dict):
        return None
    return data


def build_manifest_payload(
    *,
    workspace: str,
    inputs: dict[str, dict[str, dict[str, Any]]],
    profiles: dict[str, str],
    result_summary: dict[str, Any],
) -> dict[str, Any]:
    """Assemble the manifest. Everything is sorted; no timestamps (UTC
    determinism: identical inputs => byte-identical manifest)."""
    return {
        "artifact_type": MANIFEST_NAME,
        "version": MANIFEST_VERSION,
        "generated_by": "onboard-workspace",
        "machine_only": True,
        "workspace": workspace,
        "inputs": {
            category: {rel: dict(sorted(fp.items())) for rel, fp in sorted(entries.items())}
            for category, entries in sorted(inputs.items())
        },
        "profiles": dict(sorted(profiles.items())),
        "result": result_summary,
    }


def write_manifest(state_dir: Path, payload: dict[str, Any]) -> str:
    """Write the machine-only JSON manifest plus its paired ``.md`` summary."""
    state_dir.mkdir(parents=True, exist_ok=True)
    path = manifest_path(state_dir)
    path.write_text(json.dumps(payload, indent=2, default=str, sort_keys=True) + "\n", encoding="utf-8")
    manifest_markdown_path(state_dir).write_text(render_manifest_markdown(payload), encoding="utf-8")
    return str(path)


def render_manifest_markdown(payload: dict[str, Any]) -> str:
    inputs = payload.get("inputs") or {}
    lines = [
        "# Onboarding Manifest",
        "",
        f"- **Workspace:** `{payload.get('workspace', '')}`",
        f"- **Manifest:** `{MANIFEST_NAME}` (machine-only JSON; this file is the paired summary)",
        "",
        "Input fingerprints (`size + mtime + sha256`) recorded by the last",
        "`onboard-workspace` run. Re-onboarding reuses profiles for datasets whose",
        "content hash is unchanged and re-profiles only changed/new datasets.",
        "Pass `--force` for a full re-run.",
        "",
        "| Input category | Files tracked |",
        "|---|---:|",
    ]
    for category in INPUT_CATEGORIES:
        lines.append(f"| {category} | {len(inputs.get(category) or {})} |")
    data_files = inputs.get("data_files") or {}
    if data_files:
        lines += [
            "",
            "## Datasets",
            "",
            "| Dataset | Size (bytes) | Content hash (first 12) |",
            "|---|---:|---|",
        ]
        for rel in sorted(data_files):
            fp = data_files[rel] or {}
            sha = str(fp.get("sha256") or "")
            lines.append(f"| `{rel}` | {fp.get('size', '?')} | `{sha[:12]}` |")
    lines += [""]
    return "\n".join(lines)


def artifacts_exist(repo_root: Path, manifest: dict[str, Any]) -> bool:
    """Whether every artifact recorded by the last run is still on disk.

    Checks both the result's artifact map and each per-dataset profile path.
    Any missing file disables the skip fast-path so the run regenerates it.
    """
    result = manifest.get("result") or {}
    artifacts = result.get("artifacts") or {}
    for raw in artifacts.values():
        if not isinstance(raw, str) or not raw:
            continue
        path = Path(raw)
        if not path.is_absolute():
            path = repo_root / raw
        if not path.exists():
            return False
    for raw in (manifest.get("profiles") or {}).values():
        if not isinstance(raw, str) or not raw:
            continue
        path = Path(raw)
        if not path.is_absolute():
            path = repo_root / raw
        if not path.exists():
            return False
    return True
