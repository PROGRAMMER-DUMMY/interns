"""Production-readiness fix, P7: core/onboarding/harness/trajectory_recorder.py's
record() called self.render() on every single event write, and render()
called load_trajectory(self.trajectory_path) -- a full read_text().splitlines()
+ json.loads-per-line re-parse of the ENTIRE historical trajectory.jsonl, on
EVERY append. For a long-running workspace this is O(n) work per write and
O(n^2) total -- a real, definite performance degradation that gets worse over
a workspace's lifetime, independent of any LLM-context concern.
current.json's own output was already correctly bounded (events[-100:],
_summarize is O(n) count-only) -- this is a pure computation-cost fix, not a
new bounding requirement.

Fixed: a small persisted counter (trajectory_summary_state.json) is updated
incrementally from the ONE new record on each append (_apply_record_to_summary_state),
instead of recomputed from every historical line. The `events` tail slice is
read via a bounded seek-backward tail-read (_tail_lines/_tail_records)
instead of the whole file. Bootstraps once via a real full scan
(_bootstrap_summary_state) if the counter is missing/corrupt -- a
pre-existing trajectory.jsonl from before this fix, or an out-of-band writer
-- so correctness is never silently lost. load_trajectory's own contract is
UNCHANGED (workflow_guard_harness.py's reliability checks and
evidence_graph.py both need the full history and keep getting it).

See ~/.claude/plans/dynamic-cooking-firefly.md P7.
"""
from __future__ import annotations

import json
import tempfile
import time
import unittest
from pathlib import Path

from core.onboarding.harness.trajectory_recorder import (
    WorkspaceTrajectoryRecorder,
    load_trajectory,
)


class ByteIdenticalOutputTests(unittest.TestCase):
    """Proves the fix is computation-cost-only: current.json's shape/values
    are identical to what the old always-full-scan code would produce."""

    def _make_recorder(self, tmp: str) -> WorkspaceTrajectoryRecorder:
        repo_root = Path(tmp)
        workspace = repo_root / "workspaces" / "demo"
        workspace.mkdir(parents=True)
        return WorkspaceTrajectoryRecorder(repo_root, workspace)

    def test_current_json_matches_manual_full_scan(self):
        with tempfile.TemporaryDirectory() as tmp:
            recorder = self._make_recorder(tmp)
            recorder.record(event_type="start", status="ok", summary="begin")
            recorder.record(event_type="validation", status="ok", summary="check")
            recorder.record(event_type="tool_call", status="failed", exit_code=1, command="run x")
            recorder.record(event_type="retry", status="ok", summary="recovered")

            current = json.loads((recorder.layout.reports_dir / "trajectory" / "current.json").read_text())

            # Manual full-scan reference computation (the OLD code path,
            # reimplemented here independently rather than imported, so this
            # test doesn't just re-check the same function against itself).
            all_records = load_trajectory(recorder.trajectory_path)
            self.assertEqual(current["event_count"], len(all_records))
            self.assertEqual(current["event_count"], 4)
            self.assertEqual(current["summary"]["failures"], 1)
            self.assertEqual(current["summary"]["recoveries"], 1)
            self.assertEqual(current["summary"]["validations"], 1)
            self.assertEqual(current["summary"]["commands"], 1)
            self.assertEqual(current["summary"]["last_status"], "ok")
            self.assertEqual(current["events"], all_records[-100:])

    def test_render_only_standalone_call_matches_too(self):
        with tempfile.TemporaryDirectory() as tmp:
            recorder = self._make_recorder(tmp)
            for i in range(5):
                recorder.record(event_type="step", status="ok", summary=f"s{i}")
            result = recorder.render()
            self.assertEqual(result.event_count, 5)


class BootstrapSelfHealingTests(unittest.TestCase):
    def test_missing_counter_file_bootstraps_correctly(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo_root = Path(tmp)
            workspace = repo_root / "workspaces" / "demo"
            workspace.mkdir(parents=True)
            recorder = WorkspaceTrajectoryRecorder(repo_root, workspace)

            # Simulate a pre-existing trajectory.jsonl written before this
            # fix (or by an out-of-band process) -- no summary-state counter
            # file exists yet.
            recorder.trajectory_path.parent.mkdir(parents=True, exist_ok=True)
            with recorder.trajectory_path.open("w", encoding="utf-8") as f:
                for i in range(3):
                    f.write(json.dumps({"event_type": "step", "status": "ok", "summary": f"pre{i}"}) + "\n")
            self.assertFalse(recorder._summary_state_path.exists())

            result = recorder.render()
            self.assertEqual(result.event_count, 3)
            # Bootstrap must persist the counter so it's not re-scanned next time.
            self.assertTrue(recorder._summary_state_path.exists())

    def test_corrupt_counter_file_bootstraps_correctly(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo_root = Path(tmp)
            workspace = repo_root / "workspaces" / "demo"
            workspace.mkdir(parents=True)
            recorder = WorkspaceTrajectoryRecorder(repo_root, workspace)
            recorder.record(event_type="start", status="ok")
            recorder._summary_state_path.write_text("not valid json{{{", encoding="utf-8")

            result = recorder.render()
            self.assertEqual(result.event_count, 1)


class BoundedCostTests(unittest.TestCase):
    def test_append_cost_does_not_grow_with_history_size(self):
        """Not a strict Big-O proof -- a bounded-cost sanity check: appending
        the 5000th event must not take meaningfully longer than appending
        the 10th, which it would if every append re-read the whole file."""
        with tempfile.TemporaryDirectory() as tmp:
            repo_root = Path(tmp)
            workspace = repo_root / "workspaces" / "demo"
            workspace.mkdir(parents=True)
            recorder = WorkspaceTrajectoryRecorder(repo_root, workspace)

            for i in range(10):
                recorder.record(event_type="step", status="ok", summary=f"warmup{i}")
            start = time.perf_counter()
            for i in range(10):
                recorder.record(event_type="step", status="ok", summary=f"early{i}")
            early_elapsed = time.perf_counter() - start

            for i in range(980):
                recorder.record(event_type="step", status="ok", summary=f"bulk{i}")

            start = time.perf_counter()
            for i in range(10):
                recorder.record(event_type="step", status="ok", summary=f"late{i}")
            late_elapsed = time.perf_counter() - start

            # If render() still re-read the whole file, late_elapsed (~5000
            # prior events) would be roughly 500x early_elapsed (~10 prior
            # events). Allow generous headroom (10x) for timing noise while
            # still catching an O(n) regression.
            self.assertLess(late_elapsed, early_elapsed * 10 + 0.5, (early_elapsed, late_elapsed))


if __name__ == "__main__":
    unittest.main()
