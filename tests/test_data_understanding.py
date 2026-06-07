"""Tests for the data-understanding classifier core (BUG-010)."""
import unittest

from core.onboarding.data_model.data_understanding import (
    classify_quality_tier,
    classify_schema_type,
    scoped_processing_options,
)


def _col(name, dtype="String", null_count=None, distinct_count=None, samples=None):
    col = {"name": name, "dtype": dtype}
    if null_count is not None:
        col["null_count"] = null_count
    if distinct_count is not None:
        col["distinct_count"] = distinct_count
    if samples is not None:
        col["sample_values"] = samples
    return col


def _profile(name, row_count, columns):
    return {"table_name": name, "row_count": row_count, "columns": columns}


class QualityTierTests(unittest.TestCase):
    def test_high_null_and_dup_profile_is_raw_bronze(self):
        # 5% nulls, key not unique, plus key has nulls -> raw/bronze.
        profiles = [
            _profile("orders_raw", 1000, [
                _col("order_id", "String", null_count=50, distinct_count=600),
                _col("amount", "String", null_count=100),
                _col("status", "String", null_count=80),
            ]),
        ]
        result = classify_quality_tier(profiles)
        self.assertEqual(result["tier"], "raw/bronze")
        self.assertGreater(result["confidence"], 0.4)
        self.assertTrue(any("null" in e.lower() or "non-unique" in e.lower() for e in result["evidence"]))

    def test_clean_unique_typed_profile_is_silver(self):
        profiles = [
            _profile("orders", 1000, [
                _col("order_id", "String", null_count=0, distinct_count=1000),
                _col("amount", "Decimal", null_count=0),
                _col("created_at", "Timestamp", null_count=0),
                _col("status", "String", null_count=0),
            ]),
        ]
        result = classify_quality_tier(profiles)
        self.assertEqual(result["tier"], "silver")
        self.assertGreaterEqual(result["confidence"], 0.7)

    def test_clean_aggregated_single_table_is_gold(self):
        profiles = [
            _profile("daily_revenue_summary", 365, [
                _col("business_date", "Date", null_count=0, distinct_count=365),
                _col("gross_revenue", "Decimal", null_count=0),
                _col("order_count", "Int64", null_count=0),
                _col("average_order_value", "Decimal", null_count=0),
            ]),
        ]
        result = classify_quality_tier(profiles)
        self.assertEqual(result["tier"], "gold")
        self.assertTrue(any("gold" in e.lower() for e in result["evidence"]))

    def test_missing_stats_returns_low_confidence_raw(self):
        # No null_count / distinct_count anywhere -> cannot prove clean.
        profiles = [
            _profile("mystery", 100, [
                _col("a", "String"),
                _col("b", "String"),
            ]),
        ]
        result = classify_quality_tier(profiles)
        self.assertEqual(result["tier"], "raw/bronze")
        self.assertLessEqual(result["confidence"], 0.4)
        self.assertTrue(any("insufficient" in e.lower() or "no null" in e.lower() for e in result["evidence"]))

    def test_empty_profiles_is_low_confidence_raw(self):
        result = classify_quality_tier([])
        self.assertEqual(result["tier"], "raw/bronze")
        self.assertLess(result["confidence"], 0.3)


class SchemaTypeTests(unittest.TestCase):
    def test_single_wide_table_is_obt(self):
        wide = _profile("all_events", 1000, [_col(f"c{i}", "String") for i in range(60)])
        result = classify_schema_type([wide], [])
        self.assertEqual(result["schema_type"], "OBT")

    def test_single_narrow_table_is_flat(self):
        flat = _profile("export", 1000, [_col("a"), _col("b"), _col("c")])
        result = classify_schema_type([flat], [])
        self.assertEqual(result["schema_type"], "flat")

    def test_dimension_referencing_dimension_is_snowflake(self):
        # fact -> patient, fact -> department, provider -> department (nested),
        # fact -> provider. Provider is both a dimension (referenced by fact) and
        # references another dimension (department) -> snowflake + nested.
        profiles = [
            _profile("transactions", 10000, [_col("TransactionID"), _col("PatientID"), _col("ProviderID"), _col("DeptID")]),
            _profile("patients", 5000, [_col("PatientID"), _col("DOB")]),
            _profile("departments", 20, [_col("DeptID"), _col("Name")]),
            _profile("providers", 25, [_col("ProviderID"), _col("DeptID")]),
        ]
        relationships = {
            "relationships": [
                {"left_dataset": "transactions", "left_column": "PatientID",
                 "right_dataset": "patients", "right_column": "PatientID"},
                {"left_dataset": "transactions", "left_column": "DeptID",
                 "right_dataset": "departments", "right_column": "DeptID"},
                {"left_dataset": "transactions", "left_column": "ProviderID",
                 "right_dataset": "providers", "right_column": "ProviderID"},
                {"left_dataset": "providers", "left_column": "DeptID",
                 "right_dataset": "departments", "right_column": "DeptID"},
            ]
        }
        result = classify_schema_type(profiles, relationships)
        self.assertEqual(result["schema_type"], "snowflake")
        self.assertTrue(any("nested" in e.lower() or "snowflake" in e.lower() for e in result["evidence"]))

    def test_fact_with_flat_dimensions_is_star(self):
        profiles = [
            _profile("sales", 10000, [_col("SaleID"), _col("ProductID"), _col("CustomerID"), _col("DateID")]),
            _profile("products", 100, [_col("ProductID")]),
            _profile("customers", 500, [_col("CustomerID")]),
            _profile("dates", 365, [_col("DateID")]),
        ]
        rels = [
            {"left_dataset": "sales", "right_dataset": "products"},
            {"left_dataset": "sales", "right_dataset": "customers"},
            {"left_dataset": "sales", "right_dataset": "dates"},
        ]
        result = classify_schema_type(profiles, rels)
        self.assertEqual(result["schema_type"], "star")

    def test_two_facts_sharing_dimension_is_galaxy(self):
        profiles = [
            _profile("sales", 10000, [_col("SaleID"), _col("ProductID"), _col("DateID")]),
            _profile("returns", 2000, [_col("ReturnID"), _col("ProductID"), _col("DateID")]),
            _profile("products", 100, [_col("ProductID")]),
            _profile("dates", 365, [_col("DateID")]),
        ]
        rels = [
            {"left_dataset": "sales", "right_dataset": "products"},
            {"left_dataset": "sales", "right_dataset": "dates"},
            {"left_dataset": "returns", "right_dataset": "products"},
            {"left_dataset": "returns", "right_dataset": "dates"},
        ]
        result = classify_schema_type(profiles, rels)
        self.assertEqual(result["schema_type"], "galaxy")

    def test_self_reference_is_hierarchical(self):
        profiles = [_profile("employee", 500, [_col("emp_id"), _col("manager_id")])]
        rels = [{"left_dataset": "employee", "right_dataset": "employee"}]
        # two tables minimum is bypassed since self-ref is detected before fact analysis;
        # provide a second table-free scenario by including the self edge with >1 table guard.
        profiles2 = profiles + [_profile("dept", 10, [_col("dept_id")])]
        rels2 = rels + [{"left_dataset": "employee", "right_dataset": "dept"}]
        result = classify_schema_type(profiles, rels)
        # single table + relationship: with one profile we hit the flat/OBT branch,
        # so test the multi-table self-ref-only path instead.
        self.assertIn(result["schema_type"], ("flat", "OBT"))
        result2 = classify_schema_type(profiles2, [rels2[0]])
        self.assertEqual(result2["schema_type"], "hierarchical")

    def test_legacy_from_to_keys_tolerated(self):
        profiles = [
            _profile("f", 100, [_col("a")]),
            _profile("d1", 10, [_col("b")]),
            _profile("d2", 10, [_col("c")]),
        ]
        rels = [
            {"from_table": "f", "to_table": "d1"},
            {"from_table": "f", "to_table": "d2"},
        ]
        result = classify_schema_type(profiles, rels)
        self.assertEqual(result["schema_type"], "star")


class ScopedOptionsTests(unittest.TestCase):
    def test_raw_options_are_cleansing_steps(self):
        opts = scoped_processing_options("raw/bronze", [], None)
        ids = {o["id"] for o in opts}
        self.assertIn("cast_types", ids)
        self.assertIn("deduplicate", ids)
        for o in opts:
            self.assertEqual(o["applies_when"], "raw/bronze")
            self.assertTrue(o["description"])

    def test_silver_options_are_promotion_steps(self):
        opts = scoped_processing_options("silver", [], None)
        ids = {o["id"] for o in opts}
        self.assertIn("denormalize", ids)
        self.assertIn("aggregate_metrics", ids)

    def test_raw_options_differ_from_silver_options(self):
        raw_ids = {o["id"] for o in scoped_processing_options("raw/bronze", [], None)}
        silver_ids = {o["id"] for o in scoped_processing_options("silver", [], None)}
        self.assertNotEqual(raw_ids, silver_ids)
        self.assertEqual(raw_ids & silver_ids, set())

    def test_snowflake_silver_adds_prejoin_option(self):
        ids = {o["id"] for o in scoped_processing_options("silver", [], "snowflake")}
        self.assertIn("prejoin_nested_dimensions", ids)

    def test_gold_offers_maintenance_not_recleaning(self):
        ids = {o["id"] for o in scoped_processing_options("gold", [], None)}
        self.assertIn("optimize_layout", ids)
        self.assertNotIn("cast_types", ids)
        self.assertNotIn("deduplicate", ids)

    def test_unknown_tier_returns_minimal_fallback(self):
        opts = scoped_processing_options("platinum", [], None)
        self.assertEqual(len(opts), 1)
        self.assertEqual(opts[0]["applies_when"], "unknown")


if __name__ == "__main__":
    unittest.main()
