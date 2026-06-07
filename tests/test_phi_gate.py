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
    assess_workspace_phi,
    databricks_phi_covered,
    detect_phi_columns,
    enforce_remote_phi_gate,
    identifier_category,
)
from core.storage.workspace_layout import WorkspaceLayout


@dataclass
class _DBCfg:
    phi_covered: bool = False


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


if __name__ == "__main__":
    unittest.main()
