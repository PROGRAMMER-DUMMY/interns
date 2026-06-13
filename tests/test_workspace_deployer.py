"""Medallion Unity Catalog deployer (PRD section 9 seam).

Contract under test (no network anywhere; the Databricks client is a fake):

- REFUSES without an approval artifact (the refusal IS the feature).
- REFUSES on a stale plan: G4 (plan hash vs manifest on disk) is re-verified
  at execution time, as is the approval-plan hash binding.
- REFUSES live apply when the G5 env re-check fails at execution time.
- Without the human-set env var the deployer runs in dry-run / plan-echo mode
  and says so; no api object is ever touched.
- Emits the correct ordered UC action sequence from a fixture deploy_plan.json
  and routes execute/upload/deferred actions correctly on live apply.
"""
from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from core.onboarding.databricks.deploy_gates import GATE_NAMES, REMOTE_ENV
from core.onboarding.databricks.workspace_deployer import (
    build_unity_catalog_actions,
    deploy_medallion_from_approval,
    medallion_deploy_main,
    verify_deploy_approval,
)

WS = "ws"
HASH = "hash123"


class FakeApi:
    """Records calls; never touches a network."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, str]] = []

    def execute_sql(self, statement: str) -> None:
        self.calls.append(("sql", statement))

    def upload_volume_file(self, source: Path, volume_path: str) -> None:
        self.calls.append(("upload", volume_path))


def _write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def _write_json(path: Path, payload: dict) -> None:
    _write(path, json.dumps(payload, indent=2))


def _manifest_yaml(inputs_hash: str = HASH) -> str:
    return (
        "schema_version: 1\n"
        f"workspace: {WS}\n"
        "generated_at: '2026-06-12T00:00:00+00:00'\n"
        f"inputs_hash: {inputs_hash}\n"
        "target: auto\n"
    )


def _plan(inputs_hash: str = HASH) -> dict:
    catalog = "ws_demo"
    return {
        "schema_version": 1,
        "plan_kind": "medallion_databricks_deployment",
        "workspace": WS,
        "source_manifest": {
            "path": f"{WS}/interns/generated/medallion/manifest.yaml",
            "inputs_hash": inputs_hash,
        },
        "unity_catalog": {
            "catalog": catalog,
            "schemas": {"bronze": "bronze", "silver": "silver", "gold": "gold", "ops": "ops"},
            "volumes": [
                {
                    "name": "raw_sources",
                    "schema": "bronze",
                    "volume_path": f"/Volumes/{catalog}/bronze/raw_sources",
                    "uploads": [
                        {
                            "source_file": f"{WS}/data/orders.csv",
                            "target_path": f"/Volumes/{catalog}/bronze/raw_sources/{WS}/data/orders.csv",
                        }
                    ],
                }
            ],
            "tables": [
                {
                    "layer": "bronze",
                    "name": "orders__src",
                    "fqn": f"{catalog}.bronze.orders__src",
                    "format": "DELTA",
                    "load_strategy": "full",
                    "source_file": f"{WS}/data/orders.csv",
                },
                {
                    "layer": "silver",
                    "name": "orders",
                    "fqn": f"{catalog}.silver.orders",
                    "format": "DELTA",
                    "load_strategy": "merge",
                },
                {
                    "layer": "gold",
                    "name": "kpi_x_gold",
                    "fqn": f"{catalog}.gold.kpi_x_gold",
                    "format": "DELTA",
                    "load_strategy": "full",
                },
            ],
        },
        "orchestration": {
            "incremental": {"remote_manifest_table": f"{catalog}.ops.refresh_manifest"},
        },
        "validation": {"errors": [], "valid": True},
    }


def _approval(plan_hash: str = HASH, **overrides) -> dict:
    approval = {
        "artifact_type": "medallion/deploy_approval.json",
        "version": 1,
        "workspace": WS,
        "approved_at": "2026-06-12T00:00:00+00:00",
        "confirmed_by": "Tester",
        "source": "human",
        "plan_inputs_hash": plan_hash,
        "plan_path": f"{WS}/interns/generated/medallion/deploy_plan.json",
        "gates": [{"gate": name, "ok": True, "evidence": {}} for name in GATE_NAMES],
    }
    approval.update(overrides)
    return approval


def _fixture_repo(tmp: str, *, with_approval: bool = True, manifest_hash: str = HASH,
                  approval_overrides: dict | None = None) -> Path:
    repo = Path(tmp)
    ws = repo / WS
    _write(ws / "interns" / "generated" / "medallion" / "manifest.yaml",
           _manifest_yaml(manifest_hash))
    _write_json(ws / "interns" / "generated" / "medallion" / "deploy_plan.json", _plan())
    _write(ws / "data" / "orders.csv", "id,amount\n1,10\n")
    if with_approval:
        _write_json(
            ws / "interns" / "state" / "medallion" / "deploy_approval.json",
            _approval(**(approval_overrides or {})),
        )
    return repo


class RefusalWithoutApprovalTest(unittest.TestCase):
    def test_refuses_and_touches_nothing(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = _fixture_repo(tmp, with_approval=False)
            api = FakeApi()
            outcome = deploy_medallion_from_approval(repo, WS, api=api)
            self.assertEqual(outcome.mode, "refused")
            self.assertTrue(any("apply-deploy" in r for r in outcome.refusal_reasons))
            self.assertEqual(api.calls, [])
            self.assertFalse(
                (repo / WS / "interns" / "state" / "medallion" / "deploy_execution.json").exists()
            )

    def test_cli_exit_code_is_2(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = _fixture_repo(tmp, with_approval=False)
            code = medallion_deploy_main(["--workspace", WS, "--repo-root", str(repo)])
            self.assertEqual(code, 2)


class StaleApprovalRefusalTest(unittest.TestCase):
    def test_g4_recheck_blocks_stale_manifest(self) -> None:
        # Manifest changed on disk AFTER plan + approval were recorded.
        with tempfile.TemporaryDirectory() as tmp:
            repo = _fixture_repo(tmp, manifest_hash="hash999")
            api = FakeApi()
            outcome = deploy_medallion_from_approval(repo, WS, api=api)
            self.assertEqual(outcome.mode, "refused")
            self.assertTrue(any("G4 re-check failed" in r for r in outcome.refusal_reasons))
            self.assertEqual(api.calls, [])

    def test_approval_plan_binding_mismatch_blocks(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = _fixture_repo(tmp, approval_overrides={"plan_inputs_hash": "other"})
            outcome = deploy_medallion_from_approval(repo, WS)
            self.assertEqual(outcome.mode, "refused")
            self.assertTrue(any("binding" in r for r in outcome.refusal_reasons))

    def test_consumed_approval_blocks(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = _fixture_repo(
                tmp, approval_overrides={"consumed_at": "2026-06-12T01:00:00+00:00"}
            )
            outcome = deploy_medallion_from_approval(repo, WS)
            self.assertEqual(outcome.mode, "refused")
            self.assertTrue(any("already consumed" in r for r in outcome.refusal_reasons))

    def test_agent_asserted_approval_blocks(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = _fixture_repo(
                tmp, approval_overrides={"source": "agent", "confirmed_by": ""}
            )
            outcome = deploy_medallion_from_approval(repo, WS)
            self.assertEqual(outcome.mode, "refused")
            self.assertTrue(any("human-attributed" in r for r in outcome.refusal_reasons))

    def test_failing_recorded_gate_blocks(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            gates = [{"gate": name, "ok": name != "G2_design_ratified"} for name in GATE_NAMES]
            repo = _fixture_repo(tmp, approval_overrides={"gates": gates})
            outcome = deploy_medallion_from_approval(repo, WS)
            self.assertEqual(outcome.mode, "refused")
            self.assertTrue(any("G2_design_ratified" in r for r in outcome.refusal_reasons))


class G5ExecutionRecheckTest(unittest.TestCase):
    def test_live_apply_refused_when_env_unset(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = _fixture_repo(tmp)
            api = FakeApi()
            with mock.patch.dict(os.environ, {REMOTE_ENV: ""}):
                outcome = deploy_medallion_from_approval(
                    repo, WS, apply=True, confirm_remote_mutation=True, api=api
                )
            self.assertEqual(outcome.mode, "refused")
            self.assertTrue(any("G5 re-check failed" in r for r in outcome.refusal_reasons))
            self.assertEqual(api.calls, [])

    def test_live_apply_refused_without_confirm_flag(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = _fixture_repo(tmp)
            api = FakeApi()
            with mock.patch.dict(os.environ, {REMOTE_ENV: "1"}):
                outcome = deploy_medallion_from_approval(repo, WS, apply=True, api=api)
            self.assertEqual(outcome.mode, "refused")
            self.assertTrue(
                any("--confirm-remote-mutation" in r for r in outcome.refusal_reasons)
            )
            self.assertEqual(api.calls, [])

    def test_g5_check_never_sets_the_env_var(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = _fixture_repo(tmp)
            with mock.patch.dict(os.environ, {REMOTE_ENV: ""}):
                deploy_medallion_from_approval(
                    repo, WS, apply=True, confirm_remote_mutation=True, api=FakeApi()
                )
                self.assertNotEqual(os.environ.get(REMOTE_ENV), "1")


class DryRunTest(unittest.TestCase):
    def test_dry_run_echoes_plan_and_says_so(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = _fixture_repo(tmp)
            with mock.patch.dict(os.environ, {REMOTE_ENV: ""}):
                outcome = deploy_medallion_from_approval(repo, WS)
            self.assertEqual(outcome.mode, "dry_run")
            self.assertIn("[dry-run] no Databricks call was made", outcome.note)
            self.assertIn(REMOTE_ENV, outcome.note)
            self.assertEqual(outcome.executed_count, 0)
            self.assertGreater(len(outcome.actions), 0)
            # Approval is NOT consumed by a dry run.
            approval = json.loads(
                (repo / WS / "interns" / "state" / "medallion" / "deploy_approval.json")
                .read_text(encoding="utf-8")
            )
            self.assertNotIn("consumed_at", approval)
            # Execution state artifacts are written for review.
            state = json.loads(
                (repo / WS / "interns" / "state" / "medallion" / "deploy_execution.json")
                .read_text(encoding="utf-8")
            )
            self.assertEqual(state["mode"], "dry_run")
            md = (repo / WS / "interns" / "state" / "medallion" / "deploy_execution.md") \
                .read_text(encoding="utf-8")
            self.assertIn("Dry Run", md)


class ActionSequenceTest(unittest.TestCase):
    def test_ordered_action_sequence_from_fixture_plan(self) -> None:
        actions = build_unity_catalog_actions(_plan())
        self.assertEqual(
            [(a.action, a.target) for a in actions],
            [
                ("ensure_catalog", "ws_demo"),
                ("ensure_schema", "ws_demo.bronze"),
                ("ensure_schema", "ws_demo.silver"),
                ("ensure_schema", "ws_demo.gold"),
                ("ensure_schema", "ws_demo.ops"),
                ("ensure_volume", "ws_demo.bronze.raw_sources"),
                ("upload_raw_source",
                 f"/Volumes/ws_demo/bronze/raw_sources/{WS}/data/orders.csv"),
                ("create_bronze_table", "ws_demo.bronze.orders__src"),
                ("materialize_silver_table", "ws_demo.silver.orders"),
                ("materialize_gold_table", "ws_demo.gold.kpi_x_gold"),
                ("ensure_ops_manifest_table", "ws_demo.ops.refresh_manifest"),
            ],
        )
        self.assertEqual([a.seq for a in actions], list(range(1, len(actions) + 1)))
        by_action = {a.action: a for a in actions}
        self.assertIn("CREATE CATALOG IF NOT EXISTS ws_demo", by_action["ensure_catalog"].statement)
        self.assertIn("read_files", by_action["create_bronze_table"].statement)
        self.assertIn("format => 'csv'", by_action["create_bronze_table"].statement)
        self.assertFalse(by_action["materialize_silver_table"].execute)
        self.assertFalse(by_action["materialize_gold_table"].execute)
        self.assertIn("USING DELTA", by_action["ensure_ops_manifest_table"].statement)

    def test_unsupported_bronze_source_format_is_deferred(self) -> None:
        plan = _plan()
        uc = plan["unity_catalog"]
        uc["volumes"][0]["uploads"][0]["source_file"] = f"{WS}/data/orders.xlsx"
        uc["tables"][0]["source_file"] = f"{WS}/data/orders.xlsx"
        actions = build_unity_catalog_actions(plan)
        bronze = next(a for a in actions if a.action == "create_bronze_table")
        self.assertFalse(bronze.execute)
        self.assertIn("read_files", bronze.reason)

    def test_live_apply_routes_actions_through_mock_api(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = _fixture_repo(tmp)
            api = FakeApi()
            with mock.patch.dict(os.environ, {REMOTE_ENV: "1"}):
                outcome = deploy_medallion_from_approval(
                    repo, WS, apply=True, confirm_remote_mutation=True, api=api
                )
            self.assertEqual(outcome.mode, "applied")
            self.assertEqual(outcome.failed_count, 0)
            self.assertEqual(outcome.deferred_count, 2)  # silver + gold tables
            kinds = [kind for kind, _ in api.calls]
            # 1 catalog + 4 schemas + 1 volume, then upload, then bronze CTAS + ops table.
            self.assertEqual(kinds, ["sql"] * 6 + ["upload"] + ["sql"] * 2)
            self.assertEqual(api.calls[0][1], "CREATE CATALOG IF NOT EXISTS ws_demo")
            self.assertEqual(
                api.calls[6][1],
                f"/Volumes/ws_demo/bronze/raw_sources/{WS}/data/orders.csv",
            )
            self.assertIn("CREATE TABLE IF NOT EXISTS ws_demo.bronze.orders__src", api.calls[7][1])
            self.assertIn("refresh_manifest", api.calls[8][1])
            # Consume-once: a successful live apply uses up the approval ...
            approval = json.loads(
                (repo / WS / "interns" / "state" / "medallion" / "deploy_approval.json")
                .read_text(encoding="utf-8")
            )
            self.assertIn("consumed_at", approval)
            # ... so an immediate re-run refuses.
            api2 = FakeApi()
            with mock.patch.dict(os.environ, {REMOTE_ENV: "1"}):
                rerun = deploy_medallion_from_approval(
                    repo, WS, apply=True, confirm_remote_mutation=True, api=api2
                )
            self.assertEqual(rerun.mode, "refused")
            self.assertEqual(api2.calls, [])

    def test_failed_action_does_not_consume_approval(self) -> None:
        class FlakyApi(FakeApi):
            def execute_sql(self, statement: str) -> None:
                super().execute_sql(statement)
                if "orders__src" in statement:
                    raise RuntimeError("boom")

        with tempfile.TemporaryDirectory() as tmp:
            repo = _fixture_repo(tmp)
            with mock.patch.dict(os.environ, {REMOTE_ENV: "1"}):
                outcome = deploy_medallion_from_approval(
                    repo, WS, apply=True, confirm_remote_mutation=True, api=FlakyApi()
                )
            self.assertEqual(outcome.mode, "applied")
            self.assertEqual(outcome.failed_count, 1)
            approval = json.loads(
                (repo / WS / "interns" / "state" / "medallion" / "deploy_approval.json")
                .read_text(encoding="utf-8")
            )
            self.assertNotIn("consumed_at", approval)


class VerifyApprovalUnitTest(unittest.TestCase):
    def test_clean_fixture_verifies_with_no_reasons(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = _fixture_repo(tmp)
            approval, plan, reasons = verify_deploy_approval(repo, repo / WS)
            self.assertEqual(reasons, [])
            self.assertIsNotNone(approval)
            self.assertIsNotNone(plan)

    def test_invalid_plan_blocks(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = _fixture_repo(tmp)
            plan = _plan()
            plan["validation"] = {"errors": ["bad"], "valid": False}
            _write_json(
                repo / WS / "interns" / "generated" / "medallion" / "deploy_plan.json", plan
            )
            _, _, reasons = verify_deploy_approval(repo, repo / WS)
            self.assertTrue(any("not valid" in r for r in reasons))


if __name__ == "__main__":
    unittest.main()
