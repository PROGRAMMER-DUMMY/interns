"""Reliability checks for the workflow guard harness.

These tests build a temporary workspace, write a small trajectory.jsonl (and,
where needed, workflow session panels), and assert that the new CLI-agent
reliability checks emit the expected finding codes — and that a clean trajectory
yields none. unittest only (no pytest).
"""
import json
import tempfile
import unittest
from pathlib import Path

from core.onboarding.harness.workflow_guard_harness import (
    SLOW_STEP_THRESHOLD_MS,
    WorkflowGuardHarness,
)


def _harness(root: Path) -> WorkflowGuardHarness:
    ws = root / "workspaces" / "demo"
    (ws / "interns" / "state").mkdir(parents=True, exist_ok=True)
    return WorkflowGuardHarness(root, "workspaces/demo")


def _write_trajectory(root: Path, rows: list[dict]) -> None:
    path = root / "workspaces" / "demo" / "interns" / "state" / "trajectory.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "\n".join(json.dumps(row) for row in rows) + "\n",
        encoding="utf-8",
    )


def _write_panel(root: Path, rel: str, payload: dict) -> None:
    path = root / "workspaces" / "demo" / "interns" / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload), encoding="utf-8")


class StalledStepTests(unittest.TestCase):
    def test_tool_start_without_result_warns(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            _write_trajectory(
                root,
                [
                    {"event_type": "tool_start", "status": "ok", "metadata": {"tool_id": "call-1"}},
                ],
            )
            codes = [f["code"] for f in _harness(root)._check_stalled_steps()]
            self.assertIn("stalled_step", codes)

    def test_slow_step_warns(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            _write_trajectory(
                root,
                [
                    {
                        "event_type": "command",
                        "status": "ok",
                        "command": "uv run onboard-workspace --workspace workspaces/demo",
                        "metadata": {"duration_ms": SLOW_STEP_THRESHOLD_MS + 5000},
                    },
                ],
            )
            codes = [f["code"] for f in _harness(root)._check_stalled_steps()]
            self.assertIn("slow_step", codes)

    def test_paired_start_and_fast_step_clean(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            _write_trajectory(
                root,
                [
                    {"event_type": "tool_start", "status": "ok", "metadata": {"tool_id": "call-1"}},
                    {
                        "event_type": "tool_result",
                        "status": "ok",
                        "metadata": {"tool_id": "call-1", "duration_ms": 1500},
                    },
                ],
            )
            codes = [f["code"] for f in _harness(root)._check_stalled_steps()]
            self.assertNotIn("stalled_step", codes)
            self.assertNotIn("slow_step", codes)


class UnsupportedCommandTests(unittest.TestCase):
    def test_unknown_uv_run_tool_warns(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            _write_trajectory(
                root,
                [
                    {
                        "event_type": "command",
                        "status": "ok",
                        "command": "uv run totally-not-a-real-tool --workspace workspaces/demo",
                    },
                ],
            )
            codes = [f["code"] for f in _harness(root)._check_unsupported_commands()]
            self.assertIn("unsupported_command", codes)

    def test_bare_unknown_executable_warns(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            _write_trajectory(
                root,
                [
                    {"event_type": "command", "status": "ok", "command": "frobnicate --do-stuff"},
                ],
            )
            codes = [f["code"] for f in _harness(root)._check_unsupported_commands()]
            self.assertIn("unsupported_command", codes)

    def test_known_tool_and_generic_command_clean(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            _write_trajectory(
                root,
                [
                    {
                        "event_type": "command",
                        "status": "ok",
                        "command": "uv run onboard-workspace --workspace workspaces/demo",
                    },
                    {"event_type": "command", "status": "ok", "command": "git status"},
                ],
            )
            codes = [f["code"] for f in _harness(root)._check_unsupported_commands()]
            self.assertNotIn("unsupported_command", codes)


class FailedWithoutRetryTests(unittest.TestCase):
    def test_failure_without_recovery_errors(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            _write_trajectory(
                root,
                [
                    {
                        "event_type": "command",
                        "status": "error",
                        "command": "uv run onboard-workspace --workspace workspaces/demo",
                    },
                    {"event_type": "workflow_step", "status": "ok", "summary": "moved on, no retry"},
                ],
            )
            findings = _harness(root)._check_failed_without_retry()
            codes = [f["code"] for f in findings]
            self.assertIn("failed_without_recovery", codes)
            self.assertTrue(all(f["severity"] == "error" for f in findings))

    def test_failure_with_retry_clean(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            _write_trajectory(
                root,
                [
                    {
                        "event_type": "command",
                        "status": "error",
                        "command": "uv run onboard-workspace --workspace workspaces/demo",
                    },
                    {
                        "event_type": "command",
                        "status": "ok",
                        "command": "uv run onboard-workspace --workspace workspaces/demo",
                    },
                ],
            )
            codes = [f["code"] for f in _harness(root)._check_failed_without_retry()]
            self.assertNotIn("failed_without_recovery", codes)

    def test_failure_with_validation_recovery_clean(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            _write_trajectory(
                root,
                [
                    {"event_type": "command", "status": "blocked", "command": "uv run generate-kpi-sql --workspace workspaces/demo"},
                    {"event_type": "validation", "status": "ok", "summary": "ran validator"},
                ],
            )
            codes = [f["code"] for f in _harness(root)._check_failed_without_retry()]
            self.assertNotIn("failed_without_recovery", codes)


class IncompleteWorkflowTests(unittest.TestCase):
    def test_awaiting_stage_no_progress_warns(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            _write_panel(
                root,
                "state/workflow_sessions/sess-1/current.json",
                {"stage": "awaiting_user_answer", "status": "pending", "generated_at": "2026-05-29T12:00:00+00:00"},
            )
            _write_trajectory(
                root,
                [
                    {"event_type": "workflow_step", "status": "ok", "timestamp": "2026-05-29T11:00:00+00:00"},
                ],
            )
            codes = [f["code"] for f in _harness(root)._check_incomplete_workflow()]
            self.assertIn("workflow_incomplete", codes)

    def test_awaiting_stage_with_later_progress_clean(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            _write_panel(
                root,
                "state/workflow_sessions/sess-1/current.json",
                {"stage": "awaiting_user_answer", "status": "pending", "generated_at": "2026-05-29T12:00:00+00:00"},
            )
            _write_trajectory(
                root,
                [
                    {"event_type": "workflow_step", "status": "ok", "timestamp": "2026-05-29T13:00:00+00:00"},
                ],
            )
            codes = [f["code"] for f in _harness(root)._check_incomplete_workflow()]
            self.assertNotIn("workflow_incomplete", codes)

    def test_terminal_stage_clean(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            _write_panel(
                root,
                "state/workflow_sessions/sess-1/current.json",
                {"stage": "complete", "status": "passed", "generated_at": "2026-05-29T12:00:00+00:00"},
            )
            _write_trajectory(
                root,
                [
                    {"event_type": "workflow_step", "status": "ok", "timestamp": "2026-05-29T11:00:00+00:00"},
                ],
            )
            codes = [f["code"] for f in _harness(root)._check_incomplete_workflow()]
            self.assertNotIn("workflow_incomplete", codes)


class RosterSeverityFlagTests(unittest.TestCase):
    def _panel(self, root: Path) -> None:
        _write_panel(root, "reports/kpi_generation/current.json", {"stage": "route_selection"})

    def test_default_is_warning(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self._panel(root)
            harness = WorkflowGuardHarness(root, "workspaces/demo")
            findings = [f for f in harness._check_roster_utilization() if f["code"] == "roster_not_routed"]
            self.assertTrue(findings)
            self.assertEqual(findings[0]["severity"], "warning")

    def test_promoted_to_error(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self._panel(root)
            harness = WorkflowGuardHarness(root, "workspaces/demo", roster_severity="error")
            findings = [f for f in harness._check_roster_utilization() if f["code"] == "roster_not_routed"]
            self.assertTrue(findings)
            self.assertEqual(findings[0]["severity"], "error")

    def test_invalid_severity_falls_back_to_warning(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self._panel(root)
            harness = WorkflowGuardHarness(root, "workspaces/demo", roster_severity="bogus")
            self.assertEqual(harness.roster_severity, "warning")


class RepeatedCommandTests(unittest.TestCase):
    def test_same_command_three_times_warns(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            cmd = "uv run validate-project-harness --workspace workspaces/demo"
            _write_trajectory(
                root,
                [
                    {"event_type": "command", "status": "ok", "command": cmd},
                    {"event_type": "command", "status": "ok", "command": cmd},
                    {"event_type": "command", "status": "ok", "command": cmd},
                ],
            )
            findings = _harness(root)._check_repeated_commands()
            codes = [f["code"] for f in findings]
            self.assertIn("repeated_identical_command", codes)
            self.assertTrue(all(f["severity"] == "warning" for f in findings))

    def test_volatile_flags_normalized_so_quiet_variant_still_counts(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            base = "uv run workspace-flow start --workspace workspaces/demo"
            _write_trajectory(
                root,
                [
                    {"event_type": "command", "status": "ok", "command": base},
                    {"event_type": "command", "status": "ok", "command": base + " --quiet"},
                    {"event_type": "command", "status": "ok", "command": base + " --diff"},
                    {"event_type": "command", "status": "ok", "command": base},
                ],
            )
            codes = [f["code"] for f in _harness(root)._check_repeated_commands()]
            self.assertIn("repeated_identical_command", codes)

    def test_two_runs_is_clean(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            cmd = "uv run list-workspace-files --workspace workspaces/demo"
            _write_trajectory(
                root,
                [
                    {"event_type": "command", "status": "ok", "command": cmd},
                    {"event_type": "command", "status": "ok", "command": cmd},
                ],
            )
            codes = [f["code"] for f in _harness(root)._check_repeated_commands()]
            self.assertNotIn("repeated_identical_command", codes)


class HandEditedGeneratedArtifactTests(unittest.TestCase):
    def test_edit_under_generated_solutions_warns(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            _write_trajectory(
                root,
                [
                    {
                        "event_type": "edit",
                        "status": "ok",
                        "artifact": "workspaces/demo/interns/generated/solutions/kpi_002.sql",
                    },
                ],
            )
            findings = _harness(root)._check_hand_edited_generated_artifacts()
            codes = [f["code"] for f in findings]
            self.assertIn("generated_artifact_hand_edited", codes)
            self.assertTrue(all(f["severity"] == "warning" for f in findings))

    def test_edit_path_in_metadata_warns(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            _write_trajectory(
                root,
                [
                    {
                        "event_type": "file_write",
                        "status": "ok",
                        "metadata": {"path": "interns/generated/contracts/kpi_registry.json"},
                    },
                ],
            )
            codes = [f["code"] for f in _harness(root)._check_hand_edited_generated_artifacts()]
            self.assertIn("generated_artifact_hand_edited", codes)

    def test_edit_outside_generated_is_clean(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            _write_trajectory(
                root,
                [
                    {
                        "event_type": "edit",
                        "status": "ok",
                        "artifact": "core/onboarding/kpi/sql_generator.py",
                    },
                ],
            )
            codes = [f["code"] for f in _harness(root)._check_hand_edited_generated_artifacts()]
            self.assertNotIn("generated_artifact_hand_edited", codes)


class ThrowawayReaderScriptTests(unittest.TestCase):
    def test_reader_script_at_repo_root_warns(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            _write_trajectory(
                root,
                [
                    {"event_type": "create_file", "status": "ok", "artifact": "read_panel.py"},
                ],
            )
            findings = _harness(root)._check_throwaway_reader_scripts()
            codes = [f["code"] for f in findings]
            self.assertIn("throwaway_reader_script", codes)
            self.assertTrue(all(f["severity"] == "warning" for f in findings))

    def test_normal_module_is_clean(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            _write_trajectory(
                root,
                [
                    {"event_type": "write", "status": "ok", "artifact": "tools/read_workspace_files.py"},
                    {"event_type": "write", "status": "ok", "artifact": "core/dev/green_gate.py"},
                ],
            )
            codes = [f["code"] for f in _harness(root)._check_throwaway_reader_scripts()]
            self.assertNotIn("throwaway_reader_script", codes)


class CleanTrajectoryTests(unittest.TestCase):
    def test_clean_trajectory_yields_no_reliability_findings(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            _write_trajectory(
                root,
                [
                    {"event_type": "tool_start", "status": "ok", "metadata": {"tool_id": "c1"}},
                    {"event_type": "tool_result", "status": "ok", "metadata": {"tool_id": "c1", "duration_ms": 900}},
                    {
                        "event_type": "command",
                        "status": "ok",
                        "command": "uv run onboard-workspace --workspace workspaces/demo",
                        "metadata": {"duration_ms": 2000},
                    },
                ],
            )
            harness = _harness(root)
            codes = []
            codes += [f["code"] for f in harness._check_stalled_steps()]
            codes += [f["code"] for f in harness._check_unsupported_commands()]
            codes += [f["code"] for f in harness._check_failed_without_retry()]
            codes += [f["code"] for f in harness._check_incomplete_workflow()]
            codes += [f["code"] for f in harness._check_repeated_commands()]
            codes += [f["code"] for f in harness._check_hand_edited_generated_artifacts()]
            codes += [f["code"] for f in harness._check_throwaway_reader_scripts()]
            self.assertEqual(codes, [])

    def test_missing_trajectory_is_tolerated(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            harness = _harness(root)
            self.assertEqual(harness._check_stalled_steps(), [])
            self.assertEqual(harness._check_unsupported_commands(), [])
            self.assertEqual(harness._check_failed_without_retry(), [])
            self.assertEqual(harness._check_incomplete_workflow(), [])
            self.assertEqual(harness._check_repeated_commands(), [])
            self.assertEqual(harness._check_hand_edited_generated_artifacts(), [])
            self.assertEqual(harness._check_throwaway_reader_scripts(), [])


if __name__ == "__main__":
    unittest.main()
