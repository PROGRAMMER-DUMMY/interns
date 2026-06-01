from __future__ import annotations

import contextlib
import io
import json
import tempfile
import unittest
from pathlib import Path

from core.onboarding.workspace.flow import (
    WorkspaceFlow,
    _build_kpi_resolution_review,
    _compact_panel,
    main as workspace_flow_main,
    _render_panel_markdown,
    _result_view,
)


class WorkspaceFlowTests(unittest.TestCase):
    def test_start_kpi_generation_creates_compact_session_panel(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            workspace = root / "workspaces" / "demo"
            workspace.mkdir(parents=True)
            (workspace / "encounters.csv").write_text("Id,START\nE1,2024-01-01\n", encoding="utf-8")
            (workspace / "hospital_analytics_questions.sql").write_text(
                "-- How many encounters occurred each year?\n",
                encoding="utf-8",
            )

            result = WorkspaceFlow(root, "workspaces/demo").start(intent="kpi_generation")

            self.assertEqual(result.status, "needs_user_answer")
            self.assertEqual(result.stage, "kpi_generation_route")
            self.assertTrue((root / result.state_path).exists())
            panel = json.loads((root / result.current_panel_path).read_text(encoding="utf-8"))
            self.assertEqual(panel["source"], "kpi_generation")
            self.assertIn("instruction", panel)
            self.assertTrue(panel["options"])

            reloaded = WorkspaceFlow.from_session(root, result.session_id).status()
            self.assertEqual(reloaded.session_id, result.session_id)
            self.assertEqual(reloaded.current_panel_path, result.current_panel_path)

    def test_kpi_blocker_panel_keeps_full_resolution_review_visible(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            workspace = root / "workspaces" / "demo"
            contracts = workspace / "interns" / "generated" / "contracts"
            contracts.mkdir(parents=True)
            (contracts / "kpi_feature_mapping.json").write_text(
                json.dumps(
                    {
                        "kpis": [
                            {
                                "kpi_id": "kpi_001",
                                "name": (
                                    "What is trend for amount paid for medicare LOB across gender "
                                    "and payer for patients above 50 years of age"
                                ),
                                "source": "workspaces/demo/docs/Sample KPI.xlsx",
                                "metric": "sum(PaidAmount)",
                                "cuts": (
                                    "Month (ServiceDate), LineOfBusiness, PayorID, Gender, "
                                    "Age(DOB), LOB = Medicare, Age > 50"
                                ),
                                "status": "ready_for_sql",
                                "features": [
                                    {
                                        "feature": "PaidAmount",
                                        "state": "proven_direct",
                                        "source_columns": [
                                            {
                                                "dataset": "workspaces/demo/datasets/transactions.csv",
                                                "column": "PaidAmount",
                                            }
                                        ],
                                    },
                                    {
                                        "feature": "Age",
                                        "state": "proven_formula",
                                        "source_columns": [
                                            {
                                                "dataset": "workspaces/demo/datasets/patients.csv",
                                                "column": "DOB",
                                            }
                                        ],
                                    },
                                ],
                            }
                        ]
                    }
                ),
                encoding="utf-8",
            )

            review = _build_kpi_resolution_review(root, "workspaces/demo")
            panel = _compact_panel(
                stage="kpi_blocker",
                status="needs_user_answer",
                source_panel={
                    "question": "Approve KPI 1 resolution?",
                    "options": [{"option_id": "option_a", "label": "Approve mapping"}],
                    "recommended_option_id": "option_a",
                    "output_dialect": {
                        "label": "SQL (default)",
                        "alternatives": ["polars", "pyspark"],
                        "rule": "Render SQL by default.",
                    },
                    "immutable_kpi_policy": {
                        "rule": "The KPI from the source workbook or registry is hard truth and must not be rewritten.",
                    },
                    "kpi_understanding": [
                        {
                            "kpi_id": "kpi_001",
                            "original_kpi": {
                                "business_question": "What is trend for amount paid for medicare LOB?",
                                "metric": "sum(PaidAmount)",
                                "cuts": ["Month (ServiceDate)", "LOB = Medicare"],
                            },
                            "my_understanding": "Answer the KPI exactly as written.",
                            "strict_proven_sql": "SELECT \"PaidAmount\" FROM \"transactions\" LIMIT 20;",
                            "intent_sql_sketch": "-- NON-EXECUTABLE INTENT SKETCH\nSELECT sum(PaidAmount) FROM <SOURCE_TABLE>;",
                            "demo_result_table": "| metric_value |\n| --- |\n| <computed> |",
                        }
                    ],
                },
                instruction="Review the full KPI resolution before answering.",
                artifact_paths=["workspaces/demo/interns/reports/blocker_question_panel/current.json"],
                resolution_review=review,
            )
            markdown = _render_panel_markdown(panel)

            self.assertIn("KPI Resolution Review", markdown)
            self.assertIn("What is trend for amount paid for medicare LOB", markdown)
            self.assertIn("sum(PaidAmount)", markdown)
            self.assertIn("Month (ServiceDate)", markdown)
            self.assertIn("LOB = Medicare", markdown)
            self.assertIn("Age > 50", markdown)
            self.assertIn("Resolved Source Mapping", markdown)
            self.assertIn("## Output Dialect", markdown)
            self.assertIn("SQL (default)", markdown)
            self.assertIn("## Immutable KPI Policy", markdown)
            self.assertIn("## KPI Understanding Review", markdown)
            self.assertIn("#### My Understanding", markdown)
            self.assertIn("#### Strict Proven SQL", markdown)
            self.assertIn("#### Placeholder Intent SQL", markdown)
            self.assertIn("Demo Result Table", markdown)
            self.assertTrue(panel["hidden_panel_harness"]["hidden"])
            self.assertTrue(panel["hidden_panel_harness"]["passed"])

    def test_workspace_flow_cli_prints_panel_markdown_not_only_summary_json(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            workspace = root / "workspaces" / "demo"
            workspace.mkdir(parents=True)
            (workspace / "encounters.csv").write_text("Id,START\nE1,2024-01-01\n", encoding="utf-8")
            (workspace / "hospital_analytics_questions.sql").write_text(
                "-- How many encounters occurred each year?\n",
                encoding="utf-8",
            )
            stdout = io.StringIO()

            with contextlib.redirect_stdout(stdout):
                exit_code = workspace_flow_main(
                    [
                        "--repo-root",
                        str(root),
                        "start",
                        "--workspace",
                        "workspaces/demo",
                        "--intent",
                        "kpi_generation",
                    ]
                )

            output = stdout.getvalue()
            self.assertEqual(exit_code, 0)
            self.assertIn("# Workspace Flow: kpi_generation_route", output)
            self.assertIn("## Question", output)
            self.assertIn("## Options", output)
            self.assertIn("## Next Step", output)
            self.assertNotEqual(output.lstrip()[0], "{")

    def test_plan_mode_returns_three_orchestration_choices(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            workspace = root / "workspaces" / "demo"
            workspace.mkdir(parents=True)
            (workspace / "encounters.csv").write_text("Id,START\nE1,2024-01-01\n", encoding="utf-8")

            result = WorkspaceFlow(root, "workspaces/demo").start(intent="full_kpi_sql", mode="plan")

            self.assertEqual(result.stage, "workflow_checkpoint")
            self.assertEqual(result.status, "needs_user_choice")
            panel = json.loads((root / result.current_panel_path).read_text(encoding="utf-8"))
            self.assertEqual(panel["source"], "workflow_checkpoint")
            self.assertEqual([option["option_id"] for option in panel["options"]], ["option_a", "option_b", "option_c"])

    def test_data_quality_gate_runs_before_kpi_blocker_and_records_duplicate_decision(self):
        try:
            import duckdb  # noqa: F401
            import polars  # noqa: F401
        except ImportError:
            self.skipTest("duckdb or polars is not installed")

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            workspace = root / "workspaces" / "demo"
            (workspace / "datasets").mkdir(parents=True)
            (workspace / "docs").mkdir()
            (workspace / "datasets" / "transactions.csv").write_text(
                "TransactionID,PatientID,ServiceDate,PayorID,PaidAmount,ModifiedDate\n"
                "T1,P1,2024-01-01,PAY1,10.00,2024-01-03\n"
                "T1,P1,2024-01-01,PAY1,10.00,2024-01-04\n"
                "T2,P2,2024-01-02,PAY2,20.00,2024-01-05\n",
                encoding="utf-8",
            )
            (workspace / "docs" / "kpi_registry.csv").write_text(
                "Key business question,Description,Cuts / grain hints,Metric,Data model refinement required\n"
                "What is paid amount?,Baseline KPI,PayorID,sum(PaidAmount),Confirm source\n",
                encoding="utf-8",
            )
            (workspace / "docs" / "data_model.md").write_text(
                "# Data Model\n\ntransactions has TransactionID, PatientID, ServiceDate, PayorID, PaidAmount.\n",
                encoding="utf-8",
            )

            result = WorkspaceFlow(root, "workspaces/demo").start(intent="full_kpi_sql")

            self.assertEqual(result.stage, "data_quality_duplicate_review")
            panel = json.loads((root / result.current_panel_path).read_text(encoding="utf-8"))
            self.assertEqual(panel["source"], "duplicate_review")
            self.assertEqual(panel["recommended_option_id"], "option_a")
            self.assertIn("orchestration_context", panel)
            self.assertEqual(panel["orchestration_context"]["layer_route"]["selected_track"], "medallion")

            WorkspaceFlow.from_session(root, result.session_id).answer(answer="option_a")

            decisions = json.loads(
                (workspace / "interns" / "generated" / "contracts" / "duplicate_decisions.json").read_text(
                    encoding="utf-8"
                )
            )
            self.assertEqual(decisions["decisions"][0]["action"], "preserve")

    def test_full_kpi_sql_flow_runs_quiet_backend_and_writes_results(self):
        try:
            import duckdb  # noqa: F401
            import polars  # noqa: F401
        except ImportError:
            self.skipTest("duckdb or polars is not installed")

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            workspace = root / "workspaces" / "demo"
            (workspace / "docs").mkdir(parents=True)
            (workspace / "datasets").mkdir()
            (workspace / "datasets" / "encounters.csv").write_text(
                "Id,START,ENCOUNTERCLASS\nE1,2024-01-01,ambulatory\nE2,2024-01-02,inpatient\n",
                encoding="utf-8",
            )
            (workspace / "docs" / "kpi_registry.csv").write_text(
                "Key business question,Description,Cuts / grain hints,Metric,Data model refinement required\n"
                "How many encounters are recorded?,Count encounters,,count(Id),\n",
                encoding="utf-8",
            )

            result = WorkspaceFlow(root, "workspaces/demo").start(intent="full_kpi_sql")

            # Hard gate: completion now requires a recorded kpi-analyst semantic
            # review. The flow stops at the review gate first.
            self.assertEqual(result.status, "needs_specialist_review")
            self.assertEqual(result.stage, "kpi_analyst_review")
            # Orchestrator invokes kpi-analyst and posts the verdict back.
            result = WorkspaceFlow.from_session(root, result.session_id).review(
                verdict="ok", summary="all 1 KPI answers its intent",
            )

            self.assertEqual(result.status, "complete")
            panel = json.loads((root / result.current_panel_path).read_text(encoding="utf-8"))
            self.assertEqual(panel["source"], "complete")
            self.assertEqual(panel["summary"]["generated_kpi_count"], 1)
            self.assertEqual(panel["summary"]["kpi_analyst_review"]["verdict"], "ok")
            result_md = workspace / "interns" / "reports" / "kpi_results" / "current.md"
            self.assertTrue(result_md.exists())
            self.assertIn("kpi_001", result_md.read_text(encoding="utf-8"))
            harness = workspace / "interns" / "generated" / "evidence" / "kpi_execution_harness.json"
            self.assertTrue(harness.exists())
            self.assertTrue(json.loads(harness.read_text(encoding="utf-8"))["ok"])

            # The dated runs/<date>/ snapshot is written by the executor and must
            # mirror the live results exactly — same combined body, per-KPI file present.
            from datetime import date

            runs_dir = workspace / "interns" / "runs" / date.today().isoformat()
            self.assertTrue((runs_dir / "results.md").exists())
            self.assertEqual(
                (runs_dir / "results.md").read_text(encoding="utf-8"),
                result_md.read_text(encoding="utf-8"),
            )
            self.assertTrue((runs_dir / "kpi_001.md").exists())
            self.assertIn("kpi_001", (runs_dir / "kpi_001.md").read_text(encoding="utf-8"))

    def test_completion_is_gated_on_kpi_analyst_review(self):
        try:
            import duckdb  # noqa: F401
            import polars  # noqa: F401
        except ImportError:
            self.skipTest("duckdb or polars is not installed")

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            workspace = root / "workspaces" / "demo"
            (workspace / "docs").mkdir(parents=True)
            (workspace / "datasets").mkdir()
            (workspace / "datasets" / "encounters.csv").write_text(
                "Id,START,ENCOUNTERCLASS\nE1,2024-01-01,ambulatory\nE2,2024-01-02,inpatient\n",
                encoding="utf-8",
            )
            (workspace / "docs" / "kpi_registry.csv").write_text(
                "Key business question,Description,Cuts / grain hints,Metric,Data model refinement required\n"
                "How many encounters are recorded?,Count encounters,,count(Id),\n",
                encoding="utf-8",
            )

            # 1. Without a review, the flow stops at the blocking gate (not complete).
            result = WorkspaceFlow(root, "workspaces/demo").start(intent="full_kpi_sql")
            self.assertEqual(result.status, "needs_specialist_review")
            self.assertEqual(result.stage, "kpi_analyst_review")
            session_id = result.session_id
            panel = json.loads((root / result.current_panel_path).read_text(encoding="utf-8"))
            self.assertIn("kpi-analyst", panel["summary"]["required_specialists"])
            self.assertIn("kpi_signature", panel["summary"])

            # 2. A 'blocked' verdict keeps the workflow blocked (does not complete).
            blocked = WorkspaceFlow.from_session(root, session_id).review(
                verdict="blocked", summary="kpi_001 aggregates the wrong column",
            )
            self.assertEqual(blocked.status, "blocked")
            self.assertEqual(blocked.stage, "kpi_analyst_review_blocked")

            # 3. An 'ok' verdict releases the gate and the workflow completes.
            done = WorkspaceFlow.from_session(root, session_id).review(
                verdict="ok", summary="kpi_001 answers its intent",
            )
            self.assertEqual(done.status, "complete")
            panel = json.loads((root / done.current_panel_path).read_text(encoding="utf-8"))
            self.assertEqual(panel["summary"]["kpi_analyst_review"]["verdict"], "ok")

    def test_every_stage_panel_auto_attaches_its_specialist_and_skill_roster(self):
        from core.onboarding.workspace.delegation import routing_for
        from core.onboarding.workspace.flow import _attach_stage_routing

        # A panel that historically carried NO roster now gets the kpi_definition
        # roster attached automatically (every agent + skill that owns the stage).
        panel = {"stage": "kpi_blocker", "summary": {}}
        _attach_stage_routing(panel)
        expected = routing_for("kpi_definition")
        self.assertEqual(
            set(panel["summary"]["required_specialists"]), set(expected["agents"])
        )
        for skill in expected["skills"]:
            self.assertIn(skill, panel["summary"]["suggested_skills"])

        # An unmapped stage attaches nothing (never a wrong roster).
        bare = {"stage": "totally_unknown_stage"}
        _attach_stage_routing(bare)
        self.assertNotIn("summary", bare)

    def test_data_understanding_gate_emits_artifact_with_tier_schema_and_options(self):
        try:
            import duckdb  # noqa: F401
            import polars  # noqa: F401
        except ImportError:
            self.skipTest("duckdb or polars is not installed")

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            workspace = root / "workspaces" / "demo"
            (workspace / "docs").mkdir(parents=True)
            (workspace / "datasets").mkdir()
            (workspace / "datasets" / "encounters.csv").write_text(
                "Id,START,ENCOUNTERCLASS\nE1,2024-01-01,ambulatory\nE2,2024-01-02,inpatient\n",
                encoding="utf-8",
            )
            (workspace / "docs" / "kpi_registry.csv").write_text(
                "Key business question,Description,Cuts / grain hints,Metric,Data model refinement required\n"
                "How many encounters are recorded?,Count encounters,,count(Id),\n",
                encoding="utf-8",
            )

            result = WorkspaceFlow(root, "workspaces/demo").start(intent="full_kpi_sql")

            # Existing full_kpi_sql end-to-end behavior still completes once the
            # kpi-analyst review gate is satisfied.
            self.assertEqual(result.status, "needs_specialist_review")
            result = WorkspaceFlow.from_session(root, result.session_id).review(
                verdict="ok", summary="reviewed",
            )
            self.assertEqual(result.status, "complete")

            du_json = workspace / "interns" / "reports" / "data_understanding" / "current.json"
            du_md = workspace / "interns" / "reports" / "data_understanding" / "current.md"
            self.assertTrue(du_json.exists())
            self.assertTrue(du_md.exists())

            payload = json.loads(du_json.read_text(encoding="utf-8"))
            self.assertIn("quality_tier", payload)
            self.assertTrue(payload["quality_tier"]["tier"])
            self.assertTrue(payload["quality_tier"]["evidence"])
            self.assertIn("schema_type", payload)
            self.assertTrue(payload["schema_type"]["schema_type"])
            self.assertTrue(payload["schema_type"]["evidence"])
            option_ids = [o["option_id"] for o in payload["top_level_options"]]
            self.assertEqual(option_ids, ["option_generate", "option_forward"])
            self.assertIsInstance(payload["scoped_processing_options"], list)

            markdown = du_md.read_text(encoding="utf-8")
            self.assertIn("Detected Quality Tier", markdown)
            self.assertIn(payload["quality_tier"]["tier"], markdown)
            self.assertIn("Detected Schema Type", markdown)
            self.assertIn(payload["schema_type"]["schema_type"], markdown)
            self.assertIn("Generate KPI", markdown)
            self.assertIn("Move forward with the current workflow", markdown)
            self.assertIn("Scoped Processing Options", markdown)

    def test_result_preview_requires_exact_result_view(self):
        try:
            import duckdb
        except ImportError:
            self.skipTest("duckdb is not installed")

        conn = duckdb.connect(":memory:")
        try:
            conn.execute('CREATE OR REPLACE VIEW "kpi_001_features" AS SELECT 1 AS value')
            self.assertEqual(_result_view(conn, "kpi_001"), "")
            conn.execute('CREATE OR REPLACE VIEW "kpi_001_results" AS SELECT 1 AS value')
            self.assertEqual(_result_view(conn, "kpi_001"), "kpi_001_results")
        finally:
            conn.close()


class BugFixTests(unittest.TestCase):
    """Regression tests for BUG-013, BUG-014, BUG-016, BUG-019, BUG-020."""

    def _make_simple_workspace(self, root: Path) -> str:
        workspace = root / "workspaces" / "demo"
        (workspace / "docs").mkdir(parents=True)
        (workspace / "datasets").mkdir()
        (workspace / "datasets" / "encounters.csv").write_text(
            "Id,START,ENCOUNTERCLASS\nE1,2024-01-01,ambulatory\nE2,2024-01-02,inpatient\n",
            encoding="utf-8",
        )
        (workspace / "docs" / "kpi_registry.csv").write_text(
            "Key business question,Description,Cuts / grain hints,Metric,Data model refinement required\n"
            "How many encounters are recorded?,Count encounters,,count(Id),\n",
            encoding="utf-8",
        )
        return "workspaces/demo"

    # BUG-020: review --verdict ok should complete on the FIRST valid call.
    def test_bug020_review_ok_completes_on_first_call(self):
        try:
            import duckdb  # noqa: F401
            import polars  # noqa: F401
        except ImportError:
            self.skipTest("duckdb or polars is not installed")

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            ws = self._make_simple_workspace(root)

            result = WorkspaceFlow(root, ws).start(intent="full_kpi_sql")
            self.assertEqual(result.status, "needs_specialist_review")
            session_id = result.session_id

            # First call must complete — no second call required.
            done = WorkspaceFlow.from_session(root, session_id).review(
                verdict="ok", summary="looks good on first call",
            )
            self.assertEqual(done.status, "complete",
                             "BUG-020: first review --verdict ok must complete the workflow")

    # BUG-013: completion output must print the full KPI result packet.
    def test_bug013_completion_cli_emits_kpi_result_packet(self):
        try:
            import duckdb  # noqa: F401
            import polars  # noqa: F401
        except ImportError:
            self.skipTest("duckdb or polars is not installed")

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            ws = self._make_simple_workspace(root)

            result = WorkspaceFlow(root, ws).start(intent="full_kpi_sql")
            result = WorkspaceFlow.from_session(root, result.session_id).review(
                verdict="ok", summary="all good",
            )
            self.assertEqual(result.status, "complete")

            stdout = io.StringIO()
            with contextlib.redirect_stdout(stdout):
                exit_code = workspace_flow_main(
                    ["--repo-root", str(root), "results", "--session", result.session_id]
                )
            output = stdout.getvalue()
            self.assertEqual(exit_code, 0)
            # BUG-013 / BUG-016: the full result packet must be printed
            self.assertIn("KPI Result Packet", output)
            self.assertIn("kpi_001", output)

    # BUG-016: results --session must always emit the full packet (not pointer-only).
    def test_bug016_results_subcommand_renders_full_packet(self):
        try:
            import duckdb  # noqa: F401
            import polars  # noqa: F401
        except ImportError:
            self.skipTest("duckdb or polars is not installed")

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            ws = self._make_simple_workspace(root)

            result = WorkspaceFlow(root, ws).start(intent="full_kpi_sql")
            result = WorkspaceFlow.from_session(root, result.session_id).review(
                verdict="ok", summary="reviewed",
            )
            self.assertEqual(result.status, "complete")

            stdout = io.StringIO()
            with contextlib.redirect_stdout(stdout):
                exit_code = workspace_flow_main(
                    ["--repo-root", str(root), "results", "--session", result.session_id]
                )
            output = stdout.getvalue()
            self.assertEqual(exit_code, 0)
            self.assertIn("KPI Result Packet", output,
                          "BUG-016: results subcommand must print the full KPI result packet")
            # Must contain SQL or result-table content, not just a path pointer.
            self.assertIn("kpi_001", output)
            # Must NOT be pointer-only (just an artifact path without content).
            self.assertNotIn("Review generated KPI SQL and result previews.\n\n## Next Step", output[:500])

    # BUG-014: review records provenance (agent vs human).
    def test_bug014_review_records_agent_provenance_by_default(self):
        try:
            import duckdb  # noqa: F401
            import polars  # noqa: F401
        except ImportError:
            self.skipTest("duckdb or polars is not installed")

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            ws = self._make_simple_workspace(root)

            result = WorkspaceFlow(root, ws).start(intent="full_kpi_sql")
            done = WorkspaceFlow.from_session(root, result.session_id).review(
                verdict="ok", summary="agent check",
            )
            panel = json.loads((root / done.current_panel_path).read_text(encoding="utf-8"))
            review = panel["summary"]["kpi_analyst_review"]
            self.assertEqual(review["source"], "agent",
                             "BUG-014: default review source must be 'agent'")
            self.assertEqual(review["confirmed_by"], "")

    def test_bug014_review_records_human_provenance_when_confirmed_by_set(self):
        try:
            import duckdb  # noqa: F401
            import polars  # noqa: F401
        except ImportError:
            self.skipTest("duckdb or polars is not installed")

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            ws = self._make_simple_workspace(root)

            result = WorkspaceFlow(root, ws).start(intent="full_kpi_sql")
            done = WorkspaceFlow.from_session(root, result.session_id).review(
                verdict="ok", summary="human check", confirmed_by="alice",
            )
            panel = json.loads((root / done.current_panel_path).read_text(encoding="utf-8"))
            review = panel["summary"]["kpi_analyst_review"]
            self.assertEqual(review["source"], "human",
                             "BUG-014: confirmed_by must set source to 'human'")
            self.assertEqual(review["confirmed_by"], "alice")

    def test_bug014_confirmed_by_cli_flag_sets_human_source(self):
        try:
            import duckdb  # noqa: F401
            import polars  # noqa: F401
        except ImportError:
            self.skipTest("duckdb or polars is not installed")

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            ws = self._make_simple_workspace(root)

            result = WorkspaceFlow(root, ws).start(intent="full_kpi_sql")
            self.assertEqual(result.status, "needs_specialist_review")

            stdout = io.StringIO()
            with contextlib.redirect_stdout(stdout):
                exit_code = workspace_flow_main([
                    "--repo-root", str(root),
                    "review",
                    "--session", result.session_id,
                    "--verdict", "ok",
                    "--summary", "reviewed by human",
                    "--confirmed-by", "bob",
                ])
            self.assertEqual(exit_code, 0)
            output = stdout.getvalue()
            self.assertIn("human:bob", output,
                          "BUG-014: CLI output must show human provenance")

    # BUG-019: --quiet accepted after the subcommand.
    def test_bug019_quiet_accepted_after_subcommand(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            workspace = root / "workspaces" / "demo"
            workspace.mkdir(parents=True)
            (workspace / "encounters.csv").write_text("Id,START\nE1,2024-01-01\n", encoding="utf-8")

            stdout = io.StringIO()
            try:
                with contextlib.redirect_stdout(stdout):
                    exit_code = workspace_flow_main([
                        "--repo-root", str(root),
                        "status",
                        "--workspace", "workspaces/demo",
                        "--diff",
                        "--quiet",   # after subcommand — this was BUG-019
                    ])
            except SystemExit as exc:
                self.fail(f"BUG-019: --quiet after subcommand raised SystemExit: {exc}")
            # exit_code 0 or any value is fine; the key requirement is it did not crash
            output = stdout.getvalue()
            # quiet mode emits [ok] workflow-diff line
            self.assertIn("[ok] workflow-diff", output)

    def test_bug019_quiet_top_level_still_works(self):
        """Regression: --quiet at top level must keep working."""
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            workspace = root / "workspaces" / "demo"
            workspace.mkdir(parents=True)
            (workspace / "encounters.csv").write_text("Id,START\nE1,2024-01-01\n", encoding="utf-8")

            stdout = io.StringIO()
            try:
                with contextlib.redirect_stdout(stdout):
                    exit_code = workspace_flow_main([
                        "--repo-root", str(root),
                        "--quiet",
                        "status",
                        "--workspace", "workspaces/demo",
                        "--diff",
                    ])
            except SystemExit as exc:
                self.fail(f"BUG-019 regression: --quiet at top level raised SystemExit: {exc}")
            output = stdout.getvalue()
            self.assertIn("[ok] workflow-diff", output)


if __name__ == "__main__":
    unittest.main()
