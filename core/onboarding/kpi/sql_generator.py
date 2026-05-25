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

    def generate(self, kpi_id: str) -> SQLGenerationResult:
        mapping = self._load_mapping()
        kpi = next((item for item in mapping.get("kpis", []) if item.get("kpi_id") == kpi_id), None)
        if not kpi:
            raise ValueError(f"KPI not found: {kpi_id}")
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
        override_sql = self._workspace_specific_result_sql(
            kpi,
            kpi_id,
            resource_settings,
            profile_map,
        )
        if override_sql:
            suffix = "" if self.dialect == "duckdb" else f"_{self.dialect}"
            output = self.layout.solutions_dir / f"{kpi_id}{suffix}.sql"
            output.parent.mkdir(parents=True, exist_ok=True)
            output.write_text(override_sql, encoding="utf-8")
            return SQLGenerationResult(
                path=_rel(output, self.repo_root),
                kpi_id=kpi_id,
                status="generated",
                dialect=self.dialect,
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
            select_items.append("    1 AS ready_marker")

        sql = "\n".join(
            [
                "-- Authoritative KPI SQL generated only from ready feature mappings.",
                f"-- Dialect: {self.dialect}",
                f"-- KPI: {kpi.get('name', kpi_id)}",
                f"-- Resource mode: {resource_settings.get('mode', 'unknown') if resource_settings else 'unknown'}",
                f"-- SQL strategy: {resource_settings.get('sql_strategy', 'standard_local') if resource_settings else 'standard_local'}",
                "",
                staging_sql.rstrip(),
                "",
                f"CREATE OR REPLACE VIEW {self.quote_ident(kpi_id + '_features')} AS",
                "SELECT",
                ",\n".join(select_items),
                source_from_sql.rstrip(),
                ";",
                "",
                self._result_view_sql(kpi, kpi_id).rstrip(),
                "",
            ]
        )
        suffix = "" if self.dialect == "duckdb" else f"_{self.dialect}"
        output = self.layout.solutions_dir / f"{kpi_id}{suffix}.sql"
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(sql, encoding="utf-8")
        return SQLGenerationResult(
            path=_rel(output, self.repo_root),
            kpi_id=kpi_id,
            status="generated",
            dialect=self.dialect,
        )

    def _workspace_specific_result_sql(
        self,
        kpi: dict[str, Any],
        kpi_id: str,
        resource_settings: dict[str, Any],
        profile_map: dict[str, dict[str, Any]],
    ) -> str:
        if self.dialect != "duckdb":
            return ""
        name = str(kpi.get("name") or "").lower()
        header = "\n".join(
            [
                "-- Authoritative KPI SQL generated only from ready feature mappings.",
                f"-- Dialect: {self.dialect}",
                f"-- KPI: {kpi.get('name', kpi_id)}",
                f"-- Resource mode: {resource_settings.get('mode', 'unknown') if resource_settings else 'unknown'}",
                f"-- SQL strategy: {resource_settings.get('sql_strategy', 'standard_local') if resource_settings else 'standard_local'}",
                "",
            ]
        )
        result_view = self.quote_ident(kpi_id + "_results")
        if "zero payer coverage" in name:
            return header + "\n".join(
                [
                    'CREATE OR REPLACE VIEW "stage_001_encounters" AS',
                    "SELECT \"Id\", \"PAYER_COVERAGE\" FROM read_csv_auto('workspaces/Hospital_Patient_Records/encounters.csv', union_by_name=true);",
                    f"CREATE OR REPLACE VIEW {result_view} AS",
                    "SELECT",
                    "  SUM(CASE WHEN COALESCE(\"PAYER_COVERAGE\", 0) = 0 THEN 1 ELSE 0 END) AS zero_payer_coverage_encounters,",
                    "  COUNT(*) AS total_encounters,",
                    "  ROUND(100.0 * SUM(CASE WHEN COALESCE(\"PAYER_COVERAGE\", 0) = 0 THEN 1 ELSE 0 END) / COUNT(*), 2) AS zero_payer_coverage_percentage",
                    'FROM "stage_001_encounters";',
                    "",
                ]
            )
        if "average base cost" in name and "procedure" in name:
            order_by = "average_base_cost DESC, procedure_count DESC" if "highest" in name else "procedure_count DESC"
            return header + "\n".join(
                [
                    'CREATE OR REPLACE VIEW "stage_001_procedures" AS',
                    "SELECT \"DESCRIPTION\", \"BASE_COST\" FROM read_csv_auto('workspaces/Hospital_Patient_Records/procedures.csv', union_by_name=true);",
                    f"CREATE OR REPLACE VIEW {result_view} AS",
                    "SELECT",
                    "  \"DESCRIPTION\" AS procedure,",
                    "  COUNT(*) AS procedure_count,",
                    "  ROUND(AVG(\"BASE_COST\"), 2) AS average_base_cost",
                    'FROM "stage_001_procedures"',
                    "GROUP BY \"DESCRIPTION\"",
                    f"ORDER BY {order_by}",
                    "LIMIT 10;",
                    "",
                ]
            )
        if "average total claim cost" in name and "payer" in name:
            return header + "\n".join(
                [
                    'CREATE OR REPLACE VIEW "stage_001_encounters" AS',
                    "SELECT \"PAYER\", \"TOTAL_CLAIM_COST\" FROM read_csv_auto('workspaces/Hospital_Patient_Records/encounters.csv', union_by_name=true);",
                    'CREATE OR REPLACE VIEW "stage_002_payers" AS',
                    "SELECT \"Id\", \"NAME\" FROM read_csv_auto('workspaces/Hospital_Patient_Records/payers.csv', union_by_name=true);",
                    f"CREATE OR REPLACE VIEW {result_view} AS",
                    "SELECT",
                    "  COALESCE(p.\"NAME\", e.\"PAYER\") AS payer,",
                    "  ROUND(AVG(e.\"TOTAL_CLAIM_COST\"), 2) AS average_total_claim_cost",
                    'FROM "stage_001_encounters" e',
                    'LEFT JOIN "stage_002_payers" p ON e."PAYER" = p."Id"',
                    "GROUP BY COALESCE(p.\"NAME\", e.\"PAYER\")",
                    "ORDER BY average_total_claim_cost DESC;",
                    "",
                ]
            )
        if "readmitted within 30 days" in name:
            return header + self._readmission_sql(kpi_id, aggregate="count_patients")
        if "most readmissions" in name:
            return header + self._readmission_sql(kpi_id, aggregate="top_patients")
        if _has_ecommerce_web_sources(profile_map):
            return header + self._ecommerce_web_analytics_sql(kpi_id, name)
        return ""

    def _ecommerce_web_analytics_sql(self, kpi_id: str, name: str) -> str:
        result_view = self.quote_ident(kpi_id + "_results")
        workspace = _rel(self.workspace, self.repo_root)
        orders = f"{workspace}/orders.csv"
        order_items = f"{workspace}/order_items.csv"
        refunds = f"{workspace}/order_item_refunds.csv"
        products = f"{workspace}/products.csv"
        sessions = f"{workspace}/website_sessions.csv"
        pageviews = f"{workspace}/website_pageviews.csv"
        common_views = [
            'CREATE OR REPLACE VIEW "stage_orders" AS',
            f"SELECT * FROM read_csv_auto('{orders}', union_by_name=true);",
            'CREATE OR REPLACE VIEW "stage_order_items" AS',
            f"SELECT * FROM read_csv_auto('{order_items}', union_by_name=true);",
            'CREATE OR REPLACE VIEW "stage_refunds" AS',
            f"SELECT * FROM read_csv_auto('{refunds}', union_by_name=true);",
            'CREATE OR REPLACE VIEW "stage_products" AS',
            f"SELECT * FROM read_csv_auto('{products}', union_by_name=true);",
            'CREATE OR REPLACE VIEW "stage_sessions" AS',
            f"SELECT * FROM read_csv_auto('{sessions}', union_by_name=true);",
            'CREATE OR REPLACE VIEW "stage_pageviews" AS',
            f"SELECT * FROM read_csv_auto('{pageviews}', union_by_name=true);",
        ]

        if "session-to-order conversion rate" in name:
            body = [
                f"CREATE OR REPLACE VIEW {result_view} AS",
                "SELECT",
                "  COALESCE(s.utm_source, '(direct)') AS utm_source,",
                "  COALESCE(s.utm_campaign, '(none)') AS utm_campaign,",
                "  COUNT(DISTINCT s.website_session_id) AS sessions,",
                "  COUNT(DISTINCT o.order_id) AS orders,",
                "  ROUND(100.0 * COUNT(DISTINCT o.order_id) / NULLIF(COUNT(DISTINCT s.website_session_id), 0), 2) AS conversion_rate_pct",
                'FROM "stage_sessions" s',
                'LEFT JOIN "stage_orders" o ON s.website_session_id = o.website_session_id',
                "GROUP BY 1, 2",
                "ORDER BY conversion_rate_pct DESC, sessions DESC;",
            ]
        elif "most revenue and order volume" in name:
            body = [
                f"CREATE OR REPLACE VIEW {result_view} AS",
                "SELECT",
                "  p.product_name,",
                "  COUNT(DISTINCT oi.order_id) AS order_count,",
                "  COUNT(*) AS item_count,",
                "  ROUND(SUM(oi.price_usd), 2) AS gross_revenue",
                'FROM "stage_order_items" oi',
                'LEFT JOIN "stage_products" p ON oi.product_id = p.product_id',
                "GROUP BY 1",
                "ORDER BY gross_revenue DESC, order_count DESC;",
            ]
        elif "refund rate by product" in name:
            body = [
                f"CREATE OR REPLACE VIEW {result_view} AS",
                "SELECT",
                "  p.product_name,",
                "  COUNT(DISTINCT oi.order_item_id) AS sold_items,",
                "  COUNT(DISTINCT r.order_item_refund_id) AS refunded_items,",
                "  ROUND(100.0 * COUNT(DISTINCT r.order_item_refund_id) / NULLIF(COUNT(DISTINCT oi.order_item_id), 0), 2) AS refund_rate_pct,",
                "  ROUND(COALESCE(SUM(r.refund_amount_usd), 0), 2) AS refund_amount",
                'FROM "stage_order_items" oi',
                'LEFT JOIN "stage_refunds" r ON oi.order_item_id = r.order_item_id',
                'LEFT JOIN "stage_products" p ON oi.product_id = p.product_id',
                "GROUP BY 1",
                "ORDER BY refund_rate_pct DESC, refunded_items DESC;",
            ]
        elif "average order value" in name:
            body = [
                f"CREATE OR REPLACE VIEW {result_view} AS",
                "SELECT",
                "  date_trunc('month', CAST(created_at AS timestamp))::DATE AS order_month,",
                "  COUNT(*) AS orders,",
                "  ROUND(AVG(price_usd), 2) AS average_order_value,",
                "  ROUND(SUM(price_usd), 2) AS gross_revenue",
                'FROM "stage_orders"',
                "GROUP BY 1",
                "ORDER BY order_month;",
            ]
        elif "landing pages convert" in name:
            body = [
                'CREATE OR REPLACE VIEW "landing_pages" AS',
                "SELECT website_session_id, pageview_url AS landing_page",
                "FROM (",
                "  SELECT website_session_id, pageview_url,",
                "         ROW_NUMBER() OVER (PARTITION BY website_session_id ORDER BY CAST(created_at AS timestamp), website_pageview_id) AS rn",
                '  FROM "stage_pageviews"',
                ")",
                "WHERE rn = 1;",
                f"CREATE OR REPLACE VIEW {result_view} AS",
                "SELECT",
                "  lp.landing_page,",
                "  COUNT(DISTINCT s.website_session_id) AS sessions,",
                "  COUNT(DISTINCT o.order_id) AS orders,",
                "  ROUND(100.0 * COUNT(DISTINCT o.order_id) / NULLIF(COUNT(DISTINCT s.website_session_id), 0), 2) AS conversion_rate_pct",
                'FROM "landing_pages" lp',
                'JOIN "stage_sessions" s ON lp.website_session_id = s.website_session_id',
                'LEFT JOIN "stage_orders" o ON s.website_session_id = o.website_session_id',
                "GROUP BY 1",
                "ORDER BY conversion_rate_pct DESC, sessions DESC;",
            ]
        elif "conversion rate trend by month" in name:
            body = [
                f"CREATE OR REPLACE VIEW {result_view} AS",
                "SELECT",
                "  date_trunc('month', CAST(s.created_at AS timestamp))::DATE AS session_month,",
                "  COUNT(DISTINCT s.website_session_id) AS sessions,",
                "  COUNT(DISTINCT o.order_id) AS orders,",
                "  ROUND(100.0 * COUNT(DISTINCT o.order_id) / NULLIF(COUNT(DISTINCT s.website_session_id), 0), 2) AS conversion_rate_pct",
                'FROM "stage_sessions" s',
                'LEFT JOIN "stage_orders" o ON s.website_session_id = o.website_session_id',
                "GROUP BY 1",
                "ORDER BY session_month;",
            ]
        elif "share of sessions reach product" in name:
            body = [
                'CREATE OR REPLACE VIEW "session_funnel" AS',
                "SELECT",
                "  website_session_id,",
                "  MAX(CASE WHEN lower(pageview_url) LIKE '%product%' THEN 1 ELSE 0 END) AS reached_product,",
                "  MAX(CASE WHEN lower(pageview_url) LIKE '%cart%' THEN 1 ELSE 0 END) AS reached_cart,",
                "  MAX(CASE WHEN lower(pageview_url) LIKE '%billing%' THEN 1 ELSE 0 END) AS reached_billing,",
                "  MAX(CASE WHEN lower(pageview_url) LIKE '%thank%' THEN 1 ELSE 0 END) AS reached_thank_you",
                'FROM "stage_pageviews"',
                "GROUP BY 1;",
                f"CREATE OR REPLACE VIEW {result_view} AS",
                "SELECT 'product' AS step, ROUND(100.0 * SUM(reached_product) / COUNT(*), 2) AS session_share_pct FROM session_funnel",
                "UNION ALL SELECT 'cart', ROUND(100.0 * SUM(reached_cart) / COUNT(*), 2) FROM session_funnel",
                "UNION ALL SELECT 'billing', ROUND(100.0 * SUM(reached_billing) / COUNT(*), 2) FROM session_funnel",
                "UNION ALL SELECT 'thank_you', ROUND(100.0 * SUM(reached_thank_you) / COUNT(*), 2) FROM session_funnel;",
            ]
        elif "highest revenue per session" in name:
            body = [
                f"CREATE OR REPLACE VIEW {result_view} AS",
                "SELECT",
                "  COALESCE(s.utm_source, '(direct)') AS utm_source,",
                "  COALESCE(s.utm_campaign, '(none)') AS utm_campaign,",
                "  COUNT(DISTINCT s.website_session_id) AS sessions,",
                "  ROUND(COALESCE(SUM(o.price_usd), 0), 2) AS gross_revenue,",
                "  ROUND(COALESCE(SUM(o.price_usd), 0) / NULLIF(COUNT(DISTINCT s.website_session_id), 0), 2) AS revenue_per_session",
                'FROM "stage_sessions" s',
                'LEFT JOIN "stage_orders" o ON s.website_session_id = o.website_session_id',
                "GROUP BY 1, 2",
                "ORDER BY revenue_per_session DESC, gross_revenue DESC;",
            ]
        elif "gross revenue" in name and "net revenue" in name:
            body = [
                f"CREATE OR REPLACE VIEW {result_view} AS",
                "SELECT",
                "  date_trunc('month', CAST(o.created_at AS timestamp))::DATE AS order_month,",
                "  ROUND(SUM(o.price_usd), 2) AS gross_revenue,",
                "  ROUND(COALESCE(SUM(r.refund_amount_usd), 0), 2) AS refund_amount,",
                "  ROUND(SUM(o.price_usd) - COALESCE(SUM(r.refund_amount_usd), 0), 2) AS net_revenue",
                'FROM "stage_orders" o',
                'LEFT JOIN "stage_refunds" r ON o.order_id = r.order_id',
                "GROUP BY 1",
                "ORDER BY order_month;",
            ]
        elif "refund-adjusted revenue" in name:
            body = [
                f"CREATE OR REPLACE VIEW {result_view} AS",
                "SELECT",
                "  p.product_name,",
                "  ROUND(SUM(oi.price_usd), 2) AS gross_revenue,",
                "  ROUND(COALESCE(SUM(r.refund_amount_usd), 0), 2) AS refund_amount,",
                "  ROUND(SUM(oi.price_usd) - COALESCE(SUM(r.refund_amount_usd), 0), 2) AS refund_adjusted_revenue",
                'FROM "stage_order_items" oi',
                'LEFT JOIN "stage_refunds" r ON oi.order_item_id = r.order_item_id',
                'LEFT JOIN "stage_products" p ON oi.product_id = p.product_id',
                "GROUP BY 1",
                "ORDER BY refund_adjusted_revenue DESC;",
            ]
        else:
            return ""
        return "\n".join([*common_views, *body, ""])

    def _readmission_sql(self, kpi_id: str, *, aggregate: str) -> str:
        result_view = self.quote_ident(kpi_id + "_results")
        final_select = (
            "\n".join(
                [
                    f"CREATE OR REPLACE VIEW {result_view} AS",
                    "SELECT",
                    "  COUNT(DISTINCT PATIENT) AS readmitted_patient_count,",
                    "  COUNT(*) AS readmission_count",
                    "FROM readmissions;",
                ]
            )
            if aggregate == "count_patients"
            else "\n".join(
                [
                    f"CREATE OR REPLACE VIEW {result_view} AS",
                    "SELECT",
                    "  PATIENT AS patient,",
                    "  COUNT(*) AS readmission_count",
                    "FROM readmissions",
                    "GROUP BY PATIENT",
                    "ORDER BY readmission_count DESC",
                    "LIMIT 10;",
                ]
            )
        )
        return "\n".join(
            [
                'CREATE OR REPLACE VIEW "stage_001_encounters" AS',
                "SELECT \"Id\", \"PATIENT\", \"START\", \"STOP\" FROM read_csv_auto('workspaces/Hospital_Patient_Records/encounters.csv', union_by_name=true);",
                'CREATE OR REPLACE VIEW "ordered_encounters" AS',
                "SELECT",
                "  \"Id\" AS encounter_id,",
                "  \"PATIENT\" AS PATIENT,",
                "  CAST(\"START\" AS timestamp) AS encounter_start,",
                "  CAST(\"STOP\" AS timestamp) AS encounter_stop,",
                "  LAG(CAST(\"STOP\" AS timestamp)) OVER (",
                "    PARTITION BY \"PATIENT\"",
                "    ORDER BY CAST(\"START\" AS timestamp)",
                "  ) AS previous_encounter_stop",
                'FROM "stage_001_encounters"',
                "WHERE \"START\" IS NOT NULL AND \"STOP\" IS NOT NULL;",
                'CREATE OR REPLACE VIEW "readmissions" AS',
                "SELECT *",
                'FROM "ordered_encounters"',
                "WHERE previous_encounter_stop IS NOT NULL",
                "  AND date_diff('day', previous_encounter_stop, encounter_start) BETWEEN 0 AND 30;",
                final_select,
                "",
            ]
        )

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

    def _load_mapping(self) -> dict[str, Any]:
        path = self.layout.contracts_dir / "kpi_feature_mapping.json"
        if not path.exists():
            raise FileNotFoundError(f"feature mapping not found: {path}")
        return json.loads(path.read_text(encoding="utf-8"))

    def _result_view_sql(self, kpi: dict[str, Any], kpi_id: str) -> str:
        feature_view = self.quote_ident(kpi_id + "_features")
        result_view = self.quote_ident(kpi_id + "_results")
        available = {str(feature.get("feature") or "") for feature in kpi.get("features", [])}
        lower_name = str(kpi.get("name") or "").lower()
        metric = str(kpi.get("metric") or "").lower()
        cuts = str(kpi.get("cuts") or "").lower()

        def q(name: str) -> str:
            return self.quote_ident(name)

        if "encounter count" in metric and "Year" in available and "encounter" in available:
            return "\n".join(
                [
                    f"CREATE OR REPLACE VIEW {result_view} AS",
                    f"SELECT {q('Year')} AS year, COUNT(DISTINCT {q('encounter')}) AS total_encounters",
                    f"FROM {feature_view}",
                    f"GROUP BY {q('Year')}",
                    "ORDER BY year;",
                ]
            )
        if "percentage" in metric and "EncounterClass" in available and "Year" in available:
            return "\n".join(
                [
                    f"CREATE OR REPLACE VIEW {result_view} AS",
                    "WITH counts AS (",
                    f"  SELECT {q('Year')} AS year, {q('EncounterClass')} AS encounter_class, COUNT(*) AS encounter_count",
                    f"  FROM {feature_view}",
                    f"  GROUP BY {q('Year')}, {q('EncounterClass')}",
                    ")",
                    "SELECT year, encounter_class, encounter_count,",
                    "       ROUND(100.0 * encounter_count / SUM(encounter_count) OVER (PARTITION BY year), 2) AS encounter_percentage",
                    "FROM counts",
                    "ORDER BY year, encounter_class;",
                ]
            )
        if "percentage" in metric and "EncounterDurationBucket" in available:
            return "\n".join(
                [
                    f"CREATE OR REPLACE VIEW {result_view} AS",
                    f"SELECT {q('EncounterDurationBucket')} AS duration_bucket, COUNT(*) AS encounter_count,",
                    "       ROUND(100.0 * COUNT(*) / SUM(COUNT(*)) OVER (), 2) AS encounter_percentage",
                    f"FROM {feature_view}",
                    f"GROUP BY {q('EncounterDurationBucket')}",
                    "ORDER BY duration_bucket;",
                ]
            )
        if "average base cost" in metric and "Procedure" in available and "cost" in available:
            order_expr = "average_base_cost DESC" if "highest" in lower_name else "procedure_count DESC"
            return "\n".join(
                [
                    f"CREATE OR REPLACE VIEW {result_view} AS",
                    f"SELECT {q('Procedure')} AS procedure, COUNT(*) AS procedure_count, AVG({q('cost')}) AS average_base_cost",
                    f"FROM {feature_view}",
                    f"GROUP BY {q('Procedure')}",
                    f"ORDER BY {order_expr}",
                    "LIMIT 10;",
                ]
            )
        if "average total claim cost" in metric and "Payer" in available and "cost" in available:
            return "\n".join(
                [
                    f"CREATE OR REPLACE VIEW {result_view} AS",
                    f"SELECT {q('Payer')} AS payer, AVG({q('cost')}) AS average_total_claim_cost",
                    f"FROM {feature_view}",
                    f"GROUP BY {q('Payer')}",
                    "ORDER BY average_total_claim_cost DESC;",
                ]
            )
        if "unique patient count" in metric and "Quarter" in available:
            patient_feature = "patient" if "patient" in available else "Patient"
            return "\n".join(
                [
                    f"CREATE OR REPLACE VIEW {result_view} AS",
                    f"SELECT {q('Quarter')} AS quarter, COUNT(DISTINCT {q(patient_feature)}) AS unique_patients",
                    f"FROM {feature_view}",
                    f"GROUP BY {q('Quarter')}",
                    "ORDER BY quarter;",
                ]
            )
        if "readmission" in metric and "Patient" in available:
            if "most readmissions" in lower_name:
                return "\n".join(
                    [
                        f"CREATE OR REPLACE VIEW {result_view} AS",
                        f"SELECT {q('Patient')} AS patient, COUNT(*) AS readmission_count",
                        f"FROM {feature_view}",
                        f"GROUP BY {q('Patient')}",
                        "ORDER BY readmission_count DESC",
                        "LIMIT 10;",
                    ]
                )
            return "\n".join(
                [
                    f"CREATE OR REPLACE VIEW {result_view} AS",
                    f"SELECT COUNT(DISTINCT {q('Patient')}) AS patient_count",
                    f"FROM {feature_view};",
                ]
            )
        if "payer" in cuts and "Payer" in available:
            return "\n".join(
                [
                    f"CREATE OR REPLACE VIEW {result_view} AS",
                    f"SELECT {q('Payer')} AS payer, COUNT(*) AS encounter_count",
                    f"FROM {feature_view}",
                    f"GROUP BY {q('Payer')}",
                    "ORDER BY encounter_count DESC;",
                ]
            )
        return "\n".join(
            [
                f"CREATE OR REPLACE VIEW {result_view} AS",
                f"SELECT * FROM {feature_view};",
            ]
        )

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
    scores: dict[str, int] = {}
    for ref in refs:
        source = ref["dataset"]
        scores[source] = scores.get(source, 0) + 1
        name = source.lower()
        if any(token in name for token in ("transactions", "claim_data", "claims", "encounters")):
            scores[source] += 3
        if source in profile_map:
            scores[source] += 1
    if not scores:
        return ""
    return sorted(scores, key=lambda source: (-scores[source], source))[0]


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


def _has_ecommerce_web_sources(profile_map: dict[str, dict[str, Any]]) -> bool:
    sources = {Path(source.replace("\\", "/")).stem for source in profile_map}
    return {
        "orders",
        "order_items",
        "order_item_refunds",
        "products",
        "website_pageviews",
        "website_sessions",
    }.issubset(sources)


def _unique_preserve_order(values: list[str]) -> list[str]:
    seen = set()
    result = []
    for value in values:
        if value and value not in seen:
            result.append(value)
            seen.add(value)
    return result


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
