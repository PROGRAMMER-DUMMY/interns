"""Generic dashboard layout planner: tile any number of widgets into a clean
12-column grid that never leaves ragged gaps.

The dashboard canvas is a 12-column CSS grid; each widget declares a column span
(1-12). The old rule ``span = max(3, 12 // n)`` only tiled cleanly when N divided
the grid (3, 4, 6, 12) -- for 5/7/8/9/10/11 widgets it left empty cells (the
ragged trailing row). This planner instead JUSTIFIES every row: items fill all 12
columns, and a partial last row stretches its items to fill the width. Result:
no gaps for ANY workspace's KPI count.

Pure functions, no rendering deps -- unit-testable and reused by the screener's
layout-balance check, so the CLI both DECIDES and VERIFIES the layout.
"""
from __future__ import annotations

GRID = 12
# Clean column spans (divisors of 12) and the per-row counts they yield.
_CLEAN_SPANS = (12, 6, 4, 3, 2, 1)            # span -> 1,2,3,4,6,12 per row
# Readable items-per-row for cards. 4 and 3 are the legible densities; 2 is too
# sparse and 6+ too dense for a KPI card. Last-row justification fills any
# remainder, so density (not gap) is what the selector optimizes.
_CARD_PER_ROW_CANDIDATES = (4, 3)


def _rows_for(count: int, per_row: int) -> list[int]:
    """Split ``count`` items into rows of at most ``per_row`` (last row holds the
    remainder)."""
    rows: list[int] = []
    remaining = count
    while remaining > 0:
        rows.append(min(per_row, remaining))
        remaining -= per_row
    return rows


def _justify(row_count: int) -> list[int]:
    """Column spans for ONE row of ``row_count`` items that sum to exactly 12,
    distributing any remainder so the row is full (no gap). e.g. 2 items -> [6,6];
    5 items -> [3,3,2,2,2] (sums to 12); 1 item -> [12]."""
    if row_count <= 0:
        return []
    base = GRID // row_count
    extra = GRID - base * row_count            # columns to spread over the first items
    return [base + (1 if i < extra else 0) for i in range(row_count)]


def _best_per_row(count: int) -> int:
    """Readable cards-per-row for ``count`` items. Prefer a UNIFORM tiling when
    the count divides a legible density (4, then 3 -> all tiles equal width);
    otherwise justify with 4/row, unless that strands a lonely single tile in the
    last row, in which case 3/row gives a fuller, more balanced last row."""
    for per_row in _CARD_PER_ROW_CANDIDATES:          # 4, then 3
        if count % per_row == 0:
            return per_row                             # uniform, gap-free
    if _rows_for(count, 4)[-1] == 1:                   # 4/row would strand 1 tile
        return 3
    return 4


def plan_widths(count: int, *, per_row: int | None = None) -> list[int]:
    """Column span (1-12) for each of ``count`` widgets so every row fills the
    grid with no gaps. ``per_row`` forces a density; otherwise it is chosen to
    balance the rows. Returns one width per widget, in order."""
    if count <= 0:
        return []
    if count == 1:
        return [GRID]
    chosen = per_row or _best_per_row(count)
    rows = _rows_for(count, chosen)
    # Never strand a lonely single tile in the last row (it stretches to a full-
    # width 12 and looks broken). Pull one item from the previous row so the last
    # row has two; both rows still justify to fill the grid.
    if len(rows) > 1 and rows[-1] == 1:
        rows[-2] -= 1
        rows[-1] = 2
    widths: list[int] = []
    for row_count in rows:
        widths.extend(_justify(row_count))
    return widths


def row_partition(widths: list[int]) -> list[list[int]]:
    """Group a flat width list back into rows (each summing to <= 12). Used by the
    layout-balance check to verify tiling."""
    rows: list[list[int]] = []
    cur: list[int] = []
    total = 0
    for w in widths:
        if total + w > GRID:
            rows.append(cur)
            cur, total = [], 0
        cur.append(w)
        total += w
    if cur:
        rows.append(cur)
    return rows


def layout_findings(widths: list[int]) -> list[str]:
    """Deterministic layout-balance check: every row must fill the grid (no gap)
    and no widget may overflow it. Returns human-readable findings (empty = OK).
    The screener runs this so a ragged layout fails the gate, not the eyeball."""
    findings: list[str] = []
    for w in widths:
        if w < 1 or w > GRID:
            findings.append(f"widget span {w} is outside 1..{GRID}")
    for i, row in enumerate(row_partition(widths), start=1):
        total = sum(row)
        if total < GRID:
            findings.append(
                f"row {i} fills {total}/{GRID} columns -- {GRID - total} empty "
                "(ragged gap); justify the row to fill the grid")
        if total > GRID:
            findings.append(f"row {i} overflows the grid ({total}/{GRID})")
    return findings


__all__ = ["plan_widths", "row_partition", "layout_findings", "GRID"]
