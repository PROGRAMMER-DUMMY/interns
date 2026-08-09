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


class FailedRunIsNotAnAppliedOpTests(unittest.TestCase):
    """F24: `record_op` fired whenever `fn()` RETURNED, and the cloud-first
    commands report failure as a structured payload (`ok: False`) instead of
    raising -- that is the whole design of the refusal ladder. So a run that
    executed nothing and failed was stamped as applied, and every honest retry
    afterwards came back `idempotent_replay` telling the operator to pass
    `--allow-replay` to redo work that never happened."""

    def _run(self, payload: dict) -> Path:
        from core.onboarding.workspace import cli_runner

        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        root = Path(tmp.name)
        (root / "workspaces" / "demo").mkdir(parents=True)
        cli_runner.run_workspace_command(
            command="run-ingestion",
            workspace="workspaces/demo",
            repo_root=str(root),
            fn=lambda: payload,
            record_idempotent=True,
        )
        return root / "workspaces" / "demo" / "interns" / "state" / "applied_ops.jsonl"

    def test_a_failed_result_is_not_recorded_as_applied(self) -> None:
        log = self._run({"ok": False, "status": "failed", "executed": 0, "failed": 1})
        self.assertFalse(
            log.exists() and log.read_text(encoding="utf-8").strip(),
            "a failed run must never be stamped as an applied op",
        )

    def test_a_successful_result_is_still_recorded(self) -> None:
        log = self._run({"ok": True, "status": "executed", "executed": 12, "failed": 0})
        self.assertTrue(log.exists() and log.read_text(encoding="utf-8").strip())


class OpIdCoversConsumedArtifactTests(unittest.TestCase):
    """F16: all three live `apply-provisioning` runs reported the SAME op_id
    even though `provision_plan.json` had materially changed between them (the
    catalog step gained `storage_root`). The envelope claimed "this exact call
    was already applied" about a call whose PLAN was different. Harmless only
    because replay re-runs `fn()` to refresh; any future path that trusts the
    cache to short-circuit would silently skip a changed plan."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        # ONE stable path, rewritten between calls -- `fingerprint_paths` hashes
        # the path alongside the bytes, which is what a real workspace has.
        self.plan = Path(self._tmp.name) / "provision_plan.json"

    def _op_id_for_plan(self, plan_body: str) -> str:
        from core.onboarding.workspace.idempotency import fingerprint_paths

        self.plan.write_text(plan_body, encoding="utf-8")
        return compute_op_id(
            "apply-provisioning",
            workspace="workspaces/demo",
            dry_run=False,
            plan_fingerprint=fingerprint_paths(self.plan),
        )

    def test_a_changed_plan_yields_a_different_op_id(self) -> None:
        before = self._op_id_for_plan('{"steps": [{"kind": "create_catalog"}]}')
        after = self._op_id_for_plan(
            '{"steps": [{"kind": "create_catalog", "storage_root": "s3://b/"}]}'
        )
        self.assertNotEqual(before, after)

    def test_an_unchanged_plan_still_replays(self) -> None:
        body = '{"steps": [{"kind": "create_catalog"}]}'
        self.assertEqual(self._op_id_for_plan(body), self._op_id_for_plan(body))

    def test_apply_provisioning_actually_passes_a_plan_fingerprint(self) -> None:
        # The seam every other test here mocks away: the CLI must really put
        # the artifact into op_args, or the two tests above prove nothing about
        # the shipped command. (The F19 lesson.)
        import inspect

        from core.provisioning import cli

        source = inspect.getsource(cli.apply_provisioning_main)
        self.assertIn("plan_fingerprint", source)
        self.assertIn("fingerprint_paths", source)

    def test_run_ingestion_fingerprints_its_manifest(self) -> None:
        import inspect

        from core.provisioning import cli

        source = inspect.getsource(cli.run_ingestion_main)
        self.assertIn("fingerprint_paths", source)


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
