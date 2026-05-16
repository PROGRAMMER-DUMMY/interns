from __future__ import annotations

import re
from typing import Any, Optional


def human_size(n_bytes: int) -> str:
    if not n_bytes:
        return "Unknown Size"
    for unit in ["B", "KB", "MB", "GB", "TB", "PB"]:
        if n_bytes < 1024:
            return f"{n_bytes:.2f} {unit}"
        n_bytes /= 1024
    return f"{n_bytes:.2f} EB"


def pct_label(pct: float) -> str:
    return f"{pct:g}".replace(".", "p").replace("-", "neg")


def is_numeric_dtype(dtype: Any) -> bool:
    return bool(getattr(dtype, "is_numeric", lambda: False)())


def safe_filename_part(value: str) -> str:
    return re.sub(r"[^A-Za-z0-9_.-]+", "_", value).strip("_") or "column"


def parse_column_list(value: Optional[str]) -> list[str]:
    if not value:
        return []
    return [item.strip() for item in value.split(",") if item.strip()]


def clamp_score(value: float) -> float:
    return round(max(0.0, min(100.0, value)), 3)


def mean_safe(values: list[float], default: float = 0.0) -> float:
    clean = [value for value in values if value is not None]
    return sum(clean) / len(clean) if clean else default


def unique_preserve_order(values: list[str]) -> list[str]:
    return list(dict.fromkeys([value for value in values if value]))
