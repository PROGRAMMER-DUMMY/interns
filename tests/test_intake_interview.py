"""Intake interview: merged question set, panel envelope, and measured-beats-asked."""
from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from core.intake.interview import (
    QUESTIONS,
    QUESTIONS_BY_ID,
    IntakeAnswerRecorder,
    IntakePanelBuilder,
    load_intake_answers,
)
from core.onboarding.workspace.delegation import routing_for
from core.storage.workspace_layout import WorkspaceLayout


class _TempWorkspace(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.repo_root = Path(self._tmp.name)
        self.workspace = self.repo_root / "workspaces" / "sample_ws"
        self.workspace.mkdir(parents=True)
        self.layout = WorkspaceLayout(project_root=self.workspace)
        self.addCleanup(self._tmp.cleanup)

    def write_discovery(self, payload: dict) -> None:
        path = self.layout.generated_dir / "intake" / "discovery.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(payload), encoding="utf-8")

    def write_answers(self, answers: dict) -> None:
        path = self.layout.generated_dir / "intake" / "intake_answers.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps({k: {"answer": v, "answered_by": "fixture", "at": "t"} for k, v in answers.items()}),
            encoding="utf-8",
        )

    def playback(self) -> str:
        return (self.layout.reports_dir / "intake_playback" / "current.md").read_text(
            encoding="utf-8"
        )

    def panel(self) -> dict:
        return json.loads(
            (self.layout.reports_dir / "intake_panel" / "current.json").read_text(encoding="utf-8")
        )

    def markdown(self) -> str:
        return (self.layout.reports_dir / "intake_panel" / "current.md").read_text(encoding="utf-8")


class TestQuestionSet(unittest.TestCase):
    def test_question_ids_are_unique(self):
        ids = [question["id"] for question in QUESTIONS]
        self.assertEqual(len(ids), len(set(ids)))

    def test_every_question_carries_its_contract_fields(self):
        for question in QUESTIONS:
            with self.subTest(question=question["id"]):
                for field in ("id", "text", "why_it_matters", "answer_type", "maps_to", "merged_from"):
                    self.assertTrue(question.get(field), f"{question['id']} missing {field}")

    def test_both_research_question_sets_are_represented(self):
        sources = {
            source.split(":")[0]
            for question in QUESTIONS
            for source in question["merged_from"]
        }
        self.assertEqual(sources, {"end_users", "engine"})

    def test_the_merge_is_deduplicated_not_concatenated(self):
        # 14 (end_users) + 12 (engine) = 26 asked separately; the merged set is
        # smaller, and every merged question names more than one origin.
        self.assertLess(len(QUESTIONS), 26)
        merged = [q for q in QUESTIONS if len(q["merged_from"]) > 1]
        self.assertTrue(merged)

    def test_option_backed_questions_have_unique_option_ids(self):
        for question in QUESTIONS:
            options = question.get("options") or []
            ids = [option["option_id"] for option in options]
            self.assertEqual(len(ids), len(set(ids)), question["id"])

    def test_lookup_covers_the_asked_and_the_system_questions(self):
        from core.intake.interview import SYSTEM_QUESTIONS

        self.assertEqual(
            {q["id"] for q in QUESTIONS} | {q["id"] for q in SYSTEM_QUESTIONS},
            set(QUESTIONS_BY_ID),
        )
        for question in SYSTEM_QUESTIONS:
            self.assertTrue(question.get("hidden"), f"{question['id']} must be hidden")
        for question in QUESTIONS:
            self.assertFalse(question.get("hidden"), f"{question['id']} must not be hidden")

    def test_recommended_option_is_a_real_option(self):
        for question in QUESTIONS:
            recommended = question.get("recommended_option_id") or ""
            if not recommended:
                continue
            ids = {option["option_id"] for option in question.get("options") or []}
            self.assertIn(recommended, ids, question["id"])


class TestPanel(_TempWorkspace):
    def test_fresh_workspace_asks_the_first_question(self):
        result = IntakePanelBuilder(self.repo_root, "workspaces/sample_ws").prepare()
        panel = self.panel()
        self.assertEqual(result.status, "needs_user_answer")
        self.assertEqual(panel["current_question_id"], QUESTIONS[0]["id"])
        self.assertEqual(panel["pending_count"], len(QUESTIONS))

    def test_markdown_uses_ascii_markers_and_no_emoji(self):
        IntakePanelBuilder(self.repo_root, "workspaces/sample_ws").prepare()
        text = self.markdown()
        self.assertIn("[x]", text)
        self.assertTrue(all(ord(char) < 0x2190 for char in text), "non-ASCII symbol in panel")

    def test_measured_volume_is_not_asked(self):
        self.write_discovery(
            {
                "status": "ok",
                "working_set_estimate_bytes": 4096,
                "tables": [{"name": "t", "size_bytes": 4096}],
            }
        )
        result = IntakePanelBuilder(self.repo_root, "workspaces/sample_ws").prepare()
        states = {q["id"]: q["state"] for q in self.panel()["questions"]}
        self.assertEqual(states["volume_and_growth"], "skipped_measured")
        self.assertEqual(result.skipped_count, 1)
        self.assertIn("[~]", self.markdown())

    def test_unmeasured_volume_is_still_asked(self):
        self.write_discovery({"status": "ok", "working_set_estimate_bytes": None, "tables": []})
        IntakePanelBuilder(self.repo_root, "workspaces/sample_ws").prepare()
        states = {q["id"]: q["state"] for q in self.panel()["questions"]}
        self.assertEqual(states["volume_and_growth"], "pending")


class TestVelocityQuestion(_TempWorkspace):
    """`target_latency` is asked unless the arrival pattern was measured AND the
    freshness SLA the user gave is coherent with it."""

    def _discovery(self, pattern: str) -> dict:
        return {
            "status": "ok",
            "working_set_estimate_bytes": 4096,
            "tables": [
                {"name": "t", "size_bytes": 4096, "arrival_pattern": pattern, "is_streaming": False}
            ],
        }

    def test_question_offers_the_four_lanes_and_maps_to_the_velocity_lane(self):
        question = QUESTIONS_BY_ID["target_latency"]
        self.assertEqual(
            {"batch_daily", "micro_batch_hourly", "streaming_continuous", "realtime_serving"},
            {option["option_id"] for option in question["options"]},
        )
        self.assertIn("lane.velocity", question["maps_to"])

    def test_it_is_asked_when_the_arrival_pattern_could_not_be_measured(self):
        self.write_discovery(self._discovery("unknown"))
        self.write_answers({"freshness_sla": "daily"})
        IntakePanelBuilder(self.repo_root, "workspaces/sample_ws").prepare()
        states = {q["id"]: q["state"] for q in self.panel()["questions"]}
        self.assertEqual("pending", states["target_latency"])

    def test_it_is_asked_when_the_sla_contradicts_the_measured_pattern(self):
        self.write_discovery(self._discovery("one_shot"))
        self.write_answers({"freshness_sla": "sub_minute"})
        IntakePanelBuilder(self.repo_root, "workspaces/sample_ws").prepare()
        states = {q["id"]: q["state"] for q in self.panel()["questions"]}
        self.assertEqual("pending", states["target_latency"])

    def test_it_is_skipped_when_the_pattern_is_measured_and_the_sla_agrees(self):
        self.write_discovery(self._discovery("periodic"))
        self.write_answers({"freshness_sla": "daily"})
        IntakePanelBuilder(self.repo_root, "workspaces/sample_ws").prepare()
        entry = next(q for q in self.panel()["questions"] if q["id"] == "target_latency")
        self.assertEqual("skipped_measured", entry["state"])
        self.assertEqual("batch", entry["measured_evidence"]["implied_lane"])


class TestPlayback(_TempWorkspace):
    ANSWERS = {
        "consumers": "bi_dashboard",
        "decisions_driven": "whether to reorder stock",
        "freshness_sla": "daily",
        "grain_sentence": "one row per order per day",
        "as_it_was_then": "as_it_is_now",
        "history_backfill_retention": "3 years queryable, raw kept 7 years",
        "target_latency": "batch_daily",
    }

    def test_playback_is_written_by_the_panel_step(self):
        self.write_answers(self.ANSWERS)
        result = IntakePanelBuilder(self.repo_root, "workspaces/sample_ws").prepare()
        text = self.playback()
        self.assertTrue(result.playback_markdown_path.endswith("intake_playback/current.md"))
        for expected in (
            "bi_dashboard",
            "whether to reorder stock",
            "one row per order per day",
            "daily",
            "3 years queryable",
        ):
            self.assertIn(expected, text)

    def test_every_line_is_tagged_with_where_it_came_from(self):
        self.write_discovery(
            {
                "status": "ok",
                "working_set_estimate_bytes": 10,
                "tables": [{"name": "t", "size_bytes": 10, "arrival_pattern": "periodic"}],
            }
        )
        self.write_answers(self.ANSWERS)
        IntakePanelBuilder(self.repo_root, "workspaces/sample_ws").prepare()
        text = self.playback()
        self.assertIn("(you said)", text)
        self.assertIn("(measured)", text)
        self.assertIn("(default)", text)
        for line in text.splitlines():
            if line.startswith("- "):
                self.assertTrue(
                    line.endswith("(you said)")
                    or line.endswith("(measured)")
                    or line.endswith("(default)"),
                    f"untagged playback line: {line}",
                )

    def test_playback_names_the_lane_and_the_confirm_command(self):
        self.write_answers(self.ANSWERS)
        IntakePanelBuilder(self.repo_root, "workspaces/sample_ws").prepare()
        text = self.playback()
        self.assertIn("Velocity lane", text)
        self.assertIn("--question playback_confirm --answer confirmed", text)

    def test_playback_is_ascii_only(self):
        self.write_answers(self.ANSWERS)
        IntakePanelBuilder(self.repo_root, "workspaces/sample_ws").prepare()
        self.assertTrue(all(ord(ch) < 0x2190 for ch in self.playback()))

    def test_playback_confirm_is_a_system_question_not_shown_in_the_panel(self):
        IntakePanelBuilder(self.repo_root, "workspaces/sample_ws").prepare()
        panel = self.panel()
        self.assertNotIn("playback_confirm", {q["id"] for q in panel["questions"]})
        self.assertEqual(len(QUESTIONS), panel["pending_count"])
        self.assertIn("playback_confirm", QUESTIONS_BY_ID)

    def test_playback_confirm_can_be_answered(self):
        IntakeAnswerRecorder(self.repo_root, "workspaces/sample_ws").apply(
            "playback_confirm", "confirmed", answered_by="Reviewer"
        )
        self.assertEqual(
            "confirmed", load_intake_answers(self.layout)["playback_confirm"]["answer"]
        )


class TestReIntakeInvalidatesAlignment(_TempWorkspace):
    def _confirm_blueprint_exists(self) -> None:
        path = self.layout.reports_dir / "solution_blueprint" / "current.confirmed.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({"status": "confirmed"}), encoding="utf-8")

    def test_changing_an_answer_after_confirmation_invalidates_the_playback(self):
        recorder = IntakeAnswerRecorder(self.repo_root, "workspaces/sample_ws")
        recorder.apply("freshness_sla", "daily", answered_by="Reviewer")
        recorder.apply("playback_confirm", "confirmed", answered_by="Reviewer")
        self._confirm_blueprint_exists()

        out = recorder.apply("freshness_sla", "hourly", answered_by="Reviewer")

        self.assertTrue(out["playback_invalidated"])
        self.assertNotIn("playback_confirm", load_intake_answers(self.layout))

    def test_re_answering_with_the_same_value_keeps_the_alignment(self):
        recorder = IntakeAnswerRecorder(self.repo_root, "workspaces/sample_ws")
        recorder.apply("freshness_sla", "daily", answered_by="Reviewer")
        recorder.apply("playback_confirm", "confirmed", answered_by="Reviewer")
        self._confirm_blueprint_exists()

        out = recorder.apply("freshness_sla", "daily", answered_by="Reviewer")

        self.assertFalse(out["playback_invalidated"])
        self.assertIn("playback_confirm", load_intake_answers(self.layout))

    def test_a_first_answer_before_any_confirmation_does_not_invalidate(self):
        recorder = IntakeAnswerRecorder(self.repo_root, "workspaces/sample_ws")
        recorder.apply("playback_confirm", "confirmed", answered_by="Reviewer")
        out = recorder.apply("freshness_sla", "daily", answered_by="Reviewer")
        self.assertFalse(out["playback_invalidated"])
        self.assertIn("playback_confirm", load_intake_answers(self.layout))


class TestAnswers(_TempWorkspace):
    def setUp(self) -> None:
        super().setUp()
        self.recorder = IntakeAnswerRecorder(self.repo_root, "workspaces/sample_ws")

    def test_option_answer_is_persisted_with_provenance(self):
        out = self.recorder.apply("freshness_sla", "daily", answered_by="Reviewer")
        answers = load_intake_answers(self.layout)
        self.assertEqual(answers["freshness_sla"]["answer"], "daily")
        self.assertEqual(answers["freshness_sla"]["answered_by"], "Reviewer")
        self.assertEqual(answers["freshness_sla"]["source"], "human")
        self.assertTrue(answers["freshness_sla"]["at"])
        self.assertEqual(out["maps_to"], QUESTIONS_BY_ID["freshness_sla"]["maps_to"])

    def test_empty_answered_by_is_recorded_as_agent_asserted(self):
        self.recorder.apply("freshness_sla", "daily")
        self.assertEqual(load_intake_answers(self.layout)["freshness_sla"]["source"], "agent")

    def test_answer_by_label_resolves_to_the_option_id(self):
        label = QUESTIONS_BY_ID["as_it_was_then"]["options"][1]["label"]
        self.recorder.apply("as_it_was_then", label)
        self.assertEqual(
            load_intake_answers(self.layout)["as_it_was_then"]["answer"], "as_it_was_then"
        )

    def test_multi_select_accepts_a_comma_separated_list(self):
        self.recorder.apply("consumers", "bi_dashboard, regulatory")
        self.assertEqual(
            load_intake_answers(self.layout)["consumers"]["answer"], "bi_dashboard,regulatory"
        )

    def test_option_backed_question_rejects_free_text(self):
        with self.assertRaises(ValueError):
            self.recorder.apply("freshness_sla", "whenever")

    def test_free_text_question_accepts_text_but_not_blank(self):
        self.recorder.apply("grain_sentence", "one row per order per day")
        self.assertEqual(
            load_intake_answers(self.layout)["grain_sentence"]["answer"],
            "one row per order per day",
        )
        with self.assertRaises(ValueError):
            self.recorder.apply("grain_sentence", "   ")

    def test_unknown_question_is_rejected(self):
        with self.assertRaises(ValueError):
            self.recorder.apply("not_a_question", "yes")

    def test_answering_advances_the_panel(self):
        first = QUESTIONS[0]["id"]
        out = self.recorder.apply(first, "bi_dashboard")
        self.assertEqual(out["next_panel"]["answered_count"], 1)
        self.assertNotEqual(out["next_panel"]["current_question_id"], first)
        self.assertIn("[ok]", self.markdown())

    def test_reanswering_overwrites_rather_than_appending(self):
        self.recorder.apply("freshness_sla", "daily", answered_by="Reviewer")
        self.recorder.apply("freshness_sla", "hourly", answered_by="Reviewer")
        answers = load_intake_answers(self.layout)
        self.assertEqual(answers["freshness_sla"]["answer"], "hourly")
        self.assertEqual(len(answers), 1)


class StageRoutingTests(_TempWorkspace):
    def test_panel_and_result_carry_the_intake_interview_roster(self):
        result = IntakePanelBuilder(self.repo_root, "workspaces/sample_ws").prepare()
        roster = routing_for("intake_interview")
        self.assertTrue(roster["agents"], "intake_interview routes no agent")
        for label, payload in (("panel", self.panel()), ("result", result.summary())):
            with self.subTest(payload=label):
                self.assertEqual(payload["stage"], "intake_interview")
                self.assertEqual(payload["required_specialists"], roster["agents"])
                self.assertEqual(payload["suggested_skills"], roster["skills"])


if __name__ == "__main__":
    unittest.main()
