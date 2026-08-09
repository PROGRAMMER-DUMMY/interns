"""Provision planner: source declaration + discovery -> additive-only plan.

Reads two inputs produced upstream in the cloud-first spine:

* ``workspace_settings.json`` key ``source_declaration``
  (``{type, location, format_hint, credential_ref, declared_by, declared_at}``)
* ``interns/generated/intake/discovery.json``
  (``{connector, scanned_at, tables: [...], working_set_estimate_bytes, notes}``)

and writes ``interns/generated/contracts/provision_plan.json``: an ordered list
of steps whose ``kind`` is one of the four additive creates plus ``grant``.

The planner CANNOT plan destruction. There is no DROP/ALTER step kind. When a
requested object already exists with a DIFFERENT definition -- the only way an
additive create could turn into a rewrite -- the step is replaced by
``{"kind": "blocked_destructive", "reason": ...}`` and the human gate owns it
(spec Section 10).
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from core.contracts.versioning import register_contract
from core.onboarding.panel_contract import attach_stage_routing
from core.sql_safety import assert_safe_identifier
from core.storage.workspace_layout import WorkspaceLayout

PLAN_VERSION = 1
ARTIFACT_TYPE = "contracts/provision_plan.json"
# STAGE_ROUTING key (core/onboarding/workspace/delegation.py) plan/apply belong to.
ROUTING_STAGE = "provisioning"
register_contract(ARTIFACT_TYPE, current_version=PLAN_VERSION)

KIND_CATALOG = "create_catalog"
KIND_SCHEMA = "create_schema"
KIND_EXTERNAL_LOCATION = "create_external_location"
KIND_VOLUME = "create_volume"
KIND_GRANT = "grant"
KIND_BLOCKED = "blocked_destructive"

ADDITIVE_KINDS = (KIND_CATALOG, KIND_SCHEMA, KIND_EXTERNAL_LOCATION, KIND_VOLUME, KIND_GRANT)

# Object-store connectors are the ones that need an external location to be
# readable from Unity Catalog. jdbc/kafka authenticate per-job instead.
OBJECT_STORE_CONNECTORS = ("s3", "adls", "gcs")
# Connectors whose ingestion is a stream and therefore needs durable checkpoints.
CHECKPOINTING_CONNECTORS = ("s3", "adls", "gcs", "kafka")

DEFAULT_SCHEMAS = ("bronze", "silver", "gold")
# Checkpoints live in their OWN managed volume, never under the customer's
# source prefix: object lifecycle expiration rules on a source bucket corrupt
# stream state (pipeline_practices_gap_research.md, Auto Loader constraints).
CHECKPOINT_VOLUME = "_checkpoints"


@dataclass
class ProvisionPlan:
    plan_path: str
    catalog: str
    env: str
    connector: str
    steps: list[dict[str, Any]] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)

    @property
    def blocked(self) -> list[dict[str, Any]]:
        return [step for step in self.steps if step["kind"] == KIND_BLOCKED]

    def summary(self) -> dict[str, Any]:
        return attach_stage_routing({
            "plan_path": self.plan_path,
            "catalog": self.catalog,
            "env": self.env,
            "connector": self.connector,
            "step_count": len(self.steps),
            "additive_step_count": len(self.steps) - len(self.blocked),
            "blocked_count": len(self.blocked),
            "blocked": self.blocked,
            "notes": self.notes,
            "ok": True,
        }, ROUTING_STAGE)


def env_catalog(catalog_base: str, env: str) -> str:
    """Catalog-per-env naming: ``<catalog>_dev`` / ``<catalog>_prod`` (spec 8)."""
    base = assert_safe_identifier(str(catalog_base).strip(), context="catalog")
    suffix = assert_safe_identifier(str(env).strip().lower(), context="env")
    return base if base.endswith(f"_{suffix}") else f"{base}_{suffix}"


def load_source_declaration(layout: WorkspaceLayout) -> dict[str, Any]:
    """Read ``source_declaration`` from either settings location (durable wins)."""
    for path in (layout.durable_workspace_settings, layout.workspace_settings):
        if not path.exists():
            continue
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        declaration = payload.get("source_declaration")
        if isinstance(declaration, dict) and declaration:
            return declaration
    return {}


def discovery_path(layout: WorkspaceLayout) -> Path:
    return layout.generated_dir / "intake" / "discovery.json"


def load_discovery(layout: WorkspaceLayout) -> dict[str, Any]:
    path = discovery_path(layout)
    if not path.exists():
        return {}
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return payload if isinstance(payload, dict) else {}


def _step(kind: str, step_id: str, params: dict[str, Any], check: dict[str, Any]) -> dict[str, Any]:
    return {"step_id": step_id, "kind": kind, "params": params, "idempotent_check": check}


def _blocked(step_id: str, requested_kind: str, target: str, reason: str) -> dict[str, Any]:
    return {
        "step_id": step_id,
        "kind": KIND_BLOCKED,
        "params": {"requested_kind": requested_kind, "target": target},
        "idempotent_check": None,
        "reason": reason,
    }


def build_steps(
    *,
    catalog: str,
    connector: str,
    location: str,
    credential_ref: str,
    schemas: tuple[str, ...] = DEFAULT_SCHEMAS,
    grant_principals: tuple[str, ...] = (),
    existing_objects: dict[str, str] | None = None,
    storage_root: str = "",
) -> list[dict[str, Any]]:
    """Ordered additive steps. ``existing_objects`` maps ``"<kind>:<name>"`` to
    the object's CURRENT definition (url/location; empty string when the object
    has no definition worth comparing). A definition mismatch is the only way an
    additive create would rewrite something, so that -- and a grant against a
    pre-existing catalog -- are the two blocked_destructive cases.

    # ponytail: existing_objects comes from the discovery scan, not a live UC
    # query, so the planner is offline. Apply re-checks existence per step
    # anyway; query UC here only if plan-time blocking needs to be exact."""
    existing = dict(existing_objects or {})
    steps: list[dict[str, Any]] = []
    bronze = schemas[0] if schemas else "bronze"

    if connector in OBJECT_STORE_CONNECTORS and location:
        loc_name = f"{catalog}_root"
        url = location.rstrip("/") + "/"
        current = existing.get(f"external_location:{loc_name}")
        if current is not None and current and current.rstrip("/") + "/" != url:
            steps.append(_blocked(
                f"{KIND_EXTERNAL_LOCATION}:{loc_name}", KIND_EXTERNAL_LOCATION, loc_name,
                f"external location {loc_name} already points at a different url; "
                "repointing it rewrites an existing object and needs the destructive gate",
            ))
        else:
            steps.append(_step(
                KIND_EXTERNAL_LOCATION, f"{KIND_EXTERNAL_LOCATION}:{loc_name}",
                {"name": loc_name, "url": url, "credential_ref": credential_ref},
                {"api": "external_location_exists", "args": [loc_name]},
            ))

    steps.append(_step(
        KIND_CATALOG, f"{KIND_CATALOG}:{catalog}",
        {"name": catalog, "storage_root": storage_root},
        {"api": "catalog_exists", "args": [catalog]},
    ))
    for schema in schemas:
        assert_safe_identifier(schema, context="schema")
        steps.append(_step(
            KIND_SCHEMA, f"{KIND_SCHEMA}:{catalog}.{schema}",
            {"catalog": catalog, "schema": schema},
            {"api": "schema_exists", "args": [catalog, schema]},
        ))

    if connector in CHECKPOINTING_CONNECTORS:
        vol_key = f"volume:{catalog}.{bronze}.{CHECKPOINT_VOLUME}"
        current = existing.get(vol_key)
        if current:
            steps.append(_blocked(
                f"{KIND_VOLUME}:{catalog}.{bronze}.{CHECKPOINT_VOLUME}", KIND_VOLUME,
                f"{catalog}.{bronze}.{CHECKPOINT_VOLUME}",
                "an external volume already occupies the checkpoint name; changing "
                "its storage location rewrites an existing object and needs the "
                "destructive gate",
            ))
        else:
            steps.append(_step(
                KIND_VOLUME, f"{KIND_VOLUME}:{catalog}.{bronze}.{CHECKPOINT_VOLUME}",
                {
                    "catalog": catalog,
                    "schema": bronze,
                    "name": CHECKPOINT_VOLUME,
                    "volume_type": "MANAGED",
                    "purpose": (
                        "Auto Loader / structured streaming checkpoints and schema "
                        "locations. Managed volume on purpose: it sits outside any "
                        "customer object-lifecycle expiration prefix, so stream state "
                        "survives bucket retention rules."
                    ),
                },
                {"api": "volume_exists", "args": [catalog, bronze, CHECKPOINT_VOLUME]},
            ))

    for principal in grant_principals:
        step_id = f"{KIND_GRANT}:{catalog}:{principal}"
        if existing.get(f"catalog:{catalog}") is not None:
            steps.append(_blocked(
                step_id, KIND_GRANT, f"{catalog} -> {principal}",
                f"catalog {catalog} already exists; changing grants on an existing "
                "securable is a gated destructive op (spec Section 10)",
            ))
            continue
        steps.append(_step(
            KIND_GRANT, step_id,
            {
                "securable_type": "catalog",
                "securable": catalog,
                "principal": principal,
                "privileges": ["USE_CATALOG", "USE_SCHEMA", "SELECT"],
            },
            {"api": "grant_is_idempotent", "args": []},
        ))
    return steps


def build_provision_plan(
    repo_root: str | Path,
    workspace: str | Path,
    *,
    catalog: str = "",
    env: str = "dev",
    schemas: tuple[str, ...] = DEFAULT_SCHEMAS,
    grant_principals: tuple[str, ...] = (),
    storage_root: str = "",
) -> ProvisionPlan:
    repo_root = Path(repo_root).resolve()
    layout = WorkspaceLayout(project_root=(repo_root / workspace).resolve())

    declaration = load_source_declaration(layout)
    if not declaration:
        raise FileNotFoundError(
            "no source_declaration in workspace_settings.json; declare the source "
            "first (Phase 0 of the cloud-first flow)."
        )
    discovery = load_discovery(layout)

    connector = str(declaration.get("type") or discovery.get("connector") or "").strip().lower()
    if not connector:
        raise ValueError("source_declaration.type is empty; cannot choose provisioning steps.")
    location = str(declaration.get("location") or "").strip()
    credential_ref = str(declaration.get("credential_ref") or "").strip()

    catalog_base = str(catalog or layout.project_root.name).strip()
    full_catalog = env_catalog(catalog_base, env)

    existing = discovery.get("existing_objects")
    steps = build_steps(
        catalog=full_catalog,
        connector=connector,
        location=location,
        credential_ref=credential_ref,
        schemas=tuple(schemas),
        grant_principals=tuple(grant_principals),
        existing_objects=existing if isinstance(existing, dict) else None,
        storage_root=storage_root,
    )

    notes: list[str] = []
    if connector in OBJECT_STORE_CONNECTORS and not credential_ref:
        notes.append(
            "[!] source_declaration.credential_ref is empty; the external location "
            "step cannot bind a storage credential until it is declared."
        )
    if connector not in OBJECT_STORE_CONNECTORS:
        notes.append(
            f"[~] connector {connector!r} authenticates per ingestion job; no external "
            "location is provisioned."
        )
    tables = discovery.get("tables") or []
    if not tables:
        notes.append("[~] discovery listed no tables; provisioning containers only.")

    bronze = tuple(schemas)[0] if schemas else "bronze"
    payload = {
        "artifact_type": ARTIFACT_TYPE,
        "version": PLAN_VERSION,
        "generated_by": "plan-provisioning",
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "workspace": layout.project_root.name,
        "connector": connector,
        "source_location": location,
        "credential_ref": credential_ref,
        "catalog_base": catalog_base,
        "env": str(env).strip().lower(),
        "catalog": full_catalog,
        "schemas": list(schemas),
        "bronze_schema": bronze,
        "checkpoint_root": f"/Volumes/{full_catalog}/{bronze}/{CHECKPOINT_VOLUME}",
        "additive_only": True,
        "discovered_table_count": len(tables),
        "working_set_estimate_bytes": discovery.get("working_set_estimate_bytes"),
        "step_count": len(steps),
        "blocked_count": sum(1 for step in steps if step["kind"] == KIND_BLOCKED),
        "steps": steps,
        "notes": notes,
    }
    attach_stage_routing(payload, ROUTING_STAGE)

    layout.contracts_dir.mkdir(parents=True, exist_ok=True)
    plan_file = layout.contracts_dir / "provision_plan.json"
    plan_file.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")

    return ProvisionPlan(
        plan_path=_rel(plan_file, repo_root),
        catalog=full_catalog,
        env=payload["env"],
        connector=connector,
        steps=steps,
        notes=notes,
    )


def load_provision_plan(layout: WorkspaceLayout) -> dict[str, Any]:
    path = layout.contracts_dir / "provision_plan.json"
    if not path.exists():
        return {}
    return json.loads(path.read_text(encoding="utf-8"))


def _rel(path: Path, root: Path) -> str:
    try:
        return path.resolve().relative_to(root.resolve()).as_posix()
    except ValueError:
        return path.as_posix()


__all__ = [
    "ADDITIVE_KINDS",
    "CHECKPOINT_VOLUME",
    "DEFAULT_SCHEMAS",
    "KIND_BLOCKED",
    "ProvisionPlan",
    "build_provision_plan",
    "build_steps",
    "env_catalog",
    "load_discovery",
    "load_provision_plan",
    "load_source_declaration",
]
