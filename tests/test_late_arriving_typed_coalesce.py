"""Typed unknown-member fallback for late-arriving dimensions.

The dimension-side COALESCE used to cast every attribute to string, so an
early-arriving fact kept its row but a numeric dimension attribute came out as
text -- which silently breaks numeric comparison and ordering downstream. The
fallback is now typed from the column's profiled dtype.

Two cases are deliberately NOT typed, and both are tested as such: a boolean
(there is no honest "unknown" boolean, so the NULL stands) and a column with no
profile evidence (keeps the previous string form rather than guessing).
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from core.onboarding.kpi.dbt_project_generator import (  # noqa: E402
    _late_arriving_expr,
    _unknown_member_literal,
)

SOURCE = "workspaces/x/dim.csv"
ALIASES = {SOURCE: "s1"}
PROFILES = {
    SOURCE: {
        "schema": {
            "PayerName": "String",
            "ClaimCount": "Int64",
            "SmallCount": "Int8",
            "Unsigned": "UInt32",
            "Rate": "Float64",
            "Money": "Decimal(18, 2)",
            "AdmitTs": "Datetime(time_unit='us', time_zone=None)",
            "BirthDate": "Date",
            "IsActive": "Boolean",
            "StartTime": "Time",
            "Tags": "List(String)",
        }
    }
}


def expr_for(column: str) -> str:
    return _late_arriving_expr(f's1."{column}"', ALIASES, PROFILES)


class TypedUnknownMemberTest(unittest.TestCase):
    def test_numeric_attributes_fall_back_to_minus_one(self) -> None:
        for column in ("ClaimCount", "SmallCount", "Unsigned", "Rate", "Money"):
            with self.subTest(column=column):
                rendered = expr_for(column)
                self.assertIn("-1", rendered)
                self.assertNotIn("as string", rendered)

    def test_timestamp_attributes_fall_back_to_the_epoch(self) -> None:
        self.assertEqual(
            expr_for("AdmitTs"),
            "coalesce(s1.\"AdmitTs\", timestamp('1970-01-01 00:00:00'))",
        )

    def test_date_attribute_stays_a_date_not_a_timestamp(self) -> None:
        rendered = expr_for("BirthDate")
        self.assertIn("date '1970-01-01'", rendered)
        self.assertNotIn("timestamp(", rendered)

    def test_text_attributes_keep_the_string_sentinel(self) -> None:
        rendered = expr_for("PayerName")
        self.assertIn("__unknown_member__", rendered)

    def test_boolean_attribute_is_left_null_on_purpose(self) -> None:
        # Substituting false would assert something the data never said.
        self.assertEqual(expr_for("IsActive"), 's1."IsActive"')

    def test_time_and_nested_types_use_the_string_sentinel(self) -> None:
        for column in ("StartTime", "Tags"):
            with self.subTest(column=column):
                self.assertIn("__unknown_member__", expr_for(column))

    def test_unprofiled_column_keeps_previous_behaviour(self) -> None:
        # No profile evidence -> do not guess a type.
        self.assertEqual(
            _late_arriving_expr('s1."Whatever"', {}, {}),
            "coalesce(cast(s1.\"Whatever\" as string), '__unknown_member__')",
        )

    def test_fact_side_expression_is_never_coalesced(self) -> None:
        # s0 is the base/fact alias -- coalescing a measure would corrupt it.
        self.assertEqual(_late_arriving_expr('s0."Amount"', ALIASES, PROFILES), 's0."Amount"')

    def test_non_qualified_expression_is_untouched(self) -> None:
        self.assertEqual(_late_arriving_expr("count(*)", ALIASES, PROFILES), "count(*)")

    def test_backtick_quoting_resolves_the_dtype(self) -> None:
        # dbt-databricks quotes with backticks; the dtype lookup must still hit.
        self.assertIn("-1", _late_arriving_expr("s1.`ClaimCount`", ALIASES, PROFILES))

    def test_literal_helper_contract(self) -> None:
        self.assertEqual(_unknown_member_literal("Int64"), "-1")
        self.assertIsNone(_unknown_member_literal("Boolean"))
        self.assertIsNone(_unknown_member_literal(""))
        self.assertIsNone(_unknown_member_literal(None))


if __name__ == "__main__":
    unittest.main()
