from __future__ import annotations

import unittest
from pathlib import Path

from core.medallion.design_naming import (
    detect_natural_key,
    detect_watermark,
    logical_entity_from_path,
    safe_relative_posix,
    source_system_from_path,
    source_system_from_silver_source,
)


class MedallionDesignNamingTests(unittest.TestCase):
    def test_logical_entity_strips_source_suffixes(self):
        self.assertEqual(logical_entity_from_path("datasets/claims_hospital_a.csv"), "claim")

    def test_source_system_from_path_prefers_hospital_token(self):
        self.assertEqual(
            source_system_from_path("datasets/EMR/trendytech-hospital-b/claims.csv"),
            "hospital_b",
        )

    def test_detect_key_and_watermark(self):
        self.assertEqual(detect_natural_key({"ClaimID": "str"}, "claim"), ["ClaimID"])
        self.assertEqual(detect_watermark({"UpdatedDate": "date"}), "UpdatedDate")

    def test_source_system_and_relative_path_helpers(self):
        self.assertEqual(source_system_from_silver_source("claim__hospital_a"), "hospital_a")
        self.assertEqual(safe_relative_posix(Path("a/b.csv"), Path(".")), "a/b.csv")


if __name__ == "__main__":
    unittest.main()
