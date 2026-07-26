"""Production-readiness fix, P3: core/agents/llm_engine.py's APIEngine.generate
(the Gemini API path) built its request body with `user[:4000]` -- a hard,
silent character truncation. Confirmed live/reachable via core/agents/registry.py
and core/config.py wiring, not dead code. Any evidence/context beyond 4000
characters was dropped with zero signal to the caller or the model itself.

Fixed: `_truncate_with_signal` logs a warning (flows through the already-
installed root-logger RedactionFilter) and appends an explicit
"[...truncated, N characters omitted...]" marker, so the model reasons over
a prompt it knows is incomplete rather than one that silently looks whole.
Text under the limit is passed through byte-identical.

See ~/.claude/plans/dynamic-cooking-firefly.md P3.
"""
from __future__ import annotations

import logging
import unittest

from core.agents.llm_engine import _GEMINI_USER_TEXT_LIMIT, _truncate_with_signal


class TruncationSignalTests(unittest.TestCase):
    def test_text_under_limit_is_unchanged(self):
        text = "a" * 100
        self.assertEqual(_truncate_with_signal(text, 4000), text)

    def test_text_at_exact_limit_is_unchanged(self):
        text = "a" * 4000
        self.assertEqual(_truncate_with_signal(text, 4000), text)

    def test_text_over_limit_is_truncated_with_marker(self):
        text = "a" * 5000
        result = _truncate_with_signal(text, 4000)
        self.assertNotEqual(result, text)
        self.assertIn("truncated", result)
        self.assertIn("1000 characters omitted", result)
        self.assertTrue(result.startswith("a" * 4000))

    def test_truncation_logs_a_warning(self):
        with self.assertLogs("core.agents.llm_engine", level="WARNING") as ctx:
            _truncate_with_signal("a" * 5000, 4000)
        self.assertTrue(any("truncated" in msg for msg in ctx.output))

    def test_default_gemini_limit_constant_used_by_generate(self):
        self.assertEqual(_GEMINI_USER_TEXT_LIMIT, 4000)


if __name__ == "__main__":
    unittest.main()
