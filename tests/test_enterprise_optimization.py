import json
import os
import tempfile
import unittest
from pathlib import Path

from core.execution.backend import normalize_command
from core.execution.backend import DuckDBBackend, StrictWarehouseBackend, build_execution_backend
from core.execution.databricks_client import DatabricksClient
from core.config import Config, DatabricksConfig
from core.governance.contracts import OptimizationPolicy
from core.governance.evaluator import GovernanceEvaluator
from core.governance.mode_policy import ModePlanner
from core.governance.semantic_contract import SemanticContract
from core.onboarding.databricks_assets import DatabricksAssetManifestBuilder
from core.onboarding.databricks_workspace_deployer import run_deployment
from core.onboarding.derivation_search import DerivationPatternSearcher, DerivationSearchInput
from core.onboarding.genie_workspace import GenieWorkspaceSpecBuilder
from core.onboarding.auto_bootstrap import AutoBootstrap
from core.onboarding.kpi_feature_resolver import (
    KPIFeatureResolver,
    apply_user_decision,
    apply_workspace_definition,
    extract_expression,
)
from core.onboarding.kpi_blocker_workflow import apply_kpi_panel_answer, prepare_kpi_blocker_panel
from core.onboarding.kpi_generation_workflow import KPIGenerationWorkflow
from core.onboarding.data_model_generation_workflow import DataModelGenerationWorkflow
from core.onboarding.relationship_contracts import RelationshipContractBuilder
from core.onboarding.source_to_target_planner import SourceToTargetPlanner
from core.onboarding.kpi_sql_generator import DuckDBKPISQLGenerator
from core.onboarding.kickstart_workspace import WorkspaceKickstarter
from core.onboarding.derived_feature_markdown import DerivedFeatureMarkdownConverter
from core.onboarding.blocker_question_panel import BlockerQuestionPanelBuilder
from core.onboarding.workspace_artifact_validator import WorkspaceArtifactValidator
from core.onboarding.workspace_cleanup import run_cleanup
from core.skills.adapter_generator import SkillAdapterGenerator
from tools.list_workspace_files import list_workspace_files
from core.onboarding.workspace_onboarding import (
    WorkspaceOnboarder,
    find_root_artifact_violations,
)
from core.optimization.change_classifier import classify_diff
from core.optimization.memory import OptimizationMemory, OptimizationMemoryRecord
from core.optimization.planner import OptimizationPlanner
from core.profiling.data_model_profiler import DataModelProfiler
from core.storage.metadata_store import (
    DeltaMetadataStore,
    LocalMetadataStore,
    MongoMetadataStore,
    build_metadata_store,
)
from core.storage.workspace import Workspace
from core.storage.workspace_layout import WorkspaceLayout


class EnterpriseOptimizationTests(unittest.TestCase):
    def _create_demo_workspace(self, root: Path) -> Path:
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
            "What is paid amount by line of business?,Baseline KPI,LineOfBusiness,sum(PaidAmount),Confirm paid amount source\n",
            encoding="utf-8",
        )
        (workspace / "docs" / "data_model.md").write_text(
            "# Data Model\n\ntransactions has ClaimID, PaidAmount, LineOfBusiness.\n",
            encoding="utf-8",
        )
        return workspace

    def test_command_normalization_accepts_legacy_strings(self):
        self.assertEqual(
            normalize_command("uv run python workspaces/demo/interns/evaluation/experiment.py"),
            ["uv", "run", "python", "workspaces/demo/interns/evaluation/experiment.py"],
        )
        self.assertEqual(normalize_command(["uv", "run"]), ["uv", "run"])

    def test_skill_adapter_generator_writes_tool_agnostic_indexes(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            skill_dir = root / "skills" / "demo-skill"
            skill_dir.mkdir(parents=True)
            skill_dir.joinpath("SKILL.md").write_text(
                "---\n"
                "name: demo-skill\n"
                "description: >\n"
                "  Use when testing skill adapter routing.\n"
                "---\n"
                "\n"
                "# Demo Skill\n"
                "\n"
                "Follow the demo procedure.\n",
                encoding="utf-8",
            )

            result = SkillAdapterGenerator(root, tools=("generic", "claude", "gemini")).run()

            self.assertEqual(result.skill_count, 1)
            self.assertFalse(result.embedded_full)
            index = json.loads((root / result.index_path).read_text(encoding="utf-8"))
            self.assertEqual(index["skills"][0]["name"], "demo-skill")
            self.assertNotIn("body", index["skills"][0])
            self.assertEqual(len(result.tool_paths), 3)
            adapter = (root / ".agents" / "claude" / "SKILLS.md").read_text(encoding="utf-8")
            self.assertIn("skills/*/SKILL.md", adapter)
            self.assertIn("demo-skill", adapter)
            self.assertIn("Use when testing skill adapter routing.", adapter)

    def test_skill_adapter_generator_can_embed_full_skill_body(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            skill_dir = root / "skills" / "demo-skill"
            skill_dir.mkdir(parents=True)
            skill_dir.joinpath("SKILL.md").write_text(
                "---\n"
                "name: demo-skill\n"
                "description: Use when full embedding is needed.\n"
                "---\n"
                "\n"
                "# Demo Skill\n"
                "\n"
                "Full body marker.\n",
                encoding="utf-8",
            )

            result = SkillAdapterGenerator(root, tools=("generic",), embed_full=True).run()

            index = json.loads((root / result.index_path).read_text(encoding="utf-8"))
            self.assertTrue(result.embedded_full)
            self.assertIn("Full body marker.", index["skills"][0]["body"])
            adapter = (root / ".agents" / "generic" / "SKILLS.md").read_text(encoding="utf-8")
            self.assertIn("<skill-body>", adapter)
            self.assertIn("Full body marker.", adapter)

    def test_change_classifier_labels_sql_diff(self):
        diff = """
diff --git a/model.sql b/model.sql
+WHERE Tot_Clms > 0
-JOIN old_table o ON x.id = o.id
+JOIN new_table n ON x.id = n.id
"""
        result = classify_diff(diff, "model.sql")
        self.assertEqual(result.primary_type, "predicate_pushdown")
        self.assertIn("join_rewrite", result.change_types)

    def test_semantic_contract_reads_registry_and_methodology(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            registry = root / "registry.md"
            registry.write_text(
                """
| KPI Name | Definition |
| :--- | :--- |
| **Cost Per Claim** | `Tot_Drug_Cst / Tot_Clms` |

## Transformation Guardrails
1. **NPI Uniqueness**: one row per NPI.
""",
                encoding="utf-8",
            )
            methodology = root / "methodology.json"
            methodology.write_text(
                json.dumps({
                    "columns": {
                        "Prscrbr_NPI": {
                            "expected_type": "string",
                            "is_primary_key": True,
                        }
                    },
                    "relationships": [],
                }),
                encoding="utf-8",
            )
            contract = SemanticContract.from_task(
                {
                    "id": "task",
                    "semantic_contract": {
                        "kpi_registry": "registry.md",
                        "methodology_json": "methodology.json",
                    },
                },
                root,
            )
            summary = contract.summary()
            self.assertEqual(summary["kpi_count"], 1)
            self.assertGreaterEqual(summary["rule_count"], 2)

    def test_memory_records_pattern_stats(self):
        workspace = Workspace(":memory:")
        memory = OptimizationMemory(workspace)
        memory.record(
            OptimizationMemoryRecord(
                run_id="exp_1",
                task_id="task",
                artifact="model.sql",
                change_type="predicate_pushdown",
                change_types=["predicate_pushdown"],
                expected_reason="reduce rows",
                actual_result="improved",
                decision="keep",
                baseline_metric=1.0,
                candidate_metric=2.0,
                metric_delta=1.0,
                direction="higher",
                correctness_passed=True,
            )
        )
        stats = memory.pattern_stats()
        self.assertEqual(stats[0]["change_type"], "predicate_pushdown")
        self.assertEqual(stats[0]["success_rate"], 1.0)

    def test_planner_uses_hotspot_suggestions(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            hotspot_path = root / "hotspots.json"
            hotspot_path.write_text(
                json.dumps({
                    "suggestions": [
                        {"type": "repeated_scan", "message": "table scanned repeatedly"}
                    ]
                }),
                encoding="utf-8",
            )
            workspace = Workspace(":memory:")
            planner = OptimizationPlanner(OptimizationMemory(workspace), root)
            plan = planner.build_plan(
                {"id": "task", "hotspots_file": "hotspots.json"},
                SemanticContract(name="task"),
            )
            self.assertEqual(plan.recommended_strategy, "cte_rewrite")

    def test_mode_policy_blocks_global_promotion_by_default(self):
        policy = OptimizationPolicy.from_task({})
        plan = ModePlanner(policy).build_plan("global_exploration")
        self.assertEqual(plan.active_mode, "global_exploration")
        self.assertFalse(plan.promotion_allowed)
        self.assertIn("global_exploration_no_auto_promotion", plan.warnings)

    def test_mode_policy_supports_explicit_pyspark_mode(self):
        policy = OptimizationPolicy.from_task({})
        plan = ModePlanner(policy).build_plan("pyspark")
        self.assertEqual(plan.active_mode, "pyspark")
        self.assertIn("pyspark", plan.candidate_modes)
        self.assertTrue(plan.promotion_allowed)

    def test_governance_records_review_decision_and_alert(self):
        policy = OptimizationPolicy.from_task({})
        mode_plan = ModePlanner(policy).build_plan("sql")
        classification = classify_diff("+WHERE amount > 0", "model.sql")
        contract = SemanticContract(name="task")
        decision = GovernanceEvaluator(policy).evaluate(
            run_id="exp_1",
            task={"id": "task", "editable_file": "model.sql", "direction": "higher"},
            status="keep",
            baseline_metric=1.0,
            candidate_metric=2.0,
            metric_delta=1.0,
            execution_time_seconds=1.5,
            matching_score=100.0,
            classification=classification,
            semantic_contract=contract,
            mode_plan=mode_plan,
            artifact_diff="+WHERE amount > 0",
        )
        self.assertEqual(decision.decision, "needs_review")
        self.assertEqual(decision.approval_state, "pending_human")
        self.assertTrue(decision.alerts)

        workspace = Workspace(":memory:")
        workspace.log_governance_decision(decision.summary())
        stored = workspace.get_recent_governance_decisions()
        alerts = workspace.get_recent_alerts()
        self.assertEqual(stored[0]["decision"], "needs_review")
        self.assertEqual(alerts[0]["severity"], "review")

    def test_data_model_profiler_records_exact_bounds_and_downcast(self):
        try:
            import polars as pl
        except ImportError:
            self.skipTest("polars is not installed")

        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "data.parquet"
            pl.DataFrame({
                "small_id": [1, 2, 3],
                "amount": [1.1, 2.2, 3.3],
            }).write_parquet(path)
            profile = DataModelProfiler().profile_path(path, exact=True)
            by_name = {col.name: col for col in profile.columns}
            self.assertEqual(by_name["small_id"].exact_min, 1)
            self.assertEqual(by_name["small_id"].exact_max, 3)
            recs = {rec.column: rec for rec in profile.downcast_recommendations}
            self.assertEqual(recs["small_id"].decision, "recommend")
            self.assertEqual(recs["amount"].decision, "approval_required")

    def test_workspace_layout_groups_outputs_under_interns(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            layout = WorkspaceLayout.from_task(
                {
                    "editable_file": "workspaces/demo_project/src/model.sql",
                },
                root,
            )
            self.assertEqual(layout.project_root, root / "workspaces" / "demo_project")
            self.assertEqual(layout.interns_dir, layout.project_root / "interns")
            self.assertEqual(layout.workspace_db, layout.interns_dir / "state" / "workspace.db")
            self.assertEqual(layout.run_log, layout.interns_dir / "state" / "run.log")

            explicit = WorkspaceLayout.from_task(
                {
                    "workspace": "workspaces/explicit_project",
                    "editable_file": "somewhere/else/model.sql",
                },
                root,
            )
            self.assertEqual(explicit.project_root, root / "workspaces" / "explicit_project")
            explicit.ensure_runtime_dirs()
            self.assertTrue(explicit.solutions_dir.exists())
            self.assertTrue(explicit.requirements_dir.exists())
            self.assertTrue(explicit.memory_dir.exists())
            self.assertTrue(explicit.evaluation_dir.exists())

    def test_workspace_onboarding_generates_fresh_workspace_artifacts(self):
        try:
            import polars  # noqa: F401
        except ImportError:
            self.skipTest("polars is not installed")

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            workspace = self._create_demo_workspace(root)

            result = WorkspaceOnboarder(root, "workspaces/demo", sample_rows=10).run()

            self.assertEqual(result.kpi_count, 1)
            self.assertEqual(result.profile_count, 1)
            self.assertTrue((workspace / "interns" / "generated" / "solutions" / "kpi_metrics.sql").exists())
            self.assertTrue((workspace / "interns" / "evaluation" / "experiment.py").exists())
            self.assertTrue((workspace / "interns" / "evaluation" / "evaluator.py").exists())
            self.assertTrue((workspace / "interns" / "generated" / "contracts" / "semantic_contract.json").exists())
            self.assertTrue((workspace / "interns" / "generated" / "profiles" / "profile_index.json").exists())
            self.assertTrue((workspace / "interns" / "state" / "delta_metadata" / "contracts" / "_delta_log").exists())
            self.assertTrue((workspace / "interns" / "state" / "delta_metadata" / "profiles" / "_delta_log").exists())
            self.assertTrue((workspace / "interns" / "reports" / "open_questions.md").exists())
            self.assertEqual(find_root_artifact_violations(workspace), [])

            baseline_sql = (
                workspace / "interns" / "generated" / "solutions" / "kpi_metrics.sql"
            ).read_text(encoding="utf-8")
            self.assertIn("kpi_baseline_manifest", baseline_sql)
            self.assertIn("What is paid amount by line of business?", baseline_sql)

    def test_workspace_onboarding_infers_question_only_kpi_rows(self):
        try:
            import polars  # noqa: F401
        except ImportError:
            self.skipTest("polars is not installed")

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            workspace = root / "workspaces" / "demo"
            (workspace / "datasets").mkdir(parents=True)
            (workspace / "docs").mkdir(parents=True)
            (workspace / "datasets" / "transactions.csv").write_text(
                "ClaimID,PaidAmount,LineOfBusiness,PayorID,PatientID,VisitType,DepartmentID\n"
                "C1,10.50,Commercial,P1,PT1,Office,D1\n"
                "C2,20.25,Medicare,P2,PT2,Inpatient,D2\n",
                encoding="utf-8",
            )
            (workspace / "docs" / "kpi_registry.csv").write_text(
                "Key business question,Description,Unused,Unused2\n"
                "Which key business question is the KPI/metric/Feature meant to answer,Short description,,\n"
                "\"What is trend for amount paid for medicare LOB across gender and payer for patients above 50 years of age\",Paid amount across LOB Gender Payer Age,,\n"
                "\"What is percentage share of lives by gender, age, visit type, department\",Percentage of lives count,,\n"
                "\"What are top 10 payers in Commercial LOB w.r.t. amount paid\",Top 10 Payers for LOB w.r.t. Amount Paid,,\n",
                encoding="utf-8",
            )
            (workspace / "docs" / "data_model.md").write_text("# Data Model\n", encoding="utf-8")

            result = WorkspaceOnboarder(root, "workspaces/demo", sample_rows=10).run()

            self.assertEqual(result.kpi_count, 3)
            registry = json.loads(
                (workspace / "interns" / "generated" / "contracts" / "kpi_registry.json")
                .read_text(encoding="utf-8")
            )
            self.assertEqual(registry["generated_by"], "onboard-workspace")
            self.assertNotIn("Which key business question", registry["kpis"][0]["name"])
            self.assertEqual(registry["kpis"][0]["metric"], "amount paid")
            self.assertIn("LOB = Medicare", registry["kpis"][0]["cuts"])
            self.assertEqual(registry["kpis"][1]["metric"], "percentage share of lives")
            self.assertIn("VisitType", registry["kpis"][1]["cuts"])

    def test_list_workspace_files_reports_all_paths_without_generating_artifacts(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            workspace = self._create_demo_workspace(root)

            listing = list_workspace_files(root, "demo")

            self.assertEqual(listing.workspace, "workspaces/demo")
            self.assertTrue(listing.exists)
            self.assertEqual(listing.interns_state, "missing")
            self.assertIn("workspaces/demo/docs/kpi_registry.csv", listing.files)
            self.assertIn("workspaces/demo/docs/data_model.md", listing.files)
            self.assertIn("workspaces/demo/datasets/transactions.csv", listing.files)
            self.assertIn("workspaces/demo/docs/kpi_registry.csv", listing.possible_kpi_files)
            self.assertIn(
                "workspaces/demo/docs/data_model.md",
                listing.possible_data_model_files,
            )
            self.assertEqual(listing.dataset_roots, ["workspaces/demo/datasets"])
            self.assertFalse((workspace / "interns").exists())

    def test_workspace_root_artifact_policy_flags_generated_files(self):
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp) / "workspaces" / "demo"
            workspace.mkdir(parents=True)
            (workspace / "kpi_metrics.sql").write_text("select 1;", encoding="utf-8")
            (workspace / "analytics.duckdb").write_text("", encoding="utf-8")
            violations = find_root_artifact_violations(workspace)
            self.assertEqual(len(violations), 2)
            self.assertTrue(any(path.endswith("kpi_metrics.sql") for path in violations))
            self.assertTrue(any(path.endswith("analytics.duckdb") for path in violations))

    def test_auto_bootstrap_generates_then_reuses_current_artifacts(self):
        try:
            import polars  # noqa: F401
        except ImportError:
            self.skipTest("polars is not installed")

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            workspace = self._create_demo_workspace(root)
            task = {"id": "demo", "workspace": "workspaces/demo"}

            first = AutoBootstrap(root, task).ensure_ready()
            second = AutoBootstrap(root, task).ensure_ready()

            self.assertEqual(first.action, "generated")
            self.assertEqual(second.action, "reuse")
            self.assertEqual(second.reason, "artifacts_current")
            self.assertTrue((workspace / "interns" / "state" / "bootstrap_manifest.json").exists())

    def test_auto_bootstrap_regenerates_when_input_fingerprint_changes(self):
        try:
            import polars  # noqa: F401
        except ImportError:
            self.skipTest("polars is not installed")

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            workspace = self._create_demo_workspace(root)
            task = {"id": "demo", "workspace": "workspaces/demo"}

            first = AutoBootstrap(root, task).ensure_ready()
            (workspace / "docs" / "kpi_registry.csv").write_text(
                "Key business question,Description,Cuts / grain hints,Metric,Data model refinement required\n"
                "What is paid amount by line of business?,Baseline KPI,LineOfBusiness,sum(PaidAmount),Confirm paid amount source\n"
                "What is claim count?,Count KPI,LineOfBusiness,count(ClaimID),Confirm claim grain\n",
                encoding="utf-8",
            )
            second = AutoBootstrap(root, task).ensure_ready()

            self.assertEqual(first.action, "generated")
            self.assertEqual(second.action, "generated")
            self.assertEqual(second.reason, "input_fingerprint_changed")
            contract = json.loads(
                (workspace / "interns" / "generated" / "contracts" / "semantic_contract.json")
                .read_text(encoding="utf-8")
            )
            self.assertEqual(contract["kpi_count"], 2)

    def test_kpi_expression_extraction_ignores_literals_and_functions(self):
        extracted = extract_expression(
            "sum(case when LineOfBusiness = 'Medicare' and age(DOB, ServiceDate) > 50 "
            "then coalesce(PaidAmount,0) else 0 end)"
        )
        self.assertEqual(
            extracted.identifiers,
            ["LineOfBusiness", "DOB", "ServiceDate", "PaidAmount"],
        )
        functions = {item["function"] for item in extracted.functions}
        self.assertIn("age", functions)
        self.assertIn("coalesce", functions)

    def test_derivation_search_returns_candidate_patterns_without_proof(self):
        searcher = DerivationPatternSearcher()
        results = searcher.search(
            DerivationSearchInput(
                feature="age",
                available_columns=["DOB", "ServiceDate", "PatientID"],
                expression_context="age(DOB, ServiceDate) > 50",
                domain="healthcare",
            )
        )
        self.assertEqual(results[0].pattern_id, "age_years")
        self.assertEqual(results[0].state, "candidate_pattern")
        self.assertEqual(
            results[0].candidate_bindings["birth_date"],
            ["DOB"],
        )

    def test_derivation_search_does_not_offer_patterns_for_unrelated_features(self):
        searcher = DerivationPatternSearcher()
        results = searcher.search(
            DerivationSearchInput(
                feature="FacilityID",
                available_columns=[
                    "DOB",
                    "ServiceDate",
                    "ClaimDate",
                    "PaidDate",
                    "ProcedureCode",
                    "ProviderID",
                    "Specialization",
                ],
                expression_context="count(distinct PatientID) by FacilityID",
                domain="healthcare",
            )
        )
        self.assertEqual(results, [])

    def test_derivation_search_does_not_use_age_years_as_age_band_output(self):
        searcher = DerivationPatternSearcher()
        results = searcher.search(
            DerivationSearchInput(
                feature="AgeBand",
                available_columns=["DOB", "ServiceDate"],
                expression_context="group by AgeBand",
                domain="healthcare",
            )
        )
        self.assertEqual([result.pattern_id for result in results], ["age_band"])

    def test_kpi_feature_resolver_writes_mapping_and_blockers(self):
        try:
            import polars  # noqa: F401
        except ImportError:
            self.skipTest("polars is not installed")

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            workspace = self._create_demo_workspace(root)
            WorkspaceOnboarder(root, "workspaces/demo", sample_rows=10).run()

            result = KPIFeatureResolver(
                root,
                "workspaces/demo",
                domain="healthcare",
                include_candidates=True,
            ).run()

            self.assertEqual(result.kpi_count, 1)
            self.assertTrue((root / result.question_panel_path).exists())
            self.assertTrue((root / result.question_panel_markdown_path).exists())
            self.assertIn("question_panel", result.summary()["question_panel_path"])
            self.assertIn("next_step", result.summary())
            mapping = json.loads(
                (workspace / "interns" / "generated" / "contracts" / "kpi_feature_mapping.json")
                .read_text(encoding="utf-8")
            )
            self.assertEqual(mapping["mode"], "candidate_enriched")
            kpi = mapping["kpis"][0]
            by_feature = {feature["feature"]: feature for feature in kpi["features"]}
            self.assertEqual(by_feature["PaidAmount"]["state"], "proven_direct")
            self.assertEqual(by_feature["LineOfBusiness"]["state"], "proven_direct")
            self.assertGreaterEqual(mapping["summary"]["ready_kpi_count"], 1)
            self.assertTrue((workspace / "interns" / "reports" / "open_questions.md").exists())

    def test_kpi_feature_resolver_writes_strict_derived_feature_options(self):
        try:
            import polars  # noqa: F401
        except ImportError:
            self.skipTest("polars is not installed")

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            workspace = self._create_demo_workspace(root)
            (workspace / "datasets" / "transactions.csv").write_text(
                "ClaimID,DOB,ServiceDate,PaidAmount\n"
                "C1,1980-01-01,2024-01-01,10.50\n"
                "C2,1990-01-01,2024-02-01,20.25\n",
                encoding="utf-8",
            )
            (workspace / "docs" / "kpi_registry.csv").write_text(
                "Key business question,Description,Cuts / grain hints,Metric,Data model refinement required\n"
                "What is average patient age?,Needs derived evidence,,avg(Age),Confirm age anchor\n",
                encoding="utf-8",
            )
            WorkspaceOnboarder(root, "workspaces/demo", sample_rows=10).run()
            KPIFeatureResolver(root, "workspaces/demo", domain="healthcare", include_candidates=True).run()

            mapping = json.loads(
                (workspace / "interns" / "generated" / "contracts" / "kpi_feature_mapping.json")
                .read_text(encoding="utf-8")
            )
            self.assertEqual(mapping["derived_feature_evidence_contract"]["version"], 1)
            feature = mapping["kpis"][0]["features"][0]
            option = feature["derived_feature_options"][0]
            self.assertEqual(option["derived_column_name"], "Age")
            self.assertEqual(option["source_pattern_id"], "age_years")
            self.assertEqual(option["evidence_state"], "candidate_derivation_not_ground_truth")
            self.assertTrue(option["needs_user_confirmation"])
            self.assertIn("date_diff", option["formula"])
            self.assertEqual(
                {column["input_name"] for column in option["input_columns"]},
                {"birth_date", "anchor_date"},
            )
            by_input = {column["input_name"]: column for column in option["input_columns"]}
            self.assertIn("1980-01-01", by_input["birth_date"]["observed_values"])
            self.assertIn("2024-01-01", by_input["anchor_date"]["observed_values"])
            self.assertEqual(
                by_input["birth_date"]["value_profile"]["note"],
                "Values come from bounded profile evidence, not a full raw-data dump.",
            )
            self.assertIn("semantic_meaning_sources", by_input["birth_date"])
            self.assertIn("reason", by_input["birth_date"])
            self.assertIn("derivation_reasoning", option)
            self.assertIn("why_this_formula", option["derivation_reasoning"])
            self.assertIn("why_not_ground_truth", option["derivation_reasoning"])
            self.assertIn("remaining_risk", option["derivation_reasoning"])
            self.assertEqual(option["example"]["output"], {"Age": 44})
            self.assertTrue(any(source["evidence_type"] == "profile_schema" for source in option["evidence_sources"]))

    def test_derived_feature_markdown_converter_strictly_validates_and_writes_kpi_file(self):
        try:
            import polars  # noqa: F401
        except ImportError:
            self.skipTest("polars is not installed")

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            workspace = self._create_demo_workspace(root)
            (workspace / "datasets" / "transactions.csv").write_text(
                "ClaimID,DOB,ServiceDate,PaidAmount\n"
                "C1,1980-01-01,2024-01-01,10.50\n",
                encoding="utf-8",
            )
            (workspace / "docs" / "kpi_registry.csv").write_text(
                "Key business question,Description,Cuts / grain hints,Metric,Data model refinement required\n"
                "What is average patient age?,Needs derived evidence,,avg(Age),Confirm age anchor\n",
                encoding="utf-8",
            )
            WorkspaceOnboarder(root, "workspaces/demo", sample_rows=10).run()
            KPIFeatureResolver(root, "workspaces/demo", domain="healthcare", include_candidates=True).run()
            stale_md = (
                workspace
                / "interns"
                / "reports"
                / "derived_feature_reviews"
                / "md"
                / "kpi_001_facilityid.md"
            )
            stale_json = (
                workspace
                / "interns"
                / "reports"
                / "derived_feature_reviews"
                / "json"
                / "kpi_001_facilityid.json"
            )
            stale_md.parent.mkdir(parents=True, exist_ok=True)
            stale_json.parent.mkdir(parents=True, exist_ok=True)
            stale_md.write_text("# Old bad option\n", encoding="utf-8")
            stale_json.write_text('{"old": true}\n', encoding="utf-8")

            result = DerivedFeatureMarkdownConverter(root, "workspaces/demo").run()

            self.assertGreaterEqual(result.option_count, 1)
            review_path = next(
                root / file
                for file in result.markdown_files
                if Path(file).name.startswith("kpi_001_age")
            )
            self.assertEqual(review_path.name, "kpi_001_age.md")
            self.assertEqual(review_path.parent.name, "md")
            review = review_path.read_text(encoding="utf-8")
            self.assertIn("# Derived Feature Review: Age", review)
            self.assertIn("### Columns Used", review)
            self.assertIn("1980-01-01", review)
            self.assertIn("Evidence state: `candidate_derivation_not_ground_truth`", review)
            json_path = review_path.parent.parent / "json" / review_path.with_suffix(".json").name
            self.assertEqual(json_path.parent.name, "json")
            self.assertIn(str(json_path.relative_to(root)).replace("\\", "/"), result.json_files)
            review_json = json.loads(json_path.read_text(encoding="utf-8"))
            self.assertEqual(review_json["kpi_id"], "kpi_001")
            self.assertEqual(review_json["derived_feature_options"][0]["derived_column_name"], "Age")
            self.assertEqual(result.option_count, 1)
            index = (review_path.parent.parent / "index.md").read_text(encoding="utf-8")
            self.assertIn("md/", index)
            self.assertIn("json/", index)
            self.assertIn(str(stale_md.relative_to(root)).replace("\\", "/"), result.stale_files)
            self.assertIn("stale", stale_md.read_text(encoding="utf-8").lower())
            self.assertTrue(json.loads(stale_json.read_text(encoding="utf-8"))["stale"])

            mapping_path = (
                workspace / "interns" / "generated" / "contracts" / "kpi_feature_mapping.json"
            )
            mapping = json.loads(mapping_path.read_text(encoding="utf-8"))
            del mapping["kpis"][0]["features"][0]["derived_feature_options"][0]["example"]
            mapping_path.write_text(json.dumps(mapping), encoding="utf-8")

            with self.assertRaisesRegex(ValueError, "missing required fields"):
                DerivedFeatureMarkdownConverter(root, "workspaces/demo").run()

    def test_blocker_question_panel_writes_current_json_and_markdown(self):
        try:
            import polars  # noqa: F401
        except ImportError:
            self.skipTest("polars is not installed")

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            workspace = self._create_demo_workspace(root)
            (workspace / "datasets" / "transactions.csv").write_text(
                "ClaimID,DOB,ServiceDate,PaidAmount\n"
                "C1,1980-01-01,2024-01-01,10.50\n",
                encoding="utf-8",
            )
            (workspace / "docs" / "kpi_registry.csv").write_text(
                "Key business question,Description,Cuts / grain hints,Metric,Data model refinement required\n"
                "What is average patient age?,Needs derived evidence,,avg(Age),Confirm age anchor\n",
                encoding="utf-8",
            )
            WorkspaceOnboarder(root, "workspaces/demo", sample_rows=10).run()
            KPIFeatureResolver(root, "workspaces/demo", domain="healthcare", include_candidates=True).run()

            result = BlockerQuestionPanelBuilder(root, "workspaces/demo").run()

            self.assertEqual(result.current_feature, "Age")
            current = json.loads((root / result.current_json).read_text(encoding="utf-8"))
            self.assertEqual(current["answer_type"], "select_json_backed_option_or_custom_rule")
            self.assertEqual(current["options"][0]["json_backed"], True)
            self.assertIn("derived_feature_option", current["options"][0])
            self.assertIn("derivation_reasoning", current["options"][0]["derived_feature_option"])
            markdown = (root / result.current_markdown).read_text(encoding="utf-8")
            self.assertIn("# Blocker Question Panel: Age", markdown)
            self.assertIn("```json", markdown)

    def test_blocker_question_panel_includes_physical_column_options_for_aliases(self):
        try:
            import polars  # noqa: F401
        except ImportError:
            self.skipTest("polars is not installed")

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            workspace = root / "workspaces" / "demo"
            (workspace / "datasets").mkdir(parents=True)
            (workspace / "docs").mkdir(parents=True)
            (workspace / "datasets" / "transactions.csv").write_text(
                "ClaimID,PaidAmount,LineOfBusiness,PayorID\n"
                "C1,10.50,Commercial,P1\n"
                "C2,20.25,Medicare,P2\n",
                encoding="utf-8",
            )
            (workspace / "docs" / "kpi_registry.csv").write_text(
                "Key business question,Description,Cuts / grain hints,Metric,Data model refinement required\n"
                "What is paid?,Alias KPI,,paid,\n",
                encoding="utf-8",
            )
            (workspace / "docs" / "data_model.md").write_text("# Data Model\n", encoding="utf-8")
            WorkspaceOnboarder(root, "workspaces/demo", sample_rows=10).run()
            result = KPIFeatureResolver(root, "workspaces/demo", domain="healthcare").run()

            current = json.loads((root / result.question_panel_path).read_text(encoding="utf-8"))
            self.assertEqual(current["answer_type"], "select_physical_column_or_custom_rule")
            self.assertTrue(any(option.get("physical_column_option") for option in current["options"]))
            paid_option = next(
                option["physical_column_option"]
                for option in current["options"]
                if "PaidAmount" in option["label"]
            )
            self.assertIn(10.5, paid_option["observed_values"])
            self.assertIn("value_profile", paid_option)
            self.assertIn("semantic_meaning_sources", paid_option)
            self.assertIn("profile_path", paid_option)
            labels = [option["label"] for option in current["options"]]
            self.assertTrue(any("PaidAmount" in label for label in labels))
            markdown = (root / result.question_panel_markdown_path).read_text(encoding="utf-8")
            self.assertIn("physical column candidates", markdown)

    def test_prepare_kpi_blocker_panel_blocks_parser_artifacts_and_validates(self):
        try:
            import polars  # noqa: F401
        except ImportError:
            self.skipTest("polars is not installed")

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            workspace = root / "workspaces" / "demo"
            (workspace / "datasets").mkdir(parents=True)
            (workspace / "docs").mkdir(parents=True)
            (workspace / "datasets" / "transactions.csv").write_text(
                "ClaimID,PaidAmount,LineOfBusiness,PayorID,PatientID,VisitType,DepartmentID\n"
                "C1,10.50,Commercial,P1,PT1,Office,D1\n"
                "C2,20.25,Medicare,P2,PT2,Inpatient,D2\n",
                encoding="utf-8",
            )
            (workspace / "docs" / "kpi_registry.csv").write_text(
                "Key business question,Description,Unused,Unused2\n"
                "\"What is percentage share of lives by gender, age, visit type, department\",Percentage of lives count,,\n"
                "\"What are top 10 payers in Commercial LOB w.r.t. amount paid\",Top 10 Payers for LOB w.r.t. Amount Paid,,\n",
                encoding="utf-8",
            )
            (workspace / "docs" / "data_model.md").write_text("# Data Model\n", encoding="utf-8")

            result = prepare_kpi_blocker_panel(root, "workspaces/demo", domain="healthcare")

            self.assertTrue(result.validation["ok"], result.validation)
            mapping = json.loads(
                (workspace / "interns" / "generated" / "contracts" / "kpi_feature_mapping.json")
                .read_text(encoding="utf-8")
            )
            features = {
                feature["feature"]
                for kpi in mapping["kpis"]
                for feature in kpi["features"]
            }
            self.assertNotIn("of", features)
            self.assertNotIn("percentage", features)
            self.assertNotIn("Commercial", features)
            self.assertNotIn("Medicare", features)

    def test_apply_kpi_panel_answer_resolves_friendly_option_and_reprepares(self):
        try:
            import polars  # noqa: F401
        except ImportError:
            self.skipTest("polars is not installed")

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            workspace = root / "workspaces" / "demo"
            (workspace / "datasets").mkdir(parents=True)
            (workspace / "docs").mkdir(parents=True)
            (workspace / "datasets" / "transactions.csv").write_text(
                "ClaimID,PaidAmount,LineOfBusiness,PayorID\n"
                "C1,10.50,Commercial,P1\n"
                "C2,20.25,Medicare,P2\n",
                encoding="utf-8",
            )
            (workspace / "docs" / "kpi_registry.csv").write_text(
                "Key business question,Description,Cuts / grain hints,Metric,Data model refinement required\n"
                "What is paid?,Alias KPI,,paid,\n",
                encoding="utf-8",
            )
            (workspace / "docs" / "data_model.md").write_text("# Data Model\n", encoding="utf-8")
            prepared = prepare_kpi_blocker_panel(root, "workspaces/demo", domain="healthcare")
            current = json.loads((root / prepared.question_panel_path).read_text(encoding="utf-8"))
            self.assertEqual(current["feature"], "paid")

            result = apply_kpi_panel_answer(
                root,
                "workspaces/demo",
                answer="Option A: PaidAmount",
                domain="healthcare",
            )

            self.assertEqual(result.feature, "paid")
            definitions = json.loads(
                (workspace / "interns" / "generated" / "contracts" / "workspace_feature_definitions.json")
                .read_text(encoding="utf-8")
            )
            self.assertEqual(definitions["definitions"][0]["feature"], "paid")
            self.assertEqual(definitions["definitions"][0]["resolution_type"], "physical_column")
            self.assertTrue(result.prepared_panel["validation"]["ok"])

    def test_workspace_artifact_validator_checks_generated_contract_shapes(self):
        try:
            import polars  # noqa: F401
        except ImportError:
            self.skipTest("polars is not installed")

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            workspace = root / "workspaces" / "demo"
            (workspace / "datasets").mkdir(parents=True)
            (workspace / "docs").mkdir(parents=True)
            (workspace / "datasets" / "transactions.csv").write_text(
                "ClaimID,DOB,ServiceDate,PaidAmount\n"
                "C1,1980-01-01,2024-01-01,10.50\n",
                encoding="utf-8",
            )
            (workspace / "docs" / "kpi_registry.csv").write_text(
                "Key business question,Description,Cuts / grain hints,Metric,Data model refinement required\n"
                "What is average patient age?,Needs derived evidence,,avg(Age),Confirm age anchor\n",
                encoding="utf-8",
            )
            (workspace / "docs" / "data_model.md").write_text("# Data Model\n", encoding="utf-8")

            WorkspaceOnboarder(root, "workspaces/demo", sample_rows=10).run()
            KPIFeatureResolver(root, "workspaces/demo", domain="healthcare", include_candidates=True).run()
            DerivedFeatureMarkdownConverter(root, "workspaces/demo").run()

            result = WorkspaceArtifactValidator(root, "workspaces/demo").run()

            self.assertTrue(result.ok, result.summary())
            self.assertTrue(any(path.endswith("current.json") for path in result.checked_files))
            self.assertTrue(any(path.endswith("kpi_feature_mapping.json") for path in result.checked_files))

            registry_path = workspace / "interns" / "generated" / "contracts" / "kpi_registry.json"
            registry = json.loads(registry_path.read_text(encoding="utf-8"))
            del registry["generated_by"]
            registry_path.write_text(json.dumps(registry, indent=2), encoding="utf-8")

            invalid = WorkspaceArtifactValidator(root, "workspaces/demo").run()

            self.assertFalse(invalid.ok)
            self.assertTrue(any("generated_by" in error for error in invalid.errors))

            mapping_path = workspace / "interns" / "generated" / "contracts" / "kpi_feature_mapping.json"
            mapping = json.loads(mapping_path.read_text(encoding="utf-8"))
            mapping["version"] = 999
            mapping_path.write_text(json.dumps(mapping, indent=2), encoding="utf-8")

            invalid_version = WorkspaceArtifactValidator(root, "workspaces/demo").run()

            self.assertFalse(invalid_version.ok)
            self.assertTrue(any("version=999 is not supported" in error for error in invalid_version.errors))

    def test_kpi_feature_resolver_proves_structural_aliases_and_prioritizes_blockers(self):
        try:
            import polars  # noqa: F401
        except ImportError:
            self.skipTest("polars is not installed")

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            workspace = self._create_demo_workspace(root)
            (workspace / "datasets" / "transactions.csv").write_text(
                "ClaimID,PaidAmount,LineOfBusiness,DeptID\n"
                "C1,10.50,Commercial,D1\n"
                "C2,20.25,Medicare,D2\n",
                encoding="utf-8",
            )
            (workspace / "docs" / "kpi_registry.csv").write_text(
                "Key business question,Description,Cuts / grain hints,Metric,Data model refinement required\n"
                "Which departments have burden?,Alias KPI,DepartmentID;RefundAmount,sum(ClaimID),Confirm refund source\n",
                encoding="utf-8",
            )
            WorkspaceOnboarder(root, "workspaces/demo", sample_rows=10).run()
            KPIFeatureResolver(root, "workspaces/demo", domain="healthcare").run()

            mapping = json.loads(
                (workspace / "interns" / "generated" / "contracts" / "kpi_feature_mapping.json")
                .read_text(encoding="utf-8")
            )
            by_feature = {
                feature["feature"]: feature
                for feature in mapping["kpis"][0]["features"]
            }
            self.assertEqual(by_feature["DepartmentID"]["state"], "proven_alias")
            self.assertEqual(by_feature["RefundAmount"]["state"], "blocked_missing_evidence")
            self.assertEqual(mapping["blocker_clusters"][0]["feature"], "RefundAmount")
            self.assertEqual(mapping["blocker_clusters"][0]["risk"], "financial_correctness")

    def test_apply_user_decision_updates_mapping_memory_and_requirements(self):
        try:
            import polars  # noqa: F401
        except ImportError:
            self.skipTest("polars is not installed")

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            workspace = self._create_demo_workspace(root)
            (workspace / "docs" / "kpi_registry.csv").write_text(
                "Key business question,Description,Cuts / grain hints,Metric,Data model refinement required\n"
                "What is refund adjusted paid?,Needs decision,RefundAmount,sum(RefundAmount),Confirm refund source\n",
                encoding="utf-8",
            )
            WorkspaceOnboarder(root, "workspaces/demo", sample_rows=10).run()
            KPIFeatureResolver(root, "workspaces/demo", domain="healthcare").run()

            summary = apply_user_decision(
                root,
                "workspaces/demo",
                kpi_id="kpi_001",
                feature="RefundAmount",
                state="user_confirmed",
                resolution_type="user_confirmed_formula",
                evidence_note="User confirmed refunds are excluded for this demo KPI.",
            )

            self.assertEqual(summary["ready_kpi_count"], 1)
            mapping = json.loads(
                (workspace / "interns" / "generated" / "contracts" / "kpi_feature_mapping.json")
                .read_text(encoding="utf-8")
            )
            feature = mapping["kpis"][0]["features"][0]
            self.assertEqual(feature["state"], "user_confirmed")
            self.assertTrue((workspace / "interns" / "generated" / "memory" / "decision_history.md").exists())
            self.assertTrue((workspace / "interns" / "generated" / "requirements" / "requirements.json").exists())

    def test_apply_workspace_definition_updates_all_matching_features_and_survives_rerun(self):
        try:
            import polars  # noqa: F401
        except ImportError:
            self.skipTest("polars is not installed")

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            workspace = self._create_demo_workspace(root)
            (workspace / "docs" / "kpi_registry.csv").write_text(
                "Key business question,Description,Cuts / grain hints,Metric,Data model refinement required\n"
                "What is refund adjusted paid?,Needs decision,RefundAmount,sum(RefundAmount),Confirm refund source\n"
                "Where are refunds trending?,Needs same decision,RefundAmount,sum(RefundAmount),Confirm refund source\n",
                encoding="utf-8",
            )
            WorkspaceOnboarder(root, "workspaces/demo", sample_rows=10).run()
            KPIFeatureResolver(root, "workspaces/demo", domain="healthcare").run()

            summary = apply_workspace_definition(
                root,
                "workspaces/demo",
                feature="RefundAmount",
                state="user_confirmed",
                resolution_type="user_confirmed_formula",
                definition="Refunds are excluded for this demo workspace.",
                evidence_note="User confirmed one reusable refund definition for all KPIs.",
            )

            self.assertEqual(summary["ready_kpi_count"], 2)
            definitions_path = (
                workspace
                / "interns"
                / "generated"
                / "contracts"
                / "workspace_feature_definitions.json"
            )
            self.assertTrue(definitions_path.exists())
            definitions = json.loads(definitions_path.read_text(encoding="utf-8"))
            self.assertEqual(definitions["definitions"][0]["feature"], "RefundAmount")

            rerun = KPIFeatureResolver(root, "workspaces/demo", domain="healthcare").run()
            self.assertEqual(rerun.ready_kpi_count, 2)
            mapping = json.loads(
                (workspace / "interns" / "generated" / "contracts" / "kpi_feature_mapping.json")
                .read_text(encoding="utf-8")
            )
            states = [
                feature["state"]
                for kpi in mapping["kpis"]
                for feature in kpi["features"]
                if feature["feature"] == "RefundAmount"
            ]
            self.assertEqual(states, ["user_confirmed", "user_confirmed"])

    def test_kpi_sql_generator_blocks_unresolved_and_generates_ready_kpi(self):
        try:
            import polars  # noqa: F401
        except ImportError:
            self.skipTest("polars is not installed")

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            workspace = self._create_demo_workspace(root)
            WorkspaceOnboarder(root, "workspaces/demo", sample_rows=10).run()
            KPIFeatureResolver(root, "workspaces/demo", domain="healthcare").run()
            result = DuckDBKPISQLGenerator(root, "workspaces/demo").generate("kpi_001")
            self.assertTrue((root / result.path).exists())
            sql = (root / result.path).read_text(encoding="utf-8")
            self.assertIn("CREATE OR REPLACE VIEW", sql)
            self.assertIn("all_workspace_rows", sql)

            (workspace / "docs" / "kpi_registry.csv").write_text(
                "Key business question,Description,Cuts / grain hints,Metric,Data model refinement required\n"
                "What is blocked?,Blocked,RefundAmount,sum(RefundAmount),Confirm refund source\n",
                encoding="utf-8",
            )
            WorkspaceOnboarder(root, "workspaces/demo", sample_rows=10).run()
            KPIFeatureResolver(root, "workspaces/demo", domain="healthcare").run()
            with self.assertRaises(ValueError):
                DuckDBKPISQLGenerator(root, "workspaces/demo").generate("kpi_001")

    def test_kpi_sql_generator_joins_profiled_sources_instead_of_sparse_union(self):
        try:
            import duckdb
            import polars  # noqa: F401
        except ImportError:
            self.skipTest("duckdb or polars is not installed")

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            workspace = root / "workspaces" / "demo"
            (workspace / "datasets").mkdir(parents=True)
            (workspace / "docs").mkdir(parents=True)
            (workspace / "datasets" / "transactions.csv").write_text(
                "TransactionID,PatientID,PaidAmount,ServiceDate\n"
                "T1,P1,10.50,2024-01-01\n"
                "T2,P2,20.25,2024-01-02\n",
                encoding="utf-8",
            )
            (workspace / "datasets" / "patients.csv").write_text(
                "PatientID,Gender,DOB\n"
                "P1,F,1980-01-01\n"
                "P2,M,1990-01-01\n",
                encoding="utf-8",
            )
            (workspace / "docs" / "kpi_registry.csv").write_text(
                "Key business question,Description,Cuts / grain hints,Metric,Data model refinement required\n"
                "What is paid amount by gender?,Join KPI,Gender,sum(PaidAmount),Confirm join\n",
                encoding="utf-8",
            )
            (workspace / "docs" / "data_model.md").write_text(
                "# Data Model\n\ntransactions joins patients on PatientID.\n",
                encoding="utf-8",
            )

            WorkspaceOnboarder(root, "workspaces/demo", sample_rows=10).run()
            KPIFeatureResolver(root, "workspaces/demo", domain="healthcare").run()
            with self.assertRaisesRegex(ValueError, "relationship contract"):
                DuckDBKPISQLGenerator(root, "workspaces/demo").generate("kpi_001")

            contract_result = RelationshipContractBuilder(root, "workspaces/demo").build()
            self.assertGreaterEqual(contract_result.executable_relationship_count, 1)
            result = DuckDBKPISQLGenerator(root, "workspaces/demo").generate("kpi_001")
            sql = (root / result.path).read_text(encoding="utf-8")

            self.assertIn("LEFT JOIN", sql)
            self.assertIn('"PatientID" =', sql)
            conn = duckdb.connect(":memory:")
            old_cwd = Path.cwd()
            try:
                os.chdir(root)
                conn.execute(sql)
                rows = conn.execute(
                    'SELECT count(*) total, count("Gender") gender_count FROM "kpi_001_features"'
                ).fetchone()
            finally:
                os.chdir(old_cwd)
            self.assertEqual(rows, (2, 2))

    def test_relationship_contract_builder_marks_profile_only_relationships_non_executable(self):
        try:
            import polars  # noqa: F401
        except ImportError:
            self.skipTest("polars is not installed")

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            workspace = root / "workspaces" / "demo"
            (workspace / "datasets").mkdir(parents=True)
            (workspace / "docs").mkdir(parents=True)
            (workspace / "datasets" / "transactions.csv").write_text(
                "TransactionID,PatientID,PaidAmount\nT1,P1,10.50\n",
                encoding="utf-8",
            )
            (workspace / "datasets" / "patients.csv").write_text(
                "PatientID,Gender\nP1,F\n",
                encoding="utf-8",
            )
            (workspace / "docs" / "kpi_registry.csv").write_text(
                "Key business question,Description,Cuts / grain hints,Metric,Data model refinement required\n"
                "What is paid amount by gender?,Join KPI,Gender,sum(PaidAmount),Confirm join\n",
                encoding="utf-8",
            )
            (workspace / "docs" / "data_model.md").write_text(
                "# Data Model\n\nNo explicit relationship documented.\n",
                encoding="utf-8",
            )

            WorkspaceOnboarder(root, "workspaces/demo", sample_rows=10).run()
            result = RelationshipContractBuilder(root, "workspaces/demo").build()

            self.assertEqual(result.executable_relationship_count, 0)
            self.assertGreaterEqual(result.candidate_relationship_count, 1)
            contract = json.loads((root / result.json_path).read_text(encoding="utf-8"))
            relationship = contract["relationships"][0]
            self.assertEqual(relationship["state"], "profile_validated")
            self.assertFalse(
                relationship["executable_usage_policy"]["allowed_in_sql_generation"]
            )

    def test_data_model_generation_finalize_promotes_relationship_contracts(self):
        try:
            import polars  # noqa: F401
        except ImportError:
            self.skipTest("polars is not installed")

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            workspace = root / "workspaces" / "demo"
            (workspace / "datasets").mkdir(parents=True)
            (workspace / "docs").mkdir(parents=True)
            (workspace / "datasets" / "transactions.csv").write_text(
                "TransactionID,PatientID,PaidAmount\nT1,P1,10.50\n",
                encoding="utf-8",
            )
            (workspace / "datasets" / "patients.csv").write_text(
                "PatientID,Gender\nP1,F\n",
                encoding="utf-8",
            )
            (workspace / "docs" / "DataModel.png").write_text("not an actual image", encoding="utf-8")

            WorkspaceOnboarder(root, "workspaces/demo", sample_rows=10).run()
            first_contract = RelationshipContractBuilder(root, "workspaces/demo").build()
            self.assertEqual(first_contract.executable_relationship_count, 0)

            workflow = DataModelGenerationWorkflow(root, "workspaces/demo")
            prepared = workflow.prepare()
            self.assertEqual(prepared.stage, "route_selection")
            panel = json.loads((root / prepared.current_json_path).read_text(encoding="utf-8"))
            self.assertEqual(panel["recommended_option_id"], "option_c")

            applied = workflow.apply_answer(answer="option_b")
            self.assertEqual(applied.stage, "final_preview")
            draft = json.loads(
                (root / "workspaces/demo/interns/generated/requirements/data_model_draft.json")
                .read_text(encoding="utf-8")
            )
            self.assertGreaterEqual(draft["summary"]["relationship_count"], 1)
            self.assertEqual(draft["image_model_policy"]["state"], "review_gated")

            finalized = workflow.finalize(approve_final_preview=True)
            self.assertTrue((root / finalized.data_model_markdown_path).exists())
            self.assertTrue((root / finalized.erd_markdown_path).exists())
            self.assertTrue((root / finalized.relationships_markdown_path).exists())

            result = RelationshipContractBuilder(root, "workspaces/demo").build()
            self.assertGreaterEqual(result.executable_relationship_count, 1)
            contract = json.loads((root / result.json_path).read_text(encoding="utf-8"))
            self.assertTrue(
                any(
                    rel["state"] == "user_confirmed"
                    and rel["executable_usage_policy"]["allowed_in_sql_generation"]
                    for rel in contract["relationships"]
                )
            )

    def test_source_to_target_planner_writes_data_model_backed_plan(self):
        try:
            import polars  # noqa: F401
        except ImportError:
            self.skipTest("polars is not installed")

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self._create_demo_workspace(root)
            WorkspaceOnboarder(root, "workspaces/demo", sample_rows=10).run()
            KPIFeatureResolver(root, "workspaces/demo", domain="healthcare").run()

            result = SourceToTargetPlanner(root, "workspaces/demo", target_engine="sql").build()

            self.assertEqual(result.kpi_count, 1)
            self.assertEqual(result.ready_kpi_count, 1)
            self.assertTrue((root / result.json_path).exists())
            self.assertTrue((root / result.markdown_path).exists())
            plan = json.loads((root / result.json_path).read_text(encoding="utf-8"))
            kpi_plan = plan["kpis"][0]
            self.assertEqual(kpi_plan["target_engine"], "sql")
            self.assertEqual(kpi_plan["status"], "ready_for_generation")
            self.assertEqual(
                kpi_plan["selected_source_datasets"],
                ["workspaces/demo/datasets/transactions.csv"],
            )
            self.assertEqual(kpi_plan["medallion_layers"]["gold_output"]["engine"], "sql")
            self.assertIn("grain_matches_kpi_cuts", kpi_plan["validation_checks"])
            markdown = (root / result.markdown_path).read_text(encoding="utf-8")
            self.assertIn("Source To Target Plan", markdown)
            self.assertIn("workspaces/demo/datasets/transactions.csv", markdown)

    def test_source_to_target_planner_blocks_missing_join_proof(self):
        try:
            import polars  # noqa: F401
        except ImportError:
            self.skipTest("polars is not installed")

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            workspace = root / "workspaces" / "demo"
            (workspace / "datasets").mkdir(parents=True)
            (workspace / "docs").mkdir(parents=True)
            (workspace / "datasets" / "transactions.csv").write_text(
                "TransactionID,PaidAmount\nT1,10.50\n",
                encoding="utf-8",
            )
            (workspace / "datasets" / "patients.csv").write_text(
                "PatientKey,Gender\nP1,F\n",
                encoding="utf-8",
            )
            (workspace / "docs" / "kpi_registry.csv").write_text(
                "Key business question,Description,Cuts / grain hints,Metric,Data model refinement required\n"
                "What is paid by gender?,Multi source,Gender,sum(PaidAmount),Confirm join\n",
                encoding="utf-8",
            )
            (workspace / "docs" / "data_model.md").write_text(
                "# Data Model\n\nNo join key documented.\n",
                encoding="utf-8",
            )
            WorkspaceOnboarder(root, "workspaces/demo", sample_rows=10).run()
            KPIFeatureResolver(root, "workspaces/demo", domain="healthcare").run()

            result = SourceToTargetPlanner(root, "workspaces/demo", target_engine="pyspark").build()

            self.assertEqual(result.blocked_kpi_count, 1)
            plan = json.loads((root / result.json_path).read_text(encoding="utf-8"))
            blockers = plan["kpis"][0]["blockers"]
            self.assertTrue(any(blocker["type"] == "join_proof_missing" for blocker in blockers))
            self.assertEqual(plan["kpis"][0]["target_engine"], "pyspark")

    def test_kpi_generation_prepare_writes_two_path_panel_and_quality_score(self):
        try:
            import polars  # noqa: F401
        except ImportError:
            self.skipTest("polars is not installed")

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            workspace = self._create_demo_workspace(root)
            transcript = workspace / "docs" / "meeting_transcript.md"
            transcript.write_text(
                "# Meeting\n\nProduct lead wants paid amount visibility by line of business.",
                encoding="utf-8",
            )

            result = KPIGenerationWorkflow(root, "workspaces/demo").prepare(
                context_files=["workspaces/demo/docs/meeting_transcript.md"]
            )

            self.assertEqual(result.stage, "route_selection")
            self.assertTrue((root / result.session_path).exists())
            panel = json.loads((root / result.current_json_path).read_text(encoding="utf-8"))
            self.assertEqual(panel["stage"], "route_selection")
            self.assertEqual(
                [option["option_id"] for option in panel["options"]],
                ["option_a", "option_b"],
            )
            self.assertIn("quality_score", panel)
            self.assertIn("implementation_readiness", panel["quality_score"])
            session = json.loads((root / result.session_path).read_text(encoding="utf-8"))
            self.assertEqual(session["context_files"], ["workspaces/demo/docs/meeting_transcript.md"])

    def test_kpi_generation_interview_writes_draft_and_requires_final_approval(self):
        try:
            import polars  # noqa: F401
        except ImportError:
            self.skipTest("polars is not installed")

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self._create_demo_workspace(root)
            workflow = KPIGenerationWorkflow(root, "workspaces/demo")
            workflow.prepare()
            workflow.apply_answer(answer="option_a")
            workflow.apply_answer(answer="option_b")
            workflow.apply_answer(answer="option_c")
            result = workflow.apply_answer(answer="option_c")

            self.assertEqual(result.stage, "final_preview")
            draft_path = (
                root
                / "workspaces/demo/interns/generated/requirements/kpi_registry_draft.json"
            )
            self.assertTrue(draft_path.exists())
            draft = json.loads(draft_path.read_text(encoding="utf-8"))
            self.assertEqual(draft["draft_status"], "requires_final_preview_approval")
            self.assertEqual(draft["proofs"][0]["proof_level"], "evidence_proof")
            self.assertEqual(draft["competitive_review"]["mode"], "competitive_review")
            with self.assertRaises(PermissionError):
                workflow.finalize(approve_final_preview=False)

            finalized = workflow.finalize(approve_final_preview=True)

            self.assertTrue((root / finalized.output_registry_path).exists())
            self.assertTrue((root / finalized.production_proof_path).exists())
            self.assertTrue((root / finalized.workspace_memory_path).exists())
            self.assertTrue((root / finalized.team_memory_path).exists())
            proof = json.loads((root / finalized.production_proof_path).read_text(encoding="utf-8"))
            self.assertEqual(proof["proof_level"], "production_readiness_proof")
            self.assertTrue(
                any("source-to-target" in item for item in proof["required_before_publish"])
            )

    def test_kpi_sql_generator_emits_databricks_dialect(self):
        try:
            import polars  # noqa: F401
        except ImportError:
            self.skipTest("polars is not installed")

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self._create_demo_workspace(root)
            WorkspaceOnboarder(root, "workspaces/demo", sample_rows=10).run()
            KPIFeatureResolver(root, "workspaces/demo", domain="healthcare").run()
            result = DuckDBKPISQLGenerator(
                root,
                "workspaces/demo",
                dialect="databricks",
                catalog="main",
                schema="rcm",
            ).generate("kpi_001")
            self.assertEqual(result.dialect, "databricks")
            sql = (root / result.path).read_text(encoding="utf-8")
            self.assertIn("-- Dialect: databricks", sql)
            self.assertIn("CREATE OR REPLACE TEMP VIEW", sql)
            self.assertIn("`main`.`rcm`.`transactions`", sql)
            self.assertNotIn("read_csv_auto", sql)

    def test_databricks_asset_manifest_maps_profiles_to_uc_tables(self):
        try:
            import polars  # noqa: F401
        except ImportError:
            self.skipTest("polars is not installed")

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            workspace = self._create_demo_workspace(root)
            WorkspaceOnboarder(root, "workspaces/demo", sample_rows=10).run()
            result = DatabricksAssetManifestBuilder(
                root,
                "workspaces/demo",
                environment="dev",
                domain="rcm",
                workspace_root="/Workspace/Autoresearch",
                catalog="main",
                schema="rcm",
            ).build()
            self.assertEqual(result.dataset_count, 1)
            self.assertGreaterEqual(result.workspace_asset_count, 1)
            self.assertEqual(result.environment, "dev")
            self.assertEqual(result.domain, "rcm")
            self.assertEqual(result.workspace_path, "/Workspace/Autoresearch/dev/rcm")
            manifest = json.loads((root / result.path).read_text(encoding="utf-8"))
            self.assertEqual(manifest["operating_model"], "federated_enterprise")
            self.assertEqual(manifest["environment"], "dev")
            self.assertEqual(manifest["domain"], "rcm")
            self.assertFalse(manifest["execution_truth"]["promotion_from_local_execution_allowed"])
            self.assertTrue(manifest["genie_role"]["not_source_of_truth"])
            self.assertEqual(manifest["non_genie_fallback"]["mode"], "api_agent_or_ci")
            asset = manifest["assets"][0]
            self.assertEqual(asset["target_fqn"], "main.rcm.transactions")
            self.assertEqual(asset["environment"], "dev")
            self.assertEqual(asset["domain"], "rcm")
            self.assertEqual(asset["registration_state"], "requires_user_approved_upload_or_table_registration")
            workspace_asset = manifest["workspace_assets"][0]
            self.assertTrue(workspace_asset["target_path"].startswith("/Workspace/Autoresearch/dev/rcm/"))
            self.assertIn("source_sha256", workspace_asset)
            self.assertIn("edit_policy", workspace_asset)
            self.assertTrue((workspace / "interns" / "generated" / "requirements" / "databricks_asset_manifest.json").exists())

    def test_genie_workspace_spec_generates_runbook_permissions_and_evolution(self):
        try:
            import polars  # noqa: F401
        except ImportError:
            self.skipTest("polars is not installed")

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            workspace = self._create_demo_workspace(root)
            WorkspaceOnboarder(root, "workspaces/demo", sample_rows=10).run()
            DatabricksAssetManifestBuilder(
                root,
                "workspaces/demo",
                environment="dev",
                domain="rcm",
                catalog="dev",
                schema="rcm",
            ).build()

            result = GenieWorkspaceSpecBuilder(root, "workspaces/demo").build()

            self.assertGreaterEqual(result.folder_count, 1)
            self.assertEqual(result.genie_space_count, 1)
            self.assertGreaterEqual(result.prompt_count, 5)
            self.assertGreaterEqual(result.permission_count, 5)
            spec = json.loads((root / result.spec_path).read_text(encoding="utf-8"))
            self.assertEqual(spec["databricks_workspace_path"], "/Workspace/Autoresearch/dev/rcm")
            self.assertFalse(spec["remote_mutation"]["enabled_by_this_generator"])
            self.assertEqual(spec["non_genie_fallback"]["mode"], "api_agent_or_ci")
            self.assertTrue(any(folder.endswith("/genie") for folder in spec["workspace_folders"]))
            space = spec["genie_spaces"][0]
            self.assertTrue(space["target_path"].endswith("/genie/kpi_operator"))
            self.assertIn("Do not treat Genie answers as source of truth.", space["guardrails"])
            self.assertTrue(space["starter_prompts"])
            roles = {item["role"] for item in spec["permissions"]["role_action_matrix"]}
            self.assertIn("platform_admin", roles)
            self.assertIn("api_agent_ci", roles)
            tracked = spec["drift_detection"]["tracked_workspace_assets"]
            self.assertTrue(tracked)
            self.assertIn("source_sha256", tracked[0])
            runbook = (root / result.runbook_path).read_text(encoding="utf-8")
            self.assertIn("Genie Operator Runbook", runbook)
            self.assertTrue((workspace / "interns" / "generated" / "memory" / "genie_workspace_decisions.json").exists())
            self.assertTrue((workspace / "interns" / "generated" / "memory" / "evolution.md").exists())
            lessons = json.loads((root / result.lessons_path).read_text(encoding="utf-8"))
            self.assertEqual(lessons[0]["topic"], "genie_workspace_setup")

    def test_databricks_workspace_deployment_dry_run_writes_plan(self):
        try:
            import polars  # noqa: F401
        except ImportError:
            self.skipTest("polars is not installed")

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            workspace = self._create_demo_workspace(root)
            WorkspaceOnboarder(root, "workspaces/demo", sample_rows=10).run()
            DatabricksAssetManifestBuilder(root, "workspaces/demo").build()
            GenieWorkspaceSpecBuilder(root, "workspaces/demo").build()

            result = run_deployment(root, "workspaces/demo")

            self.assertFalse(result.applied)
            self.assertGreater(result.operation_count, 0)
            self.assertGreater(result.apply_supported_count, 0)
            self.assertGreater(result.spec_only_count, 0)
            plan = json.loads((root / result.plan_path).read_text(encoding="utf-8"))
            actions = {operation["action"] for operation in plan["operations"]}
            self.assertIn("create_workspace_folder", actions)
            self.assertIn("deploy_workspace_file", actions)
            self.assertIn("ensure_unity_catalog_schema", actions)
            self.assertIn("create_evidence_table", actions)
            self.assertIn("prepare_dataset_registration", actions)
            self.assertIn("prepare_job_payload", actions)
            self.assertIn("prepare_dashboard_spec", actions)
            self.assertIn("prepare_genie_space_spec", actions)
            uc_ops = [operation for operation in plan["operations"] if operation["op_id"].startswith("uc:")]
            self.assertGreaterEqual(len(uc_ops), 4)
            self.assertTrue(any("CREATE TABLE IF NOT EXISTS" in operation["payload"].get("sql", "") for operation in uc_ops))
            self.assertEqual(plan["remote_mutation_default"], "disabled")
            self.assertTrue((workspace / "interns" / "reports" / "databricks_workspace_deployment_dry_run.md").exists())
            self.assertTrue((root / result.central_report_path).exists())
            self.assertTrue((root / result.deployment_index_path).exists())
            index = json.loads((root / result.deployment_index_path).read_text(encoding="utf-8"))
            self.assertEqual(index["latest"]["workspace"], "workspaces/demo")
            self.assertEqual(index["latest"]["workspace_report_path"], result.report_path)
            self.assertEqual(index["latest"]["central_report_path"], result.central_report_path)

    def test_databricks_workspace_deployment_apply_requires_explicit_approval(self):
        try:
            import polars  # noqa: F401
        except ImportError:
            self.skipTest("polars is not installed")

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self._create_demo_workspace(root)
            WorkspaceOnboarder(root, "workspaces/demo", sample_rows=10).run()
            DatabricksAssetManifestBuilder(root, "workspaces/demo").build()
            GenieWorkspaceSpecBuilder(root, "workspaces/demo").build()
            old = os.environ.get("AUTORESEARCH_ALLOW_REMOTE_EXECUTION")
            try:
                os.environ.pop("AUTORESEARCH_ALLOW_REMOTE_EXECUTION", None)
                with self.assertRaises(PermissionError):
                    run_deployment(root, "workspaces/demo", apply=True)
                with self.assertRaises(PermissionError):
                    run_deployment(
                        root,
                        "workspaces/demo",
                        apply=True,
                        confirm_remote_mutation=True,
                    )
            finally:
                if old is None:
                    os.environ.pop("AUTORESEARCH_ALLOW_REMOTE_EXECUTION", None)
                else:
                    os.environ["AUTORESEARCH_ALLOW_REMOTE_EXECUTION"] = old

    def test_workspace_cleanup_removes_generated_references_only(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            workspace = self._create_demo_workspace(root)
            (workspace / "interns" / "generated").mkdir(parents=True)
            (workspace / "interns" / "generated" / "stale.json").write_text("{}", encoding="utf-8")
            (root / "config").mkdir()
            (root / "config" / "tasks.json").write_text(
                json.dumps({
                    "active_task": "demo",
                    "tasks": [
                        {
                            "id": "demo",
                            "workspace": "workspaces/demo",
                            "sql_file": "workspaces/demo/interns/generated/solutions/kpi.sql",
                        },
                        {"id": "keep", "workspace": "workspaces/keep"},
                    ],
                }),
                encoding="utf-8",
            )
            (root / "state" / "databricks" / "deployments" / "workspaces-demo" / "run").mkdir(
                parents=True
            )
            (root / "state" / "databricks" / "deployments" / "latest.md").write_text(
                "Workspace: workspaces/demo", encoding="utf-8"
            )
            (root / "state" / "databricks" / "deployments" / "deployment_index.json").write_text(
                json.dumps({
                    "version": 1,
                    "deployments": [
                        {"workspace": "workspaces/demo", "report": "workspaces/demo/interns/report.md"},
                        {"workspace": "workspaces/keep"},
                    ],
                    "latest": {"workspace": "workspaces/demo"},
                }),
                encoding="utf-8",
            )
            (root / "state" / "workspace.db").write_text("", encoding="utf-8")

            dry_run = run_cleanup(
                root,
                "workspaces/demo",
                remove_repo_state=True,
                remove_task_config=True,
            )

            self.assertFalse(dry_run.applied)
            self.assertTrue(any(action.action == "delete_tree" for action in dry_run.actions))
            self.assertTrue((workspace / "interns").exists())

            with self.assertRaises(PermissionError):
                run_cleanup(
                    root,
                    "workspaces/demo",
                    apply=True,
                    remove_repo_state=True,
                    remove_task_config=True,
                )

            result = run_cleanup(
                root,
                "workspaces/demo",
                apply=True,
                confirm_delete="workspaces/demo",
                remove_repo_state=True,
                remove_task_config=True,
            )

            self.assertTrue(result.applied)
            self.assertFalse((workspace / "interns").exists())
            self.assertTrue((workspace / "docs" / "data_model.md").exists())
            self.assertTrue((workspace / "datasets" / "transactions.csv").exists())
            self.assertFalse((root / "state" / "workspace.db").exists())
            self.assertFalse(
                (root / "state" / "databricks" / "deployments" / "workspaces-demo").exists()
            )
            task_config = json.loads((root / "config" / "tasks.json").read_text(encoding="utf-8"))
            self.assertEqual(task_config["active_task"], "keep")
            self.assertEqual(task_config["tasks"], [{"id": "keep", "workspace": "workspaces/keep"}])
            deployment_index = json.loads(
                (root / "state" / "databricks" / "deployments" / "deployment_index.json").read_text(
                    encoding="utf-8"
                )
            )
            self.assertEqual(deployment_index["deployments"], [{"workspace": "workspaces/keep"}])
            self.assertEqual(deployment_index["latest"], {"workspace": "workspaces/keep"})

    def test_workspace_kickstart_writes_hybrid_task_and_discovery(self):
        try:
            import polars  # noqa: F401
        except ImportError:
            self.skipTest("polars is not installed")

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            workspace = self._create_demo_workspace(root)
            (root / "config").mkdir()
            (root / "config" / "tasks.json").write_text(
                json.dumps({"active_task": "old", "tasks": [{"id": "old", "workspace": "workspaces/old"}]}),
                encoding="utf-8",
            )
            (workspace / "docs" / "approval_policy.md").write_text(
                "# Approval Policy\n\nProduction changes require review.\n",
                encoding="utf-8",
            )
            (workspace / "docs" / "claims_data_contract.md").write_text(
                "# Data Contract\n\nClaims must include ClaimID and PaidAmount.\n",
                encoding="utf-8",
            )

            result = WorkspaceKickstarter(
                root,
                "workspaces/demo",
                task_id="demo_task",
                name="Demo Task",
                sample_rows=10,
            ).run()

            tasks = json.loads((root / "config" / "tasks.json").read_text(encoding="utf-8"))
            self.assertEqual(tasks["active_task"], "demo_task")
            self.assertEqual(len(tasks["tasks"]), 2)
            task = next(item for item in tasks["tasks"] if item["id"] == "demo_task")
            self.assertEqual(task["workspace"], "workspaces/demo")
            self.assertEqual(
                task["editable_file"],
                "workspaces/demo/interns/generated/solutions/kpi_metrics.sql",
            )
            self.assertEqual(
                task["experiment_cmd"],
                ["uv", "run", "python", "workspaces/demo/interns/evaluation/experiment.py"],
            )
            self.assertIn("optimization_policy", task)
            self.assertEqual(
                task["semantic_contract"]["feature_mapping"],
                "workspaces/demo/interns/generated/contracts/kpi_feature_mapping.json",
            )
            self.assertEqual(
                task["enterprise_artifacts"]["feature_mapping"],
                "workspaces/demo/interns/generated/contracts/kpi_feature_mapping.json",
            )
            self.assertIn("feature_resolution", task)
            self.assertEqual(
                task["enterprise_artifacts"]["policy_docs"],
                ["workspaces/demo/docs/approval_policy.md"],
            )
            self.assertEqual(
                task["enterprise_artifacts"]["data_contract_docs"],
                ["workspaces/demo/docs/claims_data_contract.md"],
            )
            self.assertTrue((workspace / "interns" / "generated" / "requirements" / "enterprise_discovery.json").exists())
            self.assertTrue((workspace / "interns" / "generated" / "contracts" / "kpi_feature_mapping.json").exists())
            self.assertTrue((workspace / "interns" / "reports" / "open_questions.md").exists())
            self.assertIn("missing_enterprise_docs:sla", result.warnings)

    def test_workspace_kickstart_ignores_generated_interns_and_temp_docs(self):
        try:
            import polars  # noqa: F401
        except ImportError:
            self.skipTest("polars is not installed")

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            workspace = self._create_demo_workspace(root)
            (root / "config").mkdir()
            (workspace / "interns" / "generated").mkdir(parents=True)
            (workspace / "interns" / "generated" / "policy.md").write_text(
                "generated policy should not be treated as input",
                encoding="utf-8",
            )
            (workspace / "docs" / "~$Sample KPI.xlsx").write_text("", encoding="utf-8")
            (workspace / "docs" / "ops_sla.md").write_text(
                "# SLA\n\nRuntime target is 90 seconds.\n",
                encoding="utf-8",
            )

            result = WorkspaceKickstarter(
                root,
                "workspaces/demo",
                task_id="demo_task",
                sample_rows=10,
            ).run()

            discovery = json.loads(
                (workspace / "interns" / "generated" / "requirements" / "enterprise_discovery.json")
                .read_text(encoding="utf-8")
            )
            document_paths = {item["path"] for item in discovery["documents"]}
            self.assertIn("workspaces/demo/docs/ops_sla.md", document_paths)
            self.assertNotIn("workspaces/demo/interns/generated/policy.md", document_paths)
            self.assertNotIn("workspaces/demo/docs/~$Sample KPI.xlsx", document_paths)
            self.assertIn("workspaces/demo/datasets/transactions.csv", {
                item["path"] for item in discovery["datasets"]
            })
            self.assertEqual(result.task["enterprise_artifacts"]["sla_docs"], ["workspaces/demo/docs/ops_sla.md"])

    def test_databricks_backend_requires_explicit_remote_approval(self):
        os.environ.pop("AUTORESEARCH_ALLOW_REMOTE_EXECUTION", None)
        cfg = Config(
            databricks=DatabricksConfig(
                enabled=True,
                execution="jobs",
                host="https://example.cloud.databricks.com",
                token="token",
            )
        )
        self.assertIsInstance(build_execution_backend(cfg), DuckDBBackend)

    def test_databricks_jobs_reject_local_python_paths_before_submission(self):
        client = DatabricksClient(DatabricksConfig(enabled=True, host="https://example", token="token"))
        with self.assertRaises(ValueError):
            client.submit_job_run(
                {"experiment_cmd": ["uv", "run", "python", "workspaces/demo/interns/evaluation/experiment.py"]},
                30,
            )

    def test_databricks_warehouse_strict_mode_does_not_fallback(self):
        class FailingClient:
            def _extract_warehouse_id(self):
                return "warehouse_1"

            def get_client(self):
                raise RuntimeError("remote unavailable")

        with tempfile.TemporaryDirectory() as tmp:
            sql_path = Path(tmp) / "query.sql"
            log_path = Path(tmp) / "run.log"
            sql_path.write_text("SELECT 1", encoding="utf-8")
            backend = StrictWarehouseBackend(
                FailingClient(),
                DatabricksConfig(enabled=True, execution="warehouse", fallback="fail"),
            )
            result = backend.execute({"sql_file": str(sql_path)}, 10, 10, log_path)
            self.assertEqual(result.exit_code, 1)
            self.assertIn("databricks_backend: warehouse", result.log_content)
            self.assertIn("remote unavailable", result.log_content)

    def test_local_metadata_store_writes_structured_document(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = LocalMetadataStore(Path(tmp) / "metadata")
            result = store.upsert(
                "contracts",
                "semantic_contract",
                {"kpi_count": 2},
                workspace="workspaces/demo",
            )
            self.assertEqual(result.backend, "local")
            path = Path(result.path)
            self.assertTrue(path.exists())
            payload = json.loads(path.read_text(encoding="utf-8"))
            self.assertEqual(payload["workspace"], "workspaces/demo")
            self.assertEqual(payload["payload"]["kpi_count"], 2)

    def test_delta_metadata_store_writes_structured_table(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = DeltaMetadataStore(Path(tmp) / "delta")
            result = store.upsert(
                "contracts",
                "semantic_contract",
                {"kpi_count": 2},
                workspace="workspaces/demo",
            )
            self.assertEqual(result.backend, "delta")
            table_path = Path(result.path)
            self.assertTrue((table_path / "_delta_log").exists())

            from deltalake import DeltaTable

            rows = DeltaTable(str(table_path)).to_pyarrow_table().to_pylist()
            self.assertEqual(rows[0]["workspace"], "workspaces/demo")
            self.assertEqual(rows[0]["document_id"], "semantic_contract")
            self.assertIn('"kpi_count": 2', rows[0]["payload_json"])

    def test_mongo_metadata_store_falls_back_to_local_when_unavailable(self):
        with tempfile.TemporaryDirectory() as tmp:
            fallback = LocalMetadataStore(Path(tmp) / "metadata")
            store = MongoMetadataStore("mongodb://127.0.0.1:1", local_fallback=fallback)
            result = store.upsert(
                "contracts",
                "semantic_contract",
                {"kpi_count": 2},
                workspace="workspaces/demo",
            )
            self.assertIn("mongo->local", result.backend)
            self.assertIsNotNone(result.warning)
            self.assertTrue(Path(result.path).exists())

    def test_build_metadata_store_uses_local_without_mongo_uri(self):
        with tempfile.TemporaryDirectory() as tmp:
            old_backend = os.environ.get("AUTORESEARCH_METADATA_BACKEND")
            old_uri = os.environ.get("AUTORESEARCH_MONGO_URI")
            try:
                os.environ["AUTORESEARCH_METADATA_BACKEND"] = "mongo"
                os.environ.pop("AUTORESEARCH_MONGO_URI", None)
                layout = WorkspaceLayout(project_root=Path(tmp) / "workspaces" / "demo")
                layout.ensure_runtime_dirs()
                self.assertIsInstance(build_metadata_store(layout), DeltaMetadataStore)
            finally:
                if old_backend is None:
                    os.environ.pop("AUTORESEARCH_METADATA_BACKEND", None)
                else:
                    os.environ["AUTORESEARCH_METADATA_BACKEND"] = old_backend
                if old_uri is None:
                    os.environ.pop("AUTORESEARCH_MONGO_URI", None)
                else:
                    os.environ["AUTORESEARCH_MONGO_URI"] = old_uri

    def test_build_metadata_store_defaults_to_delta(self):
        with tempfile.TemporaryDirectory() as tmp:
            old_backend = os.environ.get("AUTORESEARCH_METADATA_BACKEND")
            try:
                os.environ.pop("AUTORESEARCH_METADATA_BACKEND", None)
                layout = WorkspaceLayout(project_root=Path(tmp) / "workspaces" / "demo")
                layout.ensure_runtime_dirs()
                self.assertIsInstance(build_metadata_store(layout), DeltaMetadataStore)
            finally:
                if old_backend is None:
                    os.environ.pop("AUTORESEARCH_METADATA_BACKEND", None)
                else:
                    os.environ["AUTORESEARCH_METADATA_BACKEND"] = old_backend


if __name__ == "__main__":
    unittest.main()
