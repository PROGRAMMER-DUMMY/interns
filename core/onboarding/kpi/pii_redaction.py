"""PII redaction helpers for *display* of executed sample-result rows.

This module exists solely to scrub PHI-like values (names, SSN, phone, email,
address, DOB, ...) from material that will be rendered to the user — for
example, the blocker question panel's embedded sample tables. It must NOT be
used to rewrite SQL, mutate upstream profile artifacts, or alter the values
that the executor sends to the warehouse. SQL execution always operates on
the unredacted source data; only the rendered surface is redacted.

The PCI cardholder-data patterns are NOT hand-copied here — they are derived at
import time from the single source of truth, ``phi_gate.PCI_IDENTIFIER_PATTERNS``,
so the enforced upstream gate and this display-time redactor can never drift
(previously they did: phi_gate added ``cid``/``magstripe``/``exp_month``/``swift``
patterns this copy lacked, so those columns were gated upstream but rendered raw).
The only ``core.*`` dependency is that pattern import; ``phi_gate`` itself depends
only on ``core.failures`` at module load, so importing it here is cheap and
acyclic.
"""

from __future__ import annotations

import re
from typing import Pattern

from core.governance.phi_gate import PCI_IDENTIFIER_PATTERNS

# Flatten the canonical PCI pattern map (category -> patterns) into the tuple
# shape this module matches against. Sorted for a stable, deterministic order.
_PCI_COLUMN_PATTERNS: tuple[str, ...] = tuple(
    sorted({p for patterns in PCI_IDENTIFIER_PATTERNS.values() for p in patterns})
)

# Display-only HIPAA-ish identifier patterns (name/contact/DOB/address). These
# scrub the most common person-identifier columns on rendered surfaces. The PCI
# subset below is single-sourced from phi_gate and appended.
_DISPLAY_HIPAA_PATTERNS: tuple[str, ...] = (
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

DEFAULT_PII_COLUMN_PATTERNS: tuple[str, ...] = _DISPLAY_HIPAA_PATTERNS + _PCI_COLUMN_PATTERNS

REDACTION_PLACEHOLDER: str = "<redacted-pii>"

# ---------------------------------------------------------------------------
# Quasi-identifiers: values that are not direct identifiers but become
# identifying at the extremes. HIPAA Safe Harbor requires ages over 89 to be
# aggregated into a single "90+" category on any rendered surface.
# ---------------------------------------------------------------------------

QUASI_IDENTIFIER_AGE_PATTERNS: tuple[str, ...] = (
    r"^age$",
    r"^age[_ ]?(years|yrs|in[_ ]?years)$",
    r"^(patient|member|subscriber|customer|person)[_ ]?age$",
)

AGE_SAFE_HARBOR_MAX: int = 89
AGE_OVERFLOW_LABEL: str = "90+"

_COMPILED_AGE: tuple[Pattern[str], ...] = tuple(
    re.compile(p, re.IGNORECASE) for p in QUASI_IDENTIFIER_AGE_PATTERNS
)


def is_age_column(column_name: str) -> bool:
    """Return True if ``column_name`` looks like a person-age column."""
    if not isinstance(column_name, str) or not column_name:
        return False
    return any(regex.match(column_name) for regex in _COMPILED_AGE)


def bucket_age_value(value: object) -> object:
    """Return ``value`` unchanged unless it is a numeric age > 89, which is
    rendered as the Safe Harbor overflow label ("90+"). Non-numeric values and
    None pass through untouched.
    """
    if isinstance(value, bool) or value is None:
        return value
    if isinstance(value, (int, float)):
        return AGE_OVERFLOW_LABEL if value > AGE_SAFE_HARBOR_MAX else value
    if isinstance(value, str):
        try:
            numeric = float(value.strip())
        except (ValueError, AttributeError):
            return value
        return AGE_OVERFLOW_LABEL if numeric > AGE_SAFE_HARBOR_MAX else value
    return value


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

    if is_pii_column(column_name, patterns=patterns):
        return [None if v is None else placeholder for v in values]
    if is_age_column(column_name):
        return [bucket_age_value(v) for v in values]
    return list(values)


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
        elif isinstance(key, str) and is_age_column(key):
            redacted[key] = bucket_age_value(value)
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


def workspace_redaction_patterns(workspace_path: object) -> tuple[str, ...]:
    """Effective display-redaction patterns for a workspace: the built-in
    defaults PLUS any user-authored ``data_policy.json`` declared patterns.

    The data policy can only ADD coverage on rendered surfaces — its allowlist
    never narrows what is hidden when displaying values. Loading the policy must
    never break a render, so failures fall back to the defaults.
    """
    if workspace_path is None:
        return DEFAULT_PII_COLUMN_PATTERNS
    try:
        from core.governance.data_policy import (
            load_workspace_data_policy,
            policy_redaction_patterns,
        )

        extras = policy_redaction_patterns(load_workspace_data_policy(workspace_path))
    except Exception:
        extras = ()
    if not extras:
        return DEFAULT_PII_COLUMN_PATTERNS
    return DEFAULT_PII_COLUMN_PATTERNS + tuple(extras)


def redact_table_rows(
    columns: list,
    rows: list,
    *,
    patterns: tuple[str, ...] = DEFAULT_PII_COLUMN_PATTERNS,
    placeholder: str = REDACTION_PLACEHOLDER,
) -> list[list]:
    """Redact row-major rows (tuples/lists) by column for tabular DISPLAY.

    For each column index: PII columns -> ``placeholder`` (None preserved), age
    columns -> Safe-Harbor bucketed. Non-sensitive columns pass through. Returns
    new row lists; never mutates the input. Used by the result packet and the
    verifier sample tables so the highest-traffic display surfaces redact too.
    """
    col_names = [str(c) for c in columns]
    pii_flags = [is_pii_column(c, patterns=patterns) for c in col_names]
    age_flags = [is_age_column(c) for c in col_names]
    out: list[list] = []
    for row in rows:
        new_row = list(row)
        for idx in range(min(len(new_row), len(col_names))):
            if pii_flags[idx]:
                new_row[idx] = None if new_row[idx] is None else placeholder
            elif age_flags[idx]:
                new_row[idx] = bucket_age_value(new_row[idx])
        out.append(new_row)
    return out


__all__ = [
    "AGE_OVERFLOW_LABEL",
    "AGE_SAFE_HARBOR_MAX",
    "DEFAULT_PII_COLUMN_PATTERNS",
    "QUASI_IDENTIFIER_AGE_PATTERNS",
    "REDACTION_PLACEHOLDER",
    "bucket_age_value",
    "is_age_column",
    "is_pii_column",
    "redact_row_dict",
    "redact_rows",
    "redact_sample_values",
    "redact_table_rows",
    "workspace_redaction_patterns",
]
