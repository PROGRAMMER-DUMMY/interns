"""PII redaction helpers for *display* of executed sample-result rows.

This module exists solely to scrub PHI-like values (names, SSN, phone, email,
address, DOB, ...) from material that will be rendered to the user — for
example, the blocker question panel's embedded sample tables. It must NOT be
used to rewrite SQL, mutate upstream profile artifacts, or alter the values
that the executor sends to the warehouse. SQL execution always operates on
the unredacted source data; only the rendered surface is redacted.

Dependency-free by design: stdlib only, no imports from ``core.*``.
"""

from __future__ import annotations

import re
from typing import Pattern


DEFAULT_PII_COLUMN_PATTERNS: tuple[str, ...] = (
    # Case-insensitive regex patterns matched against column names.
    # Anchored with ^ and $ so only exact column names are redacted —
    # ``Patient_FirstName`` is intentionally NOT matched by ``^first_name$``.
    r"^ssn$",
    r"^first[_ ]?name$",
    r"^last[_ ]?name$",
    r"^middle[_ ]?name$",
    r"^full[_ ]?name$",
    r"^name$",
    r"^phone([_ ]?number)?$",
    r"^email([_ ]?address)?$",
    r"^address(_?line\d*)?$",
    r"^street$",
    r"^zip([_ ]?code)?$",
    r"^postal([_ ]?code)?$",
    r"^dob$",
    r"^date[_ ]?of[_ ]?birth$",
    r"^birth[_ ]?date$",
)

REDACTION_PLACEHOLDER: str = "<redacted-pii>"


_COMPILED_DEFAULT: tuple[Pattern[str], ...] = tuple(
    re.compile(p, re.IGNORECASE) for p in DEFAULT_PII_COLUMN_PATTERNS
)


# Cache of compiled patterns for custom tuples, so callers passing the same
# ``patterns=`` tuple repeatedly don't re-compile on every call.
_COMPILED_CACHE: dict[tuple[str, ...], tuple[Pattern[str], ...]] = {
    DEFAULT_PII_COLUMN_PATTERNS: _COMPILED_DEFAULT,
}


def _compile(patterns: tuple[str, ...]) -> tuple[Pattern[str], ...]:
    cached = _COMPILED_CACHE.get(patterns)
    if cached is not None:
        return cached
    compiled = tuple(re.compile(p, re.IGNORECASE) for p in patterns)
    _COMPILED_CACHE[patterns] = compiled
    return compiled


def is_pii_column(
    column_name: str,
    *,
    patterns: tuple[str, ...] = DEFAULT_PII_COLUMN_PATTERNS,
) -> bool:
    """Return True if ``column_name`` matches any PII pattern (case-insensitive)."""

    if not isinstance(column_name, str) or not column_name:
        return False
    compiled = _compile(patterns)
    for regex in compiled:
        if regex.match(column_name):
            return True
    return False


def redact_sample_values(
    column_name: str,
    values: list,
    *,
    patterns: tuple[str, ...] = DEFAULT_PII_COLUMN_PATTERNS,
    placeholder: str = REDACTION_PLACEHOLDER,
) -> list:
    """If ``column_name`` is PII, replace each value with ``placeholder``; else copy.

    Always returns a new list; never mutates the input. ``None`` values are
    preserved so downstream null-rate analytics stay meaningful.
    """

    if not is_pii_column(column_name, patterns=patterns):
        return list(values)
    return [None if v is None else placeholder for v in values]


def redact_row_dict(
    row: dict[str, object],
    *,
    patterns: tuple[str, ...] = DEFAULT_PII_COLUMN_PATTERNS,
    placeholder: str = REDACTION_PLACEHOLDER,
) -> dict[str, object]:
    """Return a new dict with PII columns' values replaced by ``placeholder``.

    Non-PII keys pass through unchanged. ``None`` values are preserved even in
    PII columns. The input dict is never mutated.
    """

    redacted: dict[str, object] = {}
    for key, value in row.items():
        if isinstance(key, str) and is_pii_column(key, patterns=patterns):
            redacted[key] = None if value is None else placeholder
        else:
            redacted[key] = value
    return redacted


def redact_rows(
    rows: list[dict[str, object]],
    *,
    patterns: tuple[str, ...] = DEFAULT_PII_COLUMN_PATTERNS,
    placeholder: str = REDACTION_PLACEHOLDER,
) -> list[dict[str, object]]:
    """Apply :func:`redact_row_dict` to each row. Returns a new list."""

    return [
        redact_row_dict(row, patterns=patterns, placeholder=placeholder)
        for row in rows
    ]


__all__ = [
    "DEFAULT_PII_COLUMN_PATTERNS",
    "REDACTION_PLACEHOLDER",
    "is_pii_column",
    "redact_row_dict",
    "redact_rows",
    "redact_sample_values",
]
