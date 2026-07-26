"""Regression: a backtick-quoted Unity Catalog FQN must keep its table identity.

Origin (2026-07-26 agy-harness audit, docs/core_audit/antigravity_cli_eval_log.md):
the medallion and dashboard-model paths derived table identity by ad-hoc string
parsing designed for local file paths. `Path("`cat`.`schema`.`tbl`").stem` strips
only the LAST dotted suffix, so every table in one schema collapsed onto the same
`` `cat`.`schema` `` identity -- six real source tables became two medallion
entities, and emitted filenames literally contained backticks
(``gold/fact_`healthcare.duckdb.sql``).

The shared, already-tested helper `core.profiling.dataset_identity` handles both
shapes. These tests fail if any of the affected call sites reintroduces a local
parser.
"""
from __future__ import annotations

import unittest

from core.dashboard.model.conformed import _stem as conformed_stem
from core.medallion.design_naming import dataset_name_key, logical_entity_from_path

FQNS = [
    "`healthcare_rcm`.`bronze`.`departments`",
    "`healthcare_rcm`.`bronze`.`encounters`",
    "`healthcare_rcm`.`bronze`.`transactions`",
    "`healthcare_rcm`.`bronze`.`patients`",
    "`healthcare_rcm`.`bronze`.`claims`",
    "`healthcare_rcm`.`bronze`.`providers`",
]


class MedallionIdentityTests(unittest.TestCase):
    def test_uc_tables_in_one_schema_keep_distinct_name_keys(self):
        keys = [dataset_name_key({"path": f}) for f in FQNS]
        self.assertEqual(len(set(keys)), len(FQNS), f"identities collapsed: {keys}")
        self.assertEqual(
            keys,
            ["departments", "encounters", "transactions", "patients", "claims", "providers"],
        )

    def test_no_backtick_survives_into_an_identity(self):
        for f in FQNS:
            self.assertNotIn("`", dataset_name_key({"path": f}))
            self.assertNotIn("`", logical_entity_from_path(f))

    def test_uc_logical_entities_are_singularized_per_table(self):
        self.assertEqual(logical_entity_from_path(FQNS[0]), "department")
        self.assertEqual(logical_entity_from_path(FQNS[2]), "transaction")

    def test_local_path_behaviour_is_unchanged(self):
        # The fix must not regress the local-native majority.
        self.assertEqual(dataset_name_key({"path": "data/claims.csv"}), "claims")
        self.assertEqual(dataset_name_key({"path": "data/patients_data.parquet"}), "patients_data")
        self.assertEqual(logical_entity_from_path("data/patients_data.parquet"), "patient")
        self.assertEqual(dataset_name_key({"path": ""}), "*")


class ConformedModelIdentityTests(unittest.TestCase):
    def test_fqn_stems_stay_distinct(self):
        stems = [conformed_stem(f) for f in FQNS]
        self.assertEqual(len(set(stems)), len(FQNS), f"identities collapsed: {stems}")
        self.assertIn("departments", stems)

    def test_local_file_stem_drops_only_the_extension(self):
        # The agy-session edit returned the EXTENSION here ('csv'), collapsing every
        # local table in a workspace onto one identity.
        self.assertEqual(conformed_stem("data/claims.csv"), "claims")
        self.assertEqual(conformed_stem("workspaces/x/datasets/Patients.xlsx"), "patients")

    def test_bare_table_name_is_passed_through(self):
        self.assertEqual(conformed_stem("transactions"), "transactions")


if __name__ == "__main__":
    unittest.main()
