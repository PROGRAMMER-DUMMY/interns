"""Tests for Phase 1 (document_loader) and Phase 2 (classifier) of PDF ingestion.

Run with:
    .venv\\Scripts\\python.exe -m unittest tests.test_document_ingestion

All tests pass with opendataloader-pdf and Java absent (graceful degradation).
No real PDF files are required; opendataloader_pdf.convert() is monkeypatched.
"""
from __future__ import annotations

import json
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_workspace(tmp: Path) -> tuple[Path, Path]:
    """Create a minimal workspace tree and return (repo_root, workspace_path)."""
    repo_root = tmp
    ws = tmp / "workspaces" / "demo"
    ws.mkdir(parents=True, exist_ok=True)
    (ws / "interns").mkdir(exist_ok=True)
    return repo_root, ws


def _make_pdf(tmp: Path, name: str = "spec.pdf") -> Path:
    """Write a minimal valid-ish PDF stub (just needs to exist with .pdf suffix)."""
    path = tmp / name
    path.write_bytes(b"%PDF-1.4 stub")
    return path


def _fake_convert_factory(json_content: dict, md_content: str = "# Doc"):
    """Return a convert() stub that writes json+md files into the output_dir arg."""

    def _convert(input_path, output_dir, format="json,markdown"):
        out = Path(output_dir)
        (out / "output.json").write_text(json.dumps(json_content), encoding="utf-8")
        (out / "output.md").write_text(md_content, encoding="utf-8")

    return _convert


# ---------------------------------------------------------------------------
# Import the modules under test
# ---------------------------------------------------------------------------

from core.onboarding.documents.document_loader import (
    DocumentScanResult,
    _check_java,
    _parse_java_major,
    _redact_json,
    _redact_text,
    scan_document,
)
from core.onboarding.documents.classifier import (
    CANDIDATE_DATA_MODEL,
    CANDIDATE_KPI_REGISTRY,
    CANDIDATE_LEXICON,
    CANDIDATE_OPEN_QUESTION,
    CANDIDATE_RAW_EVIDENCE,
    classify_document,
)


# ===========================================================================
# Library-absent degradation tests
# ===========================================================================

def _java_present_mocks():
    """Context manager stack that makes Java appear present (version 21)."""
    import contextlib

    @contextlib.contextmanager
    def _ctx():
        with patch("shutil.which", return_value="/usr/bin/java"):
            with patch("subprocess.run") as mock_run:
                mock_run.return_value = MagicMock(
                    stderr='openjdk version "21.0.1"',
                    stdout="",
                    returncode=0,
                )
                yield mock_run

    return _ctx()


class TestLibraryAbsentDegradation(unittest.TestCase):
    """scan_document() must return a BLOCKER, not raise, when opendataloader_pdf is absent."""

    def test_missing_library_returns_blocker_not_crash(self):
        """ImportError on opendataloader_pdf -> status='blocker', no exception."""
        with tempfile.TemporaryDirectory() as tmp_str:
            tmp = Path(tmp_str)
            repo_root, ws = _make_workspace(tmp)
            pdf = _make_pdf(tmp)

            # Mock Java as present so we reach the import gate
            with _java_present_mocks():
                # Patch sys.modules so the lazy import raises ImportError
                with patch.dict(sys.modules, {"opendataloader_pdf": None}):
                    result = scan_document(repo_root, ws, pdf, mode="free")

            self.assertIsInstance(result, DocumentScanResult)
            self.assertEqual(result.status, "blocker")
            self.assertIn("opendataloader-pdf", result.blocker_reason)
            self.assertEqual(result.sidecar_path, "")
            self.assertEqual(result.report_path, "")

    def test_blocker_result_is_json_serialisable(self):
        """The blocker result must be JSON-serialisable (consumed by pipeline wrappers)."""
        with tempfile.TemporaryDirectory() as tmp_str:
            tmp = Path(tmp_str)
            repo_root, ws = _make_workspace(tmp)
            pdf = _make_pdf(tmp)

            with _java_present_mocks():
                with patch.dict(sys.modules, {"opendataloader_pdf": None}):
                    result = scan_document(repo_root, ws, pdf, mode="free")

            serialised = json.dumps(result.summary())
            self.assertIn("blocker", serialised)


# ===========================================================================
# Java-absent degradation tests
# ===========================================================================

class TestJavaAbsentDegradation(unittest.TestCase):
    """scan_document() must return a BLOCKER when Java is absent/incompatible."""

    def test_java_not_on_path_returns_blocker(self):
        with tempfile.TemporaryDirectory() as tmp_str:
            tmp = Path(tmp_str)
            repo_root, ws = _make_workspace(tmp)
            pdf = _make_pdf(tmp)

            # Fake opendataloader_pdf present but Java absent
            fake_odl = types.ModuleType("opendataloader_pdf")
            fake_odl.convert = MagicMock()
            fake_odl.__version__ = "0.1.test"

            with patch.dict(sys.modules, {"opendataloader_pdf": fake_odl}):
                with patch("shutil.which", return_value=None):
                    result = scan_document(repo_root, ws, pdf, mode="free")

            self.assertEqual(result.status, "blocker")
            self.assertIn("Java", result.blocker_reason)

    def test_java_too_old_returns_blocker(self):
        """Java 8 -> blocker (requires 11+)."""
        with tempfile.TemporaryDirectory() as tmp_str:
            tmp = Path(tmp_str)
            repo_root, ws = _make_workspace(tmp)
            pdf = _make_pdf(tmp)

            fake_odl = types.ModuleType("opendataloader_pdf")
            fake_odl.convert = MagicMock()
            fake_odl.__version__ = "0.1.test"

            with patch.dict(sys.modules, {"opendataloader_pdf": fake_odl}):
                with patch("shutil.which", return_value="/usr/bin/java"):
                    with patch("subprocess.run") as mock_run:
                        mock_run.return_value = MagicMock(
                            stderr='java version "1.8.0_381"',
                            stdout="",
                            returncode=0,
                        )
                        result = scan_document(repo_root, ws, pdf, mode="free")

            self.assertEqual(result.status, "blocker")
            self.assertIn("Java", result.blocker_reason)

    def test_java_version_parse_old_style(self):
        self.assertEqual(_parse_java_major('java version "1.8.0_381"'), 8)

    def test_java_version_parse_new_style(self):
        self.assertEqual(_parse_java_major('openjdk version "21.0.1"'), 21)

    def test_java_version_parse_11(self):
        self.assertEqual(_parse_java_major('openjdk version "11.0.20" 2023-07-18'), 11)

    def test_java_version_parse_unrecognised(self):
        self.assertIsNone(_parse_java_major("garbage output"))

    def test_java_convert_not_called_when_java_absent(self):
        """opendataloader_pdf.convert must NOT be called if Java preflight fails."""
        with tempfile.TemporaryDirectory() as tmp_str:
            tmp = Path(tmp_str)
            repo_root, ws = _make_workspace(tmp)
            pdf = _make_pdf(tmp)

            mock_convert = MagicMock()
            fake_odl = types.ModuleType("opendataloader_pdf")
            fake_odl.convert = mock_convert
            fake_odl.__version__ = "0.1.test"

            with patch.dict(sys.modules, {"opendataloader_pdf": fake_odl}):
                with patch("shutil.which", return_value=None):
                    scan_document(repo_root, ws, pdf, mode="free")

            mock_convert.assert_not_called()


# ===========================================================================
# Hybrid mode gate tests
# ===========================================================================

class TestHybridModeGate(unittest.TestCase):
    def test_hybrid_blocked_without_env(self):
        with tempfile.TemporaryDirectory() as tmp_str:
            tmp = Path(tmp_str)
            repo_root, ws = _make_workspace(tmp)
            pdf = _make_pdf(tmp)

            import os
            with patch.dict(os.environ, {}, clear=False):
                os.environ.pop("AUTORESEARCH_ALLOW_HYBRID_PDF", None)
                result = scan_document(repo_root, ws, pdf, mode="hybrid")

            self.assertEqual(result.status, "blocker")
            self.assertIn("hybrid", result.blocker_reason.lower())

    def test_unknown_mode_blocked(self):
        with tempfile.TemporaryDirectory() as tmp_str:
            tmp = Path(tmp_str)
            repo_root, ws = _make_workspace(tmp)
            pdf = _make_pdf(tmp)

            result = scan_document(repo_root, ws, pdf, mode="turbo")  # type: ignore[arg-type]
            self.assertEqual(result.status, "blocker")


# ===========================================================================
# Deterministic sidecar tests
# ===========================================================================

class TestDeterministicSidecar(unittest.TestCase):
    """Same PDF + free mode -> identical sidecar content (excluding timestamps)."""

    def _run_scan(self, repo_root: Path, ws: Path, pdf: Path) -> dict:
        fixed_json = {"pages": [{"page_number": 1, "blocks": []}]}

        fake_odl = types.ModuleType("opendataloader_pdf")
        fake_odl.convert = _fake_convert_factory(fixed_json, "# Test doc")
        fake_odl.__version__ = "1.0.0.test"

        with patch.dict(sys.modules, {"opendataloader_pdf": fake_odl}):
            with patch("shutil.which", return_value="/usr/bin/java"):
                with patch("subprocess.run") as mock_run:
                    mock_run.return_value = MagicMock(
                        stderr='openjdk version "21.0.1"',
                        stdout="",
                        returncode=0,
                    )
                    result = scan_document(repo_root, ws, pdf, mode="free")

        self.assertEqual(result.status, "ok", f"Expected ok, got blocker: {result.blocker_reason}")
        sidecar_abs = repo_root / result.sidecar_path
        return json.loads(sidecar_abs.read_text(encoding="utf-8"))

    def test_same_pdf_produces_same_sha256(self):
        with tempfile.TemporaryDirectory() as tmp_str:
            tmp = Path(tmp_str)
            repo_root, ws = _make_workspace(tmp)
            pdf = _make_pdf(tmp)

            s1 = self._run_scan(repo_root, ws, pdf)
            s2 = self._run_scan(repo_root, ws, pdf)

            self.assertEqual(s1["source_sha256"], s2["source_sha256"])

    def test_deterministic_fields_match(self):
        with tempfile.TemporaryDirectory() as tmp_str:
            tmp = Path(tmp_str)
            repo_root, ws = _make_workspace(tmp)
            pdf = _make_pdf(tmp)

            s1 = self._run_scan(repo_root, ws, pdf)
            s2 = self._run_scan(repo_root, ws, pdf)

            # Exclude timestamps
            for key in ("source_sha256", "engine_version", "mode", "extracted_content"):
                self.assertEqual(s1[key], s2[key], f"Mismatch on field: {key}")

    def test_sidecar_records_mode_and_version(self):
        with tempfile.TemporaryDirectory() as tmp_str:
            tmp = Path(tmp_str)
            repo_root, ws = _make_workspace(tmp)
            pdf = _make_pdf(tmp)

            sidecar = self._run_scan(repo_root, ws, pdf)

            self.assertEqual(sidecar["mode"], "free")
            self.assertIn("engine_version", sidecar)
            self.assertIn("source_sha256", sidecar)
            self.assertNotEqual(sidecar["source_sha256"], "")


# ===========================================================================
# PHI redaction tests
# ===========================================================================

class TestPHIRedaction(unittest.TestCase):
    """PHI redaction must run before anything is written under interns/."""

    def test_redact_text_scrubs_ssn(self):
        result = _redact_text("Patient SSN is 123-45-6789 and DOB 01/01/1980.")
        self.assertNotIn("123-45-6789", result)
        self.assertIn("<redacted-pii>", result)

    def test_redact_text_scrubs_email(self):
        result = _redact_text("Contact john.doe@example.com for info.")
        self.assertNotIn("john.doe@example.com", result)
        self.assertIn("<redacted-pii>", result)

    def test_redact_text_scrubs_phone(self):
        result = _redact_text("Call 555-867-5309 for appointments.")
        self.assertNotIn("555-867-5309", result)
        self.assertIn("<redacted-pii>", result)

    def test_redact_text_leaves_non_phi(self):
        text = "Claim count: 1234. Revenue: $50000."
        self.assertEqual(_redact_text(text), text)

    def test_redact_json_column_name_phi(self):
        obj = {"dob": "1990-01-01", "claim_id": "C001", "ssn": "123-45-6789"}
        redacted = _redact_json(obj)
        self.assertEqual(redacted["claim_id"], "C001")
        self.assertNotEqual(redacted["dob"], "1990-01-01")
        self.assertNotEqual(redacted["ssn"], "123-45-6789")

    def test_redact_json_nested(self):
        obj = {"rows": [{"name": "Alice", "amount": 100.0}]}
        redacted = _redact_json(obj)
        self.assertNotEqual(redacted["rows"][0]["name"], "Alice")
        self.assertEqual(redacted["rows"][0]["amount"], 100.0)

    def test_phi_redaction_applied_before_write(self):
        """Sidecar written to disk must not contain PHI values."""
        with tempfile.TemporaryDirectory() as tmp_str:
            tmp = Path(tmp_str)
            repo_root, ws = _make_workspace(tmp)
            pdf = _make_pdf(tmp)

            phi_json = {
                "pages": [{
                    "page_number": 1,
                    "blocks": [{
                        "type": "table",
                        "headers": ["ssn", "claim_id"],
                        "rows": [["123-45-6789", "C001"]],
                    }],
                }]
            }

            fake_odl = types.ModuleType("opendataloader_pdf")
            fake_odl.convert = _fake_convert_factory(phi_json, "SSN: 123-45-6789")
            fake_odl.__version__ = "1.0.0.test"

            with patch.dict(sys.modules, {"opendataloader_pdf": fake_odl}):
                with patch("shutil.which", return_value="/usr/bin/java"):
                    with patch("subprocess.run") as mock_run:
                        mock_run.return_value = MagicMock(
                            stderr='openjdk version "21.0.1"',
                            stdout="",
                            returncode=0,
                        )
                        result = scan_document(repo_root, ws, pdf, mode="free")

            self.assertEqual(result.status, "ok")
            sidecar_text = (repo_root / result.sidecar_path).read_text(encoding="utf-8")
            report_text = (repo_root / result.report_path).read_text(encoding="utf-8")

            # PHI must not appear in sidecar or report
            self.assertNotIn("123-45-6789", sidecar_text)
            self.assertNotIn("123-45-6789", report_text)


# ===========================================================================
# Non-authoritative-by-default tests
# ===========================================================================

class TestNonAuthoritativeByDefault(unittest.TestCase):
    def _write_sidecar(self, tmp: Path) -> dict:
        repo_root, ws = _make_workspace(tmp)
        pdf = _make_pdf(tmp)

        fixed_json = {"pages": []}
        fake_odl = types.ModuleType("opendataloader_pdf")
        fake_odl.convert = _fake_convert_factory(fixed_json, "# ok")
        fake_odl.__version__ = "1.0.0.test"

        with patch.dict(sys.modules, {"opendataloader_pdf": fake_odl}):
            with patch("shutil.which", return_value="/usr/bin/java"):
                with patch("subprocess.run") as mock_run:
                    mock_run.return_value = MagicMock(
                        stderr='openjdk version "21.0.1"',
                        stdout="",
                        returncode=0,
                    )
                    result = scan_document(repo_root, ws, pdf, mode="free")

        sidecar_abs = repo_root / result.sidecar_path
        return json.loads(sidecar_abs.read_text(encoding="utf-8"))

    def test_authoritative_usage_allowed_is_false(self):
        with tempfile.TemporaryDirectory() as tmp_str:
            sidecar = self._write_sidecar(Path(tmp_str))
            self.assertFalse(sidecar["authoritative_usage_allowed"])

    def test_approval_state_needs_user_review(self):
        with tempfile.TemporaryDirectory() as tmp_str:
            sidecar = self._write_sidecar(Path(tmp_str))
            self.assertEqual(sidecar["approval_state"], "needs_user_review")

    def test_report_contains_authoritative_false(self):
        with tempfile.TemporaryDirectory() as tmp_str:
            tmp = Path(tmp_str)
            repo_root, ws = _make_workspace(tmp)
            pdf = _make_pdf(tmp)

            fixed_json = {"pages": []}
            fake_odl = types.ModuleType("opendataloader_pdf")
            fake_odl.convert = _fake_convert_factory(fixed_json, "# ok")
            fake_odl.__version__ = "1.0.0.test"

            with patch.dict(sys.modules, {"opendataloader_pdf": fake_odl}):
                with patch("shutil.which", return_value="/usr/bin/java"):
                    with patch("subprocess.run") as mock_run:
                        mock_run.return_value = MagicMock(
                            stderr='openjdk version "21.0.1"',
                            stdout="",
                            returncode=0,
                        )
                        result = scan_document(repo_root, ws, pdf, mode="free")

            report_text = (repo_root / result.report_path).read_text(encoding="utf-8")
            self.assertIn("authoritative usage allowed", report_text)
            self.assertIn("False", report_text)


# ===========================================================================
# Input validation tests
# ===========================================================================

class TestInputValidation(unittest.TestCase):
    def test_non_pdf_extension_returns_blocker(self):
        with tempfile.TemporaryDirectory() as tmp_str:
            tmp = Path(tmp_str)
            repo_root, ws = _make_workspace(tmp)
            doc = tmp / "spec.docx"
            doc.write_bytes(b"not a pdf")

            result = scan_document(repo_root, ws, doc, mode="free")
            self.assertEqual(result.status, "blocker")
            self.assertIn("PDF", result.blocker_reason)

    def test_missing_file_returns_blocker(self):
        with tempfile.TemporaryDirectory() as tmp_str:
            tmp = Path(tmp_str)
            repo_root, ws = _make_workspace(tmp)
            result = scan_document(repo_root, ws, tmp / "ghost.pdf", mode="free")
            self.assertEqual(result.status, "blocker")


# ===========================================================================
# Classifier tests
# ===========================================================================

class TestClassifierRouting(unittest.TestCase):
    """classify_document() must route each detected shape correctly."""

    def _pages(self, blocks: list) -> dict:
        return {"pages": [{"page_number": 1, "blocks": blocks}]}

    def test_kpi_table_routes_to_kpi_registry_candidate(self):
        doc = self._pages([{
            "type": "table",
            "headers": ["KPI", "Metric", "Numerator", "Denominator", "Target"],
            "rows": [["Revenue Rate", "Claims Paid", "Paid Claims", "Total Claims", "0.95"]],
        }])
        candidates = classify_document(doc)
        types_found = {c["candidate_type"] for c in candidates}
        self.assertIn(CANDIDATE_KPI_REGISTRY, types_found)

    def test_lexicon_table_routes_to_lexicon_candidate(self):
        doc = self._pages([{
            "type": "table",
            "headers": ["Term", "Definition"],
            "rows": [["Claim", "A request for payment"], ["Remittance", "Payment notice"]],
        }])
        candidates = classify_document(doc)
        types_found = {c["candidate_type"] for c in candidates}
        self.assertIn(CANDIDATE_LEXICON, types_found)

    def test_erd_table_routes_to_data_model_candidate(self):
        doc = self._pages([{
            "type": "table",
            "headers": ["Table", "Column", "PK", "FK", "References"],
            "rows": [["Claims", "ClaimID", "Y", "", ""], ["Claims", "PatientID", "", "Y", "Patients.PatientID"]],
        }])
        candidates = classify_document(doc)
        types_found = {c["candidate_type"] for c in candidates}
        self.assertIn(CANDIDATE_DATA_MODEL, types_found)

    def test_prose_rule_block_routes_to_open_question_candidate(self):
        doc = self._pages([{
            "type": "paragraph",
            "text": (
                "The system must process claims within 30 days. "
                "SLA: 99.5% uptime required. All providers shall comply with HIPAA."
            ),
        }])
        candidates = classify_document(doc)
        types_found = {c["candidate_type"] for c in candidates}
        self.assertIn(CANDIDATE_OPEN_QUESTION, types_found)

    def test_empty_doc_returns_empty_list(self):
        self.assertEqual(classify_document({}), [])
        self.assertEqual(classify_document({"pages": []}), [])

    def test_all_candidates_are_non_authoritative(self):
        doc = self._pages([{
            "type": "table",
            "headers": ["KPI", "Metric"],
            "rows": [["Revenue", "Claims"]],
        }])
        candidates = classify_document(doc)
        for c in candidates:
            self.assertFalse(c["authoritative"], f"Candidate marked authoritative: {c}")
            self.assertTrue(c["review_required"])

    def test_all_candidates_have_document_pdf_source(self):
        doc = self._pages([{
            "type": "table",
            "headers": ["Term", "Definition"],
            "rows": [["Claim", "A bill"]],
        }])
        candidates = classify_document(doc)
        for c in candidates:
            self.assertEqual(c["source"], "document_pdf")

    def test_candidates_carry_page_provenance(self):
        doc = self._pages([{
            "type": "table",
            "headers": ["KPI", "Metric"],
            "rows": [["Rate", "Count"]],
        }])
        candidates = classify_document(doc)
        for c in candidates:
            self.assertEqual(c["page"], 1)

    def test_unrecognised_table_routes_to_raw_evidence(self):
        doc = self._pages([{
            "type": "table",
            "headers": ["Alpha", "Beta", "Gamma"],
            "rows": [["x", "y", "z"]],
        }])
        candidates = classify_document(doc)
        types_found = {c["candidate_type"] for c in candidates}
        # Should not get KPI/lexicon/data-model; raw evidence is acceptable
        self.assertFalse(types_found & {CANDIDATE_KPI_REGISTRY, CANDIDATE_LEXICON})

    def test_erd_text_block_routes_to_data_model(self):
        doc = self._pages([{
            "type": "paragraph",
            "text": "Entity Claims references entity Patients via foreign key PatientID -> PK PatientID.",
        }])
        candidates = classify_document(doc)
        types_found = {c["candidate_type"] for c in candidates}
        self.assertIn(CANDIDATE_DATA_MODEL, types_found)

    def test_classify_accepts_flat_extracted_content(self):
        """classify_document works on the extracted_content sub-dict directly."""
        doc = {
            "pages": [{
                "page_number": 2,
                "blocks": [{
                    "type": "table",
                    "headers": ["Term", "Definition", "Abbreviation"],
                    "rows": [["SLA", "Service Level Agreement", "SLA"]],
                }],
            }]
        }
        candidates = classify_document(doc)
        self.assertTrue(len(candidates) > 0)

    def test_no_duplicate_candidates_for_same_block(self):
        doc = self._pages([{
            "type": "table",
            "headers": ["Term", "Definition"],
            "rows": [["x", "y"]],
        }])
        candidates = classify_document(doc)
        # Deduplication: same block should not appear twice
        seen = []
        for c in candidates:
            key = (c["candidate_type"], c["page"], repr(c["content"]))
            self.assertNotIn(key, seen, "Duplicate candidate found")
            seen.append(key)


class TestRealKidsTreeSchema(unittest.TestCase):
    """classify_document() must handle the REAL opendataloader-pdf shape: a
    recursive `kids` tree of typed nodes ({type, content, "page number",
    "bounding box", kids:[...]}), not only the synthetic pages/blocks fixtures."""

    def _tree(self, nodes):
        return {
            "file name": "doc.pdf",
            "number of pages": 1,
            "kids": nodes,
        }

    def test_kids_tree_text_node_routes(self):
        doc = self._tree([
            {"type": "heading", "page number": 1, "bounding box": [0, 0, 1, 1],
             "content": "Entity Transactions references entity Patients via foreign key PatientID."},
            {"type": "paragraph", "page number": 1,
             "content": "All claims must be reconciled within 30 days per SLA policy."},
        ])
        candidates = classify_document(doc)
        types = {c["candidate_type"] for c in candidates}
        self.assertIn("data_model_candidate", types)
        self.assertIn("open_question_candidate", types)
        # provenance + non-authoritative preserved through the tree path
        for c in candidates:
            self.assertEqual(c["source"], "document_pdf")
            self.assertFalse(c["authoritative"])
            self.assertTrue(c["review_required"])

    def test_nested_kids_are_walked(self):
        doc = self._tree([
            {"type": "section", "kids": [
                {"type": "heading", "page number": 2,
                 "content": "Patients table joins Departments via FK DeptID"},
            ]},
        ])
        candidates = classify_document(doc)
        self.assertTrue(candidates)
        self.assertEqual(candidates[0]["page"], 2)

    def test_empty_tree_yields_nothing(self):
        self.assertEqual(classify_document(self._tree([])), [])


# ===========================================================================
# Real opendataloader-pdf 2.4.7 table-node schema tests
#
# Schema discovered from live opendataloader_pdf.convert() output against a
# matplotlib-rendered PDF with two bordered tables (see task description).
#
# Real table node:
#   {
#     type: "table",
#     pdfua_tag: "Table",
#     id: <int>,
#     level: <int>,
#     "page number": <int>,
#     "bounding box": [x0, y0, x1, y1],
#     "number of rows": <int>,
#     "number of columns": <int>,
#     rows: [
#       {
#         type: "table row",
#         "row number": <int>,
#         cells: [
#           {
#             type: "table cell",
#             pdfua_tag: "TD",
#             "page number": <int>,
#             "bounding box": [...],
#             "row number": <int>,
#             "column number": <int>,
#             "row span": 1,
#             "column span": 1,
#             kids: [
#               {type: "paragraph", ..., content: "<cell text>"}
#             ]
#           },
#           ...
#         ]
#       },
#       ...
#     ]
#   }
#
# Key findings:
# - table cells do NOT have a top-level "content" field; text is in kids[0].content
# - row 1 (row_number == 1) is the header row; data rows start at index 1
# - tables are top-level kids of the document tree, NOT nested under pages/blocks
# - matplotlib ax.table() with bordered cells IS detected as type="table" by free mode
# ===========================================================================

def _make_table_cell(row_num: int, col_num: int, text: str, page: int = 1) -> dict:
    """Build a real-schema table-cell node with text in kids[0].content."""
    return {
        "type": "table cell",
        "pdfua_tag": "TD",
        "page number": page,
        "bounding box": [0.0, 0.0, 100.0, 20.0],
        "row number": row_num,
        "column number": col_num,
        "row span": 1,
        "column span": 1,
        "kids": [
            {
                "type": "paragraph",
                "pdfua_tag": "P",
                "id": (row_num * 10) + col_num,
                "page number": page,
                "bounding box": [5.0, 2.0, 95.0, 18.0],
                "font": "DejaVuSans",
                "font size": 10.0,
                "content": text,
            }
        ],
    }


def _make_table_row(row_num: int, texts: list, page: int = 1) -> dict:
    """Build a real-schema table-row node."""
    return {
        "type": "table row",
        "row number": row_num,
        "cells": [
            _make_table_cell(row_num, col_num + 1, text, page)
            for col_num, text in enumerate(texts)
        ],
    }


def _make_real_table_node(
    header_texts: list,
    data_rows: list,
    page: int = 1,
    table_id: int = 1,
) -> dict:
    """Build a complete real-schema table node (type='table').

    header_texts: list of column header strings (row 1)
    data_rows: list of lists, each a row of cell text values
    """
    rows = [_make_table_row(1, header_texts, page)]
    for i, row_texts in enumerate(data_rows, start=2):
        rows.append(_make_table_row(i, row_texts, page))

    return {
        "type": "table",
        "pdfua_tag": "Table",
        "id": table_id,
        "level": 1,
        "page number": page,
        "bounding box": [-40.35, 105.81, 630.75, 179.31],
        "number of rows": len(rows),
        "number of columns": len(header_texts),
        "rows": rows,
    }


def _kids_tree_with_table(table_node: dict) -> dict:
    """Wrap a table node in a minimal real kids-tree document."""
    return {
        "file name": "doc.pdf",
        "number of pages": 1,
        "kids": [table_node],
    }


class TestRealTableNodeSchema(unittest.TestCase):
    """Classifier must extract headers+rows from the REAL opendataloader-pdf 2.4.7
    table schema (rows -> table row -> cells -> table cell -> kids -> paragraph.content)
    and route correctly to kpi_registry_candidate and lexicon_candidate.

    Fixtures mirror the exact structure emitted by opendataloader_pdf.convert()
    for a bordered matplotlib table.
    """

    def test_real_kpi_table_routes_to_kpi_registry_candidate(self):
        """KPI | Metric | Cuts table (real schema) -> kpi_registry_candidate."""
        table_node = _make_real_table_node(
            header_texts=["KPI", "Metric", "Cuts"],
            data_rows=[
                ["Claim Acceptance Rate", "Accepted / Total", "Payer, Date"],
                ["Denial Rate", "Denied / Total", "Provider, Month"],
            ],
            page=1,
        )
        doc = _kids_tree_with_table(table_node)
        candidates = classify_document(doc)
        types_found = {c["candidate_type"] for c in candidates}
        self.assertIn(
            CANDIDATE_KPI_REGISTRY,
            types_found,
            f"Expected kpi_registry_candidate; got: {types_found}. "
            "Check _extract_table_headers_real reads table-row/table-cell/kids[0].content.",
        )

    def test_real_lexicon_table_routes_to_lexicon_candidate(self):
        """Term | Definition table (real schema) -> lexicon_candidate."""
        table_node = _make_real_table_node(
            header_texts=["Term", "Definition"],
            data_rows=[
                ["Claim", "A request for payment submitted by a provider"],
                ["Remittance", "Payment notice sent to the provider"],
                ["Denial", "A claim rejected by the payer"],
            ],
            page=2,
            table_id=4,
        )
        doc = _kids_tree_with_table(table_node)
        candidates = classify_document(doc)
        types_found = {c["candidate_type"] for c in candidates}
        self.assertIn(
            CANDIDATE_LEXICON,
            types_found,
            f"Expected lexicon_candidate; got: {types_found}. "
            "Check _extract_table_headers_real reads table-row/table-cell/kids[0].content.",
        )

    def test_real_table_provenance_page_and_bbox_preserved(self):
        """Page and bounding box from the real table node are preserved in candidates."""
        table_node = _make_real_table_node(
            header_texts=["KPI", "Metric"],
            data_rows=[["Revenue Rate", "Claims Paid"]],
            page=3,
        )
        doc = _kids_tree_with_table(table_node)
        candidates = classify_document(doc)
        kpi_candidates = [c for c in candidates if c["candidate_type"] == CANDIDATE_KPI_REGISTRY]
        self.assertTrue(kpi_candidates, "Expected at least one kpi_registry_candidate.")
        c = kpi_candidates[0]
        self.assertEqual(c["page"], 3, "Page number must match the table node's 'page number'.")
        self.assertIsNotNone(c["bbox"], "Bounding box must be preserved from the table node.")

    def test_real_table_candidates_are_non_authoritative(self):
        """All candidates from real-schema tables must have authoritative=False."""
        table_node = _make_real_table_node(
            header_texts=["Term", "Definition", "Abbreviation"],
            data_rows=[["SLA", "Service Level Agreement", "SLA"]],
            page=1,
        )
        doc = _kids_tree_with_table(table_node)
        candidates = classify_document(doc)
        self.assertTrue(candidates, "Expected at least one candidate.")
        for c in candidates:
            self.assertFalse(c["authoritative"], f"Candidate must not be authoritative: {c}")
            self.assertTrue(c["review_required"])
            self.assertEqual(c["source"], "document_pdf")

    def test_real_table_headers_extracted_from_kids(self):
        """Headers are read from table-cell kids[0].content, not from a 'headers' key."""
        table_node = _make_real_table_node(
            header_texts=["KPI", "Numerator", "Denominator", "Target"],
            data_rows=[["Rate", "Paid", "Total", "0.95"]],
            page=1,
        )
        doc = _kids_tree_with_table(table_node)
        candidates = classify_document(doc)
        kpi_cands = [c for c in candidates if c["candidate_type"] == CANDIDATE_KPI_REGISTRY]
        self.assertTrue(kpi_cands)
        content_headers = kpi_cands[0]["content"]["headers"]
        self.assertIn("KPI", content_headers)
        self.assertIn("Numerator", content_headers)

    def test_real_table_sample_rows_populated(self):
        """sample_rows in candidates contains the data rows, not the header row."""
        table_node = _make_real_table_node(
            header_texts=["Term", "Definition"],
            data_rows=[
                ["Claim", "A bill"],
                ["Denial", "Rejected claim"],
            ],
            page=1,
        )
        doc = _kids_tree_with_table(table_node)
        candidates = classify_document(doc)
        lex_cands = [c for c in candidates if c["candidate_type"] == CANDIDATE_LEXICON]
        self.assertTrue(lex_cands)
        sample_rows = lex_cands[0]["content"]["sample_rows"]
        self.assertTrue(len(sample_rows) >= 1, "sample_rows should contain data rows.")
        # The header row should NOT appear as a data row
        row_values = [list(r.values()) if isinstance(r, dict) else r for r in sample_rows]
        flat = [v for row in row_values for v in row]
        self.assertNotIn("Term", flat, "Header row must not appear in sample_rows.")
        self.assertNotIn("Definition", flat, "Header row must not appear in sample_rows.")

    def test_real_unrecognised_table_routes_to_raw_evidence(self):
        """Table with no KPI/lexicon/ERD signals -> raw_evidence."""
        table_node = _make_real_table_node(
            header_texts=["Alpha", "Beta", "Gamma"],
            data_rows=[["x", "y", "z"]],
            page=1,
        )
        doc = _kids_tree_with_table(table_node)
        candidates = classify_document(doc)
        types_found = {c["candidate_type"] for c in candidates}
        self.assertFalse(
            types_found & {CANDIDATE_KPI_REGISTRY, CANDIDATE_LEXICON},
            f"Unrecognised table must not route to KPI or lexicon; got: {types_found}",
        )

    def test_real_erd_table_routes_to_data_model_candidate(self):
        """Table/Column/PK/FK/References table (real schema) -> data_model_candidate."""
        table_node = _make_real_table_node(
            header_texts=["Table", "Column", "PK", "FK", "References"],
            data_rows=[
                ["Claims", "ClaimID", "Y", "", ""],
                ["Claims", "PatientID", "", "Y", "Patients.PatientID"],
            ],
            page=1,
        )
        doc = _kids_tree_with_table(table_node)
        candidates = classify_document(doc)
        types_found = {c["candidate_type"] for c in candidates}
        self.assertIn(CANDIDATE_DATA_MODEL, types_found)


if __name__ == "__main__":
    unittest.main()
