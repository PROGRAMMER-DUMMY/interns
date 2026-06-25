"""Export smoke test for the edge-case data types that broke PDF/PPTX export
this session: DuckDB DECIMAL values are not JSON-serializable by kaleido, which
silently failed charts and (via a dangling Contents link) aborted the whole PDF.
`_json_safe` must coerce them so any figure rasterizes."""
from __future__ import annotations

import decimal
import unittest
from pathlib import Path

from tools.dashboard_export_common import add_vendor_to_path

add_vendor_to_path(Path(".").resolve())


class JsonSafeDecimalTests(unittest.TestCase):
    def setUp(self):
        try:
            from minus.export.model import _json_safe
        except Exception as exc:  # pragma: no cover - export stack optional
            self.skipTest(f"export stack unavailable: {exc}")
        self._json_safe = _json_safe

    def test_decimal_scalar_becomes_float(self):
        out = self._json_safe(decimal.Decimal("85.55"))
        self.assertIsInstance(out, float)
        self.assertEqual(out, 85.55)

    def test_decimals_nested_in_figure_dict(self):
        # plotly figure-shaped: traces with Decimal x/y (what DuckDB returns).
        fig = {"data": [{"x": [decimal.Decimal("1"), decimal.Decimal("2")],
                         "y": [decimal.Decimal("85.55"), 90.0]}],
               "layout": {"title": {"text": "t"}}}
        out = self._json_safe(fig)
        self.assertEqual(out["data"][0]["y"][0], 85.55)
        for v in out["data"][0]["x"] + out["data"][0]["y"]:
            self.assertNotIsInstance(v, decimal.Decimal)

    def test_is_json_serializable_after_coercion(self):
        import json
        fig = {"v": [decimal.Decimal("3.14"), {"n": decimal.Decimal("2")}]}
        json.dumps(self._json_safe(fig))  # must not raise

    def test_non_decimal_values_pass_through(self):
        payload = {"a": 1, "b": "x", "c": [1.5, None, True]}
        self.assertEqual(self._json_safe(payload), payload)


if __name__ == "__main__":
    unittest.main()
