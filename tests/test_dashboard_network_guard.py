"""Tests for the workspace-dashboard network exposure guard."""

from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from tools.workspace_dashboard import (
    DASHBOARD_TOKEN_ENV,
    attach_token_auth,
    is_loopback_host,
    main,
)


class LoopbackDetectionTests(unittest.TestCase):
    def test_loopback_hosts(self) -> None:
        for host in ("127.0.0.1", "localhost", "LOCALHOST", "::1", "127.0.0.5"):
            self.assertTrue(is_loopback_host(host), host)

    def test_non_loopback_hosts(self) -> None:
        for host in ("0.0.0.0", "192.168.1.10", "", "example.com"):
            self.assertFalse(is_loopback_host(host), host)


class NonLoopbackRefusalTests(unittest.TestCase):
    def test_public_bind_without_token_is_refused(self) -> None:
        with tempfile.TemporaryDirectory() as t:
            ws = Path(t) / "workspaces" / "demo"
            ws.mkdir(parents=True)
            env = {k: v for k, v in os.environ.items() if k != DASHBOARD_TOKEN_ENV}
            with mock.patch.dict(os.environ, env, clear=True):
                rc = main([
                    "--workspace", "workspaces/demo",
                    "--repo-root", t,
                    "--host", "0.0.0.0",
                    "--no-refresh",
                ])
        self.assertEqual(rc, 2)


class TokenAuthTests(unittest.TestCase):
    def _client(self, token: str):
        from flask import Flask

        class _FakeDashApp:
            server = Flask(__name__)

        app = _FakeDashApp()

        @app.server.route("/ping")
        def _ping():  # pragma: no cover - trivial
            return "pong"

        attach_token_auth(app, token)
        return app.server.test_client()

    def test_request_without_token_is_401(self) -> None:
        client = self._client("s3cret")
        self.assertEqual(client.get("/ping").status_code, 401)

    def test_request_with_bearer_token_passes(self) -> None:
        client = self._client("s3cret")
        resp = client.get("/ping", headers={"Authorization": "Bearer s3cret"})
        self.assertEqual(resp.status_code, 200)

    def test_request_with_query_token_passes(self) -> None:
        client = self._client("s3cret")
        self.assertEqual(client.get("/ping?token=s3cret").status_code, 200)

    def test_wrong_token_is_401(self) -> None:
        client = self._client("s3cret")
        resp = client.get("/ping", headers={"Authorization": "Bearer nope"})
        self.assertEqual(resp.status_code, 401)


if __name__ == "__main__":
    unittest.main()
