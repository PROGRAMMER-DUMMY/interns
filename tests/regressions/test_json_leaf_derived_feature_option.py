"""Silver JSON-leaf-promotion as a derived-feature-option candidate, P3.

Fields nested inside a JSON payload column are now discoverable (P1) and
PHI/PCI-classifiable (P2), but a KPI referencing one of those fields (e.g.
`ssn` matching a profiled `metadata.patient.ssn` leaf) still could not be
resolved to anything -- there was no path from "a nested leaf exists" to "a
real typed silver column exists". Fixed: `schema_index_from_profiles`
(schema_alias_matching.py) and `columns_from_profile_index`
(metric_derivation.py) now also index nested leaves (tagged
`is_nested_leaf=True`, traced back to `raw_source_column`); a new
`detect_json_leaf_promotion_candidates` (derivation_patterns.py) turns a
token match into a JSON-backed derived-feature option -- the SAME
human-confirmation contract every other pattern in this module uses (no
silent promotion) -- with per-dialect (DuckDB/Spark/Polars) JSON-extraction
formulas; `feature_resolver.py` wires it into the existing fallback branch.
`core/medallion/design.py`'s existing lift of a `user_confirmed` review's
`derived_feature_options[0]` into `derived_columns` needed NO changes --
verified directly, and proven again here end-to-end.

See ~/.claude/plans/dynamic-cooking-firefly.md P3.
"""
from __future__ import annotations

import unittest

from core.onboarding.features.derivation_patterns import (
    detect_derivation_patterns,
    detect_json_leaf_promotion_candidates,
)
from core.onboarding.kpi.metric_derivation import columns_from_profile_index
from core.onboarding.relationships.schema_alias_matching import schema_index_from_profiles
from core.medallion.design import _seed_proposal


def _profile_with_nested_leaf(dataset_path: str) -> dict:
    return {
        "path": dataset_path,
        "row_count": 100,
        "schema": {"metadata": "Struct({'patient': Struct({'ssn': String})})", "amount": "Int64"},
        "nested_leaf_columns": [
            {
                "name": "metadata.patient.ssn",
                "dtype": "String",
                "null_count": 2,
                "sample_values": ["123-45-6789"],
            }
        ],
    }


class SchemaIndexNestedLeafTaggingTests(unittest.TestCase):
    def test_leaf_entry_is_tagged_and_traces_to_raw_column(self):
        index = schema_index_from_profiles([_profile_with_nested_leaf("datasets/api_source/data.json")])
        leaf_entries = [
            entry
            for entries in index.values()
            for entry in entries
            if entry.get("is_nested_leaf")
        ]
        self.assertEqual(len(leaf_entries), 1)
        self.assertEqual(leaf_entries[0]["column"], "metadata.patient.ssn")
        self.assertEqual(leaf_entries[0]["raw_source_column"], "metadata")

    def test_flat_profile_fixture_unaffected(self):
        flat_profile = {
            "path": "datasets/flat/data.csv",
            "row_count": 10,
            "schema": {"id": "Int64", "name": "String"},
        }
        index = schema_index_from_profiles([flat_profile])
        for entries in index.values():
            for entry in entries:
                self.assertNotIn("is_nested_leaf", entry)


class ColumnsFromProfileIndexNestedLeafTests(unittest.TestCase):
    def test_nested_leaf_appears_in_flattened_columns(self):
        columns = columns_from_profile_index(
            {"profiles": [_profile_with_nested_leaf("datasets/api_source/data.json")]}
        )
        leaf_cols = [c for c in columns if c.get("is_nested_leaf")]
        self.assertEqual(len(leaf_cols), 1)
        self.assertEqual(leaf_cols[0]["column"], "metadata.patient.ssn")

    def test_flat_profile_fixture_unaffected(self):
        columns = columns_from_profile_index(
            {"profiles": [{"path": "datasets/flat/data.csv", "row_count": 5, "schema": {"id": "Int64"}}]}
        )
        self.assertTrue(all(not c.get("is_nested_leaf") for c in columns))
        self.assertEqual({c["column"] for c in columns}, {"id"})

    def test_nested_start_stop_pair_is_detected_as_a_duration_bucket(self):
        """Closes the blind spot found during plan verification: a duration-
        bucket KPI whose start/stop timestamps live inside a nested struct
        must still be detected once columns_from_profile_index surfaces them."""
        profile = {
            "path": "datasets/api_source/data.json",
            "row_count": 50,
            "schema": {"visit": "Struct({'admitted_at': String, 'discharged_at': String})"},
            "nested_leaf_columns": [
                {
                    "name": "visit.admitted_at",
                    "dtype": "String",
                    "sample_values": ["2024-01-01T08:00:00", "2024-01-02T09:00:00"],
                },
                {
                    "name": "visit.discharged_at",
                    "dtype": "String",
                    "sample_values": ["2024-01-01T20:00:00", "2024-01-02T21:00:00"],
                },
            ],
        }
        columns = columns_from_profile_index({"profiles": [profile]})
        options = detect_derivation_patterns(
            "How many stays lasted over 24 hours?", columns
        )
        self.assertTrue(any(o.get("source_pattern_id") == "duration_bucket" for o in options))


class JsonLeafPromotionCandidateTests(unittest.TestCase):
    def test_token_matching_leaf_last_segment_produces_a_full_option(self):
        index = schema_index_from_profiles([_profile_with_nested_leaf("datasets/api_source/data.json")])
        options = detect_json_leaf_promotion_candidates("ssn", index)
        self.assertEqual(len(options), 1)
        opt = options[0]
        self.assertEqual(opt["derived_column_name"], "metadata_patient_ssn")
        self.assertEqual(opt["option_kind"], "json_leaf_promotion")
        self.assertTrue(opt["needs_user_confirmation"])
        templates = opt["formula_templates"]
        for dialect in ("duckdb_sql", "spark_sql", "polars"):
            self.assertTrue(templates.get(dialect), f"missing {dialect}")
        self.assertIn("metadata", templates["duckdb_sql"])
        self.assertIn("metadata", templates["spark_sql"])
        self.assertIn("metadata", templates["polars"])
        # Real evidence, not blank placeholders: the LEAF's own sample values
        # (a Struct column has no meaningful "sample values" of its own) --
        # P1's leaf stats feeding through as the review-facing evidence, while
        # "column" still routes to the RAW physical column the formula reads.
        self.assertEqual(opt["input_columns"][0]["observed_values"], ["123-45-6789"])
        self.assertEqual(opt["input_columns"][0]["column"], "metadata")

    def test_no_match_for_an_unrelated_token(self):
        index = schema_index_from_profiles([_profile_with_nested_leaf("datasets/api_source/data.json")])
        self.assertEqual(detect_json_leaf_promotion_candidates("totally_unrelated", index), [])

    def test_no_nested_columns_yields_no_candidates(self):
        index = schema_index_from_profiles(
            [{"path": "datasets/flat/data.csv", "row_count": 5, "schema": {"ssn": "String"}}]
        )
        # A PHYSICAL column literally named "ssn" is a direct match, not a
        # promotion candidate -- detect_json_leaf_promotion_candidates only
        # ever looks at is_nested_leaf entries.
        self.assertEqual(detect_json_leaf_promotion_candidates("ssn", index), [])


class DesignPyLiftRoundTripTests(unittest.TestCase):
    def test_confirmed_json_leaf_option_lifts_into_derived_columns_all_dialects(self):
        index = schema_index_from_profiles([_profile_with_nested_leaf("datasets/api_source/data.json")])
        options = detect_json_leaf_promotion_candidates("ssn", index)
        self.assertEqual(len(options), 1)

        inputs = {
            "domain_model": {
                "datasets": [
                    {
                        "path": "datasets/api_source/data.json",
                        "schema": {"metadata": "Struct(...)", "amount": "Int64"},
                    }
                ]
            },
            "semantic_contract": {},
            "relationship_contracts": {},
            "derived_feature_reviews": [
                {
                    "feature_state": "user_confirmed",
                    "kpi_id": "kpi_001",
                    "_source_path": "interns/reports/derived_feature_reviews/json/ssn.json",
                    "derived_feature_options": options,
                }
            ],
        }
        proposal = _seed_proposal(inputs)
        derived_all = {
            name: table.get("derived_columns", {})
            for name, table in proposal.get("silver_tables", proposal).items()
        } if "silver_tables" not in proposal else proposal["silver_tables"]
        # _seed_proposal's return shape nests tables under a top-level key in
        # some builds and flat in others across this repo's history -- locate
        # the lifted entry defensively rather than assume one exact shape.
        found = None
        for _, table in _iter_tables(proposal):
            if "metadata_patient_ssn" in (table.get("derived_columns") or {}):
                found = table["derived_columns"]["metadata_patient_ssn"]
                break
        self.assertIsNotNone(found, "derived column was not lifted into any silver table")
        for dialect in ("duckdb_sql", "spark_sql", "polars"):
            self.assertTrue(
                found["formula_templates"].get(dialect), f"missing {dialect} after lift"
            )


def _iter_tables(proposal: dict):
    silver = proposal.get("silver_tables")
    if isinstance(silver, dict):
        for name, table in silver.items():
            if isinstance(table, dict):
                yield name, table
        return
    for key, value in proposal.items():
        if isinstance(value, dict) and "derived_columns" in value:
            yield key, value


if __name__ == "__main__":
    unittest.main()
