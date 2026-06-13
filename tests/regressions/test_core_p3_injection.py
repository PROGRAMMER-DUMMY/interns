"""Regression tests for core remediation P3 — injection into emitted artifacts.

Locks down theme T4: workspace-owned formula bodies, filter values, PII
expressions, and PK/column names must be escaped/validated/parameterized before
they reach emitted SQL, F.expr(...) PySpark, or a Delta INSERT. Workspace-agnostic.
"""
from __future__ import annotations

import unittest
from pathlib import Path


# ── Shared safety layer ──────────────────────────────────────────────────────
class SqlSafetyTests(unittest.TestCase):
    def test_identifier_validation(self) -> None:
        from core.sql_safety import (
            UnsafeIdentifierError,
            assert_safe_identifier,
            is_safe_identifier,
        )

        for good in ("amount", "Patient_Id", "_x", "col1"):
            self.assertTrue(is_safe_identifier(good), good)
        for bad in ('a";DROP TABLE t;--', "a b", "1col", "a-b", "", None, "a)"):
            self.assertFalse(is_safe_identifier(bad), repr(bad))
            with self.assertRaises(UnsafeIdentifierError):
                assert_safe_identifier(bad)

    def test_quoting_escapes_embedded_delimiters(self) -> None:
        from core.sql_safety import (
            escape_sql_literal,
            quote_ident_backtick,
            quote_ident_sql,
        )

        self.assertEqual(quote_ident_sql('a"b'), '"a""b"')
        self.assertEqual(quote_ident_backtick("a`b"), "`a``b`")
        self.assertEqual(escape_sql_literal("O'Brien"), "'O''Brien'")

    def test_expression_guard_blocks_injection(self) -> None:
        from core.sql_safety import (
            UnsafeExpressionError,
            is_expression_safe,
            validate_expression_safe,
        )

        # Legitimate scalar expressions pass.
        for ok in ("amount * 1.0", "COALESCE(a, 0) / NULLIF(b, 0)", "CASE WHEN x > 0 THEN 1 ELSE 0 END"):
            self.assertTrue(is_expression_safe(ok), ok)
            validate_expression_safe(ok)

        # Injection attempts (statement terminators / comments / DDL+DML) rejected.
        for bad in (
            "1); DROP TABLE patients; --",
            "x /* comment */ + 1",
            "a; delete from t",
            "1; insert into t values (1)",
            "x -- drop",
        ):
            self.assertFalse(is_expression_safe(bad), bad)


# ── P3a: write_delta is parameterized (no f-string JSON injection) ───────────
class WriteDeltaParameterizationTests(unittest.TestCase):
    def test_write_delta_uses_bound_parameter_not_fstring_json(self) -> None:
        from core.execution import databricks_client

        src = Path(databricks_client.__file__).read_text(encoding="utf-8")
        # The old injection: from_json('{rows_json}', ...) must be gone.
        self.assertNotIn("from_json('", src)
        # The fix: bound parameter :rows + parameters=[...].
        self.assertIn(":rows", src)
        self.assertIn("parameters=", src)
        self.assertIn("assert_safe_identifier", src)


if __name__ == "__main__":
    unittest.main()
