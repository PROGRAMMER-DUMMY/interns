import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from core.onboarding.data_model.image_parser import DataModelImageParser, _parse_ocr_schema_text


PNG_1X1 = bytes.fromhex(
    "89504e470d0a1a0a0000000d49484452000000010000000108060000001f15c489"
    "0000000a49444154789c636000000200015d0b2a0000000049454e44ae426082"
)


class DataModelImageParserTests(unittest.TestCase):
    def test_parses_ocr_text_into_schema_candidates(self) -> None:
        text = """
        Dim_Patient Dim_Department
        SK Patient ID Dept ID
        First_Name Fact. Name
        Last_Name PK Data_Source
        DOB FK Patient_ID
        Gender FK DeptID
        Data_Source FK Provider_ID
        FK ICD_Code
        Encounter_ID
        Claims_ID
        Payor_Name Dim_Provider
        Dim_Diagnosis Service_Date PK Provider ID
        ICD_Code Charge_Amt Provider_Name NPI
        Description Payor_Paid_Amt
        ICD_Type Adjustment_Amt FK Dept_ID
        Patient_Paid_Amt
        Claim_Date
        Paid_Date
        Data_Source
        Dim_NPI
        PK NPI
        FirstName
        LastName
        Position
        Organisation Name
        """

        parsed = _parse_ocr_schema_text(text)

        table_names = {item["table_name"] for item in parsed["tables"]}
        self.assertIn("Fact", table_names)
        self.assertIn("Dim_Patient", table_names)
        self.assertIn("Dim_Department", table_names)
        self.assertIn("Dim_Provider", table_names)
        self.assertIn("Dim_Diagnosis", table_names)
        self.assertIn("Dim_NPI", table_names)
        self.assertEqual(parsed["detected_schema_types"], ["star_schema"])
        relationship_ids = {item["relationship_id"] for item in parsed["relationships"]}
        self.assertTrue(any("patient" in item for item in relationship_ids))
        self.assertIn("fact__deptid__dim_department__dept_id", relationship_ids)
        self.assertIn("fact__provider_id__dim_provider__provider_id", relationship_ids)
        self.assertFalse(any("encounter_id" in item for item in relationship_ids))
        self.assertFalse(any("claims_id" in item for item in relationship_ids))
        self.assertTrue(all(not item["executable_usage_allowed"] for item in parsed["relationships"]))

    def test_writes_review_gated_sidecars_under_interns(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            workspace = root / "workspaces" / "demo"
            docs = workspace / "docs"
            docs.mkdir(parents=True)
            (docs / "DataModel.png").write_bytes(PNG_1X1)

            result = DataModelImageParser(root, "workspaces/demo").parse(local_ocr="off")

            self.assertEqual(result.image_count, 1)
            self.assertEqual(result.status, "needs_user_review")
            sidecar_path = root / result.generated_sidecars[0]
            report_path = root / result.report_paths[0]
            current_path = root / result.current_json_path
            self.assertTrue(sidecar_path.exists())
            self.assertTrue(report_path.exists())
            self.assertTrue(current_path.exists())

            sidecar = json.loads(sidecar_path.read_text(encoding="utf-8"))
            self.assertEqual(sidecar["source_image"], "workspaces/demo/docs/DataModel.png")
            self.assertEqual(sidecar["image_metadata"]["width_px"], 1)
            self.assertEqual(sidecar["image_metadata"]["height_px"], 1)
            self.assertEqual(sidecar["approval_state"], "needs_parser_or_manual_sidecar")
            self.assertFalse(sidecar["executable_usage_allowed"])
            self.assertEqual(sidecar["parsers"]["ocr_layout"]["state"], "disabled")
            self.assertFalse(sidecar["parsers"]["ocr_layout"]["auto_install"]["attempted"])
            self.assertEqual(sidecar["parsers"]["multimodal_vision"]["state"], "not_requested")
            self.assertIn("catalog/schema evidence", sidecar["promotion_policy"]["relationship_candidate"])

            current = json.loads(current_path.read_text(encoding="utf-8"))
            self.assertFalse(current["summary"]["remote_vision_called"])
            self.assertFalse(current["summary"]["executable_usage_allowed"])
            self.assertEqual(current["summary"]["needs_manual_review"], 1)

    def test_remote_vision_requires_sensitive_upload_confirmation(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            workspace = root / "workspaces" / "healthcare"
            docs = workspace / "docs"
            docs.mkdir(parents=True)
            (docs / "rcm_schema_diagram.png").write_bytes(PNG_1X1)

            result = DataModelImageParser(root, "workspaces/healthcare").parse(
                allow_remote_vision=True,
                confirm_sensitive_upload=False,
                local_ocr="off",
            )

            sidecar = json.loads((root / result.generated_sidecars[0]).read_text(encoding="utf-8"))
            self.assertEqual(
                sidecar["parsers"]["multimodal_vision"]["state"],
                "blocked_missing_sensitive_upload_confirmation",
            )
            self.assertFalse(sidecar["parsers"]["multimodal_vision"]["remote_call_made"])

    def test_missing_ocr_can_request_auto_install_without_silent_mutation(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            workspace = root / "workspaces" / "demo"
            docs = workspace / "docs"
            docs.mkdir(parents=True)
            (docs / "DataModel.png").write_bytes(PNG_1X1)

            with patch("core.onboarding.data_model.image_parser._find_tesseract", return_value=None), patch(
                "core.onboarding.data_model.image_parser._tesseract_install_command",
                return_value=None,
            ):
                result = DataModelImageParser(root, "workspaces/demo").parse(
                    local_ocr="auto",
                    auto_install_ocr=True,
                )

            sidecar = json.loads((root / result.generated_sidecars[0]).read_text(encoding="utf-8"))
            auto_install = sidecar["parsers"]["ocr_layout"]["auto_install"]
            self.assertTrue(auto_install["requested"])
            self.assertFalse(auto_install["attempted"])
            self.assertEqual(auto_install["state"], "no_supported_package_manager")

    def test_profile_matching_marks_candidates_without_executable_promotion(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            workspace = root / "workspaces" / "demo"
            docs = workspace / "docs"
            profiles = workspace / "interns" / "generated" / "profiles"
            docs.mkdir(parents=True)
            profiles.mkdir(parents=True)
            (docs / "DataModel.png").write_bytes(PNG_1X1)
            (profiles / "profile_index.json").write_text(
                json.dumps(
                    {
                        "profiles": [
                            {
                                "path": "workspaces/demo/datasets/patients.csv",
                                "schema": {"Patient_ID": "int", "First_Name": "str"},
                            },
                            {
                                "path": "workspaces/demo/datasets/departments.csv",
                                "schema": {"Dept_ID": "int", "Name": "str"},
                            },
                            {
                                "path": "workspaces/demo/datasets/providers.csv",
                                "schema": {"Provider_ID": "int", "Dept_ID": "int"},
                            },
                            {
                                "path": "workspaces/demo/datasets/diagnosis.csv",
                                "schema": {"ICD_Code": "str", "Description": "str"},
                            },
                        ]
                    }
                ),
                encoding="utf-8",
            )

            with patch(
                "core.onboarding.data_model.image_parser._run_local_ocr",
                return_value={
                    "state": "completed",
                    "text": (
                        "Dim_Patient Dim_Department\n"
                        "SK Patient ID Dept ID\n"
                        "Fact\n"
                        "FK Patient_ID\n"
                        "FK DeptID\n"
                        "FK Provider_ID\n"
                        "FK ICD_Code\n"
                        "Dim_Provider\n"
                        "PK Provider ID\n"
                        "Dim_Diagnosis\n"
                        "PK ICD_Code\n"
                    ),
                    "confidence": 0.5,
                    "engine": "test",
                },
            ):
                result = DataModelImageParser(root, "workspaces/demo").parse(local_ocr="auto")

            sidecar = json.loads((root / result.generated_sidecars[0]).read_text(encoding="utf-8"))
            self.assertEqual(sidecar["profile_matching"]["state"], "matched")
            self.assertGreaterEqual(len(sidecar["profile_matching"]["column_matches"]), 4)
            matched_relationships = [
                item
                for item in sidecar["relationships"]
                if item["state"] == "candidate_image_profile_matched"
            ]
            self.assertGreaterEqual(len(matched_relationships), 3)
            self.assertTrue(all(not item["executable_usage_allowed"] for item in matched_relationships))


if __name__ == "__main__":
    unittest.main()
