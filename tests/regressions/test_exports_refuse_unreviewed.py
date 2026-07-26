"""Regression: an export must refuse a dashboard nobody has reviewed.

Origin (2026-07-26 audit): `workspace-dashboard` was invoked 6 times, never with
`--screen`, so `interns/reports/dashboard_screener/` was never created -- and a
.pptx and .pdf were produced anyway. The deck carried row-level patient
identifiers on an executive slide.

Gated on the EXPORT path, not on `minus_adapter.generate()`: the screener has to
render a dashboard in order to review it, so gating generation would make a
first-ever dashboard impossible. Rendering is safe; distributing is not.
"""
from __future__ import annotations

import json
import shutil
import unittest
from pathlib import Path

from core.paths import PROJECT_ROOT
from core.storage.workspace_layout import WorkspaceLayout
from tools.dashboard_export_common import prepare_minus_root, screener_review_state


class ScreenerReviewStateTests(unittest.TestCase):
    def setUp(self):
        self.ws = PROJECT_ROOT / "workspaces" / "_tmp_export_gate"
        (self.ws / "interns" / "reports" / "dashboard_screener").mkdir(parents=True, exist_ok=True)
        self.layout = WorkspaceLayout(project_root=self.ws)
        self.report = self.layout.reports_dir / "dashboard_screener" / "current.json"

    def tearDown(self):
        shutil.rmtree(self.ws, ignore_errors=True)

    def _write(self, payload: dict) -> None:
        self.report.write_text(json.dumps(payload), encoding="utf-8")

    def test_missing_report_is_not_satisfied(self):
        ok, detail = screener_review_state(self.layout)
        self.assertFalse(ok)
        self.assertIn("--screen", detail)

    def test_failing_screener_is_not_satisfied(self):
        self._write({"ok": False, "error_count": 3})
        ok, detail = screener_review_state(self.layout)
        self.assertFalse(ok)
        self.assertIn("3", detail)

    def test_clean_screener_without_a_review_is_not_satisfied(self):
        self._write({"ok": True, "error_count": 0})
        ok, detail = screener_review_state(self.layout)
        self.assertFalse(ok)
        self.assertIn("--record-vision-review", detail)

    def test_clean_screener_with_a_recorded_review_is_satisfied(self):
        self._write({
            "ok": True, "error_count": 0,
            "vision_review": {"recorded_at": "2026-07-26T00:00:00Z", "reviewed_by": "shubham"},
        })
        ok, detail = screener_review_state(self.layout)
        self.assertTrue(ok)
        self.assertIn("shubham", detail)

    def test_unreadable_report_is_not_satisfied(self):
        self.report.write_text("{not json", encoding="utf-8")
        ok, _ = screener_review_state(self.layout)
        self.assertFalse(ok)


class ExportRefusalTests(unittest.TestCase):
    def setUp(self):
        self.ws = PROJECT_ROOT / "workspaces" / "_tmp_export_refuse"
        self.ws.mkdir(parents=True, exist_ok=True)

    def tearDown(self):
        shutil.rmtree(self.ws, ignore_errors=True)

    def test_export_refuses_when_unreviewed(self):
        with self.assertRaises(RuntimeError) as ctx:
            prepare_minus_root(PROJECT_ROOT, f"workspaces/{self.ws.name}")
        self.assertIn("refusing to export an unreviewed dashboard", str(ctx.exception))

    def test_the_override_is_explicit(self):
        # With the override the screener check is skipped; it then fails later for
        # an unrelated reason (no dashboard data), which proves the gate was passed.
        with self.assertRaises(Exception) as ctx:
            prepare_minus_root(PROJECT_ROOT, f"workspaces/{self.ws.name}",
                               allow_unscreened=True)
        self.assertNotIn("refusing to export an unreviewed dashboard", str(ctx.exception))


if __name__ == "__main__":
    unittest.main()
