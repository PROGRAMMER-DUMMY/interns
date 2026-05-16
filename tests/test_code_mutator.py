"""Unit tests for core/agents/code_mutator.py."""
from __future__ import annotations

import unittest
from unittest.mock import patch

from core.config import Config
from core.agents.code_mutator import (
    CodeMutator,
    _CODE_BLOCK_RE,
)
from core.agents.intern_bus import truncate_for_chain


def _cfg(**overrides) -> Config:
    cfg = Config()
    cfg.main_agent = "gemini-cli"
    cfg.main_agent_timeout_sec = 5
    cfg.max_editable_file_kb = 10
    cfg.prior_output_max_chars = 100
    for k, v in overrides.items():
        setattr(cfg, k, v)
    return cfg


class CodeBlockExtractionTests(unittest.TestCase):
    """Regex extraction must handle real-world model outputs."""

    def test_extracts_python_block(self):
        out = "Here is the rewrite:\n```python\ndef f():\n    return 42\n```\nDone."
        m = _CODE_BLOCK_RE.search(out)
        self.assertIsNotNone(m)
        self.assertEqual(m.group("body"), "def f():\n    return 42")

    def test_extracts_sql_block(self):
        out = "```sql\nSELECT 1\n```"
        m = _CODE_BLOCK_RE.search(out)
        self.assertIsNotNone(m)
        self.assertEqual(m.group("body"), "SELECT 1")

    def test_extracts_unlabelled_block(self):
        out = "```\nplain text\n```"
        m = _CODE_BLOCK_RE.search(out)
        self.assertIsNotNone(m)
        self.assertEqual(m.group("body"), "plain text")

    def test_no_block_returns_none(self):
        m = _CODE_BLOCK_RE.search("Sorry, I cannot help with that.")
        self.assertIsNone(m)

    def test_first_block_wins_on_multiple(self):
        out = "```python\nfirst\n```\n```python\nsecond\n```"
        m = _CODE_BLOCK_RE.search(out)
        self.assertEqual(m.group("body"), "first")


class SyntaxValidationTests(unittest.TestCase):
    def setUp(self):
        self.mut = CodeMutator(_cfg())

    def test_valid_python(self):
        ok, err = self.mut._validate_syntax("def f():\n    return 1\n", "python")
        self.assertTrue(ok)
        self.assertIsNone(err)

    def test_invalid_python(self):
        ok, err = self.mut._validate_syntax("def f(:\n  pass\n", "python")
        self.assertFalse(ok)
        self.assertIn("line", err)

    def test_empty_python(self):
        # Empty Python file is technically valid syntax.
        ok, _ = self.mut._validate_syntax("", "python")
        self.assertTrue(ok)

    def test_valid_sql(self):
        ok, err = self.mut._validate_syntax("SELECT 1 FROM t", "sql")
        self.assertTrue(ok)
        self.assertIsNone(err)

    def test_empty_sql_rejected(self):
        ok, err = self.mut._validate_syntax("   ", "sql")
        self.assertFalse(ok)

    def test_sql_without_keywords_rejected(self):
        ok, err = self.mut._validate_syntax("hello world\nno keywords here", "sql")
        self.assertFalse(ok)
        self.assertIn("keyword", err.lower())

    def test_sql_with_stray_fence_rejected(self):
        ok, err = self.mut._validate_syntax("SELECT 1\n```", "sql")
        self.assertFalse(ok)

    def test_valid_json(self):
        ok, _ = self.mut._validate_syntax('{"a": 1}', "json")
        self.assertTrue(ok)

    def test_invalid_json(self):
        ok, err = self.mut._validate_syntax("{not json}", "json")
        self.assertFalse(ok)

    def test_unknown_lang_passes(self):
        ok, _ = self.mut._validate_syntax("anything goes", "text")
        self.assertTrue(ok)


class LangDetectionTests(unittest.TestCase):
    def test_python(self):
        self.assertEqual(CodeMutator._detect_lang("foo/bar.py"), "python")

    def test_sql(self):
        self.assertEqual(CodeMutator._detect_lang("foo/bar.SQL"), "sql")

    def test_yaml(self):
        self.assertEqual(CodeMutator._detect_lang("manifest.yaml"), "yaml")
        self.assertEqual(CodeMutator._detect_lang("manifest.yml"), "yaml")

    def test_unknown(self):
        self.assertEqual(CodeMutator._detect_lang("foo.xyz"), "text")
        self.assertEqual(CodeMutator._detect_lang(""), "text")


class TruncationTests(unittest.TestCase):
    def test_under_cap_returns_unchanged(self):
        self.assertEqual(truncate_for_chain("hello", 100), "hello")

    def test_over_cap_truncates_and_marks(self):
        long = "x" * 500
        out = truncate_for_chain(long, 100)
        self.assertTrue(out.startswith("x" * 100))
        self.assertIn("[TRUNCATED", out)

    def test_empty_returns_empty(self):
        self.assertEqual(truncate_for_chain("", 100), "")
        self.assertEqual(truncate_for_chain(None, 100), "")


class PromptConstructionTests(unittest.TestCase):
    def setUp(self):
        self.mut = CodeMutator(_cfg())

    def test_prompt_contains_all_sections(self):
        prompt = self.mut._build_prompt(
            editable_file="exp.py",
            file_content="x = 1",
            intern_reports=["report A", "report B"],
            semantic_contract_text="must keep output schema",
            best_metric=0.42,
            optimization_plan="try X then Y",
            experiment_count=3,
            lang="python",
        )
        self.assertIn("exp.py", prompt)
        self.assertIn("0.42", prompt)
        self.assertIn("must keep output schema", prompt)
        self.assertIn("try X then Y", prompt)
        self.assertIn("report A", prompt)
        self.assertIn("report B", prompt)
        self.assertIn("```python", prompt)
        self.assertIn("x = 1", prompt)
        self.assertIn("iteration #3", prompt)

    def test_prompt_handles_no_intern_reports(self):
        prompt = self.mut._build_prompt(
            editable_file="exp.py", file_content="", intern_reports=[],
            semantic_contract_text="c", best_metric=None,
            optimization_plan="p", experiment_count=1, lang="python",
        )
        self.assertIn("no intern reports", prompt)


class MutationPreflightTests(unittest.TestCase):
    """Pre-flight checks short-circuit before spawning the subprocess."""

    def test_file_too_big_returns_failure(self):
        mut = CodeMutator(_cfg(max_editable_file_kb=1))
        result = mut.mutate(
            editable_file="big.py",
            file_content="x" * (2 * 1024),   # 2KB > 1KB cap
            intern_reports=[],
            semantic_contract_text="",
            best_metric=None,
            optimization_plan="",
            experiment_count=1,
        )
        self.assertFalse(result.success)
        self.assertIn("KB", result.error)

    def test_unknown_main_agent_returns_failure(self):
        cfg = _cfg(main_agent="unknown-cli")
        mut = CodeMutator(cfg)
        result = mut.mutate(
            editable_file="x.py", file_content="x=1",
            intern_reports=[], semantic_contract_text="",
            best_metric=None, optimization_plan="", experiment_count=1,
        )
        self.assertFalse(result.success)
        self.assertIn("unknown main_agent", result.error)

    @patch("core.agents.code_mutator.shutil.which", return_value=None)
    def test_missing_cli_returns_failure(self, _which):
        mut = CodeMutator(_cfg())
        result = mut.mutate(
            editable_file="x.py", file_content="x=1",
            intern_reports=[], semantic_contract_text="",
            best_metric=None, optimization_plan="", experiment_count=1,
        )
        self.assertFalse(result.success)
        self.assertIn("not in PATH", result.error)


class MutationSubprocessTests(unittest.TestCase):
    """Mock the subprocess to test stdout-handling paths end-to-end."""

    def _run_mutate(self, fake_stdout: str, returncode: int = 0,
                    *, stderr: str = "", lang_file: str = "x.py"):
        fake_proc = type("P", (), {})()
        fake_proc.returncode = returncode
        fake_proc.stdout = fake_stdout
        fake_proc.stderr = stderr
        cfg = _cfg()
        mut = CodeMutator(cfg)
        with patch("core.agents.code_mutator.shutil.which", return_value="gemini"), \
             patch("core.agents.code_mutator.subprocess.run", return_value=fake_proc):
            return mut.mutate(
                editable_file=lang_file,
                file_content="x = 0\n",
                intern_reports=["foo"],
                semantic_contract_text="",
                best_metric=None,
                optimization_plan="",
                experiment_count=1,
            )

    def test_happy_path_python(self):
        out = "Here:\n```python\nx = 99\n```\n"
        result = self._run_mutate(out)
        self.assertTrue(result.success, msg=result.error)
        self.assertEqual(result.new_content, "x = 99")
        self.assertEqual(result.lang, "python")

    def test_no_code_block_fails(self):
        result = self._run_mutate("I cannot help with that.")
        self.assertFalse(result.success)
        self.assertIn("no fenced code block", result.error)

    def test_empty_code_block_fails(self):
        result = self._run_mutate("```python\n   \n```")
        self.assertFalse(result.success)
        self.assertIn("empty", result.error)

    def test_invalid_python_syntax_fails(self):
        result = self._run_mutate("```python\ndef bad(:\n  pass\n```")
        self.assertFalse(result.success)
        self.assertIn("syntax", result.error)

    def test_nonzero_exit_fails(self):
        result = self._run_mutate("```python\nx = 1\n```", returncode=2,
                                  stderr="boom")
        self.assertFalse(result.success)
        self.assertIn("exit 2", result.error)

    def test_sql_happy_path(self):
        out = "```sql\nSELECT * FROM t WHERE x = 1\n```"
        result = self._run_mutate(out, lang_file="query.sql")
        self.assertTrue(result.success, msg=result.error)
        self.assertEqual(result.lang, "sql")
        self.assertIn("SELECT", result.new_content)

    def test_ansi_stripped(self):
        out = "\x1b[32m```python\nx = 1\n```\x1b[0m"
        result = self._run_mutate(out)
        self.assertTrue(result.success)


if __name__ == "__main__":
    unittest.main()
