"""Tests for :mod:`core.onboarding.kpi.pii_redaction`."""

from __future__ import annotations

import unittest

from core.onboarding.kpi.pii_redaction import (
    AGE_OVERFLOW_LABEL,
    DEFAULT_PII_COLUMN_PATTERNS,
    REDACTION_PLACEHOLDER,
    bucket_age_value,
    is_age_column,
    is_pii_column,
    redact_row_dict,
    redact_rows,
    redact_sample_values,
)


class IsPiiColumnTests(unittest.TestCase):
    def test_ssn_upper(self) -> None:
        self.assertTrue(is_pii_column("SSN"))

    def test_ssn_lower(self) -> None:
        self.assertTrue(is_pii_column("ssn"))

    def test_first_name_camel(self) -> None:
        self.assertTrue(is_pii_column("FirstName"))

    def test_first_name_snake(self) -> None:
        self.assertTrue(is_pii_column("first_name"))

    def test_first_name_with_space(self) -> None:
        self.assertTrue(is_pii_column("First Name"))

    def test_last_name_variants(self) -> None:
        self.assertTrue(is_pii_column("LastName"))
        self.assertTrue(is_pii_column("last_name"))
        self.assertTrue(is_pii_column("Last Name"))

    def test_middle_full_name(self) -> None:
        self.assertTrue(is_pii_column("MiddleName"))
        self.assertTrue(is_pii_column("FullName"))
        self.assertTrue(is_pii_column("Name"))

    def test_phone_variants(self) -> None:
        self.assertTrue(is_pii_column("Phone"))
        self.assertTrue(is_pii_column("PhoneNumber"))
        self.assertTrue(is_pii_column("phone_number"))

    def test_email_variants(self) -> None:
        self.assertTrue(is_pii_column("Email"))
        self.assertTrue(is_pii_column("EmailAddress"))
        self.assertTrue(is_pii_column("email_address"))

    def test_address_variants(self) -> None:
        self.assertTrue(is_pii_column("Address"))
        self.assertTrue(is_pii_column("AddressLine1"))
        self.assertTrue(is_pii_column("address_line2"))
        self.assertTrue(is_pii_column("Street"))

    def test_zip_postal(self) -> None:
        self.assertTrue(is_pii_column("Zip"))
        self.assertTrue(is_pii_column("ZipCode"))
        self.assertTrue(is_pii_column("zip_code"))
        self.assertTrue(is_pii_column("PostalCode"))
        self.assertTrue(is_pii_column("postal_code"))

    def test_dob_variants(self) -> None:
        self.assertTrue(is_pii_column("DOB"))
        self.assertTrue(is_pii_column("DateOfBirth"))
        self.assertTrue(is_pii_column("date_of_birth"))
        self.assertTrue(is_pii_column("BirthDate"))

    def test_non_pii_paid_amount(self) -> None:
        self.assertFalse(is_pii_column("PaidAmount"))

    def test_non_pii_patient_id(self) -> None:
        # IDs are deliberately not redacted by default.
        self.assertFalse(is_pii_column("PatientID"))

    def test_non_pii_gender(self) -> None:
        self.assertFalse(is_pii_column("Gender"))

    def test_anchored_no_substring_match(self) -> None:
        # ``Patient_FirstName`` should NOT match anchored ``^first_name$``.
        self.assertFalse(is_pii_column("Patient_FirstName"))
        self.assertFalse(is_pii_column("PatientSSN"))
        self.assertFalse(is_pii_column("EmergencyContactPhone"))

    def test_empty_and_weird_inputs_do_not_raise(self) -> None:
        self.assertFalse(is_pii_column(""))
        # Non-string defensively returns False rather than raising.
        self.assertFalse(is_pii_column(None))  # type: ignore[arg-type]


class RedactSampleValuesTests(unittest.TestCase):
    def test_redacts_pii_column(self) -> None:
        result = redact_sample_values("FirstName", ["Rick", "Mary", "Gregory"])
        self.assertEqual(result, [REDACTION_PLACEHOLDER] * 3)

    def test_leaves_non_pii_unchanged(self) -> None:
        result = redact_sample_values("Gender", ["Female", "Male"])
        self.assertEqual(result, ["Female", "Male"])

    def test_preserves_none_values(self) -> None:
        result = redact_sample_values("FirstName", ["Rick", None, "Mary"])
        self.assertEqual(
            result,
            [REDACTION_PLACEHOLDER, None, REDACTION_PLACEHOLDER],
        )

    def test_returns_new_list(self) -> None:
        original = ["Female", "Male"]
        result = redact_sample_values("Gender", original)
        self.assertIsNot(result, original)
        # Mutating result must not affect original.
        result.append("Other")
        self.assertEqual(original, ["Female", "Male"])

    def test_empty_values(self) -> None:
        self.assertEqual(redact_sample_values("FirstName", []), [])
        self.assertEqual(redact_sample_values("Gender", []), [])

    def test_custom_placeholder(self) -> None:
        result = redact_sample_values(
            "SSN", ["123-45-6789"], placeholder="***"
        )
        self.assertEqual(result, ["***"])


class RedactRowDictTests(unittest.TestCase):
    def test_redacts_pii_keys(self) -> None:
        row = {"PatientID": "P1", "FirstName": "Rick", "Gender": "Female"}
        result = redact_row_dict(row)
        self.assertEqual(
            result,
            {
                "PatientID": "P1",
                "FirstName": REDACTION_PLACEHOLDER,
                "Gender": "Female",
            },
        )

    def test_does_not_mutate_input(self) -> None:
        row = {"PatientID": "P1", "FirstName": "Rick", "Gender": "Female"}
        snapshot = dict(row)
        _ = redact_row_dict(row)
        self.assertEqual(row, snapshot)

    def test_preserves_none_in_pii_column(self) -> None:
        row = {"FirstName": None, "Gender": "Female"}
        result = redact_row_dict(row)
        self.assertEqual(result, {"FirstName": None, "Gender": "Female"})

    def test_returns_new_dict(self) -> None:
        row = {"Gender": "Female"}
        result = redact_row_dict(row)
        self.assertIsNot(result, row)

    def test_anchored_pii_passes_through(self) -> None:
        row = {"Patient_FirstName": "Rick", "PatientID": "P1"}
        result = redact_row_dict(row)
        # ``Patient_FirstName`` is NOT redacted because it's not anchored-equal
        # to ``first_name``.
        self.assertEqual(
            result, {"Patient_FirstName": "Rick", "PatientID": "P1"}
        )


class RedactRowsTests(unittest.TestCase):
    def test_redacts_each_row(self) -> None:
        rows = [
            {"PatientID": "P1", "FirstName": "Rick", "SSN": "111-11-1111"},
            {"PatientID": "P2", "FirstName": "Mary", "SSN": "222-22-2222"},
            {"PatientID": "P3", "FirstName": None, "SSN": "333-33-3333"},
        ]
        result = redact_rows(rows)
        self.assertEqual(
            result,
            [
                {
                    "PatientID": "P1",
                    "FirstName": REDACTION_PLACEHOLDER,
                    "SSN": REDACTION_PLACEHOLDER,
                },
                {
                    "PatientID": "P2",
                    "FirstName": REDACTION_PLACEHOLDER,
                    "SSN": REDACTION_PLACEHOLDER,
                },
                {
                    "PatientID": "P3",
                    "FirstName": None,
                    "SSN": REDACTION_PLACEHOLDER,
                },
            ],
        )

    def test_no_mutation_of_input_rows(self) -> None:
        rows = [{"FirstName": "Rick"}, {"FirstName": "Mary"}]
        snapshots = [dict(r) for r in rows]
        _ = redact_rows(rows)
        for original, snapshot in zip(rows, snapshots):
            self.assertEqual(original, snapshot)

    def test_empty_input(self) -> None:
        self.assertEqual(redact_rows([]), [])


class CustomPatternsTests(unittest.TestCase):
    def test_narrowed_patterns_only_redact_ssn(self) -> None:
        custom: tuple[str, ...] = (r"^ssn$",)
        self.assertTrue(is_pii_column("SSN", patterns=custom))
        self.assertTrue(is_pii_column("ssn", patterns=custom))
        # Under the custom config, FirstName is NOT redacted.
        self.assertFalse(is_pii_column("FirstName", patterns=custom))
        self.assertFalse(is_pii_column("Email", patterns=custom))

        row = {"SSN": "111-11-1111", "FirstName": "Rick", "Gender": "Female"}
        result = redact_row_dict(row, patterns=custom)
        self.assertEqual(
            result,
            {
                "SSN": REDACTION_PLACEHOLDER,
                "FirstName": "Rick",
                "Gender": "Female",
            },
        )

    def test_default_patterns_tuple_is_immutable(self) -> None:
        self.assertIsInstance(DEFAULT_PII_COLUMN_PATTERNS, tuple)


class PciColumnTests(unittest.TestCase):
    def test_cardholder_columns_are_pii(self) -> None:
        for col in (
            "CardNumber", "card_number", "credit_card_number", "PAN",
            "CVV", "cvc", "CardExpiry", "CardholderName", "Track1Data",
            "IBAN", "RoutingNumber", "bank_account_number",
        ):
            self.assertTrue(is_pii_column(col), col)

    def test_generic_business_columns_stay_clear(self) -> None:
        for col in ("PaidAmount", "OrderId", "expiry_date", "panel_id", "company"):
            self.assertFalse(is_pii_column(col), col)

    def test_card_values_redacted_in_rows(self) -> None:
        rows = [{"CardNumber": "4111111111111111", "Amount": 12.5}]
        result = redact_rows(rows)
        self.assertEqual(result[0]["CardNumber"], REDACTION_PLACEHOLDER)
        self.assertEqual(result[0]["Amount"], 12.5)


class QuasiIdentifierAgeTests(unittest.TestCase):
    def test_age_column_detection(self) -> None:
        for col in ("age", "Age", "AGE", "patient_age", "PatientAge", "age_years"):
            self.assertTrue(is_age_column(col), col)
        for col in ("age_band", "average_age", "page", "agent"):
            self.assertFalse(is_age_column(col), col)

    def test_bucket_age_value_caps_over_89(self) -> None:
        self.assertEqual(bucket_age_value(99), AGE_OVERFLOW_LABEL)
        self.assertEqual(bucket_age_value(90), AGE_OVERFLOW_LABEL)
        self.assertEqual(bucket_age_value(89), 89)
        self.assertEqual(bucket_age_value(0), 0)
        self.assertEqual(bucket_age_value("95"), AGE_OVERFLOW_LABEL)
        self.assertEqual(bucket_age_value("42"), "42")
        self.assertIsNone(bucket_age_value(None))
        self.assertEqual(bucket_age_value("unknown"), "unknown")

    def test_row_dict_buckets_age_but_keeps_other_columns(self) -> None:
        row = {"Age": 97, "Gender": "Female", "PaidAmount": 167.59}
        result = redact_row_dict(row)
        self.assertEqual(result["Age"], AGE_OVERFLOW_LABEL)
        self.assertEqual(result["Gender"], "Female")
        self.assertEqual(result["PaidAmount"], 167.59)

    def test_sample_values_bucket_ages(self) -> None:
        self.assertEqual(
            redact_sample_values("age", [88, 90, None, 102]),
            [88, AGE_OVERFLOW_LABEL, None, AGE_OVERFLOW_LABEL],
        )


class GateRedactionSyncTests(unittest.TestCase):
    """Every PCI gate category must also be covered by display redaction."""

    def test_pci_gate_columns_are_redacted_on_display(self) -> None:
        from core.governance.phi_gate import pci_identifier_category

        representative = (
            "card_number", "pan", "cvv", "card_expiry",
            "cardholder_name", "track1_data", "iban",
        )
        for col in representative:
            self.assertIsNotNone(pci_identifier_category(col), col)
            self.assertTrue(is_pii_column(col), col)

    def test_display_pci_patterns_are_full_set_equal_to_gate(self) -> None:
        """Full-set equality, not a spot-check: the PCI patterns the display
        redactor uses must equal the flattened phi_gate source of truth, so they
        can never drift (the old copy had silently dropped cid/magstripe/exp/swift).
        """
        from core.governance.phi_gate import PCI_IDENTIFIER_PATTERNS
        from core.onboarding.kpi import pii_redaction

        gate_pci = {
            p for patterns in PCI_IDENTIFIER_PATTERNS.values() for p in patterns
        }
        display_pci = set(pii_redaction._PCI_COLUMN_PATTERNS)
        self.assertEqual(display_pci, gate_pci)
        self.assertTrue(gate_pci.issubset(set(pii_redaction.DEFAULT_PII_COLUMN_PATTERNS)))

    def test_previously_drifted_columns_now_redacted(self) -> None:
        """The exact columns the audit found gated-but-not-redacted."""
        for col in ("cid", "magstripe", "magstripe_data", "exp_month", "exp_year", "swift_code"):
            self.assertTrue(is_pii_column(col), col)


if __name__ == "__main__":
    unittest.main()
