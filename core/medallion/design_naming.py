from __future__ import annotations

import re
from pathlib import Path
from typing import Any, Optional


def dataset_name_key(dataset: dict[str, Any]) -> str:
    path = dataset.get("path", "")
    return Path(path).stem if path else "*"


def logical_entity_from_path(path: str) -> str:
    stem = Path(path).stem.lower()
    stem = re.sub(r"(_hospital_[a-z0-9]+|hospital\d+_|_data)$", "", stem)
    stem = re.sub(r"^hospital\d+_", "", stem)
    stem = stem.rstrip("s") if stem.endswith("s") else stem
    return stem or "entity"


def source_system_from_path(path: str) -> str:
    posix_path = Path(path).as_posix().lower()
    match = re.search(r"(hospital[-_ ]?[a-z0-9]+)", posix_path)
    if match:
        return match.group(1).replace(" ", "_").replace("-", "_")
    parts = Path(path).parts
    if len(parts) >= 2:
        return parts[-2].lower().replace("-", "_").replace(" ", "_")
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
