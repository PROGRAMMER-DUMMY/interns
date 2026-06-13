from __future__ import annotations

import unittest

from tools.dashboard_verify import _contrast_ratio, _delta_e, _parse_rgb


class ColorParseTests(unittest.TestCase):
    def test_parse_rgb_and_hex(self):
        self.assertEqual(_parse_rgb("rgb(180, 68, 28)"), (180.0, 68.0, 28.0))
        self.assertEqual(_parse_rgb("#b4441c"), (180.0, 68.0, 28.0))
        self.assertEqual(_parse_rgb("#fff"), (255.0, 255.0, 255.0))
        self.assertIsNone(_parse_rgb("none"))
        self.assertIsNone(_parse_rgb(""))


class DeltaETests(unittest.TestCase):
    def test_distinct_colors_are_far_apart(self):
        # sienna vs slate (the editorial trend pair) must be clearly distinguishable
        de = _delta_e("rgb(180,68,28)", "rgb(47,68,82)")
        self.assertIsNotNone(de)
        self.assertGreater(de, 16.0)

    def test_near_identical_colors_flag_as_clash(self):
        # two near-identical siennas read as the same series -> below JND band
        de = _delta_e("rgb(180,68,28)", "rgb(186,74,34)")
        self.assertIsNotNone(de)
        self.assertLess(de, 16.0)

    def test_identical_is_zero(self):
        self.assertEqual(_delta_e("rgb(10,20,30)", "rgb(10,20,30)"), 0.0)

    def test_unparseable_returns_none(self):
        self.assertIsNone(_delta_e("none", "rgb(1,2,3)"))


class ContrastTests(unittest.TestCase):
    def test_low_contrast_pale_on_white_flags(self):
        cr = _contrast_ratio("rgb(230,225,215)", "rgb(255,255,255)")
        self.assertIsNotNone(cr)
        self.assertLess(cr, 1.6)

    def test_dark_on_white_is_high_contrast(self):
        cr = _contrast_ratio("rgb(47,68,82)", "rgb(255,255,255)")
        self.assertIsNotNone(cr)
        self.assertGreater(cr, 1.6)

    def test_symmetric(self):
        a = _contrast_ratio("rgb(0,0,0)", "rgb(255,255,255)")
        b = _contrast_ratio("rgb(255,255,255)", "rgb(0,0,0)")
        self.assertAlmostEqual(a, b, places=6)


if __name__ == "__main__":
    unittest.main()
