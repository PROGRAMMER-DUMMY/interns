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
from core.observability.cost_ledger import anchored

import argparse
import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from core.onboarding.kpi.sql_generator import (
    DuckDBKPISQLGenerator,
    READY_STATES,
    _safe_name,
)
from core.paths import PROJECT_ROOT
from core.profiling.dataset_identity import dataset_display_stem
from core.storage.workspace_layout import WorkspaceLayout

_DBT_ADAPTER_TYPE = "databricks"


@dataclass
class DbtProjectResult:
    dbt_project_dir: str
    kpi_count: int
    generated_kpi_ids: list[str] = field(default_factory=list)
    skipped: list[dict[str, str]] = field(default_factory=list)

    def summary(self) -> dict[str, Any]:
        return {
            "artifact_type": "dbt_project_generation_result",
            "version": 1,
            "generated_by": "generate-dbt-project",
            "dbt_project_dir": self.dbt_project_dir,
            "kpi_count": self.kpi_count,
            "generated_kpi_ids": self.generated_kpi_ids,
            "skipped": self.skipped,
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
        skipped: list[dict[str, str]] = []

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
                    self._ensure_staging_model(source, profile_map, required_columns, staging_dir)

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
                features_sql = "\n".join(
                    [
                        "{{ config(materialized='view') }}",
                        "",
                        f"-- Intermediate: {kpi.get('name', kpi_id)}",
                        "select",
                        ",\n".join(select_items),
                        _apply_subs(source_from_sql, subs),
                    ]
                )
                (intermediate_dir / f"{features_model}.sql").write_text(
                    features_sql, encoding="utf-8"
                )

                marts_subs = dict(subs)
                marts_subs[self._sql.quote_ident(kpi_id + "_features")] = (
                    "{{ ref('%s') }}" % features_model
                )
                result_ident = self._sql.quote_ident(kpi_id + "_results")
                result_body = self._sql._extract_result_select_body(
                    self._sql._result_view_sql(kpi, kpi_id), result_ident
                )
                marts_sql = "\n".join(
                    [
                        "{{ config(materialized='table') }}",
                        "",
                        f"-- Mart: {kpi.get('name', kpi_id)}",
                        _apply_subs(result_body, marts_subs),
                    ]
                )
                (marts_dir / f"fct_{kpi_id}.sql").write_text(marts_sql, encoding="utf-8")
                generated_kpi_ids.append(kpi_id)
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
        if self.enforce_contracts:
            self._write_contracts(marts_dir, generated_kpi_ids)

        return DbtProjectResult(
            dbt_project_dir=_rel(dbt_dir, self.repo_root),
            kpi_count=len(generated_kpi_ids),
            generated_kpi_ids=generated_kpi_ids,
            skipped=skipped,
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
        sql = "\n".join(
            [
                "{{ config(materialized='view') }}",
                "",
                f"-- Staging: 1:1 typed mirror of {stem}. No joins (medallion rule).",
                f"select {select_list}",
                "from {{ source('raw', '%s') }}" % stem,
            ]
        )
        (staging_dir / f"{model_name}.sql").write_text(sql, encoding="utf-8")

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
        items: list[str] = []
        for feature in kpi.get("features", []):
            column = self._sql._feature_expression(feature, source_aliases, profile_map)
            if not column:
                continue
            feat_name = feature["feature"]
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
                    f"      +schema: {self.gold_schema}",
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
        query_tags_json = json.dumps({"project_name": self.enterprise_id, "env": "prod"})
        profiles_lines.append(f"      query_tags: '{query_tags_json}'")
        profiles_lines.append("")
        (dbt_dir / "profiles.yml").write_text("\n".join(profiles_lines), encoding="utf-8")
        source_tables = "\n".join(
            f"      - name: {_safe_name(dataset_display_stem(source))}"
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


def _render_dbt_test(decision: dict[str, Any]) -> list[str]:
    check_type = str(decision.get("check_type") or "")
    severity = str(decision.get("severity") or "warn")
    config = decision.get("check_config") or {}
    if check_type == "accepted_values":
        values = config.get("values") or []
        values_yaml = ", ".join(json.dumps(v) for v in values)
        return [
            f"          - accepted_values:",
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
