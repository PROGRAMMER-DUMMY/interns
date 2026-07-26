"""Regression: `run-kpi-pipeline` must not exit 0 without emitting a result packet.

Origin (2026-07-26 agy-harness audit): `pipeline_main`'s terminal fallthrough for an
unrecognised flow status printed a `[~]` note and returned 0. Every completion gate
(BUG-014 provenance, stale-packet, result-content review) lives inside the
`status == "complete"` branch, so that path emits no packet and runs no gate -- yet
told the caller, and any agent reading the exit code, that the run had succeeded.

Guarded structurally: the source of the fallthrough must return non-zero. A
behavioural test would need a full workspace fixture; this asserts the invariant
that actually regressed.
"""
from __future__ import annotations

import inspect
import re
import unittest

from core.onboarding.workspace import flow


class PipelineExitCodeTests(unittest.TestCase):
    def setUp(self):
        self.src = inspect.getsource(flow.pipeline_main)

    def test_terminal_fallthrough_returns_nonzero(self):
        # The last `return` statement in pipeline_main is the unexpected-status path.
        returns = re.findall(r"^\s*return\s+(\d+)\s*$", self.src, re.M)
        self.assertTrue(returns, "no literal integer returns found in pipeline_main")
        self.assertNotEqual(
            returns[-1], "0",
            "pipeline_main's terminal fallthrough returns 0: an unexpected flow status "
            "would report success with no result packet emitted.",
        )

    def test_success_is_only_returned_inside_the_complete_branch(self):
        # `return 0` must appear only after the `status == "complete"` guard.
        complete_guard = self.src.find('result.status == "complete"')
        self.assertGreater(complete_guard, -1, "the completion guard has moved or changed")
        for match in re.finditer(r"^\s*return\s+0\s*$", self.src, re.M):
            self.assertGreater(
                match.start(), complete_guard,
                "pipeline_main returns 0 before/outside the `status == \"complete\"` "
                "branch, bypassing every completion gate.",
            )


if __name__ == "__main__":
    unittest.main()
