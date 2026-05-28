"""Workspace artifact inventory.

Single generic source of truth for "what artifacts does any workspace
produce, where do they live, what command produces them, and are they
fresh?". Used by `workspace-flow artifacts` to give the orchestrating
agent a complete map without grepping the filesystem.

Workspace-agnostic by design: uses WorkspaceLayout for every path, never
hardcodes a workspace name, dataset name, or domain word. The artifact
catalog below is the contract — any new platform-level artifact should
be registered here so it shows up in every workspace's inventory.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from core.storage.workspace_layout import WorkspaceLayout


@dataclass(frozen=True)
class ArtifactSpec:
    """A registered artifact: where it lives + what produces it + why it exists.

    The ``lifecycle`` classification drives gitignore, GC, and "is this
    load-bearing?" decisions for the whole platform:

    - ``source_of_truth``: governed contract or accepted state that defines
      the workspace. Never auto-deleted. Must be tracked in version control.
    - ``derived``: rendered or projected from a source_of_truth artifact.
      Safe to delete; regeneratable. Gitignore by default.
    - ``cache``: short-lived intermediate result (panel previews, plan caches).
      GC after TTL.
    - ``log``: append-only event/trajectory streams. Rotated on size/age.
    - ``scaffold``: hand-authored per-workspace glue (evaluation harness,
      bootstrap scripts). Tracked in VC, never auto-touched.
    """

    key: str
    purpose: str
    relative_path: str
    producer_command: str
    category: str = "generated"
    kind: str = "file"
    lifecycle: str = "derived"


def _layout_artifacts(layout: WorkspaceLayout) -> list[ArtifactSpec]:
    """Catalog all generic platform-level workspace artifacts.

    The list is intentionally explicit — every artifact registered here
    becomes part of the platform's documented surface area. New artifacts
    must be added here to be discoverable by `workspace-flow artifacts`.
    """
    return [
        ArtifactSpec(
            key="kpi_registry",
            category="contract",
            lifecycle="source_of_truth",
            purpose="Canonical normalized KPI list — business questions, metrics, cuts.",
            relative_path=_rel(layout.kpi_registry_path, layout.project_root),
            producer_command="uv run onboard-workspace --workspace <workspace>",
        ),
        ArtifactSpec(
            key="kpi_feature_mapping",
            category="contract",
            lifecycle="source_of_truth",
            purpose="Per-KPI features resolved against profiled columns + workspace definitions.",
            relative_path=_rel(layout.kpi_feature_mapping_path, layout.project_root),
            producer_command="uv run resolve-kpi-features --workspace <workspace>",
        ),
        ArtifactSpec(
            key="relationship_contracts",
            category="contract",
            lifecycle="source_of_truth",
            purpose="Inferred + user-confirmed FK relationships gating multi-source SQL generation.",
            relative_path=_rel(layout.relationship_contracts_path, layout.project_root),
            producer_command="uv run build-relationship-contracts --workspace <workspace>",
        ),
        ArtifactSpec(
            key="data_model_contract",
            category="contract",
            lifecycle="source_of_truth",
            purpose="Finalized data-model entities, columns, and approved relationships.",
            relative_path=_rel(layout.data_model_contract_path, layout.project_root),
            producer_command="uv run finalize-data-model-generation --workspace <workspace>",
        ),
        ArtifactSpec(
            key="source_to_target_plan",
            category="contract",
            lifecycle="derived",
            purpose="Per-KPI plan: selected sources, joins, validation checks, blockers.",
            relative_path=_rel(layout.source_to_target_plan_path, layout.project_root),
            producer_command="uv run workspace-flow start --workspace <workspace> --intent full_kpi_sql",
        ),
        ArtifactSpec(
            key="workspace_feature_definitions",
            category="contract",
            lifecycle="source_of_truth",
            purpose="User-accepted workspace-level definitions reused across KPIs (canonical wiki-memory store).",
            relative_path=_rel(layout.contracts_dir / "workspace_feature_definitions.json", layout.project_root),
            producer_command="uv run apply-kpi-panel-answer --workspace <workspace>",
        ),
        ArtifactSpec(
            key="profile_index",
            category="profile",
            lifecycle="source_of_truth",
            purpose="Index of per-dataset column profiles (dtype, sample values, cardinality).",
            relative_path=_rel(layout.profile_index_path, layout.project_root),
            producer_command="uv run onboard-workspace --workspace <workspace>",
        ),
        ArtifactSpec(
            key="solutions_dir",
            category="generated",
            kind="dir",
            lifecycle="derived",
            purpose="Generated executable KPI SQL (one .sql per KPI) + bundled kpi_metrics.sql.",
            relative_path=_rel(layout.solutions_dir, layout.project_root),
            producer_command="uv run workspace-flow start --workspace <workspace> --intent full_kpi_sql",
        ),
        ArtifactSpec(
            key="kpi_results",
            category="report",
            lifecycle="derived",
            purpose="Per-KPI definition + generated SQL + first-N result rows (markdown).",
            relative_path=_rel(layout.reports_dir / "kpi_results" / "current.md", layout.project_root),
            producer_command="uv run workspace-flow results --session <session_id>",
        ),
        ArtifactSpec(
            key="blocker_question_panel",
            category="report",
            lifecycle="derived",
            purpose="Current open blocker question for KPI feature resolution (markdown + json).",
            relative_path=_rel(layout.reports_dir / "blocker_question_panel" / "current.md", layout.project_root),
            producer_command="uv run prepare-kpi-blocker-panel --workspace <workspace>",
        ),
        ArtifactSpec(
            key="source_to_target_plan_md",
            category="report",
            lifecycle="derived",
            purpose="Human-readable source-to-target plan (markdown view of the contract).",
            relative_path=_rel(layout.reports_dir / "source_to_target_plan.md", layout.project_root),
            producer_command="uv run workspace-flow start --workspace <workspace> --intent full_kpi_sql",
        ),
        ArtifactSpec(
            key="relationship_contracts_md",
            category="report",
            lifecycle="derived",
            purpose="Human-readable relationships report (markdown view of the contract).",
            relative_path=_rel(layout.reports_dir / "relationship_contracts.md", layout.project_root),
            producer_command="uv run build-relationship-contracts --workspace <workspace>",
        ),
        ArtifactSpec(
            key="kpi_proof_packet",
            category="evidence",
            lifecycle="derived",
            purpose="Evidence pack proving each KPI's mapping/resolution before SQL generation.",
            relative_path=_rel(layout.evidence_dir / "kpi_proof_packet" / "current.json", layout.project_root),
            producer_command="uv run kpi-proof-packet --workspace <workspace>",
        ),
        ArtifactSpec(
            key="kpi_results_evidence",
            category="evidence",
            lifecycle="derived",
            purpose="Structured per-KPI execution evidence (definition + sql_text + preview rows).",
            relative_path=_rel(layout.evidence_dir / "kpi_results" / "current.json", layout.project_root),
            producer_command="uv run workspace-flow results --session <session_id>",
        ),
        ArtifactSpec(
            key="workflow_sessions",
            category="state",
            kind="dir",
            lifecycle="cache",
            purpose="Per-session workflow state (session.json + current panel json/md). GC'd by workspace-flow gc.",
            relative_path=_rel(layout.workflow_sessions_dir, layout.project_root),
            producer_command="uv run workspace-flow start --workspace <workspace>",
        ),
        ArtifactSpec(
            key="handoffs",
            category="state",
            kind="dir",
            lifecycle="log",
            purpose="Compact handoff snapshots written when resuming open sessions.",
            relative_path=_rel(layout.handoffs_dir, layout.project_root),
            producer_command="(auto, on workspace-flow start resume)",
        ),
        ArtifactSpec(
            key="applied_ops",
            category="state",
            lifecycle="source_of_truth",
            purpose="Append-only log of applied-op records used for idempotency.",
            relative_path=_rel(layout.state_dir / "applied_ops.jsonl", layout.project_root),
            producer_command="(auto, on every apply-* command)",
        ),
        ArtifactSpec(
            key="panel_previews",
            category="state",
            kind="dir",
            lifecycle="cache",
            purpose="Per-panel content-hash cache. GC'd by workspace-flow gc.",
            relative_path=_rel(layout.state_dir / "panel_previews", layout.project_root),
            producer_command="(auto, on every panel render)",
        ),
        ArtifactSpec(
            key="trajectory_log",
            category="state",
            lifecycle="log",
            purpose="Canonical append-only trajectory event stream. Other trajectory artifacts are derived views.",
            relative_path=_rel(layout.state_dir / "trajectory.jsonl", layout.project_root),
            producer_command="(auto, on every tool call)",
        ),
        ArtifactSpec(
            key="events_log",
            category="state",
            lifecycle="log",
            purpose="Step-level events log (deprecated mirror of trajectory_log; kept for back-compat).",
            relative_path=_rel(layout.state_dir / "events.jsonl", layout.project_root),
            producer_command="(auto)",
        ),
        ArtifactSpec(
            key="delta_metadata",
            category="state",
            kind="dir",
            lifecycle="cache",
            purpose="Delta-table commit logs for the metadata store. Only used when workspace_settings.delta_enabled=true or AUTORESEARCH_METADATA_BACKEND=delta; safe to delete otherwise.",
            relative_path=_rel(layout.state_dir / "delta_metadata", layout.project_root),
            producer_command="(auto, only when Delta backend selected)",
        ),
        ArtifactSpec(
            key="workspace_definitions_memory",
            category="memory",
            kind="dir",
            lifecycle="derived",
            purpose="Workspace-memory scratch (wiki_memory_candidates etc.); canonical store is contracts/workspace_feature_definitions.json.",
            relative_path=_rel(layout.memory_dir, layout.project_root),
            producer_command="uv run apply-kpi-panel-answer --workspace <workspace>",
        ),
        ArtifactSpec(
            key="wiki_kpis",
            category="wiki",
            kind="dir",
            lifecycle="source_of_truth",
            purpose="Per-KPI human-readable wiki notes (machine + human sections preserved).",
            relative_path="wiki/kpis",
            producer_command="(auto, on workspace-flow complete + apply-kpi-panel-answer)",
        ),
        ArtifactSpec(
            key="wiki_features",
            category="wiki",
            kind="dir",
            lifecycle="source_of_truth",
            purpose="Per-feature wiki notes capturing decision rationale and evidence.",
            relative_path="wiki/features",
            producer_command="(auto, on apply-kpi-panel-answer)",
        ),
        ArtifactSpec(
            key="wiki_sources",
            category="wiki",
            kind="dir",
            lifecycle="source_of_truth",
            purpose="Per-dataset wiki notes capturing source-of-truth metadata.",
            relative_path="wiki/sources",
            producer_command="(auto, on onboard-workspace)",
        ),
        ArtifactSpec(
            key="evaluation_scaffolds",
            category="scaffold",
            kind="dir",
            lifecycle="scaffold",
            purpose="Per-workspace evaluation harness scaffolds (evaluator.py, experiment.py). Hand-edited platform glue, not auto-generated.",
            relative_path="interns/evaluation",
            producer_command="(hand-authored; created during workspace bootstrap)",
        ),
        ArtifactSpec(
            key="dashboard_index",
            category="dashboard",
            lifecycle="source_of_truth",
            purpose="Dashboard index spec — which KPIs appear, in what order, with which user_overrides.",
            relative_path="dashboard/index.json",
            producer_command="uv run workspace-dashboard --workspace <workspace> (auto on workspace-flow complete)",
        ),
        ArtifactSpec(
            key="dashboard_specs",
            category="dashboard",
            kind="dir",
            lifecycle="source_of_truth",
            purpose="Per-KPI dashboard JSON specs (machine_defaults + user_overrides). Edit user_overrides to customize charts; machine_defaults refresh on every regeneration.",
            relative_path="dashboard",
            producer_command="uv run workspace-dashboard --workspace <workspace>",
        ),
        ArtifactSpec(
            key="dashboard_exports",
            category="dashboard",
            kind="dir",
            lifecycle="derived",
            purpose="Static HTML export of the dashboard (one page per KPI + index.html). Regeneratable.",
            relative_path="dashboard/exports",
            producer_command="uv run workspace-dashboard --workspace <workspace> --export",
        ),
    ]


def _rel(path: Path, root: Path) -> str:
    try:
        return path.resolve().relative_to(root.resolve()).as_posix()
    except (ValueError, OSError):
        return path.as_posix()


def _mtime_iso(path: Path) -> str:
    try:
        ts = path.stat().st_mtime
    except OSError:
        return ""
    return datetime.fromtimestamp(ts, tz=timezone.utc).isoformat()


def _summarize_dir(path: Path) -> dict[str, Any]:
    try:
        entries = sorted(p for p in path.iterdir() if p.is_file())
    except OSError:
        return {"file_count": 0, "files": []}
    return {
        "file_count": len(entries),
        "files": [entry.name for entry in entries[:25]],
    }


def inventory(layout: WorkspaceLayout) -> dict[str, Any]:
    """Return the full artifact inventory for a workspace.

    Generic. Walks the registered catalog, reports existence + freshness +
    summary per artifact. No workspace-specific logic.
    """
    artifacts: list[dict[str, Any]] = []
    workspace_root = layout.project_root
    for spec in _layout_artifacts(layout):
        full_path = workspace_root / spec.relative_path
        exists = full_path.exists()
        item: dict[str, Any] = {
            "key": spec.key,
            "category": spec.category,
            "kind": spec.kind,
            "lifecycle": spec.lifecycle,
            "purpose": spec.purpose,
            "relative_path": spec.relative_path,
            "producer_command": spec.producer_command,
            "exists": exists,
            "updated_at": _mtime_iso(full_path) if exists else "",
        }
        if exists and spec.kind == "dir":
            item.update(_summarize_dir(full_path))
        elif exists and spec.kind == "file":
            try:
                item["size_bytes"] = full_path.stat().st_size
            except OSError:
                item["size_bytes"] = 0
        artifacts.append(item)
    categories: dict[str, int] = {}
    for item in artifacts:
        if item["exists"]:
            categories[item["category"]] = categories.get(item["category"], 0) + 1
    return {
        "workspace": workspace_root.name,
        "workspace_root": str(workspace_root),
        "artifact_count": len(artifacts),
        "present_count": sum(1 for a in artifacts if a["exists"]),
        "missing_count": sum(1 for a in artifacts if not a["exists"]),
        "presence_by_category": categories,
        "artifacts": artifacts,
    }


def render_inventory_markdown(inv: dict[str, Any]) -> str:
    """Render the inventory as a human-readable markdown table grouped by category."""
    lines = [
        "# Workspace Artifact Inventory",
        "",
        f"- Workspace: `{inv.get('workspace', '')}`",
        f"- Present: {inv.get('present_count', 0)} / {inv.get('artifact_count', 0)}",
        "",
        "Artifacts are platform-generic. Each row shows what the artifact is for,",
        "where it lives, its lifecycle classification (source_of_truth / derived / cache / log),",
        "and the exact command that (re)produces it.",
        "",
    ]
    by_cat: dict[str, list[dict[str, Any]]] = {}
    for item in inv.get("artifacts") or []:
        by_cat.setdefault(str(item.get("category") or "other"), []).append(item)
    for category in sorted(by_cat):
        lines.append(f"## {category}")
        lines.append("")
        lines.append("| Key | Path | Lifecycle | Present | Updated | Producer |")
        lines.append("| --- | --- | --- | --- | --- | --- |")
        for item in by_cat[category]:
            present = "yes" if item.get("exists") else "no"
            updated = item.get("updated_at", "") or "-"
            lines.append(
                "| `{key}` | `{path}` | `{lifecycle}` | {present} | {updated} | `{producer}` |".format(
                    key=item.get("key", ""),
                    path=item.get("relative_path", ""),
                    lifecycle=item.get("lifecycle", ""),
                    present=present,
                    updated=updated,
                    producer=item.get("producer_command", ""),
                )
            )
        lines.append("")
    return "\n".join(lines).rstrip() + "\n"


def gitignore_patterns(layout: WorkspaceLayout) -> list[str]:
    """Return gitignore patterns for non-source-of-truth artifacts.

    Derived artifacts, caches, and logs should not bloat the repo. Source-of-truth
    artifacts (contracts, wiki, applied_ops) stay tracked. Caller decides where to
    write these — typically appended to the workspace's .gitignore (or the repo's).
    """
    patterns: list[str] = []
    for spec in _layout_artifacts(layout):
        if spec.lifecycle in {"source_of_truth", "scaffold"}:
            continue
        path = spec.relative_path
        if spec.kind == "dir":
            patterns.append(f"{path}/")
        else:
            patterns.append(path)
    return sorted(set(patterns))


def render_workspace_readme(layout: WorkspaceLayout) -> str:
    """Generic README content explaining what `interns/` is and how to read it.

    Workspace-agnostic. Pulls the lifecycle classification from the artifact
    catalog so any new artifact added there shows up in the README automatically.
    """
    specs = _layout_artifacts(layout)
    by_lifecycle: dict[str, list[ArtifactSpec]] = {}
    for spec in specs:
        by_lifecycle.setdefault(spec.lifecycle, []).append(spec)

    lines = [
        "# `interns/` — Workspace State and Artifacts",
        "",
        "This directory holds every artifact the platform produces for this workspace.",
        "Each artifact has a **lifecycle** that decides whether it is tracked in version",
        "control, regeneratable, or safe to garbage-collect.",
        "",
        "**Generic across all workspaces.** No file under `interns/` is workspace-specific",
        "in *shape* — only in *content*. If you can read this README for one workspace, you",
        "can read it for any workspace.",
        "",
        "## Lifecycle classification",
        "",
        "| Lifecycle | Tracked in VC? | Auto-deleted? | What it means |",
        "| --- | --- | --- | --- |",
        "| `source_of_truth` | Yes | Never | Governed contract or accepted decision. Lose it and you lose the workspace. |",
        "| `scaffold` | Yes | Never | Hand-authored per-workspace glue (evaluation harnesses, bootstrap scripts). |",
        "| `derived` | No | On regeneration | Rendered or projected from a source-of-truth artifact. Always regeneratable. |",
        "| `cache` | No | On TTL (workspace-flow gc) | Short-lived intermediate (panel previews, session checkpoints). |",
        "| `log` | No | On size rotation | Append-only event/trajectory streams. |",
        "",
        "## Quick map",
        "",
    ]
    lifecycle_order = ["source_of_truth", "scaffold", "derived", "cache", "log"]
    for lifecycle in lifecycle_order:
        items = by_lifecycle.get(lifecycle) or []
        if not items:
            continue
        lines.append(f"### {lifecycle}")
        lines.append("")
        for spec in items:
            lines.append(f"- `{spec.relative_path}` — {spec.purpose}")
        lines.append("")

    lines.extend(
        [
            "## Canonical sources (do not edit derived projections)",
            "",
            "- Trajectory: `interns/state/trajectory.jsonl` — projections live under",
            "  `interns/generated/evidence/trajectory/` and `interns/reports/trajectory/` and",
            "  are regenerated by `workspace-flow gc` (or `tools/state_consolidator.py`).",
            "- Workspace memory: `interns/generated/contracts/workspace_feature_definitions.json`",
            "  — projections under `interns/generated/memory/` and `interns/reports/wiki_memory/`",
            "  are likewise regenerated.",
            "- Wiki notes (`wiki/kpis`, `wiki/features`, `wiki/sources`) live OUTSIDE",
            "  `interns/` because they carry hand-authored prose.",
            "",
            "## How to read this workspace",
            "",
            "1. `interns/MANIFEST.md` — full per-artifact inventory with freshness.",
            "2. `interns/generated/contracts/kpi_registry.json` — the source-of-truth KPI list.",
            "3. `interns/reports/source_to_target_plan.md` — human-readable execution plan.",
            "4. `interns/reports/kpi_results/current.md` — KPI definitions + generated SQL + result tables.",
            "",
            "## Maintenance commands (all generic, all safe-by-default)",
            "",
            "- `uv run workspace-flow artifacts --workspace <ws>` — print inventory.",
            "- `uv run workspace-flow artifacts --workspace <ws> --print-gitignore` — get gitignore patterns.",
            "- `uv run workspace-flow status --workspace <ws> --diff` — per-KPI gap + recovery commands.",
            "- `uv run workspace-flow gc --workspace <ws>` — dry-run garbage collection.",
            "- `uv run workspace-flow gc --workspace <ws> --apply` — execute GC.",
            "",
            "**Do not hand-edit anything inside `interns/generated/contracts/`** — those are",
            "machine-governed. To change them, use the corresponding `apply-*` CLI so the",
            "decision history and idempotency log stay correct.",
            "",
        ]
    )
    return "\n".join(lines)


def write_workspace_readme(layout: WorkspaceLayout) -> str:
    """Write the generic `interns/README.md`. Returns the relative path."""
    layout.interns_dir.mkdir(parents=True, exist_ok=True)
    out = layout.interns_dir / "README.md"
    out.write_text(render_workspace_readme(layout), encoding="utf-8")
    return _rel(out, layout.project_root)


def _render_delegations_section(layout: WorkspaceLayout) -> str:
    try:
        from core.onboarding.workspace.delegation import recent_delegations
    except Exception:
        return ""
    events = recent_delegations(layout, tail=20)
    if not events:
        return ""
    lines = [
        "## Recent specialist activity (last 20 delegations)",
        "",
        "| When | Agent | Stage | Verdict | Summary |",
        "| --- | --- | --- | --- | --- |",
    ]
    for ev in events:
        timestamp = str(ev.get("timestamp", ""))[:19].replace("T", " ")
        agent = str(ev.get("agent", ""))
        stage = str(ev.get("stage", ""))
        status = str(ev.get("status", ""))
        summary = str(ev.get("summary", "")).replace("|", "\\|")
        lines.append(f"| {timestamp} | `{agent}` | `{stage}` | `{status}` | {summary} |")
    lines.append("")
    return "\n".join(lines)


def write_manifest(layout: WorkspaceLayout) -> dict[str, str]:
    """Write `interns/MANIFEST.md` + `interns/MANIFEST.json` + `interns/README.md`.

    Generic. Always overwrites — the manifest + README are machine-owned;
    the wiki holds human-owned context.
    """
    inv = inventory(layout)
    layout.interns_dir.mkdir(parents=True, exist_ok=True)
    md_path = layout.interns_dir / "MANIFEST.md"
    json_path = layout.interns_dir / "MANIFEST.json"
    md_body = render_inventory_markdown(inv)
    delegations_section = _render_delegations_section(layout)
    if delegations_section:
        md_body = md_body + "\n" + delegations_section
    md_path.write_text(md_body, encoding="utf-8")
    json_path.write_text(json.dumps(inv, indent=2) + "\n", encoding="utf-8")
    readme_path = write_workspace_readme(layout)
    return {
        "markdown_path": _rel(md_path, layout.project_root),
        "json_path": _rel(json_path, layout.project_root),
        "readme_path": readme_path,
    }


__all__ = [
    "ArtifactSpec",
    "gitignore_patterns",
    "inventory",
    "render_inventory_markdown",
    "render_workspace_readme",
    "write_manifest",
    "write_workspace_readme",
]
