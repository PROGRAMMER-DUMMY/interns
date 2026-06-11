"""Tests for the roster/reliability guard signals and generation-scoring gate."""
import json
import tempfile
import unittest
from pathlib import Path

from core.onboarding.harness.project_harness import (
    ProjectHarness,
    _collect_blockers,
    _collect_warnings,
    _dedupe_with_counts,
    _next_commands,
)


def _base_checks() -> dict:
    """A minimal-but-complete checks dict the collectors can consume."""
    return {
        "workspace_artifacts": {"ok": True, "errors": [], "warnings": []},
        "kpi_execution": {"ok": True, "records": []},
        "git_hygiene": {"ok": True, "issues": []},
        "agent_benchmark": {"scorecard": {}, "release_gate": {}},
        "evidence_graph": {"ok": True, "warnings": []},
        "workflow_guardrails": {},
        "generation_scoring": {"status": "skipped", "ok": True, "panels": []},
    }


class WorkflowReliabilitySignalTests(unittest.TestCase):
    def test_failed_without_recovery_error_becomes_blocker(self):
        checks = _base_checks()
        checks["workflow_guardrails"] = {
            "ok": False,
            "report": {
                "findings": [
                    {
                        "severity": "error",
                        "code": "failed_without_recovery",
                        "message": "Step crashed and never recovered.",
                    }
                ]
            },
        }

        blockers = _collect_blockers(checks)

        self.assertTrue(
            any(
                "workflow reliability:" in b and "failed_without_recovery" in b
                for b in blockers
            ),
            blockers,
        )

    def test_failed_without_recovery_blocks_even_when_guard_ok(self):
        # Even if the overall guard reports ok, failed_without_recovery must block.
        checks = _base_checks()
        checks["workflow_guardrails"] = {
            "ok": True,
            "report": {
                "findings": [
                    {
                        "severity": "warning",
                        "code": "failed_without_recovery",
                        "message": "Recovered late but flagged.",
                    }
                ]
            },
        }

        blockers = _collect_blockers(checks)

        self.assertTrue(
            any("failed_without_recovery" in b for b in blockers), blockers
        )

    def test_error_severity_reliability_finding_becomes_blocker(self):
        checks = _base_checks()
        checks["workflow_guardrails"] = {
            "ok": False,
            "report": {
                "findings": [
                    {
                        "severity": "error",
                        "code": "workflow_incomplete",
                        "message": "Workflow ended mid-step.",
                    }
                ]
            },
        }

        blockers = _collect_blockers(checks)

        self.assertTrue(
            any(
                "workflow reliability:" in b and "workflow_incomplete" in b
                for b in blockers
            ),
            blockers,
        )

    def test_dedupe_with_counts_collapses_repeats(self):
        items = ["a", "a", "b", "a", "c", "c"]
        self.assertEqual(
            _dedupe_with_counts(items),
            ["a (x3)", "b", "c (x2)"],
        )

    def test_repeated_reliability_warnings_collapse_to_one_line(self):
        finding = {
            "severity": "warning",
            "code": "trajectory_failed_step_without_recovery",
            "message": "Trajectory contains a failed step without a nearby retry or recovery event.",
        }
        checks = _base_checks()
        checks["workflow_guardrails"] = {"ok": True, "report": {"findings": [finding] * 6}}

        warnings = _collect_warnings(checks)

        matching = [w for w in warnings if "trajectory_failed_step_without_recovery" in w]
        self.assertEqual(len(matching), 1, warnings)
        self.assertTrue(matching[0].endswith("(x6)"), matching[0])

    def test_roster_not_routed_warning_appears_in_warnings(self):
        checks = _base_checks()
        checks["workflow_guardrails"] = {
            "ok": True,
            "report": {
                "findings": [
                    {
                        "severity": "warning",
                        "code": "roster_not_routed",
                        "message": "A roster role was never routed.",
                    }
                ]
            },
        }

        warnings = _collect_warnings(checks)

        self.assertTrue(
            any(
                "workflow reliability:" in w and "roster_not_routed" in w
                for w in warnings
            ),
            warnings,
        )

    def test_reliability_warnings_use_reliability_prefix(self):
        checks = _base_checks()
        checks["workflow_guardrails"] = {
            "ok": True,
            "report": {
                "findings": [
                    {"severity": "warning", "code": "stalled_step", "message": "stalled"},
                    {"severity": "warning", "code": "slow_step", "message": "slow"},
                    {"severity": "warning", "code": "unsupported_command", "message": "bad cmd"},
                    {"severity": "warning", "code": "repeated_identical_command", "message": "rerun"},
                    {"severity": "warning", "code": "generated_artifact_hand_edited", "message": "edit"},
                    {"severity": "warning", "code": "throwaway_reader_script", "message": "reader"},
                ]
            },
        }

        warnings = _collect_warnings(checks)

        for code in (
            "stalled_step",
            "slow_step",
            "unsupported_command",
            "repeated_identical_command",
            "generated_artifact_hand_edited",
            "throwaway_reader_script",
        ):
            self.assertTrue(
                any(f"workflow reliability: {code}" in w for w in warnings),
                (code, warnings),
            )

    def test_reliability_findings_emit_validate_workflow_guardrails_command(self):
        checks = _base_checks()
        checks["workflow_guardrails"] = {
            "ok": True,
            "report": {
                "findings": [
                    {
                        "severity": "warning",
                        "code": "roster_not_routed",
                        "message": "roster signal",
                    }
                ]
            },
        }

        commands = _next_commands("workspaces/demo", "general", checks)

        self.assertIn(
            "uv run harness workflow-guardrails --workspace workspaces/demo", commands
        )

    def test_missing_report_is_tolerated(self):
        checks = _base_checks()
        checks["workflow_guardrails"] = {"ok": False}  # no "report" key
        # Should not raise and should not invent reliability blockers.
        blockers = _collect_blockers(checks)
        warnings = _collect_warnings(checks)
        self.assertFalse(any("workflow reliability" in b for b in blockers))
        self.assertFalse(any("workflow reliability" in w for w in warnings))


class GenerationScoringGateTests(unittest.TestCase):
    def _harness(self, root: Path) -> ProjectHarness:
        return ProjectHarness(root, "workspaces/demo", domain="general")

    def _kpi_panel(self, workspace: Path, payload: dict) -> None:
        path = workspace / "interns" / "reports" / "kpi_generation" / "current.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(payload), encoding="utf-8")

    def _data_model_panel(self, workspace: Path, payload: dict) -> None:
        path = (
            workspace / "interns" / "reports" / "data_model_generation" / "current.json"
        )
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(payload), encoding="utf-8")

    def test_kpi_panel_with_understanding_score_is_ok(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            workspace = root / "workspaces" / "demo"
            workspace.mkdir(parents=True)
            self._kpi_panel(workspace, {"quality_score": {"understanding_score": 88}})

            result = self._harness(root)._run_generation_scoring()

            self.assertEqual(result["status"], "passed")
            self.assertTrue(result["ok"])
            self.assertEqual(len(result["panels"]), 1)
            self.assertEqual(result["panels"][0]["understanding_score"], 88.0)

    def test_data_model_panel_with_understanding_score_is_ok(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            workspace = root / "workspaces" / "demo"
            workspace.mkdir(parents=True)
            self._data_model_panel(workspace, {"understanding": {"score": 72.5}})

            result = self._harness(root)._run_generation_scoring()

            self.assertTrue(result["ok"])
            self.assertEqual(result["panels"][0]["understanding_score"], 72.5)

    def test_panel_without_understanding_score_is_not_ok(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            workspace = root / "workspaces" / "demo"
            workspace.mkdir(parents=True)
            self._kpi_panel(workspace, {"kpis": [{"id": "k1"}]})  # no quality_score

            result = self._harness(root)._run_generation_scoring()

            self.assertEqual(result["status"], "blocked")
            self.assertFalse(result["ok"])
            self.assertFalse(result["panels"][0]["has_score"])

    def test_no_panel_is_skipped(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            workspace = root / "workspaces" / "demo"
            workspace.mkdir(parents=True)

            result = self._harness(root)._run_generation_scoring()

            self.assertEqual(result["status"], "skipped")
            self.assertTrue(result["ok"])
            self.assertEqual(result["panels"], [])

    def test_failing_generation_scoring_listed_as_warning_not_hard_blocker(self):
        # When the gate fails, it should be a warning + a next command, not a blocker.
        checks = _base_checks()
        checks["generation_scoring"] = {
            "status": "blocked",
            "ok": False,
            "reason": "Generation panel(s) missing an understanding/confidence score: kpi.",
            "panels": [{"kind": "kpi", "has_score": False}],
        }

        warnings = _collect_warnings(checks)
        blockers = _collect_blockers(checks)
        commands = _next_commands("workspaces/demo", "general", checks)

        self.assertTrue(any("generation scoring:" in w for w in warnings), warnings)
        self.assertFalse(any("generation scoring" in b for b in blockers), blockers)
        self.assertIn(
            "uv run validate-generation-scoring --workspace workspaces/demo", commands
        )


if __name__ == "__main__":
    unittest.main()
