from __future__ import annotations

import unittest

from tools.profiler_utils import (
    clamp_score,
    human_size,
    mean_safe,
    parse_column_list,
    pct_label,
    safe_filename_part,
    unique_preserve_order,
)


class ProfilerUtilsTests(unittest.TestCase):
    def test_formatting_and_parse_helpers(self):
        self.assertEqual(human_size(1024), "1.00 KB")
        self.assertEqual(pct_label(12.5), "12p5")
        self.assertEqual(safe_filename_part("A/B C"), "A_B_C")
        self.assertEqual(parse_column_list("a, b,, c"), ["a", "b", "c"])

    def test_numeric_helpers(self):
        self.assertEqual(clamp_score(120), 100.0)
        self.assertEqual(mean_safe([1, None, 3]), 2)
        self.assertEqual(unique_preserve_order(["a", "b", "a", ""]), ["a", "b"])


if __name__ == "__main__":
    unittest.main()
