"""Authoritative KPI SQL generation for fully resolved feature mappings."""
from __future__ import annotations
from core.observability.cost_ledger import anchored

import argparse
import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from core.onboarding.kpi.feature_resolver import READY_STATES
from core.profiling.dataset_identity import dataset_display_stem
from core.onboarding.kpi.sensitive_masking import (
    is_feature_sensitive,
    load_sensitive_columns,
    mask_sql_expr,
)
from core.sql_safety import validate_expression_safe
from core.onboarding.relationships.base_source_selector import (
    BASE_SOURCE_DECISIONS_KEY,
    select_base_source,
)
from core.onboarding.relationships.contracts import (
    find_executable_relationship,
    load_relationship_contracts,
)
from core.storage.workspace_layout import WorkspaceLayout

# Standard ANSI SQL reserved words that also carry syntactic meaning inside a
# derived-formula expression (e.g. CASE...END). When a declared column's name
# collides with one of these (e.g. a "END"/"START" timestamp column used
# inside `CASE WHEN ... END`), an unqualified, unquoted occurrence in the
# formula text is inherently ambiguous between "the SQL keyword" and "the
# column" -- exactly how real SQL treats it: an unquoted reserved word is
# always parsed as the keyword, and quoting is required to reference it as an
# identifier. So for these names only the explicitly quoted form ("END") is
# substituted as a column reference; a bare occurrence is left untouched as
# the keyword. Found live: `CASE WHEN date_diff('day', START, END) <= x THEN
# 1 ELSE 0 END` (a column literally named END) had every bare "END" replaced
# blindly, including the CASE-closing keyword, producing invalid SQL.
_SQL_RESERVED_WORDS = frozenset({
    "AND", "OR", "NOT", "IS", "IN", "AS", "ON", "BY",
    "ALL", "ANY", "ASC", "DESC", "DISTINCT", "EXISTS", "BETWEEN", "LIKE",
    "NULL", "TRUE", "FALSE", "INTERVAL", "CAST", "WITH", "UNION", "LIMIT",
    "HAVING", "GROUP", "ORDER", "JOIN", "FROM", "WHERE", "SELECT",
    "CASE", "WHEN", "THEN", "ELSE", "END",
})


@dataclass(frozen=True)
class SQLGenerationResult:
    path: str
    kpi_id: str
    status: str
    dialect: str = "duckdb"

    def summary(self) -> dict[str, str]:
        return {
            "path": self.path,
            "kpi_id": self.kpi_id,
            "status": self.status,
            "dialect": self.dialect,
        }


class DuckDBKPISQLGenerator:
    def __init__(
        self,
        repo_root: str | Path,
        workspace: str | Path,
        *,
        dialect: str = "duckdb",
        catalog: str = "workspace",
        schema: str = "autoresearch",
    ):
        self.repo_root = Path(repo_root).resolve()
        self.workspace = (self.repo_root / workspace).resolve()
        self.layout = WorkspaceLayout(project_root=self.workspace)
        self.dialect = dialect
        self.catalog = catalog
        self.schema = schema
        if self.dialect not in {"duckdb", "databricks"}:
            raise ValueError(f"Unsupported SQL dialect: {self.dialect}")
        # Cached pipeline_decisions.json (None = not yet loaded; {} = absent/unparseable)
        self._pipeline_decisions_cache: dict[str, Any] | None = None

    def generate(self, kpi_id: str) -> SQLGenerationResult:
        mapping = self._load_mapping()
        kpi = next((item for item in mapping.get("kpis", []) if item.get("kpi_id") == kpi_id), None)
        if not kpi:
            raise ValueError(f"KPI not found: {kpi_id}")
        if not kpi.get("description"):
            kpi = dict(kpi)
            kpi["description"] = self._registry_description(kpi_id)
        blocked = [
            feature
            for feature in kpi.get("features", [])
            if feature.get("state") not in READY_STATES
        ]
        if blocked:
            names = ", ".join(feature.get("feature", "") for feature in blocked)
            raise ValueError(f"KPI {kpi_id} is not ready for SQL. Blocked features: {names}")

        profile_map = self._profile_map()
        resource_settings = self._resource_transform_settings()
        if (
            self.dialect == "duckdb"
            and resource_settings
            and resource_settings.get("local_execution_allowed") is False
        ):
            raise ValueError(
                "Local DuckDB SQL generation blocked by resource plan; "
                "generate Databricks SQL or reduce local workload."
            )
        relationships = load_relationship_contracts(
            self.repo_root,
            _rel(self.workspace, self.repo_root),
        )
        required_sources, required_columns = self._required_source_columns(
            kpi,
            profile_map,
            relationships,
        )
        staging_sql, stage_views = self._staging_views(profile_map, required_sources, required_columns)
        source_from_sql, source_aliases = self._kpi_source_from(
            kpi,
            profile_map,
            stage_views,
            relationships,
        )
        select_items = []
        # Sensitive-column masking is single-sourced in sensitive_masking so all
        # three engines agree on WHICH columns are sensitive and mask IDENTICALLY
        # (SHA-256 hex). Ref: core-audit ob-kpi-b.md (T2).
        sensitive_cols = load_sensitive_columns(self.layout)
        # Columns the result view consumes as RAW dates (age/days-since bands,
        # time-bucket anchors). Masking them in the features view would break the
        # downstream CAST(... AS DATE) and is pointless — the raw value is a
        # derivation INPUT, never projected to output (only the derived band is,
        # which HIPAA Safe Harbor permits). So a sensitive date column like DOB is
        # left raw HERE but still masked anywhere it would actually be emitted.
        from core.onboarding.kpi.result_view_builder import raw_date_input_columns
        raw_date_inputs = raw_date_input_columns(kpi)

        for feature in kpi.get("features", []):
            column = self._feature_expression(feature, source_aliases, profile_map)
            if column:
                res_type = feature.get("resolution_type")
                feat_name = feature['feature']

                # Mask when the feature's SOURCE COLUMN metadata says it is
                # sensitive — not by string-splitting the rendered expression
                # (which mis-detected derived formulas). Ref: ob-kpi-b.md:126.
                # EXCEPT when the column is only consumed as a raw date-arithmetic
                # input (see raw_date_inputs above): the raw value never reaches
                # output, so masking it only breaks the derivation.
                feat_cols = {
                    str(sc.get("column") or "").lower()
                    for sc in (feature.get("source_columns") or [])
                    if isinstance(sc, dict)
                }
                feat_cols.add(str(feat_name).lower())
                expr = column
                if (
                    is_feature_sensitive(feature, sensitive_cols)
                    and not (feat_cols & raw_date_inputs)
                ):
                    expr = mask_sql_expr(column, self.dialect)

                if res_type == "derived_formula":
                    select_items.append(
                        f"    {expr} AS {self.quote_ident(feat_name)}"
                    )
                else:
                    select_items.append(f"    {expr} AS {self.quote_ident(feat_name)}")
        
        if not select_items:
            # No feature expressions resolved — the KPI cannot produce
            # executable SQL. Emitting a ready_marker placeholder creates SQL
            # that is designed to fail the execution harness ("exposes only
            # placeholder readiness columns"), which is a doomed-stub smell.
            # Raise instead so callers record this KPI as blocked/skipped.
            raise ValueError(
                f"KPI {kpi_id} has no resolvable feature expressions. "
                "All features either lack source_columns or could not be "
                "matched to a dataset column. Resolve feature mappings "
                "before generating SQL."
            )

        # Build staging SQL — use Delta tables if they exist, fall back to CSV
        staging_sql_final = self._staging_with_delta(
            staging_sql, profile_map, required_sources
        )

        sql = "\n".join(
            [
                "-- Authoritative KPI SQL generated only from ready feature mappings.",
                f"-- Dialect: {self.dialect}",
                f"-- KPI: {kpi.get('name', kpi_id)}",
                f"-- Resource mode: {resource_settings.get('mode', 'unknown') if resource_settings else 'unknown'}",
                f"-- SQL strategy: {resource_settings.get('sql_strategy', 'standard_local') if resource_settings else 'standard_local'}",
                "",
                staging_sql_final.rstrip(),
                "",
                f"CREATE OR REPLACE VIEW {self.quote_ident(kpi_id + '_features')} AS",
                "SELECT",
                ",\n".join(select_items),
                source_from_sql.rstrip(),
                ";",
                "",
                self._result_view_sql(kpi, kpi_id).rstrip(),
                "",
                self._delta_write_sql(kpi_id),
            ]
        )
        suffix = "" if self.dialect == "duckdb" else f"_{self.dialect}"
        output = self.layout.solutions_dir / f"{kpi_id}{suffix}.sql"
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(sql, encoding="utf-8")
        self._ingest_bronze_delta(profile_map, required_sources)
        # The dated runs/<date>/ snapshot is written by WorkspaceFlow._write_result_preview,
        # which re-executes the on-disk SQL. Writing it here would freeze an "as-generated"
        # copy that drifts the moment the SQL is edited or re-executed downstream.
        return SQLGenerationResult(
            path=_rel(output, self.repo_root),
            kpi_id=kpi_id,
            status="generated",
            dialect=self.dialect,
        )

    def _resolve_run_source_mode(
        self,
        required_sources: list[str],
    ) -> str:
        """Determine the source layer for this generation run.

        Returns one of:
          "warehouse"   — all sources are materialized AND a warehouse exists
          "delta"       — all sources have Bronze Delta but no warehouse
          "csv"         — at least one source lacks a Bronze Delta layer

        The decision is made once per run so every catalog_raw_* view in the
        generated SQL uses the SAME layer.  Never mixes read_csv_auto and
        delta_scan within a single run.
        """
        if not required_sources:
            return "csv"

        warehouse_path = self.layout.state_dir / "warehouse.duckdb"
        all_delta = all(
            (self.layout.bronze_dir / _safe_name(Path(src).stem) / "_delta_log").exists()
            for src in required_sources
        )
        if not all_delta:
            return "csv"
        if warehouse_path.exists():
            return "warehouse"
        return "delta"

    def _staging_with_delta(
        self,
        staging_sql: str,
        profile_map: dict[str, dict[str, Any]],
        required_sources: list[str],
    ) -> str:
        """Replace read_csv_auto() with warehouse table refs or delta_scan().

        Source layer is resolved ONCE for the whole run via _resolve_run_source_mode
        so every catalog_raw_* view uses the same layer.  Mixed CSV+Delta within
        a single run is never emitted (BUG-022 fix).
        """
        if self.dialect != "duckdb":
            return staging_sql

        run_mode = self._resolve_run_source_mode(required_sources)
        if run_mode == "csv":
            # All sources must use raw CSV — nothing to replace.
            return staging_sql

        warehouse_path = self.layout.state_dir / "warehouse.duckdb"
        result = staging_sql

        # Resolve fact/dim classification once (warehouse mode only)
        fact_sources: set[str] = set()
        if run_mode == "warehouse":
            try:
                from core.onboarding.kpi.local_warehouse import warehouse_table_name
                mapping = self._load_mapping()
                wh_relationships = load_relationship_contracts(
                    self.repo_root, _rel(self.workspace, self.repo_root)
                )
                for kpi in mapping.get("kpis", []):
                    refs = [r for f in kpi.get("features", [])
                            for r in _feature_source_refs(f, self.repo_root)]
                    base = _choose_base_source(
                        refs,
                        profile_map,
                        wh_relationships,
                        grain_dimensions=_grain_dimensions(kpi),
                        pinned=self._pinned_base_source(kpi),
                    )
                    if base:
                        fact_sources.add(base)
            except Exception:
                # warehouse import failed — fall back uniformly to delta
                run_mode = "delta"

        for source in required_sources:
            stem = _safe_name(Path(source).stem)
            bronze_path = self.layout.bronze_dir / stem
            old_reader = f"read_csv_auto('{source}', union_by_name=true)"
            if run_mode == "warehouse":
                tname = warehouse_table_name(source, fact_sources)
                result = result.replace(old_reader, f'"{tname}"')
            else:
                # run_mode == "delta": all sources have bronze, use delta_scan uniformly
                result = result.replace(old_reader, f"delta_scan('{bronze_path.as_posix()}')")

        if run_mode == "warehouse":
            header = (
                f"-- Warehouse: {_rel(warehouse_path, self.repo_root)}\n"
                f"-- Staging views below proxy to registered fact/dim tables.\n"
                f"-- Run this SQL inside the warehouse:\n"
                f"--   uv run kpi-local-warehouse query --workspace {_rel(self.workspace, self.repo_root)} --sql @kpi.sql\n\n"
            )
        else:
            header = "INSTALL delta;\nLOAD delta;\n\n"
        return header + result

    def _delta_write_sql(self, kpi_id: str) -> str:
        """Emit COPY statement to write Gold results to Parquet (delta written via Python)."""
        gold_path = self.layout.gold_dir / f"{kpi_id}_results"
        return "\n".join([
            "-- Gold results -> Parquet (use deltalake Python lib to wrap as Delta)",
            f"-- COPY (SELECT * FROM {self.quote_ident(kpi_id + '_results')})",
            f"--   TO '{gold_path.as_posix()}/data.parquet' (FORMAT PARQUET);",
        ])

    def _ingest_bronze_delta(
        self,
        profile_map: dict[str, dict[str, Any]],
        required_sources: list[str],
    ) -> None:
        """Write each required CSV source to a Bronze Delta table if not already present."""
        if self.dialect != "duckdb":
            return
        try:
            import deltalake
            import pyarrow.csv as pa_csv
        except ImportError:
            return  # deltalake/pyarrow not installed — skip silently
        for source in required_sources:
            bronze_path = self.layout.bronze_dir / _safe_name(Path(source).stem)
            if (bronze_path / "_delta_log").exists():
                continue  # already ingested
            full_path = self.repo_root / source
            if not full_path.exists():
                continue
            bronze_path.mkdir(parents=True, exist_ok=True)
            try:
                table = pa_csv.read_csv(str(full_path))
                deltalake.write_deltalake(str(bronze_path), table, mode="overwrite")
            except Exception:
                pass  # ingestion failure must not break SQL generation

    def _profile_map(self) -> dict[str, dict[str, Any]]:
        profile_index = self.layout.profiles_dir / "profile_index.json"
        if not profile_index.exists():
            return {}
        data = json.loads(profile_index.read_text(encoding="utf-8"))
        return {
            _repo_path(str(profile.get("path") or ""), self.repo_root): profile
            for profile in data.get("profiles", [])
            if profile.get("path")
        }

    def _resource_transform_settings(self) -> dict[str, Any]:
        path = self.layout.contracts_dir / "source_to_target_plan.json"
        if not path.exists():
            return {}
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            return {}
        settings = data.get("resource_transform_settings")
        return settings if isinstance(settings, dict) else {}

    def _staging_views(
        self,
        profile_map: dict[str, dict[str, Any]],
        required_sources: list[str] | None = None,
        required_columns: dict[str, set[str]] | None = None,
    ) -> tuple[str, dict[str, str]]:
        view_lines = []
        stage_views = {}
        union_parts = []
        required_source_set = set(required_sources or [])
        # "csv" is a local file profile (core.profiling.data_model_profiler).
        # "delta" is a Unity-Catalog-sourced profile
        # (core.profiling.databricks_table_profiler) -- its `path` is already
        # a live, fully-qualified `catalog`.`schema`.`table` reference, not a
        # local file to bootstrap from. Previously only "csv" profiles were
        # staged at all, so a UC-sourced dataset silently produced no staging
        # view whatsoever for databricks-dialect generation. A "delta" profile
        # is only stageable for the databricks dialect -- the duckdb dialect
        # has no local proxy for a remote UC table, and read_csv_auto() on a
        # fqn string would be nonsense.
        stageable_profiles = [
            (path, profile)
            for path, profile in sorted(profile_map.items())
            if (
                profile.get("format") == "csv"
                or (profile.get("format") == "delta" and self.dialect == "databricks")
            )
            and (not required_source_set or path in required_source_set)
        ]
        for idx, (rel_path, _profile) in enumerate(stageable_profiles, start=1):
            stem = _safe_name(dataset_display_stem(rel_path))
            view_name = f"catalog_raw_{stem}" if self.dialect == "duckdb" else f"stage_{idx:03d}_{stem}"
            stage_views[rel_path] = view_name
            select_list = self._stage_select_list(rel_path, _profile, required_columns or {})
            if self.dialect == "databricks":
                # A UC-sourced profile's path IS the real source table --
                # reference it directly. table_ident(stem) would instead
                # reconstruct `self.catalog`.`self.schema`.`stem`, which is
                # only correct for a local-file profile whose CSV stem is
                # assumed to match a same-named table already registered
                # under the generator's OWN target catalog/schema -- wrong
                # when the UC source lives in a different catalog/schema
                # than the KPI's output target.
                table_name = rel_path if _profile.get("format") == "delta" else self.table_ident(stem)
                view_lines.append(
                    f"CREATE OR REPLACE TEMP VIEW {self.quote_ident(view_name)} AS "
                    f"SELECT {select_list} FROM {table_name};"
                )
            else:
                view_lines.append(
                    f"CREATE OR REPLACE VIEW {self.quote_ident(view_name)} AS "
                    f"SELECT {select_list} FROM read_csv_auto('{rel_path}', union_by_name=true);"
                )
            union_parts.append(f"SELECT * FROM {self.quote_ident(view_name)}")
        if not union_parts and not required_source_set:
            return "CREATE OR REPLACE VIEW all_workspace_rows AS SELECT 1 AS ready_marker;", stage_views
        if not union_parts:
            return "", stage_views
        union_operator = "UNION ALL" if self.dialect == "databricks" else "UNION ALL BY NAME"
        lines = []
        if self.dialect == "duckdb":
            lines.append("-- BEGIN CATALOG BOOTSTRAP")
        lines.extend(view_lines)
        if self.dialect == "duckdb":
            lines.append("-- END CATALOG BOOTSTRAP")
        lines.extend(
            [
                "CREATE OR REPLACE TEMP VIEW all_workspace_rows AS"
                if self.dialect == "databricks"
                else "CREATE OR REPLACE VIEW all_workspace_rows AS",
                f"\n{union_operator}\n".join(union_parts) + ";",
            ]
        )
        return ("\n".join(lines), stage_views)

    def _stage_select_list(
        self,
        rel_path: str,
        profile: dict[str, Any],
        required_columns: dict[str, set[str]],
    ) -> str:
        schema = _schema(profile)
        requested = required_columns.get(rel_path) or set()
        selected = [column for column in schema if column in requested]
        if not selected:
            selected = list(schema)
        return ", ".join(self.quote_ident(column) for column in selected) or "*"

    def _required_source_columns(
        self,
        kpi: dict[str, Any],
        profile_map: dict[str, dict[str, Any]],
        relationships: list[dict[str, Any]],
    ) -> tuple[list[str], dict[str, set[str]]]:
        feature_refs = [
            ref
            for feature in kpi.get("features", [])
            for ref in _feature_source_refs(feature, self.repo_root)
        ]
        feature_refs.extend(self._derived_formula_refs(kpi, "", profile_map))
        base_source = _choose_base_source(
            feature_refs,
            profile_map,
            relationships,
            grain_dimensions=_grain_dimensions(kpi),
            pinned=self._pinned_base_source(kpi),
        )
        if not base_source:
            return [], {}

        required_refs = [
            self._choose_feature_ref(feature, base_source, feature_refs, profile_map)
            for feature in kpi.get("features", [])
        ]
        for feature in kpi.get("features", []):
            if feature.get("resolution_type") != "derived_formula":
                continue
            # Every declared (dataset, column) pair, inclusive -- see
            # _declared_formula_refs. This copy drives the catalog
            # bootstrap, so it must stay inclusive of every declared
            # column regardless of bare-vs-qualified use in the formula.
            required_refs.extend(_declared_formula_refs(feature, base_source, profile_map))

        required_sources = _unique_preserve_order(
            [base_source]
            + [
                ref["dataset"]
                for ref in required_refs
                if ref and ref.get("dataset") and ref.get("dataset") != base_source
            ]
        )
        columns_by_source: dict[str, set[str]] = {source: set() for source in required_sources}
        for ref in required_refs:
            if ref and ref.get("dataset") and ref.get("column"):
                columns_by_source.setdefault(ref["dataset"], set()).add(ref["column"])

        for source in required_sources[1:]:
            relationship = find_executable_relationship(relationships, base_source, source)
            if relationship:
                columns_by_source.setdefault(base_source, set()).add(str(relationship.get("left_column") or ""))
                columns_by_source.setdefault(source, set()).add(str(relationship.get("right_column") or ""))
        for source in list(columns_by_source):
            columns_by_source[source] = {column for column in columns_by_source[source] if column}
        return required_sources, columns_by_source

    def _kpi_source_from(
        self,
        kpi: dict[str, Any],
        profile_map: dict[str, dict[str, Any]],
        stage_views: dict[str, str],
        relationships: list[dict[str, Any]],
    ) -> tuple[str, dict[str, str]]:
        feature_refs = [
            ref
            for feature in kpi.get("features", [])
            for ref in _feature_source_refs(feature, self.repo_root)
        ]
        feature_refs.extend(self._derived_formula_refs(kpi, "", profile_map))
        base_source = _choose_base_source(
            feature_refs,
            profile_map,
            relationships,
            grain_dimensions=_grain_dimensions(kpi),
            pinned=self._pinned_base_source(kpi),
        )
        if not base_source:
            return "FROM all_workspace_rows", {}
        required_refs = [
            self._choose_feature_ref(feature, base_source, feature_refs, profile_map)
            for feature in kpi.get("features", [])
        ]
        for feature in kpi.get("features", []):
            if feature.get("resolution_type") != "derived_formula":
                continue
            # This loop builds the OUTER JOIN chain specifically (unlike
            # _required_source_columns's identical-looking loop, which
            # drives the catalog bootstrap and must stay inclusive). Only a
            # column referenced BARE in the formula needs the outer join;
            # a column only ever referenced dot-qualified (e.g. `i.acct`)
            # is scoped to the formula's own subquery and needs the
            # bootstrap view to exist (already ensured elsewhere) but not
            # an outer join, which would fan the base grain out to one row
            # per joined-table row.
            bare = _bare_formula_columns(feature)
            for ref in _declared_formula_refs(feature, base_source, profile_map):
                if ref["column"] not in bare:
                    continue
                required_refs.append(ref)
        required_sources = _unique_preserve_order(
            [base_source]
            + [
                ref["dataset"]
                for ref in required_refs
                if ref and ref.get("dataset") and ref.get("dataset") != base_source
            ]
        )
        missing_stages = [source for source in required_sources if source not in stage_views]
        if missing_stages:
            raise ValueError(
                f"KPI {kpi.get('kpi_id')} has source datasets without staging views: "
                + ", ".join(missing_stages)
            )

        aliases = {source: f"s{idx}" for idx, source in enumerate(required_sources)}
        base_alias = aliases[base_source]
        lines = [
            f"FROM {self.quote_ident(stage_views[base_source])} AS {base_alias}",
        ]
        for source in required_sources[1:]:
            relationship = find_executable_relationship(relationships, base_source, source)
            if not relationship:
                raise ValueError(
                    f"KPI {kpi.get('kpi_id')} needs `{source}` but no executable relationship "
                    f"contract links it to base source `{base_source}`. Run "
                    "`uv run build-relationship-contracts --workspace "
                    f"{_rel(self.workspace, self.repo_root)}` and confirm candidate relationships "
                    "before trusted SQL generation."
                )
            join = _relationship_join_condition(
                relationship,
                base_alias,
                aliases[source],
                self,
            )
            if not join:
                raise ValueError(
                    f"KPI {kpi.get('kpi_id')} relationship contract is malformed for "
                    f"`{base_source}` -> `{source}`"
                )
            lines.append(
                f"LEFT JOIN {self.quote_ident(stage_views[source])} AS {aliases[source]} ON {join}"
            )
        return "\n".join(lines), aliases

    def _derived_formula_refs(
        self,
        kpi: dict[str, Any],
        base_source: str,
        profile_map: dict[str, dict[str, Any]],
    ) -> list[dict[str, str]]:
        refs = []
        for feature in kpi.get("features", []):
            if feature.get("resolution_type") != "derived_formula":
                continue
            refs.extend(_declared_formula_refs(feature, base_source, profile_map))
        return refs

    def _choose_feature_ref(
        self,
        feature: dict[str, Any],
        base_source: str,
        all_refs: list[dict[str, str]],
        profile_map: dict[str, dict[str, Any]],
    ) -> dict[str, str] | None:
        # Canonical per-feature resolution shared with the engine generators
        # (choose_feature_ref module function) so every engine reads the same
        # sources.
        return choose_feature_ref(
            feature, base_source, all_refs, profile_map, self.repo_root
        )

    def _feature_expression(
        self,
        feature: dict[str, Any],
        source_aliases: dict[str, str],
        profile_map: dict[str, dict[str, Any]],
    ) -> str | None:
        if feature.get("resolution_type") == "derived_formula":
            formula = _derived_formula(feature)
            if not formula:
                return None
            # T4: the formula body is workspace-owned text inlined verbatim into
            # executable SQL. Reject statement terminators / comment sequences /
            # DDL-DML keywords before inlining so a hostile derivation rule cannot
            # inject SQL. Legitimate arithmetic/function formulas pass unchanged.
            validate_expression_safe(formula, context="derived KPI formula")
            # A formula may reference a SOURCE TABLE by its raw name
            # (`FROM "encounters" p` in a self-join). The script only creates
            # catalog views, so map known dataset stems to their view names —
            # the author of a custom rule cannot know generator-internal names.
            for source, source_profile in profile_map.items():
                stem = _safe_name(dataset_display_stem(source))
                if self.dialect == "duckdb":
                    view = self.quote_ident(f"catalog_raw_{stem}")
                elif source_profile.get("format") == "delta":
                    # UC-sourced: `source` IS the real table reference already
                    # (see _staging_views' identical reasoning) -- table_ident
                    # would wrongly reconstruct it under the generator's own
                    # target catalog/schema instead.
                    view = source
                else:
                    view = self.table_ident(stem)
                formula = re.sub(
                    rf'(?i)(\b(?:FROM|JOIN)\s+)"{re.escape(stem)}"',
                    lambda m, v=view: m.group(1) + v,
                    formula,
                )
            for column in sorted(_formula_inputs(feature), key=len, reverse=True):
                qualified = self._qualified_column(column, source_aliases, profile_map)
                if qualified:
                    # Consume an already-quoted occurrence ("START") whole so
                    # qualification cannot nest quotes (s0."START"" bug), and
                    # never rewrite alias-prefixed references (p."START" inside
                    # a self-join formula belongs to the formula's own alias).
                    # A column whose name is itself a SQL reserved word (END,
                    # CASE, ...) is only substituted when explicitly quoted --
                    # a bare occurrence is the keyword, not the column (see
                    # _SQL_RESERVED_WORDS).
                    if column.upper() in _SQL_RESERVED_WORDS:
                        pattern = rf'"{re.escape(column)}"'
                    else:
                        pattern = rf'(?<![.\w"])(?:"{re.escape(column)}"|{re.escape(column)}\b)(?!")'
                    formula = re.sub(pattern, qualified, formula)
            return formula
        ref = self._choose_feature_ref(
            feature,
            next(iter(source_aliases), ""),
            [ref for ref in _feature_source_refs(feature, self.repo_root)],
            profile_map,
        )
        if not ref:
            return None
        alias = source_aliases.get(ref["dataset"])
        if not alias:
            return self.quote_ident(ref["column"])
        return f"{alias}.{self.quote_ident(ref['column'])}"

    def _qualified_column(
        self,
        column: str,
        source_aliases: dict[str, str],
        profile_map: dict[str, dict[str, Any]],
    ) -> str | None:
        for source, alias in source_aliases.items():
            if column in _schema(profile_map.get(source, {})):
                return f"{alias}.{self.quote_ident(column)}"
        return None

    def _registry_description(self, kpi_id: str) -> str:
        path = self.layout.contracts_dir / "kpi_registry.json"
        if not path.exists():
            return ""
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            for entry in data.get("kpis", []):
                if entry.get("kpi_id") == kpi_id or entry.get("id") == kpi_id:
                    return entry.get("description", "")
            idx = int(kpi_id.split("_")[-1]) - 1
            entries = data.get("kpis", [])
            if 0 <= idx < len(entries):
                return entries[idx].get("description", "")
        except Exception:
            pass
        return ""

    def _load_mapping(self) -> dict[str, Any]:
        path = self.layout.contracts_dir / "kpi_feature_mapping.json"
        if not path.exists():
            raise FileNotFoundError(f"feature mapping not found: {path}")
        return json.loads(path.read_text(encoding="utf-8"))

    def _result_view_sql(self, kpi: dict[str, Any], kpi_id: str) -> str:
        """Build the result-view SQL for a KPI. Workspace-agnostic.

        Delegates to the generic builder which parses `kpi.metric` + `kpi.cuts`
        + `kpi.features` and composes SELECT/GROUP BY/WHERE/ORDER BY. For
        complex shapes the builder returns a clearly-commented `SELECT *`
        fallback so the pipeline still produces a valid view but the
        reviewer sees the gap.

        Loads the resolved ``denominator_scope`` facet from
        ``pipeline_decisions.json`` (if present) so a recorded within-group
        decision is wired through to the denominator window instead of being
        silently ignored (design/kpi_intent_contract.md §7 phase 1 / BUG-025).
        """
        from core.onboarding.kpi.result_view_builder import build_result_view_sql

        feature_view = self.quote_ident(kpi_id + "_features")
        result_view = self.quote_ident(kpi_id + "_results")
        decisions = self._pipeline_decisions()
        denominator_scope = (
            (decisions.get("percentage_denominator_scopes") or {}).get(kpi_id)
        )
        grain_bucketing = (
            (decisions.get("grain_bucketing_decisions") or {}).get(kpi_id)
        )
        return build_result_view_sql(
            kpi,
            kpi_id=kpi_id,
            feature_view=feature_view,
            result_view=result_view,
            dialect=self.dialect,
            denominator_scope=denominator_scope,
            grain_bucketing=grain_bucketing,
        )

    def _pipeline_decisions(self) -> dict[str, Any]:
        """Load ``pipeline_decisions.json`` once per generator instance.

        Returns an empty dict when the file is absent, unreadable, or contains
        invalid JSON.  Cached so repeated KPI generations in one run don't hit
        the filesystem multiple times.
        """
        if self._pipeline_decisions_cache is None:
            path = self.layout.contracts_dir / "pipeline_decisions.json"
            if path.exists():
                try:
                    data = json.loads(path.read_text(encoding="utf-8"))
                    self._pipeline_decisions_cache = data if isinstance(data, dict) else {}
                except (json.JSONDecodeError, OSError):
                    self._pipeline_decisions_cache = {}
            else:
                self._pipeline_decisions_cache = {}
        return self._pipeline_decisions_cache

    def _pinned_base_source(self, kpi: dict[str, Any]) -> str | None:
        """Human-recorded base-source decision for this KPI, if any."""
        decisions = self._pipeline_decisions().get(BASE_SOURCE_DECISIONS_KEY)
        if not isinstance(decisions, dict):
            return None
        return str(decisions.get(str(kpi.get("kpi_id") or "")) or "") or None

    def quote_ident(self, value: str) -> str:
        if self.dialect == "databricks":
            return "`" + str(value).replace("`", "``") + "`"
        return quote_ident(value)

    def table_ident(self, table: str) -> str:
        parts = [self.catalog, self.schema, table]
        return ".".join(self.quote_ident(part) for part in parts if part)


def quote_ident(value: str) -> str:
    return '"' + str(value).replace('"', '""') + '"'


def _feature_source_refs(feature: dict[str, Any], repo_root: Path) -> list[dict[str, str]]:
    refs = []
    for source in feature.get("source_columns") or []:
        dataset = _repo_path(str(source.get("dataset") or ""), repo_root)
        column = str(source.get("column") or "")
        if not dataset and "." in column:
            dataset, column = _split_dataset_column(column, repo_root)
        if dataset and column:
            refs.append({"dataset": dataset, "column": column})
    return refs


def _split_dataset_column(value: str, repo_root: Path) -> tuple[str, str]:
    normalized = value.replace("\\", "/")
    for marker in (".csv.", ".parquet."):
        if marker in normalized:
            dataset, column = normalized.split(marker, 1)
            return _repo_path(dataset + marker[:-1], repo_root), column
    parts = normalized.rsplit(".", 1)
    if len(parts) == 2:
        return _repo_path(parts[0], repo_root), parts[1]
    return "", value


def _derived_formula(feature: dict[str, Any]) -> str:
    # Evidence entries are append-only: re-applying a workspace definition
    # (e.g. a human correcting an earlier formula) appends a NEW entry
    # rather than replacing the old one, so the list can carry several
    # "workspace_feature_definition" entries for the same feature over
    # successive resolver runs. The most recently appended one is the
    # human's latest word on this formula -- search in reverse so it wins,
    # not whichever happened to be recorded first. Found live: a corrected
    # formula (fixing an unqualified column reference) was silently ignored
    # in favor of the original, already-superseded one.
    for ev in reversed(feature.get("evidence") or []):
        detail = str(ev.get("detail") or "")
        if ev.get("type") == "workspace_feature_definition" and _FORMULA_SHAPED.search(detail):
            return detail
    for column in feature.get("source_columns") or []:
        detail = str(column.get("detail") or "")
        if _FORMULA_SHAPED.search(detail):
            return detail
    return ""


# Distinguishes a genuine SQL-formula "workspace_feature_definition" evidence
# detail from a plain "dataset.column" pin string (both share the same
# evidence type; only the detail text tells them apart). A plain pin like
# "workspaces/demo/datasets/shipments.csv.Id" never has parens, comparison
# operators, or CASE/EXISTS -- but neither does a bare-comparison formula
# with no function call, e.g. `CASE WHEN Status = 'DELIVERED' THEN 1 ELSE 0
# END`, which requiring "(" alone rejected (found live: kpi_004's "delivered"
# formula silently vanished from the generated features view -- no formula
# text was recognized at all, so the feature produced no SELECT item, and a
# WHERE clause elsewhere still referenced it as a features-view column,
# raising a binder error). Mirrors the same detection already used for
# --custom-definition text in blocker_workflow.py's
# _custom_definition_source_columns.
_FORMULA_SHAPED = re.compile(r"[()<>=]|\bexists\b|\bcase\b", re.IGNORECASE)


def _formula_inputs(feature: dict[str, Any]) -> list[str]:
    inputs = [
        str(column.get("column") or "")
        for column in feature.get("source_columns") or []
        if column.get("column")
    ]
    if inputs:
        # Explicit source_columns are the authoritative, human-confirmed
        # binding -- trust them and skip re-parsing the raw formula/
        # definition text entirely. Doing both unconditionally (as this
        # used to) re-tokenized the definition's free-text prose (e.g. a
        # WHERE/EXISTS custom definition written in natural English) with
        # a bare regex that had no stopword filtering at all, so ordinary
        # words like "period"/"account" got treated as extra candidate
        # columns and resolved against ANY dataset with a matching column
        # name -- pulling in a completely unrelated table. Found live:
        # "period" from "... for that account in the period" resolved to
        # settlements.csv, which the KPI never referenced.
        return _unique_preserve_order(inputs)
    formula = _derived_formula(feature)
    if formula:
        from core.onboarding.features.expression import extract_expression

        inputs.extend(
            token for token in extract_expression(formula).identifiers
            if token.lower() not in {"date_diff", "year", "month", "day"}
        )
    return _unique_preserve_order(inputs)


def _declared_formula_refs(
    feature: dict[str, Any],
    base_source: str,
    profile_map: dict[str, dict[str, Any]],
) -> list[dict[str, str]]:
    """(dataset, column) refs for a derived formula's inputs.

    Explicit source_columns are the authoritative, human-confirmed binding --
    every declared (dataset, column) PAIR is trusted directly, not routed
    through a column-name-keyed dict. A name-keyed dict can only remember one
    dataset per column NAME, which breaks the moment the SAME bare name is
    declared against two DIFFERENT datasets -- a normal shape for a join key
    shared by both sides (e.g. `inv_no` on both invoices.csv and disputes.csv
    for a formula that dot-qualifies each occurrence, `iv.inv_no`/
    `d.inv_no`): whichever pair happened to be declared last silently
    overwrote the other, dropping that dataset's column from the required-
    columns/catalog-bootstrap set entirely.
    Found live: kpi_010's `perfect_shipment` formula declared both
    `invoices.inv_no` and `disputes.inv_no`; the bootstrap view for
    invoices.csv ended up selecting only `ship_no`, and the formula's own
    `iv.inv_no = d.inv_no` join failed to bind at execution.
    Falls back to `_source_for_column`'s bare-name matching only when a
    formula token has no explicit declaration at all (empty source_columns).
    """
    declared_pairs = [
        {"dataset": str(sc.get("dataset")), "column": str(sc.get("column"))}
        for sc in (feature.get("source_columns") or [])
        if isinstance(sc, dict) and sc.get("column") and sc.get("dataset")
    ]
    if declared_pairs:
        return declared_pairs
    refs = []
    for column in _formula_inputs(feature):
        source = _source_for_column(column, base_source, profile_map)
        if source:
            refs.append({"dataset": source, "column": column})
    return refs


def _bare_formula_columns(feature: dict[str, Any]) -> set[str]:
    """The subset of a derived formula's declared source_columns that need
    the OUTER row's join, as opposed to only existing inside the formula's
    own self-contained subqueries.

    A formula may declare columns from a dataset OTHER than the KPI's base
    source purely so the catalog bootstrap creates a view for that dataset
    (so the formula's own inner subquery -- e.g. a correlated EXISTS check
    against another table -- has something to reference via the FROM/JOIN
    raw-table-name rewrite). Such a column is always written with an
    explicit alias inside the formula (``i.acct``, ``x.Date``) and is never
    substituted for the outer row (the substitution regex skips dot-
    qualified occurrences on purpose). Only a column referenced BARE
    (unqualified) anywhere in the formula text is genuinely correlated to
    the outer row and needs that dataset outer-joined.

    Treating every declared column as an outer-join need regardless
    (the previous behavior) added a superfluous LEFT JOIN whenever a
    formula referenced another table only inside its own subquery, fanning
    the base grain out to one row per joined-table row and silently
    inflating every downstream count -- found live: a per-account boolean
    (238 accounts) summed to over 4,500 once the KPI accidentally joined in
    every one of an account's invoices.
    """
    formula = _derived_formula(feature)
    if not formula:
        return set(_formula_inputs(feature))
    bare: set[str] = set()
    for column in _formula_inputs(feature):
        pattern = rf'(?<![.\w"])(?:"{re.escape(column)}"|{re.escape(column)}\b)(?!")'
        if re.search(pattern, formula):
            bare.add(column)
    return bare


def _choose_base_source(
    refs: list[dict[str, str]],
    profile_map: dict[str, dict[str, Any]],
    relationships: list[dict[str, Any]] | None = None,
    *,
    grain_dimensions: list[str] | tuple[str, ...] = (),
    pinned: str | None = None,
) -> str:
    # Relationship-graph scoring (coverage, grain, fan-out safety, evidence);
    # row count is a final tiebreak only. Every engine and the planner delegate
    # to the same selector so they all anchor the identical base. A near-tie is
    # resolved deterministically here (top score) — the source-to-target
    # planner is the gate that turns a near-tie into a blocker-panel question.
    selection = select_base_source(
        refs,
        profile_map,
        relationships,
        grain_dimensions=grain_dimensions,
        pinned=pinned,
    )
    return selection.base_source


def choose_feature_ref(
    feature: dict[str, Any],
    base_source: str,
    all_refs: list[dict[str, str]],
    profile_map: dict[str, dict[str, Any]],
    repo_root: Path,
) -> dict[str, str] | None:
    """Pick the ONE source ref this feature resolves to, preferring the base.

    Order: a ref already on the base source -> the same column present in the
    base schema -> a ref in the base's source group -> the first ref. This is
    the canonical per-feature resolution every engine must share; unioning ALL
    candidate refs instead pulled in datasets with no executable relationship
    (the cross-engine parity skips/failures).
    """
    refs = _feature_source_refs(feature, repo_root)
    feature_name = str(feature.get("feature") or "")
    # A single recorded ref is not a choice to make -- it's the one thing a
    # human (or an earlier proven-direct/proven-alias resolution) already
    # said this feature means. The base-source-preference logic below
    # matches candidates by column NAME ALONE against the base table's own
    # schema, which silently substitutes a same-named column from a
    # DIFFERENT table whenever the base source happens to also have a
    # column with that name (found live: a feature confirmed as
    # `cargo_claims.Id` silently resolved to `shipments.Id` instead, purely
    # because the base source -- shipments -- also has an "Id" column).
    # Only fall through to that disambiguation logic when there are
    # genuinely multiple candidates to choose among.
    if len(refs) == 1:
        return refs[0]
    if refs:
        for ref in refs:
            if ref["dataset"] == base_source:
                return ref
        base_schema = _schema(profile_map.get(base_source, {}))
        for ref in refs:
            if ref["column"] in base_schema:
                return {"dataset": base_source, "column": ref["column"]}
        base_group = _source_group(base_source)
        for ref in refs:
            if _source_group(ref["dataset"]) == base_group:
                return ref
        return refs[0]
    base_schema = _schema(profile_map.get(base_source, {}))
    for candidate in [feature_name, *_formula_inputs(feature)]:
        if candidate in base_schema:
            return {"dataset": base_source, "column": candidate}
    for ref in all_refs:
        if ref["column"] == feature_name:
            return ref
    return None


def plan_required_sources(
    kpi: dict[str, Any],
    profile_map: dict[str, dict[str, Any]],
    repo_root: Path,
    relationships: list[dict[str, Any]] | None = None,
    *,
    pinned: str | None = None,
) -> tuple[str, list[str], list[dict[str, str]]]:
    """The canonical source plan for a KPI: (base_source, required_sources, refs).

    Single source of truth for WHICH datasets a KPI reads and joins. The SQL
    generator derives its FROM/JOIN chain from this; the Polars and PySpark
    generators MUST consume the same plan so cross-engine parity starts from
    identical sources. ``refs`` is the chosen one-ref-per-feature list
    (including derived-formula inputs); ``base_source`` is "" when no plan can
    be made. ``relationships``/``pinned`` feed the relationship-graph base
    selector; pass them identically from every engine.
    """
    feature_refs = [
        ref
        for feature in kpi.get("features", [])
        for ref in _feature_source_refs(feature, repo_root)
    ]
    base_source = _choose_base_source(
        feature_refs,
        profile_map,
        relationships,
        grain_dimensions=_grain_dimensions(kpi),
        pinned=pinned,
    )
    if not base_source:
        return "", [], []
    required_refs = [
        choose_feature_ref(feature, base_source, feature_refs, profile_map, repo_root)
        for feature in kpi.get("features", [])
    ]
    for feature in kpi.get("features", []):
        if feature.get("resolution_type") != "derived_formula":
            continue
        # Uses _declared_formula_refs (see its docstring) -- this copy
        # matters most since plan_required_sources is the canonical,
        # explicitly cross-engine-shared source plan (SQL, Polars, PySpark
        # all consume it).
        #
        # Only a BARE (unqualified) reference genuinely needs this dataset
        # outer-joined into the base grain. A column the formula only ever
        # references dot-qualified (e.g. `i.acct` inside its own correlated
        # subquery) doesn't need an outer join at all -- adding one fans the
        # base grain out to one row per joined-table row, silently inflating
        # every downstream count. Found live: a per-account boolean (240
        # accounts) summed to over 4,500 once the plan joined in every one
        # of an account's invoices for a formula that never referenced the
        # outer join in the first place.
        bare = _bare_formula_columns(feature)
        for ref in _declared_formula_refs(feature, base_source, profile_map):
            if ref["column"] not in bare:
                continue
            required_refs.append(ref)
    chosen = [ref for ref in required_refs if ref and ref.get("dataset")]
    required_sources = _unique_preserve_order(
        [base_source]
        + [ref["dataset"] for ref in chosen if ref["dataset"] != base_source]
    )
    return base_source, required_sources, chosen


def _grain_dimensions(kpi: dict[str, Any]) -> list[str]:
    """KPI cut tokens for grain-compatibility scoring (mirrors the planner's
    cuts split so every engine scores grain identically)."""
    return [
        part.strip()
        for part in re.split(r"[,;]", str(kpi.get("cuts") or ""))
        if part.strip()
    ]


def _source_for_column(
    column: str,
    base_source: str,
    profile_map: dict[str, dict[str, Any]],
) -> str:
    if column in _schema(profile_map.get(base_source, {})):
        return base_source
    base_group = _source_group(base_source)
    same_group = [
        source
        for source, profile in profile_map.items()
        if _source_group(source) == base_group and column in _schema(profile)
    ]
    if same_group:
        return sorted(same_group)[0]
    matches = [
        source
        for source, profile in profile_map.items()
        if column in _schema(profile)
    ]
    return sorted(matches)[0] if matches else ""


def _relationship_join_condition(
    relationship: dict[str, Any],
    left_alias: str,
    right_alias: str,
    generator: DuckDBKPISQLGenerator,
) -> str:
    left_column = str(relationship.get("left_column") or "")
    right_column = str(relationship.get("right_column") or "")
    if not left_column or not right_column:
        return ""
    return (
        f"{left_alias}.{generator.quote_ident(left_column)} = "
        f"{right_alias}.{generator.quote_ident(right_column)}"
    )


def _schema(profile: dict[str, Any]) -> dict[str, Any]:
    schema = profile.get("schema") or {}
    if isinstance(schema, dict):
        return schema
    return {}


def _repo_path(value: str, root: Path) -> str:
    if not value:
        return ""
    normalized = value.replace("\\", "/")
    marker = "workspaces/"
    if marker in normalized:
        return normalized[normalized.index(marker):]
    path = Path(value)
    if path.is_absolute():
        return _rel(path, root)
    return normalized


def _source_group(source: str) -> str:
    normalized = source.replace("\\", "/")
    parts = normalized.split("/")
    if "datasets" in parts:
        idx = parts.index("datasets")
        if len(parts) > idx + 2:
            return "/".join(parts[: idx + 3])
    return normalized


def _unique_preserve_order(values: list[str]) -> list[str]:
    seen = set()
    result = []
    for value in values:
        if value and value not in seen:
            result.append(value)
            seen.add(value)
    return result


def _df_to_md(df: Any) -> str:
    def _fmt(v: Any) -> str:
        if isinstance(v, float):
            return f"{v:.2f}"
        return str(v)

    cols   = list(df.columns)
    header = "| " + " | ".join(str(c) for c in cols) + " |"
    sep    = "| " + " | ".join("---" for _ in cols) + " |"
    rows   = [
        "| " + " | ".join(_fmt(v) for v in row) + " |"
        for row in df.itertuples(index=False)
    ]
    return "\n".join([header, sep] + rows)


def _norm(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", "", str(value).lower())


def _safe_name(value: str) -> str:
    return re.sub(r"[^A-Za-z0-9_]+", "_", value).strip("_") or "table"


def _rel(path: Path, root: Path) -> str:
    try:
        return str(path.resolve().relative_to(root.resolve())).replace("\\", "/")
    except ValueError:
        return str(path)



@anchored("generate-kpi-sql")
def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Generate SQL for one fully resolved KPI.")
    parser.add_argument("--workspace", required=True, help="Workspace path relative to repo root.")
    parser.add_argument("--repo-root", default=".", help="Repository root. Defaults to current directory.")
    parser.add_argument("--kpi-id", required=True, help="KPI id to generate, for example kpi_001.")
    parser.add_argument("--dialect", choices=["duckdb", "databricks"], default="duckdb")
    parser.add_argument("--catalog", default="workspace", help="Databricks catalog for generated table references.")
    parser.add_argument("--schema", default="autoresearch", help="Databricks schema for generated table references.")
    args = parser.parse_args(argv)
    result = DuckDBKPISQLGenerator(
        args.repo_root,
        args.workspace,
        dialect=args.dialect,
        catalog=args.catalog,
        schema=args.schema,
    ).generate(args.kpi_id)
    print(json.dumps(result.summary(), indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
