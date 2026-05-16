"""Integration tests for the experiment loop wiring.

Covers the three phase-1/2/3 fixes that don't need a real LLM:
  * intern output chaining (prior reports flow into next intern's context)
  * deep_research_every_n scheduling gate
  * InternBus retry-once-then-placeholder behaviour
  * stale-PID crash recovery on startup
  * review-pause signal file gate

We bypass `ExperimentLoop.__init__` (which has heavy dependencies on the
real workspace bootstrap) and exercise the affected methods in isolation
with a hand-built loop-like object plus a fake bus / mutator.
"""
from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from core.agents.intern_bus import InternBus, truncate_for_chain, _is_failure
from core.config import Config
from core.orchestration.loop import ExperimentLoop, _EXPENSIVE_INTERNS


# ── helpers ───────────────────────────────────────────────────────────────────

def _stub_loop(tmp: Path, *, deep_research_every_n=3, stuck_threshold=3,
               dry_run=False) -> ExperimentLoop:
    """Build a partially-initialised ExperimentLoop without calling __init__.
    Only the attributes used by the methods under test are set."""
    loop = ExperimentLoop.__new__(ExperimentLoop)
    cfg = Config()
    cfg.deep_research_every_n = deep_research_every_n
    cfg.stuck_threshold = stuck_threshold
    cfg.prior_output_max_chars = 100
    loop.cfg = cfg
    loop.dry_run = dry_run
    loop._state = {
        "experiment_count":     0,
        "consecutive_discards": 0,
        "best_metric":          None,
        "best_commit":          None,
    }
    loop.task = {"editable_file": "x.py", "name": "test"}
    loop.loop_pid_file       = tmp / "loop.pid"
    loop.review_pending_file = tmp / "REVIEW_PENDING.json"
    loop.dry_run_dir         = tmp / "dry_run"
    return loop


# ── chaining + scheduling ─────────────────────────────────────────────────────

class InternChainingTests(unittest.TestCase):
    def test_truncate_for_chain_handles_unicode(self):
        # Unicode content should not crash the truncation
        text = "café " * 100
        out = truncate_for_chain(text, 50)
        self.assertTrue(out.startswith("café"))
        self.assertIn("[TRUNCATED", out)

    def test_format_prior_outputs_joins_and_caps(self):
        with tempfile.TemporaryDirectory() as td:
            loop = _stub_loop(Path(td))
            out = loop._format_prior_outputs([
                ("insights", "A" * 500),
                ("code_reviewer", "B" * 500),
            ])
            self.assertIn("Prior output from `insights`", out)
            self.assertIn("Prior output from `code_reviewer`", out)
            self.assertIn("[TRUNCATED", out)

    def test_format_prior_outputs_empty(self):
        with tempfile.TemporaryDirectory() as td:
            loop = _stub_loop(Path(td))
            self.assertEqual(loop._format_prior_outputs([]), "")


class SchedulingGateTests(unittest.TestCase):
    def test_expensive_set_contains_insights(self):
        self.assertIn("insights", _EXPENSIVE_INTERNS)

    def test_should_run_expensive_first_iteration(self):
        with tempfile.TemporaryDirectory() as td:
            loop = _stub_loop(Path(td), deep_research_every_n=5)
            self.assertTrue(loop._should_run_expensive(1))

    def test_should_run_expensive_every_n(self):
        with tempfile.TemporaryDirectory() as td:
            loop = _stub_loop(Path(td), deep_research_every_n=5)
            self.assertFalse(loop._should_run_expensive(2))
            self.assertFalse(loop._should_run_expensive(5))
            self.assertTrue(loop._should_run_expensive(6))   # 1 + 5
            self.assertTrue(loop._should_run_expensive(11))  # 1 + 2*5

    def test_should_run_expensive_when_stuck(self):
        with tempfile.TemporaryDirectory() as td:
            loop = _stub_loop(Path(td), deep_research_every_n=99,
                              stuck_threshold=3)
            self.assertFalse(loop._should_run_expensive(2))
            loop._state["consecutive_discards"] = 3
            self.assertTrue(loop._should_run_expensive(2))

    def test_is_expensive(self):
        with tempfile.TemporaryDirectory() as td:
            loop = _stub_loop(Path(td))
            self.assertTrue(loop._is_expensive("insights"))
            self.assertFalse(loop._is_expensive("code_reviewer"))


# ── retry + failure semantics ─────────────────────────────────────────────────

class RetrySemanticsTests(unittest.TestCase):
    def test_failure_detection(self):
        self.assertTrue(_is_failure(None))
        self.assertTrue(_is_failure(""))
        self.assertTrue(_is_failure("   "))
        self.assertTrue(_is_failure("[ERROR] something blew up"))
        self.assertFalse(_is_failure("real output"))
        self.assertFalse(_is_failure("INTERN_FAILED: x"))  # placeholder is not a re-retry trigger


# ── review pause gate ─────────────────────────────────────────────────────────

class ReviewGateTests(unittest.TestCase):
    def test_gate_blocks_when_pending_file_exists(self):
        with tempfile.TemporaryDirectory() as td:
            loop = _stub_loop(Path(td))
            loop.review_pending_file.write_text(
                json.dumps({"run_id": "exp_3", "rationale": "human check"}),
                encoding="utf-8",
            )
            self.assertTrue(loop._check_review_gate())

    def test_gate_passes_when_no_file(self):
        with tempfile.TemporaryDirectory() as td:
            loop = _stub_loop(Path(td))
            self.assertFalse(loop._check_review_gate())

    def test_write_review_pending_creates_file(self):
        with tempfile.TemporaryDirectory() as td:
            loop = _stub_loop(Path(td))
            loop._write_review_pending("exp_1", "needs human", 0.42)
            self.assertTrue(loop.review_pending_file.exists())
            data = json.loads(loop.review_pending_file.read_text(encoding="utf-8"))
            self.assertEqual(data["run_id"], "exp_1")
            self.assertEqual(data["rationale"], "needs human")
            self.assertEqual(data["metric"], 0.42)


# ── stale-PID crash recovery ──────────────────────────────────────────────────

class CrashRecoveryTests(unittest.TestCase):
    def test_no_pid_file_is_no_op(self):
        with tempfile.TemporaryDirectory() as td:
            loop = _stub_loop(Path(td))
            loop.workspace = SimpleNamespace(revert_file=lambda f: None,
                                              append_run_log=lambda m: None)
            loop._recover_from_stale_pid()  # should not raise

    def test_stale_pid_triggers_revert(self):
        with tempfile.TemporaryDirectory() as td:
            loop = _stub_loop(Path(td))
            # Write a PID that almost certainly does not exist
            dead_pid = 999_999_999
            loop.loop_pid_file.write_text(
                json.dumps({"pid": dead_pid}), encoding="utf-8",
            )
            reverted = []
            loop.workspace = SimpleNamespace(
                revert_file=lambda f: reverted.append(f),
                append_run_log=lambda m: None,
            )
            loop._recover_from_stale_pid()
            self.assertEqual(reverted, ["x.py"])
            self.assertFalse(loop.loop_pid_file.exists())

    def test_live_pid_refuses_to_start(self):
        with tempfile.TemporaryDirectory() as td:
            loop = _stub_loop(Path(td))
            my_pid = os.getpid()  # this process is alive by definition
            loop.loop_pid_file.write_text(
                json.dumps({"pid": my_pid}), encoding="utf-8",
            )
            loop.workspace = SimpleNamespace(revert_file=lambda f: None,
                                              append_run_log=lambda m: None)
            with self.assertRaises(RuntimeError) as cm:
                loop._recover_from_stale_pid()
            self.assertIn("already running", str(cm.exception))

    def test_pid_file_write_and_clear(self):
        with tempfile.TemporaryDirectory() as td:
            loop = _stub_loop(Path(td))
            loop._write_pid_file()
            self.assertTrue(loop.loop_pid_file.exists())
            data = json.loads(loop.loop_pid_file.read_text(encoding="utf-8"))
            self.assertEqual(data["pid"], os.getpid())
            loop._clear_pid_file()
            self.assertFalse(loop.loop_pid_file.exists())


# ── InternBus chain integration with a stubbed registry ───────────────────────

class _StubIntern:
    """An intern that returns whatever it was told to."""
    def __init__(self, name, output):
        self.name = name
        self._output = output

    def run(self, request, context):
        # Capture last context so the test can assert what the chain received
        self.last_context = dict(context)
        if isinstance(self._output, Exception):
            raise self._output
        return self._output


class _StubRegistry:
    def __init__(self, mapping):
        self._mapping = mapping

    def get_intern(self, name):
        return self._mapping[name]


class InternBusIntegrationTests(unittest.TestCase):
    """Verify retry + chain context flow through InternBus end-to-end."""

    def _make_bus(self, stubs, *, backoff=0):
        cfg = Config()
        cfg.intern_retry_backoff_s = backoff
        with tempfile.TemporaryDirectory() as td:
            cfg.root = Path(td)
            bus = InternBus.__new__(InternBus)
            bus.cfg = cfg
            bus.registry = _StubRegistry(stubs)
            bus.workspace = SimpleNamespace(log_intern_activity=lambda *a, **k: None)
            bus.telemetry = None
            return bus

    def test_success_first_try(self):
        stubs = {"a": _StubIntern("a", "hello")}
        bus = self._make_bus(stubs)
        out = bus.invoke("a", "req", {})
        self.assertEqual(out, "hello")

    def test_retry_on_exception_then_succeed(self):
        # Counter so the second call succeeds
        calls = {"n": 0}
        class Flaky:
            def run(self, req, ctx):
                calls["n"] += 1
                if calls["n"] == 1:
                    raise RuntimeError("boom")
                return "ok"
        bus = self._make_bus({"a": Flaky()})
        out = bus.invoke("a", "req", {})
        self.assertEqual(out, "ok")
        self.assertEqual(calls["n"], 2)

    def test_retry_on_empty_then_succeed(self):
        calls = {"n": 0}
        class Empty:
            def run(self, req, ctx):
                calls["n"] += 1
                return "" if calls["n"] == 1 else "good"
        bus = self._make_bus({"a": Empty()})
        out = bus.invoke("a", "req", {})
        self.assertEqual(out, "good")

    def test_two_failures_returns_placeholder(self):
        class AlwaysFails:
            def run(self, req, ctx):
                return "[ERROR] permanent"
        bus = self._make_bus({"a": AlwaysFails()})
        out = bus.invoke("a", "req", {})
        self.assertTrue(out.startswith("INTERN_FAILED:"))
        self.assertIn("twice", out)


if __name__ == "__main__":
    unittest.main()
