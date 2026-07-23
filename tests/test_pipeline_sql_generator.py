from __future__ import annotations

import re
import tempfile
import unittest
from pathlib import Path

from core.onboarding.kpi.feature_resolver import KPIFeatureResolver
from core.onboarding.kpi.sql_generator import (
    DuckDBKPISQLGenerator,
    _bare_formula_columns,
    _derived_formula,
    _formula_inputs,
    choose_feature_ref,
    plan_required_sources,
)
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

    def test_bare_formula_columns_excludes_dot_qualified_only_references(self) -> None:
        # party_key is bare (correlated to the outer row); acct/Status/Date
        # are always dot-qualified inside the formula's own subquery.
        feature = {
            "source_columns": [
                {"dataset": "workspaces/demo/datasets/party.csv", "column": "party_key"},
                {"dataset": "workspaces/demo/datasets/invoices.csv", "column": "acct"},
                {"dataset": "workspaces/demo/datasets/invoices.csv", "column": "Status"},
            ],
            "evidence": [
                {
                    "type": "workspace_feature_definition",
                    "detail": (
                        "CASE WHEN EXISTS (SELECT 1 FROM \"invoices\" i "
                        "WHERE i.acct = party_key AND i.Status != 'VOID') "
                        "THEN 1 ELSE 0 END"
                    ),
                }
            ],
        }
        bare = _bare_formula_columns(feature)
        self.assertEqual(bare, {"party_key"})

    def test_join_chain_skips_dot_qualified_only_dataset_no_fanout(self) -> None:
        # End-to-end: a formula referencing another table ONLY inside its
        # own subquery must not join that table into the base grain at all
        # -- doing so fans the base grain out (found live: a 240-account
        # table summed to over 4,500 once every one of an account's
        # invoices got joined in for a formula that never used the join).
        kpi = {
            "kpi_id": "kpi_test",
            "features": [
                {
                    "feature": "churned",
                    "resolution_type": "derived_formula",
                    "source_columns": [
                        {"dataset": "workspaces/demo/datasets/party.csv", "column": "party_key"},
                        {"dataset": "workspaces/demo/datasets/invoices.csv", "column": "acct"},
                        {"dataset": "workspaces/demo/datasets/invoices.csv", "column": "Status"},
                    ],
                    "evidence": [
                        {
                            "type": "workspace_feature_definition",
                            "detail": (
                                "CASE WHEN EXISTS (SELECT 1 FROM \"invoices\" i "
                                "WHERE i.acct = party_key AND i.Status != 'VOID') "
                                "THEN 1 ELSE 0 END"
                            ),
                        }
                    ],
                }
            ],
        }
        profile_map = {
            "workspaces/demo/datasets/party.csv": {"schema": {"party_key": "string"}},
            "workspaces/demo/datasets/invoices.csv": {"schema": {"acct": "string", "Status": "string"}},
        }
        # Pinned to party.csv, matching the real scenario: a human-confirmed
        # base_source answer (per the platform's base_source_selection
        # blocker) already settled this; the property under test is what
        # happens to the OTHER (dot-qualified-only) dataset once a base is
        # chosen, not the base-selection heuristic itself.
        base_source, required_sources, chosen = plan_required_sources(
            kpi, profile_map, Path("."), pinned="workspaces/demo/datasets/party.csv",
        )
        self.assertEqual(base_source, "workspaces/demo/datasets/party.csv")
        self.assertNotIn("workspaces/demo/datasets/invoices.csv", required_sources)

    def test_plan_required_sources_trusts_declared_dataset_too(self) -> None:
        # plan_required_sources is the explicitly cross-engine-shared source
        # plan (SQL, Polars, PySpark all consume it) -- the same bare-name-
        # matching bug lived here too, independently of the SQL-generator-
        # only copies above, so a fix scoped to only those wouldn't have
        # protected Polars/PySpark from the same wrong-table resolution.
        kpi = {
            "kpi_id": "kpi_test",
            "features": [
                {
                    "feature": "churned",
                    "resolution_type": "derived_formula",
                    "source_columns": [
                        {"dataset": "workspaces/demo/datasets/party.csv", "column": "party_key"},
                        {"dataset": "workspaces/demo/datasets/invoices.csv", "column": "acct"},
                    ],
                }
            ],
        }
        profile_map = {
            "workspaces/demo/datasets/party.csv": {"schema": {"party_key": "string"}},
            "workspaces/demo/datasets/addr.csv": {"schema": {"acct": "string"}},
            "workspaces/demo/datasets/invoices.csv": {"schema": {"acct": "string"}},
        }
        base_source, required_sources, chosen = plan_required_sources(
            kpi, profile_map, Path("."),
        )
        self.assertNotIn("workspaces/demo/datasets/addr.csv", required_sources)
        self.assertIn("workspaces/demo/datasets/invoices.csv", required_sources)


class ReservedWordFormulaColumnTests(unittest.TestCase):
    """A declared source column whose name collides with a SQL reserved
    keyword used syntactically inside the formula (e.g. a column literally
    named "END" alongside a CASE...END expression) must not have every bare
    occurrence blindly substituted -- that rewrites the CASE-closing keyword
    itself into a column reference and produces invalid SQL.

    Found live (kpi_001, Hostile_Synthetic): `CASE WHEN date_diff('day',
    START, END) <= sla_days THEN 1 ELSE 0 END`, where shipments has real
    START/END columns, generated `... ELSE 0 s0."END" AS "on_time"` --
    the formula's own closing END got rewritten.
    """

    def _generator(self, root: Path) -> DuckDBKPISQLGenerator:
        (root / "workspaces" / "demo").mkdir(parents=True, exist_ok=True)
        return DuckDBKPISQLGenerator(root, "workspaces/demo")

    def test_bare_end_keyword_is_not_rewritten_when_end_is_also_a_column(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            gen = self._generator(root)
            feature = {
                "resolution_type": "derived_formula",
                "source_columns": [
                    {"dataset": "workspaces/demo/datasets/shipments.csv", "column": "START"},
                    {"dataset": "workspaces/demo/datasets/shipments.csv", "column": "END"},
                    {"dataset": "workspaces/demo/datasets/svc_catalog.csv", "column": "sla_days"},
                ],
                "evidence": [
                    {
                        "type": "workspace_feature_definition",
                        "detail": (
                            "CASE WHEN date_diff('day', START, \"END\") <= sla_days "
                            "THEN 1 ELSE 0 END"
                        ),
                    }
                ],
            }
            source_aliases = {
                "workspaces/demo/datasets/shipments.csv": "s0",
                "workspaces/demo/datasets/svc_catalog.csv": "s1",
            }
            profile_map = {
                "workspaces/demo/datasets/shipments.csv": {
                    "schema": {"START": "date", "END": "date"}
                },
                "workspaces/demo/datasets/svc_catalog.csv": {"schema": {"sla_days": "int"}},
            }
            expression = gen._feature_expression(feature, source_aliases, profile_map)
            self.assertEqual(
                expression,
                "CASE WHEN date_diff('day', s0.\"START\", s0.\"END\") <= s1.\"sla_days\" "
                "THEN 1 ELSE 0 END",
            )
            # Exactly one bare, unqualified END remains: the CASE terminator.
            self.assertEqual(len(re.findall(r"(?<!\.)\bEND\b(?!\")", expression)), 1)


class ParenlessFormulaDetectionTests(unittest.TestCase):
    """_derived_formula must recognize a formula-shaped workspace definition
    even when it contains no function-call parens.

    A "workspace_feature_definition" evidence detail is either a genuine
    formula or a plain "dataset.column" pin (recorded for a direct_column /
    physical_column apply) -- both share the same evidence type, so the
    detail TEXT is the only way to tell them apart. Requiring "(" rejected a
    bare CASE/comparison formula with no function call.

    Found live (kpi_004, Hostile_Synthetic): `CASE WHEN Status = 'DELIVERED'
    THEN 1 ELSE 0 END` has no parens at all, so _derived_formula returned ""
    and the feature produced no SELECT item in the generated features view --
    while the result view's WHERE clause still referenced it by name,
    raising a DuckDB binder error ('delivered' not found).
    """

    def test_bare_case_formula_without_parens_is_recognized(self) -> None:
        feature = {
            "resolution_type": "derived_formula",
            "source_columns": [
                {"dataset": "workspaces/demo/datasets/shipments.csv", "column": "Status"},
            ],
            "evidence": [
                {
                    "type": "workspace_feature_definition",
                    "detail": "CASE WHEN Status = 'DELIVERED' THEN 1 ELSE 0 END",
                }
            ],
        }
        self.assertEqual(
            _derived_formula(feature),
            "CASE WHEN Status = 'DELIVERED' THEN 1 ELSE 0 END",
        )

    def test_plain_column_pin_is_still_not_mistaken_for_a_formula(self) -> None:
        feature = {
            "resolution_type": "physical_column",
            "source_columns": [
                {"dataset": "workspaces/demo/datasets/shipments.csv", "column": "Id"},
            ],
            "evidence": [
                {
                    "type": "workspace_feature_definition",
                    "detail": "workspaces/demo/datasets/shipments.csv.Id",
                }
            ],
        }
        self.assertEqual(_derived_formula(feature), "")


class DerivedFormulaLatestEvidenceWinsTests(unittest.TestCase):
    """_derived_formula must use the MOST RECENTLY applied definition, not
    the first one ever recorded.

    Found live: evidence entries are append-only (re-applying a workspace
    definition -- e.g. a human correcting an earlier formula that had an
    unqualified column reference -- appends a new entry rather than
    replacing the old one). Picking the first match silently kept using an
    already-superseded, broken formula across multiple resolver runs even
    though the corrected one was right there in the same list.
    """

    def test_last_workspace_feature_definition_evidence_wins(self) -> None:
        feature = {
            "evidence": [
                {"type": "workspace_feature_definition", "detail": "OLD_FORMULA(x)"},
                {"type": "schema_alias", "detail": "irrelevant"},
                {"type": "workspace_feature_definition", "detail": "CORRECTED_FORMULA(x)"},
            ]
        }
        self.assertEqual(_derived_formula(feature), "CORRECTED_FORMULA(x)")

    def test_falls_back_to_source_column_detail_when_no_evidence_match(self) -> None:
        feature = {
            "evidence": [],
            "source_columns": [{"detail": "FALLBACK_FORMULA(y)"}],
        }
        self.assertEqual(_derived_formula(feature), "FALLBACK_FORMULA(y)")


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
