"""
core/medallion/deploy_plan.py — `uv run medallion plan-deploy`.

Emits a deterministic, validated Databricks deployment plan JSON for a
workspace's medallion pipeline, following docs/prd/databricks_deployment.md:

- Unity Catalog mapping (catalog/schema/volume layout per workspace)
- Lakeflow/Jobs orchestration of the medallion refresh with the incremental
  fingerprint semantics from core/medallion/incremental.py
- Permission model (PHI/PCI gate state, PII redaction interplay, UC grants)
- Cost guardrails (cluster policy, serverless default, query-history hooks
  per config/databricks_scopes.json)
- Deployment gates (local green requirements, --confirmed-by human provenance)
- Rollback strategy

PLAN ONLY. This command never talks to Databricks. Execution/apply stays
behind the existing remote-approval gate (AUTORESEARCH_ALLOW_REMOTE_EXECUTION,
--apply --confirm-remote-mutation in deploy-databricks-workspace).
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from core.governance.provenance import decision_source
from core.medallion.design import medallion_dirs
from core.medallion.incremental import MANIFEST_JSON as REFRESH_MANIFEST_JSON
from core.medallion.manifest import Manifest
from core.paths import PROJECT_ROOT
from core.storage.workspace_layout import WorkspaceLayout

SCHEMA_VERSION = 1
PLAN_KIND = "medallion_databricks_deployment"
PLAN_JSON = "deploy_plan.json"
PLAN_MD = "deploy_plan.md"

_IDENT_RE = re.compile(r"^[a-z][a-z0-9_]*$")

_APPLY_REQUIREMENTS = [
    "Run deploy-databricks-workspace with --apply",
    "Pass --confirm-remote-mutation",
    "Set AUTORESEARCH_ALLOW_REMOTE_EXECUTION=1",
    "Configure DATABRICKS_HOST and DATABRICKS_TOKEN",
    "PHI/PCI gate must pass for the configured target (core.governance.phi_gate)",
]

_LOCAL_GREEN_GATES = [
    "uv run medallion build --workspace <workspace> exits 0 with tables_failed=0",
    "Silver assertions all pass in the latest run.json",
    "KPI row-equality diff has 0 unequal KPIs (run.json kpi_diff)",
    "Project execution harness passes (run-kpi-execution-harness)",
    "green-gate suite green (core/dev/green_gate.py)",
    "validate-workspace-artifacts reports no errors",
]


# ── Plan builder ───────────────────────────────────────────────────────────────

def build_deploy_plan(
    workspace: Path,
    repo_root: Path,
    *,
    confirmed_by: str = "",
    cfg: Any = None,
) -> dict[str, Any]:
    """Build the deterministic deployment plan dict (no timestamps inside the
    deterministic body; volatile fields live under ``meta``)."""
    workspace = workspace.resolve()
    repo_root = repo_root.resolve()
    layout = WorkspaceLayout(project_root=workspace)
    paths = medallion_dirs(layout)

    manifest_path = paths["medallion"] / "manifest.yaml"
    if not manifest_path.exists():
        raise FileNotFoundError(
            f"No medallion manifest at {manifest_path}. "
            f"Run `uv run medallion design --workspace {_rel(workspace, repo_root)}` first."
        )
    manifest = _load_manifest(manifest_path)

    catalog = _catalog_name(workspace.name)
    schemas = {"bronze": "bronze", "silver": "silver", "gold": "gold", "ops": "ops"}

    tables = _table_mappings(manifest, catalog, schemas)
    tasks = _job_tasks(manifest)
    phi, pci, blockers = _sensitivity(layout, cfg, repo_root=repo_root)

    plan: dict[str, Any] = {
        "schema_version": SCHEMA_VERSION,
        "plan_kind": PLAN_KIND,
        "workspace": _rel(workspace, repo_root),
        "workspace_name": workspace.name,
        "source_manifest": {
            "path": _rel(manifest_path, repo_root),
            "inputs_hash": manifest.inputs_hash,
            "generated_at": manifest.generated_at,
        },
        "execution": {
            "mode": "plan_only",
            "remote_mutation": "disabled",
            "apply_requirements": list(_APPLY_REQUIREMENTS),
            "approval_gate": (
                "core.execution.backend.build_execution_backend (env approval) + "
                "core.onboarding.databricks.workspace_deployer._require_remote_approval"
            ),
        },
        "unity_catalog": {
            "catalog": catalog,
            "schemas": schemas,
            "volumes": [
                {
                    "name": "raw_sources",
                    "schema": schemas["bronze"],
                    "volume_path": f"/Volumes/{catalog}/{schemas['bronze']}/raw_sources",
                    "uploads": [
                        {
                            "source_file": b.source_file,
                            "target_path": (
                                f"/Volumes/{catalog}/{schemas['bronze']}/raw_sources/"
                                + Path(b.source_file).as_posix().lstrip("/")
                            ),
                        }
                        for b in manifest.bronze
                    ],
                }
            ],
            "tables": tables,
        },
        "orchestration": {
            "engine": "lakeflow_jobs",
            "job_name": f"medallion_refresh__{catalog}",
            "job_parameters": [{"name": "force", "default": "false"}],
            "tasks": tasks,
            "incremental": {
                "strategy": "fingerprint_manifest",
                "local_manifest_path": _rel(paths["state"] / REFRESH_MANIFEST_JSON, repo_root),
                "remote_manifest_table": f"{catalog}.{schemas['ops']}.refresh_manifest",
                "semantics": (
                    "Each task computes its input fingerprints (source files, "
                    "emitted SQL, upstream table fingerprints) and exits early "
                    "as SKIPPED when unchanged since the last successful build; "
                    "force=true rebuilds every table."
                ),
            },
            "schedule": {
                "type": "manual",
                "note": "Triggers/cron require human approval and a cost review first.",
            },
        },
        "permissions": {
            "phi": phi,
            "pci": pci,
            "pii_redaction": (
                "Silver PII columns are salt-hashed at build time "
                "(core/medallion/pii.py + salt_store); panel/report text is "
                "redacted by core/onboarding/kpi/pii_redaction.py. Raw PII "
                "columns must never be granted below the data_engineer role."
            ),
            "grants": _grants(catalog, schemas),
        },
        "cost_guardrails": {
            "default_compute": "serverless",
            "classic_compute_policy": {
                "max_workers": 2,
                "autotermination_minutes": 15,
                "allowed_node_types": ["smallest available"],
                "note": "Classic clusters only when serverless is unavailable; policy must cap size.",
            },
            "job_timeout_seconds": 3600,
            "monitoring": {
                "query_history": "Poll query-history scope for slow/expensive statements per run.",
                "scopes_config": "config/databricks_scopes.json",
                "required_scopes": ["sql", "jobs", "files", "query-history"],
            },
        },
        "deployment_gates": {
            "local_green": list(_LOCAL_GREEN_GATES),
            "human_provenance": {
                "confirmed_by": confirmed_by,
                "source": decision_source(confirmed_by),
                "policy": (
                    "Deployment approval must be recorded with source=human via "
                    "--confirmed-by <name>; an agent-asserted plan (source=agent) "
                    "cannot clear the apply gate."
                ),
            },
            "blockers": blockers,
        },
        "rollback": {
            "strategy": "delta_time_travel_plus_versioned_artifacts",
            "steps": [
                "RESTORE TABLE <fqn> TO VERSION AS OF <pre-deploy version> for affected Gold/Silver tables",
                "Re-point the job to the previous repo-pinned artifact set (git SHA recorded in the plan)",
                "Re-run medallion build locally to verify parity before re-deploying",
                "Volumes: raw uploads are append-only; remove only the newly uploaded files",
            ],
        },
    }

    errors = validate_deploy_plan(plan)
    plan["validation"] = {"errors": errors, "valid": not errors}
    return plan


def _load_manifest(path: Path) -> Manifest:
    import yaml

    with path.open("r", encoding="utf-8") as fh:
        return Manifest.from_dict(yaml.safe_load(fh))


def _catalog_name(workspace_name: str) -> str:
    chars = [c if c.isalnum() else "_" for c in workspace_name.lower()]
    name = "_".join(part for part in "".join(chars).split("_") if part)
    if not name or not name[0].isalpha():
        name = "ws_" + name
    return name


def _table_mappings(manifest: Manifest, catalog: str, schemas: dict[str, str]) -> list[dict[str, Any]]:
    tables: list[dict[str, Any]] = []
    for b in manifest.bronze:
        tables.append({
            "layer": "bronze",
            "name": b.name,
            "fqn": f"{catalog}.{schemas['bronze']}.{b.name}",
            "format": "DELTA",
            "load_strategy": b.load_strategy,
            "source_file": b.source_file,
        })
    for s in manifest.silver:
        tables.append({
            "layer": "silver",
            "name": s.name,
            "fqn": f"{catalog}.{schemas['silver']}.{s.name}",
            "format": "DELTA",
            "load_strategy": s.load_strategy,
            "primary_key": list(s.primary_key),
        })
    for g in manifest.gold:
        tables.append({
            "layer": "gold",
            "name": g.name,
            "fqn": f"{catalog}.{schemas['gold']}.{g.name}",
            "format": "DELTA",
            "load_strategy": g.load_strategy,
            "kind": g.kind,
        })
    return tables


def _job_tasks(manifest: Manifest) -> list[dict[str, Any]]:
    bronze_keys = [f"bronze__{b.name}" for b in manifest.bronze]

    def _resolve(dep: str, allowed: dict[str, str]) -> str | None:
        bare = dep.split(".", 1)[-1]
        return allowed.get(bare)

    bronze_by_bare = {b.name: f"bronze__{b.name}" for b in manifest.bronze}
    silver_by_bare = {s.name: f"silver__{s.name}" for s in manifest.silver}
    gold_by_bare = {g.name: f"gold__{g.name}" for g in manifest.gold}

    tasks: list[dict[str, Any]] = []
    for b in manifest.bronze:
        tasks.append({
            "task_key": f"bronze__{b.name}",
            "layer": "bronze",
            "table": b.name,
            "depends_on": [],
            "parameters": {"force": "{{job.parameters.force}}"},
        })
    for s in manifest.silver:
        deps = sorted({
            resolved for dep in s.derived_from
            if (resolved := _resolve(dep, bronze_by_bare)) is not None
        })
        tasks.append({
            "task_key": f"silver__{s.name}",
            "layer": "silver",
            "table": s.name,
            "depends_on": deps or list(bronze_keys),
            "parameters": {"force": "{{job.parameters.force}}"},
        })
    for g in manifest.gold:
        deps: list[str] = []
        for dep in g.derived_from:
            resolved = _resolve(dep, silver_by_bare) or _resolve(dep, gold_by_bare)
            if resolved and resolved != f"gold__{g.name}":
                deps.append(resolved)
        if not deps:
            deps = sorted(silver_by_bare.values())
        tasks.append({
            "task_key": f"gold__{g.name}",
            "layer": "gold",
            "table": g.name,
            "depends_on": sorted(set(deps)),
            "parameters": {"force": "{{job.parameters.force}}"},
        })
    return tasks


def _sensitivity(
    layout: WorkspaceLayout, cfg: Any, *, repo_root: Path
) -> tuple[dict[str, Any], dict[str, Any], list[str]]:
    from core.governance.phi_gate import (
        assess_workspace_phi,
        assess_workspace_pci,
        databricks_phi_covered,
        databricks_pci_covered,
    )

    phi_assessment = assess_workspace_phi(layout)
    pci_assessment = assess_workspace_pci(layout)
    phi_covered = databricks_phi_covered(cfg) if cfg is not None else False
    pci_covered = databricks_pci_covered(cfg) if cfg is not None else False

    blockers: list[str] = []
    if phi_assessment.is_phi and not phi_covered:
        blockers.append(
            "PHI detected and the configured Databricks target is not HIPAA-covered: "
            "deployment of identifiable data is blocked (core.governance.phi_gate). "
            "Provide a BAA-covered target or de-identified data."
        )
    if pci_assessment.tier == "pci" and not pci_covered:
        blockers.append(
            "PCI cardholder data detected and the target is not PCI-attested: "
            "deployment is blocked (core.governance.phi_gate)."
        )

    # Dataset paths must be REPO-RELATIVE: the plan is a shareable artifact;
    # absolute local paths leak machine layout and break plan-freshness
    # comparison across checkouts. The validator rejects absolute paths.
    def _relativize(paths: list[str]) -> list[str]:
        out = []
        for value in paths:
            path = Path(value)
            try:
                out.append(path.resolve().relative_to(repo_root).as_posix())
            except ValueError:
                out.append(str(value).replace("\\", "/"))
        return sorted(out)

    phi = {
        "tier": phi_assessment.tier,
        "datasets": _relativize(list(phi_assessment.datasets)),
        "target_covered": phi_covered,
        "gate": "core.governance.phi_gate.enforce_remote_sensitive_gate",
    }
    pci = {
        "tier": pci_assessment.tier,
        "datasets": _relativize(list(pci_assessment.datasets)),
        "target_covered": pci_covered,
        "gate": "core.governance.phi_gate.enforce_remote_sensitive_gate",
    }
    return phi, pci, blockers


def _grants(catalog: str, schemas: dict[str, str]) -> list[dict[str, Any]]:
    return [
        {
            "principal_role": "data_engineer",
            "securable": f"catalog:{catalog}",
            "privileges": ["USE CATALOG", "USE SCHEMA", "CREATE TABLE", "MODIFY", "SELECT"],
            "note": "Runs the medallion refresh job; only role allowed to write Bronze/Silver.",
        },
        {
            "principal_role": "analyst",
            "securable": f"schema:{catalog}.{schemas['gold']}",
            "privileges": ["USE CATALOG", "USE SCHEMA", "SELECT"],
            "note": "Read-only on Gold; no access to Bronze/Silver (pre-redaction layers).",
        },
        {
            "principal_role": "dashboard_service",
            "securable": f"schema:{catalog}.{schemas['gold']}",
            "privileges": ["SELECT"],
            "note": "Dashboards/Genie read Gold only.",
        },
        {
            "principal_role": "ops_auditor",
            "securable": f"schema:{catalog}.{schemas['ops']}",
            "privileges": ["SELECT"],
            "note": "Read refresh manifest + run evidence for audit.",
        },
    ]


# ── Validation ─────────────────────────────────────────────────────────────────

def validate_deploy_plan(plan: dict[str, Any]) -> list[str]:
    """Return a list of validation errors; empty list means the plan is valid."""
    errors: list[str] = []

    for key in (
        "schema_version", "plan_kind", "workspace", "workspace_name",
        "source_manifest", "execution", "unity_catalog", "orchestration",
        "permissions", "cost_guardrails", "deployment_gates", "rollback",
    ):
        if key not in plan:
            errors.append(f"missing top-level key: {key}")
    if errors:
        return errors

    if plan["plan_kind"] != PLAN_KIND:
        errors.append(f"plan_kind must be {PLAN_KIND}")
    execution = plan["execution"]
    if execution.get("mode") != "plan_only":
        errors.append("execution.mode must be plan_only")
    if execution.get("remote_mutation") != "disabled":
        errors.append("execution.remote_mutation must be disabled")
    if not execution.get("apply_requirements"):
        errors.append("execution.apply_requirements must be non-empty")

    uc = plan["unity_catalog"]
    catalog = uc.get("catalog", "")
    if not _IDENT_RE.match(catalog or ""):
        errors.append(f"invalid catalog identifier: {catalog!r}")
    schemas = uc.get("schemas", {})
    for layer in ("bronze", "silver", "gold", "ops"):
        if not _IDENT_RE.match(schemas.get(layer, "") or ""):
            errors.append(f"invalid or missing schema for layer: {layer}")

    tables = uc.get("tables", [])
    if not tables:
        errors.append("unity_catalog.tables must be non-empty")
    seen_fqn: set[str] = set()
    for t in tables:
        fqn = t.get("fqn", "")
        if fqn in seen_fqn:
            errors.append(f"duplicate table fqn: {fqn}")
        seen_fqn.add(fqn)
        expected = f"{catalog}.{schemas.get(t.get('layer', ''), '?')}.{t.get('name', '?')}"
        if fqn != expected:
            errors.append(f"table fqn {fqn!r} does not match layout {expected!r}")

    orch = plan["orchestration"]
    tasks = orch.get("tasks", [])
    task_keys = [t.get("task_key", "") for t in tasks]
    if len(task_keys) != len(set(task_keys)):
        errors.append("duplicate task_key in orchestration.tasks")
    key_set = set(task_keys)
    table_keys = {f"{t['layer']}__{t['name']}" for t in tables}
    for expected_key in table_keys:
        if expected_key not in key_set:
            errors.append(f"missing job task for table: {expected_key}")
    layer_rank = {"bronze": 0, "silver": 1, "gold": 2}
    by_key = {t.get("task_key", ""): t for t in tasks}
    for t in tasks:
        for dep in t.get("depends_on", []):
            if dep not in key_set:
                errors.append(f"task {t.get('task_key')} depends on unknown task {dep}")
                continue
            if layer_rank.get(by_key[dep].get("layer"), 9) > layer_rank.get(t.get("layer"), 0):
                errors.append(
                    f"task {t.get('task_key')} depends on downstream layer task {dep}"
                )
        if t.get("layer") == "bronze" and t.get("depends_on"):
            errors.append(f"bronze task {t.get('task_key')} must not have dependencies")
    params = {p.get("name") for p in orch.get("job_parameters", [])}
    if "force" not in params:
        errors.append("orchestration.job_parameters must include force")
    incremental = orch.get("incremental", {})
    if incremental.get("strategy") != "fingerprint_manifest":
        errors.append("orchestration.incremental.strategy must be fingerprint_manifest")

    permissions = plan["permissions"]
    phi = permissions.get("phi", {})
    if "tier" not in phi:
        errors.append("permissions.phi.tier is required")
    # Dataset paths must be repo-relative: absolute paths leak machine layout
    # and break cross-checkout plan comparison (PRD section 10 item 1).
    for section_name in ("phi", "pci"):
        for dataset in (permissions.get(section_name) or {}).get("datasets") or []:
            text = str(dataset)
            if re.match(r"^([A-Za-z]:[\\/]|[\\/])", text):
                errors.append(
                    f"permissions.{section_name}.datasets contains an absolute "
                    f"path: {text!r} (must be repo-relative)"
                )
    gates = plan["deployment_gates"]
    if not gates.get("local_green"):
        errors.append("deployment_gates.local_green must be non-empty")
    provenance = gates.get("human_provenance", {})
    if provenance.get("source") not in {"human", "agent"}:
        errors.append("deployment_gates.human_provenance.source must be human or agent")
    if phi.get("tier") == "phi" and not phi.get("target_covered"):
        blocker_text = " ".join(gates.get("blockers", []))
        if "PHI" not in blocker_text:
            errors.append(
                "PHI workspace with uncovered target requires a PHI blocker in deployment_gates.blockers"
            )

    if not plan["rollback"].get("steps"):
        errors.append("rollback.steps must be non-empty")
    return errors


# ── Markdown rendering ─────────────────────────────────────────────────────────

def render_deploy_plan_md(plan: dict[str, Any]) -> str:
    uc = plan["unity_catalog"]
    orch = plan["orchestration"]
    gates = plan["deployment_gates"]
    validation = plan.get("validation", {})
    lines = [
        "# Medallion Databricks Deployment Plan",
        "",
        f"- Workspace: `{plan['workspace']}`",
        f"- Catalog: `{uc['catalog']}`",
        f"- Mode: **{plan['execution']['mode']}** (remote mutation {plan['execution']['remote_mutation']})",
        f"- Job: `{orch['job_name']}` ({len(orch['tasks'])} tasks, engine {orch['engine']})",
        f"- Incremental: {orch['incremental']['strategy']} "
        f"(manifest `{orch['incremental']['local_manifest_path']}`)",
        f"- Provenance: source={gates['human_provenance']['source']} "
        f"confirmed_by={gates['human_provenance']['confirmed_by'] or '(none)'}",
        f"- Valid: {validation.get('valid', False)}",
        "",
        "## Tables",
        "",
    ]
    for t in uc["tables"]:
        lines.append(f"- [{t['layer']}] `{t['fqn']}` ({t['load_strategy']})")
    lines += ["", "## Job Tasks", ""]
    for t in orch["tasks"]:
        deps = ", ".join(t["depends_on"]) or "(none)"
        lines.append(f"- `{t['task_key']}` depends on: {deps}")
    if gates.get("blockers"):
        lines += ["", "## Blockers", ""]
        for b in gates["blockers"]:
            lines.append(f"- [blocked] {b}")
    if validation.get("errors"):
        lines += ["", "## Validation Errors", ""]
        for e in validation["errors"]:
            lines.append(f"- [x] {e}")
    lines += [
        "",
        "## Apply Gate",
        "",
        "No remote call is made by plan-deploy. Apply requires:",
        "",
    ]
    for req in plan["execution"]["apply_requirements"]:
        lines.append(f"- {req}")
    return "\n".join(lines).rstrip() + "\n"


# ── CLI ────────────────────────────────────────────────────────────────────────

def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="medallion plan-deploy",
        description=(
            "Emit a deterministic, validated Databricks deployment plan for the "
            "workspace medallion pipeline (plan only; no remote calls)."
        ),
    )
    parser.add_argument("--workspace", required=True,
                        help="Workspace path relative to repo root.")
    parser.add_argument("--repo-root", default=None,
                        help="Repository root. Defaults to detected project root.")
    parser.add_argument("--confirmed-by", default="",
                        help="Human reviewer name for provenance (empty = agent-asserted).")
    parser.add_argument("--json", action="store_true",
                        help="Print the full plan JSON to stdout.")
    args = parser.parse_args(argv)

    repo_root = Path(args.repo_root).resolve() if args.repo_root else PROJECT_ROOT
    workspace = (
        Path(args.workspace).resolve()
        if Path(args.workspace).is_absolute()
        else (repo_root / args.workspace).resolve()
    )

    cfg = None
    try:
        import core.config as core_config
        cfg = core_config.load()
    except Exception:
        pass

    try:
        plan = build_deploy_plan(workspace, repo_root, confirmed_by=args.confirmed_by, cfg=cfg)
    except FileNotFoundError as exc:
        print(f"[plan-deploy] {exc}", file=sys.stderr)
        return 2

    plan["meta"] = {"generated_at": datetime.now(timezone.utc).isoformat()}

    layout = WorkspaceLayout(project_root=workspace)
    paths = medallion_dirs(layout)
    paths["medallion"].mkdir(parents=True, exist_ok=True)
    json_path = paths["medallion"] / PLAN_JSON
    md_path = paths["medallion"] / PLAN_MD
    json_path.write_text(json.dumps(plan, indent=2) + "\n", encoding="utf-8")
    md_path.write_text(render_deploy_plan_md(plan), encoding="utf-8")

    errors = plan["validation"]["errors"]
    if args.json:
        print(json.dumps(plan, indent=2))
    else:
        print(f"[plan-deploy] workspace: {plan['workspace']}")
        print(f"  catalog:        {plan['unity_catalog']['catalog']}")
        print(f"  tables:         {len(plan['unity_catalog']['tables'])}")
        print(f"  job_tasks:      {len(plan['orchestration']['tasks'])}")
        print(f"  provenance:     {plan['deployment_gates']['human_provenance']['source']}")
        print(f"  blockers:       {len(plan['deployment_gates']['blockers'])}")
        print(f"  valid:          {plan['validation']['valid']}")
        print(f"  plan_json:      {_rel(json_path, repo_root)}")
        print(f"  plan_md:        {_rel(md_path, repo_root)}")
        print("  remote_mutation: disabled (plan only; apply stays behind the approval gate)")
    if errors:
        for e in errors:
            print(f"  [x] {e}", file=sys.stderr)
        return 1
    return 0


def _rel(path: Path, root: Path) -> str:
    try:
        return str(path.resolve().relative_to(root.resolve())).replace("\\", "/")
    except ValueError:
        return str(path)


if __name__ == "__main__":
    raise SystemExit(main())
