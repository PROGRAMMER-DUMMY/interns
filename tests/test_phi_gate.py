"""PHI gate: HIPAA-identifier detection, PHI tier, and non-covered remote block.

The enforced counterpart to display-only pii_redaction. Closes the systemic
gap behind the 2026-06 incident (real PHI uploaded to a non-HIPAA Databricks
trial).
"""
from __future__ import annotations

import json
import tempfile
import unittest
from dataclasses import dataclass
from pathlib import Path

from core.governance.phi_gate import (
    assess_workspace_pci,
    assess_workspace_phi,
    databricks_pci_covered,
    databricks_phi_covered,
    detect_pci_columns,
    detect_phi_columns,
    enforce_remote_pci_gate,
    enforce_remote_phi_gate,
    enforce_remote_sensitive_gate,
    identifier_category,
    pci_identifier_category,
)
from core.storage.workspace_layout import WorkspaceLayout


@dataclass
class _DBCfg:
    phi_covered: bool = False
    pci_covered: bool = False


@dataclass
class _Cfg:
    databricks: _DBCfg


def _profile_index(columns_by_dataset: dict[str, list[str]]) -> dict:
    return {
        "profiles": [
            {"path": ds, "schema": {c: {"dtype": "VARCHAR"} for c in cols}}
            for ds, cols in columns_by_dataset.items()
        ]
    }


def _ws_with_profiles(tmp: Path, columns_by_dataset: dict[str, list[str]]) -> WorkspaceLayout:
    ws = tmp / "workspaces" / "demo"
    layout = WorkspaceLayout(project_root=ws.resolve())
    layout.profiles_dir.mkdir(parents=True, exist_ok=True)
    (layout.profiles_dir / "profile_index.json").write_text(
        json.dumps(_profile_index(columns_by_dataset)), encoding="utf-8"
    )
    return layout


class IdentifierDetectionTests(unittest.TestCase):
    def test_direct_identifiers_classified(self):
        self.assertEqual(identifier_category("SSN"), "ssn")
        self.assertEqual(identifier_category("FirstName"), "name")
        self.assertEqual(identifier_category("DOB"), "date_of_birth")
        self.assertEqual(identifier_category("MedicaidID"), "health_plan_beneficiary")
        self.assertEqual(identifier_category("MedicareID"), "health_plan_beneficiary")
        self.assertEqual(identifier_category("MRN"), "medical_record_number")
        self.assertEqual(identifier_category("PhoneNumber"), "phone")

    def test_non_identifier_columns_are_none(self):
        for col in ("PaidAmount", "DepartmentName", "ClaimCount", "ServiceDate", "Gender"):
            self.assertIsNone(identifier_category(col), col)

    def test_detect_phi_columns_finds_incident_schema(self):
        # The real incident schema (column names only — no values).
        idx = _profile_index({
            "patients.csv": ["SSN", "FirstName", "LastName", "DOB", "Address", "PhoneNumber"],
            "transactions.csv": ["MedicaidID", "MedicareID", "PaidAmount"],
        })
        findings = detect_phi_columns(idx)
        cols = {f.column for f in findings}
        self.assertIn("SSN", cols)
        self.assertIn("MedicaidID", cols)
        self.assertNotIn("PaidAmount", cols)


class AssessmentTests(unittest.TestCase):
    def test_phi_tier_when_identifiers_present(self):
        with tempfile.TemporaryDirectory() as t:
            layout = _ws_with_profiles(Path(t), {"p.csv": ["SSN", "PaidAmount"]})
            a = assess_workspace_phi(layout)
            self.assertEqual(a.tier, "phi")
            self.assertTrue(a.is_phi)

    def test_none_tier_when_clean(self):
        with tempfile.TemporaryDirectory() as t:
            layout = _ws_with_profiles(Path(t), {"p.csv": ["PaidAmount", "DeptName"]})
            self.assertEqual(assess_workspace_phi(layout).tier, "none")

    def test_missing_profile_is_none_tier(self):
        with tempfile.TemporaryDirectory() as t:
            ws = Path(t) / "workspaces" / "demo"
            layout = WorkspaceLayout(project_root=ws.resolve())
            layout.profiles_dir.mkdir(parents=True, exist_ok=True)
            self.assertEqual(assess_workspace_phi(layout).tier, "none")


class EnforcementTests(unittest.TestCase):
    def test_phi_to_noncovered_target_is_blocked(self):
        with tempfile.TemporaryDirectory() as t:
            layout = _ws_with_profiles(Path(t), {"patients.csv": ["SSN", "DOB"]})
            failure = enforce_remote_phi_gate(layout, _Cfg(_DBCfg(phi_covered=False)))
            self.assertIsNotNone(failure)
            self.assertEqual(failure.kind.value, "remote_execution_denied")
            self.assertIn("PHI gate", failure.message)

    def test_phi_to_covered_target_is_allowed(self):
        with tempfile.TemporaryDirectory() as t:
            layout = _ws_with_profiles(Path(t), {"patients.csv": ["SSN", "DOB"]})
            self.assertIsNone(enforce_remote_phi_gate(layout, _Cfg(_DBCfg(phi_covered=True))))

    def test_deidentified_phi_is_allowed(self):
        with tempfile.TemporaryDirectory() as t:
            layout = _ws_with_profiles(Path(t), {"patients.csv": ["SSN"]})
            self.assertIsNone(
                enforce_remote_phi_gate(layout, _Cfg(_DBCfg(phi_covered=False)), deidentified=True)
            )

    def test_clean_workspace_is_allowed(self):
        with tempfile.TemporaryDirectory() as t:
            layout = _ws_with_profiles(Path(t), {"p.csv": ["PaidAmount"]})
            self.assertIsNone(enforce_remote_phi_gate(layout, _Cfg(_DBCfg(phi_covered=False))))

    def test_databricks_phi_covered_accepts_databricks_config_directly(self):
        # backends pass the DatabricksConfig itself (not the full Config)
        self.assertTrue(databricks_phi_covered(_DBCfg(phi_covered=True)))
        self.assertFalse(databricks_phi_covered(_DBCfg(phi_covered=False)))


class PCIDetectionTests(unittest.TestCase):
    def test_cardholder_identifiers_classified(self):
        self.assertEqual(pci_identifier_category("CardNumber"), "primary_account_number")
        self.assertEqual(pci_identifier_category("credit_card_number"), "primary_account_number")
        self.assertEqual(pci_identifier_category("PAN"), "primary_account_number")
        self.assertEqual(pci_identifier_category("CVV"), "card_verification")
        self.assertEqual(pci_identifier_category("cvc2"), "card_verification")
        self.assertEqual(pci_identifier_category("CardExpiry"), "card_expiry")
        self.assertEqual(pci_identifier_category("exp_month"), "card_expiry")
        self.assertEqual(pci_identifier_category("CardholderName"), "cardholder_name")
        self.assertEqual(pci_identifier_category("Track1Data"), "card_track_data")
        self.assertEqual(pci_identifier_category("IBAN"), "bank_account")
        self.assertEqual(pci_identifier_category("RoutingNumber"), "bank_account")

    def test_non_pci_columns_are_none(self):
        for col in (
            "PaidAmount", "OrderId", "Gender", "DepartmentName", "panel_id",
            "company", "expiry_date", "ExpirationDate",
        ):
            self.assertIsNone(pci_identifier_category(col), col)

    def test_detect_pci_columns_scans_profiles(self):
        idx = _profile_index({
            "payments.csv": ["CardNumber", "CVV", "Amount"],
            "orders.csv": ["OrderId", "Total"],
        })
        findings = detect_pci_columns(idx)
        cols = {f.column for f in findings}
        self.assertEqual(cols, {"CardNumber", "CVV"})

    def test_pci_tier_assessment(self):
        with tempfile.TemporaryDirectory() as t:
            layout = _ws_with_profiles(Path(t), {"payments.csv": ["CardNumber", "Amount"]})
            self.assertEqual(assess_workspace_pci(layout).tier, "pci")
            layout2 = _ws_with_profiles(Path(t) / "b", {"orders.csv": ["OrderId", "Total"]})
            self.assertEqual(assess_workspace_pci(layout2).tier, "none")


class PCIEnforcementTests(unittest.TestCase):
    def test_pci_to_noncovered_target_is_blocked(self):
        with tempfile.TemporaryDirectory() as t:
            layout = _ws_with_profiles(Path(t), {"payments.csv": ["CardNumber", "CVV"]})
            failure = enforce_remote_pci_gate(layout, _Cfg(_DBCfg(pci_covered=False)))
            self.assertIsNotNone(failure)
            self.assertEqual(failure.kind.value, "remote_execution_denied")
            self.assertIn("PCI gate", failure.message)

    def test_pci_to_covered_target_is_allowed(self):
        with tempfile.TemporaryDirectory() as t:
            layout = _ws_with_profiles(Path(t), {"payments.csv": ["CardNumber"]})
            self.assertIsNone(enforce_remote_pci_gate(layout, _Cfg(_DBCfg(pci_covered=True))))

    def test_tokenized_pci_is_allowed(self):
        with tempfile.TemporaryDirectory() as t:
            layout = _ws_with_profiles(Path(t), {"payments.csv": ["CardNumber"]})
            self.assertIsNone(
                enforce_remote_pci_gate(layout, _Cfg(_DBCfg(pci_covered=False)), deidentified=True)
            )

    def test_databricks_pci_covered_accepts_databricks_config_directly(self):
        self.assertTrue(databricks_pci_covered(_DBCfg(pci_covered=True)))
        self.assertFalse(databricks_pci_covered(_DBCfg(pci_covered=False)))


class CombinedSensitiveGateTests(unittest.TestCase):
    def test_phi_block_wins_first(self):
        with tempfile.TemporaryDirectory() as t:
            layout = _ws_with_profiles(Path(t), {"mix.csv": ["SSN", "CardNumber"]})
            failure = enforce_remote_sensitive_gate(layout, _Cfg(_DBCfg()))
            self.assertIsNotNone(failure)
            self.assertIn("PHI gate", failure.message)

    def test_pci_blocks_when_phi_clear(self):
        with tempfile.TemporaryDirectory() as t:
            layout = _ws_with_profiles(Path(t), {"payments.csv": ["CardNumber", "Amount"]})
            failure = enforce_remote_sensitive_gate(
                layout, _Cfg(_DBCfg(phi_covered=True, pci_covered=False))
            )
            self.assertIsNotNone(failure)
            self.assertIn("PCI gate", failure.message)

    def test_clean_workspace_passes_combined_gate(self):
        with tempfile.TemporaryDirectory() as t:
            layout = _ws_with_profiles(Path(t), {"orders.csv": ["OrderId", "Total"]})
            self.assertIsNone(enforce_remote_sensitive_gate(layout, _Cfg(_DBCfg())))

    def test_both_covered_passes_combined_gate(self):
        with tempfile.TemporaryDirectory() as t:
            layout = _ws_with_profiles(Path(t), {"mix.csv": ["SSN", "CardNumber"]})
            self.assertIsNone(
                enforce_remote_sensitive_gate(
                    layout, _Cfg(_DBCfg(phi_covered=True, pci_covered=True))
                )
            )


if __name__ == "__main__":
    unittest.main()
