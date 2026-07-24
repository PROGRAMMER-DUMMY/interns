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
    ) -> None:
        self.repo_root = Path(repo_root).resolve()
        self.workspace = (self.repo_root / workspace).resolve()
        self.layout = WorkspaceLayout(project_root=self.workspace)
        self.catalog = catalog
        self.schema = schema
        self.enterprise_id = enterprise_id or self.layout.enterprise_id()
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
                    'test-paths: ["tests"]',
                    "",
                    "models:",
                    f"  {self._project_name}:",
                    "    staging:",
                    "      +materialized: view",
                    "    intermediate:",
                    "      +materialized: view",
                    "    marts:",
                    "      +materialized: table",
                    "",
                ]
            ),
            encoding="utf-8",
        )
        (dbt_dir / "profiles.yml").write_text(
            "\n".join(
                [
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
                    "",
                ]
            ),
            encoding="utf-8",
        )
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
            "# dbt-expectations added here in Phase D2 (data-quality panel).\npackages: []\n",
            encoding="utf-8",
        )


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
    parser.add_argument("--catalog", required=True, help="Databricks catalog for this project.")
    parser.add_argument("--schema", required=True, help="Databricks schema for this project.")
    parser.add_argument("--enterprise-id", default="", help="Overrides workspace_settings.json's databricks_source.enterprise_id.")
    args = parser.parse_args(argv)
    result = DbtProjectGenerator(
        args.repo_root,
        args.workspace,
        catalog=args.catalog,
        schema=args.schema,
        enterprise_id=args.enterprise_id,
    ).generate()
    print(json.dumps(result.summary(), indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
