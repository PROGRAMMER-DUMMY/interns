import json
import tempfile
import unittest
from pathlib import Path

from core.onboarding.kpi.execution_harness import KPIExecutionHarness
from core.onboarding.kpi.sql_generator import DuckDBKPISQLGenerator
from core.onboarding.benchmark.agent_benchmark import AgentBenchmarkScorecardBuilder
from core.onboarding.workspace.validation import WorkspaceArtifactValidator


class KPIExecutionHarnessTests(unittest.TestCase):
    def _write_harness_artifact(self, root: Path, payload: dict) -> Path:
        path = root / "workspaces" / "demo" / "interns" / "generated" / "evidence" / "kpi_execution_harness.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(payload), encoding="utf-8")
        return path

    def test_harness_executes_exact_result_view_and_writes_table_sample(self):
        try:
            import duckdb  # noqa: F401
        except ImportError:
            self.skipTest("duckdb is not installed")

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            solutions = root / "workspaces" / "demo" / "interns" / "generated" / "solutions"
            solutions.mkdir(parents=True)
            (solutions / "kpi_001.sql").write_text(
                'CREATE OR REPLACE VIEW "kpi_001_results" AS SELECT 42 AS answer;',
                encoding="utf-8",
            )

            result = KPIExecutionHarness(root, "workspaces/demo", sample_limit=5).run()

            self.assertTrue(result.ok)
            self.assertEqual(result.records[0].result_view, "kpi_001_results")
            self.assertEqual(result.records[0].row_count, 1)
            self.assertIn("| answer |", result.records[0].sample_output_table)
            manifest = json.loads(
                (
                    root
                    / "workspaces"
                    / "demo"
                    / "interns"
                    / "generated"
                    / "evidence"
                    / "kpi_execution_harness.json"
                ).read_text(encoding="utf-8")
            )
            self.assertTrue(manifest["ok"])

    def test_sql_generator_produces_generic_aggregation_from_metric_and_cuts(self):
        """The result-view SQL is now built by a workspace-agnostic generic
        builder that parses `kpi.metric` + `kpi.cuts` into structural SQL.
        No domain vocabulary should appear; a SUM/GROUP BY shape should be
        derived for any KPI with `sum(...)` metric + dimensional cuts."""
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            workspace = root / "workspaces" / "demo"
            workspace.mkdir(parents=True)
            generator = DuckDBKPISQLGenerator(root, "workspaces/demo")

            sql = generator._result_view_sql(
                {"name": "Anything", "metric": "sum(x)", "cuts": "y, z", "features": []},
                "kpi_001",
            )
            self.assertIn('CREATE OR REPLACE VIEW "kpi_001_results"', sql)
            self.assertIn('SUM("x")', sql)
            self.assertIn('"y", "z"', sql)
            self.assertIn("GROUP BY", sql)
            for healthcare_word in ("medicare", "patient", "encounter", "payor", "claim"):
                self.assertNotIn(healthcare_word, sql.lower())

    def test_sql_generator_falls_back_for_unparseable_metric(self):
        """When a metric is genuinely unparseable (free-form prose, no agg
        function), the platform emits a clearly-commented SELECT * fallback
        rather than silently-wrong SQL."""
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            workspace = root / "workspaces" / "demo"
            workspace.mkdir(parents=True)
            generator = DuckDBKPISQLGenerator(root, "workspaces/demo")

            sql = generator._result_view_sql(
                {
                    "name": "Free-form goal",
                    "metric": "a narrative metric description without any agg fn",
                    "cuts": "",
                    "features": [],
                },
                "kpi_complex",
            )
            self.assertIn("-- Generic builder fallback:", sql)
            self.assertIn('SELECT * FROM "kpi_complex_features"', sql)

    def test_harness_rejects_feature_view_without_final_result_view(self):
        try:
            import duckdb  # noqa: F401
        except ImportError:
            self.skipTest("duckdb is not installed")

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            solutions = root / "workspaces" / "demo" / "interns" / "generated" / "solutions"
            solutions.mkdir(parents=True)
            (solutions / "kpi_001.sql").write_text(
                'CREATE OR REPLACE VIEW "kpi_001_features" AS SELECT 42 AS answer;',
                encoding="utf-8",
            )

            result = KPIExecutionHarness(root, "workspaces/demo").run()

            self.assertFalse(result.ok)
            self.assertIn("kpi_001_results", result.records[0].errors[0])

    def test_harness_rejects_placeholder_ready_marker_result(self):
        try:
            import duckdb  # noqa: F401
        except ImportError:
            self.skipTest("duckdb is not installed")

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            solutions = root / "workspaces" / "demo" / "interns" / "generated" / "solutions"
            solutions.mkdir(parents=True)
            (solutions / "kpi_001.sql").write_text(
                'CREATE OR REPLACE VIEW "kpi_001_results" AS SELECT 1 AS ready_marker;',
                encoding="utf-8",
            )

            result = KPIExecutionHarness(root, "workspaces/demo").run()

            self.assertFalse(result.ok)
            self.assertIn("placeholder", result.records[0].errors[0])

    def test_harness_rejects_sql_that_ignores_workbook_metric_semantics(self):
        try:
            import duckdb  # noqa: F401
        except ImportError:
            self.skipTest("duckdb is not installed")

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            workspace = root / "workspaces" / "demo"
            solutions = workspace / "interns" / "generated" / "solutions"
            contracts = workspace / "interns" / "generated" / "contracts"
            solutions.mkdir(parents=True)
            contracts.mkdir(parents=True)
            (contracts / "kpi_registry.json").write_text(
                json.dumps(
                    {
                        "kpis": [
                            {
                                "name": "What are top 10 payers in Commercial LOB w.r.t. amount paid",
                                "description": "Top 10 Payers for LOB w.r.t. Amount Paid",
                                "cuts": "LineOfBusiness, PayorID, LOB = Commercial",
                                "metric": "sum(PaidAmount)",
                            }
                        ]
                    }
                ),
                encoding="utf-8",
            )
            (solutions / "kpi_001.sql").write_text(
                "\n".join(
                    [
                        'CREATE OR REPLACE VIEW "kpi_001_results" AS',
                        "SELECT 'PAYOR1' AS payer, COUNT(*) AS encounter_count;",
                    ]
                ),
                encoding="utf-8",
            )

            result = KPIExecutionHarness(root, "workspaces/demo").run()

            self.assertFalse(result.ok)
            self.assertTrue(result.records[0].semantic_checks)
            self.assertTrue(
                any("sum(PaidAmount)" in error for error in result.records[0].errors)
            )

    def test_harness_accepts_sum_distinct_metric_implemented_as_count_distinct(self):
        # A sum(distinct X) metric is a distinct count; the builder renders it as
        # COUNT(DISTINCT X). The semantic gate must accept that faithful
        # rendering rather than demanding a literal SUM(. (Also tolerates the
        # `disitnct` misspelling that appears in the source workbook.)
        try:
            import duckdb  # noqa: F401
        except ImportError:
            self.skipTest("duckdb is not installed")

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            workspace = root / "workspaces" / "demo"
            solutions = workspace / "interns" / "generated" / "solutions"
            contracts = workspace / "interns" / "generated" / "contracts"
            solutions.mkdir(parents=True)
            contracts.mkdir(parents=True)
            (contracts / "kpi_registry.json").write_text(
                json.dumps(
                    {
                        "kpis": [
                            {
                                "name": "percentage share of lives by department",
                                "cuts": "Department Name",
                                "metric": (
                                    "percentage of sum(distinct PatientID) / "
                                    "sum(disitnct PatientID) for departement"
                                ),
                                "features": [
                                    {"feature": "PatientID",
                                     "source_columns": [{"column": "PatientID"}]},
                                    {"feature": "Department Name",
                                     "source_columns": [{"column": "name"}]},
                                ],
                            }
                        ]
                    }
                ),
                encoding="utf-8",
            )
            (solutions / "kpi_001.sql").write_text(
                "\n".join(
                    [
                        'CREATE OR REPLACE VIEW "kpi_001_results" AS',
                        "SELECT DISTINCT name,",
                        '  COUNT(DISTINCT "PatientID") OVER (PARTITION BY name) AS per_dept,',
                        '  COUNT(DISTINCT "PatientID") OVER () AS total',
                        "FROM (VALUES ('A', 1), ('A', 2), ('B', 3))"
                        ' AS t(name, "PatientID");',
                    ]
                ),
                encoding="utf-8",
            )

            result = KPIExecutionHarness(root, "workspaces/demo").run()

            # The metric-implementation gate must NOT reject the COUNT(DISTINCT)
            # rendering of sum(distinct PatientID).
            self.assertFalse(
                any(
                    "does not implement workbook metric" in error
                    for error in result.records[0].errors
                ),
                result.records[0].errors,
            )
            self.assertEqual(result.records[0].status, "passed", result.records[0].errors)

    def test_validator_rejects_generated_sql_without_result_view(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            workspace = root / "workspaces" / "demo"
            contracts = workspace / "interns" / "generated" / "contracts"
            solutions = workspace / "interns" / "generated" / "solutions"
            contracts.mkdir(parents=True)
            solutions.mkdir(parents=True)
            (contracts / "kpi_feature_mapping.json").write_text(
                json.dumps(
                    {
                        "artifact_type": "kpi_feature_mapping.json",
                        "version": 2,
                        "generated_by": "resolve-kpi-features",
                        "workspace": "workspaces/demo",
                        "kpis": [
                            {
                                "kpi_id": "kpi_001",
                                "name": "Demo KPI",
                                "status": "ready_for_sql",
                                "features": [],
                                "open_questions": [],
                            }
                        ],
                        "summary": {
                            "kpi_count": 1,
                            "ready_kpi_count": 1,
                            "blocked_kpi_count": 0,
                            "unresolved_feature_count": 0,
                        },
                        "blocker_clusters": [],
                    }
                ),
                encoding="utf-8",
            )
            (solutions / "kpi_001.sql").write_text(
                'CREATE OR REPLACE VIEW "kpi_001_features" AS SELECT 42 AS answer;',
                encoding="utf-8",
            )

            result = WorkspaceArtifactValidator(root, "workspaces/demo").run()

            self.assertFalse(result.ok)
            self.assertTrue(any("kpi_001_results" in error for error in result.errors))

    def test_benchmark_treats_onboarded_domain_model_as_data_model_ready(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            contracts = root / "workspaces" / "demo" / "interns" / "generated" / "contracts"
            contracts.mkdir(parents=True)
            (contracts / "domain_model.json").write_text(
                json.dumps(
                    {
                        "artifact_type": "domain_model.json",
                        "version": 1,
                        "generated_by": "onboard-workspace",
                        "workspace": "workspaces/demo",
                        "datasets": [{"path": "workspaces/demo/datasets/facts.csv"}],
                        "data_models": [],
                    }
                ),
                encoding="utf-8",
            )

            component = AgentBenchmarkScorecardBuilder(
                root,
                "workspaces/demo",
                domain="healthcare",
            )._data_model_readiness_component()

            self.assertEqual(component["status"], "ready")
            self.assertEqual(component["details"]["state"], "onboarded_domain_model")

    def test_benchmark_recognizes_passing_kpi_execution_harness_artifact(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            artifact_path = self._write_harness_artifact(
                root,
                {
                    "artifact_type": "kpi_execution_harness.json",
                    "version": 1,
                    "generated_by": "run-kpi-execution-harness",
                    "workspace": "workspaces/demo",
                    "ok": True,
                    "kpi_count": 1,
                    "passed_count": 1,
                    "failed_count": 0,
                    "records": [
                        {
                            "kpi_id": "kpi_001",
                            "sql_path": "workspaces/demo/interns/generated/solutions/kpi_001.sql",
                            "status": "passed",
                            "result_view": "kpi_001_results",
                            "row_count": 1,
                            "columns": ["answer"],
                            "sample_output_table": "| answer |\n| --- |\n| 42 |",
                            "errors": [],
                            "warnings": [],
                        }
                    ],
                    "manifest_path": "workspaces/demo/interns/generated/evidence/kpi_execution_harness.json",
                    "report_path": "workspaces/demo/interns/reports/kpi_execution_harness.md",
                },
            )

            component = AgentBenchmarkScorecardBuilder(root, "workspaces/demo")._kpi_execution_harness_component()

            self.assertEqual(component["status"], "ready")
            self.assertEqual(component["score"], 100.0)
            self.assertEqual(component["details"]["state"], "passed")
            self.assertIn(str(artifact_path.relative_to(root)).replace("\\", "/"), component["evidence_paths"])

    def test_benchmark_blocks_missing_or_failed_kpi_execution_harness_artifact(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            builder = AgentBenchmarkScorecardBuilder(root, "workspaces/demo")

            missing_component = builder._kpi_execution_harness_component()
            self.assertEqual(missing_component["status"], "blocked")
            self.assertEqual(missing_component["details"]["state"], "missing")
            self.assertTrue(missing_component["blockers"])

            self._write_harness_artifact(
                root,
                {
                    "artifact_type": "kpi_execution_harness.json",
                    "version": 1,
                    "generated_by": "run-kpi-execution-harness",
                    "workspace": "workspaces/demo",
                    "ok": False,
                    "kpi_count": 1,
                    "passed_count": 0,
                    "failed_count": 1,
                    "records": [
                        {
                            "kpi_id": "kpi_001",
                            "sql_path": "workspaces/demo/interns/generated/solutions/kpi_001.sql",
                            "status": "failed",
                            "result_view": "kpi_001_features",
                            "row_count": None,
                            "columns": [],
                            "sample_output_table": "",
                            "errors": ["final result view `kpi_001_results` was not created"],
                            "warnings": [],
                        }
                    ],
                    "manifest_path": "workspaces/demo/interns/generated/evidence/kpi_execution_harness.json",
                    "report_path": "workspaces/demo/interns/reports/kpi_execution_harness.md",
                },
            )

            failed_component = builder._kpi_execution_harness_component()
            self.assertEqual(failed_component["status"], "blocked")
            self.assertEqual(failed_component["details"]["state"], "failed")
            self.assertIn("KPI execution harness did not pass", failed_component["blockers"])
            self.assertIn("does not target exact result view `kpi_001_results`", " ".join(failed_component["blockers"]))

    def test_validator_rejects_placeholder_harness_columns(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self._write_harness_artifact(
                root,
                {
                    "artifact_type": "kpi_execution_harness.json",
                    "version": 1,
                    "generated_by": "run-kpi-execution-harness",
                    "workspace": "workspaces/demo",
                    "ok": True,
                    "kpi_count": 1,
                    "passed_count": 1,
                    "failed_count": 0,
                    "records": [
                        {
                            "kpi_id": "kpi_001",
                            "sql_path": "workspaces/demo/interns/generated/solutions/kpi_001.sql",
                            "status": "passed",
                            "result_view": "kpi_001_results",
                            "row_count": 1,
                            "columns": ["ready_marker"],
                            "sample_output_table": "| ready_marker |\n| --- |\n| 1 |",
                            "errors": [],
                            "warnings": [],
                        }
                    ],
                },
            )

            result = WorkspaceArtifactValidator(root, "workspaces/demo").run()

            self.assertFalse(result.ok)
            self.assertTrue(any("placeholder result columns" in error for error in result.errors))

    def test_benchmark_release_gates_require_kpi_execution_harness(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            builder = AgentBenchmarkScorecardBuilder(root, "workspaces/demo")
            components = {
                "kpi_readiness": {"status": "ready"},
                "data_model_readiness": {"status": "ready", "score": 100.0},
                "relationship_proof": {"status": "ready"},
                "source_to_target_readiness": {"status": "ready"},
                "validation_status": {"status": "ready"},
                "kpi_execution_harness": {"status": "blocked"},
                "presentation_readiness": {"status": "ready"},
                "wiki_reuse": {"status": "ready"},
                "workflow_checkpoint": {"status": "ready"},
                "autopilot_safety": {"status": "ready"},
            }

            gates = builder._release_gates(components, core_readiness=100.0, product_maturity=100.0)
            executable_gate = next(gate for gate in gates if gate["gate"] == "executable_sql_generation")
            production_gate = next(gate for gate in gates if gate["gate"] == "production_promotion")

            self.assertEqual(executable_gate["status"], "blocked")
            self.assertEqual(production_gate["status"], "blocked")


class DenominatorScopeHarnessTests(unittest.TestCase):
    """Phase 1: the execution harness must fail a KPI whose pipeline_decisions.json
    records a within-group denominator scope but whose SQL uses OVER () instead
    of OVER (PARTITION BY <group>).

    This is the live BUG-025: the review rubber-stamped unchanged SQL because
    the recorded decision was never enforced.
    """

    def _setup_workspace(
        self,
        tmp: str,
        *,
        denominator_scope: str | None,
        sql: str,
    ) -> "KPIExecutionHarness":  # type: ignore[name-defined]
        root = Path(tmp)
        workspace = root / "workspaces" / "demo"
        solutions = workspace / "interns" / "generated" / "solutions"
        contracts = workspace / "interns" / "generated" / "contracts"
        solutions.mkdir(parents=True)
        contracts.mkdir(parents=True)

        registry = {
            "kpis": [
                {
                    "kpi_id": "kpi_002",
                    "name": "Percentage share of lives by department",
                    "metric": (
                        "percentage of sum(distinct PatientID) / "
                        "sum(distinct PatientID) for departement"
                    ),
                    "cuts": "departement, Gender",
                    "features": [
                        {"feature": "PatientID",
                         "source_columns": [{"column": "PatientID"}]},
                        {"feature": "departement",
                         "source_columns": [{"column": "departement"}]},
                        {"feature": "Gender",
                         "source_columns": [{"column": "Gender"}]},
                    ],
                }
            ]
        }
        (contracts / "kpi_registry.json").write_text(
            json.dumps(registry), encoding="utf-8"
        )

        if denominator_scope is not None:
            decisions = {
                "percentage_denominator_scopes": {"kpi_002": denominator_scope}
            }
            (contracts / "pipeline_decisions.json").write_text(
                json.dumps(decisions), encoding="utf-8"
            )

        (solutions / "kpi_002.sql").write_text(sql, encoding="utf-8")
        return KPIExecutionHarness(root, "workspaces/demo")

    def test_harness_fails_when_within_scope_recorded_but_over_grand_total_used(self):
        """Recorded within_department scope + OVER () in SQL → semantic error."""
        try:
            import duckdb  # noqa: F401
        except ImportError:
            self.skipTest("duckdb is not installed")

        sql = "\n".join([
            'CREATE OR REPLACE VIEW "kpi_002_results" AS',
            "SELECT DISTINCT",
            '  "departement" AS departement,',
            '  "Gender" AS gender,',
            '  COUNT(DISTINCT "PatientID") OVER (PARTITION BY "departement", "Gender") AS per_group,',
            '  COUNT(DISTINCT "PatientID") OVER () AS total,',
            "  CAST(per_group AS DOUBLE) / NULLIF(total, 0) * 100 AS percentage_share",
            "FROM (VALUES ('A', 1, 'M'), ('A', 2, 'F'), ('B', 3, 'M'))"
            ' AS t("departement", "PatientID", "Gender");',
        ])
        with tempfile.TemporaryDirectory() as tmp:
            harness = self._setup_workspace(
                tmp,
                denominator_scope="within_department",
                sql=sql,
            )
            result = harness.run()

        self.assertFalse(result.ok)
        all_errors = " ".join(result.records[0].errors)
        self.assertIn("denominator_scope_not_realized", all_errors)

    def test_harness_passes_when_within_scope_and_partition_by_match(self):
        """Recorded within_department scope + OVER (PARTITION BY ...) → passes."""
        try:
            import duckdb  # noqa: F401
        except ImportError:
            self.skipTest("duckdb is not installed")

        sql = "\n".join([
            'CREATE OR REPLACE VIEW "kpi_002_results" AS',
            "SELECT DISTINCT",
            '  "departement" AS departement,',
            '  "Gender" AS gender,',
            '  COUNT(DISTINCT "PatientID") OVER (PARTITION BY "departement", "Gender") AS per_group,',
            '  COUNT(DISTINCT "PatientID") OVER (PARTITION BY "departement") AS total,',
            "  CAST(per_group AS DOUBLE) / NULLIF(total, 0) * 100 AS percentage_share",
            "FROM (VALUES ('A', 1, 'M'), ('A', 2, 'F'), ('B', 3, 'M'))"
            ' AS t("departement", "PatientID", "Gender");',
        ])
        with tempfile.TemporaryDirectory() as tmp:
            harness = self._setup_workspace(
                tmp,
                denominator_scope="within_department",
                sql=sql,
            )
            result = harness.run()

        # Scope is realized — the denomination check should pass.
        denom_errors = [
            e for e in result.records[0].errors
            if "denominator_scope_not_realized" in e
        ]
        self.assertEqual(
            denom_errors, [],
            f"unexpected denominator_scope errors: {result.records[0].errors}",
        )

    def test_harness_passes_when_no_pipeline_decisions_file(self):
        """Absent pipeline_decisions.json → no denominator check → no extra errors."""
        try:
            import duckdb  # noqa: F401
        except ImportError:
            self.skipTest("duckdb is not installed")

        sql = "\n".join([
            'CREATE OR REPLACE VIEW "kpi_002_results" AS',
            "SELECT DISTINCT",
            '  "departement" AS departement,',
            '  COUNT(DISTINCT "PatientID") OVER () AS total',
            "FROM (VALUES ('A', 1), ('B', 2))"
            ' AS t("departement", "PatientID");',
        ])
        with tempfile.TemporaryDirectory() as tmp:
            harness = self._setup_workspace(
                tmp,
                denominator_scope=None,  # no decisions file written
                sql=sql,
            )
            result = harness.run()

        denom_errors = [
            e for e in result.records[0].errors
            if "denominator_scope_not_realized" in e
        ]
        self.assertEqual(denom_errors, [])


if __name__ == "__main__":
    unittest.main()
