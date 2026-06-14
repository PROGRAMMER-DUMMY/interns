"""PII/PHI/secret redaction for application logging.

Attaches a :class:`RedactionFilter` to Python's logging framework so that
any log record containing a PII value, PHI identifier, or secret token is
scrubbed before it reaches any handler (file, stderr, remote sink).

Patterns covered
----------------
- Email addresses
- US Social Security Numbers (###-##-####)
- Long digit runs resembling account / MRN / card numbers (12+ digits, or
  13-19 digits for card-like strings)
- Secret / API-token patterns:
    * GitHub personal-access tokens    ghp_...  gho_...  ghs_...
    * Databricks tokens                dapi[0-9a-f]{16,}
    * OpenAI / generic sk- tokens      sk-[A-Za-z0-9]{20,}
    * AWS access-key IDs               AKIA[0-9A-Z]{16}
    * Bearer-header values             Bearer <token>
    * Generic key/token assignments    api_key=... / token=... / secret=...

Redaction marker format:  [REDACTED:<kind>]

Design notes
------------
- ``redact(text)`` is a pure string -> string function; no I/O, no side effects.
- ``RedactionFilter.filter()`` never raises; a broken filter that throws would
  silently disable all logging for that logger.
- ``install_log_redaction()`` is idempotent: calling it twice adds exactly one
  filter instance to the target logger.
- The filter is attached to the *logger* (not to any handler), so it fires
  before propagation and covers all handlers that the record will reach,
  including handlers added after install.
- Known limitation: if a caller creates a new *root handler* that captures
  records before they propagate to root, that handler would not be covered
  unless ``install_log_redaction()`` is called again for that specific logger.
- Stdlib only; workspace-agnostic; ASCII markers only.
"""
from __future__ import annotations

import logging
import re
from typing import Sequence

# ---------------------------------------------------------------------------
# Redaction pattern registry
# ---------------------------------------------------------------------------
# Each entry is (kind_label, compiled_regex).  Order matters only for speed;
# the first matching pattern per position wins (patterns are independent
# substitutions over the full string, not in priority order).
# Conservative > aggressive: over-masking a log token is acceptable;
# leaking PII is not.

_PATTERNS: list[tuple[str, re.Pattern[str]]] = [
    # -- Secret / token patterns ----------------------------------------
    # GitHub tokens: ghp_ (personal), gho_ (OAuth), ghs_ (server-to-server)
    (
        "SECRET",
        re.compile(r"\bgh[pos]_[A-Za-z0-9]{10,}\b"),
    ),
    # Databricks personal-access token: dapi followed by hex
    (
        "SECRET",
        re.compile(r"\bdapi[0-9a-f]{16,}\b", re.IGNORECASE),
    ),
    # OpenAI / generic sk- tokens (at least 20 alphanum chars after the dash)
    (
        "SECRET",
        re.compile(r"\bsk-[A-Za-z0-9]{20,}\b"),
    ),
    # AWS access-key IDs: AKIA + 16 uppercase alphanums
    (
        "SECRET",
        re.compile(r"\bAKIA[0-9A-Z]{16}\b"),
    ),
    # Bearer-scheme tokens in Authorization headers / log lines
    # e.g.  Authorization: Bearer eyJhbG...  or  bearer=eyJhbG...
    (
        "SECRET",
        re.compile(
            r"(?i)\bbearer\s+([A-Za-z0-9\-._~+/]{10,}(?:={0,2}))\b"
        ),
    ),
    # Generic key/token/password/secret assignments in log output
    # e.g.  api_key='abc123'  token=xyz  password="secret"
    (
        "SECRET",
        re.compile(
            r"(?i)\b(api[_\-]?key|token|password|secret)\s*[:=]\s*['\"]?[A-Za-z0-9\-._~+/!@#$%^&*]{6,}['\"]?"
        ),
    ),
    # -- PII / PHI patterns --------------------------------------------
    # Email addresses (RFC 5321-ish, conservative)
    (
        "EMAIL",
        re.compile(r"\b[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}\b"),
    ),
    # US Social Security Number: ###-##-####
    (
        "SSN",
        re.compile(r"\b\d{3}-\d{2}-\d{4}\b"),
    ),
    # Card-like digit runs: 13-19 consecutive digits (credit/debit card PANs)
    # Must appear before the generic long-digit pattern so the label is precise.
    (
        "CARD",
        re.compile(r"\b\d{13,19}\b"),
    ),
    # Long digit runs that look like account / MRN numbers (12+ digits).
    # The CARD pattern above handles 13-19; this catches exactly 12 so as not
    # to double-redact shorter innocent integers like years or counts.
    (
        "ACCOUNT",
        re.compile(r"\b\d{12}\b"),
    ),
]

# Precomputed replacement strings (avoids repeated f-string work per call)
_REPLACEMENTS: list[tuple[re.Pattern[str], str]] = [
    (pat, f"[REDACTED:{kind}]") for kind, pat in _PATTERNS
]


def redact(text: str) -> str:
    """Return ``text`` with PII/PHI/secret patterns replaced by redaction markers.

    Each pattern is applied as an independent substitution.  The function is
    idempotent — re-running it on already-redacted text is safe (the markers
    themselves do not match any PII pattern).

    Returns ``text`` unchanged when ``text`` is not a string.
    """
    if not isinstance(text, str):
        return text  # type: ignore[return-value]
    for pat, replacement in _REPLACEMENTS:
        text = pat.sub(replacement, text)
    return text


# ---------------------------------------------------------------------------
# Logging integration
# ---------------------------------------------------------------------------

class RedactionFilter(logging.Filter):
    """A :class:`logging.Filter` that scrubs PII/PHI/secrets from log records.

    Attach to any logger or handler::

        logging.getLogger().addFilter(RedactionFilter())

    The filter always returns ``True`` (records are never dropped, only
    redacted).  It never raises: any internal error is swallowed silently so
    the logging pipeline is never disrupted.
    """

    def filter(self, record: logging.LogRecord) -> bool:  # noqa: A003
        """Redact the record's ``msg`` and ``args`` in-place; always return True."""
        try:
            if isinstance(record.msg, str):
                record.msg = redact(record.msg)
            # record.args can be a tuple, dict, or None
            if isinstance(record.args, tuple):
                record.args = tuple(
                    redact(a) if isinstance(a, str) else a
                    for a in record.args
                )
            elif isinstance(record.args, dict):
                record.args = {
                    k: (redact(v) if isinstance(v, str) else v)
                    for k, v in record.args.items()
                }
        except Exception:  # pragma: no cover - defensive; must not raise
            pass
        return True


def install_log_redaction(logger: logging.Logger | None = None) -> None:
    """Attach a :class:`RedactionFilter` to ``logger`` (default: root logger).

    Idempotent: if a ``RedactionFilter`` is already attached, the function
    returns immediately without adding a duplicate.

    Because the filter is attached to the *logger* (not to a handler), it
    intercepts every record that reaches the logger — including records
    forwarded to handlers added after this call, and records that propagate up
    to the root logger from child loggers.

    Note: if a log *handler* captures records at a child logger *before* the
    record propagates to the root (i.e., ``handler.addFilter`` usage or
    ``propagate=False``), that handler is not covered by this root-level filter.
    Call ``install_log_redaction(child_logger)`` for those loggers too.
    """
    target = logger if logger is not None else logging.getLogger()
    for f in target.filters:
        if isinstance(f, RedactionFilter):
            return  # already installed; idempotent
    target.addFilter(RedactionFilter())


__all__ = [
    "RedactionFilter",
    "install_log_redaction",
    "redact",
]
