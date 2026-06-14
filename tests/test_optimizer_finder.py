"""
tests/test_optimizer_finder.py -- regression tests for the code-injection fix in
PythonOptimizer._profile_memory.

The vulnerability: the original implementation f-string interpolated the CLI-supplied
target path directly into the Python -c code string passed to subprocess.  A path
containing a single quote (or other Python-significant characters) could break the
child interpreter's syntax or inject arbitrary code.

The fix: the path is now passed exclusively via the OPTIMIZER_FINDER_TARGET environment
variable; the -c code string contains no reference to the literal path value.

These tests verify:
  1. The -c code string built by _profile_memory does NOT contain the literal path.
  2. End-to-end: running _profile_memory on a target whose path contains a single quote
     (and/or a space) returns the expected list structure without raising a SyntaxError
     or other injection-caused exception.
"""
from __future__ import annotations

import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_trivial_py(directory: str, name: str) -> str:
    """Write a trivial valid Python script into directory/name and return its path."""
    p = Path(directory) / name
    p.write_text("x = 1 + 1\n", encoding="utf-8")
    return str(p)


def _can_create_file(directory: str, name: str) -> bool:
    """Return True if the OS lets us create a file with that name."""
    try:
        path = Path(directory) / name
        path.write_text("", encoding="utf-8")
        path.unlink()
        return True
    except (OSError, ValueError):
        return False


# ---------------------------------------------------------------------------
# Import the class under test (lazy so import errors surface clearly)
# ---------------------------------------------------------------------------

def _get_optimizer():
    from tools.optimizer_finder import PythonOptimizer
    return PythonOptimizer()


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

class TestCodeStringDoesNotContainPath(unittest.TestCase):
    """
    Unit-level check: verify that the -c code string passed to subprocess does NOT
    embed the literal target path, regardless of what characters the path contains.
    This is the primary regression guard for the injection fix.
    """

    def _capture_subprocess_cmd(self, path: str):
        """
        Run _profile_memory while intercepting the subprocess.run call.
        Returns the args list that subprocess.run was called with.
        """
        captured = {}

        def fake_run(args, **kwargs):
            captured["args"] = args
            captured["env"]  = kwargs.get("env", {})
            m = MagicMock()
            m.stdout = "[]"
            return m

        from tools import optimizer_finder
        opt = optimizer_finder.PythonOptimizer()
        with patch.object(optimizer_finder.subprocess, "run", side_effect=fake_run):
            opt._profile_memory(path)

        return captured

    def test_path_with_single_quote_not_in_code_string(self):
        """A path containing a single quote must not appear in the -c code text."""
        tricky_path = r"C:\Users\od'd test\target.py"
        captured = self._capture_subprocess_cmd(tricky_path)

        args = captured.get("args", [])
        # args should be [sys.executable, "-c", <code_string>]
        self.assertGreaterEqual(len(args), 3, "subprocess.run args list too short")
        code_string = args[2]

        self.assertNotIn(
            tricky_path, code_string,
            "The literal path was found inside the -c code string — injection not fixed."
        )
        # Also assert the single-quote character itself did not sneak in through the path
        # (the code string may legitimately contain single quotes from its own syntax,
        # so we only check the full path substring is absent, not all single quotes).
        self.assertNotIn(
            "od'd", code_string,
            "A path fragment with a single quote was found inside the -c code string."
        )

    def test_path_travels_via_env_var(self):
        """The target path must be delivered to the child via OPTIMIZER_FINDER_TARGET."""
        target = r"C:\some path\with spaces\script.py"
        captured = self._capture_subprocess_cmd(target)

        env = captured.get("env", {})
        self.assertIn(
            "OPTIMIZER_FINDER_TARGET", env,
            "OPTIMIZER_FINDER_TARGET not set in subprocess env."
        )
        self.assertEqual(
            env["OPTIMIZER_FINDER_TARGET"], target,
            "OPTIMIZER_FINDER_TARGET value does not match the supplied path."
        )

    def test_plain_path_not_in_code_string(self):
        """Even a plain path must not appear literally in the code string."""
        plain = r"C:\projects\myapp\main.py"
        captured = self._capture_subprocess_cmd(plain)
        code_string = captured["args"][2]
        self.assertNotIn(plain, code_string)


class TestEndToEndSingleQuoteInPath(unittest.TestCase):
    """
    End-to-end test: create a real temp file whose path includes a single quote
    (or a directory with shell-significant characters if the OS allows), run
    _profile_memory on it, and assert we get the expected return structure back
    without a SyntaxError or injection exception.

    This test is skipped if the platform cannot create a filename with a single quote.
    """

    @classmethod
    def setUpClass(cls):
        cls.tmpdir = tempfile.mkdtemp()

    @classmethod
    def tearDownClass(cls):
        import shutil
        shutil.rmtree(cls.tmpdir, ignore_errors=True)

    def test_single_quote_in_filename(self):
        """Run the memory profiler on a file whose name contains a single quote."""
        filename = "od'd_target.py"
        if not _can_create_file(self.tmpdir, filename):
            self.skipTest(
                f"OS does not allow single-quote in filename ({sys.platform}); "
                "skipping end-to-end test."
            )

        target = _make_trivial_py(self.tmpdir, filename)

        from tools.optimizer_finder import PythonOptimizer
        opt = PythonOptimizer()

        # Should not raise; returns a list (possibly empty on slow/restricted envs)
        try:
            result = opt._profile_memory(target)
        except SyntaxError as exc:
            self.fail(
                f"_profile_memory raised SyntaxError — path may still be interpolated "
                f"into code string: {exc}"
            )

        self.assertIsInstance(result, list, "_profile_memory must return a list")

    def test_space_in_directory_name(self):
        """Run the memory profiler on a file inside a directory whose name has spaces."""
        subdir = os.path.join(self.tmpdir, "my project dir")
        os.makedirs(subdir, exist_ok=True)
        target = _make_trivial_py(subdir, "script.py")

        from tools.optimizer_finder import PythonOptimizer
        opt = PythonOptimizer()

        try:
            result = opt._profile_memory(target)
        except SyntaxError as exc:
            self.fail(
                f"_profile_memory raised SyntaxError on a path with spaces: {exc}"
            )

        self.assertIsInstance(result, list)


class TestImportSanity(unittest.TestCase):
    """Ensure the module can be imported without errors after the fix."""

    def test_import(self):
        import tools.optimizer_finder  # noqa: F401 — import is the assertion


if __name__ == "__main__":
    unittest.main()
