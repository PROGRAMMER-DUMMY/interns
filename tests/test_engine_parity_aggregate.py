"""Unit tests for aggregate parity mode (core.onboarding.kpi.engine_parity).

Above the parity row cap, cross-engine parity no longer skips: it compares
per-engine aggregate signatures (row count, per-column null counts,
tolerance-aware numeric sum/min/max, distinct counts of grouping columns, and
an order-insensitive content checksum over canonicalized rows). These tests
force aggregate mode on small fixtures via the explicit ``row_cap`` argument
and the ``KPI_PARITY_ROW_CAP`` env override.
"""
from __future__ import annotations

import os
import unittest

from core.onboarding.kpi.engine_parity import (
    AGGREGATE_SIGNATURE_NAMES,
    MAX_PARITY_ROWS,
    PARITY_MODE_AGGREGATE,
    PARITY_MODE_ROW,
    evaluate_parity,
    parity_row_cap,
    run_polars_parity,
)

COLUMNS = ["payor", "claim_count", "paid_amount"]
ROWS = [
    ["Medicare", 10, 1234.56],
    ["Medicaid", 7, 789.01],
    ["Commercial", 3, 456.78],
    [None, 1, None],
]
# Forces aggregate mode for any fixture larger than two rows.
SMALL_CAP = 2


class TestAggregateModeVerdicts(unittest.TestCase):
    def test_identical_results_match(self):
        verdict = evaluate_parity(COLUMNS, ROWS, COLUMNS, ROWS, row_cap=SMALL_CAP)
        self.assertEqual(verdict["mode"], PARITY_MODE_AGGREGATE)
        self.assertEqual(verdict["status"], "match")
        for name in AGGREGATE_SIGNATURE_NAMES:
            self.assertTrue(verdict["signatures"][name]["match"], name)

    def test_single_perturbed_value_fails(self):
        perturbed = [list(row) for row in ROWS]
        perturbed[1][2] = 790.01  # one numeric cell drifts
        verdict = evaluate_parity(COLUMNS, ROWS, COLUMNS, perturbed, row_cap=SMALL_CAP)
        self.assertEqual(verdict["mode"], PARITY_MODE_AGGREGATE)
        self.assertEqual(verdict["status"], "mismatch")
        self.assertFalse(verdict["signatures"]["content_checksum"]["match"])
        self.assertIn("content_checksum", verdict["reason"])

    def test_sub_tolerance_aggregate_drift_still_caught_by_checksum(self):
        # 789.01 -> 789.02 shifts the column sum by exactly the float
        # tolerance, but the per-row checksum (over rounded cells) must fail.
        perturbed = [list(row) for row in ROWS]
        perturbed[1][2] = 789.02
        verdict = evaluate_parity(COLUMNS, ROWS, COLUMNS, perturbed, row_cap=SMALL_CAP)
        self.assertEqual(verdict["status"], "mismatch")
        self.assertFalse(verdict["signatures"]["content_checksum"]["match"])

    def test_perturbed_grouping_value_fails_distinct_count(self):
        perturbed = [list(row) for row in ROWS]
        perturbed[1][0] = "Medicare"  # collapses two distinct payors into one
        verdict = evaluate_parity(COLUMNS, ROWS, COLUMNS, perturbed, row_cap=SMALL_CAP)
        self.assertEqual(verdict["status"], "mismatch")
        self.assertIn(
            "payor", verdict["signatures"]["distinct_counts"]["mismatched_columns"]
        )

    def test_reordered_identical_result_matches(self):
        # Engine emits the same content with rows reversed AND columns in a
        # different order; aggregate signatures are order-insensitive.
        engine_columns = ["claim_count", "paid_amount", "payor"]
        engine_rows = [[row[1], row[2], row[0]] for row in reversed(ROWS)]
        verdict = evaluate_parity(
            COLUMNS, ROWS, engine_columns, engine_rows, row_cap=SMALL_CAP
        )
        self.assertEqual(verdict["mode"], PARITY_MODE_AGGREGATE)
        self.assertEqual(verdict["status"], "match")

    def test_int_vs_float_normalization_is_shared_with_row_mode(self):
        # The canonicalization rules are the row-level ones (ints == equal
        # floats), not a second normalization path.
        engine_rows = [list(row) for row in ROWS]
        engine_rows[0][1] = 10.0  # canonical has int 10
        verdict = evaluate_parity(COLUMNS, ROWS, COLUMNS, engine_rows, row_cap=SMALL_CAP)
        self.assertEqual(verdict["status"], "match")

    def test_missing_row_fails_row_count(self):
        verdict = evaluate_parity(COLUMNS, ROWS, COLUMNS, ROWS[:-1], row_cap=SMALL_CAP)
        self.assertEqual(verdict["status"], "mismatch")
        self.assertFalse(verdict["signatures"]["row_count"]["match"])

    def test_null_drift_fails_null_counts(self):
        engine_rows = [list(row) for row in ROWS]
        engine_rows[3][2] = 0  # engine turned a NULL into a zero
        verdict = evaluate_parity(COLUMNS, ROWS, COLUMNS, engine_rows, row_cap=SMALL_CAP)
        self.assertEqual(verdict["status"], "mismatch")
        self.assertIn(
            "paid_amount", verdict["signatures"]["null_counts"]["mismatched_columns"]
        )

    def test_column_set_difference_is_a_mismatch(self):
        engine_columns = ["payor", "claim_count", "billed_amount"]
        verdict = evaluate_parity(COLUMNS, ROWS, engine_columns, ROWS, row_cap=SMALL_CAP)
        self.assertEqual(verdict["status"], "mismatch")
        self.assertIn("column sets differ", verdict["reason"])


class TestModeSelectionAndReporting(unittest.TestCase):
    def tearDown(self):
        os.environ.pop("KPI_PARITY_ROW_CAP", None)

    def test_under_cap_reports_row_parity_mode(self):
        verdict = evaluate_parity(COLUMNS, ROWS, COLUMNS, ROWS)
        self.assertEqual(verdict["mode"], PARITY_MODE_ROW)
        self.assertEqual(verdict["status"], "match")
        self.assertNotIn("signatures", verdict)

    def test_env_override_forces_aggregate_mode(self):
        os.environ["KPI_PARITY_ROW_CAP"] = str(SMALL_CAP)
        self.assertEqual(parity_row_cap(), SMALL_CAP)
        verdict = evaluate_parity(COLUMNS, ROWS, COLUMNS, ROWS)
        self.assertEqual(verdict["mode"], PARITY_MODE_AGGREGATE)
        self.assertEqual(verdict["status"], "match")

    def test_invalid_env_override_falls_back_to_default(self):
        os.environ["KPI_PARITY_ROW_CAP"] = "not-a-number"
        self.assertEqual(parity_row_cap(), MAX_PARITY_ROWS)
        os.environ["KPI_PARITY_ROW_CAP"] = "-5"
        self.assertEqual(parity_row_cap(), MAX_PARITY_ROWS)

    def test_run_polars_parity_reports_mode_even_when_degraded(self):
        # Generation against a nonexistent workspace degrades to
        # skipped/error, but the verdict still says which mode applied.
        os.environ["KPI_PARITY_ROW_CAP"] = str(SMALL_CAP)
        result = run_polars_parity(
            ".", "workspaces/nonexistent", "kpi_001", COLUMNS, ROWS
        )
        self.assertEqual(result["mode"], PARITY_MODE_AGGREGATE)
        self.assertIn(result["status"], {"skipped", "error"})


if __name__ == "__main__":
    unittest.main()
