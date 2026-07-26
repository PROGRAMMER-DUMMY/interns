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


# ── P3b: medallion emitters validate identifiers / expressions ───────────────
class MedallionEmitterValidationTests(unittest.TestCase):
    def _silver_table(self, pk):
        from core.medallion.manifest import SilverTable

        return SilverTable(
            name="patients",
            derived_from=["bronze.patients__hospital_a"],
            primary_key=pk,
        )

    def test_silver_spark_rejects_hostile_pk(self) -> None:
        import tempfile

        from core.medallion.delta_emitter import emit_silver_spark
        from core.medallion.silver_contract import SilverContract
        from core.sql_safety import UnsafeIdentifierError

        with tempfile.TemporaryDirectory() as tmp:
            table = self._silver_table(['PatientID); DROP TABLE x; --'])
            contract = SilverContract(workspace="demo", tables={})
            with self.assertRaises(UnsafeIdentifierError):
                emit_silver_spark(table, Path(tmp), "demo-workspace", contract)

    def test_silver_spark_accepts_valid_pk(self) -> None:
        import tempfile

        from core.medallion.delta_emitter import emit_silver_spark
        from core.medallion.silver_contract import SilverContract

        with tempfile.TemporaryDirectory() as tmp:
            table = self._silver_table(["PatientID"])
            contract = SilverContract(workspace="demo", tables={})
            path = emit_silver_spark(table, Path(tmp), "demo-workspace", contract)
            self.assertIn("tgt.PatientID = src.PatientID", path.read_text(encoding="utf-8"))

    def test_merge_emitter_rejects_hostile_pk(self) -> None:
        from core.medallion.merge_emitter import emit_silver_merge
        from core.sql_safety import UnsafeIdentifierError

        p0 = "CREATE OR REPLACE TABLE silver.x AS SELECT 1 AS a;"
        with self.assertRaises(UnsafeIdentifierError):
            emit_silver_merge("x", ['a) IN (SELECT 1); DROP TABLE y; --'], p0)

    def test_merge_emitter_accepts_valid(self) -> None:
        from core.medallion.merge_emitter import emit_silver_merge

        p0 = "CREATE OR REPLACE TABLE silver.patients AS SELECT 1 AS PatientID;"
        out = emit_silver_merge("patients", ["PatientID"], p0)
        self.assertIn("DELETE FROM silver.patients", out)
        # The PK is emitted as a QUOTED identifier -- that quoting is the injection
        # hardening this suite exists to protect, so assert the quoted form.
        self.assertIn('("PatientID") IN', out)


# ── P3c: KPI generator filter value/op + formula sanitization ────────────────
class FilterValueRenderingTests(unittest.TestCase):
    def test_string_value_is_repr_escaped(self) -> None:
        from core.sql_safety import render_python_scalar_literal

        # An embedded quote/paren can no longer break out of pl.col(...)==<value>.
        hostile = 'a") | (pl.col("x").is_not_null()) | ("b'
        rendered = render_python_scalar_literal(hostile, treat_as_string=True)
        # repr() produces a single safe Python string literal.
        self.assertEqual(eval(rendered), hostile)  # noqa: S307 - validating repr round-trip
        self.assertTrue(rendered.startswith(("'", '"')))

    def test_numeric_value_emitted_bare(self) -> None:
        from core.sql_safety import render_python_scalar_literal

        self.assertEqual(render_python_scalar_literal("42", treat_as_string=False), "42")
        self.assertEqual(render_python_scalar_literal("-3.5", treat_as_string=False), "-3.5")

    def test_non_numeric_flagged_numeric_falls_back_to_quoted(self) -> None:
        from core.sql_safety import render_python_scalar_literal

        # A non-number that claims to be numeric must NOT be emitted raw.
        out = render_python_scalar_literal("1); evil()", treat_as_string=False)
        self.assertNotEqual(out, "1); evil()")
        self.assertEqual(eval(out), "1); evil()")  # noqa: S307

    def test_op_mapping_defaults_safely(self) -> None:
        from core.sql_safety import map_comparison_op

        table = {"=": "==", ">": ">"}
        self.assertEqual(map_comparison_op("=", table), "==")
        # A hostile op collapses to the safe default instead of injecting.
        self.assertEqual(map_comparison_op(") | evil() | (", table), "==")


class GeneratorFilterEmissionTests(unittest.TestCase):
    """End-to-end: a hostile filter value yields a safe, parseable script."""

    def _intent_with_filter(self, value, *, is_literal, target="dept", op="="):
        from core.onboarding.kpi.kpi_intent import FilterIntent, KPIIntent

        intent = KPIIntent.__new__(KPIIntent)
        # Minimal duck-typed intent: _filter_exprs only touches .filters and .dims
        object.__setattr__(intent, "filters", [
            FilterIntent(target=target, op=op, value=value, is_literal=is_literal)
        ])
        object.__setattr__(intent, "dims", [])
        return intent

    def test_polars_filter_value_is_escaped(self) -> None:
        import ast

        from core.onboarding.kpi.polars_generator import PolarsKPIGenerator

        gen = PolarsKPIGenerator.__new__(PolarsKPIGenerator)
        hostile = 'a") | (1==1) | ("b'
        intent = self._intent_with_filter(hostile, is_literal=True)
        exprs = gen._filter_exprs(intent)
        self.assertEqual(len(exprs), 1)
        # Structurally: the expression is a single comparison, NOT an injected
        # boolean OR. The hostile text survives only as a string constant.
        node = ast.parse(exprs[0], mode="eval").body
        self.assertIsInstance(node, ast.Compare)
        consts = [n.value for n in ast.walk(node) if isinstance(n, ast.Constant)]
        self.assertIn(hostile, consts)

    def test_pyspark_filter_value_is_escaped(self) -> None:
        import ast

        from core.onboarding.kpi.pyspark_generator import PySparkKPIGenerator

        gen = PySparkKPIGenerator.__new__(PySparkKPIGenerator)
        hostile = 'a") | (1==1) | ("b'
        intent = self._intent_with_filter(hostile, is_literal=True)
        lines = gen._filter_lines(intent)
        self.assertEqual(len(lines), 1)
        # The .filter() argument must be a single comparison with the hostile
        # text confined to a string constant (no injected boolean structure).
        call = ast.parse(lines[0]).body[0].value  # result.filter(<arg>)
        self.assertIsInstance(call, ast.Call)
        arg = call.args[0]
        self.assertIsInstance(arg, ast.Compare)
        consts = [n.value for n in ast.walk(arg) if isinstance(n, ast.Constant)]
        self.assertIn(hostile, consts)


class DerivedFormulaGuardTests(unittest.TestCase):
    def test_sql_generator_rejects_injection_formula(self) -> None:
        from core.onboarding.kpi import sql_generator

        src = Path(sql_generator.__file__).read_text(encoding="utf-8")
        self.assertIn("validate_expression_safe", src)


if __name__ == "__main__":
    unittest.main()
