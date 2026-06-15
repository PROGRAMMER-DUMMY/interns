"""Phase 5 tests: the `workspace-dashboard --live` entrypoint.

Exercises the parity-gate + wiring of the live branch without blocking on the
Dash server (app.run is stubbed). Skips when the gold layer is absent.
"""
from __future__ import annotations

import unittest
from pathlib import Path
from unittest import mock

from core.storage.workspace_layout import WorkspaceLayout

_WS_REL = "workspaces/Healthcare-RCM-Data-Platform"
_WS = Path(_WS_REL)


class _StubApp:
    def __init__(self):
        self.ran_with = None

    def run(self, **kwargs):
        self.ran_with = kwargs  # do not actually serve


@unittest.skipUnless((_WS / "interns/state/medallion/gold").exists(),
                     "gold layer not present")
class TestLiveCli(unittest.TestCase):
    def test_live_serves_when_parity_passes(self):
        import tools.workspace_dashboard as cli
        stub = _StubApp()
        with mock.patch.object(cli, "build_dash_app") as legacy, \
             mock.patch("core.dashboard.ui.build_live_dashboard", return_value=stub) as build:
            rc = cli.main(["--workspace", _WS_REL, "--live", "--no-refresh",
                           "--port", "8099"])
        self.assertEqual(rc, 0)
        legacy.assert_not_called()           # legacy renderer NOT used on --live
        build.assert_called_once()
        self.assertEqual(stub.ran_with.get("port"), 8099)
        self.assertEqual(stub.ran_with.get("host"), "127.0.0.1")

    def test_live_refuses_on_parity_failure(self):
        import tools.workspace_dashboard as cli
        failing = [{"kpi_id": "kpi_x", "ok": False, "reason": "boom"}]
        with mock.patch("core.dashboard.model.parity.check_parity", return_value=failing), \
             mock.patch("core.dashboard.ui.build_live_dashboard") as build:
            rc = cli.main(["--workspace", _WS_REL, "--live", "--no-refresh"])
        self.assertEqual(rc, 1)              # refused
        build.assert_not_called()           # never built/served

    def test_force_serves_despite_parity_failure(self):
        import tools.workspace_dashboard as cli
        stub = _StubApp()
        failing = [{"kpi_id": "kpi_x", "ok": False, "reason": "boom"}]
        with mock.patch("core.dashboard.model.parity.check_parity", return_value=failing), \
             mock.patch("core.dashboard.ui.build_live_dashboard", return_value=stub):
            rc = cli.main(["--workspace", _WS_REL, "--live", "--no-refresh", "--force"])
        self.assertEqual(rc, 0)
        self.assertIsNotNone(stub.ran_with)


if __name__ == "__main__":
    unittest.main()
