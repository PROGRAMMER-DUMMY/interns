"""core.orchestration.dbt_state: publish dbt run state to a UC volume.

Unlocks slim CI (`dbt build --state`), `dbt retry`, `dbt clone` -- none of
which have anywhere to read prior state from today. Injectable `runner`
(same shape as `core.provisioning.sync_code`'s recording-runner tests) so
these tests never touch the network.
"""
from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from core.orchestration import dbt_state


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


def _project_with_state(tmp: str, *, artifacts=("manifest.json", "run_results.json")) -> Path:
    project_dir = Path(tmp) / "dbt"
    target = project_dir / "target"
    target.mkdir(parents=True, exist_ok=True)
    (project_dir / "dbt_project.yml").write_text(
        "name: 'demo'\nprofile: 'demo'\nversion: '1.0.0'\nvars:\n  catalog: main\n",
        encoding="utf-8",
    )
    for name in artifacts:
        (target / name).write_text("{}", encoding="utf-8")
    return project_dir


class PublishStateCommandShapeTests(unittest.TestCase):
    def test_two_fs_cp_calls_per_artifact_timestamped_and_latest(self):
        with tempfile.TemporaryDirectory() as tmp:
            project_dir = _project_with_state(tmp)
            runner = FakeRunner()
            result = dbt_state.publish_state(project_dir, "workspaces/demo", runner=runner)
            self.assertTrue(result["ok"])
            self.assertEqual(len(runner.calls), 4)  # 2 artifacts x 2 (timestamped + latest)
            for argv in runner.calls:
                self.assertEqual(argv[:3], ["databricks", "fs", "cp"])
            remote_paths = " ".join(" ".join(argv) for argv in runner.calls)
            self.assertIn("/Volumes/main/_state/dbt/demo/", remote_paths)
            self.assertIn("/latest/manifest.json", remote_paths)
            self.assertIn("/latest/run_results.json", remote_paths)
            # a non-"latest" (timestamped) path was also pushed for each artifact
            timestamped = [
                argv for argv in runner.calls
                if "/latest/" not in " ".join(argv)
            ]
            self.assertEqual(len(timestamped), 2)

    def test_only_present_artifacts_are_published(self):
        with tempfile.TemporaryDirectory() as tmp:
            project_dir = _project_with_state(tmp, artifacts=("manifest.json",))
            runner = FakeRunner()
            result = dbt_state.publish_state(project_dir, "workspaces/demo", runner=runner)
            self.assertTrue(result["ok"])
            self.assertEqual(len(runner.calls), 2)
            self.assertEqual(result["artifacts"], ["manifest.json"])


class NoTargetTests(unittest.TestCase):
    def test_missing_target_dir_makes_no_call(self):
        with tempfile.TemporaryDirectory() as tmp:
            project_dir = Path(tmp) / "dbt"
            project_dir.mkdir(parents=True)
            runner = FakeRunner()
            result = dbt_state.publish_state(project_dir, "workspaces/demo", runner=runner)
            self.assertEqual(result, {"ok": False, "reason": "no target/ artifacts"})
            self.assertEqual(runner.calls, [])

    def test_empty_target_dir_makes_no_call(self):
        with tempfile.TemporaryDirectory() as tmp:
            project_dir = Path(tmp) / "dbt"
            (project_dir / "target").mkdir(parents=True)
            runner = FakeRunner()
            result = dbt_state.publish_state(project_dir, "workspaces/demo", runner=runner)
            self.assertEqual(result, {"ok": False, "reason": "no target/ artifacts"})
            self.assertEqual(runner.calls, [])


class FailureTests(unittest.TestCase):
    def test_non_zero_exit_is_structured_and_stops_at_first_failure(self):
        with tempfile.TemporaryDirectory() as tmp:
            project_dir = _project_with_state(tmp)
            secret = "https://dbc-abc.cloud.databricks.com token=dapi0123456789abcdef"
            runner = FakeRunner(returncode=1, stderr=f"{secret}\nError: volume not found")
            result = dbt_state.publish_state(project_dir, "workspaces/demo", runner=runner)
            self.assertFalse(result["ok"])
            self.assertEqual(len(runner.calls), 1)
            raw = str(result)
            for value in ("dapi0123456789abcdef", "dbc-abc.cloud.databricks.com"):
                self.assertNotIn(value, raw)
            self.assertIn("[REDACTED:", raw)

    def test_missing_databricks_cli_is_reported_not_raised(self):
        with tempfile.TemporaryDirectory() as tmp:
            project_dir = _project_with_state(tmp)

            def _boom(argv, **kwargs):
                raise FileNotFoundError("databricks")

            result = dbt_state.publish_state(project_dir, "workspaces/demo", runner=_boom)
            self.assertFalse(result["ok"])
            self.assertIn("FileNotFoundError", str(result))


class StateDownloadCommandTests(unittest.TestCase):
    def test_reads_catalog_from_dbt_project_yml_and_targets_latest(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            _project_with_state(str(root / "workspaces" / "demo"))
            # _project_with_state wrote dbt/ under its own tmp arg -- re-lay it
            # out at workspaces/demo/dbt so state_download_command's own
            # workspace/dbt lookup finds it.
            command = dbt_state.state_download_command("workspaces/demo", repo_root=root)
            self.assertEqual(command[:3], ["databricks", "fs", "cp"])
            joined = " ".join(command)
            self.assertIn("/Volumes/main/_state/dbt/demo/latest", joined)

    def test_missing_project_yml_still_returns_a_command(self):
        with tempfile.TemporaryDirectory() as tmp:
            command = dbt_state.state_download_command("workspaces/nope", repo_root=Path(tmp))
            self.assertEqual(command[:3], ["databricks", "fs", "cp"])


if __name__ == "__main__":
    unittest.main()
