"""Shared SQL / identifier safety helpers for *emitted* artifacts.

Workspace-owned text (formula templates, filter values, PII expressions, PK and
column names) is interpolated into generated SQL, ``F.expr("...")`` PySpark, and
Delta INSERT statements that are later executed. Expression *evaluation* in this
repo is already eval/exec-free, but the emitted *text* is an injection surface.
This module centralizes the escaping/quoting/parameterization + validation so
every emitter uses the same vetted layer. Ref: core-audit theme T4 (P3).

Dependency-free (stdlib only) so any layer can import it without cycles.
"""
from __future__ import annotations

import re

# A bare, unquoted SQL/identifier token: starts with a letter/underscore, then
# letters/digits/underscores. Anything else must be quoted or rejected.
_IDENT_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")

# Statement-injection markers that must never appear inside an inlined scalar
# expression / formula body (these turn one expression into many statements or a
# comment-out attack). Conservative denylist layered on top of structural checks.
_DANGEROUS_SUBSTRINGS: tuple[str, ...] = (";", "--", "/*", "*/")

# Whole-word DDL/DML/command keywords that have no place in a scalar formula.
_DANGEROUS_KEYWORDS: frozenset[str] = frozenset(
    {
        "drop", "delete", "insert", "update", "alter", "create", "truncate",
        "grant", "revoke", "attach", "detach", "copy", "pragma", "exec",
        "execute", "merge", "call", "vacuum", "install", "load",
    }
)


_NUMERIC_RE = re.compile(r"-?\d+(?:\.\d+)?$")


class UnsafeIdentifierError(ValueError):
    """Raised when an identifier fails the safe-identifier check."""


class UnsafeExpressionError(ValueError):
    """Raised when an inlined expression/formula body looks like injection."""


def is_safe_identifier(name: object) -> bool:
    """True if ``name`` is a bare, safe SQL/Python identifier."""
    return isinstance(name, str) and bool(_IDENT_RE.match(name))


def assert_safe_identifier(name: object, *, context: str = "identifier") -> str:
    """Return ``name`` if it is a safe identifier, else raise UnsafeIdentifierError."""
    if not is_safe_identifier(name):
        raise UnsafeIdentifierError(f"unsafe {context}: {name!r}")
    return str(name)


def quote_ident_sql(name: object) -> str:
    """Double-quote a SQL identifier (ANSI/DuckDB), escaping embedded quotes."""
    return '"' + str(name).replace('"', '""') + '"'


def quote_ident_backtick(name: object) -> str:
    """Backtick-quote a Spark/Databricks identifier, escaping embedded backticks."""
    return "`" + str(name).replace("`", "``") + "`"


def escape_sql_literal(value: object) -> str:
    """Render ``value`` as a single-quoted SQL string literal, escaping quotes."""
    return "'" + str(value).replace("'", "''") + "'"


def validate_expression_safe(expression: object, *, context: str = "expression") -> str:
    """Return ``expression`` if it has no statement-injection markers, else raise.

    Conservative guard for scalar expression / derived-formula bodies that get
    inlined into emitted SQL or ``F.expr(...)``: rejects statement terminators,
    comment sequences, and standalone DDL/DML keywords. It does NOT attempt to be
    a full SQL parser — it blocks the injection classes the audit found while
    leaving legitimate arithmetic/function expressions untouched.
    """
    if not isinstance(expression, str):
        raise UnsafeExpressionError(f"unsafe {context}: not a string ({expression!r})")
    for marker in _DANGEROUS_SUBSTRINGS:
        if marker in expression:
            raise UnsafeExpressionError(
                f"unsafe {context}: contains injection marker {marker!r}"
            )
    lowered_words = set(re.findall(r"[A-Za-z_]+", expression.lower()))
    bad = lowered_words & _DANGEROUS_KEYWORDS
    if bad:
        raise UnsafeExpressionError(
            f"unsafe {context}: contains disallowed keyword(s) {sorted(bad)}"
        )
    return expression


def is_expression_safe(expression: object) -> bool:
    """Non-raising form of :func:`validate_expression_safe`."""
    try:
        validate_expression_safe(expression)
        return True
    except UnsafeExpressionError:
        return False


def render_python_scalar_literal(value: object, *, treat_as_string: bool) -> str:
    """Render a filter value as a SAFE Python literal for emitted Polars/PySpark.

    Both generators emit Python source, so ``repr()`` is the correct escape for a
    string value (an embedded quote/backslash/paren can no longer break out of
    the expression — the prior ``f'"{value}"'`` was the injection surface). A
    value flagged numeric is emitted bare only if it really matches a number;
    anything else falls back to a quoted string literal rather than raw text.
    Ref: core-audit ob-kpi-b.md (theme T4).
    """
    text = str(value)
    if not treat_as_string and _NUMERIC_RE.match(text.strip()):
        return text.strip()
    return repr(text)


def map_comparison_op(op: object, op_table: dict, *, default: str = "==") -> str:
    """Map a comparison operator through an engine op-table, defaulting safely.

    Ensures an operator from workspace text can never be interpolated raw into
    emitted code (the age-filter branch did exactly that): an unknown/hostile op
    collapses to ``default`` instead of injecting tokens.
    """
    return op_table.get(str(op), default)


__all__ = [
    "UnsafeExpressionError",
    "UnsafeIdentifierError",
    "assert_safe_identifier",
    "escape_sql_literal",
    "is_expression_safe",
    "is_safe_identifier",
    "map_comparison_op",
    "quote_ident_backtick",
    "quote_ident_sql",
    "render_python_scalar_literal",
    "validate_expression_safe",
]
