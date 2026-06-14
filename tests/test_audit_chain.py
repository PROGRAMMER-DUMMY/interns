"""Hermetic unit tests for core.governance.audit_chain.

Contract under test
-------------------
* append_audit_record  -- builds a SHA-256 hash chain one entry at a time
* verify_chain         -- re-walks the file and detects any tampering

Tamper scenarios tested
-----------------------
(a) Mutate a record's content       -> ok=False
(b) Delete a middle line            -> ok=False (seq gap or hash mismatch)
(c) Reorder / duplicate lines       -> ok=False
(d) Empty / missing file            -> ok=True, entry_count=0

Additive wiring test
--------------------
Recording a trajectory event still writes trajectory.jsonl as before AND
creates audit_chain.jsonl alongside it.  A simulated chain-write failure
does NOT propagate an exception.
"""
from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from core.governance.audit_chain import (
    GENESIS_HASH,
    append_audit_record,
    verify_chain,
)
from core.onboarding.harness.trajectory_recorder import WorkspaceTrajectoryRecorder


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _build_chain(path: Path, records: list[dict]) -> list[dict]:
    """Append *records* to *path* and return the written entries."""
    return [append_audit_record(path, r) for r in records]


# ---------------------------------------------------------------------------
# Core chain tests
# ---------------------------------------------------------------------------


class TestAppendAndVerifyHappyPath(unittest.TestCase):
    def test_empty_file_is_valid(self):
        with tempfile.TemporaryDirectory() as tmp:
            chain = Path(tmp) / "audit_chain.jsonl"
            result = verify_chain(chain)
            self.assertTrue(result["ok"], result)
            self.assertEqual(result["entry_count"], 0)
            self.assertIsNone(result["first_broken_seq"])

    def test_missing_file_is_valid(self):
        chain = Path("/nonexistent/path/audit_chain.jsonl")
        result = verify_chain(chain)
        self.assertTrue(result["ok"], result)
        self.assertEqual(result["entry_count"], 0)

    def test_single_record_verifies(self):
        with tempfile.TemporaryDirectory() as tmp:
            chain = Path(tmp) / "audit_chain.jsonl"
            append_audit_record(chain, {"event": "start", "value": 1})
            result = verify_chain(chain)
            self.assertTrue(result["ok"], result)
            self.assertEqual(result["entry_count"], 1)

    def test_multiple_records_verifies(self):
        with tempfile.TemporaryDirectory() as tmp:
            chain = Path(tmp) / "audit_chain.jsonl"
            records = [
                {"event": "onboard", "workspace": "demo"},
                {"event": "kpi_resolved", "kpi": "revenue"},
                {"event": "pipeline_run", "status": "ok"},
                {"event": "shutdown"},
            ]
            _build_chain(chain, records)
            result = verify_chain(chain)
            self.assertTrue(result["ok"], result)
            self.assertEqual(result["entry_count"], len(records))
            self.assertIsNone(result["first_broken_seq"])

    def test_genesis_hash_used_for_first_entry(self):
        with tempfile.TemporaryDirectory() as tmp:
            chain = Path(tmp) / "audit_chain.jsonl"
            entry = append_audit_record(chain, {"event": "first"})
            self.assertEqual(entry["prev_hash"], GENESIS_HASH)
            self.assertEqual(entry["seq"], 0)

    def test_seq_increments_correctly(self):
        with tempfile.TemporaryDirectory() as tmp:
            chain = Path(tmp) / "audit_chain.jsonl"
            entries = _build_chain(chain, [{"n": i} for i in range(5)])
            for i, e in enumerate(entries):
                self.assertEqual(e["seq"], i)

    def test_parent_dirs_created_automatically(self):
        with tempfile.TemporaryDirectory() as tmp:
            chain = Path(tmp) / "deep" / "nested" / "audit_chain.jsonl"
            append_audit_record(chain, {"event": "test"})
            self.assertTrue(chain.exists())


# ---------------------------------------------------------------------------
# Tamper detection tests
# ---------------------------------------------------------------------------


class TestTamperDetection(unittest.TestCase):

    # (a) Mutate a record's content
    def test_tampered_record_content_detected(self):
        with tempfile.TemporaryDirectory() as tmp:
            chain = Path(tmp) / "audit_chain.jsonl"
            _build_chain(chain, [{"n": 0}, {"n": 1}, {"n": 2}, {"n": 3}])

            # Mutate the second line (seq=1)
            lines = chain.read_text(encoding="utf-8").splitlines()
            entry = json.loads(lines[1])
            entry["record"]["n"] = 999  # tamper the payload
            lines[1] = json.dumps(entry, sort_keys=True, separators=(",", ":"))
            chain.write_text("\n".join(lines) + "\n", encoding="utf-8")

            result = verify_chain(chain)
            self.assertFalse(result["ok"], result)
            self.assertEqual(result["first_broken_seq"], 1)

    # (b) Delete a middle line
    def test_deleted_middle_line_detected(self):
        with tempfile.TemporaryDirectory() as tmp:
            chain = Path(tmp) / "audit_chain.jsonl"
            _build_chain(chain, [{"n": i} for i in range(5)])

            lines = chain.read_text(encoding="utf-8").splitlines()
            del lines[2]  # remove seq=2
            chain.write_text("\n".join(lines) + "\n", encoding="utf-8")

            result = verify_chain(chain)
            self.assertFalse(result["ok"], result)
            # The break surfaces at position 2 (seq mismatch or hash mismatch)
            self.assertIsNotNone(result["first_broken_seq"])
            self.assertLessEqual(result["first_broken_seq"], 3)

    # (c) Reorder lines
    def test_reordered_lines_detected(self):
        with tempfile.TemporaryDirectory() as tmp:
            chain = Path(tmp) / "audit_chain.jsonl"
            _build_chain(chain, [{"n": i} for i in range(4)])

            lines = chain.read_text(encoding="utf-8").splitlines()
            # Swap lines 1 and 2
            lines[1], lines[2] = lines[2], lines[1]
            chain.write_text("\n".join(lines) + "\n", encoding="utf-8")

            result = verify_chain(chain)
            self.assertFalse(result["ok"], result)
            self.assertIsNotNone(result["first_broken_seq"])

    # (c) Duplicate a line
    def test_duplicated_line_detected(self):
        with tempfile.TemporaryDirectory() as tmp:
            chain = Path(tmp) / "audit_chain.jsonl"
            _build_chain(chain, [{"n": i} for i in range(3)])

            lines = chain.read_text(encoding="utf-8").splitlines()
            lines.insert(1, lines[0])  # duplicate seq=0 at position 1
            chain.write_text("\n".join(lines) + "\n", encoding="utf-8")

            result = verify_chain(chain)
            self.assertFalse(result["ok"], result)
            self.assertIsNotNone(result["first_broken_seq"])

    # Tamper an entry_hash field directly (re-chaining attack)
    def test_modified_entry_hash_field_detected(self):
        with tempfile.TemporaryDirectory() as tmp:
            chain = Path(tmp) / "audit_chain.jsonl"
            _build_chain(chain, [{"n": i} for i in range(3)])

            lines = chain.read_text(encoding="utf-8").splitlines()
            entry0 = json.loads(lines[0])
            entry0["entry_hash"] = "a" * 64  # forge the hash
            lines[0] = json.dumps(entry0, sort_keys=True, separators=(",", ":"))
            chain.write_text("\n".join(lines) + "\n", encoding="utf-8")

            result = verify_chain(chain)
            self.assertFalse(result["ok"], result)
            # seq 0 itself is broken (recomputed != stored)
            self.assertEqual(result["first_broken_seq"], 0)

    # Corrupt a line so it is not valid JSON
    def test_corrupt_json_line_detected(self):
        with tempfile.TemporaryDirectory() as tmp:
            chain = Path(tmp) / "audit_chain.jsonl"
            _build_chain(chain, [{"n": i} for i in range(3)])

            lines = chain.read_text(encoding="utf-8").splitlines()
            lines[1] = "NOT_VALID_JSON"
            chain.write_text("\n".join(lines) + "\n", encoding="utf-8")

            result = verify_chain(chain)
            self.assertFalse(result["ok"], result)
            self.assertEqual(result["first_broken_seq"], 1)


# ---------------------------------------------------------------------------
# Additive wiring tests
# ---------------------------------------------------------------------------


class TestTrajectoryWiring(unittest.TestCase):
    def test_recording_event_writes_both_files(self):
        """trajectory.jsonl AND audit_chain.jsonl must both be written."""
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            workspace = root / "workspaces" / "demo"
            workspace.mkdir(parents=True)

            WorkspaceTrajectoryRecorder(root, "workspaces/demo").record(
                event_type="command",
                status="ok",
                summary="Test wiring.",
            )

            state_dir = workspace / "interns" / "state"
            trajectory = state_dir / "trajectory.jsonl"
            chain = state_dir / "audit_chain.jsonl"

            self.assertTrue(trajectory.exists(), "trajectory.jsonl must still be written")
            self.assertTrue(chain.exists(), "audit_chain.jsonl must be created by wiring")

    def test_trajectory_content_unchanged(self):
        """The trajectory.jsonl schema must be identical to pre-wiring behavior."""
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            workspace = root / "workspaces" / "demo"
            workspace.mkdir(parents=True)

            WorkspaceTrajectoryRecorder(root, "workspaces/demo").record(
                event_type="decision",
                status="ok",
                summary="Approved join.",
                decision="left join on patient_id",
            )

            row = json.loads(
                (workspace / "interns" / "state" / "trajectory.jsonl")
                .read_text(encoding="utf-8")
                .strip()
            )
            # Core fields from original schema
            self.assertEqual(row["event_type"], "decision")
            self.assertEqual(row["status"], "ok")
            self.assertIn("timestamp", row)
            self.assertIn("workspace", row)
            # audit_chain envelope fields must NOT appear in trajectory.jsonl
            self.assertNotIn("entry_hash", row)
            self.assertNotIn("prev_hash", row)
            self.assertNotIn("seq", row)

    def test_chain_write_failure_does_not_raise(self):
        """If append_audit_record raises, trajectory recording must not propagate the error."""
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            workspace = root / "workspaces" / "demo"
            workspace.mkdir(parents=True)

            with patch(
                "core.onboarding.harness.trajectory_recorder.append_audit_record",
                side_effect=OSError("disk full"),
            ):
                # Must not raise
                result = WorkspaceTrajectoryRecorder(root, "workspaces/demo").record(
                    event_type="command",
                    status="ok",
                    summary="Chain write failure test.",
                )

            self.assertTrue(result.ok)
            # trajectory.jsonl must still be written despite chain failure
            trajectory = workspace / "interns" / "state" / "trajectory.jsonl"
            self.assertTrue(trajectory.exists())
            row = json.loads(trajectory.read_text(encoding="utf-8").strip())
            self.assertEqual(row["event_type"], "command")

    def test_chain_is_valid_after_multiple_recordings(self):
        """audit_chain.jsonl must form a valid chain across multiple records."""
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            workspace = root / "workspaces" / "demo"
            workspace.mkdir(parents=True)

            recorder = WorkspaceTrajectoryRecorder(root, "workspaces/demo")
            for i in range(4):
                recorder.record(
                    event_type="step",
                    status="ok",
                    summary=f"Step {i}",
                )

            chain = workspace / "interns" / "state" / "audit_chain.jsonl"
            result = verify_chain(chain)
            self.assertTrue(result["ok"], result)
            self.assertEqual(result["entry_count"], 4)


if __name__ == "__main__":
    unittest.main()
