"""doctor: one-pass setup/readiness check (Python/uv/Java, Databricks/dbt/Airflow,
git hygiene). Mocks the expensive/environment-dependent sub-checks so this suite is
fast and deterministic regardless of what's actually installed on the runner."""
from __future__ import annotations

import io
import json
import unittest
from contextlib import redirect_stdout
from unittest import mock

from core.dev import doctor


def _ready(detail: str = "ok") -> dict:
    return {"status": "ready", "detail": detail}


def _blocked(detail: str = "broken") -> dict:
    return {"status": "blocked", "detail": detail}


class DoctorReportTests(unittest.TestCase):
    def test_no_blockers_when_everything_ready(self):
        report = doctor.DoctorReport(
            python=_ready(), uv_venv=_ready(), java=_ready(),
            databricks=_ready(), dbt=_ready(), airflow=_ready(), git_hygiene=_ready(),
        )
        self.assertEqual(report.blockers(), [])

    def test_blocked_status_is_surfaced_by_name(self):
        report = doctor.DoctorReport(
            python=_ready(), uv_venv=_ready(), java=_blocked("Java 24 unsupported"),
            databricks=_blocked("Invalid access token"), dbt=_ready(),
            airflow={"status": "not_installed", "detail": "optional"}, git_hygiene=_ready(),
        )
        blockers = report.blockers()
        self.assertEqual(len(blockers), 2)
        self.assertTrue(any("java" in b and "Java 24" in b for b in blockers))
        self.assertTrue(any("databricks" in b and "Invalid access token" in b for b in blockers))

    def test_not_installed_is_not_a_blocker(self):
        report = doctor.DoctorReport(
            python=_ready(), uv_venv=_ready(), java=_ready(), databricks=_ready(), dbt=_ready(),
            airflow={"status": "not_installed", "detail": "optional"}, git_hygiene=_ready(),
        )
        self.assertEqual(report.blockers(), [])


class JavaCheckTests(unittest.TestCase):
    def test_missing_java_reports_not_installed_not_blocked(self):
        with mock.patch("subprocess.run", side_effect=FileNotFoundError):
            result = doctor._check_java()
        self.assertEqual(result["status"], "not_installed")

    def test_java_18_plus_is_blocked_for_pyspark(self):
        proc = mock.Mock(stderr='openjdk version "24.0.1" 2026-01-01', stdout="")
        with mock.patch("subprocess.run", return_value=proc):
            result = doctor._check_java()
        self.assertEqual(result["status"], "blocked")
        self.assertIn("24", result["detail"])

    def test_java_17_is_ready(self):
        proc = mock.Mock(stderr='openjdk version "17.0.9" 2023-10-17', stdout="")
        with mock.patch("subprocess.run", return_value=proc):
            result = doctor._check_java()
        self.assertEqual(result["status"], "ready")


class PythonUvCheckTests(unittest.TestCase):
    def test_python_below_minimum_is_blocked(self):
        fake_version = mock.Mock(major=3, minor=8, micro=0)
        with mock.patch.object(doctor.sys, "version_info", fake_version):
            result = doctor._check_python()
        self.assertEqual(result["status"], "blocked")

    def test_uv_missing_from_path_is_blocked(self):
        with mock.patch("shutil.which", return_value=None):
            result = doctor._check_uv_venv()
        self.assertEqual(result["status"], "blocked")


class MainCliTests(unittest.TestCase):
    def _fake_report(self, *, with_blocker: bool) -> doctor.DoctorReport:
        java = _blocked("Java 24 unsupported") if with_blocker else _ready()
        return doctor.DoctorReport(
            python=_ready(), uv_venv=_ready(), java=java, databricks=_ready(),
            dbt=_ready(), airflow={"status": "not_installed", "detail": "optional"},
            git_hygiene=_ready(),
        )

    def test_json_output_is_valid_and_exit_code_reflects_blockers(self):
        with mock.patch.object(doctor, "check", return_value=self._fake_report(with_blocker=True)):
            buf = io.StringIO()
            with redirect_stdout(buf):
                code = doctor.main(["--json"])
        self.assertEqual(code, 1)
        parsed = json.loads(buf.getvalue())
        self.assertEqual(parsed["java"]["status"], "blocked")

    def test_clean_run_exits_zero(self):
        with mock.patch.object(doctor, "check", return_value=self._fake_report(with_blocker=False)):
            buf = io.StringIO()
            with redirect_stdout(buf):
                code = doctor.main([])
        self.assertEqual(code, 0)
        self.assertIn("no blockers", buf.getvalue())

    def test_main_is_anchored(self):
        self.assertTrue(getattr(doctor.main, "__anchored__", False))


if __name__ == "__main__":
    unittest.main()
