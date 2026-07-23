"""
core/medallion/design.py — Design-medallion orchestrator.

Reads workspace inputs (domain_model, kpi_registry, kpi_feature_mapping,
profiles, derived_feature_reviews, workspace_feature_definitions,
semantic_contract), invokes the Medallion Architect agent, materializes
its proposal into Manifest + StarSchema + SilverContract + Lineage, and
emits Bronze/Silver SQL files for the DuckDB target (P0 scope).

Idempotent on `inputs_hash`: a run whose hash matches the prior manifest
skips work unless `--force`.

Decisions ratification: unconfirmed star-schema decisions are written to
`interns/reports/medallion_design_panel/current.{json,md}` for the user
to resolve before the (forthcoming) `build-medallion` step runs anything.

LLM unavailable / fails: a deterministic seed proposal is built from
profiles + KPI feature mapping + confirmed derived features so the run
still produces a reviewable Bronze layer and a starter Silver shell.
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

from core.failures import WorkflowBlockedError, validation_blocker
from core.medallion import (
    Manifest, BronzeTable, SilverTable, GoldTable,
    StarSchema, FactTable, DimensionTable, Relationship,
    SilverContract,
    Lineage, LineageNode, LineageEdge,
)
from core.medallion.manifest import compute_inputs_hash, manifest_to_yaml
from core.medallion.silver_contract import (
    TableContract, TypeCast, NullPolicy, DerivedColumn,
    Assertion,
)
from core.medallion.contracts_md import (
    render_star_schema_md, render_silver_contract_md, render_lineage_md,
)
from core.medallion.design_naming import (
    dataset_name_key,
    detect_natural_key,
    detect_watermark,
    logical_entity_from_path,
    safe_relative_posix,
    source_system_from_path,
    source_system_from_silver_source,
)
from core.storage.workspace_layout import WorkspaceLayout
from core.sql_safety import quote_ident_sql


# ── Exit codes (Section 19 of the PRD) ────────────────────────────────────────

class MedallionExit(Exception):
    """Exit with a deterministic code and a suggested next command."""

    def __init__(self, code: str, message: str, next_command: str = ""):
        super().__init__(message)
        self.code = code
        self.message = message
        self.next_command = next_command


EXIT_RUN_ONBOARD_FIRST       = "RUN_ONBOARD_FIRST"
EXIT_NO_KPIS_DEFINED         = "NO_KPIS_DEFINED"
EXIT_KPI_BLOCKERS_UNRESOLVED = "KPI_BLOCKERS_UNRESOLVED"
EXIT_EMPTY_WORKSPACE         = "EMPTY_WORKSPACE"
EXIT_WORKSPACE_BUSY          = "WORKSPACE_BUSY"
EXIT_BUDGET_EXCEEDED         = "BUDGET_EXCEEDED"
EXIT_MODEL_SEARCH_FAILED     = "MODEL_SEARCH_FAILED"
EXIT_INSUFFICIENT_MODEL_CAPABILITY = "INSUFFICIENT_MODEL_CAPABILITY"


# ── Result type ────────────────────────────────────────────────────────────────

@dataclass
class DesignResult:
    workspace: str
    inputs_hash: str
    medallion_dir: str
    manifest_path: str
    star_schema_path: str
    silver_contract_path: str
    lineage_path: str
    bronze_files: list[str] = field(default_factory=list)
    silver_files: list[str] = field(default_factory=list)
    unconfirmed_decisions: list[str] = field(default_factory=list)
    design_panel_path: str = ""
    cache_hit: bool = False
    llm_used: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "workspace": self.workspace,
            "inputs_hash": self.inputs_hash,
            "medallion_dir": self.medallion_dir,
            "manifest_path": self.manifest_path,
            "star_schema_path": self.star_schema_path,
            "silver_contract_path": self.silver_contract_path,
            "lineage_path": self.lineage_path,
            "bronze_file_count": len(self.bronze_files),
            "silver_file_count": len(self.silver_files),
            "unconfirmed_decision_count": len(self.unconfirmed_decisions),
            "design_panel_path": self.design_panel_path,
            "cache_hit": self.cache_hit,
            "llm_used": self.llm_used,
        }


# ── Workspace layout extension ─────────────────────────────────────────────────

def medallion_dirs(layout: WorkspaceLayout) -> dict[str, Path]:
    base = layout.generated_dir / "medallion"
    state = layout.state_dir / "medallion"
    return {
        "medallion":   base,
        "bronze":      base / "bronze",
        "silver":      base / "silver",
        "gold":        base / "gold",
        "state":       state,
        "runs":        state / "runs",
        "cache":       state / "medallion_cache",
        "data_bronze": state / "bronze",
        "data_silver": state / "silver",
        "data_gold":   state / "gold",
    }


# ── Entry point ────────────────────────────────────────────────────────────────

def design_medallion(
    workspace: Path,
    repo_root: Path,
    *,
    cfg=None,
    intern=None,
    cheap: bool = False,
    dry_run: bool = False,
    force: bool = False,
    engine: Optional[str] = None,
    model: Optional[str] = None,
    no_search: bool = False,
    calibrate: bool = False,
) -> DesignResult:
    """
    Main orchestrator. `intern` is an optional MedallionArchitectIntern
    instance; if None, the deterministic seed proposal is used.
    """
    workspace = workspace.resolve()
    repo_root = repo_root.resolve()
    layout = WorkspaceLayout(project_root=workspace)
    _preflight(layout)

    paths = medallion_dirs(layout)
    inputs = _load_workspace_inputs(layout)
    inputs_hash = compute_inputs_hash(_hashable_paths(layout))

    existing_manifest = _read_existing_manifest(paths["medallion"] / "manifest.yaml")
    if existing_manifest and existing_manifest.get("inputs_hash") == inputs_hash and not force:
        return DesignResult(
            workspace=str(workspace.relative_to(repo_root)) if workspace.is_relative_to(repo_root) else str(workspace),
            inputs_hash=inputs_hash,
            medallion_dir=str(paths["medallion"]),
            manifest_path=str(paths["medallion"] / "manifest.yaml"),
            star_schema_path=str(paths["medallion"] / "star_schema.json"),
            silver_contract_path=str(paths["medallion"] / "silver_contract.json"),
            lineage_path=str(paths["medallion"] / "lineage.json"),
            cache_hit=True,
        )

    if dry_run:
        return DesignResult(
            workspace=str(workspace.relative_to(repo_root)) if workspace.is_relative_to(repo_root) else str(workspace),
            inputs_hash=inputs_hash,
            medallion_dir=str(paths["medallion"]),
            manifest_path=str(paths["medallion"] / "manifest.yaml"),
            star_schema_path=str(paths["medallion"] / "star_schema.json"),
            silver_contract_path=str(paths["medallion"] / "silver_contract.json"),
            lineage_path=str(paths["medallion"] / "lineage.json"),
            cache_hit=False,
        )

    for d in paths.values():
        d.mkdir(parents=True, exist_ok=True)

    _prepare_model_routing(
        paths,
        cfg=cfg,
        engine=engine,
        model=model,
        no_search=no_search,
        calibrate=calibrate,
        cheap=cheap,
    )

    proposal, llm_used = _build_proposal(intern, inputs, cheap=cheap)
    workspace_name = workspace.name

    star_schema = _build_star_schema(workspace_name, proposal)
    silver_contract = _build_silver_contract(workspace_name, proposal)
    bronze_tables = _build_bronze_tables(inputs, semantic_contract=inputs.get("semantic_contract") or {}, repo_root=repo_root)
    silver_tables = _build_silver_table_entries(silver_contract, bronze_tables)
    gold_tables = _build_gold_tables(star_schema, silver_contract)

    manifest = Manifest(
        workspace=workspace_name,
        inputs_hash=inputs_hash,
        target="duckdb",
        bronze=bronze_tables,
        silver=silver_tables,
        gold=gold_tables,
    )

    lineage = _build_lineage(workspace_name, manifest, silver_contract)

    manifest_path = paths["medallion"] / "manifest.yaml"
    manifest_path.write_text(manifest_to_yaml(manifest), encoding="utf-8")

    star_schema_path = paths["medallion"] / "star_schema.json"
    star_schema_path.write_text(json.dumps(star_schema.to_dict(), indent=2), encoding="utf-8")
    (paths["medallion"] / "star_schema.md").write_text(render_star_schema_md(star_schema), encoding="utf-8")

    silver_contract_path = paths["medallion"] / "silver_contract.json"
    silver_contract_path.write_text(json.dumps(silver_contract.to_dict(), indent=2), encoding="utf-8")
    (paths["medallion"] / "silver_contract.md").write_text(render_silver_contract_md(silver_contract), encoding="utf-8")

    # SLA / freshness contract (Step 6: non-functional latency SLA). Per silver
    # table, the freshness column + a default batch-daily lag budget; the build
    # can assert max(now - freshness_col) <= max_lag_hours.
    sla_contract = _build_sla_contract(workspace_name, manifest, silver_contract)
    (paths["medallion"] / "sla_contract.json").write_text(
        json.dumps(sla_contract, indent=2), encoding="utf-8")
    # Freshness observability (non-fatal): per-table lag vs SLA, for the build's
    # observability pass to report (not a hard gate -- historical loads are stale
    # by design).
    (paths["medallion"] / "_freshness_checks.sql").write_text(
        _emit_freshness_checks(sla_contract), encoding="utf-8")
    # Requirements-derived architecture decisions: run the dataops decision engine
    # over THIS workspace's measured volume + shape so the recommended compute /
    # table-format / modeling / SCD are recorded per workspace instead of a single
    # RCM-shaped default. Advisory artifact; never blocks the build.
    arch_decisions = _build_architecture_decisions(workspace, workspace_name, star_schema)
    (paths["medallion"] / "architecture_decisions.json").write_text(
        json.dumps(arch_decisions, indent=2), encoding="utf-8")
    # Storage strategy (compression / partitioning / compaction), derived from
    # measured volume + the event column. Written for the emitters to apply and
    # for operators to see WHY (e.g. partitioning skipped on a small workspace to
    # avoid the small-file problem).
    storage_strategy = _build_storage_strategy(arch_decisions, sla_contract)
    (paths["medallion"] / "storage_strategy.json").write_text(
        json.dumps(storage_strategy, indent=2), encoding="utf-8")
    # Referential-integrity checks from PROVEN foreign keys: each FK becomes a
    # silver orphan-row query (child FK value with no matching parent PK). Run as
    # a NON-FATAL integrity report at build (not a per-table blocking gate -- that
    # would impose a cross-table build order). Matches the framework's "verify the
    # order's restaurant id exists in the master directory" relational check.
    (paths["medallion"] / "_referential_integrity.sql").write_text(
        _emit_referential_integrity_checks(inputs.get("relationship_contracts"),
                                           silver_contract),
        encoding="utf-8")

    lineage_path = paths["medallion"] / "lineage.json"
    lineage_path.write_text(json.dumps(lineage.to_dict(), indent=2), encoding="utf-8")
    (paths["medallion"] / "lineage.md").write_text(render_lineage_md(lineage), encoding="utf-8")

    bronze_files = _emit_bronze_sql_duckdb(manifest, paths["bronze"], repo_root, workspace)
    silver_files = _emit_silver_sql_duckdb(manifest, silver_contract, paths["silver"])

    # P2: emit Gold DuckDB SQL + Spark files for all layers
    _emit_gold_sql_all(manifest, paths, repo_root, workspace, workspace_name,
                       silver_contract, storage_strategy)

    # P5: rebuild lineage with column-level edges parsed from emitted SQL
    lineage = _build_lineage_with_columns(workspace_name, manifest, silver_contract, paths)
    lineage_path.write_text(json.dumps(lineage.to_dict(), indent=2), encoding="utf-8")
    (paths["medallion"] / "lineage.md").write_text(render_lineage_md(lineage), encoding="utf-8")

    unconfirmed = star_schema.unconfirmed_decisions()
    design_panel_path = ""
    if unconfirmed:
        design_panel_path = _write_design_panel(layout, star_schema, silver_contract)

    return DesignResult(
        workspace=str(workspace.relative_to(repo_root)) if workspace.is_relative_to(repo_root) else str(workspace),
        inputs_hash=inputs_hash,
        medallion_dir=str(paths["medallion"]),
        manifest_path=str(manifest_path),
        star_schema_path=str(star_schema_path),
        silver_contract_path=str(silver_contract_path),
        lineage_path=str(lineage_path),
        bronze_files=[str(p) for p in bronze_files],
        silver_files=[str(p) for p in silver_files],
        unconfirmed_decisions=unconfirmed,
        design_panel_path=design_panel_path,
        cache_hit=False,
        llm_used=llm_used,
    )


# ── Preflight ──────────────────────────────────────────────────────────────────

def _preflight(layout: WorkspaceLayout) -> None:
    if not layout.project_root.exists():
        raise MedallionExit(
            EXIT_EMPTY_WORKSPACE,
            f"Workspace does not exist: {layout.project_root}",
        )
    domain_model = layout.contracts_dir / "domain_model.json"
    profile_index = layout.profiles_dir / "profile_index.json"
    if not domain_model.exists() or not profile_index.exists():
        raise MedallionExit(
            EXIT_RUN_ONBOARD_FIRST,
            f"Missing onboarding artifacts (domain_model.json / profile_index.json) under {layout.generated_dir}",
            next_command=f"uv run onboard-workspace --workspace {layout.project_root.name}",
        )
    kpi_registry = layout.contracts_dir / "kpi_registry.json"
    if not kpi_registry.exists():
        raise MedallionExit(
            EXIT_NO_KPIS_DEFINED,
            "No KPI registry found.",
            next_command=f"uv run resolve-kpi-features --workspace {layout.project_root.name}",
        )
    try:
        registry = json.loads(kpi_registry.read_text(encoding="utf-8"))
        if isinstance(registry, dict) and not (registry.get("kpis") or registry.get("rules") or registry.get("items")):
            raise MedallionExit(EXIT_NO_KPIS_DEFINED, "KPI registry is empty.")
    except json.JSONDecodeError as exc:
        raise WorkflowBlockedError(
            validation_blocker(
                "medallion_design.preflight",
                f"Invalid KPI registry JSON: {exc}",
                next_command=f"uv run onboard-workspace --workspace {layout.project_root.name}",
            )
        ) from exc


def _hashable_paths(layout: WorkspaceLayout) -> list[Path]:
    # Medallion structure (bronze/silver/gold) depends on the datasets, domain
    # model, semantic contract, and accepted feature definitions -- NOT on KPI
    # resolution. kpi_registry / kpi_feature_mapping are intentionally excluded:
    # re-resolving KPIs (a normal pipeline step) rewrites the mapping and would
    # otherwise make this manifest spuriously stale on every run.
    candidates = [
        layout.contracts_dir / "domain_model.json",
        layout.contracts_dir / "semantic_contract.json",
        layout.contracts_dir / "workspace_feature_definitions.json",
        layout.profiles_dir / "profile_index.json",
    ]
    review_dir = layout.reports_dir / "derived_feature_reviews" / "json"
    if review_dir.exists():
        candidates.extend(sorted(review_dir.glob("*.json")))
    return candidates


# ── Input loading ──────────────────────────────────────────────────────────────

def _load_workspace_inputs(layout: WorkspaceLayout) -> dict[str, Any]:
    def _read(path: Path) -> Optional[Any]:
        if not path.exists():
            return None
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            raise WorkflowBlockedError(
                validation_blocker(
                    "medallion_design.load_inputs",
                    f"Invalid JSON artifact: {path}: {exc}",
                    next_command=f"uv run validate-workspace-artifacts --workspace {layout.project_root.name}",
                )
            ) from exc

    inputs: dict[str, Any] = {
        "domain_model": _read(layout.contracts_dir / "domain_model.json"),
        "kpi_registry": _read(layout.contracts_dir / "kpi_registry.json"),
        "kpi_feature_mapping": _read(layout.contracts_dir / "kpi_feature_mapping.json"),
        "semantic_contract": _read(layout.contracts_dir / "semantic_contract.json"),
        "workspace_feature_definitions": _read(layout.contracts_dir / "workspace_feature_definitions.json"),
        "relationship_contracts": _read(layout.contracts_dir / "relationship_contracts.json"),
        "profile_index": _read(layout.profiles_dir / "profile_index.json"),
    }
    review_dir = layout.reports_dir / "derived_feature_reviews" / "json"
    reviews: list[dict[str, Any]] = []
    if review_dir.exists():
        for p in sorted(review_dir.glob("*.json")):
            data = _read(p)
            if data:
                data["_source_path"] = str(p)
                reviews.append(data)
    inputs["derived_feature_reviews"] = reviews
    return inputs


def _read_existing_manifest(path: Path) -> Optional[dict[str, Any]]:
    if not path.exists():
        return None
    text = path.read_text(encoding="utf-8")
    m = re.search(r"^inputs_hash:\s*\"?(sha256:[0-9a-f]+)\"?", text, re.MULTILINE)
    return {"inputs_hash": m.group(1)} if m else None


# ── Proposal: LLM or deterministic seed ────────────────────────────────────────

def _build_proposal(intern, inputs: dict[str, Any], *, cheap: bool) -> tuple[dict[str, Any], bool]:
    if intern is None or cheap:
        return _seed_proposal(inputs), False
    try:
        proposal = intern.design(inputs)
        if not isinstance(proposal, dict) or "star_schema" not in proposal:
            return _seed_proposal(inputs), False
        return proposal, True
    except Exception:
        return _seed_proposal(inputs), False


def _prepare_model_routing(
    paths: dict[str, Path],
    *,
    cfg,
    engine: Optional[str],
    model: Optional[str],
    no_search: bool,
    calibrate: bool,
    cheap: bool,
) -> dict[str, Any]:
    from core.medallion.model_classifier import ModelCapability, ModelSearchFailed, classify
    from core.medallion.model_discovery import DiscoveredModel, discover_models
    from core.medallion.tier_router import InsufficientModelCapability, assign_tiers, pick_model, rank_models

    selected_engine = engine or getattr(cfg, "main_agent", "") or "api"
    if selected_engine == "api":
        selected_engine = "gemini-api"

    if model:
        discovered = [DiscoveredModel(engine=selected_engine, model_id=model)]
    elif cheap:
        discovered = [DiscoveredModel(engine=selected_engine, model_id="cheap-seed")]
    else:
        discovered = discover_models(selected_engine, cfg=cfg)

    if not discovered:
        if no_search:
            raise MedallionExit(
                EXIT_MODEL_SEARCH_FAILED,
                f"No discovered models for engine `{selected_engine}` and --no-search was set.",
            )
        discovered = [DiscoveredModel(engine=selected_engine, model_id="unclassified-runtime-model")]

    capabilities: list[ModelCapability] = []
    cache_hits = 0
    for discovered_model in discovered:
        try:
            cap = classify(discovered_model.engine, discovered_model.model_id, no_search=True)
            cache_hits += 1
        except ModelSearchFailed as exc:
            if no_search:
                raise MedallionExit(EXIT_MODEL_SEARCH_FAILED, str(exc)) from exc
            cap = ModelCapability(
                engine=discovered_model.engine,
                model_id=discovered_model.model_id,
                classified_at="",
                evidence=["runtime discovery without cached WebSearch classification"],
                confidence=0.0,
            )
        capabilities.append(cap)

    ranking = rank_models(capabilities)
    assignment = assign_tiers(ranking)
    try:
        selected_tier, selected_model = pick_model("star_schema_design", assignment)
    except InsufficientModelCapability as exc:
        raise MedallionExit(EXIT_INSUFFICIENT_MODEL_CAPABILITY, str(exc)) from exc

    metadata = {
        "engine": selected_engine,
        "pinned_model": model or "",
        "no_search": no_search,
        "calibrate": calibrate,
        "cache_hits": cache_hits,
        "discovered_models": [
            {"engine": item.engine, "model_id": item.model_id, "raw_metadata": item.raw_metadata}
            for item in discovered
        ],
        "capabilities": [cap.to_dict() for cap in capabilities],
        "ranking": ranking,
        "tier_assignment": assignment.by_tier,
        "selected": {
            "task_class": "star_schema_design",
            "tier": selected_tier,
            "model_id": selected_model,
        },
    }
    run_dir = paths["runs"] / datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ-p4")
    run_dir.mkdir(parents=True, exist_ok=True)
    (run_dir / "discovered_models.json").write_text(json.dumps(metadata, indent=2), encoding="utf-8")
    (paths["state"] / "discovered_models_latest.json").write_text(json.dumps(metadata, indent=2), encoding="utf-8")
    return metadata


_ENTITY_METADATA_COLS = frozenset({"_source_system", "_source_file", "_load_ts"})


def _dataset_column_set(ds: dict[str, Any]) -> set[str]:
    schema = ds.get("schema")
    cols: set[str] = set()
    if isinstance(schema, dict):
        cols = {str(k).lower() for k in schema.keys()}
    elif isinstance(schema, list):
        cols = {str(c.get("name")).lower() for c in schema if isinstance(c, dict) and c.get("name")}
    return cols - _ENTITY_METADATA_COLS


def _schema_compatible(a: set[str], b: set[str], *, min_overlap: float = 0.75) -> bool:
    """Two dataset schemas are compatible enough to union as one logical
    entity if they share at least `min_overlap` of the smaller schema's
    columns. Filename similarity alone (the grouping key) doesn't imply
    this -- a dimension/master table and an unrelated event/reference table
    can share a filename prefix while having almost no columns in common.
    The threshold is high (0.75, not a bare majority) because genuine
    same-entity-multiple-sources data shares nearly its whole schema
    (ratio close to 1.0); a lower bar lets a single shared foreign/lookup
    key (e.g. a reference table's code column) alone cross it, which is
    exactly the false-positive this function exists to reject.
    """
    if not a or not b:
        return False
    return (len(a & b) / min(len(a), len(b))) >= min_overlap


def _resolve_dataset_logical_names(datasets: list[dict[str, Any]]) -> dict[int, str]:
    """Final logical-entity name per dataset (keyed by `id(dataset)`), after
    `_split_schema_incompatible_groups` has separated any naive filename-
    prefix group whose members aren't actually the same entity.

    Both bronze table naming (`_build_bronze_tables`) and silver contract
    building (`_seed_proposal`) must resolve entity names through this same
    function -- computing the split independently in each (as before) let
    them disagree, silently dropping the bronze-source mapping for any
    entity `_seed_proposal` split out. Relies on the same dataset dicts
    (from one `inputs["domain_model"]["datasets"]` list, loaded once per
    `design_medallion` call) flowing into both callers.
    """
    by_logical: dict[str, list[dict[str, Any]]] = {}
    for ds in datasets:
        logical = _logical_entity_from_path(ds.get("path", ""))
        by_logical.setdefault(logical, []).append(ds)
    by_logical = _split_schema_incompatible_groups(by_logical)
    resolved: dict[int, str] = {}
    for logical, dss in by_logical.items():
        for ds in dss:
            resolved[id(ds)] = logical
    return resolved


def _split_schema_incompatible_groups(
    by_logical: dict[str, list[dict[str, Any]]],
) -> dict[str, list[dict[str, Any]]]:
    """`_logical_entity_from_path` groups datasets purely by filename prefix
    (e.g. driver_masters.csv + driver_people.csv -> both "driver"), blind to
    whether the schemas are actually the same entity from multiple sources
    vs. two unrelated tables sharing a word. Unioning incompatible schemas
    crashes DuckDB ("Set operations can only apply to expressions with the
    same number of result columns") or, worse, silently merges garbage when
    column counts coincidentally match. Re-split any group whose members
    aren't schema-compatible with the group's first dataset, keyed by full
    filename stem so each split-out dataset gets its own logical entity.
    """
    result: dict[str, list[dict[str, Any]]] = {}
    for logical, dss in by_logical.items():
        if len(dss) <= 1:
            result[logical] = dss
            continue
        anchor_cols = _dataset_column_set(dss[0])
        result[logical] = [dss[0]]
        for ds in dss[1:]:
            if _schema_compatible(anchor_cols, _dataset_column_set(ds)):
                result[logical].append(ds)
            else:
                stem = Path(ds.get("path", "")).stem.lower() or logical
                result.setdefault(stem, []).append(ds)
    return result


def _seed_proposal(inputs: dict[str, Any]) -> dict[str, Any]:
    """
    Deterministic minimal proposal built from profiles + KPI mapping + confirmed
    derived feature reviews. Not as good as LLM, but produces a runnable Bronze
    layer and a starter Silver shell for review.
    """
    domain_model = inputs.get("domain_model") or {}
    datasets = domain_model.get("datasets", []) if isinstance(domain_model, dict) else []
    semantic = inputs.get("semantic_contract") or {}
    pii_lookup = _pii_lookup_from_semantic(semantic)

    silver_tables: dict[str, Any] = {}
    resolved_names = _resolve_dataset_logical_names(datasets)
    by_logical: dict[str, list[dict[str, Any]]] = {}
    for ds in datasets:
        by_logical.setdefault(resolved_names[id(ds)], []).append(ds)

    for logical, dss in by_logical.items():
        pii_cols = sorted({
            c
            for ds in dss
            for c in _sensitive_cols_for_dataset(
                pii_lookup, _dataset_name_key(ds), ds.get("schema")
            )
        })
        # Derive the data contract from the actual schema (deterministic, generic):
        #  - key_rename canonicalises the raw natural key (e.g. Id) to <entity>_id
        #    so the dedup_keys / MERGE / assertions reference a column that exists;
        #  - type_casts convert temporal columns to TIMESTAMP in Silver.
        schema_cols = _merged_schema_cols(dss)
        nat_key = _detect_natural_key({c: "" for c in schema_cols}, logical)
        if nat_key:
            # Single natural key -> canonicalise it to <entity>_id.
            canonical_pk = f"{logical}_id"
            key_rename = {nat_key[0]: canonical_pk}
            pk_cols = [canonical_pk]
            null_policies = {canonical_pk: "error"}
        else:
            # No single id: dedup on the full row (composite key). No canonical
            # rename, and no not-null policy on a single PK column.
            key_rename = {}
            pk_cols = [c for c in schema_cols if c not in pii_cols] or list(schema_cols)
            null_policies = {}
        type_casts = _seed_type_casts(schema_cols, exclude=set(pii_cols) | set(key_rename))
        silver_tables[logical] = {
            "primary_key": ["source_system", *pk_cols],
            "type_casts": type_casts,
            # PK must never be null -- enforced as a hard assertion (no_null_pk).
            "null_policies": null_policies,
            "dedup_keys": ["source_system", *pk_cols],
            "pii_hash_columns": pii_cols,
            "key_rename": key_rename,
            "derived_columns": {},
            # Accuracy (DQ dimension): money/quantity columns must be non-negative.
            "assertions": _seed_accuracy_assertions(schema_cols, exclude=set(pii_cols)),
        }

    confirmed_reviews = [r for r in inputs.get("derived_feature_reviews", []) if isinstance(r, dict)]
    for review in confirmed_reviews:
        if review.get("feature_state") != "user_confirmed":
            continue
        opts = review.get("derived_feature_options") or []
        if not opts:
            continue
        opt = opts[0]
        col_name = (opt.get("derived_column_name") or "").lower() or "derived"
        kpi_id = review.get("kpi_id", "")
        formula_tmpl = opt.get("formula_templates") or {}
        inputs_block = opt.get("input_columns") or []
        host_table = _silver_host_for_inputs(inputs_block, list(silver_tables.keys()))
        if host_table not in silver_tables:
            continue
        derived = silver_tables[host_table].setdefault("derived_columns", {})
        if col_name in derived:
            derived[col_name].setdefault("computed_once_reused_by_kpis", []).append(kpi_id)
            continue
        derived[col_name] = {
            "formula_templates": {
                "duckdb_sql": formula_tmpl.get("duckdb_sql", opt.get("formula", "")),
                "spark_sql":  formula_tmpl.get("spark_sql"),
                "polars":     formula_tmpl.get("polars"),
            },
            "input_columns": [
                {
                    "column": ic.get("column", ""),
                    "dataset": ic.get("dataset", ""),
                    "role": ic.get("role", ic.get("input_name", "")),
                    "dtype": ic.get("dtype", ""),
                }
                for ic in inputs_block
            ],
            "business_meaning": opt.get("business_meaning", ""),
            "reasoning": f"Lifted from {review.get('_source_path','')} (user_confirmed).",
            "source_review": review.get("_source_path", ""),
            "computed_once_reused_by_kpis": [kpi_id] if kpi_id else [],
        }

    # Classify entities into facts vs dimensions from PROVEN foreign-key evidence
    # (relationship_contracts.json). An entity that is the TARGET of a high-
    # confidence FK is a dimension (it is referenced/looked-up); the rest stay
    # facts. This builds a real star schema from evidence instead of treating
    # every table as an isolated fact. No fabrication: only proven_data_model /
    # user_confirmed relationships above the confidence floor are used.
    star_rels, dim_entities = _star_from_relationships(
        inputs.get("relationship_contracts"), set(silver_tables.keys())
    )
    fact_entities = [e for e in sorted(silver_tables.keys()) if e not in dim_entities]
    fk_by_fact: dict[str, dict[str, str]] = {}
    for rel in star_rels:
        fk_by_fact.setdefault(rel["_left_entity"], {})[rel["from_column"]] = (
            f"dim_{rel['_right_entity']}"
        )
    # Strip the internal helper fields so each relationship matches Relationship.from_dict.
    star_rels = [{k: v for k, v in r.items() if not k.startswith("_")} for r in star_rels]
    facts = [{
        "name": logical,
        "grain": f"one row per source row from silver.{logical}",
        "source_silver_tables": [f"silver.{logical}"],
        "measures": [],
        "foreign_keys": fk_by_fact.get(logical, {}),
        "reasoning": (
            "Fact entity (not a FK target). Foreign keys lifted from proven "
            "relationship_contracts." if logical in fk_by_fact
            else "Fact entity; no outgoing proven foreign keys."
        ),
        "evidence_sources": ["relationship_contracts"] if logical in fk_by_fact else ["seed_fallback"],
        "needs_user_confirmation": True,
    } for logical in fact_entities]
    dimensions = [{
        "name": entity,
        "source_silver_tables": [f"silver.{entity}"],
        # SCD Type 1 by default (overwrite); a mutating dimension can be promoted
        # to Type 2 at ratification. Recorded so the gold emitter knows the policy.
        "scd_type": 1,
        "reasoning": "Dimension entity: target of a proven foreign key.",
        "evidence_sources": ["relationship_contracts"],
        "needs_user_confirmation": True,
    } for entity in sorted(dim_entities)]

    return {
        "star_schema": {
            "facts": facts,
            "dimensions": dimensions,
            "relationships": star_rels,
            "conformed_dimensions": sorted(dim_entities),
            "derivation_reasoning": (
                "Deterministic seed proposal (no LLM call). Every fact mirrors a Silver table. "
                "Human ratification required for grain, dimension extraction, and relationships."
            ),
            "open_questions": [
                "No dimensions were extracted automatically; review which entities should become conformed dimensions.",
                "No fact measures inferred; declare them per KPI before build-medallion.",
            ],
        },
        "silver_tables": silver_tables,
    }


# Substrings that reliably indicate a temporal column. Deliberately NOT "birth"/
# "death" (they match BIRTHPLACE/DEATHPLACE, which are locations) -- BIRTHDATE/
# DEATHDATE are caught by "date".
_TEMPORAL_NAME_SUBSTR = ("date", "time", "timestamp", "datetime", "_at")
# Exact column names that are temporal on their own (avoids matching e.g.
# STARTING_BID via a loose "start" substring).
_TEMPORAL_NAME_EXACT = ("start", "stop", "dob", "ts")


def _merged_schema_cols(dss: list[dict[str, Any]]) -> dict[str, str]:
    """Union of {column_name: dtype} across a logical entity's datasets. Tolerates
    both dict-shaped (name->dtype) and list-shaped ([{name,dtype}]) schemas."""
    cols: dict[str, str] = {}
    for ds in dss:
        schema = ds.get("schema")
        if isinstance(schema, dict):
            for name, dtype in schema.items():
                cols[str(name)] = str(dtype)
        elif isinstance(schema, list):
            for col in schema:
                if isinstance(col, dict) and col.get("name"):
                    cols[str(col["name"])] = str(col.get("dtype") or col.get("type") or "")
    return cols


# Reuse the project's canonical financial vocabulary (was a private, slightly
# insurance-leaning duplicate). 'amount'/'cost' cover paid_amount, claim_cost, etc.
from core.onboarding.workspace.research import GENERIC_FINANCIAL_SEED as _MONEY_HINTS


def _seed_accuracy_assertions(schema_cols: dict[str, str], *, exclude: set[str]) -> list[dict[str, Any]]:
    """Accuracy DQ checks: monetary columns must be non-negative. Money columns
    are inherently numeric, so this keys off the (generic) name hint and stays
    safe when dtype metadata is absent. PII-hashed columns are excluded."""
    out: list[dict[str, Any]] = []
    for name in schema_cols:
        if name in exclude:
            continue
        if any(h in name.lower() for h in _MONEY_HINTS):
            out.append({
                "id": f"nonneg_{name.lower()}",
                "type": "range",
                "columns": [name],
                "min_value": 0,
            })
    return out


def _seed_type_casts(schema_cols: dict[str, str], *, exclude: set[str]) -> dict[str, Any]:
    """Deterministic type casts: temporal-looking columns -> TIMESTAMP. Generic
    (name + dtype heuristics, no domain words); PII-hashed and renamed PK columns
    are excluded so the cast never collides with their projection."""
    casts: dict[str, Any] = {}
    for name, dtype in schema_cols.items():
        if name in exclude:
            continue
        dl = name.lower()
        dt = (dtype or "").lower()
        is_temporal = (
            "date" in dt or "time" in dt
            or any(h in dl for h in _TEMPORAL_NAME_SUBSTR)
            or dl in _TEMPORAL_NAME_EXACT
        )
        if is_temporal:
            casts[name] = {"from": dtype or "string", "to": "TIMESTAMP"}
    return casts


_STAR_FK_CONFIDENCE_FLOOR = 0.9
_STAR_FK_STATES = {"proven_data_model", "user_confirmed"}


def _star_from_relationships(
    relationship_contracts: Any, entities: set[str]
) -> tuple[list[dict[str, Any]], set[str]]:
    """Derive (relationships, dimension-entities) from proven foreign keys.

    A FK target above the confidence floor is a dimension (it is looked up). Only
    proven_data_model / user_confirmed relationships are used -- candidates are
    advisory and never promote an entity to a dimension. Self-references and
    cross-dimension (low-confidence Id-collision) links are excluded. Generic.
    """
    rels: list[dict[str, Any]] = []
    dim_entities: set[str] = set()
    if not isinstance(relationship_contracts, dict):
        return rels, dim_entities

    def _entity(path: str) -> str:
        return _logical_entity_from_path(path)

    for r in relationship_contracts.get("relationships") or []:
        if not isinstance(r, dict):
            continue
        if str(r.get("state")) not in _STAR_FK_STATES:
            continue
        try:
            conf = float(r.get("confidence") or 0)
        except (TypeError, ValueError):
            conf = 0.0
        if conf < _STAR_FK_CONFIDENCE_FLOOR:
            continue
        if str(r.get("relationship_type")) not in ("foreign_key", ""):
            continue
        left = _entity(str(r.get("left_dataset") or ""))
        right = _entity(str(r.get("right_dataset") or ""))
        if not left or not right or left == right:
            continue
        if left not in entities or right not in entities:
            continue
        rels.append({
            "from_table": f"silver.{left}",
            "from_column": str(r.get("left_column") or ""),
            "to_table": f"silver.{right}",
            "to_column": str(r.get("right_column") or ""),
            "cardinality": "many_to_one",
            "confidence": conf,
            "evidence": [f"relationship_contracts:{r.get('relationship_id', '')}"],
            "needs_user_confirmation": True,
            # internal helper fields (stripped before Relationship.from_dict)
            "_left_entity": left,
            "_right_entity": right,
        })
        dim_entities.add(right)
    # An entity that is BOTH a FK target and a FK source (e.g. encounters, which
    # procedures reference but which itself references patients) stays a FACT --
    # it carries measures and is the grain. Only pure lookup targets are dims.
    fk_sources = {rel["_left_entity"] for rel in rels}
    dim_entities -= fk_sources
    return rels, dim_entities


def _pii_lookup_from_semantic(semantic: Any) -> dict[str, list[str]]:
    """Map dataset_key -> sensitive column names. The ``"*"`` key holds globally
    declared sensitive columns (no dataset attribution); callers attribute them
    to a dataset by intersecting with that dataset's schema (see
    :func:`_sensitive_cols_for_dataset`).

    Handles the FLAT ``columns.<name>.is_sensitive`` shape the onboarder writes
    (a dict, not a list) in addition to the nested/list shapes. Previously the
    flat dict was silently ignored (it failed the ``isinstance(list)`` check), so
    medallion Bronze PII tagging got nothing from a real onboarded contract.
    Ref: core-audit ob-workspace-b.md.
    """
    out: dict[str, list[str]] = {}
    if not isinstance(semantic, dict):
        return out
    columns_field = semantic.get("columns")
    fields = semantic.get("fields") or columns_field or []
    if isinstance(fields, list):
        for f in fields:
            if not isinstance(f, dict):
                continue
            if f.get("pii") or f.get("is_pii"):
                ds_key = f.get("dataset", "*")
                out.setdefault(ds_key, []).append(f.get("name", ""))
    # Flat onboarder shape: columns.<name>.is_sensitive (dict, not list).
    if isinstance(columns_field, dict):
        for name, meta in columns_field.items():
            if isinstance(meta, dict) and (meta.get("is_sensitive") or meta.get("pii")):
                out.setdefault("*", []).append(str(name))
    datasets = semantic.get("datasets") or {}
    if isinstance(datasets, dict):
        for k, v in datasets.items():
            if not isinstance(v, dict):
                continue
            pii_cols = v.get("pii_columns") or v.get("pii") or []
            if isinstance(pii_cols, list):
                out.setdefault(k, []).extend(pii_cols)
    return out


def _schema_column_names(schema: Any) -> dict[str, str]:
    """Return {lowercased_name: original_name} for a dataset schema (dict or list)."""
    names: dict[str, str] = {}
    if isinstance(schema, dict):
        for key in schema.keys():
            names[str(key).lower()] = str(key)
    elif isinstance(schema, list):
        for col in schema:
            if isinstance(col, dict) and col.get("name"):
                names[str(col["name"]).lower()] = str(col["name"])
    return names


def _sensitive_cols_for_dataset(
    pii_lookup: dict[str, list[str]], dataset_key: str, schema: Any
) -> list[str]:
    """Sensitive columns for one dataset: its dataset-keyed entries UNION any
    globally-declared (``"*"``) sensitive column that exists in this dataset's
    schema. The global set is how the flat onboarder contract (no per-dataset
    attribution) maps onto each table."""
    cols: set[str] = {c for c in pii_lookup.get(dataset_key, []) if c}
    global_sensitive = pii_lookup.get("*", [])
    if global_sensitive:
        schema_names = _schema_column_names(schema)
        for name in global_sensitive:
            match = schema_names.get(str(name).lower())
            if match:
                cols.add(match)
    return sorted(cols)


def _dataset_name_key(ds: dict[str, Any]) -> str:
    return dataset_name_key(ds)


def _logical_entity_from_path(path: str) -> str:
    return logical_entity_from_path(path)


def _silver_host_for_inputs(input_cols: list[dict[str, Any]], silver_table_names: list[str]) -> str:
    for ic in input_cols:
        ds = (ic.get("dataset") or "").lower()
        for name in silver_table_names:
            if name and name in ds:
                return name
    return silver_table_names[0] if silver_table_names else "entity"


# ── Conversion to dataclasses ──────────────────────────────────────────────────

def _build_star_schema(workspace_name: str, proposal: dict[str, Any]) -> StarSchema:
    raw = proposal.get("star_schema") or {}
    facts = [FactTable.from_dict(f) for f in raw.get("facts", [])]
    dims = [DimensionTable.from_dict(d) for d in raw.get("dimensions", [])]
    rels = [Relationship.from_dict(r) for r in raw.get("relationships", [])]
    return StarSchema(
        workspace=workspace_name,
        facts=facts,
        dimensions=dims,
        relationships=rels,
        conformed_dimensions=list(raw.get("conformed_dimensions", [])),
        derivation_reasoning=raw.get("derivation_reasoning", ""),
        open_questions=list(raw.get("open_questions", [])),
    )


def _build_sla_contract(workspace_name: str, manifest: Manifest, contract: SilverContract) -> dict[str, Any]:
    """Freshness/latency SLA per silver table. The freshness column is a temporal
    (TIMESTAMP-cast) column, preferring an event-start column; the default budget
    is batch-daily. Generic; no domain assumptions."""
    tables: list[dict[str, Any]] = []
    for s in manifest.silver:
        tc = contract.tables.get(s.name)
        if tc is None:
            continue
        temporal = list(tc.type_casts.keys())
        fresh = None
        for pref in ("start", "event", "created", "order", "timestamp", "date"):
            fresh = next((c for c in temporal if pref in c.lower()), None)
            if fresh:
                break
        if not fresh and temporal:
            fresh = temporal[0]
        tables.append({
            "table": f"silver.{s.name}",
            "freshness_column": fresh,
            "max_lag_hours": 24,
            "mode": "batch",
            "availability_target": "99.0%",
        })
    return {
        "artifact_type": "sla_contract.json",
        "version": 1,
        "generated_by": "medallion-design",
        "workspace": workspace_name,
        "default_latency_sla": {"mode": "batch", "max_lag_hours": 24},
        "retention_policy": {"bronze_days": "forever", "silver_days": 365, "gold_days": 730},
        "tables": tables,
    }


# Partition only above this volume: below it, directory-per-partition creates the
# small-file problem (more overhead than scan savings). Generic threshold.
_PARTITION_VOLUME_FLOOR = 1 * 1024 * 1024 * 1024  # 1 GB


def _build_storage_strategy(arch_decisions: dict[str, Any], sla_contract: dict[str, Any]) -> dict[str, Any]:
    """Compression / partitioning / compaction strategy from measured volume +
    the event column. zstd compression always (better analytical reads than the
    snappy default); partitioning + OPTIMIZE only above the volume floor, so a
    small workspace is not over-partitioned into tiny files. Generic."""
    total_bytes = int((arch_decisions.get("measured") or {}).get("total_source_bytes") or 0)
    large = total_bytes >= _PARTITION_VOLUME_FLOOR
    # Event column for the partition key: the first table's freshness column.
    event_col = None
    for t in sla_contract.get("tables") or []:
        if t.get("freshness_column"):
            event_col = t["freshness_column"]
            break
    partition_by = None
    if large and event_col:
        # Partition by the MONTH of the event column (date-grain directories);
        # raw per-day/timestamp would be too granular at the directory level.
        partition_by = {"column": event_col, "granularity": "month"}
    return {
        "artifact_type": "storage_strategy.json",
        "version": 1,
        "generated_by": "medallion-design",
        "measured_total_bytes": total_bytes,
        "compression": "zstd",
        "partition_by": partition_by,
        "optimize": bool(large),
        "rationale": (
            f"{_human_bytes_simple(total_bytes)} >= 1 GB: partition by "
            f"month({event_col}) + OPTIMIZE compaction."
            if partition_by else
            f"{_human_bytes_simple(total_bytes)} < 1 GB: no partitioning "
            "(directory-per-partition would create the small-file problem); "
            "zstd compression only."
        ),
    }


def _human_bytes_simple(n: int) -> str:
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if abs(n) < 1024:
            return f"{n:.0f} {unit}"
        n /= 1024
    return f"{n:.0f} PB"


def _build_architecture_decisions(
    workspace: Path, workspace_name: str, star_schema: StarSchema
) -> dict[str, Any]:
    """Run the dataops decision engine over the workspace's measured volume +
    shape, recording per-workspace architecture recommendations (compute,
    table-format, modeling, SCD, batch/stream). Advisory; degrades gracefully if
    the vendor engine is unavailable. Generic -- no domain assumptions."""
    # Measured daily volume = total bytes of the raw source files (a conservative
    # one-day proxy; the engine's thresholds are order-of-magnitude).
    total_bytes = 0
    try:
        for p in workspace.glob("*.csv"):
            total_bytes += p.stat().st_size
        for p in workspace.glob("*.parquet"):
            total_bytes += p.stat().st_size
    except OSError:
        pass
    volume_str = f"{total_bytes}B"
    # Shape signals from the star schema: a dimension that mutates -> SCD2 worth it.
    has_dimensions = bool(getattr(star_schema, "dimensions", []))
    out: dict[str, Any] = {
        "artifact_type": "architecture_decisions.json",
        "version": 1,
        "generated_by": "medallion-design",
        "workspace": workspace_name,
        "measured": {"total_source_bytes": total_bytes},
        "decisions": {},
    }
    try:
        import sys as _sys
        from core.paths import PROJECT_ROOT as _ROOT
        vendor = str(_ROOT / "vendor")
        if vendor not in _sys.path:
            _sys.path.insert(0, vendor)
        from minus_dataops import decisions as _dec

        calls = {
            "compute_engine": {"volume_per_day": volume_str},
            "table_format": {"concurrent_writes": True, "needs_acid": True},
            "storage_format": {"workload": "analytics"},
            "modeling_shape": {"predictable_dashboard": True, "many_consumers": has_dimensions},
            "scd_type": {"track_history": has_dimensions},
            "batch_vs_stream": {"latency": "1d"},
        }
        for rule, kwargs in calls.items():
            try:
                d = _dec.decide(rule, **kwargs)
                out["decisions"][rule] = {
                    "recommendation": d.recommendation,
                    "rationale": d.rationale,
                    "confidence": d.confidence,
                }
            except Exception as exc:  # pragma: no cover - defensive per-rule
                out["decisions"][rule] = {"error": str(exc)}
    except Exception as exc:  # pragma: no cover - vendor optional
        out["engine_unavailable"] = str(exc)
    return out


def _emit_referential_integrity_checks(
    relationship_contracts: Any, contract: SilverContract
) -> str:
    """One orphan-row query per PROVEN foreign key: count child rows whose FK
    value has no matching parent PK in silver. Non-fatal observability. Joins on
    the CANONICAL silver key (the contract's key_rename), so the parent column is
    the renamed <entity>_id, not the raw Id."""
    rels, _dims = _star_from_relationships(
        relationship_contracts, set(contract.tables.keys()))
    rename_by_entity = {
        e: dict(getattr(tc, "key_rename", {}) or {})
        for e, tc in contract.tables.items()
    }

    def _canon(entity: str, col: str) -> str:
        return rename_by_entity.get(entity, {}).get(col, col)

    lines = ["-- Referential integrity (proven FKs). Non-fatal: report orphans.\n"]
    for r in rels:
        child_e, parent_e = r["_left_entity"], r["_right_entity"]
        child_col = r["from_column"]                 # FK col on the child (fact)
        parent_col = _canon(parent_e, r["to_column"])  # canonical PK on the parent
        rid = f"fk_{child_e}_{child_col}__{parent_e}".lower()
        lines.append(
            f"SELECT '{rid}' AS assertion_id, COUNT(*) AS violations\n"
            f"FROM silver.{child_e} c\n"
            f"LEFT JOIN silver.{parent_e} p "
            f'ON p."{parent_col}" = c."{child_col}"\n'
            f'WHERE c."{child_col}" IS NOT NULL AND p."{parent_col}" IS NULL;\n'
        )
    return "\n".join(lines)


def _emit_freshness_checks(sla_contract: dict[str, Any]) -> str:
    """SQL that reports each table's freshness lag against its SLA. Observability
    (lag vs budget), not a fatal assertion."""
    lines = ["-- Freshness observability (lag vs SLA). Non-fatal: report, don't block.\n"]
    for entry in sla_contract.get("tables") or []:
        table = str(entry.get("table") or "")
        col = entry.get("freshness_column")
        sla = entry.get("max_lag_hours", 24)
        if not table or not col:
            continue
        tname = table.split(".", 1)[-1]
        lines.append(
            f"SELECT '{tname}' AS table_name,\n"
            f"       date_diff('hour', max(\"{col}\"), now()) AS lag_hours,\n"
            f"       {sla} AS sla_hours,\n"
            f"       date_diff('hour', max(\"{col}\"), now()) > {sla} AS breached\n"
            f"FROM silver.{tname};\n"
        )
    return "\n".join(lines)


def _build_silver_contract(workspace_name: str, proposal: dict[str, Any]) -> SilverContract:
    raw_tables = proposal.get("silver_tables") or {}
    tables: dict[str, TableContract] = {}
    for tname, body in raw_tables.items():
        if not isinstance(body, dict):
            continue
        type_casts = {
            col: TypeCast.from_dict(v) for col, v in (body.get("type_casts") or {}).items()
            if isinstance(v, dict)
        }
        null_policies = {
            col: NullPolicy.from_str(str(v)) for col, v in (body.get("null_policies") or {}).items()
        }
        derived = {
            name: DerivedColumn.from_dict(name, v)
            for name, v in (body.get("derived_columns") or {}).items()
            if isinstance(v, dict)
        }
        assertions_in = body.get("assertions") or []
        assertions = [Assertion.from_dict(a) for a in assertions_in if isinstance(a, dict)]
        pk = list(body.get("primary_key") or [])
        dedup = list(body.get("dedup_keys") or pk)
        pii = list(body.get("pii_hash_columns") or [])
        if not any(a.id == "pk_unique" for a in assertions) and pk:
            assertions.append(Assertion(id="pk_unique", type="unique", columns=pk))
        # no_null_pk only for columns the contract declares NOT-NULL (a canonical
        # single PK gets `null_policies[pk] = error`). A COMPOSITE dedup key over a
        # full row must NOT force every column non-null -- nullable columns (e.g.
        # an optional reason code) are legitimate, so only `pk_unique` (dedup)
        # applies there. The not-null check covers the declared-error columns plus
        # source_system (the partition key), never the whole composite key.
        not_null_cols = [c for c in pk
                         if c == "source_system"
                         or (c in null_policies and null_policies[c].policy == "error")]
        has_declared_pk = any(c != "source_system" and c in null_policies
                              and null_policies[c].policy == "error" for c in pk)
        if has_declared_pk and not any(a.id == "no_null_pk" for a in assertions):
            assertions.append(Assertion(id="no_null_pk", type="not_null", columns=not_null_cols))
        # Completeness (DQ dimension): a silver load that yields zero rows is a
        # silent pipeline failure -- assert the table is non-empty.
        if not any(a.id == "not_empty" for a in assertions):
            assertions.append(Assertion(id="not_empty", type="not_empty"))
        tables[tname] = TableContract(
            type_casts=type_casts,
            null_policies=null_policies,
            dedup_keys=dedup,
            pii_hash_columns=pii,
            key_rename=dict(body.get("key_rename") or {}),
            derived_columns=derived,
            assertions=assertions,
        )
    return SilverContract(workspace=workspace_name, tables=tables)


def _build_bronze_tables(inputs: dict[str, Any], *, semantic_contract: Any, repo_root: Path) -> list[BronzeTable]:
    domain_model = inputs.get("domain_model") or {}
    datasets = domain_model.get("datasets", []) if isinstance(domain_model, dict) else []
    pii_by_ds = _pii_lookup_from_semantic(semantic_contract)
    resolved_names = _resolve_dataset_logical_names(datasets)
    out: list[BronzeTable] = []
    for ds in datasets:
        path = ds.get("path", "")
        if not path:
            continue
        source_system = _source_system_from_path(path)
        logical = resolved_names[id(ds)]
        name = f"{logical}__{source_system}" if source_system else logical
        schema = ds.get("schema", {}) or {}
        natural_key = _detect_natural_key(schema, logical)
        if not natural_key:
            # No single-column id (an event table keyed by a composite, or a
            # non-entity file). Fall back to ALL columns as a composite natural
            # key: a deterministic dedup key (drops exact-duplicate rows) instead
            # of an empty key that breaks the contract + validator. Generic.
            cols = list(schema.keys()) if isinstance(schema, dict) else [
                str(c.get("name")) for c in schema if isinstance(c, dict) and c.get("name")
            ]
            natural_key = [c for c in cols if c]
        watermark = _detect_watermark(schema)
        pii_cols = _sensitive_cols_for_dataset(pii_by_ds, Path(path).stem, schema)
        rel_path = _safe_relative_posix(Path(path), repo_root)
        out.append(BronzeTable(
            name=name,
            source_file=rel_path,
            source_system=source_system,
            watermark_column=watermark,
            natural_key=natural_key,
            pii_columns=pii_cols,
        ))
    return out


def _source_system_from_path(path: str) -> str:
    return source_system_from_path(path)


def _detect_natural_key(schema: dict[str, Any], logical: str) -> list[str]:
    return detect_natural_key(schema, logical)


def _detect_watermark(schema: dict[str, Any]) -> Optional[str]:
    return detect_watermark(schema)


def _build_silver_table_entries(contract: SilverContract, bronze: list[BronzeTable]) -> list[SilverTable]:
    by_logical: dict[str, list[BronzeTable]] = {}
    for b in bronze:
        logical = b.name.split("__", 1)[0]
        by_logical.setdefault(logical, []).append(b)
    out: list[SilverTable] = []
    for sname, tc in contract.tables.items():
        sources = by_logical.get(sname, [])
        derived_from = [f"bronze.{b.name}" for b in sources]
        out.append(SilverTable(
            name=sname,
            derived_from=derived_from,
            primary_key=tc.dedup_keys or ["source_system", f"{sname}_id"],
            contract=f"silver_contract.json#/{sname}",
        ))
    return out


def _build_gold_tables(star_schema: StarSchema, contract: SilverContract | None = None) -> list[GoldTable]:
    # Silver canonicalises a single natural key (Id -> <entity>_id), so a FK that
    # targets the raw `Id` must join on the renamed canonical column in gold.
    rename_by_entity: dict[str, dict[str, str]] = {}
    if contract is not None:
        for entity, tc in contract.tables.items():
            rename_by_entity[entity] = dict(getattr(tc, "key_rename", {}) or {})

    def _silver_col(table: str, col: str) -> str:
        entity = table.split(".", 1)[-1]
        return rename_by_entity.get(entity, {}).get(col, col)

    # Index proven relationships by their source (fact) silver table so each fact
    # gold table carries the joins that denormalise it into a wide OBT.
    fks_by_source: dict[str, list[dict[str, str]]] = {}
    for r in star_schema.relationships:
        fks_by_source.setdefault(r.from_table, []).append({
            "from_column": _silver_col(r.from_table, r.from_column),
            "to_table": r.to_table,        # silver.<dim>
            "to_column": _silver_col(r.to_table, r.to_column),
        })
    out: list[GoldTable] = []
    for f in star_schema.facts:
        name = f.name if f.name.startswith("fact_") else f"fact_{f.name}"
        fks = []
        for src in f.source_silver_tables:
            fks.extend(fks_by_source.get(src, []))
        out.append(GoldTable(
            name=name,
            kind="fact",
            derived_from=list(f.source_silver_tables),
            grain=f.grain,
            foreign_keys=fks,
        ))
    for d in star_schema.dimensions:
        name = d.name if d.name.startswith("dim_") else f"dim_{d.name}"
        out.append(GoldTable(
            name=name,
            kind="dimension",
            derived_from=list(d.source_silver_tables),
            scd_type=d.scd_type,
        ))
    return out


def _build_lineage(workspace_name: str, manifest: Manifest, contract: SilverContract) -> Lineage:
    nodes: list[LineageNode] = []
    for b in manifest.bronze:
        nodes.append(LineageNode(layer="bronze", table=b.name, columns=list(b.natural_key)))
    for s in manifest.silver:
        cols = list(s.primary_key)
        tc = contract.tables.get(s.name)
        if tc:
            cols += sorted(tc.derived_columns.keys())
        nodes.append(LineageNode(layer="silver", table=s.name, columns=cols))
    for g in manifest.gold:
        nodes.append(LineageNode(layer="gold", table=g.name, columns=[]))
    edges: list[LineageEdge] = []
    for s in manifest.silver:
        for src in s.derived_from:
            edges.append(LineageEdge(
                from_node=src,
                from_columns=[],
                to_node=f"silver.{s.name}",
                to_columns=list(s.primary_key),
                transform_type="union+dedup+pii_hash",
                reasoning="Silver unifies multi-source Bronze with composite PK and hashed PII.",
            ))
    for g in manifest.gold:
        for src in g.derived_from:
            edges.append(LineageEdge(
                from_node=src,
                from_columns=[],
                to_node=f"gold.{g.name}",
                to_columns=[],
                transform_type="full_refresh",
                reasoning=f"{g.kind} built from Silver via full refresh.",
            ))
    return Lineage(workspace=workspace_name, nodes=nodes, edges=edges)


# ── P2: Gold SQL + Spark emission ─────────────────────────────────────────────

def _emit_gold_sql_all(
    manifest: Manifest,
    paths: dict[str, Any],
    repo_root: Path,
    workspace: Path,
    workspace_name: str,
    silver_contract: SilverContract,
    storage_strategy: dict[str, Any] | None = None,
) -> None:
    """Emit Gold DuckDB SQL and Spark files for all layers (P2)."""
    from core.medallion.delta_emitter import (
        emit_gold_duckdb, emit_bronze_spark, emit_silver_spark, emit_gold_spark,
    )
    paths["gold"].mkdir(parents=True, exist_ok=True)

    for g in manifest.gold:
        emit_gold_duckdb(g, paths["gold"])

    # Spark files — emitted for all targets; build-medallion --target delta executes them
    for b in manifest.bronze:
        emit_bronze_spark(b, paths["bronze"], workspace_name, repo_root, workspace,
                          storage_strategy=storage_strategy)
    for s in manifest.silver:
        emit_silver_spark(s, paths["silver"], workspace_name, silver_contract,
                          storage_strategy=storage_strategy)
    for g in manifest.gold:
        emit_gold_spark(g, paths["gold"], workspace_name,
                        storage_strategy=storage_strategy)


# ── P5: Column-level lineage from parsed SQL ──────────────────────────────────

def _build_lineage_with_columns(
    workspace_name: str,
    manifest: Manifest,
    silver_contract: SilverContract,
    paths: dict[str, Any],
) -> "Lineage":
    """Build lineage with real column-level edges parsed from emitted SQL files."""
    from core.medallion.sql_lineage_parser import extract_edges_from_sql
    from core.medallion.spark_lineage_parser import extract_edges_from_spark_py

    # Start with the placeholder lineage (layer-level nodes are still correct)
    lineage = _build_lineage(workspace_name, manifest, silver_contract)

    new_edges: list = []
    layer_dirs = [
        (paths["bronze"], "bronze."),
        (paths["silver"], "silver."),
        (paths["gold"],   "gold."),
    ]
    for layer_dir, prefix in layer_dirs:
        if not layer_dir.exists():
            continue
        for sql_path in layer_dir.glob("*.duckdb.sql"):
            stem = sql_path.stem.replace(".duckdb", "")
            target = prefix + stem
            sql = sql_path.read_text(encoding="utf-8")
            edges = extract_edges_from_sql(sql, target_table=target)
            new_edges.extend(edges)
        for py_path in layer_dir.glob("*.spark.py"):
            stem = py_path.stem.replace(".spark", "")
            target = prefix + stem
            src = py_path.read_text(encoding="utf-8")
            edges = extract_edges_from_spark_py(src, target_table=target)
            new_edges.extend(edges)

    if new_edges:
        # Replace placeholder edges with parsed column-level edges
        lineage.edges = new_edges

    return lineage


# ── SQL emission (DuckDB target, P0) ──────────────────────────────────────────

def _emit_bronze_sql_duckdb(manifest: Manifest, out_dir: Path, repo_root: Path, workspace: Path) -> list[Path]:
    out: list[Path] = []
    for b in manifest.bronze:
        # source_file is repo-relative (set by _build_bronze_tables via _safe_relative_posix)
        absolute_csv = Path(b.source_file) if Path(b.source_file).is_absolute() else (repo_root / b.source_file)
        relative_csv = _safe_relative_posix(absolute_csv, repo_root)
        sql_path = out_dir / f"{b.name}.duckdb.sql"
        sql = (
            f"-- bronze.{b.name}: append-watermarked from {b.source_system}\n"
            f"CREATE OR REPLACE TABLE bronze.{b.name} AS\n"
            f"SELECT\n"
            f"    *,\n"
            f"    '{b.source_system}' AS _source_system,\n"
            f"    '{relative_csv}' AS _source_file,\n"
            f"    current_timestamp AS _load_ts\n"
            f"FROM read_csv_auto('{relative_csv}', HEADER=TRUE);\n"
        )
        sql_path.write_text(sql, encoding="utf-8")
        out.append(sql_path)
    return out


def _emit_silver_sql_duckdb(manifest: Manifest, contract: SilverContract, out_dir: Path) -> list[Path]:
    out: list[Path] = []
    for s in manifest.silver:
        tc = contract.tables.get(s.name)
        if tc is None:
            continue
        sources = s.derived_from
        if not sources:
            continue
        select_lines: list[str] = []
        for src in sources:
            select_lines.append(f"    SELECT *, '{_source_system_from_silver_src(src)}' AS source_system FROM {src}")
        union_sql = "\n    UNION ALL\n".join(select_lines)
        pii_replacements = [
            f"sha256(coalesce(cast({quote_ident_sql(col)} AS VARCHAR), '') || $salt) AS {quote_ident_sql(col)}"
            for col in tc.pii_hash_columns
        ]
        # Canonical PK rename (raw `Id` -> `<entity>_id`): project it explicitly and
        # EXCLUDE the raw column from the star so the PK the dedup/MERGE/assertions
        # key on actually exists.
        rename_items = [f"    {quote_ident_sql(raw)} AS {canon}," for raw, canon in tc.key_rename.items()]
        exclude_cols = [raw for raw in tc.key_rename]
        # Contract type casts (e.g. temporal -> TIMESTAMP), applied via REPLACE.
        # TRY_CAST (not CAST): a single malformed value nulls that cell instead of
        # aborting the whole silver load -- a stray non-date in a temporal column
        # should not fail the pipeline. Source/alias are quoted (quote_ident_sql):
        # an unquoted reserved-word column (e.g. a real "START"/"END" column) is
        # otherwise a SQL parser error, not a hypothetical.
        cast_replacements = [
            f"TRY_CAST({quote_ident_sql(col)} AS {cast.to_type}) AS {quote_ident_sql(col)}"
            for col, cast in tc.type_casts.items()
            if col not in tc.pii_hash_columns and col not in exclude_cols
        ]
        replacements = cast_replacements + pii_replacements
        exclude_clause = (
            f" EXCLUDE ({', '.join(quote_ident_sql(c) for c in exclude_cols)})" if exclude_cols else ""
        )
        replace_clause = (
            "\n    REPLACE (\n        " + ",\n        ".join(replacements) + "\n    )"
            if replacements else ""
        )
        derived_lines: list[str] = []
        for dname, dc in tc.derived_columns.items():
            duck = dc.formula_templates.duckdb_sql or ""
            if duck:
                derived_lines.append(f"    {duck} AS {dname},")
        derived_block = "\n".join(derived_lines)
        assertion_path = out_dir / f"_{s.name}_assertions.sql"
        assertion_path.write_text(_render_assertions_sql(s.name, tc), encoding="utf-8")
        sql_path = out_dir / f"{s.name}.duckdb.sql"
        body = (
            f"-- silver.{s.name}: cleaned from {', '.join(sources)}\n"
            f"-- Contract applied: PK canonicalisation + type casts + PII hashing\n"
            f"-- (silver_contract.json#/{s.name}). Build rewrites this to an\n"
            f"-- idempotent MERGE-on-PK (merge_emitter.emit_silver_merge).\n"
            f"CREATE OR REPLACE TABLE silver.{s.name} AS\n"
            f"WITH unioned AS (\n{union_sql}\n)\n"
            f"SELECT\n"
            + ("\n".join(rename_items) + "\n" if rename_items else "")
            + (derived_block + "\n" if derived_block else "")
            + f"    *{exclude_clause}{replace_clause}\n"
            + "FROM unioned;\n\n"
            + f"\n-- Post-load assertions: see {assertion_path.name}\n"
        )
        sql_path.write_text(body, encoding="utf-8")
        out.append(sql_path)
    return out


def _source_system_from_silver_src(src: str) -> str:
    return source_system_from_silver_source(src)


def _render_assertions_sql(table: str, tc: TableContract) -> str:
    lines = [f"-- Assertions for silver.{table}\n"]
    for a in tc.assertions:
        if a.type == "not_null" and a.columns:
            cols = " OR ".join(f"{quote_ident_sql(c)} IS NULL" for c in a.columns)
            lines.append(
                f"SELECT '{a.id}' AS assertion_id, COUNT(*) AS violations\n"
                f"FROM silver.{table} WHERE {cols};\n"
            )
        elif a.type == "unique" and a.columns:
            group = ", ".join(quote_ident_sql(c) for c in a.columns)
            lines.append(
                f"SELECT '{a.id}' AS assertion_id, COUNT(*) AS violations FROM (\n"
                f"    SELECT {group} FROM silver.{table} GROUP BY {group} HAVING COUNT(*) > 1\n"
                f");\n"
            )
        elif a.type == "referential_integrity" and a.child and a.parent:
            parent_table, parent_col = a.parent.rsplit(".", 1)
            child_table, child_col = a.child.rsplit(".", 1)
            parent_col_q = quote_ident_sql(parent_col)
            child_col_q = quote_ident_sql(child_col)
            lines.append(
                f"SELECT '{a.id}' AS assertion_id, COUNT(*) AS violations\n"
                f"FROM {child_table} c\n"
                f"LEFT JOIN {parent_table} p ON p.{parent_col_q} = c.{child_col_q}\n"
                f"WHERE c.{child_col_q} IS NOT NULL AND p.{parent_col_q} IS NULL;\n"
            )
        elif a.type == "range" and a.columns:
            # Accuracy: each value must fall within [min_value, max_value].
            col = quote_ident_sql(a.columns[0])
            conds = []
            if a.min_value is not None:
                conds.append(f"{col} < {a.min_value}")
            if a.max_value is not None:
                conds.append(f"{col} > {a.max_value}")
            if conds:
                lines.append(
                    f"SELECT '{a.id}' AS assertion_id, COUNT(*) AS violations\n"
                    f"FROM silver.{table} WHERE {' OR '.join(conds)};\n"
                )
        elif a.type == "not_empty":
            # Completeness: the table must carry rows after the load.
            lines.append(
                f"SELECT '{a.id}' AS assertion_id, "
                f"CASE WHEN COUNT(*) = 0 THEN 1 ELSE 0 END AS violations\n"
                f"FROM silver.{table};\n"
            )
    return "\n".join(lines)


def _safe_relative_posix(path: Path, root: Path) -> str:
    return safe_relative_posix(path, root)


# ── Design panel ───────────────────────────────────────────────────────────────

def _write_design_panel(layout: WorkspaceLayout, schema: StarSchema, contract: SilverContract) -> str:
    panel_dir = layout.reports_dir / "medallion_design_panel"
    panel_dir.mkdir(parents=True, exist_ok=True)
    unconfirmed = schema.unconfirmed_decisions()
    panel_obj = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "workspace": schema.workspace,
        "open_count": len(unconfirmed),
        "items": [{"id": uid, "kind": uid.split(":", 1)[0]} for uid in unconfirmed],
        "open_questions": list(schema.open_questions),
    }
    (panel_dir / "current.json").write_text(json.dumps(panel_obj, indent=2), encoding="utf-8")
    md_lines = [f"# Medallion Design Panel — {schema.workspace}", "",
                f"Open ratification items: **{len(unconfirmed)}**", ""]
    for uid in unconfirmed:
        md_lines.append(f"- `{uid}`")
    md_lines.append("")
    if schema.open_questions:
        md_lines.append("## Open questions")
        md_lines.append("")
        for q in schema.open_questions:
            md_lines.append(f"- {q}")
        md_lines.append("")
    (panel_dir / "current.md").write_text("\n".join(md_lines) + "\n", encoding="utf-8")
    return str(panel_dir / "current.json")
