"""Referential-integrity checks are generated from PROVEN foreign keys (the
framework's relational-integrity check), join on the canonical silver key, and
exclude unproven/low-confidence relationships."""
from __future__ import annotations

import unittest

from core.medallion.design import _emit_referential_integrity_checks
from core.medallion.silver_contract import SilverContract, TableContract


def _contract() -> SilverContract:
    return SilverContract(workspace="demo", tables={
        "encounter": TableContract(key_rename={"Id": "encounter_id"}),
        "patient": TableContract(key_rename={"Id": "patient_id"}),
        "payer": TableContract(key_rename={"Id": "payer_id"}),
    })


def _rels(*rels) -> dict:
    return {"relationships": list(rels)}


def _fk(left_ds, left_col, right_ds, right_col, state="proven_data_model", conf=0.9):
    return {
        "left_dataset": f"workspaces/demo/{left_ds}.csv", "left_column": left_col,
        "right_dataset": f"workspaces/demo/{right_ds}.csv", "right_column": right_col,
        "relationship_type": "foreign_key", "state": state, "confidence": conf,
    }


class ReferentialIntegrityTests(unittest.TestCase):
    def test_proven_fk_becomes_orphan_query_on_canonical_key(self):
        sql = _emit_referential_integrity_checks(
            _rels(_fk("encounters", "PATIENT", "patients", "Id")), _contract())
        self.assertIn("FROM silver.encounter c", sql)
        self.assertIn("LEFT JOIN silver.patient p", sql)
        # joins on the CANONICAL renamed key, not raw Id
        self.assertIn('p."patient_id" = c."PATIENT"', sql)
        self.assertIn('WHERE c."PATIENT" IS NOT NULL AND p."patient_id" IS NULL', sql)

    def test_unproven_relationship_is_excluded(self):
        sql = _emit_referential_integrity_checks(
            _rels(_fk("encounters", "PATIENT", "patients", "Id",
                      state="candidate", conf=0.5)), _contract())
        self.assertNotIn("silver.encounter c", sql)

    def test_low_confidence_is_excluded(self):
        sql = _emit_referential_integrity_checks(
            _rels(_fk("encounters", "PAYER", "payers", "Id", conf=0.6)), _contract())
        self.assertNotIn("LEFT JOIN silver.payer", sql)

    def test_no_relationships_is_empty_but_valid(self):
        sql = _emit_referential_integrity_checks(None, _contract())
        self.assertIn("Referential integrity", sql)
        self.assertNotIn("LEFT JOIN", sql)


if __name__ == "__main__":
    unittest.main()
