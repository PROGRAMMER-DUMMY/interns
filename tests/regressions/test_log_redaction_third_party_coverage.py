"""Production-readiness fix, P5: core/observability/log_redaction.py's
install_log_redaction() was only ever called once, on the root logger, at
startup. A Python logging Filter only fires for handlers attached to the
SAME logger it's installed on -- it does NOT retroactively cover a
third-party library logger (aiohttp, databricks-sdk, pyspark, urllib3) that
attaches its own handler directly, even though records still propagate to
root for other purposes. Confirmed aiohttp is a real, live dependency
(core/onboarding/sources/catalog.py). docs/core_audit/PROD_SECURITY_GAPS.md
Gap 7 residual risk 1.

Fixed: install_log_redaction_everywhere() installs on root (unchanged
behavior) PLUS every currently-registered logger whose name starts with a
known third-party prefix. Idempotent (reuses install_log_redaction's own
guard). Wired into the same two startup call sites that previously called
install_log_redaction() alone (core/config.py, core/observability/__init__.py).

See ~/.claude/plans/dynamic-cooking-firefly.md P5.
"""
from __future__ import annotations

import logging
import unittest

from core.observability.log_redaction import (
    RedactionFilter,
    THIRD_PARTY_LOGGER_PREFIXES,
    install_log_redaction_everywhere,
)


def _has_redaction_filter(logger: logging.Logger) -> bool:
    return any(isinstance(f, RedactionFilter) for f in logger.filters)


class ThirdPartyCoverageTests(unittest.TestCase):
    def setUp(self):
        # Ensure a fresh, unfiltered logger for each known prefix so the
        # test proves install_log_redaction_everywhere() actually attaches
        # the filter, rather than finding one left over from a prior test
        # or from this repo's own real startup wiring.
        self._test_loggers = []
        for prefix in THIRD_PARTY_LOGGER_PREFIXES:
            name = f"{prefix}.test_child_logger"
            logger = logging.getLogger(name)
            logger.filters = [f for f in logger.filters if not isinstance(f, RedactionFilter)]
            self._test_loggers.append(logger)

    def test_every_known_prefix_gets_the_filter(self):
        install_log_redaction_everywhere()
        for logger in self._test_loggers:
            self.assertTrue(_has_redaction_filter(logger), logger.name)

    def test_root_still_gets_the_filter(self):
        install_log_redaction_everywhere()
        self.assertTrue(_has_redaction_filter(logging.getLogger()))

    def test_idempotent_no_duplicate_filters(self):
        install_log_redaction_everywhere()
        install_log_redaction_everywhere()
        for logger in self._test_loggers:
            count = sum(1 for f in logger.filters if isinstance(f, RedactionFilter))
            self.assertEqual(count, 1, logger.name)

    def test_unrelated_logger_name_is_not_touched(self):
        unrelated = logging.getLogger("some_unrelated_module.test_child")
        unrelated.filters = [f for f in unrelated.filters if not isinstance(f, RedactionFilter)]
        install_log_redaction_everywhere()
        self.assertFalse(_has_redaction_filter(unrelated))


if __name__ == "__main__":
    unittest.main()
