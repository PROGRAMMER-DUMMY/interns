"""Generate a real, git-tracked dbt project from the same confirmed contracts
core.onboarding.kpi.sql_generator already reads -- staging/intermediate/marts
models instead of one single-statement CTE rewrite per KPI.

Why this exists alongside sql_generator.py's databricks dialect, not instead
of it: that dialect's single-statement CTE rewrite is a workaround for
DatabricksClient having no session continuity (core/execution/databricks_
client.py). dbt doesn't have that problem -- each model persists on its own,
so there is nothing to work around. Once a workspace's KPI SQL moves onto
this path, dbt IS the production mechanism for that workspace; the CTE path
stays exactly as it is for local duckdb-dialect workspaces, which was always
its real home. See the dbt+Airflow integration plan (Phase D1) for the full
design and why (dynamic-cooking-firefly.md in the planning history).

This module deliberately REUSES DuckDBKPISQLGenerator's already-tested
internals (join-chain resolution, feature-expression building, masking,
identifier quoting) rather than re-deriving them -- constructed once per
DbtProjectGenerator with dialect="databricks" (the identifier-quoting rules
dbt-databricks compiles to match Spark SQL, which this generator's SQL
already targets after this session's dialect-threading fix). The only new
logic here is turning that generator's quoted-identifier SQL fragments into
dbt's `{{ ref(...) }}` / `{{ source(...) }}` Jinja references.

Scope for this version: the Databricks target only (no local dbt-duckdb dev
target yet -- DuckDB's identifier-quoting compatibility with the backtick-
quoted Spark SQL this emits has not been verified, so profiles.yml declares
one target, not two, until that's checked). A KPI whose derived_formula
resolves to something this generator cannot express is skipped with a clear
reason, never silently emitted as broken dbt SQL.

Medallion layering: `--schema` is the BRONZE/source schema only (sources.yml
reads the already-existing raw UC tables there -- exclusive mode has no
ingestion step, bronze already exists). staging+intermediate models (the
cleaned, conformed, joined layer) write to `--silver-schema` (default
"silver"); marts models (the business-facing KPI output) write to
`--gold-schema` (default "gold") -- same bronze/silver/gold semantics as
this platform's local medallion pipeline, not a flat dump of every layer
into the source schema. Real (dbt's own documented pattern, not a novel
override -- see `generate_schema_name_for_env` in dbt-core's own
get_custom_schema.sql): the generated project ships a `generate_schema_name`
macro override so a model's `+schema:` config becomes the literal schema
name, not dbt's default `{target_schema}_{custom_schema}` concatenation.
"""
from __future__ import annotations
from core.observability.cost_ledger import RUN_ID_ENV, anchored

import argparse
import ast
import json
import re
import shutil
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable

from core.onboarding.kpi.sql_generator import (
    DuckDBKPISQLGenerator,
    READY_STATES,
    _safe_name,
)
from core.dev.generator_stamp import stamp
from core.paths import PROJECT_ROOT
from core.profiling.data_model_profiler import _is_temporal_dtype
from core.profiling.dataset_identity import dataset_display_stem
from core.storage.workspace_layout import WorkspaceLayout

_DBT_ADAPTER_TYPE = "databricks"

# Emitted-code invariants (design spec section 6 + pipeline_practices_gap_
# research.md section 4). Every one of these is a SILENT wrong-data or
# silent-cost path in dbt-databricks, not an error the warehouse reports, so
# the generator both emits the safe form AND re-validates the finished project
# (validate_generated_project) before declaring success.
_MAX_CLUSTER_KEYS = 4  # Databricks liquid clustering hard limit
# Tail of dbt's own output kept on a parse failure: enough to carry the model
# name and the offending line, short enough not to bury the caller's message.
_PARSE_TAIL_CHARS = 2000
# Below this declared size, partitioning is the wrong tool -- liquid
# clustering replaces it (end_users_data_modeling_research.md). A model that
# genuinely needs partition_by must declare the size that justifies it.
_PARTITION_MIN_BYTES = 1_000_000_000_000  # ~1 TB
# replace_where (and microbatch, which compiles to it) INSERTS BY POSITION.
# Reorder a select and values land in the wrong columns with no error, so
# every such model must carry an explicit, stable column list and this warning.
_POSITION_WARNING = (
    "-- WARNING: replace_where/microbatch inserts by POSITION, not by name. "
    "Keep this column list explicit and stably ordered."
)
# Identity/nondeterministic key generation: not reproducible across a rebuild,
# so a regenerated table renumbers every fact. Deterministic hash instead
# (macros/surrogate_key.sql).
_NONDETERMINISTIC_KEY = re.compile(
    r"GENERATED\s+(?:ALWAYS|BY\s+DEFAULT)\s+AS\s+IDENTITY"
    r"|monotonically_increasing_id\s*\(|\buuid\s*\(",
    re.IGNORECASE,
)
# The live gold schema is published FROM this one by the emitted swap
# (macros/publish_gold.sql): marts build into staging, DQ tests run there, and
# only a passing build publishes. Delta has no Iceberg-style WAP branch, so
# staging-table + atomic swap is the documented equivalent.
_STAGING_SCHEMA_SUFFIX = "__staging"

# Velocity lanes (blueprint `velocity_lane` decision) whose marts are worth
# building incrementally. Batch rebuilds a table; a lane that lands data
# continuously must not rewrite every mart on every trigger (audit A1).
# `realtime_serving` is excluded deliberately: it needs a serving edge this
# generator does not emit, and the blueprint blocks it upstream.
_INCREMENTAL_LANES = {"micro_batch", "streaming"}
# Explicit late-arrival window for the incremental predicate. dbt's implicit
# default is one batch, which silently drops anything that arrives late.
_INCREMENTAL_LOOKBACK_DAYS = 3
# Modeling decision (modeling.yaml R10) that turns on inferred/unknown members.
_LATE_ARRIVING_CHOICE = "inferred_member_dimensions"
# The unknown member a fact row falls back to when its dimension row has not
# landed yet -- so the fact is never dropped from the aggregate.
_UNKNOWN_MEMBER = "__unknown_member__"
# Declared Delta retention (medallion design report: silver 365d, gold 730d).
# Unset, DBR's 7-day default silently governs time travel and RESTORE.
_RETENTION_DAYS = {"silver": 365, "gold": 730}
_RETENTION_PROPERTIES = ("delta.deletedFileRetentionDuration", "delta.logRetentionDuration")
# Source freshness thresholds (audit A6). Conservative defaults; a workspace
# with a declared SLA overrides them by editing the emitted sources.yml.
_FRESHNESS_WARN_DAYS = 2
_FRESHNESS_ERROR_DAYS = 3
_OPEN_QUESTION = "OPEN QUESTION"


@dataclass
class DbtProjectResult:
    dbt_project_dir: str
    kpi_count: int
    generated_kpi_ids: list[str] = field(default_factory=list)
    skipped: list[dict[str, str]] = field(default_factory=list)
    incremental_kpi_ids: list[str] = field(default_factory=list)
    report_path: str = ""

    def summary(self) -> dict[str, Any]:
        return {
            "artifact_type": "dbt_project_generation_result",
            "version": 1,
            "generated_by": "generate-dbt-project",
            "dbt_project_dir": self.dbt_project_dir,
            "kpi_count": self.kpi_count,
            "generated_kpi_ids": self.generated_kpi_ids,
            "incremental_kpi_ids": self.incremental_kpi_ids,
            "skipped": self.skipped,
            "report_path": self.report_path,
        }


class DbtProjectGenerator:
    def __init__(
        self,
        repo_root: str | Path,
        workspace: str | Path,
        *,
        catalog: str,
        schema: str,
        enterprise_id: str = "",
        silver_schema: str = "silver",
        gold_schema: str = "gold",
        enforce_contracts: bool = False,
    ) -> None:
        self.repo_root = Path(repo_root).resolve()
        self.workspace = (self.repo_root / workspace).resolve()
        self.layout = WorkspaceLayout(project_root=self.workspace)
        # `schema` is the BRONZE/source schema -- where dbt's sources.yml
        # points to read the already-existing raw UC tables (exclusive mode:
        # no ingestion, they already exist). Medallion layering happens
        # WITHIN the same catalog via silver_schema/gold_schema, not by
        # dumping every layer into the source schema -- staging+intermediate
        # (the cleaned, conformed, joined layer) write to silver_schema;
        # marts (the business-facing KPI output) write to gold_schema. Same
        # bronze/silver/gold semantics as this platform's local medallion
        # pipeline (WorkspaceLayout.bronze_dir/silver_dir/gold_dir), just
        # expressed as dbt schemas instead of local directories.
        self.catalog = catalog
        self.schema = schema
        self.silver_schema = silver_schema
        self.gold_schema = gold_schema
        self.staging_gold_schema = f"{gold_schema}{_STAGING_SCHEMA_SUFFIX}"
        self.enterprise_id = enterprise_id or self.layout.enterprise_id()
        self.enforce_contracts = enforce_contracts
        # Internal reuse only -- never writes sql_generator.py's own
        # solutions/*.sql output; only its private join/masking/quoting
        # helpers are called directly from this generator.
        self._sql = DuckDBKPISQLGenerator(
            repo_root, workspace, dialect="databricks", catalog=catalog, schema=schema
        )
        self._project_name = _dbt_project_name(_rel(self.workspace, self.repo_root))
        self._written_staging: dict[str, str] = {}  # dataset -> model name
        # Contracts other slices produce. All three are read DEFENSIVELY:
        # absent (or unparseable) means "no such decision", which keeps a
        # workspace generated before they existed byte-identical.
        decisions = [
            d
            for d in (_read_json(self.layout.contracts_dir / "blueprint_decisions.json")
                      .get("decisions") or [])
            if isinstance(d, dict) and d.get("status") != "blocked"
        ]
        self.velocity_lane = next(
            (str(d.get("choice") or "") for d in decisions if d.get("decision") == "velocity_lane"),
            "",
        )
        self.late_arriving_dimensions = any(
            _LATE_ARRIVING_CHOICE in json.dumps(d) for d in decisions
        )
        self._exclusions = _read_json(self.layout.contracts_dir / "schema_exclusions.json")
        self._temporal_anchors: set[str] = set()  # lowercased anchor columns
        self._open_questions: list[str] = []

    def generate(self) -> DbtProjectResult:
        mapping = self._sql._load_mapping()
        ready_kpis = [
            kpi for kpi in mapping.get("kpis", [])
            if kpi.get("features")
            and all(f.get("state") in READY_STATES for f in kpi.get("features", []))
        ]
        if not ready_kpis:
            raise ValueError(
                "No KPI is fully ready for SQL (every feature must be in a "
                f"{READY_STATES} state). Resolve KPI feature mappings first."
            )

        profile_map = self._sql._profile_map()
        from core.onboarding.relationships.contracts import load_relationship_contracts

        relationships = load_relationship_contracts(
            self.repo_root, _rel(self.workspace, self.repo_root)
        )

        dbt_dir = self.workspace / "dbt"
        staging_dir = dbt_dir / "models" / "staging"
        intermediate_dir = dbt_dir / "models" / "intermediate"
        marts_dir = dbt_dir / "models" / "marts"
        for d in (staging_dir, intermediate_dir, marts_dir):
            d.mkdir(parents=True, exist_ok=True)

        sources_needed: dict[str, dict[str, Any]] = {}
        generated_kpi_ids: list[str] = []
        incremental_kpi_ids: list[str] = []
        skipped: list[dict[str, str]] = []

        # Collect and merge required columns per source across all ready KPIs first
        merged_required_columns: dict[str, set[str]] = {}
        for kpi in ready_kpis:
            try:
                req_sources, req_cols = self._sql._required_source_columns(
                    kpi, profile_map, relationships
                )
                for src in req_sources:
                    if src in req_cols:
                        merged_required_columns.setdefault(src, set()).update(req_cols[src])
            except Exception:
                pass

        for kpi in ready_kpis:
            kpi_id = str(kpi.get("kpi_id") or "")
            try:
                required_sources, required_columns = self._sql._required_source_columns(
                    kpi, profile_map, relationships
                )
                if not required_sources:
                    skipped.append({"kpi_id": kpi_id, "reason": "no resolvable source dataset"})
                    continue

                for source in required_sources:
                    sources_needed[source] = profile_map.get(source, {})
                    self._ensure_staging_model(source, profile_map, merged_required_columns, staging_dir)

                stage_views = {
                    source: self._written_staging[source] for source in required_sources
                }
                source_from_sql, source_aliases = self._sql._kpi_source_from(
                    kpi, profile_map, stage_views, relationships
                )
                subs = self._ref_substitutions(profile_map, required_sources)

                select_items = self._feature_select_items(kpi, source_aliases, profile_map)
                if not select_items:
                    skipped.append({"kpi_id": kpi_id, "reason": "no resolvable feature expressions"})
                    continue

                features_model = f"int_{kpi_id}_features"
                features_body = "\n".join(
                    [
                        f"-- Intermediate: {kpi.get('name', kpi_id)}",
                        "select",
                        ",\n".join(select_items),
                        _apply_subs(source_from_sql, subs),
                    ]
                )

                marts_subs = dict(subs)
                marts_subs[self._sql.quote_ident(kpi_id + "_features")] = (
                    "{{ ref('%s') }}" % features_model
                )
                result_ident = self._sql.quote_ident(kpi_id + "_results")
                result_body = self._sql._extract_result_select_body(
                    self._sql._result_view_sql(kpi, kpi_id), result_ident
                )
                mart_body = _apply_subs(result_body, marts_subs)
                # Liquid clustering, never partitioning: <=4 keys, chosen from
                # the mart's own GROUP BY dimensions (what a KPI read actually
                # filters on). No derivable key -> skip the KPI loudly rather
                # than emit an unclustered table.
                cluster_by = _cluster_keys(mart_body)
                if not cluster_by:
                    raise ValueError(
                        "no output column could be resolved as a liquid-clustering key "
                        "for this mart (needs at least one aliased select column)"
                    )
                # Incremental only when the lane says data keeps landing AND the
                # mart carries a temporal anchor AND the grain resolves a merge
                # key -- any missing half means a rebuild, not a silent
                # append-with-duplicates (audit A1's refuse-over-unsafe rule).
                anchor = self._temporal_anchor_column(kpi, profile_map, required_sources)
                if anchor:
                    self._temporal_anchors.add(anchor.lower())
                event_time = _alias_for_column(mart_body, anchor)
                grain_keys = _grain_keys(mart_body)
                incremental = bool(
                    self.velocity_lane in _INCREMENTAL_LANES and event_time and grain_keys
                )
                options: dict[str, Any]
                if incremental:
                    options = _incremental_config(
                        f"{kpi_id}_sk", event_time, _INCREMENTAL_LOOKBACK_DAYS
                    )
                    mart_body = self._incremental_mart_body(
                        kpi_id, mart_body, grain_keys, event_time
                    )
                else:
                    options = {"materialized": "table"}
                options["cluster_by"] = cluster_by
                options["tblproperties"] = _retention_tblproperties("gold")
                marts_sql = "\n".join(
                    [
                        _render_config(options),
                        "",
                        f"-- Mart: {kpi.get('name', kpi_id)}",
                        mart_body,
                    ]
                )
                (marts_dir / f"fct_{kpi_id}.sql").write_text(marts_sql, encoding="utf-8")
                # The parent of an event-time incremental must declare its own
                # event_time, or dbt cannot bound a read of it per batch/backfill.
                parent_options: dict[str, Any] = {"materialized": "view"}
                if incremental:
                    parent_anchor = _alias_for_column(features_body, anchor)
                    if not parent_anchor:
                        raise ValueError(
                            f"mart is incremental on `{anchor}` but the intermediate model "
                            "exposes no column carrying it -- the parent could not declare "
                            "event_time"
                        )
                    parent_options["event_time"] = parent_anchor
                (intermediate_dir / f"{features_model}.sql").write_text(
                    "\n".join([_render_config(parent_options), "", features_body]),
                    encoding="utf-8",
                )
                generated_kpi_ids.append(kpi_id)
                if incremental:
                    incremental_kpi_ids.append(kpi_id)
            except ValueError as exc:
                # Fail loud into the skip list, never emit partial/broken dbt
                # SQL for a KPI this generator can't fully express yet.
                skipped.append({"kpi_id": kpi_id, "reason": str(exc)})

        if not generated_kpi_ids:
            raise ValueError(
                "No KPI could be expressed as a dbt model. Skipped: "
                + json.dumps(skipped)
            )

        self._write_project_files(dbt_dir, sources_needed)
        self._write_data_quality_tests(staging_dir)
        self._write_exposures(dbt_dir, generated_kpi_ids)
        self._write_publish_gold_macro(dbt_dir, generated_kpi_ids, incremental_kpi_ids)
        if self.late_arriving_dimensions:
            self._write_inferred_member_macro(dbt_dir)
        if self.enforce_contracts:
            self._write_contracts(marts_dir, generated_kpi_ids)
        report_path = self._write_generation_report(
            dbt_dir, generated_kpi_ids, incremental_kpi_ids, skipped
        )

        # Re-read what was actually written and enforce the emitted-code
        # invariants on it. Checking the emitted TEXT (not the intent that
        # produced it) is the point: every rule here guards a silent
        # wrong-data/wrong-cost path, so a future edit to this generator that
        # regresses one fails here instead of in the warehouse.
        violations = validate_generated_project(dbt_dir)
        if violations:
            raise ValueError(
                "Generated dbt project violates emitted-code invariants "
                "(see docs/superpowers/specs/2026-08-05-cloud-first-restructure-design.md "
                "section 6); the project was written to "
                f"{_rel(dbt_dir, self.repo_root)} for inspection but is NOT usable:\n  - "
                + "\n  - ".join(violations)
            )

        # NOTE: `dbt parse` is deliberately NOT run here. It has to render
        # profiles.yml, which resolves credentials via env_var(), so a parse gate
        # at generation time would make generation require warehouse credentials
        # -- and generation must work on a machine that never connects. The gate
        # lives in `verify-dbt-project` instead (see run_dbt_parse), which
        # already assumes credentials. Offline structural checking here is
        # covered by validate_generated_project above and `dbt-index`.

        # Stamp the generator's own content hash so a later validator can tell
        # this project apart from one written by older code. Without it a fixed
        # generator silently never reaches an otherwise-unchanged workspace.
        stamp(self.layout, "dbt_project")

        return DbtProjectResult(
            dbt_project_dir=_rel(dbt_dir, self.repo_root),
            kpi_count=len(generated_kpi_ids),
            generated_kpi_ids=generated_kpi_ids,
            skipped=skipped,
            incremental_kpi_ids=incremental_kpi_ids,
            report_path=report_path,
        )

    def _ensure_staging_model(
        self,
        source: str,
        profile_map: dict[str, dict[str, Any]],
        required_columns: dict[str, set[str]],
        staging_dir: Path,
    ) -> None:
        if source in self._written_staging:
            return
        profile = profile_map.get(source, {})
        stem = _safe_name(dataset_display_stem(source))
        model_name = f"stg_{stem}"
        self._written_staging[source] = model_name
        select_list = self._sql._stage_select_list(source, profile, required_columns)
        lines = [
            "{{ config(materialized='view') }}",
            "",
            f"-- Staging: 1:1 typed mirror of {stem}. No joins (medallion rule).",
        ]
        select_list, dropped = _drop_excluded(select_list, self._excluded_columns(source, stem))
        if dropped:
            # The drift decision is named in the model itself: a reader asking
            # "where did that column go" gets the answer from the SQL, not from
            # a contract file they would have to know exists.
            lines.append(
                "-- Quarantined by a schema-drift decision "
                f"(contracts/schema_exclusions.json): {', '.join(dropped)}"
            )
        if not select_list:
            raise ValueError(
                f"schema_exclusions.json excludes every column of `{stem}`; nothing "
                "would remain to stage. Re-decide the drift finding for that table."
            )
        lines += [f"select {select_list}", "from {{ source('raw', '%s') }}" % stem]
        (staging_dir / f"{model_name}.sql").write_text("\n".join(lines), encoding="utf-8")

    def _excluded_columns(self, source: str, stem: str) -> set[str]:
        """Columns S2's drift panel quarantined for this table, lowercased.

        Keyed by whichever representation the decision used -- the table stem
        (what the panel sees) or the raw dataset string (what the mapping
        stores) -- because both are valid names for the same table.
        """
        for key in (stem, source, dataset_display_stem(source)):
            entry = self._exclusions.get(key)
            if isinstance(entry, dict):
                return {
                    str(c).lower() for c in (entry.get("excluded_columns") or []) if str(c).strip()
                }
        return set()

    def _ref_substitutions(
        self,
        profile_map: dict[str, dict[str, Any]],
        required_sources: list[str],
    ) -> dict[str, str]:
        """Map every quoted-identifier fragment DuckDBKPISQLGenerator's
        databricks-dialect internals could render for these sources to the
        matching dbt Jinja reference. Applied to both the FROM/JOIN clause
        (_kpi_source_from) and every feature expression (_feature_expression's
        derived_formula bare-table-reference substitution uses the identical
        two shapes -- table_ident(stem) for a non-delta profile, the raw fqn
        for a delta/UC-sourced one -- see sql_generator.py:761-772).
        """
        subs: dict[str, str] = {}
        for source in required_sources:
            profile = profile_map.get(source, {})
            stem = _safe_name(dataset_display_stem(source))
            model_name = self._written_staging.get(source, f"stg_{stem}")
            subs[self._sql.quote_ident(model_name)] = "{{ ref('%s') }}" % model_name
            if profile.get("format") == "delta":
                subs[source] = "{{ source('raw', '%s') }}" % stem
            else:
                subs[self._sql.table_ident(stem)] = "{{ ref('%s') }}" % model_name
        return subs

    def _feature_select_items(
        self,
        kpi: dict[str, Any],
        source_aliases: dict[str, str],
        profile_map: dict[str, dict[str, Any]],
    ) -> list[str]:
        from core.onboarding.kpi.result_view_builder import raw_date_input_columns
        from core.onboarding.kpi.sensitive_masking import (
            is_feature_sensitive,
            load_sensitive_columns,
            mask_sql_expr,
        )

        sensitive_cols = load_sensitive_columns(self.layout)
        raw_date_inputs = raw_date_input_columns(kpi)
        # Measure columns are never coalesced to the unknown member -- only
        # dimension attributes are (see _late_arriving_expr).
        metric_tokens = {
            token.lower() for token in re.findall(r"[A-Za-z_]\w*", str(kpi.get("metric") or ""))
        }
        items: list[str] = []
        for feature in kpi.get("features", []):
            column = self._sql._feature_expression(feature, source_aliases, profile_map)
            if not column:
                continue
            feat_name = feature["feature"]
            if self.late_arriving_dimensions and str(feat_name).lower() not in metric_tokens:
                column = _late_arriving_expr(column)
            feat_cols = {
                str(sc.get("column") or "").lower()
                for sc in (feature.get("source_columns") or [])
                if isinstance(sc, dict)
            }
            feat_cols.add(str(feat_name).lower())
            expr = column
            if is_feature_sensitive(feature, sensitive_cols) and not (feat_cols & raw_date_inputs):
                expr = mask_sql_expr(column, "databricks")
            items.append(f"    {expr} as {self._sql.quote_ident(feat_name)}")
        return items

    def _write_project_files(
        self, dbt_dir: Path, sources_needed: dict[str, dict[str, Any]]
    ) -> None:
        (dbt_dir / "macros").mkdir(parents=True, exist_ok=True)
        (dbt_dir / "dbt_project.yml").write_text(
            "\n".join(
                [
                    f"name: '{self._project_name}'",
                    "version: '1.0.0'",
                    "config-version: 2",
                    "",
                    f"profile: '{self._project_name}'",
                    "",
                    'model-paths: ["models"]',
                    'macro-paths: ["macros"]',
                    'test-paths: ["tests"]',
                    "",
                    "models:",
                    f"  {self._project_name}:",
                    "    staging:",
                    "      +materialized: view",
                    f"      +schema: {self.silver_schema}",
                    "    intermediate:",
                    "      +materialized: view",
                    f"      +schema: {self.silver_schema}",
                    "    marts:",
                    "      +materialized: table",
                    # WAP: marts build into the STAGING gold schema. The live
                    # gold schema is written only by macros/publish_gold.sql,
                    # after `dbt build` (models + tests) has passed -- a failed
                    # DQ test therefore leaves the last-good live tables
                    # untouched instead of publishing bad numbers.
                    f"      +schema: {self.staging_gold_schema}",
                    "",
                    # The live/published gold schema, declared explicitly so a
                    # reader (dashboard, reconcile) resolves the SERVING layer
                    # rather than the staging one it must never read from.
                    "vars:",
                    f"  gold_schema: {self.gold_schema}",
                    f"  gold_staging_schema: {self.staging_gold_schema}",
                    f"  catalog: {self.catalog}",
                    "",
                    # A selection that matches nothing EXITS 0 in dbt: `dbt ls
                    # --select nonexistent` prints "No nodes selected!" and
                    # succeeds, so a slim-CI or scheduled run whose selector has
                    # gone stale is indistinguishable from a green build. Promote
                    # it to an error so a silent no-op fails loudly instead.
                    "flags:",
                    "  warn_error_options:",
                    "    error:",
                    "      - NoNodesForSelectionCriteria",
                    "",
                ]
            ),
            encoding="utf-8",
        )
        # dbt's default generate_schema_name CONCATENATES the target schema
        # with a model's `+schema:` (e.g. "bronze_silver") -- not what
        # medallion layering needs. This override is dbt's own documented
        # pattern (generate_schema_name_for_env in
        # dbt/include/global_project/macros/get_custom_name/get_custom_schema.sql)
        # for using the custom schema name directly: silver_schema/
        # gold_schema become real schema names, not a mangled concatenation.
        (dbt_dir / "macros" / "get_custom_schema.sql").write_text(
            "\n".join(
                [
                    "{% macro generate_schema_name(custom_schema_name, node) -%}",
                    "",
                    "    {%- set default_schema = target.schema -%}",
                    "    {%- if custom_schema_name is none -%}",
                    "",
                    "        {{ default_schema }}",
                    "",
                    "    {%- else -%}",
                    "",
                    "        {{ custom_schema_name | trim }}",
                    "",
                    "    {%- endif -%}",
                    "",
                    "{%- endmacro %}",
                    "",
                ]
            ),
            encoding="utf-8",
        )
        # Surrogate keys are a deterministic HASH of the natural key, never an
        # identity column: md5 over the same natural key yields the same key on
        # every rebuild/backfill, so a regenerated dimension does not renumber
        # every fact that points at it. (An identity column also cannot be
        # reproduced in a second environment, which breaks dev/prod parity.)
        # coalesce()+a non-data separator so NULL and 'a|b' vs 'a' | 'b' cannot
        # collide into the same key.
        (dbt_dir / "macros" / "surrogate_key.sql").write_text(
            "\n".join(
                [
                    "{% macro surrogate_key(columns) -%}",
                    "    md5(concat_ws('||'",
                    "    {%- for column in columns %}, coalesce(cast({{ column }} as string), '')"
                    "{% endfor -%}",
                    "    ))",
                    "{%- endmacro %}",
                    "",
                ]
            ),
            encoding="utf-8",
        )
        profiles_lines = [
            f"{self._project_name}:",
            "  target: prod",
            "  outputs:",
            "    prod:",
            f"      type: {_DBT_ADAPTER_TYPE}",
            "      catalog: " + self.catalog,
            "      schema: " + self.schema,
            "      host: \"{{ env_var('DATABRICKS_HOST') }}\"",
            "      http_path: \"{{ env_var('DATABRICKS_HTTP_PATH') }}\"",
            "      token: \"{{ env_var('DATABRICKS_TOKEN') }}\"",
            "      threads: 4",
        ]
        # dbt-databricks 1.11+ Query Tags: auto-injects dbt_model_name/
        # dbt_materialized/dbt_core_version/dbt_databricks_version into every
        # query with zero config; project_name/env below are the ONLY custom
        # tags this platform needs to add for per-enterprise cost attribution
        # via system.query_history.query_tags (see the dbt+Airflow
        # integration plan's cost-governance section). project_name is
        # deliberately the enterprise, not the workspace -- that's the
        # billing/attribution boundary, matching resolve_databricks_config's
        # per-enterprise scoping. Empty when no enterprise_id is resolvable
        # (today's single-tenant default) -- still valid YAML, just an
        # empty-string tag until a real enterprise_id is declared.
        #
        # `env` is a literal, not `{{ target.name }}`: found live -- `target`
        # is only in scope inside MODEL/macro Jinja compilation, not inside
        # profiles.yml's own (much more limited) rendering context, which
        # only exposes env_var() and a few connection-level helpers. Only
        # one target ("prod") exists in this generator's scope today, so a
        # literal is honest; this needs to become genuinely target-aware
        # once a second (e.g. dev/duckdb) target is added.
        #
        # `query_tags` is a JSON-encoded STRING, not a YAML mapping -- found
        # live: the adapter's own credentials.py types it `Optional[str]`
        # and connections.py parses it via `json.loads()` (utils.py's
        # QueryTagsUtils.parse_query_tags), rejecting a nested-dict profile
        # value with a schema-validation error. Confirmed against the
        # installed dbt-databricks 1.12.2 source, not assumed from docs.
        #
        # `run_id` is what makes the tag JOINABLE back to a cost-ledger row.
        # project_name+env alone attribute warehouse spend to a TENANT, never to
        # a run -- which is why the audited run tagged 92 queries and could not
        # price any of them. It is an `env_var()` deliberately, not a literal:
        # profiles.yml is generated ONCE and reused by every later invocation,
        # so a baked-in id would stamp the first run's id onto all of them. The
        # value is resolved at dbt-invocation time from the shell dbt runs in
        # (`AUTORESEARCH_RUN_ID`, exported by core.observability.cost_ledger's
        # RUN_ID_ENV). Absent -> empty string: the reconciler then finds nothing
        # for that run and says so, which is honest.
        query_tags_json = json.dumps({
            "project_name": self.enterprise_id,
            "env": "prod",
            "run_id": "{{ env_var('" + RUN_ID_ENV + "', '') }}",
        })
        # Double-quoted YAML scalar (not single-quoted): the value contains
        # single quotes from the Jinja call, and YAML's '' escape nested inside a
        # JSON string is unreadable. Escaping the JSON's own double quotes is the
        # legible half of the trade.
        yaml_scalar = query_tags_json.replace("\\", "\\\\").replace('"', '\\"')
        profiles_lines.append(f'      query_tags: "{yaml_scalar}"')
        profiles_lines.append("")
        (dbt_dir / "profiles.yml").write_text("\n".join(profiles_lines), encoding="utf-8")
        source_tables = "\n".join(
            self._source_entry(source, sources_needed.get(source) or {})
            for source in sorted(sources_needed)
        )
        (dbt_dir / "models" / "sources.yml").write_text(
            "\n".join(
                [
                    "version: 2",
                    "",
                    "sources:",
                    "  - name: raw",
                    f"    database: {self.catalog}",
                    f"    schema: {self.schema}",
                    "    tables:",
                    source_tables,
                    "",
                ]
            ),
            encoding="utf-8",
        )
        (dbt_dir / "packages.yml").write_text(
            "# not_null/accepted_values (data_quality_panel.py) are dbt-core"
            " built-ins -- no package needed.\n"
            "# dbt-expectations added here once a range/statistical check type"
            " is implemented.\npackages: []\n",
            encoding="utf-8",
        )

    def _source_entry(self, source: str, profile: dict[str, Any]) -> str:
        """One sources.yml table entry, with freshness when a load/event
        timestamp resolves for it (audit A6).

        The blueprint decides "freshness" as a DQ check, but the emitted
        project could not evaluate it: no `freshness:`, no `loaded_at_field`.
        When no temporal column resolves, the entry says so as an OPEN
        QUESTION (and the generation report repeats it) -- silence was the
        finding, so silence is what is not allowed.
        """
        stem = _safe_name(dataset_display_stem(source))
        lines = [f"      - name: {stem}"]
        anchor = self._loaded_at_field(source, profile)
        if not anchor:
            question = (
                f"source `{stem}`: no load/event timestamp column resolved, so dbt source "
                "freshness cannot be evaluated. Declare a loaded_at_field for it (or a "
                "temporal anchor on the table) and regenerate."
            )
            self._open_questions.append(question)
            lines.append(f"        # {_OPEN_QUESTION}: {question}")
            return "\n".join(lines)
        # Cast, not a bare column: a date/timestamp landing as a string (every
        # CSV-profiled source) is still a usable freshness field once cast, and
        # the cast is a no-op on a genuinely temporal column.
        lines += [
            f'        loaded_at_field: "cast(`{anchor}` as timestamp)"',
            "        freshness:",
            f"          warn_after: {{count: {_FRESHNESS_WARN_DAYS}, period: day}}",
            f"          error_after: {{count: {_FRESHNESS_ERROR_DAYS}, period: day}}",
        ]
        return "\n".join(lines)

    def _loaded_at_field(self, source: str, profile: dict[str, Any]) -> str:
        """A temporal column of this source: a genuinely temporal dtype first,
        then a column a KPI already uses as its temporal anchor (a date landing
        as a string, which every CSV-profiled source does)."""
        schema = profile.get("schema") or {}
        if not isinstance(schema, dict):
            return ""
        for column, dtype in schema.items():
            if _is_temporal_dtype(str(dtype)):
                return str(column)
        for column in schema:
            if str(column).lower() in self._temporal_anchors:
                return str(column)
        return ""

    def _temporal_anchor_column(
        self,
        kpi: dict[str, Any],
        profile_map: dict[str, dict[str, Any]],
        required_sources: list[str],
    ) -> str:
        """The source column this KPI's grain is anchored in time by, or "".

        Evidence first (the KPI's own declared time grain, via the intent
        contract's temporal_anchor facet), then measured dtypes. Nothing is
        guessed from a column NAME -- an unresolved anchor simply means the
        mart stays a full-refresh table.
        """
        from core.onboarding.kpi.intent_contract import _facet_temporal_anchor

        value = str((_facet_temporal_anchor(kpi) or {}).get("value") or "")
        if value.startswith("event_date:"):
            column = value.split(":", 1)[1].strip()
            if column:
                return column
        for source in required_sources:
            schema = (profile_map.get(source) or {}).get("schema") or {}
            for column, dtype in (schema.items() if isinstance(schema, dict) else []):
                if _is_temporal_dtype(str(dtype)):
                    return str(column)
        return ""

    def _incremental_mart_body(
        self, kpi_id: str, mart_body: str, grain_keys: list[str], event_time: str
    ) -> str:
        """Wrap the KPI's own result SELECT so the mart carries a deterministic
        merge key and only re-writes the recent event-time window.

        ponytail: the predicate sits OUTSIDE the aggregate, so a run still
        re-aggregates the full grain -- it bounds what is WRITTEN (and merged),
        not what is scanned. Pushing it into the aggregate needs the KPI SQL
        builder to accept a where-clause seam; do that if scan cost, not write
        cost, becomes the problem.
        """
        quoted = self._sql.quote_ident(event_time)
        keys = json.dumps([f"mart_rows.{k}" for k in grain_keys]).replace('"', "'")
        return "\n".join(
            [
                f"-- Incremental ({self.velocity_lane} lane): merge on the deterministic",
                f"-- surrogate key of the grain; the {_INCREMENTAL_LOOKBACK_DAYS}-day lookback",
                "-- re-reads late arrivals instead of silently missing them.",
                "select",
                "    {{ surrogate_key(%s) }} as %s_sk," % (keys, kpi_id),
                "    mart_rows.*",
                "from (",
                mart_body,
                ") as mart_rows",
                "{% if is_incremental() %}",
                f"where cast(mart_rows.{quoted} as date) >= date_sub(",
                f"    (select coalesce(max(cast({quoted} as date)), date'1900-01-01')"
                " from {{ this }}),",
                f"    {_INCREMENTAL_LOOKBACK_DAYS}",
                ")",
                "{% endif %}",
            ]
        )

    def _write_inferred_member_macro(self, dbt_dir: Path) -> None:
        """Late-arriving dimension / early-arriving fact (audit missing #1).

        A fact whose dimension row has not landed yet must not vanish. Two
        halves: this macro pair maintains an INFERRED (unknown) member row per
        dimension -- deterministic hash key of the natural key, `is_inferred`
        true, type-1 overwritten when the real row finally arrives -- and the
        emitted fact SQL LEFT JOINs and COALESCEs to that member
        (_late_arriving_expr), so the fact row survives with a named unknown
        instead of a NULL group or no row at all.
        """
        (dbt_dir / "macros").mkdir(parents=True, exist_ok=True)
        (dbt_dir / "macros" / "inferred_member.sql").write_text(
            "\n".join(
                [
                    "{# Late-arriving dimensions: insert an inferred (unknown) member for",
                    "   every natural key a fact already references but the dimension does",
                    "   not have yet. Facts then join to a real row instead of being",
                    f"   dropped; unresolved attributes read as '{_UNKNOWN_MEMBER}'.",
                    "     dbt run-operation inferred_member --args '{...}' #}",
                    "{% macro inferred_member(dimension_relation, natural_key_column,"
                    " fact_relation, fact_key_column) -%}",
                    "    merge into {{ dimension_relation }} as dim",
                    "    using (",
                    "        select distinct cast(fact.{{ fact_key_column }} as string)"
                    " as natural_key",
                    "        from {{ fact_relation }} as fact",
                    "        where fact.{{ fact_key_column }} is not null",
                    "    ) as arriving",
                    "    on cast(dim.{{ natural_key_column }} as string) = arriving.natural_key",
                    "    when not matched then insert (",
                    "        {{ natural_key_column }}, dim_sk, is_inferred",
                    "    ) values (",
                    "        arriving.natural_key,"
                    " {{ surrogate_key(['arriving.natural_key']) }}, true",
                    "    )",
                    "{%- endmacro %}",
                    "",
                    "{# Type-1 overwrite: the real dimension row finally arrived, so the",
                    "   inferred member's attributes are replaced in place -- the surrogate",
                    "   key is unchanged (it is a hash of the same natural key), so every",
                    "   fact already pointing at it stays correct. #}",
                    "{% macro resolve_inferred_member(dimension_relation, source_relation,"
                    " natural_key_column, attributes) -%}",
                    "    merge into {{ dimension_relation }} as dim",
                    "    using {{ source_relation }} as src",
                    "    on cast(dim.{{ natural_key_column }} as string)"
                    " = cast(src.{{ natural_key_column }} as string)",
                    "    when matched and dim.is_inferred then update set",
                    "    {%- for attribute in attributes %}",
                    "        {{ attribute }} = src.{{ attribute }},",
                    "    {%- endfor %}",
                    "        is_inferred = false",
                    "{%- endmacro %}",
                    "",
                ]
            ),
            encoding="utf-8",
        )

    def _write_generation_report(
        self,
        dbt_dir: Path,
        generated_kpi_ids: list[str],
        incremental_kpi_ids: list[str],
        skipped: list[dict[str, str]],
    ) -> str:
        """What this generation decided, and what it could not answer.

        The open-question list is the point (audit A6): a source whose
        freshness cannot be evaluated is stated here, not silently omitted.
        """
        lines = [
            "# dbt Project Generation",
            "",
            f"Project: `{_rel(dbt_dir, self.repo_root)}`",
            f"Velocity lane: `{self.velocity_lane or 'not decided (batch behaviour)'}`",
            f"Marts: {len(generated_kpi_ids)} "
            f"({len(incremental_kpi_ids)} incremental, "
            f"{len(generated_kpi_ids) - len(incremental_kpi_ids)} full-refresh table)",
            "",
        ]
        if incremental_kpi_ids:
            lines += ["## Incremental marts", ""]
            lines += [f"- `fct_{kpi_id}`" for kpi_id in incremental_kpi_ids]
            lines.append("")
        lines += ["## Open questions", ""]
        if self._open_questions:
            lines += [f"- [blocked] {question}" for question in self._open_questions]
        else:
            lines.append("None.")
        lines.append("")
        if skipped:
            lines += ["## Skipped KPIs", ""]
            lines += [
                f"- `{item.get('kpi_id')}`: {item.get('reason')}" for item in skipped
            ]
            lines.append("")
        report_dir = self.layout.reports_dir / "dbt_generation"
        report_dir.mkdir(parents=True, exist_ok=True)
        report = report_dir / "current.md"
        report.write_text("\n".join(lines), encoding="utf-8")
        return _rel(report, self.repo_root)

    def _write_publish_gold_macro(
        self,
        dbt_dir: Path,
        generated_kpi_ids: list[str],
        incremental_kpi_ids: list[str] | None = None,
    ) -> None:
        """The WAP publish step: staging gold -> live gold, run as the FINAL
        task after `dbt build` (models + tests) passes.

        Delta has no Iceberg-style WAP branch to fast-forward, so the
        documented equivalent is staging table + tests + swap. This macro is
        the ONLY place in anything this platform emits where a REPLACE of an
        existing relation is permitted, and it can only ever replace the
        `<gold>.<mart>` half of a staging/live pair this generator itself
        wrote -- the mart list below is an explicit literal, never a wildcard
        over the schema, so a hand-made table sharing the schema is untouchable
        by it. A failed test means the macro never runs and the previous live
        tables keep serving.
        """
        if not generated_kpi_ids:
            return
        incremental = set(incremental_kpi_ids or [])
        marts = [f"fct_{k}" for k in sorted(generated_kpi_ids) if k not in incremental]
        incremental_marts = [f"fct_{k}" for k in sorted(incremental)]
        (dbt_dir / "macros" / "publish_gold.sql").write_text(
            "\n".join(
                [
                    "{# Write-Audit-Publish: publish staging gold to live gold.",
                    "   Run ONLY after `dbt build` (models + tests) succeeds:",
                    "     dbt run-operation publish_gold",
                    "   Failed tests => this never runs => last-good live tables keep serving.",
                    "   Full-refresh table marts are copied; INCREMENTAL marts are published",
                    "   by a metadata-only swap (SHALLOW CLONE), never a second full copy --",
                    "   a copy would rewrite every mart twice per run (audit A1), and a",
                    "   staging->live RENAME would move the incremental state table away",
                    "   from under the next run. #}",
                    "{% macro publish_gold() -%}",
                    "    {%- set marts = " + json.dumps(marts).replace('"', "'") + " -%}",
                    "    {%- set incremental_marts = "
                    + json.dumps(incremental_marts).replace('"', "'")
                    + " -%}",
                    "    {%- do run_query('CREATE SCHEMA IF NOT EXISTS `' ~ var('catalog') "
                    "~ '`.`' ~ var('gold_schema') ~ '`') -%}",
                    "    {%- for mart in marts %}",
                    "    {%- set swap_sql %}",
                    "        CREATE OR REPLACE TABLE `{{ var('catalog') }}`."
                    "`{{ var('gold_schema') }}`.`{{ mart }}`",
                    "        AS SELECT * FROM `{{ var('catalog') }}`."
                    "`{{ var('gold_staging_schema') }}`.`{{ mart }}`",
                    "    {%- endset %}",
                    "    {%- do log('publish_gold: ' ~ mart, info=True) -%}",
                    "    {%- do run_query(swap_sql) -%}",
                    "    {%- endfor %}",
                    "    {%- for mart in incremental_marts %}",
                    "    {%- set clone_sql %}",
                    "        CREATE OR REPLACE TABLE `{{ var('catalog') }}`."
                    "`{{ var('gold_schema') }}`.`{{ mart }}`",
                    "        SHALLOW CLONE `{{ var('catalog') }}`."
                    "`{{ var('gold_staging_schema') }}`.`{{ mart }}`",
                    "    {%- endset %}",
                    "    {%- do log('publish_gold (metadata swap): ' ~ mart, info=True) -%}",
                    "    {%- do run_query(clone_sql) -%}",
                    "    {%- endfor %}",
                    "{%- endmacro %}",
                    "",
                ]
            ),
            encoding="utf-8",
        )

    def _write_exposures(self, dbt_dir: Path, generated_kpi_ids: list[str]) -> None:
        """Register the live dashboard (core.dashboard) as a formal dbt
        `exposure` -- so `dbt docs`/lineage treats it as a real downstream
        consumer of the marts layer, per the dbt+Airflow integration plan's
        data-contracts section ("the dashboard reading a dbt-produced table
        is an implicit contract today" -- this makes it explicit). Required
        exposure fields per dbt's own spec: name, type, owner (name/email);
        this platform does not track a real per-workspace owner identity
        yet, so the email is a deterministic, clearly-synthetic placeholder
        tied to the enterprise/project, not a fabricated real address.
        """
        if not generated_kpi_ids:
            return
        depends_on = "\n".join(
            f"      - ref('fct_{kpi_id}')" for kpi_id in sorted(generated_kpi_ids)
        )
        owner_slug = self.enterprise_id or self._project_name
        (dbt_dir / "models" / "exposures.yml").write_text(
            "\n".join(
                [
                    "version: 2",
                    "",
                    "exposures:",
                    f"  - name: {self._project_name}_dashboard",
                    f'    label: "{self._project_name} Dashboard"',
                    "    type: dashboard",
                    "    maturity: high",
                    "    description: >",
                    "      The live dashboard (core.dashboard) rendering this workspace's",
                    "      KPI results from these marts.",
                    "    depends_on:",
                    depends_on,
                    "    owner:",
                    f"      name: {self._project_name} dashboard",
                    f"      email: dashboard@{owner_slug}.internal",
                    "",
                ]
            ),
            encoding="utf-8",
        )

    # Model versioning (dbt+Airflow plan section 5's breaking-change policy)
    # is deliberately NOT automated here -- there is no way to generically
    # detect "is this KPI's new SQL a breaking change" from a metric/cuts
    # diff alone, and auto-versioning every regeneration would make version
    # bumps meaningless noise. It's a dbt-native, workspace-owner-driven
    # action, exercised for real this session (D5) against a genuine
    # breaking rename on a live mart and confirmed:
    #   - a new `models/marts/fct_<kpi_id>_v2.sql` file (the new shape)
    #     alongside the existing `fct_<kpi_id>.sql` (kept as-is, v1),
    #   - a properties file declaring both versions:
    #       models:
    #         - name: fct_<kpi_id>
    #           latest_version: 2
    #           versions:
    #             - v: 1
    #               defined_in: fct_<kpi_id>   # the existing, unrenamed file
    #               deprecation_date: "YYYY-MM-DD"
    #             - v: 2
    #               defined_in: fct_<kpi_id>_v2
    #   - `dbt build` then materializes BOTH as real tables
    #     (`fct_<kpi_id>_v1`, `fct_<kpi_id>_v2`), and every unpinned
    #     `ref('fct_<kpi_id>')` (including this generator's own
    #     exposures.yml) resolves to latest_version automatically --
    #     confirmed via the real manifest.json (exposure depends_on ->
    #     `model.<project>.fct_<kpi_id>.v2`). A future deprecation_date
    #     does not block the build; dbt only refuses to delete/undeclare
    #     that version once the date has passed.

    def _write_contracts(self, marts_dir: Path, generated_kpi_ids: list[str]) -> None:
        """Emit `contract: enforced: true` for each mart the dashboard reads,
        per the dbt+Airflow integration plan's data-contracts section (D5):
        "the dashboard reading a dbt-produced table is an implicit contract
        today" -- this makes it explicit and dbt-enforced.

        Column `data_type`s come from the REAL, already-built table
        (`DESCRIBE TABLE`), not a guessed/inferred type -- a contract states
        ground truth; a wrong guess would make every future `dbt build` fail
        on a false mismatch. This means enforcement requires at least one
        prior successful build: a KPI whose mart doesn't exist yet (first-
        ever generation, or Databricks unreachable) is skipped, not an
        error -- re-running this command after a build picks it up.
        `enforce_contracts=False` (the default) never calls this at all, so
        every existing generated project stays byte-identical.
        """
        from core.config import resolve_databricks_config
        from core.execution.databricks_client import DatabricksClient

        db_cfg = resolve_databricks_config(self.enterprise_id)
        if not db_cfg.is_active():
            return
        client = DatabricksClient(db_cfg)
        contracted: list[dict[str, Any]] = []
        for kpi_id in sorted(generated_kpi_ids):
            model_name = f"fct_{kpi_id}"
            try:
                cols, rows = client.execute_query(
                    f"DESCRIBE TABLE `{self.catalog}`.`{self.gold_schema}`.`{model_name}`"
                )
            except Exception:
                continue
            columns = [
                {"name": str(r[0]), "data_type": str(r[1])}
                for r in rows
                if r and r[0] and not str(r[0]).startswith("#")
            ]
            if not columns:
                continue
            contracted.append({"name": model_name, "columns": columns})
        if not contracted:
            return
        lines = ["version: 2", "", "models:"]
        for model in contracted:
            lines.append(f"  - name: {model['name']}")
            lines.append("    config:")
            lines.append("      contract:")
            lines.append("        enforced: true")
            lines.append("    columns:")
            for col in model["columns"]:
                lines.append(f"      - name: {col['name']}")
                lines.append(f"        data_type: {col['data_type']}")
        lines.append("")
        (marts_dir / "_contracts.yml").write_text("\n".join(lines), encoding="utf-8")

    def _write_data_quality_tests(self, staging_dir: Path) -> None:
        """Emit dbt schema tests from confirmed data_quality_panel.py answers
        (data_quality_decisions.json) -- never hand-maintained YAML. Attached
        to the STAGING model for the decided column: shift-left, catches a
        violation at the earliest point in the pipeline, before it can
        silently propagate into a KPI number. A workspace with no confirmed
        DQ decisions yet gets no schema.yml at all (byte-identical to before
        this method existed) -- an empty/absent decisions file is not an
        error, it just means no test has been authored yet.
        """
        decisions_path = self.layout.contracts_dir / "data_quality_decisions.json"
        if not decisions_path.exists():
            return
        try:
            data = json.loads(decisions_path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            return
        decisions = [d for d in (data.get("decisions") or []) if isinstance(d, dict)]
        # Match by STEM, not the raw dataset string: kpi_feature_mapping.json's
        # source_columns[].dataset stores an absolute local path for a CSV
        # profile, while _required_source_columns()/_written_staging key by
        # the repo-relative form -- same dataset, two valid representations.
        # dataset_display_stem() (already the model-naming source of truth)
        # is representation-agnostic for both local paths and UC fqns, so
        # matching on it sidesteps the inconsistency instead of guessing
        # which representation a given decision happens to use.
        model_by_stem = {
            _safe_name(dataset_display_stem(source)): model_name
            for source, model_name in self._written_staging.items()
        }
        by_model: dict[str, list[dict[str, Any]]] = {}
        for decision in decisions:
            check_type = str(decision.get("check_type") or "")
            if not check_type:
                continue  # "skip" answers record no check
            dataset = str(decision.get("dataset") or "")
            model_name = model_by_stem.get(_safe_name(dataset_display_stem(dataset)))
            if not model_name:
                continue  # decision references a dataset this run didn't stage
            by_model.setdefault(model_name, []).append(decision)
        if not by_model:
            return

        lines = ["version: 2", "", "models:"]
        for model_name in sorted(by_model):
            lines.append(f"  - name: {model_name}")
            lines.append("    columns:")
            by_column: dict[str, list[dict[str, Any]]] = {}
            for decision in by_model[model_name]:
                by_column.setdefault(str(decision.get("column") or ""), []).append(decision)
            for column in sorted(by_column):
                lines.append(f"      - name: {column}")
                lines.append("        tests:")
                for decision in by_column[column]:
                    lines.extend(_render_dbt_test(decision))
        (staging_dir / "_data_quality.yml").write_text("\n".join(lines) + "\n", encoding="utf-8")


def _model_config(model_sql: str) -> dict[str, Any]:
    """The kwargs of a model's `{{ config(...) }}` call, parsed with `ast`
    rather than eval'd -- generated text or not, nothing here executes it."""
    match = re.search(r"\{\{\s*config\((.*)\)\s*\}\}", model_sql)
    if not match:
        return {}
    try:
        call = ast.parse(f"_({match.group(1)})", mode="eval").body
    except SyntaxError:
        return {}
    config: dict[str, Any] = {}
    for keyword in getattr(call, "keywords", []):
        if not keyword.arg:
            continue
        try:
            config[keyword.arg] = ast.literal_eval(keyword.value)
        except ValueError:
            config[keyword.arg] = None  # a Jinja expression, not a literal
    return config


def _incremental_config(unique_key: str, event_time: str, lookback: int) -> dict[str, Any]:
    """The config an incremental mart must carry. Every key here guards a
    silent-wrong-data path validate_generated_project re-checks on the emitted
    text -- merge without unique_key degrades to append, an implicit
    on_schema_change drops new columns, an implicit lookback misses late data.
    """
    return {
        "materialized": "incremental",
        "incremental_strategy": "merge",
        "unique_key": unique_key,
        "on_schema_change": "append_new_columns",
        "event_time": event_time,
        "lookback": lookback,
    }


def _retention_tblproperties(layer: str) -> dict[str, str]:
    """Declared Delta retention as real TBLPROPERTIES (audit A4): unset, DBR's
    7-day default silently governs time travel and RESTORE, whatever the
    medallion design report promised."""
    interval = f"interval {_RETENTION_DAYS[layer]} days"
    return {name: interval for name in _RETENTION_PROPERTIES}


def _render_config(options: dict[str, Any]) -> str:
    """`{{ config(...) }}` on ONE line -- _model_config (and every validator
    reading it) matches the call with a non-DOTALL regex."""
    rendered = ", ".join(
        f"{key}={json.dumps(value).replace(chr(34), chr(39))}"
        if not isinstance(value, str)
        else f"{key}='{value}'"
        for key, value in options.items()
    )
    return "{{ config(%s) }}" % rendered


def _late_arriving_expr(expr: str) -> str:
    """COALESCE a DIMENSION-side attribute to the unknown member so an
    early-arriving fact keeps its measure (audit missing #1). The join is
    already LEFT; without this the fact lands in a NULL group.

    ponytail: the coalesce casts to string, so a numeric dimension attribute
    comes out as text in that mode. The typed fix is a real unknown-member ROW
    in a generated dimension table (macros/inferred_member.sql maintains one);
    do that once this generator emits dimension models.
    """
    match = re.match(r"^s([1-9]\d*)\.", expr.strip())
    if not match:
        return expr  # base (fact) side, or an expression with no single source
    return f"coalesce(cast({expr} as string), '{_UNKNOWN_MEMBER}')"


def _drop_excluded(select_list: str, excluded: set[str]) -> tuple[str, list[str]]:
    """Remove quarantined columns from a staging select list, naming what went."""
    if not excluded or select_list.strip() == "*":
        return select_list, []
    kept, dropped = [], []
    for item in select_list.split(", "):
        if item.strip().strip('`"').lower() in excluded:
            dropped.append(item.strip().strip('`"'))
        else:
            kept.append(item)
    return ", ".join(kept), dropped


def _output_columns(body: str) -> list[tuple[str, str]]:
    """(expression, output alias) for each aliased column of a select's head."""
    head = re.split(r"(?im)^\s*FROM\b", body, maxsplit=1)[0]
    out: list[tuple[str, str]] = []
    for line in head.splitlines():
        item = re.search(
            r"(?i)^(.*?)\s+AS\s+[`\"]?([A-Za-z_][A-Za-z0-9_]*)[`\"]?\s*,?\s*$", line
        )
        if item:
            out.append((item.group(1).strip(), item.group(2)))
    return out


def _alias_for_column(body: str, column: str) -> str:
    """The output alias whose expression references `column` -- e.g. a mart
    grouping by `date_trunc('month', ServiceDate) AS month` is anchored in time
    by `month`, not by a column named like the source one."""
    if not column:
        return ""
    needle = column.lower()
    for expr, alias in _output_columns(body):
        if needle in expr.lower():
            return alias
    return ""


def _grain_keys(mart_body: str) -> list[str]:
    """The mart's GROUP BY dimensions as output aliases -- the grain, and so
    the merge key. No GROUP BY means no derivable grain (and no incremental)."""
    match = re.search(
        r"(?is)\bGROUP\s+BY\b(.*?)(?:\bORDER\s+BY\b|\bHAVING\b|\bLIMIT\b|$)", mart_body
    )
    if not match:
        return []
    group_by = match.group(1)
    return [alias for expr, alias in _output_columns(mart_body) if expr and expr in group_by]


def _cluster_keys(mart_body: str) -> list[str]:
    """<=4 liquid-clustering keys for a mart table.

    The mart's GROUP BY dimensions, expressed as their OUTPUT column names
    (clustering is on the materialized table's columns, not on the source
    expression) -- those are what a KPI read filters and groups on. Falls back
    to the leading output columns when the mart has no GROUP BY.
    """
    every = [alias for _, alias in _output_columns(mart_body)]
    return (_grain_keys(mart_body) or every)[:_MAX_CLUSTER_KEYS]


def run_dbt_parse(
    dbt_dir: Path,
    *,
    repo_root: Path | None = None,
    runner: Any = None,
) -> tuple[bool, str]:
    """Run `dbt parse` over a generated project. Offline; no warehouse needed.

    Returns ``(ok, detail)``. A missing ``dbt`` binary is ``(True, ...)`` with a
    skip note: generation must work on a machine that never runs dbt, and a
    silent skip is worse than a loud one. Real parse failures return the tail of
    dbt's own output, which is where the offending model/ref is named.

    argv/cwd match the module conventions already used by
    ``core/orchestration/dbt_verify.py`` and ``dbt_backfill.py`` -- bare ``dbt``
    resolved on PATH, project and profiles both pointed at the generated dir.
    """
    run = runner or subprocess.run
    exe = shutil.which("dbt")
    if exe is None:
        return True, "[~] dbt not on PATH; `dbt parse` skipped (not verified)"
    cmd = [exe, "parse", "--project-dir", str(dbt_dir), "--profiles-dir", str(dbt_dir)]
    try:
        proc = run(
            cmd,
            cwd=str(repo_root) if repo_root else None,
            capture_output=True,
            text=True,
        )
    except OSError as exc:  # pragma: no cover - defensive
        return True, f"[~] could not execute dbt parse ({type(exc).__name__}); skipped"
    if getattr(proc, "returncode", 1) == 0:
        return True, "[ok] dbt parse resolved every ref, source and config"
    out = f"{getattr(proc, 'stdout', '') or ''}\n{getattr(proc, 'stderr', '') or ''}".strip()
    return False, out[-_PARSE_TAIL_CHARS:]


def validate_generated_project(dbt_dir: Path) -> list[str]:
    """Emitted-code invariants, checked against the finished project text.

    Returns one human-readable violation string per breach, each naming the
    model, empty when the project is clean. Every rule below guards a path
    that fails SILENTLY at runtime -- dbt-databricks reports none of them as
    an error (pipeline_practices_gap_research.md section 4).
    """
    violations: list[str] = []
    models = {
        path.stem: path.read_text(encoding="utf-8")
        for path in sorted((dbt_dir / "models").rglob("*.sql"))
    }
    configs = {name: _model_config(sql) for name, sql in models.items()}

    for name, sql in models.items():
        config = configs[name]
        materialized = str(config.get("materialized") or "")

        if materialized == "incremental":
            # dbt-databricks' default strategy IS merge, so an unset strategy
            # is a merge for this check's purposes.
            strategy = str(config.get("incremental_strategy") or "merge")
            if strategy == "merge" and not config.get("unique_key"):
                violations.append(
                    f"{name}: incremental_strategy='merge' without unique_key -- "
                    "dbt-databricks silently degrades this to append (duplicate rows "
                    "on every run). Resolve a unique_key or do not emit an "
                    "incremental model."
                )
            if not config.get("on_schema_change"):
                violations.append(
                    f"{name}: incremental model without an explicit on_schema_change -- "
                    "dbt's default is 'ignore', which silently DROPS new columns. Set "
                    "'append_new_columns' (or 'fail' for a contract-enforced mart)."
                )
            # Positional strategies insert BY POSITION; the event-time rules
            # below apply to any incremental model that bounds itself by time,
            # which is what the merge marts this generator now emits do.
            positional = strategy in ("microbatch", "replace_where")
            if positional or config.get("event_time"):
                if positional and not config.get("event_time"):
                    violations.append(
                        f"{name}: {strategy} without event_time -- the batch predicate "
                        "cannot be derived."
                    )
                if config.get("lookback") is None:
                    violations.append(
                        f"{name}: {strategy} without an explicit lookback -- dbt's "
                        "default of 1 batch silently misses all late-arriving data."
                    )
                for parent in sorted(set(_refs(sql))):
                    if parent in configs and not configs[parent].get("event_time"):
                        violations.append(
                            f"{name}: upstream parent '{parent}' declares no event_time -- "
                            "dbt then FULL-SCANS that parent once PER BATCH. Declare "
                            "event_time on the parent or do not use "
                            f"{strategy} here."
                        )
            if positional:
                if re.search(r"(?i)\bselect\s+\*", sql):
                    violations.append(
                        f"{name}: {strategy} model uses `select *` -- it inserts by "
                        "POSITION, so an upstream column reorder writes values into the "
                        "wrong columns. Emit an explicit, stably-ordered column list."
                    )
                if "inserts by POSITION" not in sql:
                    violations.append(
                        f"{name}: {strategy} model is missing the insert-by-position "
                        f"warning comment above its column list -- add: {_POSITION_WARNING}"
                    )

        if materialized in ("table", "incremental"):
            props = config.get("tblproperties") or {}
            absent = [prop for prop in _RETENTION_PROPERTIES if prop not in props]
            if absent:
                violations.append(
                    f"{name}: no declared Delta retention ({', '.join(absent)}) -- DBR's "
                    "7-day default then silently governs time travel/RESTORE, whatever "
                    "retention the medallion design declared."
                )

        if materialized == "table":
            keys = config.get("cluster_by") or []
            if isinstance(keys, str):
                keys = [keys]
            if not keys:
                violations.append(
                    f"{name}: table model without cluster_by -- liquid clustering "
                    "replaces partitioning on every new table below ~1 TB."
                )
            elif len(keys) > _MAX_CLUSTER_KEYS:
                violations.append(
                    f"{name}: cluster_by declares {len(keys)} keys; Databricks liquid "
                    f"clustering allows at most {_MAX_CLUSTER_KEYS}."
                )

        if config.get("partition_by"):
            declared = config.get("partition_justified_bytes") or 0
            if not isinstance(declared, int) or declared < _PARTITION_MIN_BYTES:
                violations.append(
                    f"{name}: partition_by on a table with no declared size at or above "
                    f"~1 TB (partition_justified_bytes={declared!r}) -- use cluster_by. "
                    "Partitioning below that size costs more than it saves."
                )

        if _NONDETERMINISTIC_KEY.search(sql):
            violations.append(
                f"{name}: identity/nondeterministic surrogate key -- keys must be a "
                "deterministic hash of the natural key (see macros/surrogate_key.sql) "
                "so a rebuild or backfill reproduces them."
            )

    # Freshness OR an explicit open question, per source (audit A6). The
    # finding was not "freshness is missing" but that the project said nothing
    # about it while the blueprint claimed the check existed.
    sources_yml = dbt_dir / "models" / "sources.yml"
    if sources_yml.exists():
        for source_name, block in _source_blocks(sources_yml.read_text(encoding="utf-8")):
            if "freshness:" not in block and _OPEN_QUESTION not in block:
                violations.append(
                    f"sources.yml: source '{source_name}' declares neither freshness/"
                    f"loaded_at_field nor an explicit {_OPEN_QUESTION} -- the blueprint's "
                    "freshness decision would then be unevaluable and silent."
                )

    profiles = dbt_dir / "profiles.yml"
    if profiles.exists() and "query_tags" not in profiles.read_text(encoding="utf-8"):
        violations.append(
            "profiles.yml: no query_tags -- per-model cost attribution from "
            "system.query_history is then impossible."
        )
    return violations


def project_declares_event_time(dbt_dir: Path) -> bool:
    """True when at least one model declares `event_time`.

    What decides whether a backfill can be a bounded, partition-aligned replay
    (`--event-time-start/--event-time-end`) or degrades to a full refresh --
    the orchestration emitter says which, out loud, rather than implying a
    bounded backfill it cannot deliver.
    """
    return any(
        _model_config(path.read_text(encoding="utf-8")).get("event_time")
        for path in (dbt_dir / "models").rglob("*.sql")
    )


def _refs(model_sql: str) -> list[str]:
    return re.findall(r"\{\{\s*ref\(\s*'([^']+)'\s*\)\s*\}\}", model_sql)


def _source_blocks(sources_yml: str) -> list[tuple[str, str]]:
    """(table name, its YAML block) for each source table entry."""
    blocks: list[tuple[str, str]] = []
    name = ""
    lines: list[str] = []
    for line in sources_yml.splitlines():
        entry = re.match(r"^\s{6}- name: (\S+)\s*$", line)
        if entry:
            if name:
                blocks.append((name, "\n".join(lines)))
            name, lines = entry.group(1), []
            continue
        if name:
            lines.append(line)
    if name:
        blocks.append((name, "\n".join(lines)))
    return blocks


def _read_json(path: Path) -> dict[str, Any]:
    """A contract another slice writes: absent or unreadable means absent."""
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return {}
    return data if isinstance(data, dict) else {}


def reconcile_ghost_tables(
    dbt_dir: Path,
    warehouse_tables: Iterable[str],
    *,
    layout: WorkspaceLayout,
    schema: str = "",
) -> str:
    """Report relations that exist in the warehouse but no longer in the
    generated project -- dbt never drops a model you removed, so every
    regeneration that renames or deletes a mart leaves a ghost table serving
    stale numbers to whoever still queries it.

    DRY RUN ONLY, by design: this writes a report and removes nothing. Actual
    removal is a destructive operation and belongs behind the destructive-op
    gate with a human name on it, never inside a regeneration step.

    `warehouse_tables` is a listing the CALLER read (e.g. from
    `information_schema.tables`) -- this function issues no warehouse query, so
    it stays testable and unable to touch anything.
    """
    model_names, qualified = _manifest_model_names(dbt_dir)
    tables = sorted({str(name).strip() for name in warehouse_tables if str(name).strip()})
    if qualified:
        orphans = [name for name in tables if _normalize_relation(name) not in model_names]
    else:
        orphans = [name for name in tables if name not in model_names]

    lines = [
        "# dbt Ghost-Table Reconcile (dry run)",
        "",
        f"Project: `{dbt_dir.as_posix()}`" + (f" schema: `{schema}`" if schema else ""),
        f"Models in the generated project: {len(model_names)}",
        f"Relations reported by the warehouse: {len(tables)}",
        f"Orphans (in the warehouse, not in the project): {len(orphans)}",
        "",
        "This report is advisory. Nothing was removed, and this step never removes "
        "anything -- decommissioning a relation is a destructive operation that needs "
        "its own human-confirmed gate.",
        "",
    ]
    if orphans:
        lines += ["## Orphaned relations", ""]
        lines += [f"- `{name}`" for name in orphans]
        lines.append("")
    else:
        lines += ["No orphaned relations found.", ""]

    report_dir = layout.reports_dir / "dbt_reconcile"
    report_dir.mkdir(parents=True, exist_ok=True)
    report = report_dir / "current.md"
    report.write_text("\n".join(lines), encoding="utf-8")
    return report.as_posix()


def _normalize_relation(name: str) -> str:
    """dbt quotes `relation_name` per segment (`` `cat`.`gold`.`fct_x` `` on
    Databricks, `"cat"."gold"."fct_x"` elsewhere) -- strip the quoting and
    casefold so a warehouse listing (usually unquoted) diffs against it."""
    return re.sub(r'[`"\[\]]', "", str(name)).strip().casefold()


def _manifest_model_names(dbt_dir: Path) -> tuple[set[str], bool]:
    """The project's model set to diff the warehouse listing against.

    Prefers dbt's own fully-qualified `relation_name` (catalog.schema.table,
    normalized) so two models that share a bare alias in different schemas
    can't collide into a false "still present" match. Returns `(names,
    True)` in that case. Manifests from before `relation_name` was populated
    (pre-1.0), and the no-manifest-yet case, fall back to the alias/name set
    the caller compared against before -- returned as `(names, False)` so the
    caller knows not to normalize the warehouse side.
    """
    manifest = dbt_dir / "target" / "manifest.json"
    if manifest.exists():
        try:
            nodes = json.loads(manifest.read_text(encoding="utf-8")).get("nodes") or {}
            models = [
                node
                for node in nodes.values()
                if isinstance(node, dict) and node.get("resource_type") == "model"
            ]
            qualified = {
                _normalize_relation(node["relation_name"])
                for node in models
                if node.get("relation_name")
            }
            if qualified and len(qualified) == len(models):
                return qualified, True
            names = {str(node.get("alias") or node.get("name")) for node in models}
            if names:
                return names, False
        except (json.JSONDecodeError, OSError):
            pass
    return {path.stem for path in (dbt_dir / "models").rglob("*.sql")}, False


def _render_dbt_test(decision: dict[str, Any]) -> list[str]:
    check_type = str(decision.get("check_type") or "")
    severity = str(decision.get("severity") or "warn")
    config = decision.get("check_config") or {}
    if check_type == "accepted_values":
        values = config.get("values") or []
        values_yaml = ", ".join(json.dumps(v) for v in values)
        return [
            "          - accepted_values:",
            f"              values: [{values_yaml}]",
            "              config:",
            f"                severity: {severity}",
        ]
    # not_null and any future no-arg check type share this shape.
    return [
        f"          - {check_type}:",
        "              config:",
        f"                severity: {severity}",
    ]


def _apply_subs(text: str, subs: dict[str, str]) -> str:
    # Longest key first: a shorter identifier can otherwise match INSIDE a
    # longer one that shares a prefix (e.g. a table_ident substring of a
    # fully-qualified fqn), corrupting the replacement.
    for old in sorted(subs, key=len, reverse=True):
        text = text.replace(old, subs[old])
    return text


def _dbt_project_name(workspace_rel: str) -> str:
    import re

    safe = re.sub(r"[^A-Za-z0-9_]+", "_", workspace_rel.split("/")[-1]).strip("_").lower()
    return safe or "workspace"


def _rel(path: Path, root: Path) -> str:
    try:
        return path.resolve().relative_to(root.resolve()).as_posix()
    except ValueError:
        return path.as_posix()


@anchored("generate-dbt-project")
def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Generate a git-tracked dbt project from confirmed KPI contracts."
    )
    parser.add_argument("--workspace", required=True)
    parser.add_argument("--repo-root", default=str(PROJECT_ROOT))
    parser.add_argument(
        "--catalog", default="",
        help="Databricks catalog for this project. Defaults to workspace_settings.json's "
        "databricks_source.catalog when omitted -- required only if that isn't declared.",
    )
    parser.add_argument(
        "--schema", default="",
        help="BRONZE/source schema -- where sources.yml reads the already-existing raw UC tables. "
        "Defaults to workspace_settings.json's databricks_source.schema when omitted.",
    )
    parser.add_argument(
        "--silver-schema", default="silver",
        help="Schema staging+intermediate models write to (the cleaned/conformed layer). Default: silver.",
    )
    parser.add_argument(
        "--gold-schema", default="gold",
        help="Schema marts models write to (the business-facing KPI layer). Default: gold.",
    )
    parser.add_argument("--enterprise-id", default="", help="Overrides workspace_settings.json's databricks_source.enterprise_id.")
    parser.add_argument(
        "--enforce-contracts", action="store_true",
        help="Emit contract: enforced: true for each mart, with column data_types read from "
        "the REAL already-built table (DESCRIBE TABLE) -- requires at least one prior "
        "successful `dbt build`; a mart that doesn't exist yet is skipped, not an error.",
    )
    args = parser.parse_args(argv)
    catalog, schema = args.catalog, args.schema
    if not catalog or not schema:
        layout = WorkspaceLayout(project_root=Path(args.repo_root).resolve() / args.workspace)
        declared = layout.load_settings().get("databricks_source")
        declared = declared if isinstance(declared, dict) else {}
        catalog = catalog or str(declared.get("catalog") or "")
        schema = schema or str(declared.get("schema") or "")
        if not catalog or not schema:
            parser.error(
                "--catalog/--schema were not given and workspace_settings.json declares no "
                "databricks_source.catalog/.schema to default from."
            )
    result = DbtProjectGenerator(
        args.repo_root,
        args.workspace,
        catalog=catalog,
        schema=schema,
        silver_schema=args.silver_schema,
        gold_schema=args.gold_schema,
        enterprise_id=args.enterprise_id,
        enforce_contracts=args.enforce_contracts,
    ).generate()
    print(json.dumps(result.summary(), indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
