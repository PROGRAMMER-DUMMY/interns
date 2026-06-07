"""Guardrail must flag unrecovered failed mutation commands before a completion claim.

Issue #6: on the CLI path a run of failed `apply` commands (ValueError: Feature
not found) was immediately followed by an "all KPIs fully proven / complete"
claim, and the workflow-guardrail / reliability layer did NOT block it.

Root cause exercised here: a completion/results command is itself a `uv run ...`
invocation, so the generic recovery heuristic (`any "uv run " in the next few
commands`) mistook the completion claim for a recovery of the preceding failures
and suppressed `failed_without_recovery`. A completion/results claim is the
opposite of a recovery and must not silence unrecovered mutation failures.

unittest only (no pytest).
"""
import json
import tempfile
import unittest
from pathlib import Path

from core.onboarding.harness.workflow_guard_harness import WorkflowGuardHarness


def _harness(root: Path) -> WorkflowGuardHarness:
    ws = root / "workspaces" / "demo"
    (ws / "interns" / "state").mkdir(parents=True, exist_ok=True)
    return WorkflowGuardHarness(root, "workspaces/demo")


def _write_trajectory(root: Path, rows: list[dict]) -> None:
    path = root / "workspaces" / "demo" / "interns" / "state" / "trajectory.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(json.dumps(row) for row in rows) + "\n", encoding="utf-8")


def _failed_apply(answer: str) -> dict:
    return {
        "event_type": "command",
        "status": "error",
        "command": (
            "uv run apply-kpi-panel-answer --workspace workspaces/demo "
            f"--domain general --answer {answer}"
        ),
        "summary": "ValueError: Feature not found",
    }


def _completion_claim() -> dict:
    return {
        "event_type": "command",
        "status": "ok",
        "command": "uv run run-kpi-pipeline --workspace workspaces/demo --domain general",
        "summary": "all KPIs fully proven / complete",
    }


class FailedApplyBeforeCompletionTests(unittest.TestCase):
    def test_completion_claim_does_not_recover_failed_apply_run(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            _write_trajectory(
                root,
                [
                    _failed_apply("opt_a"),
                    _failed_apply("opt_b"),
                    _failed_apply("opt_c"),
                    _completion_claim(),
                ],
            )
            findings = _harness(root)._check_failed_without_retry()
            codes = [f["code"] for f in findings]
            self.assertIn("failed_without_recovery", codes)
            self.assertTrue(any(f["severity"] == "error" for f in findings))

    def test_results_claim_after_unrecovered_failures_raises_completion_finding(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            _write_trajectory(
                root,
                [
                    _failed_apply("opt_a"),
                    _failed_apply("opt_b"),
                    _completion_claim(),
                ],
            )
            findings = _harness(root)._check_completion_after_unrecovered_failures()
            codes = [f["code"] for f in findings]
            self.assertIn("completion_claim_over_unrecovered_failures", codes)
            self.assertTrue(all(f["severity"] == "error" for f in findings))

    def test_genuine_retry_then_completion_is_clean(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            _write_trajectory(
                root,
                [
                    _failed_apply("opt_a"),
                    {
                        "event_type": "command",
                        "status": "ok",
                        "command": (
                            "uv run apply-kpi-panel-answer --workspace workspaces/demo "
                            "--domain general --answer opt_a"
                        ),
                        "summary": "applied",
                    },
                    _completion_claim(),
                ],
            )
            harness = _harness(root)
            codes = [f["code"] for f in harness._check_failed_without_retry()]
            codes += [
                f["code"]
                for f in harness._check_completion_after_unrecovered_failures()
            ]
            self.assertNotIn("failed_without_recovery", codes)
            self.assertNotIn("completion_claim_over_unrecovered_failures", codes)

    def test_no_completion_claim_is_clean_for_completion_check(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            _write_trajectory(
                root,
                [
                    _failed_apply("opt_a"),
                    {"event_type": "workflow_step", "status": "ok", "summary": "moved on"},
                ],
            )
            codes = [
                f["code"]
                for f in _harness(root)._check_completion_after_unrecovered_failures()
            ]
            self.assertNotIn("completion_claim_over_unrecovered_failures", codes)


if __name__ == "__main__":
    unittest.main()
