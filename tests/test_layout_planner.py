"""The generic layout planner tiles ANY number of widgets into a clean 12-column
grid with no ragged gaps -- so a differently-shaped workspace (3, 5, 7, 10... KPIs)
never ships a broken/uneven layout. The CLI both DECIDES (plan_widths) and
VERIFIES (layout_findings) the grid."""
from __future__ import annotations

import unittest

from core.dashboard.layout_planner import (
    GRID,
    layout_findings,
    plan_widths,
    row_partition,
)


class LayoutPlannerTests(unittest.TestCase):
    def test_every_count_fills_the_grid_with_no_gaps(self):
        for n in range(1, 25):
            widths = plan_widths(n)
            self.assertEqual(len(widths), n)
            self.assertEqual(layout_findings(widths), [],
                             f"N={n} produced a ragged layout: {widths}")
            for row in row_partition(widths):
                self.assertEqual(sum(row), GRID, f"N={n} row {row} != {GRID}")

    def test_rcm_three_kpis_unchanged(self):
        # RCM's 3 KPIs must stay 3-across full-width (it already looked clean).
        self.assertEqual(plan_widths(3), [4, 4, 4])

    def test_readable_density_not_too_sparse(self):
        # 10 KPIs must NOT collapse to 2-per-row (too sparse); 3-4 per row.
        rows = row_partition(plan_widths(10))
        self.assertTrue(all(2 <= len(r) <= 4 for r in rows),
                        f"unreadable density: {[len(r) for r in rows]}")

    def test_no_lonely_single_tile_last_row(self):
        # A last row with a single stretched-to-12 tile looks broken; avoid it.
        for n in (5, 7, 10, 13):
            rows = row_partition(plan_widths(n))
            self.assertNotEqual(len(rows[-1]), 1, f"N={n} stranded a lonely tile")

    def test_layout_findings_catches_a_ragged_grid(self):
        # A hand-built ragged layout (row sums to 9, not 12) must be flagged.
        self.assertTrue(layout_findings([3, 3, 3]))     # one row, 9/12 -> gap
        self.assertEqual(layout_findings([4, 4, 4]), [])  # full row -> clean

    def test_overflow_is_flagged(self):
        self.assertTrue(layout_findings([8, 8]))        # 16 > 12 on one row


if __name__ == "__main__":
    unittest.main()
