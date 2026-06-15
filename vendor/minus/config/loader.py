"""Load, validate, and cross-check YAML config.

The loader does two jobs:

1. Parse YAML into the Pydantic models (structural validation).
2. Run *semantic* checks across files — measure/table/field references must
   resolve. These produce human-readable errors so a CLI agent can fix its own
   YAML on the next pass.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Iterable

import yaml
from pydantic import ValidationError

from minus.config.models import Dashboard, Project


class ConfigError(Exception):
    """Raised for any invalid or inconsistent configuration."""


# ---------------------------------------------------------------------------
# Low-level YAML
# ---------------------------------------------------------------------------


def _read_yaml(path: Path) -> dict:
    if not path.exists():
        raise ConfigError(f"config file not found: {path}")
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except yaml.YAMLError as exc:  # pragma: no cover - passthrough
        raise ConfigError(f"{path}: invalid YAML — {exc}") from exc
    if not isinstance(data, dict):
        raise ConfigError(f"{path}: top-level YAML must be a mapping")
    return data


def _fmt_validation(path: Path, exc: ValidationError) -> ConfigError:
    lines = [f"{path}: {len(exc.errors())} config error(s):"]
    for err in exc.errors():
        loc = ".".join(str(p) for p in err["loc"])
        lines.append(f"  - {loc or '<root>'}: {err['msg']}")
    return ConfigError("\n".join(lines))


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def load_project(root: Path | str, *, validate_refs: bool = True) -> Project:
    """Load ``<root>/project.yaml`` into a :class:`Project`."""
    root = Path(root)
    path = root / "project.yaml"
    data = _read_yaml(path)
    try:
        project = Project.model_validate(data)
    except ValidationError as exc:
        raise _fmt_validation(path, exc) from exc
    if validate_refs:
        _check_project(project)
    return project


def load_dashboards(root: Path | str, project: Project) -> list[Dashboard]:
    """Load every ``*.yaml`` page under the project's dashboards dir."""
    root = Path(root)
    folder = root / project.dashboards_dir
    if not folder.exists():
        return []
    pages: list[Dashboard] = []
    seen: set[str] = set()
    for fp in sorted(folder.glob("*.y*ml")):
        page = load_dashboard(fp, project)
        if page.id in seen:
            raise ConfigError(f"duplicate dashboard id {page.id!r} ({fp})")
        seen.add(page.id)
        pages.append(page)
    pages.sort(key=lambda p: (p.order, p.title))
    return pages


def load_dashboard(path: Path | str, project: Project) -> Dashboard:
    """Load and validate a single page YAML."""
    path = Path(path)
    data = _read_yaml(path)
    try:
        page = Dashboard.model_validate(data)
    except ValidationError as exc:
        raise _fmt_validation(path, exc) from exc
    _check_dashboard(page, project, source=path)
    return page


def export_schema(out_path: Path | str | None = None) -> dict:
    """Export the combined JSON Schema (the agent's contract).

    Returns the schema dict; also writes it if ``out_path`` is given.
    """
    schema = {
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        "title": "MinusAnalyst configuration",
        "description": "project.yaml uses 'Project'; each dashboards/*.yaml uses 'Dashboard'.",
        "$defs": {
            "Project": Project.model_json_schema(),
            "Dashboard": Dashboard.model_json_schema(),
        },
    }
    if out_path is not None:
        out_path = Path(out_path)
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_text(json.dumps(schema, indent=2), encoding="utf-8")
    return schema


# ---------------------------------------------------------------------------
# Semantic checks
# ---------------------------------------------------------------------------


def _check_project(p: Project) -> None:
    errs: list[str] = []
    ds_names = {d.name for d in p.datasources}
    tbl_names = {t.name for t in p.tables}

    for t in p.tables:
        if t.source not in ds_names:
            errs.append(f"table {t.name!r} references unknown datasource {t.source!r}")
    for r in p.relationships:
        for side, tbl in (("from_table", r.from_table), ("to_table", r.to_table)):
            if tbl not in tbl_names:
                errs.append(f"relationship.{side} references unknown table {tbl!r}")

    # measure references
    m_names = {m.name for m in p.measures}
    for m in p.measures:
        if m.kind in (None,) and not m.agg:
            errs.append(f"measure {m.name!r} needs an 'agg' (or set 'kind' for derived)")
        if m.agg == "count" and not m.field and not m.table:
            errs.append(f"count measure {m.name!r} needs a 'table' (or a 'field')")
        if m.field:
            errs.extend(_check_field(m.field, tbl_names, f"measure {m.name!r}.field"))
        if m.table and m.table not in tbl_names:
            errs.append(f"measure {m.name!r}.table references unknown table {m.table!r}")
        for ref_attr in ("numerator", "denominator", "of"):
            ref = getattr(m, ref_attr)
            if ref and ref not in m_names:
                errs.append(f"measure {m.name!r}.{ref_attr} -> unknown measure {ref!r}")
        if m.date_field:
            errs.extend(_check_field(m.date_field, tbl_names, f"measure {m.name!r}.date_field"))

    if errs:
        raise ConfigError("project.yaml semantic errors:\n  - " + "\n  - ".join(errs))


def _check_dashboard(d: Dashboard, p: Project, source: Path) -> None:
    errs: list[str] = []
    tbl_names = {t.name for t in p.tables}
    m_names = {m.name for m in p.measures}
    page_ids = None  # cross-page drilldown checked at app build time

    for f in d.filters:
        errs.extend(_check_field(f.field, tbl_names, f"filter {f.id!r}.field"))

    wid_seen: set[str] = set()
    for w in d.widgets:
        if w.id in wid_seen:
            errs.append(f"duplicate widget id {w.id!r}")
        wid_seen.add(w.id)

        used_measures = ([w.measure] if w.measure else []) + list(w.measures)
        if w.type != "table" and not used_measures:
            errs.append(f"widget {w.id!r} ({w.type}) needs a 'measure' or 'measures'")
        for mref in used_measures:
            if mref not in m_names:
                errs.append(f"widget {w.id!r} -> unknown measure {mref!r}")
        for fld_attr in ("dimension", "breakdown", "compare"):
            fld = getattr(w, fld_attr)
            if fld:
                errs.extend(_check_field(fld, tbl_names, f"widget {w.id!r}.{fld_attr}"))
        if w.type not in ("kpi", "table") and not w.dimension:
            errs.append(f"widget {w.id!r} ({w.type}) needs a 'dimension'")

    if errs:
        raise ConfigError(f"{source} semantic errors:\n  - " + "\n  - ".join(errs))


def _check_field(ref: str, tables: Iterable[str], where: str) -> list[str]:
    if "." not in ref:
        return [f"{where}: {ref!r} must be 'table.column'"]
    tbl = ref.split(".", 1)[0]
    if tbl not in set(tables):
        return [f"{where}: unknown table {tbl!r} in {ref!r}"]
    return []
