"""Dashboard spec contract: machine_defaults + user_overrides.

Two-section JSON per KPI. The renderer merges `user_overrides` on top of
`machine_defaults` at render time. Regeneration rewrites `machine_defaults`
in place but never touches `user_overrides`. This is the same preservation
pattern used by the wiki module.
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from core.dashboard.inference import infer_chart
from core.onboarding.kpi.registry_loader import load_kpi_definitions
from core.storage.workspace_layout import WorkspaceLayout


SPEC_VERSION = 1


@dataclass(frozen=True)
class DashboardSpec:
    """In-memory spec after `machine_defaults` ⊕ `user_overrides`."""

    kpi_id: str
    config: dict[str, Any]
    machine_defaults: dict[str, Any]
    user_overrides: dict[str, Any]
    spec_path: str

    def summary(self) -> dict[str, Any]:
        return {
            "kpi_id": self.kpi_id,
            "config": self.config,
            "machine_defaults": self.machine_defaults,
            "user_overrides": self.user_overrides,
            "spec_path": self.spec_path,
        }


def _dashboard_dir(layout: WorkspaceLayout) -> Path:
    return layout.project_root / "dashboard"


def _exports_dir(layout: WorkspaceLayout) -> Path:
    return _dashboard_dir(layout) / "exports"


def _spec_path(layout: WorkspaceLayout, kpi_id: str) -> Path:
    return _dashboard_dir(layout) / f"{kpi_id}.json"


def _index_path(layout: WorkspaceLayout) -> Path:
    return _dashboard_dir(layout) / "index.json"


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def merge_spec(machine_defaults: dict[str, Any], user_overrides: dict[str, Any]) -> dict[str, Any]:
    """Shallow merge: user_overrides win at the top level.

    Intentionally shallow — nested overrides would force the user to know the
    full default for any key they want to change. If a future field needs
    deep merge semantics, opt-in via a `_deep_merge: true` flag rather than
    changing the default.
    """
    merged = dict(machine_defaults or {})
    merged.update(user_overrides or {})
    return merged


def build_kpi_spec(
    *,
    kpi_id: str,
    definition: dict[str, Any],
    sql_path: str = "",
    result_columns: list[str] | None = None,
) -> dict[str, Any]:
    """Build the per-KPI dashboard spec payload (full file content)."""
    machine_defaults = infer_chart(definition=definition, result_columns=result_columns)
    machine_defaults["sql_path"] = sql_path
    machine_defaults["definition"] = {
        "business_question": definition.get("business_question") or definition.get("name") or "",
        "description": definition.get("description") or "",
        "metric": definition.get("metric") or "",
        "cuts": definition.get("cuts") or "",
    }
    return {
        "artifact_type": "dashboard_spec",
        "version": SPEC_VERSION,
        "kpi_id": kpi_id,
        "updated_at": _now(),
        "machine_defaults": machine_defaults,
        "user_overrides": {},
    }


def save_kpi_spec(layout: WorkspaceLayout, kpi_id: str, spec: dict[str, Any]) -> Path:
    """Write spec to disk, preserving any existing `user_overrides`."""
    path = _spec_path(layout, kpi_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    user_overrides: dict[str, Any] = {}
    if path.exists():
        try:
            existing = json.loads(path.read_text(encoding="utf-8"))
            if isinstance(existing, dict):
                user_overrides = dict(existing.get("user_overrides") or {})
        except (json.JSONDecodeError, OSError):
            user_overrides = {}
    payload = dict(spec)
    payload["user_overrides"] = user_overrides
    payload["updated_at"] = _now()
    path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    return path


def load_kpi_spec(layout: WorkspaceLayout, kpi_id: str) -> DashboardSpec | None:
    """Read spec from disk and return the merged config + raw sections."""
    path = _spec_path(layout, kpi_id)
    if not path.exists():
        return None
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return None
    if not isinstance(payload, dict):
        return None
    machine_defaults = dict(payload.get("machine_defaults") or {})
    user_overrides = dict(payload.get("user_overrides") or {})
    merged = merge_spec(machine_defaults, user_overrides)
    rel = path.relative_to(layout.project_root).as_posix() if path.is_relative_to(layout.project_root) else path.as_posix()
    return DashboardSpec(
        kpi_id=kpi_id,
        config=merged,
        machine_defaults=machine_defaults,
        user_overrides=user_overrides,
        spec_path=rel,
    )


def _build_index(layout: WorkspaceLayout, kpi_specs: list[dict[str, Any]]) -> dict[str, Any]:
    return {
        "artifact_type": "dashboard_index",
        "version": SPEC_VERSION,
        "workspace": layout.project_root.name,
        "updated_at": _now(),
        "kpis": [
            {
                "kpi_id": spec.get("kpi_id"),
                "title": (spec.get("machine_defaults") or {}).get("title") or spec.get("kpi_id"),
                "spec_path": f"dashboard/{spec.get('kpi_id')}.json",
            }
            for spec in kpi_specs
        ],
    }


def _save_index(layout: WorkspaceLayout, index_payload: dict[str, Any]) -> Path:
    path = _index_path(layout)
    path.parent.mkdir(parents=True, exist_ok=True)
    user_overrides: dict[str, Any] = {}
    if path.exists():
        try:
            existing = json.loads(path.read_text(encoding="utf-8"))
            if isinstance(existing, dict):
                user_overrides = dict(existing.get("user_overrides") or {})
        except (json.JSONDecodeError, OSError):
            user_overrides = {}
    payload = {
        "artifact_type": index_payload["artifact_type"],
        "version": index_payload["version"],
        "workspace": index_payload["workspace"],
        "updated_at": index_payload["updated_at"],
        "machine_defaults": {"kpis": index_payload["kpis"]},
        "user_overrides": user_overrides,
    }
    path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    return path


def refresh_workspace_dashboard(
    layout: WorkspaceLayout,
    *,
    completed_kpi_entries: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Refresh every KPI's machine_defaults from the registry; preserve user_overrides.

    `completed_kpi_entries` is the same per-KPI list `_write_result_preview`
    produces (with `definition` + `sql_path` + maybe `preview_markdown`).
    If omitted, falls back to the KPI registry only — useful before any
    workflow run.
    """
    definitions = load_kpi_definitions(layout)
    entries_by_id = {
        str(entry.get("kpi_id")): entry
        for entry in (completed_kpi_entries or [])
        if isinstance(entry, dict)
    }
    solutions_index: dict[str, str] = {}
    if layout.solutions_dir.exists():
        for sql_file in layout.solutions_dir.glob("*.sql"):
            if sql_file.name == "kpi_metrics.sql":
                continue
            try:
                rel = sql_file.resolve().relative_to(layout.project_root.parents[1]).as_posix()
            except ValueError:
                rel = sql_file.as_posix()
            solutions_index[sql_file.stem] = rel
    written_specs: list[dict[str, Any]] = []
    paths: list[str] = []
    for kpi_id, definition in sorted(definitions.items()):
        completed = entries_by_id.get(kpi_id) or {}
        sql_path = str(completed.get("sql_path") or solutions_index.get(kpi_id) or "")
        result_columns = []
        preview_markdown = str(completed.get("preview_markdown") or "")
        if preview_markdown:
            first_line = preview_markdown.splitlines()[0].strip() if preview_markdown.splitlines() else ""
            if first_line.startswith("|"):
                result_columns = [
                    cell.strip()
                    for cell in first_line.strip("|").split("|")
                    if cell.strip()
                ]
        payload = build_kpi_spec(
            kpi_id=kpi_id,
            definition=definition,
            sql_path=sql_path,
            result_columns=result_columns,
        )
        written = save_kpi_spec(layout, kpi_id, payload)
        written_specs.append(payload)
        try:
            paths.append(written.relative_to(layout.project_root).as_posix())
        except ValueError:
            paths.append(str(written))
    index_payload = _build_index(layout, written_specs)
    index_path = _save_index(layout, index_payload)
    try:
        index_rel = index_path.relative_to(layout.project_root).as_posix()
    except ValueError:
        index_rel = str(index_path)
    _exports_dir(layout).mkdir(parents=True, exist_ok=True)
    return {
        "workspace": layout.project_root.name,
        "kpi_count": len(written_specs),
        "spec_paths": paths,
        "index_path": index_rel,
    }


__all__ = [
    "DashboardSpec",
    "build_kpi_spec",
    "load_kpi_spec",
    "merge_spec",
    "refresh_workspace_dashboard",
    "save_kpi_spec",
]
