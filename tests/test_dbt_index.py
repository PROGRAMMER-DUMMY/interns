"""core.orchestration.dbt_index: offline `ref()` lineage + blast radius.

No warehouse and no dbt install -- the graph is read straight out of the
generated `.sql` files, which is the point.
"""
from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from core.orchestration.dbt_index import DbtIndex, build_index
from core.storage.workspace_layout import WorkspaceLayout
from tests.test_dbt_backfill import _build_real_dbt_project


def _fake_project(root: Path, models: dict[str, str]) -> Path:
    workspace = root / "workspaces" / "demo"
    dbt_dir = workspace / "dbt"
    (dbt_dir / "models" / "marts").mkdir(parents=True)
    (dbt_dir / "dbt_project.yml").write_text("name: demo\n", encoding="utf-8")
    for name, sql in models.items():
        (dbt_dir / "models" / "marts" / f"{name}.sql").write_text(sql, encoding="utf-8")
    WorkspaceLayout(project_root=workspace).ensure_runtime_dirs()
    return dbt_dir


class GraphTests(unittest.TestCase):
    def test_parents_children_and_sources_are_read_from_the_sql(self):
        with tempfile.TemporaryDirectory() as tmp:
            dbt_dir = _fake_project(Path(tmp), {
                "stg_a": "select * from {{ source('raw', 'a') }}",
                "int_b": "select * from {{ ref('stg_a') }}",
                "fct_c": "select * from {{ ref('int_b') }}",
            })
            index = DbtIndex(dbt_dir)
            self.assertEqual(index.parents["fct_c"], ["int_b"])
            self.assertEqual(index.sources["stg_a"], ["raw.a"])
            self.assertEqual(index.children["stg_a"], ["int_b"])

    def test_blast_radius_is_the_whole_downstream_closure(self):
        with tempfile.TemporaryDirectory() as tmp:
            dbt_dir = _fake_project(Path(tmp), {
                "stg_a": "select 1",
                "int_b": "select * from {{ ref('stg_a') }}",
                "fct_c": "select * from {{ ref('int_b') }}",
                "fct_d": "select * from {{ ref('int_b') }}",
            })
            index = DbtIndex(dbt_dir)
            # Transitive, not just direct children -- that is the whole reason
            # the "delete a safety check blind" case needs this.
            self.assertEqual(index.downstream("stg_a"), ["fct_c", "fct_d", "int_b"])
            self.assertEqual(index.downstream("fct_c"), [])

    def test_an_unresolved_ref_is_reported(self):
        with tempfile.TemporaryDirectory() as tmp:
            dbt_dir = _fake_project(Path(tmp), {
                "fct_c": "select * from {{ ref('does_not_exist') }}",
            })
            self.assertEqual(
                DbtIndex(dbt_dir).unresolved_refs(),
                [{"model": "fct_c", "ref": "does_not_exist"}],
            )

    def test_a_cycle_is_reported_and_does_not_hang(self):
        with tempfile.TemporaryDirectory() as tmp:
            dbt_dir = _fake_project(Path(tmp), {
                "a": "select * from {{ ref('b') }}",
                "b": "select * from {{ ref('a') }}",
            })
            self.assertEqual(DbtIndex(dbt_dir).cycles(), ["a", "b"])


class CliTests(unittest.TestCase):
    def test_a_real_generated_project_indexes_clean(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            _build_real_dbt_project(root)
            result = build_index(root, "workspaces/demo")
            self.assertGreater(result.model_count, 0)
            self.assertEqual(result.unresolved_ref_count, 0)
            self.assertEqual(result.cycle_count, 0)
            report = json.loads((root / result.current_json_path).read_text(encoding="utf-8"))
            # A generated mart always depends on its intermediate features model.
            self.assertTrue(any(m["parents"] for m in report["models"]))

    def test_a_broken_project_raises_after_writing_the_report(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            _fake_project(root, {"fct_c": "select * from {{ ref('gone') }}"})
            with self.assertRaises(RuntimeError):
                build_index(root, "workspaces/demo")
            report = json.loads(
                (root / "workspaces" / "demo" / "interns" / "reports" / "dbt_index"
                 / "current.json").read_text(encoding="utf-8")
            )
            self.assertEqual(report["unresolved_refs"], [{"model": "fct_c", "ref": "gone"}])

    def test_missing_project_and_unknown_model_are_clear_errors(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            WorkspaceLayout(project_root=root / "workspaces" / "demo").ensure_runtime_dirs()
            with self.assertRaises(FileNotFoundError):
                build_index(root, "workspaces/demo")
            _fake_project(root, {"fct_c": "select 1"})
            with self.assertRaises(ValueError):
                build_index(root, "workspaces/demo", model="nope")


if __name__ == "__main__":
    unittest.main()
