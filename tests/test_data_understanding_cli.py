"""Tests for the standalone data-understanding CLI (BUG-010 CLI surface).

The CLI must run the classifier on a workspace's generated profiles, write the
``interns/reports/data_understanding/current.{json,md}`` artifact, and (with
``--quiet``) print a compact one-line summary naming the detected tier. It must
own no classifier logic and stay workspace-agnostic.
"""
import io
import json
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path

from core.onboarding.data_model.data_understanding_cli import main, run_understand_data


def _write_profile_index(workspace: Path) -> None:
    """Write a tiny single-table profile_index in the shape onboarding emits."""
    profiles_dir = workspace / "interns" / "generated" / "profiles"
    profiles_dir.mkdir(parents=True, exist_ok=True)
    index = {
        "profiles": [
            {
                "table_name": "orders_raw",
                "path": "datasets/orders_raw.csv",
                "row_count": 1000,
                "columns": [
                    # Key has nulls + is non-unique -> raw/bronze signals.
                    {"name": "order_id", "dtype": "String", "null_count": 50, "distinct_count": 600},
                    {"name": "amount", "dtype": "String", "null_count": 100},
                    {"name": "status", "dtype": "String", "null_count": 80},
                ],
            }
        ]
    }
    (profiles_dir / "profile_index.json").write_text(json.dumps(index), encoding="utf-8")


class DataUnderstandingCliTests(unittest.TestCase):
    def test_run_writes_artifact_and_classifies_tier(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo_root = Path(tmp)
            workspace = repo_root / "workspaces" / "demo"
            workspace.mkdir(parents=True)
            _write_profile_index(workspace)

            summary = run_understand_data(repo_root, "workspaces/demo")

            self.assertEqual(summary["quality_tier"], "raw/bronze")
            self.assertGreaterEqual(summary["scoped_option_count"], 1)
            self.assertEqual(summary["profile_count"], 1)

            json_path = repo_root / summary["json_path"]
            md_path = repo_root / summary["markdown_path"]
            self.assertTrue(json_path.exists(), "current.json must be written")
            self.assertTrue(md_path.exists(), "current.md must be written")

            payload = json.loads(json_path.read_text(encoding="utf-8"))
            self.assertEqual(payload["artifact_type"], "data_understanding/current.json")
            self.assertEqual(payload["quality_tier"]["tier"], "raw/bronze")
            self.assertIn("schema_type", payload)

    def test_main_quiet_prints_compact_summary_naming_tier(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo_root = Path(tmp)
            workspace = repo_root / "workspaces" / "demo"
            workspace.mkdir(parents=True)
            _write_profile_index(workspace)

            buf = io.StringIO()
            with redirect_stdout(buf):
                rc = main([
                    "--workspace", "workspaces/demo",
                    "--repo-root", str(repo_root),
                    "--quiet",
                ])
            out = buf.getvalue().strip()

            self.assertEqual(rc, 0)
            # Compact: a single line.
            self.assertEqual(len(out.splitlines()), 1, f"--quiet must print one line, got: {out!r}")
            # Names the tier + carries the artifact path.
            self.assertIn("raw/bronze", out)
            self.assertIn("tier=", out)
            self.assertIn("data_understanding/current.json", out)

            # Full JSON + Markdown are still on disk even in --quiet mode.
            report_dir = workspace / "interns" / "reports" / "data_understanding"
            self.assertTrue((report_dir / "current.json").exists())
            self.assertTrue((report_dir / "current.md").exists())

    def test_main_default_prints_multiline_summary(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo_root = Path(tmp)
            workspace = repo_root / "workspaces" / "demo"
            workspace.mkdir(parents=True)
            _write_profile_index(workspace)

            buf = io.StringIO()
            with redirect_stdout(buf):
                rc = main([
                    "--workspace", "workspaces/demo",
                    "--repo-root", str(repo_root),
                ])
            out = buf.getvalue()

            self.assertEqual(rc, 0)
            self.assertIn("Quality tier: raw/bronze", out)
            self.assertIn("Artifact (json):", out)


if __name__ == "__main__":
    unittest.main()
