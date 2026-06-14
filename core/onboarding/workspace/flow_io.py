"""Small JSON IO helper shared by the workspace-flow modules.

Extracted from flow.py so flow_panels.py can reuse it without importing the large
flow module (which would create an import cycle). flow.py re-imports _read_json
from here, so existing flow._read_json call sites are unchanged.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any


def _read_json(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return {}
    return data if isinstance(data, dict) else {}
