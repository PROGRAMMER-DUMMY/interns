from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from core.dashboard_services import (
    BuildControlService,
    DashboardPaths,
    GitHistoryService,
    WorkspaceCommandService,
    read_json,
    tail_file,
)


class DashboardServiceFileTests(unittest.TestCase):
    def test_read_json_returns_default_for_invalid_json(self):
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "bad.json"
            path.write_text("{bad", encoding="utf-8")

            self.assertEqual(read_json(path, {"fallback": True}), {"fallback": True})

    def test_tail_file_limits_lines(self):
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "run.log"
            path.write_text("a\nb\nc\n", encoding="utf-8")

            self.assertEqual(tail_file(path, 2), "b\nc")


class GitHistoryServiceTests(unittest.TestCase):
    @patch("core.dashboard_services.subprocess.run")
    def test_log_file_uses_safe_argv_and_parses_entries(self, run):
        run.return_value = Mock(stdout="abc123 message one\ndef456 message two\n")
        service = GitHistoryService(Path("repo"))

        entries = service.log_file("core/x.py", n=2)

        run.assert_called_once_with(
            ["git", "log", "--oneline", "--follow", "-2", "--", "core/x.py"],
            capture_output=True,
            text=True,
            cwd="repo",
            timeout=5,
        )
        self.assertEqual(
            entries,
            [
                {"hash": "abc123", "message": "message one"},
                {"hash": "def456", "message": "message two"},
            ],
        )

    def test_diff_requires_two_revisions_and_file(self):
        service = GitHistoryService(Path("repo"))

        self.assertEqual(service.diff_file("", "b", "x.py"), "Select two revisions to compare.")


class BuildControlServiceTests(unittest.TestCase):
    def test_lock_state_reports_unlocked_when_no_lock_file(self):
        with tempfile.TemporaryDirectory() as td:
            service = BuildControlService(DashboardPaths(Path(td)))

            self.assertEqual(service.lock_state("ws"), {"locked": False, "pid": 0, "age_s": 0})

    @patch("core.dashboard_services.subprocess.Popen")
    def test_trigger_build_writes_pid_and_uses_uv_argv(self, popen):
        popen.return_value = Mock(pid=1234)
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            service = BuildControlService(DashboardPaths(root))

            result = service.trigger_build("acme", target="local", layer="silver", table="claims")

            self.assertEqual(result, {"ok": True, "pid": 1234})
            self.assertEqual(
                (root / "workspaces/acme/interns/state/medallion/build.pid").read_text(
                    encoding="utf-8"
                ),
                "1234",
            )
            argv = popen.call_args.args[0]
            self.assertEqual(
                argv,
                [
                    "uv",
                    "run",
                    "build-medallion",
                    "--workspace",
                    "workspaces/acme",
                    "--target",
                    "local",
                    "--only-layer",
                    "silver",
                    "--only-table",
                    "claims",
                ],
            )


class WorkspaceCommandServiceTests(unittest.TestCase):
    @patch("core.dashboard_services.subprocess.run")
    def test_run_uses_argv_and_truncates_output(self, run):
        run.return_value = Mock(returncode=0, stdout="x" * 4000, stderr="e" * 700)
        service = WorkspaceCommandService(Path("repo"))

        result = service.run("acme", "validate-workspace-artifacts", ["--json"])

        run.assert_called_once_with(
            [
                "uv",
                "run",
                "validate-workspace-artifacts",
                "--workspace",
                "workspaces/acme",
                "--json",
            ],
            capture_output=True,
            text=True,
            cwd="repo",
            timeout=300,
        )
        self.assertTrue(result["ok"])
        self.assertEqual(len(result["stdout"]), 3000)
        self.assertEqual(len(result["stderr"]), 500)


if __name__ == "__main__":
    unittest.main()
