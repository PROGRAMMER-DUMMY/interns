"""Unit tests for the engine-generation validation harness.

These tests build a tiny self-contained workspace under a tempdir (no real
datasets, no domain vocabulary) and assert the static coherence checks pass for
valid engine artifacts and fail for a known-fatal Polars pattern.
"""
from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from core.onboarding.harness.engine_generation_harness import (
    EngineGenerationHarness,
    main,
)
from core.storage.workspace_layout import WorkspaceLayout


VALID_SQL = """\
CREATE OR REPLACE VIEW kpi_001_results AS
SELECT 1 AS ready_marker;
"""

VALID_POLARS = """\
import polars as pl


def run() -> pl.DataFrame:
    return pl.DataFrame({"ready_marker": [1]})
"""

VALID_PYSPARK = """\
# Preflight FIRST: Spark 3.5 supports Java 8/11/17; set JAVA_HOME if missing.
def run():
    return None
"""

# pl.today( does not exist in the Polars API; the harness must flag it.
FATAL_POLARS = """\
import polars as pl


def run() -> pl.DataFrame:
    return pl.DataFrame({"as_of": [pl.today()]})
"""


class EngineGenerationHarnessTest(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.repo_root = Path(self._tmp.name)
        self.workspace_rel = "workspaces/sample_project"
        self.workspace = self.repo_root / self.workspace_rel
        self.layout = WorkspaceLayout(project_root=self.workspace)
        self.layout.ensure_runtime_dirs()
        self.solutions = self.layout.solutions_dir

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def _write(self, name: str, content: str) -> None:
        (self.solutions / name).write_text(content, encoding="utf-8")

    def test_valid_engine_artifacts_pass(self) -> None:
        self._write("kpi_001.sql", VALID_SQL)
        self._write("kpi_001_polars.py", VALID_POLARS)
        self._write("kpi_001_pyspark.py", VALID_PYSPARK)

        result = EngineGenerationHarness(self.repo_root, self.workspace_rel).run()

        self.assertTrue(result.ok, result.summary())
        self.assertEqual(result.kpi_count, 1)
        self.assertEqual(result.failed_count, 0)
        record = result.records[0]
        self.assertEqual(record["kpi_id"], "kpi_001")
        self.assertCountEqual(record["engines_checked"], ["sql", "polars", "pyspark"])
        # Artifacts written.
        self.assertTrue((self.repo_root / result.current_json_path).exists())
        self.assertTrue((self.repo_root / result.current_markdown_path).exists())
        self.assertTrue((self.repo_root / result.evidence_path).exists())

    def test_polars_pl_today_fails_that_kpi(self) -> None:
        self._write("kpi_001.sql", VALID_SQL)
        self._write("kpi_001_polars.py", VALID_POLARS)
        self._write("kpi_001_pyspark.py", VALID_PYSPARK)
        # A second KPI whose Polars script uses the fatal pl.today( pattern.
        self._write("kpi_002_polars.py", FATAL_POLARS)

        result = EngineGenerationHarness(self.repo_root, self.workspace_rel).run()

        self.assertFalse(result.ok)
        by_id = {record["kpi_id"]: record for record in result.records}
        self.assertEqual(by_id["kpi_001"]["status"], "passed")
        self.assertEqual(by_id["kpi_002"]["status"], "failed")
        polars_check = next(
            check for check in by_id["kpi_002"]["checks"] if check["engine"] == "polars"
        )
        self.assertEqual(polars_check["status"], "failed")
        self.assertTrue(any("pl.today(" in error for error in polars_check["errors"]))

    def test_sql_missing_result_view_fails(self) -> None:
        self._write("kpi_001.sql", "SELECT 1 AS ready_marker;\n")
        result = EngineGenerationHarness(self.repo_root, self.workspace_rel).run()
        self.assertFalse(result.ok)
        sql_check = result.records[0]["checks"][0]
        self.assertEqual(sql_check["engine"], "sql")
        self.assertEqual(sql_check["status"], "failed")

    def test_pyspark_missing_preflight_fails(self) -> None:
        self._write("kpi_001_pyspark.py", "def run():\n    return None\n")
        result = EngineGenerationHarness(self.repo_root, self.workspace_rel).run()
        self.assertFalse(result.ok)
        check = result.records[0]["checks"][0]
        self.assertEqual(check["engine"], "pyspark")
        self.assertEqual(check["status"], "failed")

    def test_report_cross_check_flags_missing_file(self) -> None:
        self._write("kpi_001.sql", VALID_SQL)
        report_dir = self.layout.reports_dir / "engine_generation"
        report_dir.mkdir(parents=True, exist_ok=True)
        (report_dir / "current.json").write_text(
            json.dumps(
                {
                    "kpis": [
                        {
                            "kpi_id": "kpi_001",
                            "engines_generated": ["sql", "polars"],
                        }
                    ]
                }
            ),
            encoding="utf-8",
        )
        result = EngineGenerationHarness(self.repo_root, self.workspace_rel).run()
        self.assertFalse(result.ok)
        record = result.records[0]
        self.assertTrue(any("polars" in error for error in record["errors"]))

    def test_main_returns_zero_on_pass(self) -> None:
        self._write("kpi_001.sql", VALID_SQL)
        code = main(["--workspace", self.workspace_rel, "--repo-root", str(self.repo_root)])
        self.assertEqual(code, 0)

    def test_main_returns_one_on_failure(self) -> None:
        self._write("kpi_002_polars.py", FATAL_POLARS)
        code = main(["--workspace", self.workspace_rel, "--repo-root", str(self.repo_root)])
        self.assertEqual(code, 1)


if __name__ == "__main__":
    unittest.main()
