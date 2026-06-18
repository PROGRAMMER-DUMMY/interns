"""Parsing a KPI's intrinsic scope from its generated SQL, + range filters."""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

import polars as pl

from core.dashboard.kpi_scope import scope_from_sql

_VENDOR = str((Path(__file__).resolve().parents[1] / "vendor"))
if _VENDOR not in sys.path:
    sys.path.insert(0, _VENDOR)

from minus.query.engine import _apply_filters  # noqa: E402

_COLS = ["PayorID", "LineOfBusiness", "Gender", "PaidAmount", "age", "month",
         "ServiceDate", "DOB"]


def _sql(where: str, select_extra: str = "") -> str:
    sel = '"LineOfBusiness" AS lineofbusiness' + (", " + select_extra if select_extra else "")
    return f'''
CREATE OR REPLACE VIEW "kpi_x_features" AS SELECT * FROM raw;
CREATE OR REPLACE VIEW "kpi_x_results" AS
SELECT {sel}, ROUND(SUM("PaidAmount"), 2) AS sum_paidamount
FROM "kpi_x_features"
WHERE {where}
GROUP BY "LineOfBusiness"
ORDER BY sum_paidamount DESC;
'''


class TestScopeFromSql(unittest.TestCase):
    def test_equality_predicate(self):
        s = scope_from_sql(_sql("\"LineOfBusiness\" = 'Commercial'"), _COLS)
        self.assertEqual(s, {"LineOfBusiness": "Commercial"})

    def test_equality_and_range_with_expression_alias(self):
        # The range is on a derived expression aliased to `age` in the SELECT.
        sql = _sql(
            "\"LineOfBusiness\" = 'Medicare' AND date_diff('year', CAST(\"DOB\" AS DATE), "
            "CAST(\"ServiceDate\" AS DATE)) > 50",
            select_extra="date_diff('year', CAST(\"DOB\" AS DATE), CAST(\"ServiceDate\" AS DATE)) AS age",
        )
        self.assertEqual(scope_from_sql(sql, _COLS),
                         {"LineOfBusiness": "Medicare", "age": {"gt": 50}})

    def test_no_where_is_empty_scope(self):
        sql = '''CREATE OR REPLACE VIEW "kpi_x_results" AS
SELECT "LineOfBusiness" AS lineofbusiness FROM "kpi_x_features" GROUP BY "LineOfBusiness";'''
        self.assertEqual(scope_from_sql(sql, _COLS), {})

    def test_top_level_or_bails(self):
        s = scope_from_sql(_sql("\"LineOfBusiness\" = 'Commercial' OR \"Gender\" = 'F'"), _COLS)
        self.assertIsNone(s)

    def test_unmappable_column_bails(self):
        s = scope_from_sql(_sql("\"NotAColumn\" = 'x'"), _COLS)
        self.assertIsNone(s)

    def test_no_results_view_returns_none(self):
        self.assertIsNone(scope_from_sql("SELECT 1;", _COLS))


class TestRangeFilters(unittest.TestCase):
    def _df(self):
        return pl.DataFrame({"age": [40, 50, 51, 60], "v": [1, 2, 3, 4]})

    def test_gt_is_strict(self):
        out = _apply_filters(self._df(), {"age": {"gt": 50}})
        self.assertEqual(out.get_column("age").to_list(), [51, 60])

    def test_from_is_inclusive(self):
        out = _apply_filters(self._df(), {"age": {"from": 50}})
        self.assertEqual(out.get_column("age").to_list(), [50, 51, 60])

    def test_lt_is_strict(self):
        out = _apply_filters(self._df(), {"age": {"lt": 51}})
        self.assertEqual(out.get_column("age").to_list(), [40, 50])


if __name__ == "__main__":
    unittest.main()
