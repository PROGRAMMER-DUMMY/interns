"""Regression: the pipeline must not re-ingest its own output, and must not
declare a non-idempotent load strategy.

Origin (2026-07-26 agy-harness audit). Two findings from one run:

1. A KPI result view emitted into the SOURCE schema (`bronze.kpi_002_results`) was
   re-discovered by `SHOW TABLES` as a bronze SOURCE, landing in `manifest.yaml`
   with `percentage_share` -- a computed measure -- inside its natural key. The
   pipeline's output became its own input. The file-domain twin of this guard,
   `_is_platform_governance_note`, already existed for the `wiki/` tree.

2. Bronze tables were emitted with `load_strategy: append_watermarked` and
   `watermark_column: null` -- append-with-no-watermark, i.e. a full append on
   every run. Per dbt's own incremental taxonomy that is the single
   non-idempotent strategy.
"""
from __future__ import annotations

import unittest

from core.onboarding.workspace.onboarding import _is_platform_written_relation


class PlatformWrittenRelationTests(unittest.TestCase):
    def test_our_own_outputs_are_excluded(self):
        for name in (
            "kpi_001_results", "kpi_002_results",   # result_view_builder
            "stg_patients", "int_kpi_002_features", "fct_kpi_002",  # dbt generator
        ):
            with self.subTest(name=name):
                self.assertTrue(_is_platform_written_relation(name))

    def test_genuine_source_tables_are_kept(self):
        for name in ("patients", "transactions", "departments", "encounters", "claims"):
            with self.subTest(name=name):
                self.assertFalse(_is_platform_written_relation(name))

    def test_a_source_table_that_merely_ends_in_results_is_kept(self):
        # The suffix rule must be scoped to our `kpi_*` outputs; a real clinical
        # source table called `lab_results` is NOT platform-written.
        self.assertFalse(_is_platform_written_relation("lab_results"))
        self.assertFalse(_is_platform_written_relation("test_results"))
        self.assertFalse(_is_platform_written_relation("patient_features"))

    def test_backticks_and_case_do_not_defeat_the_check(self):
        self.assertTrue(_is_platform_written_relation("`kpi_002_results`"))
        self.assertTrue(_is_platform_written_relation("KPI_002_RESULTS"))
        self.assertTrue(_is_platform_written_relation("  stg_patients  "))

    def test_empty_is_not_a_platform_relation(self):
        self.assertFalse(_is_platform_written_relation(""))
        self.assertFalse(_is_platform_written_relation("   "))


class BronzeLoadStrategyTests(unittest.TestCase):
    def test_append_watermarked_is_never_declared_without_a_watermark(self):
        # `append_watermarked` + watermark_column=None is a full append per run.
        # design.py must degrade to an idempotent strategy instead.
        import inspect

        from core.medallion import design

        src = inspect.getsource(design)
        self.assertIn(
            'load_strategy = "append_watermarked" if watermark else "full_refresh"', src,
            "bronze load_strategy must be derived from watermark presence, not left "
            "at the append_watermarked dataclass default",
        )


if __name__ == "__main__":
    unittest.main()
