"""Tests for :mod:`core.onboarding.kpi.pii_redaction`."""

from __future__ import annotations

import unittest

from core.onboarding.kpi.pii_redaction import (
    DEFAULT_PII_COLUMN_PATTERNS,
    REDACTION_PLACEHOLDER,
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


if __name__ == "__main__":
    unittest.main()
