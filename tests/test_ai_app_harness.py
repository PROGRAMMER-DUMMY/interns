from __future__ import annotations

import json
import tempfile
import unittest
from contextlib import redirect_stderr
from io import StringIO
from pathlib import Path

from core.onboarding.harness.ai_app_harness import AIAppHarness, main


class AIAppHarnessTests(unittest.TestCase):
    def test_local_stub_runs_deterministic_evaluators_and_writes_current(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            workspace = root / "workspaces" / "demo"
            dataset = workspace / "interns" / "ai_harness" / "datasets" / "happy_path.jsonl"
            dataset.parent.mkdir(parents=True)
            _write_jsonl(
                dataset,
                [
                    {
                        "id": "tc_exact",
                        "target": "local_stub",
                        "input": "hello",
                        "stub_output": "Short summary",
                        "expected": "short summary",
                        "eval_type": "exact_match",
                        "tags": ["summary"],
                    },
                    {
                        "id": "tc_schema",
                        "target": "local_stub",
                        "input": "json",
                        "stub_output": '{"name": "Ada", "age": 37}',
                        "eval_type": "schema_check",
                        "expected_keys": ["name", "age"],
                        "tags": ["structured"],
                    },
                    {
                        "id": "tc_keyword",
                        "target": "local_stub",
                        "input": "list",
                        "stub_output": "Cost and speed both matter.",
                        "eval_type": "keyword",
                        "keywords": ["cost", "speed"],
                        "tags": ["content"],
                    },
                ],
            )

            result = AIAppHarness(root, "workspaces/demo", dataset=dataset).run()

            self.assertTrue(result.ok)
            self.assertEqual(result.total, 3)
            self.assertEqual(result.passed, 3)
            current = workspace / "interns" / "reports" / "ai_app_harness" / "current.json"
            current_md = workspace / "interns" / "reports" / "ai_app_harness" / "current.md"
            self.assertTrue(current.exists())
            self.assertTrue(current_md.exists())
            payload = json.loads(current.read_text(encoding="utf-8"))
            self.assertEqual(payload["summary"]["pass_rate"], 1.0)

    def test_tag_filter_only_runs_matching_cases(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            workspace = root / "workspaces" / "demo"
            dataset = workspace / "interns" / "ai_harness" / "datasets" / "cases.jsonl"
            dataset.parent.mkdir(parents=True)
            _write_jsonl(
                dataset,
                [
                    {
                        "id": "tc_a",
                        "input": "a",
                        "stub_output": "a",
                        "expected": "a",
                        "eval_type": "exact_match",
                        "tags": ["run"],
                    },
                    {
                        "id": "tc_b",
                        "input": "b",
                        "stub_output": "wrong",
                        "expected": "b",
                        "eval_type": "exact_match",
                        "tags": ["skip"],
                    },
                ],
            )

            result = AIAppHarness(root, "workspaces/demo", dataset=dataset, tags=["run"]).run()

            self.assertTrue(result.ok)
            self.assertEqual(result.total, 1)
            self.assertEqual(result.passed, 1)

    def test_remote_ai_case_is_blocked_without_approval(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            workspace = root / "workspaces" / "demo"
            dataset = workspace / "interns" / "ai_harness" / "datasets" / "remote.jsonl"
            dataset.parent.mkdir(parents=True)
            _write_jsonl(
                dataset,
                [
                    {
                        "id": "tc_remote",
                        "target": "http_ai",
                        "input": "call model",
                        "expected": "anything",
                        "eval_type": "exact_match",
                    }
                ],
            )

            result = AIAppHarness(root, "workspaces/demo", dataset=dataset).run()

            self.assertFalse(result.ok)
            self.assertEqual(result.blocked, 1)
            current = json.loads(
                (workspace / "interns" / "reports" / "ai_app_harness" / "current.json").read_text(
                    encoding="utf-8"
                )
            )
            self.assertEqual(current["records"][0]["status"], "blocked_remote_ai")

    def test_baseline_passing_case_now_failing_blocks(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            workspace = root / "workspaces" / "demo"
            dataset = workspace / "interns" / "ai_harness" / "datasets" / "cases.jsonl"
            baseline = workspace / "interns" / "ai_harness" / "baseline.json"
            dataset.parent.mkdir(parents=True)
            _write_jsonl(
                dataset,
                [
                    {
                        "id": "tc_regressed",
                        "input": "a",
                        "stub_output": "wrong",
                        "expected": "a",
                        "eval_type": "exact_match",
                    }
                ],
            )
            baseline.write_text(
                json.dumps({"records": [{"id": "tc_regressed", "passed": True, "score": 1.0}]}),
                encoding="utf-8",
            )

            result = AIAppHarness(root, "workspaces/demo", dataset=dataset, baseline_path=baseline).run()

            self.assertFalse(result.ok)
            self.assertEqual(result.baseline_regressions, 1)

    def test_kpi_reliability_evaluators_check_mapping_sql_and_results(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            workspace = root / "workspaces" / "demo"
            dataset = workspace / "interns" / "ai_harness" / "datasets" / "kpi_reliability.jsonl"
            dataset.parent.mkdir(parents=True)
            mapping_output = {
                "mappings": [
                    {
                        "feature": "PaidAmount",
                        "column": "PaidAmount",
                        "dataset": "transactions.csv",
                        "state": "proven_direct",
                        "grain": "transaction",
                    },
                    {
                        "feature": "Age",
                        "column": "DOB",
                        "dataset": "patients.csv",
                        "state": "user_confirmed",
                    },
                ]
            }
            result_output = {
                "columns": ["month", "gender", "paid_amount"],
                "row_count": 2,
                "rows": [
                    {"month": "2026-01-01", "gender": "F", "paid_amount": 125.5},
                    {"month": "2026-01-01", "gender": "M", "paid_amount": 90.0},
                ],
            }
            _write_jsonl(
                dataset,
                [
                    {
                        "id": "map_paid_amount",
                        "input": "map kpi",
                        "stub_output": json.dumps(mapping_output),
                        "eval_type": "kpi_mapping",
                        "expected_mappings": [
                            {
                                "feature": "PaidAmount",
                                "column": "PaidAmount",
                                "dataset": "transactions.csv",
                                "state": "proven_direct",
                                "grain": "transaction",
                            },
                            {"feature": "Age", "column": "DOB", "dataset": "patients.csv"},
                        ],
                        "tags": ["kpi_mapping"],
                    },
                    {
                        "id": "sql_paid_amount",
                        "input": "generate sql",
                        "stub_output": (
                            "SELECT date_trunc('month', ServiceDate) AS month, Gender, "
                            "SUM(PaidAmount) AS paid_amount FROM transactions "
                            "JOIN patients ON transactions.PatientID = patients.PatientID "
                            "WHERE LineOfBusiness = 'Medicare' AND age > 50 GROUP BY 1, 2"
                        ),
                        "eval_type": "sql_semantic",
                        "required_clauses": ["select", "group by"],
                        "required_tables": ["transactions", "patients"],
                        "required_columns": ["PaidAmount", "ServiceDate", "Gender"],
                        "required_joins": ["PatientID"],
                        "required_filters": ["LineOfBusiness = 'Medicare'", "age > 50"],
                        "forbidden_patterns": ["drop table", "delete from"],
                        "tags": ["sql_semantic", "adversarial"],
                    },
                    {
                        "id": "result_paid_amount",
                        "input": "execute sql",
                        "stub_output": json.dumps(result_output),
                        "eval_type": "result_table",
                        "expected_columns": ["month", "gender", "paid_amount"],
                        "exact_columns": True,
                        "min_rows": 2,
                        "expected_values": [
                            {
                                "label": "female_jan_paid",
                                "row": {"gender": "F"},
                                "column": "paid_amount",
                                "value": 125.5,
                            }
                        ],
                        "tags": ["result_regression"],
                    },
                ],
            )

            result = AIAppHarness(root, "workspaces/demo", dataset=dataset).run()

            self.assertTrue(result.ok)
            self.assertEqual(result.total, 3)
            current = json.loads(
                (workspace / "interns" / "reports" / "ai_app_harness" / "current.json").read_text(
                    encoding="utf-8"
                )
            )
            self.assertEqual(current["coverage"]["reliability_checks"]["kpi_mapping"], 1)
            self.assertEqual(current["coverage"]["reliability_checks"]["sql_semantics"], 1)
            self.assertEqual(current["coverage"]["reliability_checks"]["result_regression"], 1)
            self.assertEqual(current["coverage"]["reliability_checks"]["adversarial"], 1)

    def test_result_table_signature_regression_blocks_even_when_assertions_pass(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            workspace = root / "workspaces" / "demo"
            dataset = workspace / "interns" / "ai_harness" / "datasets" / "result.jsonl"
            baseline = workspace / "interns" / "ai_harness" / "baseline.json"
            dataset.parent.mkdir(parents=True)
            _write_jsonl(
                dataset,
                [
                    {
                        "id": "result_paid_amount",
                        "input": "execute sql",
                        "stub_output": json.dumps(
                            {
                                "columns": ["month", "paid_amount"],
                                "row_count": 1,
                                "rows": [{"month": "2026-01-01", "paid_amount": 101.0}],
                            }
                        ),
                        "eval_type": "result_table",
                        "expected_columns": ["month", "paid_amount"],
                        "min_rows": 1,
                        "expected_values": [
                            {
                                "label": "jan_paid",
                                "row": 0,
                                "column": "paid_amount",
                                "value": 101.0,
                            }
                        ],
                    }
                ],
            )
            baseline.write_text(
                json.dumps(
                    {
                        "records": [
                            {
                                "id": "result_paid_amount",
                                "passed": True,
                                "score": 1.0,
                                "details": {
                                    "result_signature": {
                                        "columns": ["month", "paid_amount"],
                                        "row_count": 1,
                                        "pinned_values": [
                                            {
                                                "label": "jan_paid",
                                                "column": "paid_amount",
                                                "row": 0,
                                                "value": 100.0,
                                            }
                                        ],
                                    }
                                },
                            }
                        ]
                    }
                ),
                encoding="utf-8",
            )

            result = AIAppHarness(root, "workspaces/demo", dataset=dataset, baseline_path=baseline).run()

            self.assertFalse(result.ok)
            self.assertEqual(result.baseline_regressions, 1)

    def test_dataset_must_be_workspace_scoped(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "workspaces" / "demo").mkdir(parents=True)
            dataset = root / "outside.jsonl"
            dataset.write_text('{"id": "tc", "input": "x"}\n', encoding="utf-8")
            with self.assertRaises(ValueError):
                AIAppHarness(root, "workspaces/demo", dataset=dataset).run()

    def test_cli_requires_workspace_and_dataset(self):
        with redirect_stderr(StringIO()), self.assertRaises(SystemExit):
            main([])


def _write_jsonl(path: Path, rows: list[dict]) -> None:
    path.write_text("\n".join(json.dumps(row) for row in rows) + "\n", encoding="utf-8")


if __name__ == "__main__":
    unittest.main()
