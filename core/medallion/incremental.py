"""
core/medallion/incremental.py — fingerprint-based incremental medallion refresh.

Records a content fingerprint per layer table in a refresh manifest stored next
to the layer data (interns/state/medallion/refresh_manifest.json + .md). On the
next build:

- Bronze tables are skipped when their source file + emitted SQL fingerprints
  are unchanged since the last successful ingest.
- Silver/Gold tables are skipped when the fingerprints of ALL their inputs
  (upstream table fingerprints + their own emitted SQL/assertions) are
  unchanged.
- ``--force`` rebuilds everything.

NOTE (post-merge unification): a sibling workstream is adding source
fingerprinting to onboarding/profiling. ``fingerprint_file`` /
``combine_fingerprints`` are intentionally small, dependency-free, and
workspace-agnostic so both call sites can later share one helper. Do not grow
this module beyond medallion refresh planning.
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Sequence

SCHEMA_VERSION = 1
MANIFEST_JSON = "refresh_manifest.json"
MANIFEST_MD = "refresh_manifest.md"

_LAYERS = ("bronze", "silver", "gold")

# Stable sentinel for dependencies that cannot be resolved to a known layer
# table. Constant on purpose: an unresolved name must not force a rebuild on
# every run, only a real fingerprint change should.
_UNRESOLVED = "unresolved"


# ── Fingerprint helpers (keep small + local; see module note) ──────────────────

def fingerprint_file(path: Path) -> str:
    """Content fingerprint of one file: ``sha256:<hex>``; ``missing`` if absent."""
    try:
        if not path.is_file():
            return "missing"
        return "sha256:" + hashlib.sha256(path.read_bytes()).hexdigest()
    except OSError:
        return "missing"


def combine_fingerprints(parts: Sequence[str]) -> str:
    """Order-stable combination of multiple fingerprints into one."""
    sha = hashlib.sha256()
    for part in parts:
        sha.update(str(part).encode("utf-8"))
        sha.update(b"\x00")
    return "sha256:" + sha.hexdigest()


# ── Refresh manifest (machine JSON + human MD) ─────────────────────────────────

@dataclass
class RefreshEntry:
    fingerprint: str
    inputs: dict[str, str] = field(default_factory=dict)
    built_at: str = ""
    run_id: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "fingerprint": self.fingerprint,
            "inputs": dict(self.inputs),
            "built_at": self.built_at,
            "run_id": self.run_id,
        }

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "RefreshEntry":
        return cls(
            fingerprint=str(d.get("fingerprint", "")),
            inputs={str(k): str(v) for k, v in (d.get("inputs") or {}).items()},
            built_at=str(d.get("built_at", "")),
            run_id=str(d.get("run_id", "")),
        )


@dataclass
class RefreshManifest:
    workspace: str = ""
    updated_at: str = ""
    schema_version: int = SCHEMA_VERSION
    layers: dict[str, dict[str, RefreshEntry]] = field(
        default_factory=lambda: {layer: {} for layer in _LAYERS}
    )

    def entry(self, layer: str, table: str) -> RefreshEntry | None:
        return self.layers.get(layer, {}).get(table)

    def record(self, layer: str, table: str, entry: RefreshEntry) -> None:
        self.layers.setdefault(layer, {})[table] = entry

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "workspace": self.workspace,
            "updated_at": self.updated_at,
            "layers": {
                layer: {name: e.to_dict() for name, e in sorted(tables.items())}
                for layer, tables in self.layers.items()
            },
        }

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "RefreshManifest":
        layers: dict[str, dict[str, RefreshEntry]] = {layer: {} for layer in _LAYERS}
        for layer, tables in (d.get("layers") or {}).items():
            if not isinstance(tables, dict):
                continue
            layers.setdefault(layer, {})
            for name, body in tables.items():
                if isinstance(body, dict):
                    layers[layer][name] = RefreshEntry.from_dict(body)
        return cls(
            workspace=str(d.get("workspace", "")),
            updated_at=str(d.get("updated_at", "")),
            schema_version=int(d.get("schema_version", SCHEMA_VERSION)),
            layers=layers,
        )

    @classmethod
    def load(cls, state_dir: Path, *, workspace: str = "") -> "RefreshManifest":
        path = state_dir / MANIFEST_JSON
        if not path.exists():
            return cls(workspace=workspace)
        try:
            manifest = cls.from_dict(json.loads(path.read_text(encoding="utf-8")))
        except (json.JSONDecodeError, OSError):
            return cls(workspace=workspace)
        if workspace:
            manifest.workspace = workspace
        return manifest

    def save(self, state_dir: Path) -> tuple[Path, Path]:
        state_dir.mkdir(parents=True, exist_ok=True)
        self.updated_at = datetime.now(timezone.utc).isoformat()
        json_path = state_dir / MANIFEST_JSON
        md_path = state_dir / MANIFEST_MD
        json_path.write_text(
            json.dumps(self.to_dict(), indent=2) + "\n", encoding="utf-8"
        )
        md_path.write_text(self.render_md(), encoding="utf-8")
        return json_path, md_path

    def render_md(self) -> str:
        lines = [
            "# Medallion Refresh Manifest",
            "",
            f"- Workspace: `{self.workspace}`",
            f"- Updated: {self.updated_at}",
            "",
            "Fingerprints of the last successful build per table. A table is",
            "rebuilt only when its fingerprint (source + SQL + upstream inputs)",
            "changes; pass `--force` to rebuild everything.",
            "",
        ]
        for layer in _LAYERS:
            tables = self.layers.get(layer, {})
            lines.append(f"## {layer.capitalize()} ({len(tables)} tables)")
            lines.append("")
            if not tables:
                lines.append("- (none built yet)")
                lines.append("")
                continue
            for name in sorted(tables):
                e = tables[name]
                short = e.fingerprint.split(":")[-1][:12]
                lines.append(
                    f"- `{layer}.{name}` fp=`{short}` built_at={e.built_at or '?'} "
                    f"run={e.run_id or '?'}"
                )
            lines.append("")
        return "\n".join(lines).rstrip() + "\n"


# ── Incremental plan ───────────────────────────────────────────────────────────

@dataclass
class TableDecision:
    key: str            # e.g. "bronze.patients"
    action: str         # "build" | "skip"
    reason: str
    fingerprint: str    # current computed fingerprint
    inputs: dict[str, str] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "key": self.key,
            "action": self.action,
            "reason": self.reason,
            "fingerprint": self.fingerprint,
        }


@dataclass
class IncrementalPlan:
    force: bool = False
    decisions: dict[str, TableDecision] = field(default_factory=dict)
    kpi_action: str = "build"
    kpi_reason: str = ""

    def decision(self, key: str) -> TableDecision | None:
        return self.decisions.get(key)

    def should_skip(self, key: str) -> bool:
        d = self.decisions.get(key)
        return bool(d and d.action == "skip")

    def skipped_keys(self) -> list[str]:
        return sorted(k for k, d in self.decisions.items() if d.action == "skip")

    def summary(self) -> dict[str, Any]:
        return {
            "force": self.force,
            "build_count": sum(1 for d in self.decisions.values() if d.action == "build"),
            "skip_count": sum(1 for d in self.decisions.values() if d.action == "skip"),
            "skipped": self.skipped_keys(),
            "kpi_action": self.kpi_action,
        }


def plan_incremental(
    manifest,
    paths: dict[str, Path],
    repo_root: Path,
    refresh: RefreshManifest,
    *,
    force: bool = False,
) -> IncrementalPlan:
    """Decide build/skip per table from fingerprints.

    ``manifest`` is a core.medallion.manifest.Manifest. Bronze fingerprints
    cover the source file + emitted SQL; Silver/Gold fingerprints cover their
    emitted SQL (+ assertions) and the CURRENT fingerprints of every upstream
    table, so a change anywhere upstream cascades downstream deterministically.
    """
    plan = IncrementalPlan(force=force)
    current_fp: dict[str, str] = {}  # "layer.table" -> current fingerprint

    # Bronze: source file + emitted SQL
    for b in manifest.bronze:
        source = Path(b.source_file)
        if not source.is_absolute():
            source = repo_root / b.source_file
        inputs = {
            f"source:{b.source_file}": fingerprint_file(source),
            "sql": fingerprint_file(paths["bronze"] / f"{b.name}.duckdb.sql"),
        }
        _decide(plan, refresh, current_fp, "bronze", b.name, inputs, force=force)

    # Silver: upstream bronze fingerprints + emitted SQL + assertions
    for s in manifest.silver:
        inputs = {
            "sql": fingerprint_file(paths["silver"] / f"{s.name}.duckdb.sql"),
            "assertions": fingerprint_file(paths["silver"] / f"_{s.name}_assertions.sql"),
        }
        for dep in s.derived_from:
            inputs[f"upstream:{dep}"] = _resolve_upstream(dep, current_fp)
        _decide(plan, refresh, current_fp, "silver", s.name, inputs, force=force)

    # Gold: upstream silver/gold fingerprints + emitted SQL
    for g in manifest.gold:
        inputs = {
            "sql": fingerprint_file(paths["gold"] / f"{g.name}.duckdb.sql"),
        }
        for dep in g.derived_from:
            inputs[f"upstream:{dep}"] = _resolve_upstream(dep, current_fp)
        _decide(plan, refresh, current_fp, "gold", g.name, inputs, force=force)

    gold_builds = [
        d for k, d in plan.decisions.items()
        if k.startswith("gold.") and d.action == "build"
    ]
    if force:
        plan.kpi_action, plan.kpi_reason = "build", "forced"
    elif gold_builds:
        plan.kpi_action, plan.kpi_reason = "build", "gold_inputs_changed"
    else:
        plan.kpi_action, plan.kpi_reason = "skip", "gold_unchanged"
    return plan


def _resolve_upstream(dep: str, current_fp: dict[str, str]) -> str:
    """Resolve a derived_from name to a current fingerprint.

    Accepts fully qualified ("bronze.patients") or bare ("patients") names.
    Unresolvable names map to a stable sentinel so they never thrash rebuilds.
    """
    if dep in current_fp:
        return current_fp[dep]
    bare = dep.split(".", 1)[-1]
    for layer in _LAYERS:
        candidate = f"{layer}.{bare}"
        if candidate in current_fp:
            return current_fp[candidate]
    return _UNRESOLVED


def _decide(
    plan: IncrementalPlan,
    refresh: RefreshManifest,
    current_fp: dict[str, str],
    layer: str,
    name: str,
    inputs: dict[str, str],
    *,
    force: bool,
) -> None:
    fp = combine_fingerprints([f"{k}={v}" for k, v in sorted(inputs.items())])
    key = f"{layer}.{name}"
    current_fp[key] = fp
    previous = refresh.entry(layer, name)
    if force:
        action, reason = "build", "forced"
    elif previous is None:
        action, reason = "build", "no_prior_build"
    elif previous.fingerprint != fp:
        action, reason = "build", "fingerprint_changed"
    else:
        action, reason = "skip", "fingerprint_unchanged"
    plan.decisions[key] = TableDecision(
        key=key, action=action, reason=reason, fingerprint=fp, inputs=inputs
    )


def record_built(
    refresh: RefreshManifest,
    decision: TableDecision,
    *,
    run_id: str,
) -> None:
    """Record a successful table build into the refresh manifest."""
    layer, _, name = decision.key.partition(".")
    refresh.record(
        layer,
        name,
        RefreshEntry(
            fingerprint=decision.fingerprint,
            inputs=dict(decision.inputs),
            built_at=datetime.now(timezone.utc).isoformat(),
            run_id=run_id,
        ),
    )
