"""User-authored workspace data policy: loading, PHI-gate merge, redaction widening.

The policy lets a workspace owner declare custom sensitive columns (beyond the
built-in HIPAA/PCI patterns), allowlist reviewed false positives, and force the
PHI tier. It may only WIDEN display redaction, never narrow it.
"""
from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from core.governance.data_policy import (
    POLICY_FILENAME,
    is_allowlisted,
    load_workspace_data_policy,
    policy_category_for_column,
    policy_redaction_patterns,
)
from core.governance.phi_gate import assess_workspace_phi, assess_workspace_pci
from core.onboarding.kpi.pii_redaction import (
    DEFAULT_PII_COLUMN_PATTERNS,
    is_pii_column,
    redact_rows,
)


class _Layout:
    def __init__(self, root: Path) -> None:
        self.project_root = root
        self.profiles_dir = root / "interns" / "generated" / "profiles"


def _write_workspace(
    root: Path,
    *,
    columns: list[str],
    policy: dict | None = None,
) -> _Layout:
    layout = _Layout(root)
    layout.profiles_dir.mkdir(parents=True, exist_ok=True)
    (layout.profiles_dir / "profile_index.json").write_text(
        json.dumps(
            {
                "profiles": [
                    {"path": "datasets/main.csv", "schema": {c: "VARCHAR" for c in columns}}
                ]
            }
        ),
        encoding="utf-8",
    )
    if policy is not None:
        (root / POLICY_FILENAME).write_text(json.dumps(policy), encoding="utf-8")
    return layout


class PolicyLoadingTest(unittest.TestCase):
    def test_absent_policy_returns_none(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            self.assertIsNone(load_workspace_data_policy(tmp))

    def test_docs_subdir_is_searched(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            docs = Path(tmp) / "docs"
            docs.mkdir()
            (docs / POLICY_FILENAME).write_text(
                json.dumps({"sensitive_columns": ["LoyaltyCode"]}), encoding="utf-8"
            )
            policy = load_workspace_data_policy(tmp)
            assert policy is not None
            self.assertEqual(policy.sensitive_columns, ("LoyaltyCode",))
            self.assertTrue(policy.ok)

    def test_invalid_json_reports_error_not_crash(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            (Path(tmp) / POLICY_FILENAME).write_text("{not json", encoding="utf-8")
            policy = load_workspace_data_policy(tmp)
            assert policy is not None
            self.assertFalse(policy.ok)
            self.assertTrue(policy.errors)

    def test_invalid_regex_reported_and_skipped(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            (Path(tmp) / POLICY_FILENAME).write_text(
                json.dumps(
                    {"sensitive_column_patterns": ["^ok$", "([unclosed"]}
                ),
                encoding="utf-8",
            )
            policy = load_workspace_data_policy(tmp)
            assert policy is not None
            self.assertEqual(policy.sensitive_column_patterns, ("^ok$",))
            self.assertFalse(policy.ok)

    def test_declared_and_allowlisted_conflict_is_an_error(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            (Path(tmp) / POLICY_FILENAME).write_text(
                json.dumps(
                    {
                        "sensitive_columns": ["RiskScore"],
                        "not_sensitive_columns": ["riskscore"],
                    }
                ),
                encoding="utf-8",
            )
            policy = load_workspace_data_policy(tmp)
            assert policy is not None
            self.assertFalse(policy.ok)
            self.assertEqual(policy.not_sensitive_columns, ())

    def test_category_matching(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            (Path(tmp) / POLICY_FILENAME).write_text(
                json.dumps(
                    {
                        "sensitive_columns": ["LoyaltyCode"],
                        "categories": {"trade_secret": [r"^route[_ ]?cost$"]},
                    }
                ),
                encoding="utf-8",
            )
            policy = load_workspace_data_policy(tmp)
            self.assertEqual(
                policy_category_for_column(policy, "loyaltycode"), "policy:sensitive"
            )
            self.assertEqual(
                policy_category_for_column(policy, "Route Cost"), "policy:trade_secret"
            )
            self.assertIsNone(policy_category_for_column(policy, "Quantity"))


class PHIGateMergeTest(unittest.TestCase):
    def test_policy_columns_create_findings_and_raise_tier(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            layout = _write_workspace(
                Path(tmp),
                columns=["OrderId", "LoyaltyCode"],
                policy={"sensitive_columns": ["LoyaltyCode"]},
            )
            assessment = assess_workspace_phi(layout)
            self.assertEqual(assessment.tier, "phi")
            categories = {f.identifier_category for f in assessment.findings}
            self.assertIn("policy:sensitive", categories)

    def test_allowlist_suppresses_builtin_finding(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            # "Name" matches the built-in HIPAA name pattern; the owner
            # reviewed it (it's a product name) and allowlisted it.
            layout = _write_workspace(
                Path(tmp),
                columns=["Name", "Quantity"],
                policy={"not_sensitive_columns": ["Name"]},
            )
            assessment = assess_workspace_phi(layout)
            self.assertEqual(assessment.tier, "none")
            self.assertEqual(assessment.findings, [])

    def test_tier_override_forces_phi_with_no_findings(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            layout = _write_workspace(
                Path(tmp),
                columns=["OrderId", "Quantity"],
                policy={"tier_override": "phi"},
            )
            assessment = assess_workspace_phi(layout)
            self.assertEqual(assessment.tier, "phi")

    def test_no_policy_keeps_builtin_behavior(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            layout = _write_workspace(Path(tmp), columns=["ssn", "Quantity"])
            assessment = assess_workspace_phi(layout)
            self.assertEqual(assessment.tier, "phi")

    def test_pci_allowlist_applies_but_categories_do_not(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            layout = _write_workspace(
                Path(tmp),
                columns=["account_number", "LoyaltyCode"],
                policy={
                    "not_sensitive_columns": ["account_number"],
                    "sensitive_columns": ["LoyaltyCode"],
                },
            )
            assessment = assess_workspace_pci(layout)
            self.assertEqual(assessment.tier, "none")
            self.assertEqual(assessment.findings, [])


class RedactionWideningTest(unittest.TestCase):
    def test_policy_patterns_widen_display_redaction(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            (Path(tmp) / POLICY_FILENAME).write_text(
                json.dumps({"sensitive_columns": ["LoyaltyCode"]}), encoding="utf-8"
            )
            policy = load_workspace_data_policy(tmp)
            patterns = DEFAULT_PII_COLUMN_PATTERNS + policy_redaction_patterns(policy)
            self.assertFalse(is_pii_column("LoyaltyCode"))
            self.assertTrue(is_pii_column("LoyaltyCode", patterns=patterns))
            rows = redact_rows(
                [{"LoyaltyCode": "LX-9", "Quantity": 3}], patterns=patterns
            )
            self.assertEqual(rows[0]["LoyaltyCode"], "<redacted-pii>")
            self.assertEqual(rows[0]["Quantity"], 3)

    def test_allowlist_never_narrows_redaction(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            (Path(tmp) / POLICY_FILENAME).write_text(
                json.dumps({"not_sensitive_columns": ["ssn"]}), encoding="utf-8"
            )
            policy = load_workspace_data_policy(tmp)
            self.assertTrue(is_allowlisted(policy, "ssn"))
            patterns = DEFAULT_PII_COLUMN_PATTERNS + policy_redaction_patterns(policy)
            # ssn stays redacted on rendered surfaces regardless of allowlist.
            self.assertTrue(is_pii_column("ssn", patterns=patterns))


if __name__ == "__main__":
    unittest.main()
