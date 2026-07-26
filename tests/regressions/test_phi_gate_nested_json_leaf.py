"""PHI/PCI gate nested-JSON-payload blind spot, P2.

core/governance/phi_gate.py's `_iter_profile_columns` read only a profile's
flat top-level `schema` dict, so a field nested inside a JSON payload column
(e.g. `metadata.patient.ssn`, populated by P1's profiler fix) was invisible to
`identifier_category()` -- meaning `enforce_remote_phi_gate` (the systemic
guardrail this platform's docstring says exists because of a real 2026-06 PHI
incident) could not detect PHI hidden inside a nested payload at all.

Fixed: `_iter_profile_columns` also yields from P1's `iter_nested_leaf_entries`,
and both places an anchored identifier regex is matched (`_match_category` and
`identifier_category`'s two direct bare-name/ambiguous-date checks) now also
try the leaf's own last dot-segment via `_leaf_match_candidates`, so
`metadata.patient.ssn` and `visits[].ssn` both match on `ssn`.
`_is_person_entity_table` (table-gating for the ambiguous bare-name/date
cases) is untouched -- it only ever inspects the table, never the column name.

See ~/.claude/plans/dynamic-cooking-firefly.md P2.
"""
from __future__ import annotations

import unittest

from core.governance.phi_gate import (
    detect_phi_columns,
    identifier_category,
    pci_identifier_category,
)


class LeafPathIdentifierMatchingTests(unittest.TestCase):
    def test_nested_struct_leaf_ssn_is_detected(self):
        self.assertEqual(identifier_category("metadata.patient.ssn"), "ssn")

    def test_array_element_leaf_ssn_is_detected(self):
        self.assertEqual(identifier_category("visits[].ssn"), "ssn")

    def test_bare_name_leaf_on_person_table_is_phi(self):
        self.assertEqual(
            identifier_category("patient_info.name", table="patients.json"), "name"
        )

    def test_bare_name_leaf_on_non_person_table_is_not_phi(self):
        # Proves _is_person_entity_table's table-gating survives the leaf-path
        # change untouched -- a departments/org label is still not PHI.
        self.assertIsNone(
            identifier_category("dept_info.name", table="departments.csv")
        )

    def test_flat_column_regression_guard(self):
        self.assertEqual(identifier_category("first_name"), "name")

    def test_nested_pci_leaf_is_detected(self):
        self.assertEqual(
            pci_identifier_category("payment.card_number"), "primary_account_number"
        )


class DetectPhiColumnsEndToEndTests(unittest.TestCase):
    def test_nested_leaf_produces_a_phi_finding(self):
        profile_index = {
            "profiles": [
                {
                    "path": "datasets/api_source/data.json",
                    "schema": {"metadata": "Struct({'patient': Struct({'ssn': String})})", "amount": "Int64"},
                    "nested_leaf_columns": [
                        {"name": "metadata.patient.ssn", "dtype": "String"},
                    ],
                }
            ]
        }
        findings = detect_phi_columns(profile_index)
        matches = [f for f in findings if f.column == "metadata.patient.ssn"]
        self.assertEqual(len(matches), 1)
        self.assertEqual(matches[0].identifier_category, "ssn")

    def test_profile_with_no_nested_leaf_columns_key_is_unaffected(self):
        profile_index = {
            "profiles": [
                {"path": "datasets/flat/data.csv", "schema": {"ssn": "String"}}
            ]
        }
        findings = detect_phi_columns(profile_index)
        self.assertEqual(len(findings), 1)
        self.assertEqual(findings[0].column, "ssn")


if __name__ == "__main__":
    unittest.main()
