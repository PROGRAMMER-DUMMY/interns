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
from unittest.mock import MagicMock, patch

import yaml

from core.observability.cost_ledger import RUN_ID_ENV

from core.onboarding.kpi.dbt_project_generator import DbtProjectGenerator
from core.onboarding.kpi.feature_resolver import KPIFeatureResolver
from core.onboarding.workspace.onboarding import WorkspaceOnboarder
from core.storage.workspace_layout import WorkspaceLayout


def _create_kpi001_workspace(root: Path) -> Path:
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
    WorkspaceOnboarder(root, "workspaces/demo", sample_rows=10).run()
    KPIFeatureResolver(root, "workspaces/demo", domain="healthcare").run()
    return workspace



def _query_tags_raw(profiles_yml: str) -> str:
    """The `query_tags` scalar as dbt receives it, YAML-decoded but pre-Jinja.

    Parsed with a real YAML loader rather than a regex: the value now contains
    both JSON quoting and a Jinja call, and a regex over that is how the
    escaping silently breaks.
    """
    profile = next(iter(yaml.safe_load(profiles_yml).values()))
    return profile["outputs"]["prod"]["query_tags"]


def _query_tags(profiles_yml: str, run_id: str = "") -> dict:
    """The tag dict dbt-databricks ends up json.loads()-ing, with env_var()
    resolved the way dbt would resolve it at invocation time."""
    raw = _query_tags_raw(profiles_yml)
    rendered = re.sub(
        r"\{\{\s*env_var\(\s*'" + re.escape(RUN_ID_ENV) + r"'\s*,\s*''\s*\)\s*\}\}",
        run_id,
        raw,
    )
    return json.loads(rendered)


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
            tags = _query_tags(profiles)
            self.assertEqual(tags["env"], "prod")
            self.assertIn("project_name", tags)
            # run_id is what makes the tag joinable back to a cost-ledger row.
            # It must stay an env_var() -- profiles.yml is generated once and
            # reused, so a literal would stamp the first run's id on every run.
            self.assertEqual(tags["run_id"], "")
            self.assertIn(RUN_ID_ENV, _query_tags_raw(profiles))

            # Dashboard registered as a formal dbt exposure (dbt+Airflow
            # plan section D4): dbt docs/lineage treats it as a real
            # downstream consumer of the marts layer, not an invisible one.
            exposures_yml = (dbt_dir / "models" / "exposures.yml").read_text(encoding="utf-8")
            self.assertIn("type: dashboard", exposures_yml)
            self.assertIn("ref('fct_kpi_001')", exposures_yml)
            self.assertIn("owner:", exposures_yml)
            self.assertIn("email:", exposures_yml)

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
            self.assertEqual(_query_tags(profiles)["project_name"], "acme_corp")

    def test_query_tags_run_id_resolves_from_the_invocation_environment(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self._create_workspace(root)
            WorkspaceOnboarder(root, "workspaces/demo", sample_rows=10).run()
            KPIFeatureResolver(root, "workspaces/demo", domain="healthcare").run()
            result = DbtProjectGenerator(
                root, "workspaces/demo", catalog="main", schema="rcm",
            ).generate()
            profiles = (root / result.dbt_project_dir / "profiles.yml").read_text(encoding="utf-8")
            self.assertEqual(_query_tags(profiles, run_id="sess-abc123")["run_id"], "sess-abc123")

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


class SchemaExclusionTests(unittest.TestCase):
    """S2's schema-drift decision (`schema_exclusions.json`) quarantines a
    column: silver stops selecting it, and the emitted model names the
    decision that dropped it. Absent contract = unchanged behaviour."""

    def test_excluded_column_is_dropped_from_the_staging_select(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            workspace = _create_kpi001_workspace(root)
            layout = WorkspaceLayout(project_root=workspace)
            (layout.contracts_dir / "schema_exclusions.json").write_text(
                json.dumps(
                    {
                        "transactions": {
                            "excluded_columns": ["LineOfBusiness"],
                            "decided_by": "reviewer",
                            "at": "2026-08-05T00:00:00Z",
                        }
                    }
                ),
                encoding="utf-8",
            )
            result = DbtProjectGenerator(
                root, "workspaces/demo", catalog="main", schema="rcm"
            ).generate()
            staging = (
                root / result.dbt_project_dir / "models" / "staging" / "stg_transactions.sql"
            ).read_text(encoding="utf-8")
            self.assertNotIn("`LineOfBusiness`", staging)
            self.assertIn("schema_exclusions.json", staging)
            self.assertIn("LineOfBusiness", staging)  # named in the drop comment

    def test_absent_exclusions_contract_changes_nothing(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            _create_kpi001_workspace(root)
            result = DbtProjectGenerator(
                root, "workspaces/demo", catalog="main", schema="rcm"
            ).generate()
            staging = (
                root / result.dbt_project_dir / "models" / "staging" / "stg_transactions.sql"
            ).read_text(encoding="utf-8")
            self.assertIn("`LineOfBusiness`", staging)
            self.assertNotIn("schema_exclusions", staging)


class LateArrivingDimensionTests(unittest.TestCase):
    """Audit missing #1: an early-arriving fact whose dimension row has not
    landed yet must not lose its measure. The join is already LEFT; the
    missing halves were the inferred-member macro and the COALESCE."""

    def _generate(self, root: Path, *, late_dims: bool):
        _create_kpi001_workspace(root)
        if late_dims:
            layout = WorkspaceLayout(project_root=root / "workspaces" / "demo")
            (layout.contracts_dir / "blueprint_decisions.json").write_text(
                json.dumps(
                    {
                        "decisions": [
                            {
                                "decision": "modeling",
                                "choice": "inferred_member_dimensions",
                                "rule_id": "R10",
                                "status": "decided",
                            }
                        ]
                    }
                ),
                encoding="utf-8",
            )
        return DbtProjectGenerator(root, "workspaces/demo", catalog="main", schema="rcm")

    def test_macro_emitted_only_when_the_modeling_decision_fires(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            result = self._generate(root, late_dims=False).generate()
            self.assertFalse(
                (root / result.dbt_project_dir / "macros" / "inferred_member.sql").exists()
            )

    def test_inferred_member_macro_inserts_an_unknown_member_row(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            result = self._generate(root, late_dims=True).generate()
            macro = (
                root / result.dbt_project_dir / "macros" / "inferred_member.sql"
            ).read_text(encoding="utf-8")
            self.assertIn("macro inferred_member(", macro)
            self.assertIn("when not matched then insert", macro)
            self.assertIn("{{ surrogate_key(", macro)  # deterministic hash key
            self.assertIn("is_inferred", macro)
            self.assertIn("true", macro)
            # Type-1 overwrite when the real dimension row finally arrives.
            self.assertIn("macro resolve_inferred_member(", macro)
            self.assertIn("is_inferred = false", macro)

    def test_dimension_side_feature_is_coalesced_to_the_unknown_member(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            gen = self._generate(root, late_dims=True)
            profile_map = {
                "facts.csv": {"format": "csv", "schema": {"PaidAmount": "Float64"}},
                "dim_payer.csv": {"format": "csv", "schema": {"PayerName": "String"}},
            }
            kpi = {
                "kpi_id": "kpi_x",
                "metric": "sum(PaidAmount)",
                "features": [
                    {
                        "feature": "PaidAmount",
                        "state": "proven_direct",
                        "source_columns": [{"dataset": "facts.csv", "column": "PaidAmount"}],
                    },
                    {
                        "feature": "PayerName",
                        "state": "proven_direct",
                        "source_columns": [{"dataset": "dim_payer.csv", "column": "PayerName"}],
                    },
                ],
            }
            items = gen._feature_select_items(
                kpi, {"facts.csv": "s0", "dim_payer.csv": "s1"}, profile_map
            )
            joined = "\n".join(items)
            self.assertIn("coalesce", joined)
            self.assertIn("__unknown_member__", joined)
            # The base (fact) side is never coalesced -- it is the grain.
            self.assertNotIn("coalesce(cast(s0.", joined)


class ContractEnforcementTests(unittest.TestCase):
    """contract: enforced: true (dbt+Airflow plan section D5) -- column
    data_types come from the REAL already-built table (DESCRIBE TABLE), not
    a guess, so this suite mocks DatabricksClient rather than needing a live
    warehouse (same split every other real-infra-dependent piece in this
    generator already uses)."""

    def test_enforce_contracts_false_by_default_writes_no_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            _create_kpi001_workspace(root)
            result = DbtProjectGenerator(
                root, "workspaces/demo", catalog="main", schema="rcm",
            ).generate()
            self.assertFalse(
                (root / result.dbt_project_dir / "models" / "marts" / "_contracts.yml").exists()
            )

    def test_enforce_contracts_true_reads_real_table_schema(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            _create_kpi001_workspace(root)

            mock_cfg = MagicMock(is_active=lambda: True)
            mock_client = MagicMock()
            mock_client.execute_query.return_value = (
                ["col_name", "data_type", "comment"],
                [["lineofbusiness", "string", None], ["sum_paidamount", "double", None]],
            )
            with patch("core.config.resolve_databricks_config", return_value=mock_cfg), patch(
                "core.execution.databricks_client.DatabricksClient", return_value=mock_client
            ):
                result = DbtProjectGenerator(
                    root, "workspaces/demo", catalog="main", schema="rcm",
                    enforce_contracts=True,
                ).generate()

            contracts_path = root / result.dbt_project_dir / "models" / "marts" / "_contracts.yml"
            self.assertTrue(contracts_path.exists())
            text = contracts_path.read_text(encoding="utf-8")
            self.assertIn("name: fct_kpi_001", text)
            self.assertIn("enforced: true", text)
            self.assertIn("name: lineofbusiness", text)
            self.assertIn("data_type: string", text)
            self.assertIn("data_type: double", text)
            mock_client.execute_query.assert_called_once_with(
                "DESCRIBE TABLE `main`.`gold`.`fct_kpi_001`"
            )

    def test_mart_not_built_yet_is_skipped_not_an_error(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            _create_kpi001_workspace(root)

            mock_cfg = MagicMock(is_active=lambda: True)
            mock_client = MagicMock()
            mock_client.execute_query.side_effect = RuntimeError("TABLE_OR_VIEW_NOT_FOUND")
            with patch("core.config.resolve_databricks_config", return_value=mock_cfg), patch(
                "core.execution.databricks_client.DatabricksClient", return_value=mock_client
            ):
                result = DbtProjectGenerator(
                    root, "workspaces/demo", catalog="main", schema="rcm",
                    enforce_contracts=True,
                ).generate()

            self.assertEqual(result.kpi_count, 1)  # generation itself still succeeds
            self.assertFalse(
                (root / result.dbt_project_dir / "models" / "marts" / "_contracts.yml").exists()
            )

    def test_config_inactive_skips_without_error(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            _create_kpi001_workspace(root)

            mock_cfg = MagicMock(is_active=lambda: False)
            with patch("core.config.resolve_databricks_config", return_value=mock_cfg):
                result = DbtProjectGenerator(
                    root, "workspaces/demo", catalog="main", schema="rcm",
                    enforce_contracts=True,
                ).generate()
            self.assertFalse(
                (root / result.dbt_project_dir / "models" / "marts" / "_contracts.yml").exists()
            )


if __name__ == "__main__":
    unittest.main()
