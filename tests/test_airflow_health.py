"""Airflow operability: REST-API is_paused + scheduler-heartbeat health check.

Airflow is not installed in this environment (same constraint
test_orchestration_hardening.py and test_pipeline_orchestration.py already
work under) -- irrelevant here, because `check_airflow_health` talks to a
running deployment over plain HTTP (Airflow 3's `/api/v2/`), never the CLI
(docs/reference/airflow_cli_reference.md section 7: the CLI needs
co-location, REST does not). `http` is injected so no test ever opens a real
socket.
"""
from __future__ import annotations

import json
import unittest

from core.orchestration import airflow_health
from core.orchestration.airflow_health import check_airflow_health


class FakeResponse:
    """Minimal stand-in for `http.client.HTTPResponse` -- just `.read()` and
    an optional `.close()`, the only two things `check_airflow_health` uses."""

    def __init__(self, payload: dict):
        self._body = json.dumps(payload).encode("utf-8")
        self.closed = False

    def read(self) -> bytes:
        return self._body

    def close(self) -> None:
        self.closed = True


def _fake_http(*, scheduler_status: str = "healthy", paused_dag_ids=()):
    """A canned-JSON fake `http` callable. Records every request's URL and
    Authorization header so tests can assert the seam never leaks the token
    anywhere but the header, and never calls the wrong endpoint shape."""
    calls: list = []

    def http(req, timeout=None):
        calls.append(req)
        url = req.full_url
        if "/monitor/health" in url:
            return FakeResponse({"scheduler": {"status": scheduler_status}})
        # /api/v2/dags/{dag_id}
        dag_id = url.rstrip("/").rsplit("/", 1)[-1]
        return FakeResponse({"dag_id": dag_id, "is_paused": dag_id in paused_dag_ids})

    http.calls = calls  # type: ignore[attr-defined]
    return http


class PausedDagTests(unittest.TestCase):
    def test_a_paused_dag_makes_ok_false(self):
        http = _fake_http(paused_dag_ids=("autoresearch_medallion_pipeline",))
        result = check_airflow_health(
            "https://airflow.example.internal",
            "secret-jwt",
            ["autoresearch_medallion_pipeline"],
            http=http,
        )
        self.assertFalse(result["ok"])
        self.assertEqual(result["paused_dags"], ["autoresearch_medallion_pipeline"])
        self.assertEqual(result["scheduler"], "healthy")

    def test_multiple_dags_only_the_paused_one_is_named(self):
        http = _fake_http(paused_dag_ids=("dag_b",))
        result = check_airflow_health(
            "https://airflow.example.internal", "tok", ["dag_a", "dag_b"], http=http,
        )
        self.assertFalse(result["ok"])
        self.assertEqual(result["paused_dags"], ["dag_b"])


class HealthyDagTests(unittest.TestCase):
    def test_healthy_scheduler_and_unpaused_dag_is_ok(self):
        http = _fake_http(scheduler_status="healthy", paused_dag_ids=())
        result = check_airflow_health(
            "https://airflow.example.internal", "tok", ["autoresearch_medallion_pipeline"],
            http=http,
        )
        self.assertTrue(result["ok"])
        self.assertEqual(result["paused_dags"], [])
        self.assertEqual(result["scheduler"], "healthy")

    def test_no_dag_ids_still_reports_scheduler_health(self):
        http = _fake_http(scheduler_status="healthy")
        result = check_airflow_health("https://airflow.example.internal", "tok", [], http=http)
        self.assertTrue(result["ok"])
        self.assertEqual(result["paused_dags"], [])


class UnhealthySchedulerTests(unittest.TestCase):
    def test_unhealthy_scheduler_status_makes_ok_false_even_if_unpaused(self):
        http = _fake_http(scheduler_status="unhealthy", paused_dag_ids=())
        result = check_airflow_health(
            "https://airflow.example.internal", "tok", ["dag_a"], http=http,
        )
        self.assertFalse(result["ok"])
        self.assertEqual(result["scheduler"], "unhealthy")


class ConnectionErrorTests(unittest.TestCase):
    def test_connection_error_reports_unreachable_and_not_ok(self):
        def _boom(req, timeout=None):
            raise ConnectionRefusedError("refused")

        result = check_airflow_health(
            "https://airflow.example.internal", "tok", ["dag_a"], http=_boom,
        )
        self.assertFalse(result["ok"])
        self.assertEqual(result["scheduler"], "unreachable")
        self.assertEqual(result["paused_dags"], [])

    def test_per_dag_lookup_failure_after_a_healthy_scheduler_counts_as_paused(self):
        # The health endpoint answered (scheduler IS reachable) but one dag
        # lookup itself fails -- an unknown state must not silently read as
        # healthy, so it is folded into paused_dags rather than crashing the
        # whole probe or reporting ok=True.
        def http(req, timeout=None):
            if "/monitor/health" in req.full_url:
                return FakeResponse({"scheduler": {"status": "healthy"}})
            raise TimeoutError("dag lookup timed out")

        result = check_airflow_health(
            "https://airflow.example.internal", "tok", ["dag_a"], http=http,
        )
        self.assertFalse(result["ok"])
        self.assertEqual(result["scheduler"], "healthy")
        self.assertEqual(result["paused_dags"], ["dag_a"])


class SecretHygieneTests(unittest.TestCase):
    def test_token_never_appears_in_the_returned_result(self):
        http = _fake_http()
        result = check_airflow_health(
            "https://airflow.example.internal", "super-secret-jwt-value", ["dag_a"], http=http,
        )
        self.assertNotIn("super-secret-jwt-value", json.dumps(result))

    def test_token_is_carried_only_in_the_authorization_header(self):
        http = _fake_http()
        check_airflow_health(
            "https://airflow.example.internal", "super-secret-jwt-value", ["dag_a"], http=http,
        )
        for req in http.calls:  # type: ignore[attr-defined]
            self.assertEqual(req.headers.get("Authorization"), "Bearer super-secret-jwt-value")

    def test_connection_error_detail_is_redacted(self):
        def _boom(req, timeout=None):
            raise ConnectionRefusedError(
                "connection refused to dbc-secret.cloud.databricks.com"
            )

        result = check_airflow_health(
            "https://airflow.example.internal", "tok", [], http=_boom,
        )
        self.assertNotIn("dbc-secret.cloud.databricks.com", json.dumps(result))


class ApiShapeTests(unittest.TestCase):
    def test_hits_api_v2_never_the_v1_or_bare_path(self):
        http = _fake_http()
        check_airflow_health("https://airflow.example.internal", "tok", ["dag_a"], http=http)
        urls = [req.full_url for req in http.calls]  # type: ignore[attr-defined]
        self.assertTrue(any("/api/v2/monitor/health" in u for u in urls))
        self.assertTrue(any(u.endswith("/api/v2/dags/dag_a") for u in urls))

    def test_base_url_trailing_slash_is_tolerated(self):
        http = _fake_http()
        check_airflow_health("https://airflow.example.internal/", "tok", ["dag_a"], http=http)
        urls = [req.full_url for req in http.calls]  # type: ignore[attr-defined]
        self.assertTrue(all("//api" not in u for u in urls))


class MainCliTests(unittest.TestCase):
    def test_missing_base_url_or_token_is_a_clean_failure_not_a_crash(self):
        code = airflow_health.main(["--dag-id", "x"])
        self.assertEqual(code, 1)


if __name__ == "__main__":
    unittest.main()
