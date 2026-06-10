"""Authoritative KPI SQL generation for fully resolved feature mappings."""
from __future__ import annotations

import argparse
import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from core.onboarding.kpi.feature_resolver import READY_STATES
from core.onboarding.relationships.contracts import (
    find_executable_relationship,
    load_relationship_contracts,
)
from core.storage.workspace_layout import WorkspaceLayout


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
        # Load sensitive columns from semantic contract
        contract_path = self.layout.contracts_dir / "semantic_contract.json"
        sensitive_cols = set()
        if contract_path.exists():
            try:
                contract_data = json.loads(contract_path.read_text(encoding="utf-8"))
                columns = contract_data.get("columns", {})
                for col_name, col_meta in columns.items():
                    if col_meta.get("is_sensitive"):
                        sensitive_cols.add(col_name.lower())
            except Exception:
                pass

        for feature in kpi.get("features", []):
            column = self._feature_expression(feature, source_aliases, profile_map)
            if column:
                res_type = feature.get("resolution_type")
                feat_name = feature['feature']
                
                # Apply masking if column is sensitive
                expr = column
                if feat_name.lower() in sensitive_cols or column.split('.')[-1].strip('"').lower() in sensitive_cols:
                    if self.dialect == "duckdb":
                        expr = f"hash({column})" # Simple hash for DuckDB
                    elif self.dialect == "databricks":
                        expr = f"sha2({column}, 256)"

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
                for kpi in mapping.get("kpis", []):
                    refs = [r for f in kpi.get("features", [])
                            for r in _feature_source_refs(f, self.repo_root)]
                    base = _choose_base_source(refs, profile_map)
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
                f"--   uv run warehouse query --workspace {_rel(self.workspace, self.repo_root)} --sql @kpi.sql\n\n"
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
        csv_profiles = [
            (path, profile)
            for path, profile in sorted(profile_map.items())
            if profile.get("format") == "csv"
            and (not required_source_set or path in required_source_set)
        ]
        for idx, (rel_path, _profile) in enumerate(csv_profiles, start=1):
            stem = _safe_name(Path(rel_path).stem)
            view_name = f"catalog_raw_{stem}" if self.dialect == "duckdb" else f"stage_{idx:03d}_{stem}"
            stage_views[rel_path] = view_name
            select_list = self._stage_select_list(rel_path, _profile, required_columns or {})
            if self.dialect == "databricks":
                table_name = self.table_ident(_safe_name(Path(rel_path).stem))
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
        base_source = _choose_base_source(feature_refs, profile_map)
        if not base_source:
            return [], {}

        required_refs = [
            self._choose_feature_ref(feature, base_source, feature_refs, profile_map)
            for feature in kpi.get("features", [])
        ]
        for feature in kpi.get("features", []):
            if feature.get("resolution_type") != "derived_formula":
                continue
            for column in _formula_inputs(feature):
                source = _source_for_column(column, base_source, profile_map)
                if source:
                    required_refs.append({"dataset": source, "column": column})

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
        base_source = _choose_base_source(feature_refs, profile_map)
        if not base_source:
            return "FROM all_workspace_rows", {}
        required_refs = [
            self._choose_feature_ref(feature, base_source, feature_refs, profile_map)
            for feature in kpi.get("features", [])
        ]
        for feature in kpi.get("features", []):
            if feature.get("resolution_type") != "derived_formula":
                continue
            for column in _formula_inputs(feature):
                source = _source_for_column(column, base_source, profile_map)
                if source:
                    required_refs.append({"dataset": source, "column": column})
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
            for column in _formula_inputs(feature):
                source = _source_for_column(column, base_source, profile_map)
                if source:
                    refs.append({"dataset": source, "column": column})
        return refs

    def _choose_feature_ref(
        self,
        feature: dict[str, Any],
        base_source: str,
        all_refs: list[dict[str, str]],
        profile_map: dict[str, dict[str, Any]],
    ) -> dict[str, str] | None:
        refs = _feature_source_refs(feature, self.repo_root)
        feature_name = str(feature.get("feature") or "")
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
            for column in sorted(_formula_inputs(feature), key=len, reverse=True):
                qualified = self._qualified_column(column, source_aliases, profile_map)
                if qualified:
                    formula = re.sub(rf"\b{re.escape(column)}\b", qualified, formula)
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
    for ev in feature.get("evidence") or []:
        detail = str(ev.get("detail") or "")
        if ev.get("type") == "workspace_feature_definition" and "(" in detail:
            return detail
    for column in feature.get("source_columns") or []:
        detail = str(column.get("detail") or "")
        if "(" in detail:
            return detail
    return ""


def _formula_inputs(feature: dict[str, Any]) -> list[str]:
    inputs = [
        str(column.get("column") or "")
        for column in feature.get("source_columns") or []
        if column.get("column")
    ]
    formula = _derived_formula(feature)
    if formula:
        inputs.extend(
            token
            for token in re.findall(r"\b[A-Za-z_][A-Za-z0-9_]*\b", formula)
            if token.lower() not in {"date_diff", "year", "month", "day"}
        )
    return _unique_preserve_order(inputs)


def _choose_base_source(
    refs: list[dict[str, str]],
    profile_map: dict[str, dict[str, Any]],
) -> str:
    # Pick the base/fact source from workspace evidence, not hardcoded table names:
    #   - ref count: how many of the KPI's features map to the source;
    #   - size: the largest referenced table by row count is the fact (high-volume
    #     transaction/event table that dimensions join into) — this replaces the old
    #     domain-noun bonus with a structural, workspace-agnostic signal;
    #   - profile membership: a light tiebreaker.
    ref_counts: dict[str, int] = {}
    for ref in refs:
        source = ref["dataset"]
        ref_counts[source] = ref_counts.get(source, 0) + 1
    if not ref_counts:
        return ""

    def _rows(source: str) -> int:
        return int((profile_map.get(source) or {}).get("row_count") or 0)

    max_rows = max((_rows(source) for source in ref_counts), default=0)
    scores: dict[str, int] = {}
    for source, count in ref_counts.items():
        score = count
        if source in profile_map:
            score += 1
        if max_rows > 0 and _rows(source) == max_rows:
            score += 3  # the largest referenced table is the fact
        scores[source] = score
    return sorted(scores, key=lambda source: (-scores[source], -_rows(source), source))[0]


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
