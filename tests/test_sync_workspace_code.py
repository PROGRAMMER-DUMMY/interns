"""Code delivery: `databricks sync` argv, refusals, evidence log, no secrets."""
from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from core.provisioning.sync_code import (
    STATUS_DRY_RUN,
    STATUS_FAILED,
    STATUS_NOTHING_TO_SYNC,
    STATUS_REFUSED_KILL_SWITCH,
    STATUS_REFUSED_NO_CONFIRMATION,
    STATUS_SYNCED,
    build_sync_commands,
    sync_workspace_code,
)


class FakeProc:
    def __init__(self, returncode: int = 0, stderr: str = ""):
        self.returncode = returncode
        self.stderr = stderr
        self.stdout = ""


class FakeRunner:
    """Records argv; never touches the network."""

    def __init__(self, returncode: int = 0, stderr: str = ""):
        self.calls: list[list[str]] = []
        self.returncode = returncode
        self.stderr = stderr

    def __call__(self, argv, **kwargs):
        self.calls.append(list(argv))
        return FakeProc(self.returncode, self.stderr)


def _workspace(tmp: str, *, confirmed: bool, dirs=("ingestion", "dbt")):
    root = Path(tmp)
    ws_rel = "workspaces/demo"
    ws = root / ws_rel
    ws.mkdir(parents=True, exist_ok=True)
    for name in dirs:
        (ws / name).mkdir(parents=True, exist_ok=True)
        (ws / name / "job.py").write_text("# generated\n", encoding="utf-8")
    if confirmed:
        marker = ws / "interns" / "reports" / "solution_blueprint"
        marker.mkdir(parents=True, exist_ok=True)
        (marker / "current.confirmed.json").write_text(
            json.dumps({"confirmed_by": "a human", "status": "confirmed"}),
            encoding="utf-8",
        )
    return root, ws_rel


def _log(root: Path, ws_rel: str) -> dict:
    return json.loads(
        (root / ws_rel / "interns/generated/evidence/provisioning/sync_log.json")
        .read_text(encoding="utf-8")
    )


class CommandShapeTests(unittest.TestCase):
    def test_argv_is_databricks_sync_and_never_import_dir(self):
        with tempfile.TemporaryDirectory() as tmp:
            root, ws = _workspace(tmp, confirmed=True)
            runner = FakeRunner()
            sync_workspace_code(root, ws, dry_run=False, runner=runner)
            self.assertEqual(len(runner.calls), 2)
            for argv in runner.calls:
                self.assertEqual(argv[:2], ["databricks", "sync"])
                self.assertNotIn("import-dir", argv)
                self.assertNotIn("workspace", argv[:2])

    def test_remote_root_default_and_override(self):
        with tempfile.TemporaryDirectory() as tmp:
            root, ws = _workspace(tmp, confirmed=True)
            runner = FakeRunner()
            result = sync_workspace_code(root, ws, dry_run=False, runner=runner)
            self.assertEqual(result["remote_root"], "/Workspace/Shared/demo")
            self.assertEqual(runner.calls[0][3], "/Workspace/Shared/demo/ingestion")

            other = FakeRunner()
            result = sync_workspace_code(
                root, ws, dry_run=False, remote_root="/Workspace/Users/x/demo/",
                runner=other,
            )
            self.assertEqual(result["remote_root"], "/Workspace/Users/x/demo")
            self.assertEqual(other.calls[1][3], "/Workspace/Users/x/demo/dbt")

    def test_build_sync_commands_skips_absent_directories(self):
        with tempfile.TemporaryDirectory() as tmp:
            root, ws = _workspace(tmp, confirmed=True, dirs=("dbt",))
            commands = build_sync_commands(root / ws, "/Workspace/Shared/demo")
            self.assertEqual(len(commands), 1)
            self.assertTrue(commands[0][3].endswith("/dbt"))
            self.assertIn("--full", commands[0])
            self.assertIn("--exclude", commands[0])

    def test_full_flag_is_optional(self):
        with tempfile.TemporaryDirectory() as tmp:
            root, ws = _workspace(tmp, confirmed=True)
            commands = build_sync_commands(root / ws, "/r", full=False)
            self.assertTrue(all("--full" not in argv for argv in commands))


class RefusalTests(unittest.TestCase):
    def test_without_confirmed_blueprint_it_refuses_and_pushes_nothing(self):
        with tempfile.TemporaryDirectory() as tmp:
            root, ws = _workspace(tmp, confirmed=False)
            runner = FakeRunner()
            result = sync_workspace_code(root, ws, runner=runner)
            self.assertFalse(result["ok"])
            self.assertEqual(result["status"], STATUS_REFUSED_NO_CONFIRMATION)
            self.assertEqual(runner.calls, [])
            self.assertEqual(result["synced"], [])
            self.assertIn("confirm-blueprint", result["next_command"])
            self.assertIn("Nothing was pushed", result["reason"])

    def test_explicit_no_dry_run_without_confirmation_still_refuses(self):
        with tempfile.TemporaryDirectory() as tmp:
            root, ws = _workspace(tmp, confirmed=False)
            runner = FakeRunner()
            result = sync_workspace_code(root, ws, dry_run=False, runner=runner)
            self.assertEqual(result["status"], STATUS_REFUSED_NO_CONFIRMATION)
            self.assertEqual(runner.calls, [])

    def test_kill_switch_refuses_even_when_confirmed(self):
        with tempfile.TemporaryDirectory() as tmp:
            root, ws = _workspace(tmp, confirmed=True)
            runner = FakeRunner()
            with mock.patch.dict(
                "os.environ", {"AUTORESEARCH_ALLOW_REMOTE_EXECUTION": "0"}, clear=False
            ):
                result = sync_workspace_code(root, ws, dry_run=False, runner=runner)
            self.assertEqual(result["status"], STATUS_REFUSED_KILL_SWITCH)
            self.assertFalse(result["ok"])
            self.assertEqual(runner.calls, [])

    def test_dry_run_executes_nothing(self):
        with tempfile.TemporaryDirectory() as tmp:
            root, ws = _workspace(tmp, confirmed=True)
            runner = FakeRunner()
            result = sync_workspace_code(root, ws, dry_run=True, runner=runner)
            self.assertEqual(result["status"], STATUS_DRY_RUN)
            self.assertTrue(result["ok"])
            self.assertEqual(runner.calls, [])
            self.assertTrue(all(e["status"] == "would_sync" for e in result["synced"]))


class DirectorySelectionTests(unittest.TestCase):
    def test_only_existing_directories_are_synced(self):
        with tempfile.TemporaryDirectory() as tmp:
            root, ws = _workspace(tmp, confirmed=True, dirs=("ingestion",))
            runner = FakeRunner()
            result = sync_workspace_code(root, ws, dry_run=False, runner=runner)
            self.assertEqual(len(runner.calls), 1)
            self.assertEqual([e["dir"] for e in result["synced"]], ["ingestion"])
            self.assertEqual(
                [(e["dir"], e["reason"]) for e in result["skipped"]],
                [("dbt", "not generated")],
            )

    def test_no_generated_code_is_a_named_no_op(self):
        with tempfile.TemporaryDirectory() as tmp:
            root, ws = _workspace(tmp, confirmed=True, dirs=())
            runner = FakeRunner()
            result = sync_workspace_code(root, ws, dry_run=False, runner=runner)
            self.assertEqual(result["status"], STATUS_NOTHING_TO_SYNC)
            self.assertEqual(runner.calls, [])
            self.assertIn("generate-ingestion", result["reason"])


class FailureTests(unittest.TestCase):
    def test_non_zero_exit_is_a_structured_failure_with_stderr_tail_only(self):
        secret = "https://dbc-abc.cloud.databricks.com token=dapi0123456789abcdef"
        with tempfile.TemporaryDirectory() as tmp:
            root, ws = _workspace(tmp, confirmed=True)
            runner = FakeRunner(returncode=1, stderr=f"{secret}\nError: cannot resolve path")
            result = sync_workspace_code(root, ws, dry_run=False, runner=runner)
            self.assertFalse(result["ok"])
            self.assertEqual(result["status"], STATUS_FAILED)
            self.assertEqual(len(runner.calls), 1)  # stops at the first failure
            entry = result["synced"][0]
            self.assertEqual(entry["exit_code"], 1)
            self.assertIn("cannot resolve path", entry["stderr_tail"])
            raw = json.dumps(_log(root, ws)).lower()
            # Secret VALUES and the private workspace endpoint must be gone. A
            # redacted label (`token=[REDACTED:SECRET]`) is the sanctioned shape
            # per AGENTS.md's guardrail -- it says WHAT was withheld without
            # leaking it -- so the label itself is not what we assert against.
            for value in (
                "dapi0123456789abcdef",
                "dbc-abc.cloud.databricks.com",
                "secret_value",
            ):
                self.assertNotIn(value, raw)
            self.assertIn("[redacted:secret]", raw)
            self.assertIn("[redacted:host]", raw)

    def test_missing_databricks_cli_is_reported_not_raised(self):
        with tempfile.TemporaryDirectory() as tmp:
            root, ws = _workspace(tmp, confirmed=True)

            def _boom(argv, **kwargs):
                raise FileNotFoundError("databricks")

            result = sync_workspace_code(root, ws, dry_run=False, runner=_boom)
            self.assertFalse(result["ok"])
            self.assertEqual(result["status"], STATUS_FAILED)
            self.assertIn("FileNotFoundError", result["synced"][0]["detail"])


class SyncLogTests(unittest.TestCase):
    def test_log_records_per_directory_outcome_remote_path_and_exit_code(self):
        with tempfile.TemporaryDirectory() as tmp:
            root, ws = _workspace(tmp, confirmed=True, dirs=("ingestion",))
            result = sync_workspace_code(root, ws, dry_run=False, runner=FakeRunner())
            self.assertEqual(result["status"], STATUS_SYNCED)
            log = _log(root, ws)
            self.assertEqual(log["artifact_type"], "evidence/provisioning/sync_log.json")
            self.assertEqual(log["tool"], "databricks sync")
            self.assertEqual(log["remote_root"], "/Workspace/Shared/demo")
            self.assertTrue(log["confirmed_blueprint"])
            by_dir = {d["dir"]: d for d in log["directories"]}
            self.assertEqual(by_dir["ingestion"]["status"], "synced")
            self.assertEqual(by_dir["ingestion"]["exit_code"], 0)
            self.assertEqual(
                by_dir["ingestion"]["remote_path"], "/Workspace/Shared/demo/ingestion"
            )
            self.assertEqual(by_dir["dbt"]["status"], "skipped")
            self.assertTrue(result["sync_log_path"].endswith("sync_log.json"))

    def test_refusal_is_also_logged(self):
        with tempfile.TemporaryDirectory() as tmp:
            root, ws = _workspace(tmp, confirmed=False)
            sync_workspace_code(root, ws, runner=FakeRunner())
            log = _log(root, ws)
            self.assertEqual(log["status"], STATUS_REFUSED_NO_CONFIRMATION)
            self.assertFalse(log["ok"])
            self.assertTrue(all(d["status"] == "skipped" for d in log["directories"]))


if __name__ == "__main__":
    unittest.main()
