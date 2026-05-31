"""Standalone CLI for the BUG-010 data-understanding gate.

Runs the side-effect-free classifier in
``core.onboarding.data_model.data_understanding`` against a workspace's
generated profiles + relationship contracts, then writes the same
``interns/reports/data_understanding/current.{json,md}`` artifact the flow gate
(``core.onboarding.workspace.flow``) produces and prints a summary.

This module is the CLI surface only: it owns no classification logic. It loads
profiles exactly the way the flow gate does (``profile_index.json`` first, then
per-table ``*.profile.json`` files) and delegates every decision to the
classifier functions. The artifact-shaping/markdown helpers are reused from
``flow`` so the standalone artifact is byte-compatible with the gate's artifact.

Workspace-agnostic: no hardcoded business words. ``--quiet`` prints a compact
summary line (tier + schema type + scoped option count + artifact path) while
still writing the full JSON + Markdown to disk.
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

from core.paths import PROJECT_ROOT
from core.storage.workspace_layout import WorkspaceLayout
from core.onboarding.data_model.data_understanding import (
    classify_quality_tier,
    classify_schema_type,
    scoped_processing_options,
)

# Reuse the gate's presentation + artifact-shaping helpers so the standalone
# artifact stays identical to what the in-flow gate writes. These are
# presentation helpers, not classifier logic.
from core.onboarding.workspace.flow import (
    FLOW_VERSION,
    _now,
    _read_json,
    _rel,
    _render_data_understanding_markdown,
    _summarize_current_data_model,
    _summarize_current_kpi_set,
)


def _load_profiles(layout: WorkspaceLayout) -> list[dict[str, Any]]:
    """Load generated profiles as a list of profile dicts.

    Mirrors ``WorkspaceFlow._load_profiles``: prefer the ``profile_index.json``
    written by onboarding, then fall back to the per-table ``*.profile.json``
    files. Each entry already carries ``columns``/``schema``/``row_count``/``path``
    — exactly the shape the data-understanding classifier expects.
    """
    index_path = layout.profile_index_path
    if index_path.exists():
        index = _read_json(index_path)
        profiles = [p for p in (index.get("profiles") or []) if isinstance(p, dict)]
        if profiles:
            return profiles
    profiles: list[dict[str, Any]] = []
    if layout.profiles_dir.exists():
        for path in sorted(layout.profiles_dir.glob("*.profile.json")):
            data = _read_json(path)
            if data:
                profiles.append({**data, "path": data.get("path") or str(path)})
    return profiles


def run_understand_data(repo_root: str | Path, workspace: str | Path) -> dict[str, Any]:
    """Classify quality tier + schema type, write the artifact, return a summary.

    Side-effect-free except for writing the report artifact under
    ``interns/reports/data_understanding/``. Returns the same compact summary
    dict shape the flow gate records onto its session state.
    """
    repo_root = Path(repo_root).resolve()
    workspace_path = (repo_root / workspace).resolve()
    workspace_rel = _rel(workspace_path, repo_root)
    layout = WorkspaceLayout(project_root=workspace_path)

    profiles = _load_profiles(layout)
    relationships = _read_json(layout.relationship_contracts_path)

    tier_result = classify_quality_tier(profiles)
    schema_result = classify_schema_type(profiles, relationships)
    tier = str(tier_result.get("tier") or "")
    schema_type = str(schema_result.get("schema_type") or "")
    options = scoped_processing_options(tier, profiles, schema_type)

    current_data_model = _summarize_current_data_model(profiles, relationships)
    current_kpi_set = _summarize_current_kpi_set(layout)

    top_level_options = [
        {
            "option_id": "option_generate",
            "label": "Generate KPI and/or data model artifacts",
            "description": (
                "Run the governed onboarding -> feature resolution -> KPI/SQL generation "
                "pipeline to produce new KPI and data-model artifacts for this workspace."
            ),
        },
        {
            "option_id": "option_forward",
            "label": "Move forward with the current workflow",
            "description": (
                "Proceed using the current data model and existing KPI set (echoed below) "
                "without regenerating artifacts."
            ),
        },
    ]

    understanding = {
        "artifact_type": "data_understanding/current.json",
        "version": FLOW_VERSION,
        "workspace": workspace_rel,
        "generated_at": _now(),
        "profile_count": len(profiles),
        "quality_tier": tier_result,
        "schema_type": schema_result,
        "scoped_processing_options": options,
        "top_level_options": top_level_options,
        "current_data_model": current_data_model,
        "current_kpi_set": current_kpi_set,
    }

    report_dir = layout.reports_dir / "data_understanding"
    report_dir.mkdir(parents=True, exist_ok=True)
    json_path = report_dir / "current.json"
    md_path = report_dir / "current.md"
    json_path.write_text(json.dumps(understanding, indent=2, default=str) + "\n", encoding="utf-8")
    md_path.write_text(_render_data_understanding_markdown(understanding), encoding="utf-8")

    return {
        "quality_tier": tier,
        "tier_confidence": tier_result.get("confidence"),
        "schema_type": schema_type,
        "schema_confidence": schema_result.get("confidence"),
        "scoped_option_count": len(options),
        "profile_count": len(profiles),
        "json_path": _rel(json_path, repo_root),
        "markdown_path": _rel(md_path, repo_root),
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Classify a workspace's data-quality tier + schema type from generated "
            "profiles and write the data-understanding summary."
        )
    )
    parser.add_argument("--workspace", required=True, help="Workspace path, for example workspaces/demo")
    parser.add_argument(
        "--repo-root",
        default=str(PROJECT_ROOT),
        help="Repository root. Defaults to detected project root.",
    )
    parser.add_argument("--json", action="store_true", help="Print the full summary JSON.")
    parser.add_argument(
        "--quiet",
        action="store_true",
        help=(
            "Print a compact one-line summary (tier + schema type + scoped option count + "
            "artifact path) instead of the full JSON. The full JSON + Markdown are still "
            "written to disk."
        ),
    )
    args = parser.parse_args(argv)

    summary = run_understand_data(args.repo_root, args.workspace)

    if args.json and not args.quiet:
        print(json.dumps(summary, indent=2))
        return 0

    if args.quiet:
        print(
            f"data-understanding: tier={summary['quality_tier']} "
            f"schema={summary['schema_type']} "
            f"scoped_options={summary['scoped_option_count']} "
            f"profiles={summary['profile_count']} "
            f"-> {summary['json_path']}"
        )
        return 0

    print(f"Workspace: {summary['json_path'].split('/interns/')[0]}")
    print(f"Quality tier: {summary['quality_tier']} (confidence {summary['tier_confidence']})")
    print(f"Schema type: {summary['schema_type']} (confidence {summary['schema_confidence']})")
    print(f"Scoped processing options: {summary['scoped_option_count']}")
    print(f"Profiles analyzed: {summary['profile_count']}")
    print(f"Artifact (json): {summary['json_path']}")
    print(f"Artifact (md):   {summary['markdown_path']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
