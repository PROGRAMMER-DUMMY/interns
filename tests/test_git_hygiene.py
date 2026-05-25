from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from tools.git_hygiene import classify_path, validate_paths


class GitHygieneTests(unittest.TestCase):
    def test_blocks_raw_data_extension(self):
        with tempfile.TemporaryDirectory() as td:
            issues = classify_path(
                "workspaces/project/datasets/raw.csv",
                repo_root=Path(td),
                max_bytes=25 * 1024 * 1024,
            )
        self.assertTrue(any(issue.rule == "raw_or_large_data_extension" for issue in issues))

    def test_blocks_workspace_interns_output(self):
        with tempfile.TemporaryDirectory() as td:
            issues = classify_path(
                "workspaces/project/interns/generated/contracts/kpi_registry.json",
                repo_root=Path(td),
                max_bytes=25 * 1024 * 1024,
            )
        self.assertTrue(
            any(issue.rule == "protected_workspace_generated_output" for issue in issues)
        )

    def test_blocks_large_unknown_extension(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            path = root / "large_export.txt"
            path.write_bytes(b"x" * 11)
            issues = classify_path(path.name, repo_root=root, max_bytes=10)
        self.assertTrue(any(issue.rule == "file_size_limit" for issue in issues))

    def test_allows_normal_source_file(self):
        with tempfile.TemporaryDirectory() as td:
            issues = validate_paths(
                ["core/example.py", "workspace_scenarios.md"],
                repo_root=Path(td),
                max_bytes=25 * 1024 * 1024,
            )
        self.assertEqual(issues, [])


if __name__ == "__main__":
    unittest.main()
