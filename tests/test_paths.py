from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from core.paths import find_project_root, project_path, relative_to_project


class ProjectPathsTests(unittest.TestCase):
    def test_find_project_root_uses_parent_markers(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            nested = root / "a" / "b"
            nested.mkdir(parents=True)
            (root / "pyproject.toml").write_text("[project]\nname='demo'\n", encoding="utf-8")

            self.assertEqual(find_project_root(nested), root.resolve())

    def test_project_path_rejects_absolute_parts(self) -> None:
        with self.assertRaises(ValueError):
            project_path(Path.cwd())

    def test_relative_to_project_uses_posix_separators(self) -> None:
        path = project_path("core", "paths.py")

        self.assertEqual(relative_to_project(path), "core/paths.py")


if __name__ == "__main__":
    unittest.main()
