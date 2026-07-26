"""Production-readiness fix, P1: dashboard.py's Werkzeug-debug + non-loopback
combination was only ever a stderr warning, never a hard stop. The Werkzeug
interactive debugger is arbitrary Python code execution on the server -- an
operator missing a warning line in container stdout is not an acceptable bar
for RCE prevention (docs/core_audit/PROD_SECURITY_GAPS.md Gap 1 item 5).

Fixed: `_refuse_debug_on_non_loopback` raises SystemExit before `app.run` is
ever reached when debug=True and the host is not loopback. Every other
combination (debug+loopback, no-debug+any-host) is completely unaffected --
this is a startup guard, not a behavior change to the app itself.

See ~/.claude/plans/dynamic-cooking-firefly.md P1.
"""
from __future__ import annotations

import unittest

import dashboard


class DebugHostGuardTests(unittest.TestCase):
    def setUp(self):
        self.loopback = {"127.0.0.1", "localhost", "::1"}

    def test_debug_on_non_loopback_refuses_to_start(self):
        with self.assertRaises(SystemExit) as ctx:
            dashboard._refuse_debug_on_non_loopback(True, "0.0.0.0", self.loopback)
        self.assertNotEqual(ctx.exception.code, 0)

    def test_debug_on_loopback_is_unaffected(self):
        dashboard._refuse_debug_on_non_loopback(True, "127.0.0.1", self.loopback)  # no raise

    def test_debug_off_non_loopback_is_unaffected(self):
        dashboard._refuse_debug_on_non_loopback(False, "0.0.0.0", self.loopback)  # no raise

    def test_debug_off_loopback_is_unaffected(self):
        dashboard._refuse_debug_on_non_loopback(False, "127.0.0.1", self.loopback)  # no raise


if __name__ == "__main__":
    unittest.main()
