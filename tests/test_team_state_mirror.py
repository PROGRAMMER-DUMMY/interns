"""Team decision state mirrored to a Unity Catalog volume.

`workspaces/**/interns/` is gitignored, so blocker answers live only on the machine that
gave them. This mirrors the two decision-state artifacts to UC so a teammate inherits
them instead of re-answering (possibly differently).

The contract these tests pin is mostly about what must NOT happen: a local-only
workspace must make no remote call, and a mirror failure must never raise, because the
decision is already durably on local disk. Losing the mirror must not lose the answer.
"""

from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from core.onboarding.workspace.team_state import (  # noqa: E402
    mirror_team_state,
    mirror_team_state_safe,
    team_state_remote_root,
)


def completed(returncode: int = 0, stderr: str = "") -> SimpleNamespace:
    return SimpleNamespace(returncode=returncode, stdout="", stderr=stderr)


class RecordingRunner:
    def __init__(self, returncode: int = 0, stderr: str = "") -> None:
        self.calls: list[list[str]] = []
        self._returncode = returncode
        self._stderr = stderr

    def __call__(self, argv, **_kwargs):
        self.calls.append(list(argv))
        return completed(self._returncode, self._stderr)


class TeamStateMirrorTest(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        # WorkspaceLayout refuses a root outside `workspaces/` -- honour that guard in
        # the fixture rather than bypassing it, so these tests exercise the real path.
        self.workspace = Path(self._tmp.name) / "workspaces" / "acme"
        (self.workspace / "interns" / "state").mkdir(parents=True)
        (self.workspace / "interns" / "generated" / "contracts").mkdir(parents=True)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def write_settings(self, **source) -> None:
        path = self.workspace / "interns" / "state" / "workspace_settings.json"
        path.write_text(json.dumps({"databricks_source": source}), encoding="utf-8")

    def write_artifacts(self, *, definitions: bool = True, ops: bool = True) -> None:
        if definitions:
            (
                self.workspace / "interns" / "generated" / "contracts"
                / "workspace_feature_definitions.json"
            ).write_text('{"DeniedAmount": {}}', encoding="utf-8")
        if ops:
            (self.workspace / "interns" / "state" / "applied_ops.jsonl").write_text(
                '{"op_id": "abc"}\n', encoding="utf-8"
            )

    # -- the must-nots -----------------------------------------------------------

    def test_local_only_workspace_makes_no_remote_call(self) -> None:
        self.write_artifacts()
        runner = RecordingRunner()
        result = mirror_team_state(self.workspace, runner=runner)
        self.assertTrue(result["skipped"])
        self.assertTrue(result["ok"])
        self.assertEqual(runner.calls, [], "a local-only workspace must not shell out")

    def test_partial_databricks_source_is_still_skipped(self) -> None:
        # catalog but no schema: not enough to address a volume; do not guess one.
        self.write_settings(catalog="prod")
        self.write_artifacts()
        runner = RecordingRunner()
        self.assertTrue(mirror_team_state(self.workspace, runner=runner)["skipped"])
        self.assertEqual(runner.calls, [])

    def test_no_artifacts_yet_makes_no_remote_call(self) -> None:
        self.write_settings(catalog="prod", schema="rcm")
        runner = RecordingRunner()
        result = mirror_team_state(self.workspace, runner=runner)
        self.assertTrue(result["skipped"])
        self.assertEqual(runner.calls, [])

    def test_failure_is_reported_not_raised(self) -> None:
        self.write_settings(catalog="prod", schema="rcm")
        self.write_artifacts()
        result = mirror_team_state(
            self.workspace, runner=RecordingRunner(returncode=1, stderr="PERMISSION_DENIED")
        )
        self.assertFalse(result["ok"])
        self.assertIn("PERMISSION_DENIED", result["mirrored"][0]["detail"])

    def test_missing_databricks_cli_is_reported_not_raised(self) -> None:
        self.write_settings(catalog="prod", schema="rcm")
        self.write_artifacts()

        def boom(*_a, **_k):
            raise FileNotFoundError("databricks not found")

        result = mirror_team_state(self.workspace, runner=boom)
        self.assertFalse(result["ok"])
        self.assertIn("FileNotFoundError", result["mirrored"][0]["detail"])

    def test_safe_wrapper_swallows_unexpected_errors(self) -> None:
        result = mirror_team_state_safe(Path("\x00 not a path"), runner=RecordingRunner())
        self.assertFalse(result["ok"])

    # -- the happy path ----------------------------------------------------------

    def test_both_artifacts_are_shipped_to_the_volume(self) -> None:
        self.write_settings(catalog="prod", schema="rcm")
        self.write_artifacts()
        runner = RecordingRunner()
        result = mirror_team_state(self.workspace, runner=runner)

        self.assertTrue(result["ok"])
        self.assertFalse(result["skipped"])
        self.assertEqual(len(runner.calls), 2)
        for argv in runner.calls:
            self.assertEqual(argv[:4], ["databricks", "fs", "cp", "--overwrite"])
            self.assertIn("/Volumes/prod/rcm/_state/dbt/acme/", argv[-1])
        shipped = {entry["artifact"] for entry in result["mirrored"]}
        self.assertEqual(shipped, {"feature_definitions", "applied_ops"})

    def test_only_present_artifacts_are_shipped(self) -> None:
        self.write_settings(catalog="prod", schema="rcm")
        self.write_artifacts(definitions=True, ops=False)
        runner = RecordingRunner()
        result = mirror_team_state(self.workspace, runner=runner)
        self.assertEqual(len(runner.calls), 1)
        self.assertEqual(result["mirrored"][0]["artifact"], "feature_definitions")

    def test_stops_at_the_first_failure(self) -> None:
        self.write_settings(catalog="prod", schema="rcm")
        self.write_artifacts()
        runner = RecordingRunner(returncode=1)
        mirror_team_state(self.workspace, runner=runner)
        self.assertEqual(len(runner.calls), 1, "must not keep pushing after a failure")

    def test_remote_root_shape(self) -> None:
        self.assertEqual(
            team_state_remote_root("prod", "rcm", "acme"),
            "/Volumes/prod/rcm/_state/dbt/acme",
        )

    def test_default_runner_is_subprocess_run(self) -> None:
        # Guards against a test-only default silently shipping.
        import inspect

        default = inspect.signature(mirror_team_state).parameters["runner"].default
        self.assertIs(default, subprocess.run)


if __name__ == "__main__":
    unittest.main()
