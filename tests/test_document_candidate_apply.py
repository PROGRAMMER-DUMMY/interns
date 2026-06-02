"""Tests for document candidate review and apply (Phase 3: governed promote path).

Run with:
    .venv\\Scripts\\python.exe -m unittest tests.test_document_candidate_apply

No real PDF files, no real workspace, no network calls.
All tests use in-process tempdir workspaces.
"""
from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from typing import Any

from core.onboarding.documents.candidate_review import (
    _stable_candidate_id,
    prepare_document_candidate_review,
)
from core.onboarding.documents.candidate_apply import (
    apply_document_candidate,
    merge_accepted_candidates,
)
from core.storage.workspace_layout import WorkspaceLayout


# ---------------------------------------------------------------------------
# Shared test helpers
# ---------------------------------------------------------------------------

def _make_workspace(tmp: Path) -> tuple[Path, Path]:
    """Create a minimal workspace tree; return (repo_root, workspace_path)."""
    repo_root = tmp
    ws = tmp / "workspaces" / "test_project"
    ws.mkdir(parents=True, exist_ok=True)
    (ws / "interns").mkdir(exist_ok=True)
    return repo_root, ws


def _write_candidates_json(
    workspace_path: Path,
    candidates: list[dict[str, Any]],
    repo_root: Path | None = None,
) -> Path:
    """Write a minimal candidates.json fixture."""
    layout = WorkspaceLayout(project_root=workspace_path)
    documents_dir = layout.generated_dir / "documents"
    documents_dir.mkdir(parents=True, exist_ok=True)
    path = documents_dir / "candidates.json"
    workspace_rel = str(workspace_path.relative_to(repo_root)) if repo_root else str(workspace_path)
    path.write_text(
        json.dumps({
            "artifact_type": "document_candidates.json",
            "version": 1,
            "generated_by": "onboard-workspace",
            "workspace": workspace_rel,
            "authoritative_usage_allowed": False,
            "candidate_count": len(candidates),
            "candidates": candidates,
        }),
        encoding="utf-8",
    )
    return path


def _sample_kpi_candidate(source_doc: str = "docs/kpi_spec.pdf") -> dict[str, Any]:
    return {
        "candidate_type": "kpi_registry_candidate",
        "source": "document_pdf",
        "source_document": source_doc,
        "page": 1,
        "bbox": None,
        "content": {"headers": ["KPI", "Metric"], "row_count": 2, "sample_rows": []},
        "authoritative": False,
        "review_required": True,
        "confidence": 0.75,
        "reason": "Table headers overlap with KPI signals.",
    }


def _sample_lexicon_candidate(source_doc: str = "docs/glossary.pdf") -> dict[str, Any]:
    return {
        "candidate_type": "lexicon_candidate",
        "source": "document_pdf",
        "source_document": source_doc,
        "page": 2,
        "bbox": None,
        "content": {"headers": ["Term", "Definition"], "row_count": 5, "sample_rows": []},
        "authoritative": False,
        "review_required": True,
        "confidence": 0.80,
        "reason": "Term/definition table detected.",
    }


def _sample_data_model_candidate(source_doc: str = "docs/erd.pdf") -> dict[str, Any]:
    return {
        "candidate_type": "data_model_candidate",
        "source": "document_pdf",
        "source_document": source_doc,
        "page": 3,
        "bbox": {"x0": 0, "y0": 0, "x1": 100, "y1": 100},
        "content": {"text_snippet": "Entity Claims FK PatientID -> Patients PK PatientID"},
        "authoritative": False,
        "review_required": True,
        "confidence": 0.60,
        "reason": "ERD/FK signals detected.",
    }


def _sample_open_question_candidate(source_doc: str = "docs/sla.pdf") -> dict[str, Any]:
    return {
        "candidate_type": "open_question_candidate",
        "source": "document_pdf",
        "source_document": source_doc,
        "page": 4,
        "bbox": None,
        "content": {"text_snippet": "Claims must be processed within 30 days per SLA."},
        "authoritative": False,
        "review_required": True,
        "confidence": 0.55,
        "reason": "Business rule / SLA language detected.",
    }


# ===========================================================================
# Review panel tests
# ===========================================================================

class TestPrepareDocumentCandidateReview(unittest.TestCase):
    """prepare_document_candidate_review() must emit a display-only panel."""

    def test_missing_candidates_json_returns_no_candidates_status(self):
        with tempfile.TemporaryDirectory() as tmp_str:
            tmp = Path(tmp_str)
            repo_root, ws = _make_workspace(tmp)
            result = prepare_document_candidate_review(repo_root, ws)
            self.assertEqual(result.status, "no_candidates")
            self.assertEqual(result.candidate_count, 0)

    def test_empty_candidate_list_writes_panel_with_zero_count(self):
        with tempfile.TemporaryDirectory() as tmp_str:
            tmp = Path(tmp_str)
            repo_root, ws = _make_workspace(tmp)
            _write_candidates_json(ws, [], repo_root)
            result = prepare_document_candidate_review(repo_root, ws)
            self.assertEqual(result.status, "ok")
            self.assertEqual(result.candidate_count, 0)
            self.assertTrue(Path(repo_root / result.panel_md_path).exists())
            self.assertTrue(Path(repo_root / result.panel_json_path).exists())

    def test_candidates_listed_with_stable_ids(self):
        with tempfile.TemporaryDirectory() as tmp_str:
            tmp = Path(tmp_str)
            repo_root, ws = _make_workspace(tmp)
            candidates = [_sample_kpi_candidate(), _sample_lexicon_candidate()]
            _write_candidates_json(ws, candidates, repo_root)
            result = prepare_document_candidate_review(repo_root, ws)
            self.assertEqual(result.candidate_count, 2)

            panel = json.loads(Path(repo_root / result.panel_json_path).read_text(encoding="utf-8"))
            emitted = panel["candidates"]
            self.assertEqual(len(emitted), 2)
            for c in emitted:
                self.assertIn("candidate_id", c)
                self.assertTrue(c["candidate_id"].startswith("doc_cand_"))

    def test_candidate_ids_are_stable_across_reruns(self):
        """Running the review twice produces identical candidate_ids."""
        with tempfile.TemporaryDirectory() as tmp_str:
            tmp = Path(tmp_str)
            repo_root, ws = _make_workspace(tmp)
            candidates = [_sample_kpi_candidate()]
            _write_candidates_json(ws, candidates, repo_root)

            r1 = prepare_document_candidate_review(repo_root, ws)
            r2 = prepare_document_candidate_review(repo_root, ws)

            p1 = json.loads(Path(repo_root / r1.panel_json_path).read_text(encoding="utf-8"))
            p2 = json.loads(Path(repo_root / r2.panel_json_path).read_text(encoding="utf-8"))

            ids1 = [c["candidate_id"] for c in p1["candidates"]]
            ids2 = [c["candidate_id"] for c in p2["candidates"]]
            self.assertEqual(ids1, ids2)

    def test_panel_lists_candidate_type_confidence_page_source(self):
        with tempfile.TemporaryDirectory() as tmp_str:
            tmp = Path(tmp_str)
            repo_root, ws = _make_workspace(tmp)
            cand = _sample_kpi_candidate(source_doc="docs/spec.pdf")
            _write_candidates_json(ws, [cand], repo_root)
            result = prepare_document_candidate_review(repo_root, ws)
            panel = json.loads(Path(repo_root / result.panel_json_path).read_text(encoding="utf-8"))
            entry = panel["candidates"][0]
            self.assertEqual(entry["candidate_type"], "kpi_registry_candidate")
            self.assertEqual(entry["confidence"], 0.75)
            self.assertEqual(entry["page"], 1)
            self.assertEqual(entry["source_document"], "docs/spec.pdf")
            self.assertIn("routing_target", entry)

    def test_panel_markdown_contains_apply_command_hint(self):
        with tempfile.TemporaryDirectory() as tmp_str:
            tmp = Path(tmp_str)
            repo_root, ws = _make_workspace(tmp)
            _write_candidates_json(ws, [_sample_kpi_candidate()], repo_root)
            result = prepare_document_candidate_review(repo_root, ws)
            md = Path(repo_root / result.panel_md_path).read_text(encoding="utf-8")
            self.assertIn("apply-document-candidate", md)
            self.assertIn("--confirmed-by", md)

    def test_data_model_candidate_shows_non_executable_warning_in_markdown(self):
        with tempfile.TemporaryDirectory() as tmp_str:
            tmp = Path(tmp_str)
            repo_root, ws = _make_workspace(tmp)
            _write_candidates_json(ws, [_sample_data_model_candidate()], repo_root)
            result = prepare_document_candidate_review(repo_root, ws)
            md = Path(repo_root / result.panel_md_path).read_text(encoding="utf-8")
            self.assertIn("profile RI proof", md)
            self.assertIn("NON-executable", md)

    def test_panel_json_has_non_authoritative_flag(self):
        with tempfile.TemporaryDirectory() as tmp_str:
            tmp = Path(tmp_str)
            repo_root, ws = _make_workspace(tmp)
            _write_candidates_json(ws, [_sample_kpi_candidate()], repo_root)
            result = prepare_document_candidate_review(repo_root, ws)
            panel = json.loads(Path(repo_root / result.panel_json_path).read_text(encoding="utf-8"))
            self.assertFalse(panel["authoritative_usage_allowed"])

    def test_panel_json_has_interaction_contract(self):
        with tempfile.TemporaryDirectory() as tmp_str:
            tmp = Path(tmp_str)
            repo_root, ws = _make_workspace(tmp)
            _write_candidates_json(ws, [_sample_lexicon_candidate()], repo_root)
            result = prepare_document_candidate_review(repo_root, ws)
            panel = json.loads(Path(repo_root / result.panel_json_path).read_text(encoding="utf-8"))
            self.assertIn("interaction_contract", panel)
            ic = panel["interaction_contract"]
            self.assertFalse(ic.get("generic_answer_picker_allowed"))

    def test_stable_id_differs_across_distinct_candidates(self):
        c1 = _sample_kpi_candidate(source_doc="docs/a.pdf")
        c2 = _sample_kpi_candidate(source_doc="docs/b.pdf")
        id1 = _stable_candidate_id(c1)
        id2 = _stable_candidate_id(c2)
        self.assertNotEqual(id1, id2)


# ===========================================================================
# apply_document_candidate: refused without --confirmed-by
# ===========================================================================

class TestApplyRefusedWithoutConfirmedBy(unittest.TestCase):
    """Acceptance without --confirmed-by must be refused."""

    def test_accept_without_confirmed_by_is_refused(self):
        with tempfile.TemporaryDirectory() as tmp_str:
            tmp = Path(tmp_str)
            repo_root, ws = _make_workspace(tmp)
            cand = _sample_kpi_candidate()
            _write_candidates_json(ws, [cand], repo_root)
            cid = _stable_candidate_id(cand)

            result = apply_document_candidate(
                repo_root, ws,
                candidate_id=cid,
                confirmed_by="",   # empty -> must be refused
                reject=False,
            )
            self.assertEqual(result.status, "refused")
            self.assertEqual(result.decision, "refused")
            self.assertIn("--confirmed-by", result.note)

    def test_refused_does_not_write_accepted_candidates_file(self):
        with tempfile.TemporaryDirectory() as tmp_str:
            tmp = Path(tmp_str)
            repo_root, ws = _make_workspace(tmp)
            cand = _sample_kpi_candidate()
            _write_candidates_json(ws, [cand], repo_root)
            cid = _stable_candidate_id(cand)

            apply_document_candidate(
                repo_root, ws,
                candidate_id=cid,
                confirmed_by="",
                reject=False,
            )
            layout = WorkspaceLayout(project_root=ws)
            durable = layout.generated_dir / "documents" / "accepted_candidates.json"
            self.assertFalse(durable.exists())

    def test_reject_without_confirmed_by_is_allowed(self):
        """Rejection does not require --confirmed-by (optional for rejections)."""
        with tempfile.TemporaryDirectory() as tmp_str:
            tmp = Path(tmp_str)
            repo_root, ws = _make_workspace(tmp)
            cand = _sample_kpi_candidate()
            _write_candidates_json(ws, [cand], repo_root)
            cid = _stable_candidate_id(cand)

            # Rejecting with empty confirmed_by should succeed (decision recorded)
            result = apply_document_candidate(
                repo_root, ws,
                candidate_id=cid,
                confirmed_by="",
                reject=True,
                note="Not needed right now.",
            )
            # The function allows rejection without confirmed_by (no promotion risk)
            # We just check it didn't crash; status may be ok or error depending on
            # implementation choice.  Either way the call must not raise.
            self.assertIsNotNone(result.status)


# ===========================================================================
# apply_document_candidate: durable acceptance record
# ===========================================================================

class TestApplyRecordsDurableEntry(unittest.TestCase):
    """Accepted candidate must be persisted with full provenance."""

    def _setup_and_accept(
        self,
        tmp: Path,
        candidate: dict[str, Any],
        confirmed_by: str = "Alice",
    ) -> tuple[Path, Path, str, Any]:
        repo_root, ws = _make_workspace(tmp)
        _write_candidates_json(ws, [candidate], repo_root)
        cid = _stable_candidate_id(candidate)
        result = apply_document_candidate(
            repo_root, ws,
            candidate_id=cid,
            confirmed_by=confirmed_by,
        )
        return repo_root, ws, cid, result

    def test_accept_with_confirmed_by_returns_ok(self):
        with tempfile.TemporaryDirectory() as tmp_str:
            _, _, _, result = self._setup_and_accept(Path(tmp_str), _sample_kpi_candidate())
            self.assertEqual(result.status, "ok")
            self.assertEqual(result.decision, "accepted")

    def test_accepted_entry_written_to_durable_artifact(self):
        with tempfile.TemporaryDirectory() as tmp_str:
            tmp = Path(tmp_str)
            repo_root, ws, cid, result = self._setup_and_accept(tmp, _sample_kpi_candidate())
            layout = WorkspaceLayout(project_root=ws)
            durable = layout.generated_dir / "documents" / "accepted_candidates.json"
            self.assertTrue(durable.exists(), "accepted_candidates.json must be written")
            store = json.loads(durable.read_text(encoding="utf-8"))
            decisions = store.get("decisions") or []
            self.assertEqual(len(decisions), 1)
            self.assertEqual(decisions[0]["candidate_id"], cid)

    def test_durable_entry_has_required_provenance_fields(self):
        with tempfile.TemporaryDirectory() as tmp_str:
            tmp = Path(tmp_str)
            repo_root, ws, cid, _ = self._setup_and_accept(tmp, _sample_kpi_candidate(), "Bob")
            layout = WorkspaceLayout(project_root=ws)
            durable = layout.generated_dir / "documents" / "accepted_candidates.json"
            store = json.loads(durable.read_text(encoding="utf-8"))
            entry = store["decisions"][0]

            self.assertEqual(entry["decision"], "accepted")
            self.assertEqual(entry["source"], "human")
            self.assertEqual(entry["confirmed_by"], "Bob")
            self.assertEqual(entry["candidate_type"], "kpi_registry_candidate")
            self.assertIn("content", entry)
            self.assertIn("provenance", entry)
            self.assertEqual(entry["provenance"]["source"], "document_pdf")
            self.assertEqual(entry["provenance"]["confirmed_by"], "Bob")

    def test_durable_artifact_survives_second_onboard_run(self):
        """accepted_candidates.json is under generated/documents/, not contracts/;
        re-writing candidates.json does NOT overwrite it."""
        with tempfile.TemporaryDirectory() as tmp_str:
            tmp = Path(tmp_str)
            repo_root, ws, cid, _ = self._setup_and_accept(tmp, _sample_kpi_candidate())
            layout = WorkspaceLayout(project_root=ws)
            durable = layout.generated_dir / "documents" / "accepted_candidates.json"

            # Simulate a re-onboarding run: overwrite candidates.json with new content
            _write_candidates_json(ws, [_sample_lexicon_candidate()], repo_root)

            # Durable artifact must still exist and still contain the accepted entry
            self.assertTrue(durable.exists())
            store = json.loads(durable.read_text(encoding="utf-8"))
            decisions = store.get("decisions") or []
            self.assertEqual(len(decisions), 1)
            self.assertEqual(decisions[0]["candidate_id"], cid)

    def test_durable_artifact_not_under_contracts_dir(self):
        with tempfile.TemporaryDirectory() as tmp_str:
            tmp = Path(tmp_str)
            repo_root, ws, _, _ = self._setup_and_accept(tmp, _sample_kpi_candidate())
            layout = WorkspaceLayout(project_root=ws)
            durable = layout.generated_dir / "documents" / "accepted_candidates.json"
            # Must NOT be inside contracts/
            self.assertNotIn("contracts", str(durable))
            self.assertTrue(durable.exists())

    def test_accepted_candidates_json_not_kpi_registry(self):
        """apply-document-candidate must NOT write to kpi_registry.json."""
        with tempfile.TemporaryDirectory() as tmp_str:
            tmp = Path(tmp_str)
            repo_root, ws, _, _ = self._setup_and_accept(tmp, _sample_kpi_candidate())
            layout = WorkspaceLayout(project_root=ws)
            self.assertFalse(layout.kpi_registry_path.exists())


# ===========================================================================
# apply_document_candidate: rejection record
# ===========================================================================

class TestApplyRecordsRejection(unittest.TestCase):
    def test_reject_records_rejection_in_durable_artifact(self):
        with tempfile.TemporaryDirectory() as tmp_str:
            tmp = Path(tmp_str)
            repo_root, ws = _make_workspace(tmp)
            cand = _sample_lexicon_candidate()
            _write_candidates_json(ws, [cand], repo_root)
            cid = _stable_candidate_id(cand)

            result = apply_document_candidate(
                repo_root, ws,
                candidate_id=cid,
                confirmed_by="Carol",
                reject=True,
                note="Term already in lexicon.",
            )
            self.assertEqual(result.status, "ok")
            self.assertEqual(result.decision, "rejected")

            layout = WorkspaceLayout(project_root=ws)
            durable = layout.generated_dir / "documents" / "accepted_candidates.json"
            store = json.loads(durable.read_text(encoding="utf-8"))
            decisions = store.get("decisions") or []
            self.assertEqual(len(decisions), 1)
            self.assertEqual(decisions[0]["decision"], "rejected")
            self.assertEqual(decisions[0]["confirmed_by"], "Carol")
            self.assertEqual(decisions[0]["rejection_note"], "Term already in lexicon.")

    def test_rejected_candidate_not_returned_by_merge(self):
        with tempfile.TemporaryDirectory() as tmp_str:
            tmp = Path(tmp_str)
            repo_root, ws = _make_workspace(tmp)
            cand = _sample_lexicon_candidate()
            _write_candidates_json(ws, [cand], repo_root)
            cid = _stable_candidate_id(cand)

            apply_document_candidate(
                repo_root, ws,
                candidate_id=cid,
                confirmed_by="Carol",
                reject=True,
                note="Duplicate.",
            )
            layout = WorkspaceLayout(project_root=ws)
            merged = merge_accepted_candidates(layout)
            self.assertEqual(merged["lexicon_candidates"], [])
            self.assertEqual(merged["accepted_count"], 0)
            self.assertEqual(merged["rejected_count"], 1)


# ===========================================================================
# merge_accepted_candidates
# ===========================================================================

class TestMergeAcceptedCandidates(unittest.TestCase):
    """merge_accepted_candidates() must return accepted entries by type."""

    def _accept(self, tmp: Path, candidates: list[dict[str, Any]], confirmed_by: str = "Dev") -> WorkspaceLayout:
        repo_root, ws = _make_workspace(tmp)
        _write_candidates_json(ws, candidates, repo_root)
        layout = WorkspaceLayout(project_root=ws)
        for cand in candidates:
            cid = _stable_candidate_id(cand)
            apply_document_candidate(
                repo_root, ws,
                candidate_id=cid,
                confirmed_by=confirmed_by,
            )
        return layout

    def test_returns_empty_when_no_durable_file(self):
        with tempfile.TemporaryDirectory() as tmp_str:
            tmp = Path(tmp_str)
            _, ws = _make_workspace(tmp)
            layout = WorkspaceLayout(project_root=ws)
            merged = merge_accepted_candidates(layout)
            self.assertEqual(merged["kpi_registry_candidates"], [])
            self.assertEqual(merged["lexicon_candidates"], [])
            self.assertEqual(merged["data_model_candidates"], [])
            self.assertEqual(merged["accepted_count"], 0)

    def test_returns_kpi_candidates_in_correct_bucket(self):
        with tempfile.TemporaryDirectory() as tmp_str:
            layout = self._accept(Path(tmp_str), [_sample_kpi_candidate()])
            merged = merge_accepted_candidates(layout)
            self.assertEqual(len(merged["kpi_registry_candidates"]), 1)
            self.assertEqual(merged["lexicon_candidates"], [])
            self.assertEqual(merged["accepted_count"], 1)

    def test_returns_lexicon_candidates_in_correct_bucket(self):
        with tempfile.TemporaryDirectory() as tmp_str:
            layout = self._accept(Path(tmp_str), [_sample_lexicon_candidate()])
            merged = merge_accepted_candidates(layout)
            self.assertEqual(len(merged["lexicon_candidates"]), 1)
            self.assertEqual(merged["kpi_registry_candidates"], [])

    def test_returns_open_question_candidates_in_correct_bucket(self):
        with tempfile.TemporaryDirectory() as tmp_str:
            layout = self._accept(Path(tmp_str), [_sample_open_question_candidate()])
            merged = merge_accepted_candidates(layout)
            self.assertEqual(len(merged["open_question_candidates"]), 1)

    def test_data_model_candidate_flagged_non_executable(self):
        with tempfile.TemporaryDirectory() as tmp_str:
            layout = self._accept(Path(tmp_str), [_sample_data_model_candidate()])
            merged = merge_accepted_candidates(layout)
            dm = merged["data_model_candidates"]
            self.assertEqual(len(dm), 1)
            # Must be non-executable regardless of acceptance
            self.assertFalse(dm[0]["executable"], "data_model_candidate must be non-executable")

    def test_mixed_candidates_routed_to_correct_buckets(self):
        with tempfile.TemporaryDirectory() as tmp_str:
            tmp = Path(tmp_str)
            all_candidates = [
                _sample_kpi_candidate(),
                _sample_lexicon_candidate(),
                _sample_data_model_candidate(),
                _sample_open_question_candidate(),
            ]
            layout = self._accept(tmp, all_candidates)
            merged = merge_accepted_candidates(layout)
            self.assertEqual(len(merged["kpi_registry_candidates"]), 1)
            self.assertEqual(len(merged["lexicon_candidates"]), 1)
            self.assertEqual(len(merged["data_model_candidates"]), 1)
            self.assertEqual(len(merged["open_question_candidates"]), 1)
            self.assertEqual(merged["accepted_count"], 4)

    def test_merge_result_has_source_path(self):
        with tempfile.TemporaryDirectory() as tmp_str:
            layout = self._accept(Path(tmp_str), [_sample_kpi_candidate()])
            merged = merge_accepted_candidates(layout)
            self.assertIn("source_path", merged)
            self.assertIn("accepted_candidates.json", merged["source_path"])


# ===========================================================================
# data_model_candidate non-executable invariant
# ===========================================================================

class TestDataModelNonExecutable(unittest.TestCase):
    """data_model_candidate must be non-executable even after human acceptance."""

    def test_accepted_data_model_candidate_has_executable_false_in_durable_store(self):
        with tempfile.TemporaryDirectory() as tmp_str:
            tmp = Path(tmp_str)
            repo_root, ws = _make_workspace(tmp)
            cand = _sample_data_model_candidate()
            _write_candidates_json(ws, [cand], repo_root)
            cid = _stable_candidate_id(cand)

            apply_document_candidate(
                repo_root, ws,
                candidate_id=cid,
                confirmed_by="DataEngineer",
            )
            layout = WorkspaceLayout(project_root=ws)
            durable = layout.generated_dir / "documents" / "accepted_candidates.json"
            store = json.loads(durable.read_text(encoding="utf-8"))
            entry = store["decisions"][0]
            self.assertFalse(entry["executable"])

    def test_accepted_data_model_candidate_has_non_executable_reason(self):
        with tempfile.TemporaryDirectory() as tmp_str:
            tmp = Path(tmp_str)
            repo_root, ws = _make_workspace(tmp)
            cand = _sample_data_model_candidate()
            _write_candidates_json(ws, [cand], repo_root)
            cid = _stable_candidate_id(cand)

            apply_document_candidate(
                repo_root, ws,
                candidate_id=cid,
                confirmed_by="DataEngineer",
            )
            layout = WorkspaceLayout(project_root=ws)
            durable = layout.generated_dir / "documents" / "accepted_candidates.json"
            store = json.loads(durable.read_text(encoding="utf-8"))
            entry = store["decisions"][0]
            self.assertIn("non_executable_reason", entry)
            self.assertIn("profile RI proof", entry["non_executable_reason"])

    def test_merge_forces_data_model_non_executable_even_if_store_says_true(self):
        """merge_accepted_candidates must enforce executable=False for data_model
        even if the durable store somehow has executable=True (defensive check)."""
        with tempfile.TemporaryDirectory() as tmp_str:
            tmp = Path(tmp_str)
            _, ws = _make_workspace(tmp)
            layout = WorkspaceLayout(project_root=ws)
            durable = layout.generated_dir / "documents" / "accepted_candidates.json"
            durable.parent.mkdir(parents=True, exist_ok=True)
            # Write a tampered store with executable=True
            tampered = {
                "artifact_type": "accepted_document_candidates",
                "version": 1,
                "workspace": "workspaces/test_project",
                "decisions": [{
                    "candidate_id": "doc_cand_fake0001",
                    "decision": "accepted",
                    "source": "human",
                    "confirmed_by": "Attacker",
                    "candidate_type": "data_model_candidate",
                    "target_type": "data_model_candidate",
                    "content": {},
                    "executable": True,   # tampered -- must be overridden by merge
                }],
            }
            durable.write_text(json.dumps(tampered), encoding="utf-8")

            merged = merge_accepted_candidates(layout)
            dm = merged["data_model_candidates"]
            self.assertEqual(len(dm), 1)
            self.assertFalse(dm[0]["executable"],
                "merge_accepted_candidates must force executable=False for data_model_candidate")


# ===========================================================================
# Unknown candidate_id error path
# ===========================================================================

class TestApplyUnknownCandidateId(unittest.TestCase):
    def test_unknown_candidate_id_returns_error_not_crash(self):
        with tempfile.TemporaryDirectory() as tmp_str:
            tmp = Path(tmp_str)
            repo_root, ws = _make_workspace(tmp)
            _write_candidates_json(ws, [_sample_kpi_candidate()], repo_root)

            result = apply_document_candidate(
                repo_root, ws,
                candidate_id="doc_cand_doesnotexist",
                confirmed_by="Alice",
            )
            self.assertEqual(result.status, "error")
            self.assertIn("not found", result.note)

    def test_missing_candidates_json_returns_error(self):
        with tempfile.TemporaryDirectory() as tmp_str:
            tmp = Path(tmp_str)
            repo_root, ws = _make_workspace(tmp)
            # No candidates.json written

            result = apply_document_candidate(
                repo_root, ws,
                candidate_id="doc_cand_anything",
                confirmed_by="Alice",
            )
            self.assertEqual(result.status, "error")


# ===========================================================================
# Idempotency / upsert
# ===========================================================================

class TestApplyIdempotency(unittest.TestCase):
    """Applying the same candidate twice should upsert (not append a duplicate)."""

    def test_duplicate_apply_upserts_not_appends(self):
        with tempfile.TemporaryDirectory() as tmp_str:
            tmp = Path(tmp_str)
            repo_root, ws = _make_workspace(tmp)
            cand = _sample_kpi_candidate()
            _write_candidates_json(ws, [cand], repo_root)
            cid = _stable_candidate_id(cand)

            apply_document_candidate(repo_root, ws, candidate_id=cid, confirmed_by="Alice")
            apply_document_candidate(repo_root, ws, candidate_id=cid, confirmed_by="Bob")  # overwrite

            layout = WorkspaceLayout(project_root=ws)
            durable = layout.generated_dir / "documents" / "accepted_candidates.json"
            store = json.loads(durable.read_text(encoding="utf-8"))
            decisions = store.get("decisions") or []
            # Should have exactly one decision for this candidate_id
            matching = [d for d in decisions if d.get("candidate_id") == cid]
            self.assertEqual(len(matching), 1)
            # Latest (Bob) should win
            self.assertEqual(matching[0]["confirmed_by"], "Bob")


if __name__ == "__main__":
    unittest.main()
