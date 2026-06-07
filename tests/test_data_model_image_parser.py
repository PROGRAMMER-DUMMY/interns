import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from core.onboarding.data_model.image_parser import DataModelImageParser, _parse_ocr_schema_text
from core.onboarding.workspace.onboarding import WorkspaceOnboarder


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


class OnboardingDiagramParseIntegrationTests(unittest.TestCase):
    """BUG-023: onboard-workspace must invoke DataModelImageParser so that
    relationship-contract builder can consume diagram sidecars."""

    # Minimal valid 1x1 PNG reused from the sibling tests above.
    PNG_1X1 = PNG_1X1

    def _create_workspace(self, root: Path, *, with_png: bool) -> Path:
        workspace = root / "workspaces" / "demo"
        (workspace / "datasets").mkdir(parents=True)
        (workspace / "docs").mkdir(parents=True)
        (workspace / "datasets" / "transactions.csv").write_text(
            "ClaimID,PaidAmount\nC1,10.50\nC2,20.25\n",
            encoding="utf-8",
        )
        (workspace / "docs" / "kpi_registry.csv").write_text(
            "Key business question,Metric\nTotal paid,sum(PaidAmount)\n",
            encoding="utf-8",
        )
        if with_png:
            (workspace / "docs" / "DataModel.png").write_bytes(self.PNG_1X1)
        return workspace

    def test_onboarding_produces_diagram_sidecar_when_png_present(self):
        """With a DataModel.png under docs/, onboarding must write the sidecar
        directory and include its path in the result artifacts."""
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            workspace = self._create_workspace(root, with_png=True)

            result = WorkspaceOnboarder(root, "workspaces/demo", sample_rows=10).run()

            sidecar_dir = workspace / "interns" / "generated" / "data_model_images"
            self.assertTrue(
                sidecar_dir.exists(),
                "data_model_images sidecar directory must be created by onboarding",
            )
            sidecars = list(sidecar_dir.glob("*.model.json"))
            self.assertEqual(len(sidecars), 1, "exactly one sidecar for DataModel.png")

            # Sidecar must be review-gated (not executable).
            sidecar = json.loads(sidecars[0].read_text(encoding="utf-8"))
            self.assertFalse(sidecar["executable_usage_allowed"])
            self.assertEqual(sidecar["source_image"], "workspaces/demo/docs/DataModel.png")

            # Artifact paths must be surfaced in the onboarding result.
            self.assertIn("diagram_current_json", result.artifacts)
            self.assertIn("diagram_sidecar_dir", result.artifacts)

    def test_onboarding_skips_gracefully_when_no_png(self):
        """Without any data-model image, onboarding must still succeed and must
        NOT create the sidecar directory."""
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            workspace = self._create_workspace(root, with_png=False)

            result = WorkspaceOnboarder(root, "workspaces/demo", sample_rows=10).run()

            sidecar_dir = workspace / "interns" / "generated" / "data_model_images"
            # No images -> DataModelImageParser returns image_count=0; the sidecar
            # dir is only created when images are found.
            self.assertNotIn("diagram_sidecar_dir", result.artifacts)
            # No hard failures expected.
            diagram_errors = [w for w in result.warnings if "data_model_image_parse_failed" in w]
            self.assertEqual(diagram_errors, [])

    def test_onboarding_ocr_unavailable_degrades_gracefully(self):
        """When Tesseract is absent the sidecar is still written (OCR state is
        provider_not_configured) but onboarding does not hard-fail; it surfaces
        a [~] warning and the sidecar dir is still present."""
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            workspace = self._create_workspace(root, with_png=True)

            with patch(
                "core.onboarding.data_model.image_parser._find_tesseract",
                return_value=None,
            ):
                result = WorkspaceOnboarder(root, "workspaces/demo", sample_rows=10).run()

            # No hard failure.
            hard_failures = [w for w in result.warnings if "data_model_image_parse_failed" in w]
            self.assertEqual(hard_failures, [])

            sidecar_dir = workspace / "interns" / "generated" / "data_model_images"
            self.assertTrue(sidecar_dir.exists(), "sidecar dir created even without OCR")
            sidecars = list(sidecar_dir.glob("*.model.json"))
            self.assertEqual(len(sidecars), 1)

            sidecar = json.loads(sidecars[0].read_text(encoding="utf-8"))
            ocr_state = sidecar["parsers"]["ocr_layout"]["state"]
            # OCR was attempted (auto) but found no Tesseract -> provider_not_configured.
            self.assertIn(ocr_state, {"provider_not_configured", "disabled"})

            # A [~] advisory warning should appear when OCR is unavailable.
            ocr_warnings = [
                w for w in result.warnings
                if "data_model_image_ocr_unavailable" in w or "ocr" in w.lower()
            ]
            self.assertTrue(
                len(ocr_warnings) >= 1,
                f"expected at least one OCR-unavailable advisory warning; got: {result.warnings}",
            )

    def test_onboarding_is_idempotent_for_diagram_sidecars(self):
        """Re-running onboarding refreshes sidecars instead of duplicating them."""
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            workspace = self._create_workspace(root, with_png=True)

            WorkspaceOnboarder(root, "workspaces/demo", sample_rows=10).run()
            WorkspaceOnboarder(root, "workspaces/demo", sample_rows=10).run()

            sidecar_dir = workspace / "interns" / "generated" / "data_model_images"
            sidecars = list(sidecar_dir.glob("*.model.json"))
            self.assertEqual(
                len(sidecars),
                1,
                "re-running onboarding must refresh sidecars, not duplicate them",
            )


if __name__ == "__main__":
    unittest.main()
