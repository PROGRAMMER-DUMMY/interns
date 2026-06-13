from __future__ import annotations

import re
from pathlib import Path
from typing import Any, Optional


def dataset_name_key(dataset: dict[str, Any]) -> str:
    path = dataset.get("path", "")
    return Path(path).stem if path else "*"


def logical_entity_from_path(path: str) -> str:
    """Derive a logical entity name from a dataset path. Workspace-agnostic.

    Strips trailing `_data` (a common generic suffix), then takes the first
    underscore-delimited segment as the entity (dropping any trailing
    source-system qualifiers), then singularizes a trailing `s` plural
    marker. Workspace-agnostic: no domain-specific tokens are referenced.
    """
    stem = Path(path).stem.lower()
    stem = re.sub(r"_data$", "", stem)
    entity = stem.split("_", 1)[0] if "_" in stem else stem
    entity = entity[:-1] if entity.endswith("s") else entity
    return entity or "entity"


def source_system_from_path(path: str) -> str:
    """Derive a source-system identifier from a dataset path. Workspace-agnostic.

    Uses the parent directory name as the source system. For paths like
    `workspaces/<ws>/datasets/<source_system>/<table>.csv` this returns
    `<source_system>`. When the parent name is hyphen-delimited, the last
    two hyphen segments are joined by `_` (dropping vendor/prefix noise);
    if it has fewer than two segments, the whole name is normalized.
    Falls back to `default` for shallow paths. Workspace-agnostic.
    """
    parts = Path(path).parts
    if len(parts) >= 2:
        parent = parts[-2].lower().replace(" ", "_")
        segments = [seg for seg in parent.split("-") if seg]
        if len(segments) >= 2:
            return "_".join(segments[-2:])
        return parent.replace("-", "_")
    return "default"


def detect_natural_key(schema: dict[str, Any], logical: str) -> list[str]:
    candidates = [
        f"{logical}id", f"{logical}_id", f"{logical}Id",
        "ID", "Id", "id",
    ]
    for column in schema.keys():
        lowered = column.lower()
        if lowered.endswith("id") and logical in lowered:
            return [column]
    for candidate in candidates:
        if candidate in schema:
            return [candidate]
    return []


def detect_watermark(schema: dict[str, Any]) -> Optional[str]:
    for column in schema.keys():
        lowered = column.lower()
        if lowered in {"modifieddate", "modified_at", "updateddate", "updated_at"}:
            return column
    for column in schema.keys():
        lowered = column.lower()
        if lowered in {"insertdate", "inserted_at", "createddate", "created_at"}:
            return column
    return None


def source_system_from_silver_source(source: str) -> str:
    if "__" in source:
        return source.split("__", 1)[1]
    return "default"


def safe_relative_posix(path: Path, root: Path) -> str:
    try:
        return path.resolve().relative_to(root.resolve()).as_posix()
    except ValueError:
        return path.as_posix()
