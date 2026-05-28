"""Tests for ``core.observability.events`` JSONL event emitter."""
from __future__ import annotations

import json
import tempfile
import time
import unittest
from pathlib import Path

from core.observability.events import emit_event, time_command


def _read_events(workspace: Path) -> list[dict]:
    path = workspace / "interns" / "state" / "events.jsonl"
    lines = path.read_text(encoding="utf-8").splitlines()
    return [json.loads(line) for line in lines if line.strip()]


class EmitEventTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.workspace = Path(self._tmp.name)

    def test_emit_event_writes_single_jsonl_line(self) -> None:
        emit_event(
            self.workspace,
            event_type="command",
            command="onboard-workspace",
            status="ok",
            duration_ms=42,
            summary="ran onboarding",
            details={"foo": "bar"},
        )
        events = _read_events(self.workspace)
        self.assertEqual(len(events), 1)
        ev = events[0]
        for field in ("ts", "event_type", "command", "status", "pid", "hostname"):
            self.assertIn(field, ev)
        self.assertEqual(ev["event_type"], "command")
        self.assertEqual(ev["command"], "onboard-workspace")
        self.assertEqual(ev["status"], "ok")
        self.assertEqual(ev["duration_ms"], 42)
        self.assertEqual(ev["summary"], "ran onboarding")
        self.assertEqual(ev["details"], {"foo": "bar"})
        self.assertIsInstance(ev["pid"], int)

    def test_multiple_emits_append(self) -> None:
        for i in range(3):
            emit_event(
                self.workspace,
                event_type="command",
                command=f"cmd-{i}",
            )
        events = _read_events(self.workspace)
        self.assertEqual(len(events), 3)
        self.assertEqual([e["command"] for e in events], ["cmd-0", "cmd-1", "cmd-2"])

    def test_non_serializable_details_falls_back_to_repr(self) -> None:
        sentinel = object()
        # Must not raise.
        emit_event(
            self.workspace,
            event_type="command",
            command="weird",
            details={"obj": sentinel},
        )
        events = _read_events(self.workspace)
        self.assertEqual(len(events), 1)
        details = events[0]["details"]
        self.assertIsNotNone(details)
        self.assertIn("_repr", details)
        self.assertIsInstance(details["_repr"], str)


class TimeCommandTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.workspace = Path(self._tmp.name)

    def test_time_command_records_duration(self) -> None:
        with time_command(self.workspace, "slow-cmd") as details:
            details["note"] = "sleeping"
            time.sleep(0.02)  # 20ms — comfortably above 5ms floor on Windows
        events = _read_events(self.workspace)
        self.assertEqual(len(events), 1)
        ev = events[0]
        self.assertEqual(ev["status"], "ok")
        self.assertGreaterEqual(ev["duration_ms"], 5)
        self.assertEqual(ev["details"]["note"], "sleeping")

    def test_time_command_records_error_status_on_exception(self) -> None:
        with self.assertRaises(RuntimeError):
            with time_command(self.workspace, "boom"):
                raise RuntimeError("kaboom")
        events = _read_events(self.workspace)
        self.assertEqual(len(events), 1)
        ev = events[0]
        self.assertEqual(ev["status"], "error")
        self.assertIn("error_type", ev["details"])
        self.assertEqual(ev["details"]["error_type"], "RuntimeError")
        self.assertEqual(ev["details"]["error"], "kaboom")


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
