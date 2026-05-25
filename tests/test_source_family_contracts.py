from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from core.onboarding.source_family_contracts import SourceFamilyContractBuilder


class SourceFamilyContractTests(unittest.TestCase):
    def test_groups_dated_profiles_and_detects_schema_drift(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            workspace = root / "workspaces" / "cms"
            profiles_dir = workspace / "interns" / "generated" / "profiles"
            docs_dir = workspace / "docs"
            profiles_dir.mkdir(parents=True)
            docs_dir.mkdir(parents=True)
            path_2023 = (
                r"D:\Cold_Storage\raw\Medicare Part D Prescribers - by Provider"
                r"\MUP_DPR_RY25_P04_V10_DY23_NPI.csv"
            )
            path_2022 = (
                r"D:\Cold_Storage\raw\Medicare Part D Prescribers - by Provider"
                r"\MUP_DPR_RY25_P04_V20_DY22_NPI.csv"
            )
            (profiles_dir / "profile_index.json").write_text(
                json.dumps(
                    {
                        "artifact_type": "profile_index.json",
                        "profiles": [
                            {
                                "path": path_2023,
                                "format": "csv",
                                "row_count": 10,
                                "size_bytes": 100,
                                "schema": {
                                    "Prscrbr_NPI": "Int64",
                                    "Tot_Clms": "Int64",
                                    "Avg_Pymt_Amt": "Float64",
                                },
                                "columns": [{"name": "Prscrbr_NPI", "sample_values": ["123"]}],
                            },
                            {
                                "path": path_2022,
                                "format": "csv",
                                "row_count": 8,
                                "size_bytes": 90,
                                "schema": {
                                    "Prscrbr_NPI": "Int64",
                                    "Tot_Clms": "Float64",
                                },
                            },
                        ],
                    }
                ),
                encoding="utf-8",
            )
            (docs_dir / "source_selection.json").write_text(
                json.dumps(
                    {
                        "sources": [
                            {
                                "id": "part_d_2023",
                                "path": path_2023,
                                "target_kind": "dataset",
                                "discovery_group": "Medicare Part D Prescribers - by Provider",
                            },
                            {
                                "id": "part_d_2022",
                                "path": path_2022,
                                "target_kind": "dataset",
                                "discovery_group": "Medicare Part D Prescribers - by Provider",
                            },
                            {
                                "id": "part_d_dictionary",
                                "path": r"D:\Cold_Storage\raw\Medicare Part D Prescribers - by Provider\dict.pdf",
                                "target_kind": "doc",
                                "target_name": "dictionary",
                                "discovery_group": "Medicare Part D Prescribers - by Provider",
                            },
                        ]
                    }
                ),
                encoding="utf-8",
            )

            result = SourceFamilyContractBuilder(root, "workspaces/cms").build()

            self.assertEqual(result.family_count, 1)
            self.assertEqual(result.profile_count, 2)
            self.assertEqual(result.schema_version_count, 2)
            contract = json.loads((root / result.json_path).read_text(encoding="utf-8"))
            family = contract["families"][0]
            self.assertEqual(family["source_group"], "Medicare Part D Prescribers - by Provider")
            self.assertTrue(family["schema_drift"]["has_schema_drift"])
            self.assertTrue(family["schema_drift"]["has_type_drift"])
            self.assertIn("report_year", family["bronze_plan"]["partition_columns"])
            self.assertEqual(
                sorted(family["observed_release_tokens"]["data_year"]),
                [2022, 2023],
            )
            self.assertEqual(len(family["documentation"]), 1)

    def test_contract_keeps_profile_payload_compact(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            workspace = root / "workspaces" / "cms"
            profiles_dir = workspace / "interns" / "generated" / "profiles"
            profiles_dir.mkdir(parents=True)
            (profiles_dir / "profile_index.json").write_text(
                json.dumps(
                    {
                        "profiles": [
                            {
                                "path": r"D:\Cold_Storage\raw\PBJ\PBJ_dailyNurseStaffing_CY2025Q1.csv",
                                "format": "csv",
                                "schema": {"PROVNUM": "String", "WORKDATE": "String"},
                                "columns": [
                                    {
                                        "name": "PROVNUM",
                                        "sample_values": ["005001", "005002"],
                                        "exact_min": "005001",
                                    }
                                ],
                            }
                        ]
                    }
                ),
                encoding="utf-8",
            )

            result = SourceFamilyContractBuilder(root, "workspaces/cms").build()

            contract_text = (root / result.json_path).read_text(encoding="utf-8")
            self.assertIn('"profile_payloads_duplicated": false', contract_text)
            self.assertNotIn("sample_values", contract_text)
            self.assertNotIn("005001", contract_text)


if __name__ == "__main__":
    unittest.main()
