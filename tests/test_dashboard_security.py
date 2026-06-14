"""
Hermetic security tests for the dashboard.py security helpers.

No real server is started; no network sockets are opened.
Tests cover:
  - _resolve_debug()  — debug must default to False; only True when env is "1"
  - _resolve_host()   — default host must be 127.0.0.1
  - _setup_auth()     — 401 on missing/wrong creds; 200 on correct creds
"""

from __future__ import annotations

import importlib
import sys
import types
import unittest
from unittest import mock


# ---------------------------------------------------------------------------
# Lazy import helpers — dashboard.py has heavy top-level imports (Dash, etc.)
# We mock only what is needed so the helpers can be tested without the full
# dependency chain.
# ---------------------------------------------------------------------------

def _import_dashboard_module():
    """Import dashboard and return the module.

    If dashboard is already in sys.modules (e.g. from a previous test run in
    the same process) return it directly; otherwise import it fresh.
    """
    if "dashboard" in sys.modules:
        return sys.modules["dashboard"]
    return importlib.import_module("dashboard")


# ---------------------------------------------------------------------------
# 1.  _resolve_debug()
# ---------------------------------------------------------------------------

class ResolveDebugTests(unittest.TestCase):
    """Debug must default to False and only activate on explicit env opt-in."""

    def _call(self, env: dict) -> bool:
        with mock.patch.dict("os.environ", env, clear=False):
            mod = _import_dashboard_module()
            # Re-evaluate the function against the patched environment.
            # We call it directly so no server is started.
            return mod._resolve_debug()

    def test_debug_off_by_default(self) -> None:
        """When AUTORESEARCH_DASHBOARD_DEBUG is absent, debug is False."""
        env_without = {k: v for k, v in __import__("os").environ.items()
                       if k != "AUTORESEARCH_DASHBOARD_DEBUG"}
        with mock.patch.dict("os.environ", env_without, clear=True):
            mod = _import_dashboard_module()
            self.assertFalse(mod._resolve_debug())

    def test_debug_off_when_env_is_zero(self) -> None:
        """AUTORESEARCH_DASHBOARD_DEBUG=0 must NOT enable debug."""
        with mock.patch.dict("os.environ",
                             {"AUTORESEARCH_DASHBOARD_DEBUG": "0"}, clear=False):
            mod = _import_dashboard_module()
            self.assertFalse(mod._resolve_debug())

    def test_debug_off_when_env_is_empty(self) -> None:
        """AUTORESEARCH_DASHBOARD_DEBUG='' must NOT enable debug."""
        with mock.patch.dict("os.environ",
                             {"AUTORESEARCH_DASHBOARD_DEBUG": ""}, clear=False):
            mod = _import_dashboard_module()
            self.assertFalse(mod._resolve_debug())

    def test_debug_on_when_env_is_one(self) -> None:
        """AUTORESEARCH_DASHBOARD_DEBUG=1 must enable debug."""
        with mock.patch.dict("os.environ",
                             {"AUTORESEARCH_DASHBOARD_DEBUG": "1"}, clear=False):
            mod = _import_dashboard_module()
            self.assertTrue(mod._resolve_debug())

    def test_debug_off_for_arbitrary_truthy_string(self) -> None:
        """Only the literal '1' enables debug — not 'true', 'yes', etc."""
        for val in ("true", "yes", "True", "on", "1 ", " 1"):
            with self.subTest(val=val):
                with mock.patch.dict("os.environ",
                                     {"AUTORESEARCH_DASHBOARD_DEBUG": val},
                                     clear=False):
                    mod = _import_dashboard_module()
                    # " 1" (with space) strips to "1" and should be True;
                    # everything else should be False.
                    expected = val.strip() == "1"
                    self.assertEqual(mod._resolve_debug(), expected, val)


# ---------------------------------------------------------------------------
# 2.  _resolve_host()
# ---------------------------------------------------------------------------

class ResolveHostTests(unittest.TestCase):
    """Default host must be 127.0.0.1 (loopback)."""

    def test_default_host_is_loopback(self) -> None:
        """When AUTORESEARCH_DASHBOARD_HOST is absent, host is 127.0.0.1."""
        env_without = {k: v for k, v in __import__("os").environ.items()
                       if k != "AUTORESEARCH_DASHBOARD_HOST"}
        with mock.patch.dict("os.environ", env_without, clear=True):
            mod = _import_dashboard_module()
            self.assertEqual(mod._resolve_host(), "127.0.0.1")

    def test_custom_host_is_respected(self) -> None:
        """An explicit env value overrides the default."""
        with mock.patch.dict("os.environ",
                             {"AUTORESEARCH_DASHBOARD_HOST": "0.0.0.0"},
                             clear=False):
            mod = _import_dashboard_module()
            self.assertEqual(mod._resolve_host(), "0.0.0.0")

    def test_empty_env_falls_back_to_loopback(self) -> None:
        """An empty AUTORESEARCH_DASHBOARD_HOST falls back to 127.0.0.1."""
        with mock.patch.dict("os.environ",
                             {"AUTORESEARCH_DASHBOARD_HOST": ""},
                             clear=False):
            mod = _import_dashboard_module()
            self.assertEqual(mod._resolve_host(), "127.0.0.1")


# ---------------------------------------------------------------------------
# 3.  _setup_auth()  — Basic Auth + Bearer token
# ---------------------------------------------------------------------------

class _FakeDashApp:
    """Minimal stand-in for a Dash app that exposes app.server (Flask)."""

    def __init__(self) -> None:
        from flask import Flask
        self.server = Flask(__name__ + "_test_auth")

        @self.server.route("/ping")
        def _ping():  # pragma: no cover
            return "pong", 200


class BasicAuthTests(unittest.TestCase):
    """Auth gate activated via AUTORESEARCH_DASHBOARD_AUTH_USER/PASSWORD."""

    def setUp(self) -> None:
        self.app = _FakeDashApp()
        env = {
            "AUTORESEARCH_DASHBOARD_AUTH_USER":     "alice",
            "AUTORESEARCH_DASHBOARD_AUTH_PASSWORD": "s3cret",
        }
        # Remove any token env to ensure only Basic Auth is active.
        env_clean = {k: v for k, v in __import__("os").environ.items()
                     if k not in ("AUTORESEARCH_DASHBOARD_AUTH_TOKEN",
                                  "AUTORESEARCH_DASHBOARD_AUTH_USER",
                                  "AUTORESEARCH_DASHBOARD_AUTH_PASSWORD")}
        env_clean.update(env)
        with mock.patch.dict("os.environ", env_clean, clear=True):
            mod = _import_dashboard_module()
            activated = mod._setup_auth(self.app.server)
        self.assertTrue(activated, "_setup_auth should return True when creds are set")
        self.client = self.app.server.test_client()

    def test_no_credentials_is_401(self) -> None:
        resp = self.client.get("/ping")
        self.assertEqual(resp.status_code, 401)

    def test_wrong_password_is_401(self) -> None:
        import base64 as _b64
        cred = _b64.b64encode(b"alice:wrong").decode()
        resp = self.client.get("/ping", headers={"Authorization": f"Basic {cred}"})
        self.assertEqual(resp.status_code, 401)

    def test_wrong_username_is_401(self) -> None:
        import base64 as _b64
        cred = _b64.b64encode(b"bob:s3cret").decode()
        resp = self.client.get("/ping", headers={"Authorization": f"Basic {cred}"})
        self.assertEqual(resp.status_code, 401)

    def test_correct_basic_auth_passes(self) -> None:
        import base64 as _b64
        cred = _b64.b64encode(b"alice:s3cret").decode()
        resp = self.client.get("/ping", headers={"Authorization": f"Basic {cred}"})
        self.assertEqual(resp.status_code, 200)


class TokenAuthTests(unittest.TestCase):
    """Auth gate activated via AUTORESEARCH_DASHBOARD_AUTH_TOKEN (Bearer)."""

    def setUp(self) -> None:
        self.app = _FakeDashApp()
        env_clean = {k: v for k, v in __import__("os").environ.items()
                     if k not in ("AUTORESEARCH_DASHBOARD_AUTH_TOKEN",
                                  "AUTORESEARCH_DASHBOARD_AUTH_USER",
                                  "AUTORESEARCH_DASHBOARD_AUTH_PASSWORD")}
        env_clean["AUTORESEARCH_DASHBOARD_AUTH_TOKEN"] = "tok3n"
        with mock.patch.dict("os.environ", env_clean, clear=True):
            mod = _import_dashboard_module()
            activated = mod._setup_auth(self.app.server)
        self.assertTrue(activated)
        self.client = self.app.server.test_client()

    def test_no_token_is_401(self) -> None:
        self.assertEqual(self.client.get("/ping").status_code, 401)

    def test_wrong_bearer_is_401(self) -> None:
        resp = self.client.get("/ping", headers={"Authorization": "Bearer bad"})
        self.assertEqual(resp.status_code, 401)

    def test_correct_bearer_passes(self) -> None:
        resp = self.client.get("/ping", headers={"Authorization": "Bearer tok3n"})
        self.assertEqual(resp.status_code, 200)

    def test_correct_query_token_passes(self) -> None:
        resp = self.client.get("/ping?token=tok3n")
        self.assertEqual(resp.status_code, 200)

    def test_wrong_query_token_is_401(self) -> None:
        resp = self.client.get("/ping?token=nope")
        self.assertEqual(resp.status_code, 401)


class NoAuthConfiguredTests(unittest.TestCase):
    """When no auth env vars are set, _setup_auth returns False (no gate)."""

    def test_returns_false_when_no_env(self) -> None:
        app = _FakeDashApp()
        env_clean = {k: v for k, v in __import__("os").environ.items()
                     if k not in ("AUTORESEARCH_DASHBOARD_AUTH_TOKEN",
                                  "AUTORESEARCH_DASHBOARD_AUTH_USER",
                                  "AUTORESEARCH_DASHBOARD_AUTH_PASSWORD")}
        with mock.patch.dict("os.environ", env_clean, clear=True):
            mod = _import_dashboard_module()
            result = mod._setup_auth(app.server)
        self.assertFalse(result)

    def test_no_gate_when_inactive(self) -> None:
        """Without auth configured, requests go through freely."""
        app = _FakeDashApp()
        env_clean = {k: v for k, v in __import__("os").environ.items()
                     if k not in ("AUTORESEARCH_DASHBOARD_AUTH_TOKEN",
                                  "AUTORESEARCH_DASHBOARD_AUTH_USER",
                                  "AUTORESEARCH_DASHBOARD_AUTH_PASSWORD")}
        with mock.patch.dict("os.environ", env_clean, clear=True):
            mod = _import_dashboard_module()
            mod._setup_auth(app.server)
        client = app.server.test_client()
        self.assertEqual(client.get("/ping").status_code, 200)


if __name__ == "__main__":
    unittest.main()
