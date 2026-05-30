import io
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path

from core.onboarding.workspace.handoff_cli import main


def _write_handoff(root: Path, name: str, body: str) -> None:
    d = root / "workspaces" / "demo" / "interns" / "state" / "handoffs"
    d.mkdir(parents=True, exist_ok=True)
    (d / name).write_text(body, encoding="utf-8")


def _run(argv) -> tuple[int, str]:
    buf = io.StringIO()
    with redirect_stdout(buf):
        code = main(argv)
    return code, buf.getvalue()


class HandoffCLITests(unittest.TestCase):
    def test_render_specific_handoff(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            _write_handoff(root, "kpi_output_verification__kpi-analyst.md", "# Handoff\n## Task\ncheck it")
            code, out = _run(["render", "--workspace", "workspaces/demo", "--repo-root", str(root),
                              "--stage", "kpi_output_verification", "--agent", "kpi-analyst"])
            self.assertEqual(code, 0)
            self.assertIn("## Task", out)

    def test_latest_returns_newest(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            _write_handoff(root, "a__x.md", "OLD")
            _write_handoff(root, "b__y.md", "NEW-LATEST")
            code, out = _run(["latest", "--workspace", "workspaces/demo", "--repo-root", str(root)])
            self.assertEqual(code, 0)
            self.assertIn("NEW-LATEST", out)

    def test_latest_missing_is_graceful(self):
        with tempfile.TemporaryDirectory() as tmp:
            code, out = _run(["latest", "--workspace", "workspaces/demo", "--repo-root", tmp])
            self.assertEqual(code, 1)
            self.assertIn("no handoff", out.lower())


if __name__ == "__main__":
    unittest.main()
