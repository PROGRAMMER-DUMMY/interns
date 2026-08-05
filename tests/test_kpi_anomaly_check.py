"""core.observability.kpi_anomaly_check: MAD-based KPI headline alerting
(generic pipeline alignment plan, S5.3)."""
from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from core.observability.kpi_anomaly_check import (
    DEFAULT_MAD_THRESHOLD,
    check_kpi_anomalies,
    check_kpi_anomalies_main,
    write_kpi_alerts_report,
)
from core.storage.workspace_layout import WorkspaceLayout


def _history(values: list) -> list:
    return [{"kpi_001": v} for v in values]


class CheckKpiAnomaliesTests(unittest.TestCase):
    def test_fewer_than_four_history_points_never_alarms(self):
        for n in (0, 1, 2, 3):
            findings = check_kpi_anomalies(_history([100] * n), {"kpi_001": 999})
            self.assertEqual(findings, [], f"n={n} history points must never alarm")

    def test_stable_history_flags_a_real_jump(self):
        # median 100, MAD 0 -> any deviation should be an anomaly.
        findings = check_kpi_anomalies(_history([100, 100, 100, 100]), {"kpi_001": 150})
        self.assertEqual(len(findings), 1)
        f = findings[0]
        self.assertEqual(f["kpi_id"], "kpi_001")
        self.assertEqual(f["current"], 150)
        self.assertEqual(f["median"], 100)
        self.assertEqual(f["distance"], float("inf"))

    def test_matching_stable_history_is_not_flagged(self):
        findings = check_kpi_anomalies(_history([100, 100, 100, 100]), {"kpi_001": 100})
        self.assertEqual(findings, [])

    def test_mad_math_on_a_known_fixture(self):
        # values: 10, 12, 11, 13 -> median 11.5, abs devs: 1.5, 0.5, 0.5, 1.5
        # -> MAD = 1.0. current=20 -> distance = |20-11.5|/1.0 = 8.5 > 3.0.
        findings = check_kpi_anomalies(_history([10, 12, 11, 13]), {"kpi_001": 20})
        self.assertEqual(len(findings), 1)
        f = findings[0]
        self.assertAlmostEqual(f["median"], 11.5)
        self.assertAlmostEqual(f["mad"], 1.0)
        self.assertAlmostEqual(f["distance"], 8.5)

    def test_within_threshold_is_not_flagged(self):
        # Same fixture, current close to median -> distance well under 3.0.
        findings = check_kpi_anomalies(_history([10, 12, 11, 13]), {"kpi_001": 12.5})
        self.assertEqual(findings, [])

    def test_custom_mad_threshold_changes_the_outcome(self):
        history = _history([10, 12, 11, 13])
        current = {"kpi_001": 14}  # distance = |14-11.5|/1.0 = 2.5
        self.assertEqual(check_kpi_anomalies(history, current, mad_threshold=3.0), [])
        flagged = check_kpi_anomalies(history, current, mad_threshold=2.0)
        self.assertEqual(len(flagged), 1)

    def test_default_threshold_is_the_documented_three(self):
        self.assertEqual(DEFAULT_MAD_THRESHOLD, 3.0)

    def test_multiple_kpis_independent_history(self):
        history = [
            {"kpi_001": 100, "kpi_002": 5},
            {"kpi_001": 100, "kpi_002": 5},
            {"kpi_001": 100, "kpi_002": 5},
            {"kpi_001": 100, "kpi_002": 5},
        ]
        current = {"kpi_001": 100, "kpi_002": 50}
        findings = check_kpi_anomalies(history, current)
        self.assertEqual([f["kpi_id"] for f in findings], ["kpi_002"])

    def test_non_numeric_current_value_is_skipped_not_crashed(self):
        findings = check_kpi_anomalies(_history([1, 2, 3, 4]), {"kpi_001": "n/a"})
        self.assertEqual(findings, [])


class WriteKpiAlertsReportTests(unittest.TestCase):
    def test_no_findings_writes_ok_report(self):
        with tempfile.TemporaryDirectory() as tmp:
            layout = WorkspaceLayout(project_root=Path(tmp) / "workspaces" / "demo")
            path = write_kpi_alerts_report([], layout=layout)
            self.assertTrue(path.endswith("kpi_alerts/current.md"))
            text = Path(path).read_text(encoding="utf-8")
            self.assertIn("[ok]", text)
            self.assertNotIn("[x]", text)

    def test_findings_write_details_and_never_call_webhook_without_url(self):
        with tempfile.TemporaryDirectory() as tmp:
            layout = WorkspaceLayout(project_root=Path(tmp) / "workspaces" / "demo")
            findings = [{
                "kpi_id": "kpi_001", "current": 150.0, "median": 100.0,
                "mad": 0.0, "distance": float("inf"), "mad_threshold": 3.0,
            }]

            class _BoomHttp:
                def Request(self, *a, **k):
                    raise AssertionError("must not build a request with no webhook_url")

            path = write_kpi_alerts_report(findings, layout=layout, http=_BoomHttp())
            text = Path(path).read_text(encoding="utf-8")
            self.assertIn("[x]", text)
            self.assertIn("kpi_001", text)
            self.assertIn("150.00", text)
            self.assertIn("100.00", text)

    def test_findings_with_webhook_post_it(self):
        with tempfile.TemporaryDirectory() as tmp:
            layout = WorkspaceLayout(project_root=Path(tmp) / "workspaces" / "demo")
            findings = [{
                "kpi_id": "kpi_001", "current": 150.0, "median": 100.0,
                "mad": 10.0, "distance": 5.0, "mad_threshold": 3.0,
            }]
            calls = []

            class _FakeResponse:
                def __enter__(self):
                    return self

                def __exit__(self, *a):
                    return False

            class _FakeHttp:
                def Request(self, url, data=None, headers=None):
                    calls.append((url, data))
                    return ("req", url, data)

                def urlopen(self, req, timeout=None):
                    return _FakeResponse()

            write_kpi_alerts_report(
                findings, layout=layout, webhook_url="https://hooks.example/x", http=_FakeHttp()
            )
            self.assertEqual(len(calls), 1)
            self.assertEqual(calls[0][0], "https://hooks.example/x")
            self.assertIn(b"kpi_001", calls[0][1])

    def test_webhook_failure_never_raises(self):
        with tempfile.TemporaryDirectory() as tmp:
            layout = WorkspaceLayout(project_root=Path(tmp) / "workspaces" / "demo")
            findings = [{
                "kpi_id": "kpi_001", "current": 1.0, "median": 1.0,
                "mad": 1.0, "distance": 5.0, "mad_threshold": 3.0,
            }]

            class _ExplodingHttp:
                def Request(self, *a, **k):
                    raise RuntimeError("network down")

            # Must not raise.
            write_kpi_alerts_report(
                findings, layout=layout, webhook_url="https://hooks.example/x",
                http=_ExplodingHttp(),
            )


def _write_kpi_results(layout: WorkspaceLayout, entries: list) -> None:
    result_dir = layout.reports_dir / "kpi_results"
    result_dir.mkdir(parents=True, exist_ok=True)
    (result_dir / "current.json").write_text(
        json.dumps({"kpis": entries}), encoding="utf-8"
    )


def _preview_markdown(value: float) -> str:
    return f"| total |\n| --- |\n| {value} |"


class CheckKpiAnomaliesMainTests(unittest.TestCase):
    def test_missing_kpi_results_writes_ok_report_without_crashing(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "workspaces" / "demo").mkdir(parents=True)
            rc = check_kpi_anomalies_main(["--workspace", "workspaces/demo", "--repo-root", str(root)])
            self.assertEqual(rc, 0)
            report = root / "workspaces" / "demo" / "interns" / "reports" / "kpi_alerts" / "current.md"
            self.assertTrue(report.exists())
            self.assertIn("[ok]", report.read_text(encoding="utf-8"))

    def test_end_to_end_flags_a_jump_against_history_and_updates_it(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            layout = WorkspaceLayout(project_root=root / "workspaces" / "demo")
            layout.project_root.mkdir(parents=True)
            history_path = layout.evidence_dir / "kpi_headline_history.json"
            history_path.parent.mkdir(parents=True, exist_ok=True)
            history_path.write_text(json.dumps([
                {"date": "2026-07-01", "headlines": {"kpi_001": 100.0}},
                {"date": "2026-07-08", "headlines": {"kpi_001": 100.0}},
                {"date": "2026-07-15", "headlines": {"kpi_001": 100.0}},
                {"date": "2026-07-22", "headlines": {"kpi_001": 100.0}},
            ]), encoding="utf-8")
            _write_kpi_results(layout, [{
                "kpi_id": "kpi_001",
                "status": "ok",
                "definition": {"metric": "sum(paid_amount)"},
                "preview_markdown": _preview_markdown(500.0),
            }])

            rc = check_kpi_anomalies_main(
                ["--workspace", "workspaces/demo", "--repo-root", str(root)]
            )
            self.assertEqual(rc, 0)

            report_text = (layout.reports_dir / "kpi_alerts" / "current.md").read_text(encoding="utf-8")
            self.assertIn("[x]", report_text)
            self.assertIn("kpi_001", report_text)

            updated_history = json.loads(history_path.read_text(encoding="utf-8"))
            self.assertEqual(len(updated_history), 5)
            self.assertEqual(updated_history[-1]["headlines"]["kpi_001"], 500.0)

    def test_mad_threshold_overridable_via_workspace_settings(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            layout = WorkspaceLayout(project_root=root / "workspaces" / "demo")
            layout.project_root.mkdir(parents=True)
            (layout.project_root / "workspace_settings.json").write_text(
                json.dumps({"kpi_alert_mad_threshold": 100.0}), encoding="utf-8"
            )
            history_path = layout.evidence_dir / "kpi_headline_history.json"
            history_path.parent.mkdir(parents=True, exist_ok=True)
            history_path.write_text(json.dumps([
                {"date": "d1", "headlines": {"kpi_001": 10.0}},
                {"date": "d2", "headlines": {"kpi_001": 12.0}},
                {"date": "d3", "headlines": {"kpi_001": 11.0}},
                {"date": "d4", "headlines": {"kpi_001": 13.0}},
            ]), encoding="utf-8")
            _write_kpi_results(layout, [{
                "kpi_id": "kpi_001",
                "status": "ok",
                "definition": {"metric": "sum(paid_amount)"},
                "preview_markdown": _preview_markdown(20.0),
            }])

            check_kpi_anomalies_main(["--workspace", "workspaces/demo", "--repo-root", str(root)])
            # distance 8.5 with default threshold 3.0 would normally flag --
            # the workspace-level override (100.0) must suppress it.
            report_text = (layout.reports_dir / "kpi_alerts" / "current.md").read_text(encoding="utf-8")
            self.assertIn("[ok]", report_text)
            self.assertNotIn("[x]", report_text)


if __name__ == "__main__":
    unittest.main()
