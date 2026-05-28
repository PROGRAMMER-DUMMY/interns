"""Tests for the idempotency stamp helper."""

from __future__ import annotations

import json
import tempfile
import time
import unittest
from pathlib import Path

from core.onboarding.workspace.idempotency import (
    AppliedOp,
    compute_op_id,
    get_applied_op,
    is_duplicate_op,
    list_applied_ops,
    record_op,
)


class ComputeOpIdTests(unittest.TestCase):
    def test_compute_op_id_stable_across_calls(self) -> None:
        a = compute_op_id("apply-kpi-panel-answer", workspace="ws1", answer="option_a")
        b = compute_op_id("apply-kpi-panel-answer", workspace="ws1", answer="option_a")
        self.assertEqual(a, b)
        self.assertEqual(len(a), 16)

    def test_compute_op_id_changes_with_args(self) -> None:
        a = compute_op_id("apply-kpi-panel-answer", workspace="ws1", answer="option_a")
        b = compute_op_id("apply-kpi-panel-answer", workspace="ws1", answer="option_b")
        c = compute_op_id("apply-kpi-panel-answer", workspace="ws2", answer="option_a")
        d = compute_op_id("apply-data-model-answer", workspace="ws1", answer="option_a")
        self.assertNotEqual(a, b)
        self.assertNotEqual(a, c)
        self.assertNotEqual(a, d)


class RecordOpTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.workspace = Path(self._tmp.name)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def _log_path(self) -> Path:
        return self.workspace / "interns" / "state" / "applied_ops.jsonl"

    def test_record_op_first_time_returns_true(self) -> None:
        op_id = compute_op_id("cmd", "x")
        self.assertTrue(
            record_op(
                self.workspace,
                op_id=op_id,
                command="apply-kpi-panel-answer",
                payload={"answer": "option_a"},
            )
        )
        self.assertTrue(self._log_path().exists())

    def test_record_op_duplicate_returns_false(self) -> None:
        op_id = compute_op_id("cmd", "x")
        first = record_op(
            self.workspace,
            op_id=op_id,
            command="apply-kpi-panel-answer",
            payload={"answer": "option_a"},
        )
        second = record_op(
            self.workspace,
            op_id=op_id,
            command="apply-kpi-panel-answer",
            payload={"answer": "option_a"},
        )
        self.assertTrue(first)
        self.assertFalse(second)
        with self._log_path().open("r", encoding="utf-8") as handle:
            lines = [line for line in handle if line.strip()]
        self.assertEqual(len(lines), 1)
        record = json.loads(lines[0])
        self.assertEqual(record["op_id"], op_id)

    def test_is_duplicate_op_consistent(self) -> None:
        op_id = compute_op_id("cmd", "x")
        self.assertFalse(is_duplicate_op(self.workspace, op_id))
        record_op(
            self.workspace,
            op_id=op_id,
            command="apply-kpi-panel-answer",
            payload={"answer": "option_a"},
        )
        self.assertTrue(is_duplicate_op(self.workspace, op_id))

    def test_get_applied_op_returns_payload(self) -> None:
        op_id = compute_op_id("cmd", "x")
        record_op(
            self.workspace,
            op_id=op_id,
            command="apply-kpi-panel-answer",
            payload={"answer": "option_a"},
        )
        record = get_applied_op(self.workspace, op_id)
        self.assertIsNotNone(record)
        assert record is not None  # for type-checkers
        self.assertIsInstance(record, AppliedOp)
        self.assertEqual(record.op_id, op_id)
        self.assertEqual(record.command, "apply-kpi-panel-answer")
        self.assertEqual(record.payload, {"answer": "option_a"})
        self.assertIsNone(get_applied_op(self.workspace, "deadbeefdeadbeef"))

    def test_list_applied_ops_filters_by_command(self) -> None:
        record_op(
            self.workspace,
            op_id=compute_op_id("a"),
            command="apply-kpi-panel-answer",
            payload={"answer": "option_a"},
        )
        record_op(
            self.workspace,
            op_id=compute_op_id("b"),
            command="apply-data-model-answer",
            payload={"answer": "option_b"},
        )
        record_op(
            self.workspace,
            op_id=compute_op_id("c"),
            command="apply-kpi-panel-answer",
            payload={"answer": "option_c"},
        )
        filtered = list_applied_ops(
            self.workspace, command="apply-kpi-panel-answer"
        )
        self.assertEqual(len(filtered), 2)
        for op in filtered:
            self.assertEqual(op.command, "apply-kpi-panel-answer")
        all_ops = list_applied_ops(self.workspace)
        self.assertEqual(len(all_ops), 3)

    def test_list_applied_ops_newest_first(self) -> None:
        ids = []
        for label in ("first", "second", "third"):
            op_id = compute_op_id(label)
            ids.append(op_id)
            record_op(
                self.workspace,
                op_id=op_id,
                command="apply-kpi-panel-answer",
                payload={"label": label},
            )
            # Force distinct applied_at timestamps for clarity.
            time.sleep(0.001)
        ordered = list_applied_ops(self.workspace)
        self.assertEqual([op.op_id for op in ordered], list(reversed(ids)))

        limited = list_applied_ops(self.workspace, limit=2)
        self.assertEqual(len(limited), 2)
        self.assertEqual(limited[0].op_id, ids[-1])


if __name__ == "__main__":
    unittest.main()
