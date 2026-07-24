"""core.onboarding.kpi.dbt_project_generator: generates a real dbt project
(staging/intermediate/marts) from the same confirmed contracts
sql_generator.py already reads, using dbt's `{{ ref() }}`/`{{ source() }}`
Jinja references instead of quoted-identifier SQL text.
"""
from __future__ import annotations

import json
import re
import tempfile
import unittest
from pathlib import Path

from core.onboarding.data_source_panel import DataSourceAnswerRecorder
from core.onboarding.kpi.dbt_project_generator import DbtProjectGenerator
from core.onboarding.kpi.feature_resolver import KPIFeatureResolver
from core.onboarding.workspace.onboarding import WorkspaceOnboarder
from core.storage.workspace_layout import WorkspaceLayout


class DbtProjectGeneratorLocalCsvTests(unittest.TestCase):
    """A local-CSV-sourced workspace generating a Databricks-target dbt
    project -- the "CSV profiled for schema discovery, data already
    materialized as a same-named UC table" case sql_generator.py's
    table_ident() branch already handles for the CTE path.
    """

    def _create_workspace(self, root: Path) -> Path:
        workspace = root / "workspaces" / "demo"
        (workspace / "datasets").mkdir(parents=True)
        (workspace / "docs").mkdir(parents=True)
        (workspace / "datasets" / "transactions.csv").write_text(
            "ClaimID,PaidAmount,LineOfBusiness\n"
            "C1,10.50,Commercial\n"
            "C2,20.25,Medicare\n",
            encoding="utf-8",
        )
        (workspace / "docs" / "kpi_registry.csv").write_text(
            "Key business question,Description,Cuts / grain hints,Metric,Data model refinement required\n"
            "What is paid amount by line of business?,Baseline KPI,LineOfBusiness,sum(PaidAmount),\n",
            encoding="utf-8",
        )
        return workspace

    def test_generates_staging_intermediate_marts_with_ref_and_source(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self._create_workspace(root)
            WorkspaceOnboarder(root, "workspaces/demo", sample_rows=10).run()
            KPIFeatureResolver(root, "workspaces/demo", domain="healthcare").run()

            result = DbtProjectGenerator(
                root, "workspaces/demo", catalog="main", schema="rcm"
            ).generate()

            self.assertEqual(result.kpi_count, 1)
            self.assertIn("kpi_001", result.generated_kpi_ids)

            dbt_dir = root / result.dbt_project_dir
            staging_files = list((dbt_dir / "models" / "staging").glob("stg_*.sql"))
            self.assertTrue(staging_files, "expected at least one staging model")
            staging_sql = staging_files[0].read_text(encoding="utf-8")
            self.assertIn("{{ source('raw', 'transactions') }}", staging_sql)
            # No leaked quoted-identifier SQL from the CTE-path internals.
            self.assertNotIn("`main`.`rcm`", staging_sql)
            self.assertNotIn("read_csv_auto", staging_sql)

            features_sql = (dbt_dir / "models" / "intermediate" / "int_kpi_001_features.sql").read_text(
                encoding="utf-8"
            )
            self.assertIn("{{ ref('stg_transactions') }}", features_sql)
            self.assertNotIn("`main`.`rcm`", features_sql)
            self.assertIn("PaidAmount", features_sql)

            mart_sql = (dbt_dir / "models" / "marts" / "fct_kpi_001.sql").read_text(encoding="utf-8")
            self.assertIn("{{ ref('int_kpi_001_features') }}", mart_sql)
            self.assertNotIn("`main`.`rcm`", mart_sql)
            self.assertIn("sum_paidamount", mart_sql.lower())

            # Medallion layering: staging+intermediate write to silver,
            # marts write to gold -- never dumped into the bronze/source
            # schema itself. Found live: the first version of this generator
            # wrote every layer into whatever single --schema was passed.
            project_yml = (dbt_dir / "dbt_project.yml").read_text(encoding="utf-8")
            self.assertIn("+schema: silver", project_yml)
            self.assertIn("+schema: gold", project_yml)
            macro_sql = (dbt_dir / "macros" / "get_custom_schema.sql").read_text(encoding="utf-8")
            self.assertIn("generate_schema_name", macro_sql)
            self.assertIn("custom_schema_name | trim", macro_sql)

            # Project scaffolding.
            self.assertTrue((dbt_dir / "dbt_project.yml").exists())
            profiles = (dbt_dir / "profiles.yml").read_text(encoding="utf-8")
            self.assertIn("env_var('DATABRICKS_TOKEN')", profiles)
            self.assertNotIn("DATABRICKS_TOKEN=", profiles)  # never a literal secret value
            sources_yml = (dbt_dir / "models" / "sources.yml").read_text(encoding="utf-8")
            self.assertIn("name: transactions", sources_yml)
            self.assertIn("database: main", sources_yml)
            self.assertIn("schema: rcm", sources_yml)

            # Query Tags (dbt-databricks 1.11+): per-enterprise cost
            # attribution via system.query_history.query_tags. Must be a
            # JSON-encoded STRING (the adapter's own credentials.py types
            # it Optional[str] and parses it via json.loads()) -- a nested
            # YAML mapping fails adapter schema validation.
            match = re.search(r"query_tags: '(.*)'", profiles)
            self.assertIsNotNone(match, "query_tags line not found")
            tags = json.loads(match.group(1))
            self.assertEqual(tags["env"], "prod")
            self.assertIn("project_name", tags)

    def test_query_tags_project_name_is_the_enterprise_not_the_workspace(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self._create_workspace(root)
            WorkspaceOnboarder(root, "workspaces/demo", sample_rows=10).run()
            KPIFeatureResolver(root, "workspaces/demo", domain="healthcare").run()

            result = DbtProjectGenerator(
                root, "workspaces/demo", catalog="main", schema="rcm",
                enterprise_id="acme_corp",
            ).generate()
            profiles = (root / result.dbt_project_dir / "profiles.yml").read_text(encoding="utf-8")
            match = re.search(r"query_tags: '(.*)'", profiles)
            tags = json.loads(match.group(1))
            self.assertEqual(tags["project_name"], "acme_corp")

    def test_no_feature_mapping_at_all_raises_clear_error(self):
        # Feature resolution never ran for this workspace -- no
        # kpi_feature_mapping.json exists at all yet.
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            workspace = root / "workspaces" / "demo"
            workspace.mkdir(parents=True)
            WorkspaceLayout(project_root=workspace).ensure_runtime_dirs()
            with self.assertRaises(FileNotFoundError):
                DbtProjectGenerator(root, "workspaces/demo", catalog="main", schema="rcm").generate()

    def test_mapping_present_but_no_kpi_ready_raises_value_error(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            workspace = root / "workspaces" / "demo"
            layout = WorkspaceLayout(project_root=workspace)
            layout.ensure_runtime_dirs()
            (layout.contracts_dir / "kpi_feature_mapping.json").write_text(
                '{"kpis": [{"kpi_id": "kpi_001", "features": '
                '[{"feature": "x", "state": "blocked_questions_pending"}]}]}',
                encoding="utf-8",
            )
            with self.assertRaises(ValueError):
                DbtProjectGenerator(root, "workspaces/demo", catalog="main", schema="rcm").generate()


class DbtProjectGeneratorUcSourcedTests(unittest.TestCase):
    """A UC-sourced (format='delta') profile's staging model must reference
    {{ source() }} using the real table name, and never leak the source's
    raw fqn or the generator's own catalog/schema into the model text --
    the same correctness bar as the CTE path's Phase A fix.
    """

    def test_delta_profile_staging_uses_source_not_raw_fqn(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            workspace = root / "workspaces" / "demo"
            workspace.mkdir(parents=True)
            layout = WorkspaceLayout(project_root=workspace)
            layout.ensure_runtime_dirs()

            gen = DbtProjectGenerator(root, "workspaces/demo", catalog="main", schema="rcm")
            profile_map = {
                "`healthcare_rcm`.`bronze`.`cptcodes`": {
                    "format": "delta",
                    "schema": {"Code": "string", "Description": "string"},
                }
            }
            gen._ensure_staging_model(
                "`healthcare_rcm`.`bronze`.`cptcodes`",
                profile_map,
                {},
                layout.contracts_dir.parent,  # any writable dir; content is what's asserted
            )
            model_name = gen._written_staging["`healthcare_rcm`.`bronze`.`cptcodes`"]
            self.assertEqual(model_name, "stg_cptcodes")
            written = (layout.contracts_dir.parent / "stg_cptcodes.sql").read_text(encoding="utf-8")
            self.assertIn("{{ source('raw', 'cptcodes') }}", written)
            self.assertNotIn("healthcare_rcm", written)


if __name__ == "__main__":
    unittest.main()
