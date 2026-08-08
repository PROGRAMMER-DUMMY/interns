"""Priority 1.1: PHI/PII review-and-consent panel.

Contract under test:
- The panel lists every semantic_contract.json column flagged is_sensitive.
- Answering `not_sensitive` writes to the user-authored data_policy.json's
  `not_sensitive_columns` allowlist, with confirmed_by provenance -- and that
  allowlist entry actually suppresses the sensitivity flag (is_allowlisted).
- Answering a real disposition (hash_to_key / pass_through_and_tag /
  bronze_only) writes to the new phi_disposition.json contract, not
  data_policy.json.
- Agent-asserted confirmed_by is refused outright -- nothing is written.
- A column already answered drops out of the next panel's pending list.
"""
from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from core.governance.data_policy import is_allowlisted, load_workspace_data_policy
from core.onboarding.kpi.phi_review_panel import (
    NOT_SENSITIVE_ANSWER,
    PHIReviewPanelBuilder,
    apply_phi_review_answer,
)
from core.storage.workspace_layout import WorkspaceLayout


def _write_json(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload), encoding="utf-8")


def _fixture_workspace(tmp: str) -> tuple[Path, Path]:
    repo = Path(tmp)
    ws = repo / "workspaces" / "demo"
    layout = WorkspaceLayout(project_root=ws)
    _write_json(
        layout.contracts_dir / "semantic_contract.json",
        {
            "columns": {
                "SSN": {"is_sensitive": True, "category": "ssn"},
                "PatientName": {"is_sensitive": True, "category": "name"},
                "InternalRiskScore": {"is_sensitive": True, "category": "policy:trade_secret"},
                "OrderID": {"is_sensitive": False},
            }
        },
    )
    return repo, ws


class PHIReviewPanelTests(unittest.TestCase):
    def test_panel_lists_only_sensitive_unanswered_columns(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo, ws = _fixture_workspace(tmp)
            result = PHIReviewPanelBuilder(repo, ws).run()

            self.assertEqual(result.column_count, 3)
            current = json.loads((repo / result.current_json).read_text(encoding="utf-8"))
            names = {c["column"] for c in current["columns"]}
            self.assertEqual(names, {"SSN", "PatientName", "InternalRiskScore"})
            self.assertEqual(current["status"], "needs_user_answer")

    def test_accept_two_override_one_writes_correctly(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo, ws = _fixture_workspace(tmp)

            apply_phi_review_answer(
                repo, ws, column="SSN", answer="hash_to_key", confirmed_by="Dr. Smith"
            )
            apply_phi_review_answer(
                repo, ws, column="InternalRiskScore", answer="pass_through_and_tag",
                confirmed_by="Dr. Smith",
            )
            apply_phi_review_answer(
                repo, ws, column="PatientName", answer=NOT_SENSITIVE_ANSWER,
                confirmed_by="Dr. Smith",
            )

            # -- The 2 accepted-as-sensitive columns: recorded in phi_disposition.json,
            # not in data_policy.json.
            layout = WorkspaceLayout(project_root=ws)
            disposition = json.loads(
                (layout.contracts_dir / "phi_disposition.json").read_text(encoding="utf-8")
            )
            by_column = {r["column"]: r for r in disposition["columns"]}
            self.assertEqual(set(by_column), {"SSN", "InternalRiskScore"})
            self.assertEqual(by_column["SSN"]["disposition"], "hash_to_key")
            self.assertEqual(by_column["SSN"]["confirmed_by"], "Dr. Smith")
            self.assertEqual(by_column["SSN"]["source"], "human")
            self.assertEqual(by_column["InternalRiskScore"]["disposition"], "pass_through_and_tag")

            # -- The 1 override: exactly one entry in data_policy.json's allowlist,
            # with confirmed_by recorded.
            policy_path = ws / "data_policy.json"
            policy_data = json.loads(policy_path.read_text(encoding="utf-8"))
            self.assertEqual(policy_data["not_sensitive_columns"], ["PatientName"])
            self.assertEqual(
                policy_data["not_sensitive_columns_confirmed_by"]["PatientName"]["confirmed_by"],
                "Dr. Smith",
            )

            # -- The override actually suppresses the sensitivity flag (this is what
            # "the SQL generator masks the other 2, not this one" cashes out to --
            # is_allowlisted is exactly what _sensitive_columns_section() consults).
            policy = load_workspace_data_policy(ws)
            self.assertTrue(is_allowlisted(policy, "PatientName"))
            self.assertFalse(is_allowlisted(policy, "SSN"))
            self.assertFalse(is_allowlisted(policy, "InternalRiskScore"))

    def test_agent_asserted_confirmer_is_refused_and_writes_nothing(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo, ws = _fixture_workspace(tmp)

            with self.assertRaises(ValueError):
                apply_phi_review_answer(
                    repo, ws, column="SSN", answer="hash_to_key", confirmed_by="claude"
                )
            with self.assertRaises(ValueError):
                apply_phi_review_answer(
                    repo, ws, column="SSN", answer="hash_to_key", confirmed_by=""
                )

            layout = WorkspaceLayout(project_root=ws)
            self.assertFalse((layout.contracts_dir / "phi_disposition.json").exists())
            self.assertFalse((ws / "data_policy.json").exists())

    def test_invalid_answer_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo, ws = _fixture_workspace(tmp)
            with self.assertRaises(ValueError):
                apply_phi_review_answer(
                    repo, ws, column="SSN", answer="delete_it", confirmed_by="Dr. Smith"
                )

    def test_answered_column_drops_out_of_next_panel(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo, ws = _fixture_workspace(tmp)
            apply_phi_review_answer(
                repo, ws, column="SSN", answer="hash_to_key", confirmed_by="Dr. Smith"
            )
            result = PHIReviewPanelBuilder(repo, ws).run()
            self.assertEqual(result.column_count, 2)
            current = json.loads((repo / result.current_json).read_text(encoding="utf-8"))
            names = {c["column"] for c in current["columns"]}
            self.assertEqual(names, {"PatientName", "InternalRiskScore"})

    def test_not_sensitive_answer_also_drops_out_of_next_panel(self) -> None:
        # Found live (RCM workspace): a `not_sensitive` answer is written to
        # data_policy.json's not_sensitive_columns allowlist, a DIFFERENT file
        # from phi_disposition.json (where the 3 real dispositions go). The
        # panel's pending computation only ever checked phi_disposition.json
        # and semantic_contract.json's (never-updated-post-answer) is_sensitive
        # snapshot -- so a `not_sensitive` answer could never satisfy this
        # gate. All 10 real columns answered `not_sensitive` for a real
        # workspace still showed up as pending on every re-generation,
        # forever, no matter how many times they were answered.
        with tempfile.TemporaryDirectory() as tmp:
            repo, ws = _fixture_workspace(tmp)
            apply_phi_review_answer(
                repo, ws, column="PatientName", answer=NOT_SENSITIVE_ANSWER,
                confirmed_by="Dr. Smith",
            )
            result = PHIReviewPanelBuilder(repo, ws).run()
            self.assertEqual(result.column_count, 2)
            current = json.loads((repo / result.current_json).read_text(encoding="utf-8"))
            names = {c["column"] for c in current["columns"]}
            self.assertEqual(names, {"SSN", "InternalRiskScore"})
            self.assertNotIn("PatientName", names)

    def test_hand_authored_allowlist_entry_also_suppresses_the_panel(self) -> None:
        # data_policy.json is user-authored input (AGENTS.md) -- a workspace
        # owner can hand-write not_sensitive_columns directly, bypassing this
        # panel/apply_phi_review_answer entirely. The panel must honor that
        # pre-existing declaration too, not only entries it wrote itself.
        with tempfile.TemporaryDirectory() as tmp:
            repo, ws = _fixture_workspace(tmp)
            _write_json(ws / "data_policy.json", {"not_sensitive_columns": ["SSN"]})
            result = PHIReviewPanelBuilder(repo, ws).run()
            self.assertEqual(result.column_count, 2)
            current = json.loads((repo / result.current_json).read_text(encoding="utf-8"))
            names = {c["column"] for c in current["columns"]}
            self.assertEqual(names, {"PatientName", "InternalRiskScore"})


if __name__ == "__main__":
    unittest.main()


class ProvenanceWordMatchingTests(unittest.TestCase):
    """An agent identity must not launder itself into a human approval.

    Found live during a real replay: the check normalised the WHOLE string and
    did an exact set-membership test, so `agent (platform recommendation)`
    became `agentplatformrecommendation`, matched nothing, and was recorded as
    `human` -- defeating every gate that refuses agent identities. Any agent
    could bypass a human gate by appending one word to its name.
    """

    def test_agent_identity_with_extra_words_is_still_an_agent(self):
        from core.governance.provenance import decision_source

        for identity in (
            "agent (platform recommendation)",
            "Claude Opus",
            "automated run",
            "data team (automation)",
            "cli agent",
            "bot-3",
        ):
            self.assertEqual(decision_source(identity), "agent", identity)

    def test_real_human_names_are_still_human(self):
        from core.governance.provenance import decision_source

        for identity in ("shubham", "Dr. Smith", "alice", "Priya Nair", "j.doe@corp.com"):
            self.assertEqual(decision_source(identity), "human", identity)

    def test_empty_or_blank_is_an_agent(self):
        from core.governance.provenance import decision_source

        for identity in ("", "   ", None):
            self.assertEqual(decision_source(identity), "agent", repr(identity))
