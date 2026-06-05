"""Phase 4 -- end-to-end proof of the generic NL-question -> KPI SQL chain.

This drives the WHOLE chain on a natural-language-question workspace (the real
trigger that exposed the non-genericity bug): a workspace whose only KPI source
is a comment-only ``.sql`` file of plain English questions, with NO authored
metric/cuts.

    docs/*.sql (NL questions)  --interview-->  derive metric/cuts (Phase 1)
        --finalize-->  docs/kpi_registry.generated.json (populated)
        --full_kpi_sql onboard-->  SQL generation + DuckDB execution
        --kpi-analyst review-->  aggregated result tables (realized cuts)

The point is to PROVE empirically that:

1. The interview path derives a measurable ``metric`` + ``cuts`` from the English
   question plus the workspace's OWN profiled evidence (no domain vocabulary).
2. ``full_kpi_sql`` consumes that derived registry and emits AGGREGATED SQL --
   the result collapses many raw rows into a few grouped rows, with the derived
   cut column(s) actually present in the output (not a raw row dump).

Evidence is synthetic and non-healthcare (orders) to keep the proof
workspace-agnostic. Requires duckdb + polars; skips cleanly without them.
"""
from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from core.onboarding.kpi.generation_workflow import KPIGenerationWorkflow
from core.onboarding.workspace.flow import WorkspaceFlow

# orders.csv: 6 orders across 3 months and 2 regions. Chosen so an aggregated
# answer collapses 6 raw rows into a handful of grouped rows -- the raw-vs-
# aggregated distinction is visible in the row count.
_ORDERS_CSV = (
    "order_id,order_date,order_value,region\n"
    "1,2021-01-05,100.0,North\n"
    "2,2021-01-12,50.0,South\n"
    "3,2021-02-03,75.0,North\n"
    "4,2021-02-20,200.0,South\n"
    "5,2021-03-08,30.0,North\n"
    "6,2021-03-15,120.0,South\n"
)

# Comment-only NL questions -- no metric, no cuts. This is the workspace shape
# that broke the platform before the generic derivation existed.
_ORDERS_QUESTIONS_SQL = (
    "-- ANALYTICS OBJECTIVE: understand order volume and value\n"
    "--\n"
    "-- How many orders were placed each month?\n"
    "--\n"
    "-- What is the average order value by region?\n"
)

# The profile evidence the interview's derivation reads. This is exactly the
# shape onboarding writes to interns/generated/profiles/profile_index.json;
# we seed it because the derivation runs at interview time, before full_kpi_sql
# re-profiles the same dataset. (Seeding profile evidence directly is the same
# convention the metric_derivation wiring tests use.)
_PROFILE_INDEX = {
    "profiles": [
        {
            "path": "datasets/orders.csv",
            "row_count": 6,
            "schema": {
                "order_id": "Int64",
                "order_date": "Date",
                "order_value": "Float64",
                "region": "String",
            },
            "columns": [
                {"name": "order_id", "n_unique": 6, "sample_values": ["1", "2", "3"]},
                {"name": "order_date", "n_unique": 6, "sample_values": ["2021-01-05"]},
                {"name": "order_value", "n_unique": 6, "sample_values": ["100.0"]},
                {"name": "region", "n_unique": 2, "sample_values": ["North", "South"]},
            ],
        }
    ]
}


class NLQuestionToKpiSqlChainTest(unittest.TestCase):
    def setUp(self) -> None:
        try:
            import duckdb  # noqa: F401
            import polars  # noqa: F401
        except ImportError:
            self.skipTest("duckdb or polars is not installed")

    def _build_nl_workspace(self, root: Path) -> Path:
        workspace = root / "workspaces" / "orders_nl"
        (workspace / "docs").mkdir(parents=True)
        (workspace / "datasets").mkdir()
        (workspace / "datasets" / "orders.csv").write_text(_ORDERS_CSV, encoding="utf-8")
        (workspace / "docs" / "orders_questions.sql").write_text(
            _ORDERS_QUESTIONS_SQL, encoding="utf-8"
        )
        # Seed the profile evidence the interview derivation consumes.
        profiles_dir = workspace / "interns" / "generated" / "profiles"
        profiles_dir.mkdir(parents=True)
        (profiles_dir / "profile_index.json").write_text(
            json.dumps(_PROFILE_INDEX, indent=2), encoding="utf-8"
        )
        return workspace

    def _run_interview_to_generated_registry(self, root: Path) -> dict:
        """Drive the real generation interview to finalize, returning the
        populated generated registry payload."""
        workflow = KPIGenerationWorkflow(root, "workspaces/orders_nl")
        workflow.prepare()
        workflow.apply_answer(answer="option_a")  # route_selection -> context_intake
        workflow.apply_answer(answer="option_b")  # context_intake -> orientation
        workflow.apply_answer(answer="option_c")  # orientation -> format_selection
        workflow.apply_answer(answer="option_c")  # format_selection -> result_format (builds drafts)
        workflow.apply_answer(answer="option_a")  # result_format -> final_preview
        finalized = workflow.finalize(approve_final_preview=True)
        registry_path = root / finalized.output_registry_path
        self.assertTrue(registry_path.exists(), "interview must write a generated registry")
        return json.loads(registry_path.read_text(encoding="utf-8"))

    def test_nl_questions_derive_metric_cuts_then_full_kpi_sql_aggregates(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            workspace = self._build_nl_workspace(root)

            # --- Stage 1: interview derives metric/cuts from the NL questions. ---
            registry = self._run_interview_to_generated_registry(root)
            by_q = {k["business_question"]: k for k in registry["kpis"]}

            count_kpi = by_q["How many orders were placed each month?"]
            self.assertEqual(count_kpi["metric"], "count(distinct order_id)")
            self.assertEqual(count_kpi["cuts"], "month(order_date)")

            avg_kpi = by_q["What is the average order value by region?"]
            self.assertEqual(avg_kpi["metric"], "avg(order_value)")
            self.assertEqual(avg_kpi["cuts"], "region")

            # The generated registry must now be the sole KPI source: remove the
            # raw NL .sql so full_kpi_sql onboards off the populated registry, not
            # the empty questions.
            (workspace / "docs" / "orders_questions.sql").unlink()

            # --- Stage 2: full_kpi_sql onboards off the derived registry. ---
            result = WorkspaceFlow(root, "workspaces/orders_nl").start(intent="full_kpi_sql")
            # Definition is complete now (derived), so the flow advances to the
            # kpi-analyst review gate rather than the kpi_definition_incomplete gate.
            self.assertEqual(
                result.stage,
                "kpi_analyst_review",
                f"expected review gate, got stage={result.stage} status={result.status}",
            )
            result = WorkspaceFlow.from_session(root, result.session_id).review(
                verdict="ok", summary="derived KPIs answer their NL questions",
            )
            self.assertEqual(result.status, "complete")

            # --- Stage 3: results are AGGREGATED with the realized cuts. ---
            result_md = (
                workspace / "interns" / "reports" / "kpi_results" / "current.md"
            ).read_text(encoding="utf-8")
            harness = json.loads(
                (
                    workspace
                    / "interns"
                    / "generated"
                    / "evidence"
                    / "kpi_execution_harness.json"
                ).read_text(encoding="utf-8")
            )
            self.assertTrue(harness["ok"], f"execution harness failed: {harness}")
            self.assertIn("kpi_001", result_md)
            self.assertIn("kpi_002", result_md)

            solutions = workspace / "interns" / "generated" / "solutions"
            kpi_001_sql = (solutions / "kpi_001.sql").read_text(encoding="utf-8")
            kpi_002_sql = (solutions / "kpi_002.sql").read_text(encoding="utf-8")

            # Both KPIs must AGGREGATE (GROUP BY the realized cut), not dump raw rows.
            self.assertIn("GROUP BY", kpi_001_sql)
            self.assertIn("GROUP BY", kpi_002_sql)
            # The derived count metric -> COUNT(DISTINCT ...); the time cut -> a
            # month bucket over the (cast) date column.
            self.assertIn("COUNT(DISTINCT", kpi_001_sql.upper())
            self.assertIn("date_trunc('month'", kpi_001_sql)
            # The derived average metric -> AVG grouped by the categorical cut.
            self.assertIn("AVG(", kpi_002_sql.upper())
            self.assertIn('GROUP BY "region"', kpi_002_sql)

            # Executed output proves the rows collapsed: 6 raw orders -> 3 monthly
            # buckets (2 distinct orders each) for kpi_001; -> 2 region rows for
            # kpi_002. Assert the realized cut columns + grouped values are present
            # and a raw per-order dump is NOT.
            self.assertIn("month", result_md)
            self.assertIn("count_distinct_order_id", result_md)
            self.assertIn("region", result_md)
            self.assertIn("avg_order_value", result_md)
            for region_value in ("North", "South"):
                self.assertIn(region_value, result_md)
            # Deterministic grouped averages (North=(100+75+30)/3, South=(50+200+120)/3).
            self.assertIn("68.33", result_md)
            self.assertIn("123.33", result_md)

            # The dated runs/<date>/ snapshot mirrors the live results exactly.
            from datetime import date

            runs_dir = workspace / "interns" / "runs" / date.today().isoformat()
            self.assertEqual(
                (runs_dir / "results.md").read_text(encoding="utf-8"),
                result_md,
            )

    def test_unsure_top_n_question_defers_to_definition_gate(self) -> None:
        """Ask-when-unsure, proven end-to-end. A top-N ranking can't be derived
        into an executable metric/grain safely, so the interview leaves it empty
        and full_kpi_sql routes it to the kpi_definition_incomplete gate -- never
        an executable-but-wrong ranking. This is the Phase 1 (derive) + Phase 0
        (gate) cooperation."""
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            workspace = root / "workspaces" / "orders_nl"
            (workspace / "docs").mkdir(parents=True)
            (workspace / "datasets").mkdir()
            (workspace / "datasets" / "orders.csv").write_text(_ORDERS_CSV, encoding="utf-8")
            (workspace / "docs" / "orders_questions.sql").write_text(
                "-- ANALYTICS OBJECTIVE: ranking\n--\n-- Top 3 regions by total order value?\n",
                encoding="utf-8",
            )
            profiles_dir = workspace / "interns" / "generated" / "profiles"
            profiles_dir.mkdir(parents=True)
            (profiles_dir / "profile_index.json").write_text(
                json.dumps(_PROFILE_INDEX, indent=2), encoding="utf-8"
            )

            registry = self._run_interview_to_generated_registry(root)
            ranking = registry["kpis"][0]
            # Derivation deliberately left it undefined for the panel.
            self.assertEqual(ranking["metric"], "")
            self.assertEqual(ranking["cuts"], "")

            (workspace / "docs" / "orders_questions.sql").unlink()

            result = WorkspaceFlow(root, "workspaces/orders_nl").start(intent="full_kpi_sql")
            # No fabrication, no crash, no executable-but-wrong SQL: the flow stops
            # at the definition gate and asks.
            self.assertEqual(result.stage, "kpi_definition_incomplete")
            self.assertEqual(result.status, "blocked")


if __name__ == "__main__":
    unittest.main()
