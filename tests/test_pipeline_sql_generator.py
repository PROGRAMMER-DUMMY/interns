from __future__ import annotations

import re
import tempfile
import unittest
from pathlib import Path

from core.onboarding.kpi.feature_resolver import KPIFeatureResolver
from core.onboarding.kpi.sql_generator import DuckDBKPISQLGenerator, _formula_inputs, choose_feature_ref
from core.onboarding.pipeline_plan import (
    DataEngineeringRoutePlanner,
    PipelineDecisionRecorder,
    PipelinePlanner,
)
from core.onboarding.pipeline_sql_generator import PipelineSQLGenerator
from core.onboarding.relationships.source_to_target_planner import SourceToTargetPlanner
from core.onboarding.workspace.onboarding import WorkspaceOnboarder


class PipelineSQLGeneratorTests(unittest.TestCase):
    def _create_workspace(self, root: Path) -> Path:
        workspace = root / "workspaces" / "demo"
        (workspace / "datasets").mkdir(parents=True)
        (workspace / "docs").mkdir(parents=True)
        (workspace / "datasets" / "transactions.csv").write_text(
            "ClaimID,PaidAmount,LineOfBusiness,ServiceDate\n"
            "C1,10.50,Commercial,2024-01-01\n"
            "C2,20.25,Medicare,2024-01-02\n",
            encoding="utf-8",
        )
        (workspace / "docs" / "kpi_registry.csv").write_text(
            "Key business question,Description,Cuts / grain hints,Metric,Data model refinement required\n"
            "What is paid amount by line of business?,Baseline KPI,LineOfBusiness,sum(PaidAmount),Confirm paid amount source\n",
            encoding="utf-8",
        )
        (workspace / "docs" / "data_model.md").write_text(
            "# Data Model\n\ntransactions has ClaimID, PaidAmount, LineOfBusiness, ServiceDate.\n",
            encoding="utf-8",
        )
        return workspace

    def test_generates_layer_sql_with_raw_paths_only_in_bootstrap(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self._create_workspace(root)
            WorkspaceOnboarder(root, "workspaces/demo", sample_rows=10).run()
            KPIFeatureResolver(root, "workspaces/demo", domain="healthcare").run()
            SourceToTargetPlanner(root, "workspaces/demo", target_engine="sql").build()
            DataEngineeringRoutePlanner(root, "workspaces/demo", track="medallion").build()
            PipelineDecisionRecorder(root, "workspaces/demo").record_table_format("local_parquet")
            PipelinePlanner(root, "workspaces/demo", track="medallion").build()

            result = PipelineSQLGenerator(root, "workspaces/demo").generate()

            self.assertEqual(result.status, "generated")
            sql = (root / result.path).read_text(encoding="utf-8")
            self.assertIn("-- BEGIN CATALOG BOOTSTRAP", sql)
            self.assertIn("read_csv_auto('workspaces/demo/datasets/transactions.csv'", sql)
            self.assertIn('"bronze_transactions"', sql)
            self.assertIn('"silver_transactions"', sql)
            self.assertIn('"gold_transactions"', sql)
            business_sql = re.sub(
                r"--\s*BEGIN CATALOG BOOTSTRAP\b.*?--\s*END CATALOG BOOTSTRAP\b",
                "",
                sql,
                flags=re.IGNORECASE | re.DOTALL,
            )
            self.assertNotIn("read_csv_auto", business_sql)
            self.assertNotIn("workspaces/demo/datasets/transactions.csv", business_sql)

    def test_generates_format_specific_bootstrap_reader(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            workspace = root / "workspaces" / "demo"
            contracts = workspace / "interns" / "generated" / "contracts"
            contracts.mkdir(parents=True)
            (contracts / "catalog_contract.json").write_text(
                """
{
  "artifact_type": "catalog_contract.json",
  "objects": [
    {
      "logical_name": "raw.claims",
      "dataset": "workspaces/demo/datasets/claims.parquet",
      "format": "parquet",
      "physical_bindings": [{"binding_type": "duckdb_view", "object_name": "catalog_raw_claims"}]
    }
  ]
}
""".strip(),
                encoding="utf-8",
            )
            (contracts / "pipeline_plan.json").write_text(
                """
{
  "artifact_type": "pipeline_plan.json",
  "status": "ready_for_generation",
  "selected_track": "medallion",
  "layers": [
    {
      "layer": "bronze",
      "objects": [{"source_object": "raw.claims", "target_object": "bronze.claims"}]
    }
  ]
}
""".strip(),
                encoding="utf-8",
            )

            result = PipelineSQLGenerator(root, "workspaces/demo").generate()

            sql = (root / result.path).read_text(encoding="utf-8")
            self.assertIn("read_parquet('workspaces/demo/datasets/claims.parquet'", sql)
            self.assertNotIn("read_csv_auto('workspaces/demo/datasets/claims.parquet'", sql)

    def test_blocks_when_pipeline_plan_has_blockers(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self._create_workspace(root)
            WorkspaceOnboarder(root, "workspaces/demo", sample_rows=10).run()
            DataEngineeringRoutePlanner(root, "workspaces/demo", track="existing_gold_validation").build()
            PipelinePlanner(root, "workspaces/demo", track="existing_gold_validation").build()

            with self.assertRaises(ValueError):
                PipelineSQLGenerator(root, "workspaces/demo").generate()

    def test_denominator_decision_allows_percentage_pipeline_sql_generation(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self._create_workspace(root)
            WorkspaceOnboarder(root, "workspaces/demo", sample_rows=10).run()
            contracts = root / "workspaces" / "demo" / "interns" / "generated" / "contracts"
            (contracts / "source_to_target_plan.json").write_text(
                '{"summary":{"blocked_kpi_count":0},"kpis":[{"kpi_id":"kpi_001","business_question":"percentage share","metric":"percentage share"}]}',
                encoding="utf-8",
            )
            DataEngineeringRoutePlanner(root, "workspaces/demo", track="medallion").build()
            PipelineDecisionRecorder(root, "workspaces/demo").record_table_format("local_parquet")
            PipelineDecisionRecorder(root, "workspaces/demo").record_denominator_scope("kpi_001", "global_total")
            PipelinePlanner(root, "workspaces/demo", track="medallion").build()

            result = PipelineSQLGenerator(root, "workspaces/demo").generate()

            self.assertEqual(result.status, "generated")


class FormulaInputsTests(unittest.TestCase):
    """_formula_inputs must never resolve ordinary English words in a
    free-text formula/definition as candidate columns when explicit
    source_columns are already recorded.

    Found live: a "WHERE EXISTS ... for that account in the period" custom
    definition, with source_columns correctly bound to party.party_key and
    invoices.Status, still re-tokenized the raw formula text with an
    unfiltered regex and treated "period" as an extra candidate column --
    which then resolved against an unrelated table (settlements.csv) purely
    because it happened to have a column with that name.
    """

    def test_explicit_source_columns_are_trusted_without_reparsing_formula_text(self) -> None:
        feature = {
            "source_columns": [{"column": "party_key"}, {"column": "Status"}],
            "evidence": [
                {
                    "type": "workspace_feature_definition",
                    "detail": (
                        "party.party_key WHERE EXISTS a non-void invoice "
                        "(invoices.Status != 'VOID') for that account in the period"
                    ),
                }
            ],
        }
        inputs = _formula_inputs(feature)
        self.assertEqual(inputs, ["party_key", "Status"])
        self.assertNotIn("period", inputs)
        self.assertNotIn("account", inputs)

    def test_falls_back_to_filtered_formula_parse_when_source_columns_empty(self) -> None:
        feature = {
            "source_columns": [],
            "evidence": [
                {
                    "type": "workspace_feature_definition",
                    "detail": "SUM(invoices.Date) for that account in the period",
                }
            ],
        }
        inputs = _formula_inputs(feature)
        self.assertIn("invoices", inputs)
        self.assertIn("Date", inputs)
        self.assertNotIn("for", inputs)
        self.assertNotIn("that", inputs)
        self.assertNotIn("in", inputs)
        self.assertNotIn("the", inputs)


class ChooseFeatureRefTests(unittest.TestCase):
    """choose_feature_ref is the single source of truth every engine (SQL,
    Polars, PySpark) shares for per-feature column resolution -- a fix here
    is engine-agnostic by construction, not something to special-case per
    engine.

    A feature with exactly ONE recorded source ref (a human already
    confirmed which column it means, or an earlier resolution proved it
    unambiguously) must return that ref untouched. Found live: the
    base-source-preference fallback matched candidates by column NAME ALONE
    against the base table's own schema, so a feature confirmed as
    `cargo_claims.Id` silently resolved to `shipments.Id` instead whenever
    shipments (the query's base source) happened to also have a column
    named "Id" -- a human-confirmed answer silently overridden downstream.
    """

    def test_single_confirmed_ref_is_returned_even_when_base_source_has_a_same_named_column(self):
        feature = {
            "feature": "cargo_claims",
            "state": "user_confirmed",
            "source_columns": [{"dataset": "workspaces/demo/datasets/cargo_claims.csv", "column": "Id"}],
        }
        ref = choose_feature_ref(
            feature,
            base_source="workspaces/demo/datasets/shipments.csv",
            all_refs=[],
            profile_map={
                "workspaces/demo/datasets/shipments.csv": {"schema": {"Id": "string"}},
                "workspaces/demo/datasets/cargo_claims.csv": {"schema": {"Id": "string"}},
            },
            repo_root=Path("."),
        )
        self.assertEqual(ref["dataset"], "workspaces/demo/datasets/cargo_claims.csv")

    def test_multiple_candidate_refs_still_prefer_the_base_source(self):
        # The disambiguation logic this fix leaves untouched: with genuine
        # ambiguity (more than one recorded ref), preferring the base source
        # is still correct.
        feature = {
            "feature": "carrier_cd",
            "state": "proven_alias",
            "source_columns": [
                {"dataset": "workspaces/demo/datasets/settlements.csv", "column": "carrier_cd"},
                {"dataset": "workspaces/demo/datasets/shipments.csv", "column": "carrier_cd"},
            ],
        }
        ref = choose_feature_ref(
            feature,
            base_source="workspaces/demo/datasets/shipments.csv",
            all_refs=[],
            profile_map={
                "workspaces/demo/datasets/shipments.csv": {"schema": {"carrier_cd": "string"}},
                "workspaces/demo/datasets/settlements.csv": {"schema": {"carrier_cd": "string"}},
            },
            repo_root=Path("."),
        )
        self.assertEqual(ref["dataset"], "workspaces/demo/datasets/shipments.csv")


class DerivedFormulaRefsTests(unittest.TestCase):
    """_derived_formula_refs must trust an explicitly-declared source_columns
    dataset over bare-name matching against the whole profile map.

    Found live: a derived formula explicitly declared `invoices.acct` as a
    source column, but _source_for_column's fallback (search every profiled
    dataset for a matching column name, return the alphabetically-first
    match) resolved "acct" to addr.csv instead, purely because addr.csv also
    happens to have a column named "acct" and sorts before "invoices".
    """

    def _generator(self, root: Path) -> DuckDBKPISQLGenerator:
        (root / "workspaces" / "demo").mkdir(parents=True, exist_ok=True)
        return DuckDBKPISQLGenerator(root, "workspaces/demo")

    def test_declared_dataset_wins_over_alphabetically_first_bare_name_match(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            gen = self._generator(root)
            kpi = {
                "kpi_id": "kpi_test",
                "features": [
                    {
                        "feature": "churned",
                        "resolution_type": "derived_formula",
                        "source_columns": [
                            {"dataset": "workspaces/demo/datasets/invoices.csv", "column": "acct"},
                        ],
                    }
                ],
            }
            profile_map = {
                "workspaces/demo/datasets/addr.csv": {"schema": {"acct": "string"}},
                "workspaces/demo/datasets/invoices.csv": {"schema": {"acct": "string"}},
            }
            refs = gen._derived_formula_refs(kpi, "workspaces/demo/datasets/party.csv", profile_map)
            self.assertEqual(refs, [{"dataset": "workspaces/demo/datasets/invoices.csv", "column": "acct"}])


class BUG022NoMixSourceLayerTests(unittest.TestCase):
    """BUG-022: a single SQL generation run must never mix read_csv_auto and delta_scan."""

    def _make_generator(self, root: Path) -> DuckDBKPISQLGenerator:
        workspace = root / "workspaces" / "demo"
        workspace.mkdir(parents=True, exist_ok=True)
        return DuckDBKPISQLGenerator(root, "workspaces/demo")

    def _stub_staging_sql(self, sources: list[str]) -> str:
        """Build a minimal staging SQL fragment with one read_csv_auto per source."""
        lines = []
        for src in sources:
            lines.append(
                f"CREATE OR REPLACE VIEW \"catalog_raw_stub\" AS "
                f"SELECT * FROM read_csv_auto('{src}', union_by_name=true);"
            )
        return "\n".join(lines)

    def test_all_csv_run_emits_only_read_csv_auto(self):
        """When no Delta bronze exists for any source, all views stay CSV."""
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            gen = self._make_generator(root)
            sources = [
                "workspaces/demo/datasets/orders.csv",
                "workspaces/demo/datasets/products.csv",
            ]
            staging_sql = self._stub_staging_sql(sources)
            result_sql = gen._staging_with_delta(staging_sql, {}, sources)
            self.assertNotIn("delta_scan", result_sql)
            self.assertIn("read_csv_auto", result_sql)

    def test_partial_delta_falls_back_to_all_csv(self):
        """When only SOME sources have Delta bronze, the run must NOT use delta_scan at all.

        This is the core BUG-022 fix: mixed CSV+delta within a single run is forbidden.
        The run falls back uniformly to CSV when not all sources are materialized.
        """
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            gen = self._make_generator(root)
            # Materialize bronze for one source but not the other
            bronze_dir = gen.layout.bronze_dir
            (bronze_dir / "orders" / "_delta_log").mkdir(parents=True, exist_ok=True)
            sources = [
                "workspaces/demo/datasets/orders.csv",    # has delta
                "workspaces/demo/datasets/products.csv",  # does NOT have delta
            ]
            staging_sql = self._stub_staging_sql(sources)
            result_sql = gen._staging_with_delta(staging_sql, {}, sources)
            # Must not mix — no delta_scan when any source is missing delta
            self.assertNotIn("delta_scan", result_sql)
            self.assertIn("read_csv_auto", result_sql)

    def test_all_delta_run_emits_only_delta_scan(self):
        """When ALL sources have Delta bronze (no warehouse), delta_scan is used uniformly."""
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            gen = self._make_generator(root)
            bronze_dir = gen.layout.bronze_dir
            for stem in ("orders", "products"):
                (bronze_dir / stem / "_delta_log").mkdir(parents=True, exist_ok=True)
            sources = [
                "workspaces/demo/datasets/orders.csv",
                "workspaces/demo/datasets/products.csv",
            ]
            staging_sql = self._stub_staging_sql(sources)
            result_sql = gen._staging_with_delta(staging_sql, {}, sources)
            self.assertNotIn("read_csv_auto", result_sql)
            self.assertIn("delta_scan", result_sql)

    def test_resolve_run_source_mode_returns_csv_when_any_source_missing_delta(self):
        """_resolve_run_source_mode returns 'csv' when not all sources have delta bronze."""
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            gen = self._make_generator(root)
            bronze_dir = gen.layout.bronze_dir
            (bronze_dir / "orders" / "_delta_log").mkdir(parents=True, exist_ok=True)
            sources = [
                "workspaces/demo/datasets/orders.csv",
                "workspaces/demo/datasets/products.csv",
            ]
            mode = gen._resolve_run_source_mode(sources)
            self.assertEqual(mode, "csv")

    def test_resolve_run_source_mode_returns_delta_when_all_sources_have_delta(self):
        """_resolve_run_source_mode returns 'delta' when all sources have delta bronze."""
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            gen = self._make_generator(root)
            bronze_dir = gen.layout.bronze_dir
            for stem in ("orders", "products"):
                (bronze_dir / stem / "_delta_log").mkdir(parents=True, exist_ok=True)
            sources = [
                "workspaces/demo/datasets/orders.csv",
                "workspaces/demo/datasets/products.csv",
            ]
            mode = gen._resolve_run_source_mode(sources)
            self.assertEqual(mode, "delta")


if __name__ == "__main__":
    unittest.main()
