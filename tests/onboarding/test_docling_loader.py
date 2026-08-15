"""Contract tests for the isolated docling loader.

No docling install, no model download, no network: the subprocess boundary is injected
via ``runner=``, so CI exercises the full decision path (preflight, isolation, fallback)
against fixtures. Every test pins ``root=`` to a temp dir so a developer who really has
``.venv_docling`` on disk cannot flip these results.
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT))

from core.onboarding.documents import docling_loader as dl  # noqa: E402
from core.onboarding.documents import docling_runner as dr  # noqa: E402


def completed(returncode: int = 0, stdout: str = "", stderr: str = "") -> SimpleNamespace:
    return SimpleNamespace(returncode=returncode, stdout=stdout, stderr=stderr)


def make_isolated_env(root: Path) -> Path:
    """Create the interpreter path layout the loader looks for (an empty file is enough)."""
    interpreter = dl._venv_python(root / dl.VENV_DIR)
    interpreter.parent.mkdir(parents=True, exist_ok=True)
    interpreter.write_text("", encoding="utf-8")
    return interpreter


class PreflightTest(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self._env = mock.patch.dict(os.environ, {}, clear=False)
        self._env.start()
        os.environ.pop(dl.ENV_OVERRIDE, None)

    def tearDown(self) -> None:
        self._env.stop()
        self._tmp.cleanup()

    def test_missing_env_reports_install_command(self) -> None:
        result = dl.can_parse_with_docling(self.root)
        self.assertFalse(result.available)
        self.assertIn(dl.VENV_DIR, result.next_step)
        self.assertIn("uv venv", result.next_step)
        # The diagnostic must say WHY it isn't in the primary venv.
        self.assertIn("torch", result.reason)

    def test_env_override_pointing_nowhere_is_unavailable(self) -> None:
        os.environ[dl.ENV_OVERRIDE] = str(self.root / "nope" / "python.exe")
        result = dl.can_parse_with_docling(self.root)
        self.assertFalse(result.available)

    def test_interpreter_present_but_import_fails(self) -> None:
        make_isolated_env(self.root)
        result = dl.can_parse_with_docling(
            self.root, runner=lambda *a, **k: completed(1, stderr="ModuleNotFoundError")
        )
        self.assertFalse(result.available)
        self.assertIn("import docling", result.reason)
        self.assertIn("uv pip install", result.next_step)

    def test_available_reports_version(self) -> None:
        make_isolated_env(self.root)
        result = dl.can_parse_with_docling(
            self.root, runner=lambda *a, **k: completed(0, stdout="2.55.1\n")
        )
        self.assertTrue(result.available)
        self.assertEqual(result.version, "2.55.1")

    def test_runner_exception_is_not_raised(self) -> None:
        make_isolated_env(self.root)

        def boom(*_a, **_k):
            raise OSError("exec format error")

        result = dl.can_parse_with_docling(self.root, runner=boom)
        self.assertFalse(result.available)
        self.assertIn("exec format error", result.reason)

    def test_install_command_is_platform_correct(self) -> None:
        command = dl.install_command(self.root)
        expected = "Scripts/python.exe" if os.name == "nt" else "bin/python"
        self.assertIn(expected, command)


class ParseDocumentTest(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.pdf = self.root / "sample.pdf"
        self.pdf.write_text("%PDF-1.4 stub", encoding="utf-8")
        self._env = mock.patch.dict(os.environ, {}, clear=False)
        self._env.start()
        os.environ.pop(dl.ENV_OVERRIDE, None)

    def tearDown(self) -> None:
        self._env.stop()
        self._tmp.cleanup()

    def _runner_writing(self, payload: dict, *, returncode: int = 0):
        """Fake subprocess that fulfils the runner contract: write JSON to --out."""

        def run(cmd, **_kwargs):
            if "-c" in cmd:  # the preflight probe
                return completed(0, stdout="2.55.1\n")
            out_path = Path(cmd[cmd.index("--out") + 1])
            out_path.write_text(json.dumps(payload), encoding="utf-8")
            return completed(returncode)

        return run

    def test_missing_input_is_not_an_engine_fallback(self) -> None:
        result = dl.parse_document(self.root / "absent.pdf", root=self.root)
        self.assertFalse(result.ok)
        self.assertFalse(result.fallback_recommended)

    def test_unavailable_engine_recommends_fallback(self) -> None:
        result = dl.parse_document(self.pdf, root=self.root)
        self.assertFalse(result.ok)
        self.assertTrue(result.fallback_recommended)
        self.assertIn("uv venv", result.next_step)

    def test_happy_path_returns_markdown_and_structured_tables(self) -> None:
        make_isolated_env(self.root)
        payload = {
            "ok": True,
            "engine": "docling",
            "engine_version": "2.55.1",
            "source_file": str(self.pdf),
            "markdown": "## Denials\n\nsome prose",
            "tables": [
                {
                    "index": 0,
                    "columns": ["Payer", "Denied"],
                    "rows": [["Aetna", "120"], ["Cigna", "88"]],
                    "num_rows": 2,
                    "num_cols": 2,
                }
            ],
        }
        result = dl.parse_document(
            self.pdf, root=self.root, runner=self._runner_writing(payload)
        )
        self.assertTrue(result.ok)
        self.assertEqual(result.engine_version, "2.55.1")
        self.assertEqual(len(result.tables), 1)
        self.assertEqual(result.tables[0]["rows"][1], ["Cigna", "88"])
        self.assertIn("Denials", result.markdown)
        self.assertEqual(result.summary()["table_count"], 1)

    def test_conversion_failure_recommends_fallback(self) -> None:
        make_isolated_env(self.root)
        payload = {"ok": False, "reason": "RuntimeError: encrypted PDF"}
        result = dl.parse_document(
            self.pdf, root=self.root, runner=self._runner_writing(payload, returncode=2)
        )
        self.assertFalse(result.ok)
        self.assertTrue(result.fallback_recommended)
        self.assertIn("encrypted PDF", result.reason)

    def test_runner_writing_no_payload_recommends_fallback(self) -> None:
        make_isolated_env(self.root)

        def run(cmd, **_kwargs):
            if "-c" in cmd:
                return completed(0, stdout="2.55.1\n")
            return completed(1)  # writes nothing

        result = dl.parse_document(self.pdf, root=self.root, runner=run)
        self.assertFalse(result.ok)
        self.assertTrue(result.fallback_recommended)

    def test_timeout_is_caught_not_raised(self) -> None:
        make_isolated_env(self.root)

        def run(cmd, **_kwargs):
            if "-c" in cmd:
                return completed(0, stdout="2.55.1\n")
            raise TimeoutError("docling exceeded timeout")

        result = dl.parse_document(self.pdf, root=self.root, runner=run)
        self.assertFalse(result.ok)
        self.assertTrue(result.fallback_recommended)


class RunnerTableExtractionTest(unittest.TestCase):
    """The runner must survive docling API drift and one bad table."""

    class _Frame:
        columns = ["A", "B"]
        values = SimpleNamespace(tolist=lambda: [["1", "2"]])

    def test_export_uses_doc_kwarg(self) -> None:
        seen = {}

        class Table:
            def export_to_dataframe(self, doc=None):
                seen["doc"] = doc
                return RunnerTableExtractionTest._Frame()

        payload = dr._table_payload(Table(), "DOCUMENT", 0)
        self.assertEqual(seen["doc"], "DOCUMENT")
        self.assertEqual(payload["columns"], ["A", "B"])
        self.assertEqual(payload["num_rows"], 1)

    def test_falls_back_to_legacy_signature(self) -> None:
        class LegacyTable:
            def export_to_dataframe(self):
                return RunnerTableExtractionTest._Frame()

        payload = dr._table_payload(LegacyTable(), "DOCUMENT", 3)
        self.assertEqual(payload["index"], 3)
        self.assertEqual(payload["rows"], [["1", "2"]])


if __name__ == "__main__":
    unittest.main()
