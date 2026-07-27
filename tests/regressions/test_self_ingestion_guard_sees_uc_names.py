"""Regression: the self-ingestion guard must see fully-qualified UC names.

Origin (2026-07-27 verification of workspaces/rcm_dashboard): the profile index
listed `healthcare_rcm.bronze.kpi_002_results` (4,297 rows) and
`kpi_003_results` as SOURCE tables, and the blocker panel then offered
`kpi_002_results.gender` as the RECOMMENDED source of truth for kpi_002's own
`Gender` cut -- a KPI defining its input from its own output, at the exploded
one-row-per-patient grain the KPI-002 defect produced.

`_is_platform_written_relation` was correct on a bare name and blind to the
fully-qualified, backtick-quoted form a databricks_source workspace's profile
index actually stores. `.strip("`")` leaves catalog and schema attached, so
every prefix/suffix rule missed and the guard did nothing at all for exactly
the workspaces it exists to protect. Same UC-identifier-collapse class as the
four call sites fixed in Phase 0 -- this was a fifth.
"""
from __future__ import annotations

import unittest

from core.onboarding.workspace.onboarding import _is_platform_written_relation


class PlatformWrittenRelationTests(unittest.TestCase):
    def test_a_uc_qualified_result_view_is_excluded(self):
        # The exact string observed in the real profile index.
        self.assertTrue(
            _is_platform_written_relation("`healthcare_rcm`.`bronze`.`kpi_002_results`")
        )

    def test_uc_qualified_platform_prefixes_are_excluded(self):
        for name in (
            "`c`.`s`.`stg_transactions`",
            "`c`.`s`.`int_kpi_001_features`",
            "`c`.`s`.`fct_kpi_001`",
        ):
            with self.subTest(name=name):
                self.assertTrue(_is_platform_written_relation(name))

    def test_an_unquoted_qualified_name_is_still_excluded(self):
        # Not the profiler's own output format, but a plausible hand-written
        # config value. Failing open here means a KPI ingests its own output.
        self.assertTrue(
            _is_platform_written_relation("healthcare_rcm.bronze.kpi_003_results")
        )

    def test_a_bare_name_still_works(self):
        self.assertTrue(_is_platform_written_relation("kpi_002_results"))
        self.assertTrue(_is_platform_written_relation("stg_patients"))

    def test_genuine_uc_sources_are_not_excluded(self):
        for name in (
            "`healthcare_rcm`.`bronze`.`patients`",
            "`healthcare_rcm`.`bronze`.`transactions`",
            "`healthcare_rcm`.`bronze`.`departments`",
        ):
            with self.subTest(name=name):
                self.assertFalse(_is_platform_written_relation(name))

    def test_a_source_table_merely_ending_in_results_is_kept(self):
        # The suffix rule is scoped to our own `kpi_*` output for this reason:
        # a clinical `lab_results` table is a real source, qualified or not.
        self.assertFalse(_is_platform_written_relation("lab_results"))
        self.assertFalse(_is_platform_written_relation("`c`.`s`.`lab_results`"))

    def test_local_file_paths_are_unaffected(self):
        self.assertFalse(_is_platform_written_relation("workspaces/x/datasets/patients.csv"))
        self.assertTrue(_is_platform_written_relation("workspaces/x/kpi_001_results.parquet"))

    def test_empty_and_junk_are_not_excluded(self):
        for name in ("", "   ", "`"):
            with self.subTest(name=name):
                self.assertFalse(_is_platform_written_relation(name))


if __name__ == "__main__":
    unittest.main()
