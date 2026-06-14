"""Hermetic unit tests for core.observability.log_redaction.

Uses only stdlib; no network, no filesystem, no workspace fixtures.
"""
from __future__ import annotations

import logging
import unittest

from core.observability.log_redaction import (
    RedactionFilter,
    install_log_redaction,
    redact,
)


class TestRedactFunction(unittest.TestCase):
    """Tests for the pure ``redact(text)`` function."""

    # -- PII / PHI -----------------------------------------------------------

    def test_email_is_redacted(self) -> None:
        result = redact("Contact us at patient.doe@hospital.example.com for details.")
        self.assertNotIn("patient.doe@hospital.example.com", result)
        self.assertIn("[REDACTED:EMAIL]", result)

    def test_ssn_is_redacted(self) -> None:
        result = redact("SSN on file: 123-45-6789.")
        self.assertNotIn("123-45-6789", result)
        self.assertIn("[REDACTED:SSN]", result)

    def test_card_number_is_redacted(self) -> None:
        # 16-digit number -- canonical PAN length
        result = redact("Card number 4111111111111111 was charged.")
        self.assertNotIn("4111111111111111", result)
        self.assertIn("[REDACTED:CARD]", result)

    def test_long_account_number_12_digits_is_redacted(self) -> None:
        result = redact("Account: 123456789012 was flagged.")
        self.assertNotIn("123456789012", result)
        # Could match ACCOUNT or CARD depending on length; either is acceptable
        self.assertTrue(
            "[REDACTED:ACCOUNT]" in result or "[REDACTED:CARD]" in result,
            msg=f"Expected a digit-run redaction marker, got: {result!r}",
        )

    # -- Secrets / tokens ----------------------------------------------------

    def test_github_pat_ghp_is_redacted(self) -> None:
        token = "ghp_abcdefghij1234567890"
        result = redact(f"Using token {token} to authenticate.")
        self.assertNotIn(token, result)
        self.assertIn("[REDACTED:SECRET]", result)

    def test_github_oauth_gho_is_redacted(self) -> None:
        token = "gho_ABCDEFabcdef123456"
        result = redact(f"token={token}")
        self.assertNotIn(token, result)
        self.assertIn("[REDACTED:SECRET]", result)

    def test_databricks_token_is_redacted(self) -> None:
        token = "dapi1234567890abcdef1234"
        result = redact(f"Databricks token: {token}")
        self.assertNotIn(token, result)
        self.assertIn("[REDACTED:SECRET]", result)

    def test_sk_token_is_redacted(self) -> None:
        token = "sk-abcdefghij1234567890ABCD"
        result = redact(f"api_key={token}")
        self.assertNotIn(token, result)
        self.assertIn("[REDACTED:SECRET]", result)

    def test_aws_akia_is_redacted(self) -> None:
        token = "AKIAIOSFODNN7EXAMPLE"
        result = redact(f"AWS key: {token}")
        self.assertNotIn(token, result)
        self.assertIn("[REDACTED:SECRET]", result)

    def test_bearer_token_is_redacted(self) -> None:
        result = redact("Authorization: Bearer eyJhbGciOiJSUzI1NiJ9.payload")
        self.assertNotIn("eyJhbGciOiJSUzI1NiJ9", result)
        self.assertIn("[REDACTED:SECRET]", result)

    def test_generic_api_key_assignment_is_redacted(self) -> None:
        result = redact("api_key='supersecretvalue123'")
        self.assertNotIn("supersecretvalue123", result)
        self.assertIn("[REDACTED:SECRET]", result)

    # -- Low false-positive check on normal prose ----------------------------

    def test_ordinary_sentence_is_not_redacted(self) -> None:
        sentence = "The query returned 42 rows in 100 milliseconds with no errors."
        self.assertEqual(redact(sentence), sentence)

    def test_short_word_not_treated_as_ssn(self) -> None:
        # "123-4-5678" does not match SSN pattern (middle group must be 2 digits)
        text = "Invoice 123-4-5678 processed."
        self.assertEqual(redact(text), text)

    def test_year_not_treated_as_digit_run(self) -> None:
        # "2024" is 4 digits -- well below the 12-digit threshold
        text = "Fiscal year 2024 results."
        self.assertEqual(redact(text), text)

    def test_non_string_returns_unchanged(self) -> None:
        # redact() is a str->str helper; non-strings should pass through
        self.assertEqual(redact(42), 42)  # type: ignore[arg-type]
        self.assertIsNone(redact(None))  # type: ignore[arg-type]

    def test_empty_string_is_unchanged(self) -> None:
        self.assertEqual(redact(""), "")

    def test_already_redacted_marker_is_stable(self) -> None:
        # Applying redact() twice must not alter the marker
        once = redact("SSN: 123-45-6789")
        twice = redact(once)
        self.assertEqual(once, twice)


class TestRedactionFilter(unittest.TestCase):
    """Tests for RedactionFilter attached to a real logging.Logger."""

    def _make_logger(self, name: str) -> tuple[logging.Logger, list[logging.LogRecord]]:
        """Return a logger + record list pre-wired with a capturing handler."""
        logger = logging.getLogger(f"test_redaction.{name}")
        logger.setLevel(logging.DEBUG)
        logger.propagate = False
        records: list[logging.LogRecord] = []

        class _Capture(logging.Handler):
            def emit(self, record: logging.LogRecord) -> None:
                records.append(record)

        logger.addHandler(_Capture())
        return logger, records

    def test_email_in_log_message_is_redacted(self) -> None:
        logger, records = self._make_logger("email")
        logger.addFilter(RedactionFilter())
        logger.error("Patient email is jane.doe@clinic.example.org")
        self.assertEqual(len(records), 1)
        self.assertNotIn("jane.doe@clinic.example.org", records[0].getMessage())
        self.assertIn("[REDACTED:EMAIL]", records[0].getMessage())

    def test_ssn_in_log_message_is_redacted(self) -> None:
        logger, records = self._make_logger("ssn")
        logger.addFilter(RedactionFilter())
        logger.warning("SSN lookup for 987-65-4321 completed.")
        self.assertNotIn("987-65-4321", records[0].getMessage())
        self.assertIn("[REDACTED:SSN]", records[0].getMessage())

    def test_github_token_in_log_is_redacted(self) -> None:
        logger, records = self._make_logger("ghp")
        logger.addFilter(RedactionFilter())
        token = "ghp_abcdefghij1234567890"
        logger.info("Cloning repo with token %s", token)
        msg = records[0].getMessage()
        self.assertNotIn(token, msg)
        self.assertIn("[REDACTED:SECRET]", msg)

    def test_long_digit_run_in_log_is_redacted(self) -> None:
        logger, records = self._make_logger("digits")
        logger.addFilter(RedactionFilter())
        logger.debug("MRN: 123456789012 admitted.")
        msg = records[0].getMessage()
        self.assertNotIn("123456789012", msg)

    def test_string_args_tuple_is_redacted(self) -> None:
        logger, records = self._make_logger("args_tuple")
        logger.addFilter(RedactionFilter())
        logger.info("Email: %s", "bob@example.com")
        msg = records[0].getMessage()
        self.assertNotIn("bob@example.com", msg)
        self.assertIn("[REDACTED:EMAIL]", msg)

    def test_string_args_dict_is_redacted(self) -> None:
        logger, records = self._make_logger("args_dict")
        logger.addFilter(RedactionFilter())
        logger.info("Result: %(email)s", {"email": "carol@test.example.net"})
        msg = records[0].getMessage()
        self.assertNotIn("carol@test.example.net", msg)
        self.assertIn("[REDACTED:EMAIL]", msg)

    def test_filter_with_non_string_msg_does_not_raise(self) -> None:
        """filter() must survive non-string msg (e.g. logger.info(42))."""
        logger, records = self._make_logger("nonstring_msg")
        logger.addFilter(RedactionFilter())
        # Deliberately pass a non-string object as msg
        record = logging.LogRecord(
            name="test", level=logging.INFO,
            pathname="", lineno=0,
            msg={"key": "value"},  # dict, not str
            args=None, exc_info=None,
        )
        flt = RedactionFilter()
        # Must not raise
        result = flt.filter(record)
        self.assertTrue(result)

    def test_filter_with_odd_args_does_not_raise(self) -> None:
        """filter() must survive args that are not tuple/dict/None."""
        flt = RedactionFilter()
        # Construct a valid LogRecord first, then overwrite args post-init so
        # we bypass LogRecord.__init__'s own type check (which raises TypeError
        # on bare object() in Python 3.11).
        record = logging.LogRecord(
            name="test", level=logging.INFO,
            pathname="", lineno=0,
            msg="hello %s",
            args=None,
            exc_info=None,
        )
        record.args = object()  # inject unusual args type after construction
        result = flt.filter(record)
        self.assertTrue(result)

    def test_ordinary_message_is_not_altered(self) -> None:
        logger, records = self._make_logger("plain")
        logger.addFilter(RedactionFilter())
        logger.info("Pipeline completed 5 tasks in 200 ms.")
        self.assertEqual(records[0].getMessage(), "Pipeline completed 5 tasks in 200 ms.")

    def test_filter_always_returns_true(self) -> None:
        """Records must never be suppressed -- redaction must not drop them."""
        flt = RedactionFilter()
        record = logging.LogRecord(
            name="test", level=logging.DEBUG,
            pathname="", lineno=0,
            msg="SSN: 000-00-0000 token=abc123def456ghi",
            args=None, exc_info=None,
        )
        self.assertTrue(flt.filter(record))


class TestInstallLogRedaction(unittest.TestCase):
    """Tests for install_log_redaction() idempotency and targeting."""

    def _fresh_logger(self, name: str) -> logging.Logger:
        logger = logging.getLogger(f"test_install.{name}")
        # Remove any filters left by previous test runs in the same process
        logger.filters.clear()
        logger.propagate = False
        return logger

    def test_install_adds_exactly_one_filter(self) -> None:
        logger = self._fresh_logger("once")
        install_log_redaction(logger)
        redaction_filters = [f for f in logger.filters if isinstance(f, RedactionFilter)]
        self.assertEqual(len(redaction_filters), 1)

    def test_install_is_idempotent(self) -> None:
        logger = self._fresh_logger("twice")
        install_log_redaction(logger)
        install_log_redaction(logger)  # second call must not add another
        redaction_filters = [f for f in logger.filters if isinstance(f, RedactionFilter)]
        self.assertEqual(len(redaction_filters), 1)

    def test_install_three_times_still_one_filter(self) -> None:
        logger = self._fresh_logger("thrice")
        install_log_redaction(logger)
        install_log_redaction(logger)
        install_log_redaction(logger)
        redaction_filters = [f for f in logger.filters if isinstance(f, RedactionFilter)]
        self.assertEqual(len(redaction_filters), 1)

    def test_installed_filter_redacts_email(self) -> None:
        logger = self._fresh_logger("wired")
        logger.setLevel(logging.DEBUG)
        records: list[logging.LogRecord] = []

        class _Cap(logging.Handler):
            def emit(self, r: logging.LogRecord) -> None:
                records.append(r)

        logger.addHandler(_Cap())
        install_log_redaction(logger)
        logger.info("Contact: admin@internal.example.org")
        self.assertNotIn("admin@internal.example.org", records[0].getMessage())
        self.assertIn("[REDACTED:EMAIL]", records[0].getMessage())

    def test_install_on_root_logger_works(self) -> None:
        """install_log_redaction() with no args targets the root logger."""
        root = logging.getLogger()
        before = len([f for f in root.filters if isinstance(f, RedactionFilter)])
        install_log_redaction()  # root logger
        after = len([f for f in root.filters if isinstance(f, RedactionFilter)])
        # Must have added at most one (idempotent; may already be installed)
        self.assertGreaterEqual(after, 1)
        self.assertLessEqual(after - before, 1)


if __name__ == "__main__":
    unittest.main()
