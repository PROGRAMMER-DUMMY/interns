"""Tests for `workspace-dashboard --live` (vendored-MinusAnalyst path).

Exercises the DQ gate + wiring without blocking on the Dash server (create_app
and app.run are stubbed). Skips when the bronze layer is absent.
"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path
from unittest import mock

# vendored MinusAnalyst lives under vendor/ -- ensure it's importable so the
# create_app patch target resolves.
_VENDOR = str((Path(__file__).resolve().parents[1] / "vendor"))
if _VENDOR not in sys.path:
    sys.path.insert(0, _VENDOR)

_WS_REL = "workspaces/Healthcare-RCM-Data-Platform"
_WS = Path(_WS_REL)


class _StubApp:
    def __init__(self):
        self.ran_with = None
        self.server = mock.MagicMock()

    def run(self, **kwargs):
        self.ran_with = kwargs  # do not actually serve


class _StubState:
    pages = []


@unittest.skipUnless((_WS / "interns/state/medallion/bronze").exists(),
                     "bronze layer not present")
class TestLiveCli(unittest.TestCase):
    def test_live_serves_when_certified(self):
        import tools.workspace_dashboard as cli
        stub = _StubApp()
        ok = {"ok": True, "published": True, "rows": 10000, "force": False,
              "measures": ["total_paid"], "pages": ["overview"]}
        with mock.patch.object(cli, "build_dash_app") as legacy, \
             mock.patch("core.dashboard.minus_adapter.generate", return_value=ok) as gen, \
             mock.patch("minus.render.app.create_app",
                        return_value=(stub, _StubState())) as create:
            rc = cli.main(["--workspace", _WS_REL, "--live", "--no-refresh", "--port", "8098"])
        self.assertEqual(rc, 0)
        legacy.assert_not_called()        # legacy renderer NOT used on --live
        gen.assert_called_once()
        create.assert_called_once()       # launched the vendored MinusAnalyst app
        self.assertEqual(stub.ran_with.get("port"), 8098)
        self.assertEqual(stub.ran_with.get("host"), "127.0.0.1")

    def test_live_refuses_on_dq_failure(self):
        import tools.workspace_dashboard as cli
        bad = {"ok": False, "reason": "DQ certification failed",
               "failed": [{"check": "no_fanout", "detail": "rows blew up"}]}
        with mock.patch("core.dashboard.minus_adapter.generate", return_value=bad), \
             mock.patch("minus.render.app.create_app") as create:
            rc = cli.main(["--workspace", _WS_REL, "--live", "--no-refresh"])
        self.assertEqual(rc, 1)           # refused
        create.assert_not_called()        # never launched

    def test_refresh_regenerates_and_exits(self):
        import tools.workspace_dashboard as cli
        ok = {"ok": True, "published": True, "rows": 10000, "certified": True,
              "pages": ["kpis", "overview"], "refresh_seconds": 0}
        with mock.patch("core.dashboard.minus_adapter.generate", return_value=ok) as gen, \
             mock.patch("minus.render.app.create_app") as create:
            rc = cli.main(["--workspace", _WS_REL, "--refresh", "--no-refresh"])
        self.assertEqual(rc, 0)
        gen.assert_called_once()
        create.assert_not_called()        # --refresh does NOT launch a server

    def test_refresh_blocked_on_dq_failure_returns_1(self):
        import tools.workspace_dashboard as cli
        bad = {"ok": False, "reason": "DQ certification failed", "published": False,
               "failed": [{"check": "lossless", "detail": "drift"}]}
        with mock.patch("core.dashboard.minus_adapter.generate", return_value=bad):
            rc = cli.main(["--workspace", _WS_REL, "--refresh", "--no-refresh"])
        self.assertEqual(rc, 1)

    def test_force_serves_despite_findings(self):
        import tools.workspace_dashboard as cli
        stub = _StubApp()
        forced = {"ok": True, "published": True, "rows": 10000, "force": True,
                  "measures": ["total_paid"], "pages": ["overview"]}
        with mock.patch("core.dashboard.minus_adapter.generate", return_value=forced), \
             mock.patch("minus.render.app.create_app",
                        return_value=(stub, _StubState())):
            rc = cli.main(["--workspace", _WS_REL, "--live", "--no-refresh", "--force"])
        self.assertEqual(rc, 0)
        self.assertIsNotNone(stub.ran_with)


if __name__ == "__main__":
    unittest.main()
