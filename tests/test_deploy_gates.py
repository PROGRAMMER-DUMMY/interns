"""The five Databricks deployment gates (PRD section 7).

Each gate is tested in isolation on synthetic fixtures; nothing here touches
a network. The e2e contract: apply-deploy on a workspace with unratified
design decisions and no remote env var must block on G2 + G5 — the refusal
IS the feature.
"""
from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from core.onboarding.databricks.deploy_gates import (
    REMOTE_ENV,
    check_design_ratified,
    check_human_provenance,
    check_local_green,
    check_plan_freshness,
    check_remote_approval,
)


def _ws(tmp: str) -> Path:
    return Path(tmp) / "ws"


def _write(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload), encoding="utf-8")


def _green_run(ws: Path, *, failed: bool = False, degraded: bool = False) -> None:
    _write(
        ws / "interns" / "state" / "medallion" / "runs" / "20260101T000000Z-x" / "run.json",
        {
            "run_id": "20260101T000000Z-x",
            "degraded_run": degraded,
            "per_table_status": {
                "bronze.a": {"status": "failed" if failed else "built"},
                "bronze.b": {"status": "skipped"},
            },
            "kpi_diff": {},
        },
    )


def _harness(ws: Path, *, ok: bool = True) -> None:
    _write(
        ws / "interns" / "generated" / "evidence" / "kpi_execution_harness.json",
        {"ok": ok, "passed_count": 3, "failed_count": 0 if ok else 1},
    )


class G1LocalGreenTest(unittest.TestCase):
    def test_green_run_and_harness_pass(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            ws = _ws(tmp)
            _green_run(ws)
            _harness(ws)
            verdict = check_local_green(ws)
            self.assertTrue(verdict.ok)

    def test_failed_table_blocks(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            ws = _ws(tmp)
            _green_run(ws, failed=True)
            _harness(ws)
            verdict = check_local_green(ws)
            self.assertFalse(verdict.ok)
            self.assertIn("bronze.a", verdict.blocking_reason)

    def test_degraded_run_blocks(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            ws = _ws(tmp)
            _green_run(ws, degraded=True)
            _harness(ws)
            self.assertFalse(check_local_green(ws).ok)

    def test_missing_harness_blocks(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            ws = _ws(tmp)
            _green_run(ws)
            self.assertFalse(check_local_green(ws).ok)

    def test_no_runs_blocks(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            self.assertFalse(check_local_green(_ws(tmp)).ok)

    def test_unlistable_runs_dir_fails_closed(self) -> None:
        # A gate that RAISES is a gate that did not block: the caller sees a
        # traceback, not a refusal. Unreadable run state must read as "not
        # green", never as an exception escaping the check.
        with tempfile.TemporaryDirectory() as tmp:
            ws = _ws(tmp)
            (ws / "interns" / "state" / "medallion" / "runs").mkdir(parents=True)

            def _denied(self):
                raise PermissionError("access is denied")

            with mock.patch.object(Path, "iterdir", _denied):
                verdict = check_local_green(ws)
            self.assertFalse(verdict.ok)
            self.assertIn("cannot list medallion runs", verdict.blocking_reason)


class G2DesignRatifiedTest(unittest.TestCase):
    def test_open_decisions_block_with_names(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            ws = _ws(tmp)
            _write(
                ws / "interns" / "reports" / "medallion_design_panel" / "current.json",
                {"open_count": 2, "items": [{"id": "fact:a"}, {"id": "fact:b"}]},
            )
            verdict = check_design_ratified(ws)
            self.assertFalse(verdict.ok)
            self.assertIn("fact:a", verdict.blocking_reason)

    def test_ratified_panel_passes(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            ws = _ws(tmp)
            _write(
                ws / "interns" / "reports" / "medallion_design_panel" / "current.json",
                {"open_count": 0, "items": []},
            )
            self.assertTrue(check_design_ratified(ws).ok)

    def test_missing_panel_blocks(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            self.assertFalse(check_design_ratified(_ws(tmp)).ok)

    def test_non_numeric_open_count_fails_closed(self) -> None:
        # A malformed panel is not a ratified panel. int("many") raises, and a
        # raising gate blocks nothing.
        with tempfile.TemporaryDirectory() as tmp:
            ws = _ws(tmp)
            _write(
                ws / "interns" / "reports" / "medallion_design_panel" / "current.json",
                {"open_count": "many", "items": [{"id": "fact:a"}]},
            )
            verdict = check_design_ratified(ws)
            self.assertFalse(verdict.ok)
            self.assertIn("open_count", verdict.blocking_reason)


class G3ProvenanceTest(unittest.TestCase):
    def test_empty_confirmer_blocks_as_agent(self) -> None:
        verdict = check_human_provenance("")
        self.assertFalse(verdict.ok)
        self.assertEqual(verdict.evidence["source"], "agent")

    def test_named_human_passes(self) -> None:
        verdict = check_human_provenance("Shubham")
        self.assertTrue(verdict.ok)
        self.assertEqual(verdict.evidence["source"], "human")

    def test_whitespace_is_not_a_name(self) -> None:
        self.assertFalse(check_human_provenance("   ").ok)


class G5RemoteApprovalTest(unittest.TestCase):
    def test_unset_blocks(self) -> None:
        with mock.patch.dict(os.environ, {REMOTE_ENV: ""}):
            self.assertFalse(check_remote_approval().ok)

    def test_set_passes_but_is_never_set_by_the_check(self) -> None:
        with mock.patch.dict(os.environ, {REMOTE_ENV: "1"}):
            self.assertTrue(check_remote_approval().ok)
        with mock.patch.dict(os.environ, {REMOTE_ENV: ""}):
            check_remote_approval()
            self.assertNotEqual(os.environ.get(REMOTE_ENV), "1")


class G4FreshnessTest(unittest.TestCase):
    def test_missing_plan_blocks(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            verdict = check_plan_freshness(_ws(tmp), Path(tmp))
            self.assertFalse(verdict.ok)
            self.assertIn("plan-deploy", verdict.blocking_reason)


class ApplyDeployBoundaryTest(unittest.TestCase):
    def test_blocked_run_records_no_approval(self) -> None:
        from core.medallion.apply_deploy import main

        with tempfile.TemporaryDirectory() as tmp:
            ws = _ws(tmp)
            ws.mkdir(parents=True)
            code = main([
                "--workspace", "ws", "--repo-root", tmp, "--confirmed-by", "",
            ])
            self.assertEqual(code, 1)
            self.assertFalse(
                (ws / "interns" / "state" / "medallion" / "deploy_approval.json").exists()
            )

    def test_dry_run_never_records_even_when_green(self) -> None:
        # All-green is hard to fake fully here; assert dry-run exits without
        # writing regardless of verdicts.
        from core.medallion.apply_deploy import main

        with tempfile.TemporaryDirectory() as tmp:
            ws = _ws(tmp)
            ws.mkdir(parents=True)
            main(["--workspace", "ws", "--repo-root", tmp, "--dry-run"])
            self.assertFalse(
                (ws / "interns" / "state" / "medallion" / "deploy_approval.json").exists()
            )


if __name__ == "__main__":
    unittest.main()
