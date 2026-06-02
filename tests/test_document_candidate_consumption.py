"""Onboarding consumption of non-KPI human-confirmed PDF candidates.

Closes the remaining PDF->workspace loops beyond KPI (which is covered by
test_document_kpi_merge): open_question, lexicon, and data_model candidates a
human accepted via apply-document-candidate are consumed during onboarding.

GOVERNANCE: only human-confirmed candidates (decision=accepted) are consumed.
data_model candidates are surfaced as NON-executable proposals only (profile RI
proof still required -- BUG-004/023 discipline).
"""
from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from core.onboarding.lexicon.builder import build_workspace_lexicon
from core.onboarding.workspace.onboarding import WorkspaceOnboarder


class _Base(unittest.TestCase):
    def _onboarder_with_accepted(self, tmp: Path, decisions: list[dict]) -> WorkspaceOnboarder:
        ws = tmp / "workspaces" / "demo"
        ws.mkdir(parents=True)
        ob = WorkspaceOnboarder(tmp, "workspaces/demo")
        ob.layout.ensure_runtime_dirs()
        store = ob.layout.generated_dir / "documents" / "accepted_candidates.json"
        store.parent.mkdir(parents=True, exist_ok=True)
        store.write_text(
            json.dumps({"artifact_type": "accepted_document_candidates", "decisions": decisions}),
            encoding="utf-8",
        )
        return ob


class OpenQuestionConsumptionTests(_Base):
    def _oq_decision(self, snippet: str, *, page: int | None = 3) -> dict:
        return {
            "decision": "accepted",
            "candidate_type": "open_question_candidate",
            "confirmed_by": "shubham",
            "source_document": "workspaces/demo/docs/policy.pdf",
            "page": page,
            "content": {"text_snippet": snippet, "detected_signals": ["sla", "must"]},
        }

    def test_accepted_open_question_appended(self):
        with tempfile.TemporaryDirectory() as tmp:
            ob = self._onboarder_with_accepted(
                Path(tmp),
                [self._oq_decision("Claims must be adjudicated within 30 days (SLA).")],
            )
            questions, warnings = ob._accepted_document_open_questions()
            self.assertEqual(len(questions), 1)
            self.assertIn("adjudicated within 30 days", questions[0])
            self.assertIn("policy.pdf", questions[0])
            self.assertIn("p.3", questions[0])
            self.assertTrue(any("document_open_questions_merged" in w for w in warnings))

    def test_rendered_into_open_questions_md(self):
        with tempfile.TemporaryDirectory() as tmp:
            ob = self._onboarder_with_accepted(
                Path(tmp),
                [self._oq_decision("Denials require root-cause coding.")],
            )
            questions, _ = ob._accepted_document_open_questions()
            path = ob._write_open_questions([], [], questions)
            text = (Path(tmp) / path).read_text(encoding="utf-8")
            self.assertIn("From confirmed source documents (PDF)", text)
            self.assertIn("Denials require root-cause coding.", text)

    def test_duplicate_snippets_deduped(self):
        with tempfile.TemporaryDirectory() as tmp:
            ob = self._onboarder_with_accepted(
                Path(tmp),
                [self._oq_decision("Same rule."), self._oq_decision("same rule.", page=4)],
            )
            questions, _ = ob._accepted_document_open_questions()
            self.assertEqual(len(questions), 1)

    def test_no_store_is_noop(self):
        with tempfile.TemporaryDirectory() as tmp:
            ws = Path(tmp) / "workspaces" / "demo"
            ws.mkdir(parents=True)
            ob = WorkspaceOnboarder(Path(tmp), "workspaces/demo")
            ob.layout.ensure_runtime_dirs()
            questions, warnings = ob._accepted_document_open_questions()
            self.assertEqual(questions, [])
            self.assertEqual(warnings, [])


class DataModelConsumptionTests(_Base):
    def _dm_decision(self, snippet: str) -> dict:
        return {
            "decision": "accepted",
            "candidate_type": "data_model_candidate",
            "confirmed_by": "shubham",
            "source_document": "workspaces/demo/docs/erd.pdf",
            "page": 2,
            "content": {"text_snippet": snippet, "detected_signals": ["fk", "references"]},
        }

    def test_data_model_surfaced_non_executable(self):
        with tempfile.TemporaryDirectory() as tmp:
            ob = self._onboarder_with_accepted(
                Path(tmp),
                [self._dm_decision("encounters.PatientID -> patients.PatientID")],
            )
            notes, warnings = ob._accepted_document_relationship_notes()
            self.assertEqual(len(notes), 1)
            self.assertIn("[NON-EXECUTABLE]", notes[0])
            self.assertIn("encounters.PatientID", notes[0])
            self.assertIn("profile RI proof", notes[0])
            self.assertTrue(any("document_relationships_surfaced" in w for w in warnings))

    def test_data_model_rendered_in_open_questions(self):
        with tempfile.TemporaryDirectory() as tmp:
            ob = self._onboarder_with_accepted(
                Path(tmp),
                [self._dm_decision("transactions.ProviderID -> providers.ProviderID")],
            )
            notes, _ = ob._accepted_document_relationship_notes()
            path = ob._write_open_questions([], [], None, notes)
            text = (Path(tmp) / path).read_text(encoding="utf-8")
            self.assertIn("Proposed relationships from documents (NON-EXECUTABLE)", text)
            self.assertIn("transactions.ProviderID", text)


class LexiconConsumptionTests(_Base):
    def _write_profile_index(self, ob: WorkspaceOnboarder, columns: list[str]) -> None:
        ob.layout.profiles_dir.mkdir(parents=True, exist_ok=True)
        (ob.layout.profiles_dir / "profile_index.json").write_text(
            json.dumps({
                "profiles": [
                    {"path": "claims.csv", "schema": {c: {"dtype": "VARCHAR"} for c in columns}}
                ]
            }),
            encoding="utf-8",
        )

    def _lex_decision(self, term: str, definition: str) -> dict:
        return {
            "decision": "accepted",
            "candidate_type": "lexicon_candidate",
            "confirmed_by": "shubham",
            "source_document": "workspaces/demo/docs/glossary.pdf",
            "content": {
                "headers": ["Term", "Definition"],
                "sample_rows": [{"Term": term, "Definition": definition}],
            },
        }

    def test_glossary_term_matching_existing_column_becomes_alias(self):
        with tempfile.TemporaryDirectory() as tmp:
            ob = self._onboarder_with_accepted(
                Path(tmp),
                [self._lex_decision("PaidAmt", "The PaidAmount column on claims")],
            )
            self._write_profile_index(ob, ["PaidAmount", "PayorID"])
            lex = build_workspace_lexicon(ob.layout, Path(tmp))
            aliases = lex.aliases_for_column("PaidAmount")
            self.assertIn("paidamt", aliases)
            self.assertEqual(lex.stats.get("document_lexicon_terms_carried"), 1)

    def test_term_for_unknown_column_is_not_injected(self):
        with tempfile.TemporaryDirectory() as tmp:
            ob = self._onboarder_with_accepted(
                Path(tmp),
                [self._lex_decision("Widgets", "An unrelated business term")],
            )
            self._write_profile_index(ob, ["PaidAmount"])
            lex = build_workspace_lexicon(ob.layout, Path(tmp))
            # never invents a column
            self.assertNotIn("Widgets", lex.column_aliases)
            self.assertEqual(lex.stats.get("document_lexicon_terms_carried"), 0)
            self.assertEqual(lex.stats.get("document_lexicon_terms_examined"), 1)


if __name__ == "__main__":
    unittest.main()
