"""Phase 4 tests (ship gate): redaction parity + loopback-token auth."""
from __future__ import annotations

import unittest
from pathlib import Path

import polars as pl

from core.dashboard.model.crossfilter import CanvasModel
from core.dashboard.model.cuts import KpiModel
from core.dashboard.ui.governance import (
    WorkspaceRedaction,
    is_loopback_host,
)
from core.dashboard.ui.layout import kpi_page, slicer_bar
from core.storage.workspace_layout import WorkspaceLayout

_WS = Path("workspaces/Healthcare-RCM-Data-Platform")


class TestRedactionPolicy(unittest.TestCase):
    def setUp(self):
        # None workspace -> default PII patterns only (no policy file needed)
        self.r = WorkspaceRedaction(None)

    def test_pii_columns_flagged(self):
        # exact-anchored contract: 'Name'/'ssn' match; 'Department' does not.
        self.assertTrue(self.r.is_redacted("Name"))
        self.assertTrue(self.r.is_redacted("ssn"))
        self.assertTrue(self.r.is_redacted("DOB"))
        self.assertFalse(self.r.is_redacted("Department"))
        self.assertFalse(self.r.is_redacted("sum_paidamount"))

    def test_visible_columns_drops_pii(self):
        cols = ["Department", "Name", "sum_paid", "ssn"]
        self.assertEqual(self.r.visible_columns(cols), ["Department", "sum_paid"])

    def test_panel_safe_only_when_no_redacted_axis(self):
        self.assertTrue(self.r.panel_is_safe({"x": "Department", "y": "sum_paid"}))
        self.assertFalse(self.r.panel_is_safe({"x": "Name", "y": "sum_paid"}))
        self.assertFalse(self.r.panel_is_safe({"x": "Dept", "color": "ssn", "y": "sum_paid"}))


class TestLayoutHonorsRedaction(unittest.TestCase):
    def _model_with_pii_panel(self):
        return KpiModel(
            kpi_id="k", title="K", gold_columns=["Department", "Name", "sum_paid"],
            measure="sum_paid", cuts=["Department", "Name"], kind="sum",
            additive=True,
            panels=[
                {"chart_type": "bar", "x": "Department", "y": "sum_paid", "title": "ok"},
                {"chart_type": "bar", "x": "Name", "y": "sum_paid", "title": "leak"},
            ],
        )

    def test_pii_panel_dropped_from_page(self):
        model = self._model_with_pii_panel()
        gold = pl.DataFrame({"Department": ["A", "B"], "Name": ["x", "y"],
                             "sum_paid": [10.0, 20.0]})
        r = WorkspaceRedaction(None)
        tiles = kpi_page(model, gold, "claude", r)
        # headline card + 1 safe panel (the PII 'Name' panel is dropped) = 2
        self.assertEqual(len(tiles), 2)


class TestClickCrossFilter(unittest.TestCase):
    def test_chips_built_from_click_filters(self):
        from core.dashboard.ui.layout import crossfilter_chips
        chips = crossfilter_chips({"Gender": "F", "PayorID": "P1"})
        self.assertEqual(len(chips), 2)

    def test_headline_reflects_click_filter(self):
        from core.dashboard.ui.layout import kpi_page
        model = KpiModel(kpi_id="k", title="K", gold_columns=["Gender", "sum_paid"],
                         measure="sum_paid", cuts=["Gender"], kind="sum", additive=True,
                         panels=[{"chart_type": "bar", "x": "Gender", "y": "sum_paid",
                                  "title": "by gender"}])
        gold = pl.DataFrame({"Gender": ["M", "F", "M"], "sum_paid": [10.0, 5.0, 20.0]})
        tiles = kpi_page(model, gold, "claude", None, None, {"Gender": "F"})
        # headline card is tiles[0]; its value should be the F subset (5.0)
        card = tiles[0].children  # html.Div wrapper -> kpi_card
        # kpi value text lives in the card; assert the filtered total is used
        from core.dashboard.ui.format import format_value
        self.assertIn(format_value(5.0, None), str(card))

    def test_pii_dimension_not_offered_as_slicer(self):
        a = KpiModel(kpi_id="a", title="a", gold_columns=["Department", "Name", "m"],
                     measure="m", cuts=["Department", "Name"], kind="sum", additive=True)
        b = KpiModel(kpi_id="b", title="b", gold_columns=["Department", "Name", "m"],
                     measure="m", cuts=["Department", "Name"], kind="sum", additive=True)
        gold = pl.DataFrame({"Department": ["A"], "Name": ["z"], "m": [1.0]})
        canvas = CanvasModel(layout=WorkspaceLayout(project_root=_WS.resolve()),
                             kpis={"a": a, "b": b}, gold={"a": gold, "b": gold})
        r = WorkspaceRedaction(None)
        slicers = slicer_bar(canvas, r)  # now a list of slicer divs
        # Department is shared+safe -> a slicer; Name is shared but redacted -> none
        labels = _slicer_labels(slicers)
        self.assertIn("Department", labels)
        self.assertNotIn("Name", labels)


def _slicer_labels(slicers) -> list[str]:
    out = []
    for slicer in slicers:
        # slicer.children[0] is the html.Label
        try:
            out.append(slicer.children[0].children)
        except Exception:
            pass
    return out


class TestAuthGuard(unittest.TestCase):
    def test_loopback_detection(self):
        self.assertTrue(is_loopback_host("127.0.0.1"))
        self.assertTrue(is_loopback_host("localhost"))
        self.assertTrue(is_loopback_host("::1"))
        self.assertFalse(is_loopback_host("0.0.0.0"))
        self.assertFalse(is_loopback_host("10.0.0.5"))

    def test_token_required_on_nonloopback(self):
        from dash import Dash

        from core.dashboard.ui.governance import attach_token_auth
        app = Dash(__name__)
        app.layout = __import__("dash").html.Div("x")
        attach_token_auth(app, "s3cret")
        client = app.server.test_client()
        self.assertEqual(client.get("/").status_code, 401)
        self.assertEqual(client.get("/?token=wrong").status_code, 401)
        ok = client.get("/?token=s3cret")
        self.assertNotEqual(ok.status_code, 401)


if __name__ == "__main__":
    unittest.main()
