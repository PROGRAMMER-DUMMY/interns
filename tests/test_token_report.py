from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from tools.token_report import (
    Section,
    build_snapshot,
    cli_context_sections,
    measure,
    render_compare,
    resolve_token_counter,
)


class TokenReportTests(unittest.TestCase):
    """The token reporter must be generic (discover each CLI's pinned context from
    its own config, skip absent CLIs), count tokens deterministically, and support
    a before/after compare so a change's token impact is measured, not eyeballed."""

    def test_counter_is_deterministic_and_nonzero(self):
        _method, counter = resolve_token_counter()
        self.assertEqual(counter("hello world"), counter("hello world"))
        self.assertGreater(counter("some non-empty text"), 0)
        self.assertEqual(counter(""), 0)

    def test_measure_existing_and_missing_files(self):
        _method, counter = resolve_token_counter()
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            f = root / "a.md"
            f.write_text("abcd" * 100, encoding="utf-8")
            present = measure(f, root, counter)
            self.assertTrue(present.exists)
            self.assertEqual(present.bytes, 400)
            self.assertGreater(present.tokens, 0)
            self.assertEqual(present.path, "a.md")

            absent = measure(root / "nope.md", root, counter)
            self.assertFalse(absent.exists)
            self.assertEqual(absent.tokens, 0)

    def test_cli_context_discovers_gemini_and_skips_absent(self):
        _method, counter = resolve_token_counter()
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "GEMINI.md").write_text("gemini instructions\n" * 50, encoding="utf-8")
            (root / ".gemini").mkdir()
            (root / ".gemini" / "settings.json").write_text(
                json.dumps({"context": {"fileName": ["GEMINI.md"], "includeDirectoryTree": True}}),
                encoding="utf-8",
            )
            # No CLAUDE.md -> claude provider must be skipped.
            sections = cli_context_sections(root, counter)
            names = {s.name for s in sections}
            self.assertIn("cli_context:gemini", names)
            self.assertNotIn("cli_context:claude", names)
            gemini = next(s for s in sections if s.name == "cli_context:gemini")
            self.assertEqual(len(gemini.items), 1)
            self.assertGreater(gemini.total_tokens, 0)
            # includeDirectoryTree note is surfaced on the first item.
            self.assertIn("includeDirectoryTree", gemini.items[0].note)

    def test_build_snapshot_grand_total_sums_sections(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "CLAUDE.md").write_text("claude memory\n" * 20, encoding="utf-8")
            snap = build_snapshot(root, label="t")
            self.assertEqual(
                snap.grand_total_tokens, sum(s.total_tokens for s in snap.sections)
            )
            self.assertTrue(any(s.name == "cli_context:claude" for s in snap.sections))

    def test_render_compare_computes_delta(self):
        baseline = {
            "label": "before",
            "token_method": "heuristic:utf8-bytes/4",
            "grand_total_tokens": 100,
            "sections": [{"name": "cli_context:gemini", "total_tokens": 100}],
        }

        class _Snap:
            label = "after"
            token_method = "heuristic:utf8-bytes/4"
            grand_total_tokens = 40
            sections = [Section(name="cli_context:gemini")]

        # Make the after-section report 40 tokens via a synthetic item.
        from tools.token_report import FileTokens

        _Snap.sections[0].items.append(
            FileTokens(path="GEMINI.md", exists=True, bytes=160, tokens=40)
        )
        out = render_compare(baseline, _Snap())
        self.assertIn("-60", out)
        self.assertIn("GRAND TOTAL", out)


if __name__ == "__main__":
    unittest.main()
