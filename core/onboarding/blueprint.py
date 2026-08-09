"""The solution blueprint: what we will do, end to end, before we do any of it.

The gap this closes was observed live on 2026-07-27. A user said "my data is at
`s3://amzn-workspace-rcm/` and I want everything on Databricks", and the agent
replied by asking what to name a folder. It never listed the bucket, never said
which files would become tables and which would not, never stated where dbt
would run or what modelling technique it would use, and never showed a plan to
approve. Every one of those capabilities already existed -- they sat at stage 8
of 11, *after* onboarding, KPI blockers, the data model and relationships. The
defect was ordering and narration, not capability.

So this runs FIRST, before a workspace is even populated:

    discover  ->  propose  ->  edit in plain English  ->  one human approval

Three rules it exists to enforce:

* **Nothing is created before approval.** The blueprint emits the Unity Catalog
  bootstrap commands; it never runs them. `--approve` records a human decision,
  and only then may intake proceed.
* **Tables and volumes are different securables, so the split is mandatory, not
  cosmetic.** Databricks: *"You can't register files in volumes as tables.
  Volumes are intended for path-based data access only."* A KPI spreadsheet and
  a data-model image are context, not rows.
* **Zero copy is the default.** An external table registers data where it
  already lives; UC governs metadata while the bucket keeps lifecycle and
  layout. Copying (COPY INTO / Auto Loader) is a promotion you opt into when you
  need managed optimisation -- not the default that silently doubles storage.

Edits are applied as typed flags (`--exclude`, `--as-volume`, `--as-managed`),
never parsed from prose. The agent translates the user's English into flags;
the platform records the flags. Same contract as the KPI blocker panel, for the
same reason: a freehand answer cannot be replayed or audited.
"""
from __future__ import annotations
from core.observability.cost_ledger import anchored

import argparse
import json
import sys
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from core.contracts.versioning import register_contract
from core.onboarding.cli_deprecation import announce_deprecated_cli_redirect
from core.governance.provenance import decision_source as decision_source_for
from core.paths import PROJECT_ROOT
from core.storage.workspace_layout import WorkspaceLayout

BLUEPRINT_VERSION = 1
register_contract("solution_blueprint/current.json", current_version=BLUEPRINT_VERSION)

# How a discovered group is landed in Unity Catalog. The default for a dataset
# is an EXTERNAL table -- registered in place, nothing copied.
DISPOSITION_EXTERNAL_TABLE = "external_table"
DISPOSITION_MANAGED_TABLE = "managed_table"
DISPOSITION_VOLUME = "volume"
DISPOSITION_EXCLUDED = "excluded"

# Discovery's class names -> default disposition. `log_or_state`, `database` and
# `other` are excluded by default and must be opted IN, never out: a silent
# ingest of application logs is how a bronze layer becomes unusable.
_DEFAULT_DISPOSITION = {
    "dataset": DISPOSITION_EXTERNAL_TABLE,
    "delta_table": DISPOSITION_EXTERNAL_TABLE,
    "document": DISPOSITION_VOLUME,
    "log_or_state": DISPOSITION_EXCLUDED,
    "database": DISPOSITION_EXCLUDED,
    "other": DISPOSITION_EXCLUDED,
}

_EXCLUSION_REASON = {
    "log_or_state": "application logs / platform state -- excluded by default",
    "database": "database file: inspect and export deliberately, never bulk-ingest",
    "other": "unrecognised extension -- review before ingesting",
}

# Image extensions a data model may be supplied as. When one is discovered the
# blueprint says it will be PARSED; when none is, it says a model will be
# GENERATED for confirmation -- never silently assumed either way.
_MODEL_IMAGE_SUFFIXES = {".png", ".jpg", ".jpeg"}


@dataclass
class GroupPlan:
    group: str
    disposition: str
    file_count: int
    size_bytes: int
    class_name: str
    target: str = ""
    reason: str = ""
    edited_by_user: bool = False

    def to_dict(self) -> dict[str, Any]:
        return self.__dict__.copy()


@dataclass
class BlueprintResult:
    current_json_path: str
    current_markdown_path: str
    status: str  # "draft" | "approved"
    ingestion_mode: str
    table_count: int
    volume_count: int
    excluded_count: int
    ok: bool = True

    def summary(self) -> dict[str, Any]:
        return self.__dict__.copy()


@dataclass
class BlueprintEdits:
    """User overrides, persisted so re-running discovery does not lose them."""
    exclude: list[str] = field(default_factory=list)
    include: list[str] = field(default_factory=list)
    as_volume: list[str] = field(default_factory=list)
    as_managed: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return self.__dict__.copy()

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "BlueprintEdits":
        return cls(**{
            key: [str(v) for v in (data.get(key) or [])]
            for key in ("exclude", "include", "as_volume", "as_managed")
        })

    def merged_with(self, other: "BlueprintEdits") -> "BlueprintEdits":
        """Later edits win over earlier ones for the same group.

        A group named in a new instruction is dropped from every other bucket
        first -- otherwise "put patients back" would leave it in `exclude` AND
        `include`, and which one wins would depend on iteration order.
        """
        named = set(other.exclude + other.include + other.as_volume + other.as_managed)
        keep = lambda values: [v for v in values if v not in named]  # noqa: E731
        return BlueprintEdits(
            exclude=keep(self.exclude) + other.exclude,
            include=keep(self.include) + other.include,
            as_volume=keep(self.as_volume) + other.as_volume,
            as_managed=keep(self.as_managed) + other.as_managed,
        )


def _blueprint_dir(layout: WorkspaceLayout) -> Path:
    return layout.reports_dir / "solution_blueprint"


def load_blueprint(layout: WorkspaceLayout) -> dict[str, Any]:
    path = _blueprint_dir(layout) / "current.json"
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return {}
    return data if isinstance(data, dict) else {}


def _safe_name(value: str) -> str:
    cleaned = "".join(ch if ch.isalnum() or ch == "_" else "_" for ch in str(value).lower())
    return cleaned.strip("_") or "unnamed"


def plan_groups(
    discovery: dict[str, Any], edits: BlueprintEdits, *, catalog: str, bronze_schema: str
) -> list[GroupPlan]:
    """Turn discovery output plus user edits into a per-group disposition."""
    by_group: dict[str, dict[str, Any]] = {}
    for item in discovery.get("files", []) or []:
        group = str(item.get("group") or "root")
        entry = by_group.setdefault(
            group, {"files": 0, "size": 0, "classes": {}}
        )
        entry["files"] += 1
        entry["size"] += int(item.get("size_bytes") or 0)
        cls = str(item.get("class_name") or "other")
        entry["classes"][cls] = entry["classes"].get(cls, 0) + 1

    plans: list[GroupPlan] = []
    for group, entry in sorted(by_group.items()):
        # The dominant class decides the default; a mixed folder follows its
        # majority rather than silently splitting into two dispositions.
        dominant = max(entry["classes"].items(), key=lambda kv: (kv[1], kv[0]))[0]
        disposition = _DEFAULT_DISPOSITION.get(dominant, DISPOSITION_EXCLUDED)
        reason = _EXCLUSION_REASON.get(dominant, "")
        edited = False

        if group in edits.exclude:
            disposition, reason, edited = DISPOSITION_EXCLUDED, "excluded at your request", True
        elif group in edits.as_volume:
            disposition, reason, edited = DISPOSITION_VOLUME, "kept as files at your request", True
        elif group in edits.as_managed:
            disposition, reason, edited = DISPOSITION_MANAGED_TABLE, "managed at your request (data is COPIED)", True
        elif group in edits.include and disposition == DISPOSITION_EXCLUDED:
            disposition, reason, edited = DISPOSITION_EXTERNAL_TABLE, "included at your request", True

        target = ""
        if disposition in (DISPOSITION_EXTERNAL_TABLE, DISPOSITION_MANAGED_TABLE):
            target = f"{catalog}.{bronze_schema}.{_safe_name(group)}"
        elif disposition == DISPOSITION_VOLUME:
            target = f"/Volumes/{catalog}/{bronze_schema}/{_safe_name(group.split('/')[0])}"

        plans.append(GroupPlan(
            group=group,
            disposition=disposition,
            file_count=entry["files"],
            size_bytes=entry["size"],
            class_name=dominant,
            target=target,
            reason=reason,
            edited_by_user=edited,
        ))
    return plans


def _data_model_plan(discovery: dict[str, Any]) -> dict[str, str]:
    """Whether a supplied data-model image will be parsed, or one generated.

    Never silently assumed: a generated model becomes a confirmation question,
    which is how an assumption turns into a recorded human decision.
    """
    for item in discovery.get("files", []) or []:
        rel = str(item.get("relative_path") or "")
        if Path(rel).suffix.lower() in _MODEL_IMAGE_SUFFIXES:
            return {
                "action": "parse_supplied_image",
                "source": rel,
                "detail": f"`{rel}` will be parsed into a data model (parse-data-model-images), "
                          "then shown for your confirmation.",
            }
    return {
        "action": "generate_for_confirmation",
        "source": "",
        "detail": "No data-model image was found. One will be GENERATED from the profiled "
                  "schemas and relationships, and you must confirm it before it is used.",
    }


def build_blueprint(
    repo_root: str | Path,
    workspace: str | Path,
    *,
    source_root: str,
    catalog: str,
    ingestion_mode: str = "system",
    bronze_schema: str = "bronze",
    silver_schema: str = "silver",
    gold_schema: str = "gold",
    edits: BlueprintEdits | None = None,
) -> BlueprintResult:
    repo_root = Path(repo_root).resolve()
    layout = WorkspaceLayout(project_root=(repo_root / workspace).resolve())
    layout.ensure_runtime_dirs()

    discovery_path = layout.requirements_dir / "external_source_discovery.json"
    if not discovery_path.exists():
        raise FileNotFoundError(
            f"no discovery artifact at {discovery_path}. Run "
            f"`discover-external-sources --workspace {workspace} "
            f"--external-root {source_root}` first -- a blueprint without a "
            "listing would be a guess."
        )
    discovery = json.loads(discovery_path.read_text(encoding="utf-8"))

    prior = load_blueprint(layout)
    merged = BlueprintEdits.from_dict(prior.get("edits") or {})
    if edits is not None:
        merged = merged.merged_with(edits)

    plans = plan_groups(discovery, merged, catalog=catalog, bronze_schema=bronze_schema)
    payload = {
        "artifact_type": "solution_blueprint/current.json",
        "version": BLUEPRINT_VERSION,
        "generated_by": "prepare-solution-blueprint",
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "status": "draft",
        "source_root": source_root,
        "ingestion_mode": ingestion_mode,
        "catalog": catalog,
        "schemas": {"bronze": bronze_schema, "silver": silver_schema, "gold": gold_schema},
        "groups": [p.to_dict() for p in plans],
        "data_model": _data_model_plan(discovery),
        "edits": merged.to_dict(),
        "approval": prior.get("approval") or {},
        "bootstrap_commands": _bootstrap_commands(
            source_root, catalog, bronze_schema, silver_schema, gold_schema, plans
        ),
    }
    # An edit invalidates a prior approval -- approving plan A must never be
    # carried over onto plan B.
    if edits is not None and payload["approval"]:
        payload["approval"] = {}
        payload["approval_invalidated"] = "the plan changed after it was approved"

    return _write(layout, repo_root, payload)


def approve_blueprint(
    repo_root: str | Path, workspace: str | Path, *, confirmed_by: str
) -> BlueprintResult:
    repo_root = Path(repo_root).resolve()
    layout = WorkspaceLayout(project_root=(repo_root / workspace).resolve())
    payload = load_blueprint(layout)
    if not payload:
        raise FileNotFoundError(
            "no blueprint to approve; run `prepare-solution-blueprint` first."
        )
    source = decision_source_for(confirmed_by)
    if source != "human":
        raise PermissionError(
            f"--confirmed-by {confirmed_by!r} resolves to `{source}`, not a human. "
            "A blueprint authorises creating catalogs, schemas, volumes and tables; "
            "that decision needs a real name (Human-Gate Provenance Rule)."
        )
    payload["status"] = "approved"
    payload["approval"] = {
        "confirmed_by": confirmed_by,
        "source": source,
        "approved_at": datetime.now(timezone.utc).isoformat(),
    }
    payload.pop("approval_invalidated", None)
    return _write(layout, repo_root, payload)


def _bootstrap_commands(
    source_root: str, catalog: str, bronze: str, silver: str, gold: str,
    plans: list[GroupPlan],
) -> list[str]:
    """The exact Unity Catalog chain -- emitted, never executed."""
    root = source_root.rstrip("/") + "/"
    cmds = [
        f"databricks storage-credentials create --json '{{\"name\": \"{catalog}_src\", "
        '"aws_iam_role": {"role_arn": "<YOUR_IAM_ROLE_ARN>"}}\'',
        f"databricks external-locations create --json '{{\"name\": \"{catalog}_root\", "
        f'"url": "{root}", "credential_name": "{catalog}_src"}}\'',
        f"databricks catalogs create {catalog}",
    ]
    cmds += [f"databricks schemas create {schema} {catalog}" for schema in (bronze, silver, gold)]
    for plan in plans:
        if plan.disposition == DISPOSITION_VOLUME:
            volume = plan.target.rsplit("/", 1)[-1]
            cmds.append(
                f"databricks volumes create {catalog} {bronze} {volume} EXTERNAL "
                f"--storage-location {root}{plan.group.split('/')[0]}/"
            )
        elif plan.disposition == DISPOSITION_EXTERNAL_TABLE:
            cmds.append(
                f"-- zero copy: CREATE TABLE {plan.target} LOCATION '{root}{plan.group}/'"
            )
        elif plan.disposition == DISPOSITION_MANAGED_TABLE:
            cmds.append(
                f"-- COPIES DATA: CREATE TABLE {plan.target}; "
                f"COPY INTO {plan.target} FROM '{root}{plan.group}/'"
            )
    return cmds


def _write(layout: WorkspaceLayout, repo_root: Path, payload: dict[str, Any]) -> BlueprintResult:
    out_dir = _blueprint_dir(layout)
    out_dir.mkdir(parents=True, exist_ok=True)
    current_json = out_dir / "current.json"
    current_md = out_dir / "current.md"
    current_json.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    current_md.write_text(render_markdown(payload), encoding="utf-8")
    groups = payload.get("groups", [])
    counts = {d: sum(1 for g in groups if g["disposition"] == d) for d in (
        DISPOSITION_EXTERNAL_TABLE, DISPOSITION_MANAGED_TABLE,
        DISPOSITION_VOLUME, DISPOSITION_EXCLUDED)}
    return BlueprintResult(
        _rel(current_json, repo_root),
        _rel(current_md, repo_root),
        status=str(payload.get("status") or "draft"),
        ingestion_mode=str(payload.get("ingestion_mode") or "system"),
        table_count=counts[DISPOSITION_EXTERNAL_TABLE] + counts[DISPOSITION_MANAGED_TABLE],
        volume_count=counts[DISPOSITION_VOLUME],
        excluded_count=counts[DISPOSITION_EXCLUDED],
    )


def _size(num: int) -> str:
    value = float(num)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if value < 1024 or unit == "TB":
            return f"{value:.0f} {unit}" if unit == "B" else f"{value:.1f} {unit}"
        value /= 1024
    return f"{value:.1f} TB"


def render_markdown(payload: dict[str, Any]) -> str:
    groups = payload.get("groups", [])
    schemas = payload.get("schemas", {})
    approved = payload.get("status") == "approved"
    tables = [g for g in groups if g["disposition"] in (DISPOSITION_EXTERNAL_TABLE, DISPOSITION_MANAGED_TABLE)]
    volumes = [g for g in groups if g["disposition"] == DISPOSITION_VOLUME]
    excluded = [g for g in groups if g["disposition"] == DISPOSITION_EXCLUDED]

    lines = [
        "# Solution blueprint",
        "",
        f"Source: `{payload.get('source_root','')}`",
        f"Target: catalog `{payload.get('catalog','')}` -> "
        f"`{schemas.get('bronze','bronze')}` / `{schemas.get('silver','silver')}` / "
        f"`{schemas.get('gold','gold')}`",
        f"Ingestion: **{payload.get('ingestion_mode','system')}**",
        "",
        ("**APPROVED** by "
         f"`{(payload.get('approval') or {}).get('confirmed_by','')}` "
         f"({(payload.get('approval') or {}).get('approved_at','')})"
         if approved else "**NOTHING HAS BEEN CREATED YET.** This is a proposal."),
        "",
    ]
    if payload.get("approval_invalidated"):
        lines += [f"[x] Previous approval cleared: {payload['approval_invalidated']}.", ""]

    lines += ["## Will ingest as TABLES", ""]
    if tables:
        lines += ["| Source | Files | Size | Target | How |", "|---|---|---|---|---|"]
        for g in tables:
            how = ("zero copy (external)" if g["disposition"] == DISPOSITION_EXTERNAL_TABLE
                   else "**COPIES DATA** (managed)")
            mark = " *(your edit)*" if g["edited_by_user"] else ""
            lines.append(
                f"| `{g['group']}`{mark} | {g['file_count']} | {_size(g['size_bytes'])} "
                f"| `{g['target']}` | {how} |"
            )
    else:
        lines.append("_none_")
    lines.append("")

    lines += ["## Will ingest as DOCUMENTS (Unity Catalog volume)", ""]
    if volumes:
        lines.append("Context for KPI/data-model understanding. Files in a volume "
                     "cannot be registered as tables -- they are a different securable.")
        lines.append("")
        for g in volumes:
            mark = " *(your edit)*" if g["edited_by_user"] else ""
            lines.append(f"- `{g['group']}`{mark} -- {g['file_count']} file(s), "
                         f"{_size(g['size_bytes'])} -> `{g['target']}`")
    else:
        lines.append("_none_")
    lines.append("")

    lines += ["## Will NOT ingest", ""]
    if excluded:
        for g in excluded:
            mark = " *(your edit)*" if g["edited_by_user"] else ""
            lines.append(f"- `{g['group']}`{mark} -- {g['file_count']} file(s) -- {g['reason']}")
    else:
        lines.append("_nothing excluded_")
    lines.append("")

    model = payload.get("data_model") or {}
    lines += [
        "## Data model",
        "",
        model.get("detail", ""),
        "",
        "## How it will be built",
        "",
        f"- **bronze** (`{schemas.get('bronze','bronze')}`): raw, append-only, as landed.",
        f"- **silver** (`{schemas.get('silver','silver')}`): dbt incremental `merge` on a "
        "declared `unique_key` with `incremental_predicates` -- the idempotency contract. "
        "Data contract + 5-dimension quality tests + freshness and referential-integrity checks.",
        f"- **gold** (`{schemas.get('gold','gold')}`): business aggregates. Modelling technique "
        "is proposed from the confirmed data model (star schema with conformed dimensions "
        "unless the profile argues for one-big-table) and ratified before build.",
        "- **dbt** owns every bronze->silver->gold transformation; Airflow owns when it runs; "
        "this platform owns what it means and whether it may proceed.",
        "",
        "## Edit this plan",
        "",
        "```",
        "apply-blueprint-answer --workspace <ws> --exclude <group>      # drop it",
        "apply-blueprint-answer --workspace <ws> --include <group>      # add it back",
        "apply-blueprint-answer --workspace <ws> --as-volume <group>    # keep as files",
        "apply-blueprint-answer --workspace <ws> --as-managed <group>   # copy into Databricks",
        "apply-blueprint-answer --workspace <ws> --approve --confirmed-by \"<your name>\"",
        "```",
        "",
    ]
    if not approved:
        lines += [
            "## Commands that WILL run once approved",
            "",
            "```bash",
            *payload.get("bootstrap_commands", []),
            "```",
            "",
        ]
    return "\n".join(lines)


def _rel(path: Path, root: Path) -> str:
    try:
        return path.resolve().relative_to(root.resolve()).as_posix()
    except ValueError:
        return path.as_posix()


# Flags the cloud-first `prepare-blueprint` understands. Anything else a caller
# passes to the legacy CLI is dropped on the way through, with a named reason.
_REDIRECT_FLAGS = (
    "--workspace",
    "--repo-root",
    "--catalog",
    "--bronze-schema",
    "--silver-schema",
    "--gold-schema",
)

# Legacy-only flags and why the new spine has no equivalent.
_DROPPED_FLAGS = {
    "--source-root": (
        "the source now comes from `declare-source` "
        "(workspace_settings.source_declaration), not a flag"
    ),
    "--ingestion-mode": (
        "ingestion code is emitted by `generate-ingestion` and executed by "
        "`run-ingestion`, which is gated separately"
    ),
}


def _forwardable_argv(argv: list[str]) -> tuple[list[str], list[str]]:
    """Split a legacy argv into (forwarded, notes-about-what-was-dropped)."""
    forwarded: list[str] = []
    notes: list[str] = []
    index = 0
    while index < len(argv):
        token = argv[index]
        flag = token.split("=", 1)[0]
        takes_value = "=" not in token
        if flag in _DROPPED_FLAGS:
            notes.append(f"{flag} ignored: {_DROPPED_FLAGS[flag]}")
            index += 2 if takes_value and index + 1 < len(argv) else 1
            continue
        if flag in _REDIRECT_FLAGS:
            forwarded.append(token)
            if takes_value and index + 1 < len(argv):
                forwarded.append(argv[index + 1])
                index += 2
            else:
                index += 1
            continue
        notes.append(f"{flag} ignored: not a `prepare-blueprint` option")
        index += 2 if takes_value and index + 1 < len(argv) else 1
    return forwarded, notes


@anchored("prepare-solution-blueprint")
def prepare_main(argv: list[str] | None = None) -> int:
    """Deprecated: delegates to the cloud-first `prepare-blueprint`.

    Two producers for one artifact is how they drift. `prepare-blueprint`
    (`core.blueprint.cli`) is the one that the intake playback gate, the
    provisioning planner and `confirm-blueprint` all read, so this legacy
    entry point forwards to it rather than writing a second blueprint of its
    own. Flags with no equivalent are dropped with a named reason on stderr,
    never silently.
    """
    from core.blueprint.cli import prepare_blueprint_main

    announce_deprecated_cli_redirect(
        "prepare-solution-blueprint",
        prefer="prepare-blueprint",
        reason="the cloud-first spine owns the blueprint artifact",
    )
    forwarded, notes = _forwardable_argv(list(argv or []))
    for note in notes:
        print(f"[~] {note}", file=sys.stderr)
    return prepare_blueprint_main(forwarded)


@anchored("apply-blueprint-answer")
def apply_main(argv: list[str] | None = None) -> int:
    from core.onboarding.workspace.cli_runner import run_workspace_command

    parser = argparse.ArgumentParser()
    parser.add_argument("--workspace", required=True)
    parser.add_argument("--repo-root", default=str(PROJECT_ROOT))
    parser.add_argument("--exclude", action="append", default=[])
    parser.add_argument("--include", action="append", default=[])
    parser.add_argument("--as-volume", action="append", default=[])
    parser.add_argument("--as-managed", action="append", default=[])
    parser.add_argument("--approve", action="store_true")
    parser.add_argument("--confirmed-by", default="")
    args = parser.parse_args(argv)

    def run() -> Any:
        if args.approve:
            return approve_blueprint(
                args.repo_root, args.workspace, confirmed_by=args.confirmed_by
            )
        prior = load_blueprint(
            WorkspaceLayout(project_root=(Path(args.repo_root).resolve() / args.workspace).resolve())
        )
        if not prior:
            raise FileNotFoundError(
                "no legacy blueprint to edit. `prepare-solution-blueprint` now "
                "redirects to `prepare-blueprint`, which produces the cloud-first "
                "blueprint that `confirm-blueprint` and `plan-provisioning` read -- "
                "this command only edits a legacy artifact produced before that "
                "redirect."
            )
        return build_blueprint(
            args.repo_root, args.workspace,
            source_root=str(prior.get("source_root") or ""),
            catalog=str(prior.get("catalog") or ""),
            ingestion_mode=str(prior.get("ingestion_mode") or "system"),
            bronze_schema=str((prior.get("schemas") or {}).get("bronze") or "bronze"),
            silver_schema=str((prior.get("schemas") or {}).get("silver") or "silver"),
            gold_schema=str((prior.get("schemas") or {}).get("gold") or "gold"),
            edits=BlueprintEdits(
                exclude=list(args.exclude), include=list(args.include),
                as_volume=list(args.as_volume), as_managed=list(args.as_managed),
            ),
        )

    return run_workspace_command(
        command="apply-blueprint-answer",
        workspace=args.workspace,
        repo_root=args.repo_root,
        fn=run,
        metadata={"approve": args.approve, "confirmed_by": args.confirmed_by},
    )


if __name__ == "__main__":
    raise SystemExit(prepare_main())
