"""core.dev.resolver_corpus + resolver_accuracy: the resolver's own gate.

The feature resolver had ~2,000 lines of scoring with hand-tuned weights and no
way to say whether any of it was right, so every change to it was a guess.
These two modules make it measurable. The tests below fix the properties that
make the measurement trustworthy -- above all, that the labels are HUMAN
answers and never the resolver's own output.
"""
from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from core.dev.resolver_accuracy import (
    AccuracyReport,
    Grade,
    grade,
    regressions,
    render_markdown,
)
from core.dev.resolver_corpus import (
    HUMAN_CONFIRMED_STATES,
    LabelledMapping,
    harvest_workspace,
)
from core.storage.workspace_layout import WorkspaceLayout


def _workspace(tmp: str, kpis: list[dict]) -> Path:
    root = Path(tmp)
    layout = WorkspaceLayout(project_root=root / "workspaces" / "demo")
    layout.ensure_runtime_dirs()
    layout.kpi_feature_mapping_path.write_text(
        json.dumps({"kpis": kpis}), encoding="utf-8"
    )
    return root


def _kpi(state: str, feature: str = "LineOfBusiness") -> dict:
    return {
        "kpi_id": "kpi_001",
        "name": "What is paid amount by line of business?",
        "metric": "sum(PaidAmount)",
        "cuts": "LineOfBusiness",
        "features": [{
            "feature": feature,
            "state": state,
            "source_columns": [{"dataset": "`c`.`s`.`transactions`", "column": feature}],
        }],
    }


class LabelProvenanceTests(unittest.TestCase):
    """The load-bearing property: a label is a HUMAN statement."""

    def test_only_user_confirmed_becomes_a_label(self):
        self.assertEqual(HUMAN_CONFIRMED_STATES, ("user_confirmed",))

    def test_resolver_own_verdicts_are_never_labels(self):
        # Grading against proven_* would measure self-consistency and report a
        # confidently-wrong resolver as accurate.
        for state in ("proven_direct", "proven_alias", "proven_join",
                      "proven_formula", "proven_taxonomy", "blocked_ambiguous"):
            with self.subTest(state=state), tempfile.TemporaryDirectory() as tmp:
                root = _workspace(tmp, [_kpi(state)])
                self.assertEqual(harvest_workspace(root, "workspaces/demo"), [])

    def test_a_user_confirmed_feature_becomes_one_label(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = _workspace(tmp, [_kpi("user_confirmed")])
            labels = harvest_workspace(root, "workspaces/demo")
            self.assertEqual(len(labels), 1)
            self.assertEqual(labels[0].feature, "LineOfBusiness")
            self.assertEqual(labels[0].expected_column, "LineOfBusiness")
            self.assertEqual(labels[0].label_source, "user_confirmed")
            # The context must be the SAME string production scores against.
            self.assertIn("line of business", labels[0].context.lower())

    def test_a_feature_with_no_source_column_is_not_a_label(self):
        with tempfile.TemporaryDirectory() as tmp:
            kpi = _kpi("user_confirmed")
            kpi["features"][0]["source_columns"] = []
            root = _workspace(tmp, [kpi])
            self.assertEqual(harvest_workspace(root, "workspaces/demo"), [])

    def test_workspace_definitions_are_harvested_too(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = _workspace(tmp, [])
            layout = WorkspaceLayout(project_root=root / "workspaces" / "demo")
            memory = layout.generated_dir / "memory"
            memory.mkdir(parents=True, exist_ok=True)
            (memory / "workspace_definitions.json").write_text(json.dumps({
                "definitions": [{
                    "feature": "Gender",
                    "definition": "patient gender",
                    "source_columns": [
                        {"dataset": "`c`.`s`.`patients`", "column": "Gender"}
                    ],
                }]
            }), encoding="utf-8")
            labels = harvest_workspace(root, "workspaces/demo")
            self.assertEqual(len(labels), 1)
            self.assertEqual(labels[0].label_source, "workspace_definition")

    def test_a_corrupt_mapping_yields_no_labels_rather_than_crashing(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = _workspace(tmp, [])
            layout = WorkspaceLayout(project_root=root / "workspaces" / "demo")
            layout.kpi_feature_mapping_path.write_text("{ truncated", encoding="utf-8")
            self.assertEqual(harvest_workspace(root, "workspaces/demo"), [])


class OutcomeTests(unittest.TestCase):
    """An abstention is a blocker raised, not a miss."""

    def _report(self, outcomes: list[str]) -> AccuracyReport:
        label = LabelledMapping(
            workspace="w", kpi_id="k", feature="f", context="c",
            expected_dataset="d", expected_column="col", label_source="user_confirmed",
        )
        return AccuracyReport(grades=[Grade(label=label, outcome=o) for o in outcomes])

    def test_abstentions_are_excluded_from_precision(self):
        report = self._report(["correct", "abstained", "abstained"])
        # 1 of 1 ANSWERED, not 1 of 3.
        self.assertEqual(report.precision, 1.0)
        self.assertAlmostEqual(report.answer_rate, 0.3333, places=3)

    def test_a_wrong_answer_costs_precision(self):
        report = self._report(["correct", "wrong"])
        self.assertEqual(report.precision, 0.5)
        self.assertEqual(report.answer_rate, 1.0)

    def test_answering_nothing_is_not_reported_as_perfect(self):
        report = self._report(["abstained", "abstained"])
        self.assertEqual(report.precision, 0.0)
        self.assertEqual(report.answer_rate, 0.0)


class RegressionGateTests(unittest.TestCase):
    def test_more_wrong_answers_is_a_regression(self):
        report = AccuracyReport(grades=[
            Grade(label=LabelledMapping("w", "k", "f", "c", "d", "col", "user_confirmed"),
                  outcome="wrong"),
        ])
        found = regressions(report, {"correct": 1, "wrong": 0})
        self.assertEqual(len(found), 2)  # wrong rose AND correct fell
        self.assertIn("wrong answers rose", found[0])

    def test_trading_a_correct_answer_for_an_abstention_is_a_regression(self):
        # Refusing beats guessing, but silently refusing MORE is still a loss.
        report = AccuracyReport(grades=[
            Grade(label=LabelledMapping("w", "k", "f", "c", "d", "col", "user_confirmed"),
                  outcome="abstained"),
        ])
        found = regressions(report, {"correct": 1, "wrong": 0})
        self.assertTrue(any("correct answers fell" in f for f in found))

    def test_trading_a_wrong_answer_for_an_abstention_is_an_improvement(self):
        report = AccuracyReport(grades=[
            Grade(label=LabelledMapping("w", "k", "f", "c", "d", "col", "user_confirmed"),
                  outcome="abstained"),
        ])
        self.assertEqual(regressions(report, {"correct": 0, "wrong": 1}), [])

    def test_no_baseline_never_reports_a_regression(self):
        self.assertEqual(regressions(AccuracyReport(), {}), [])

    def test_an_empty_corpus_never_reports_a_regression(self):
        # The corpus is harvested from `workspaces/`, which is untracked. On a
        # fresh checkout (or any machine but the one that wrote the baseline)
        # there are zero labels, so `correct` is 0 against a baseline of 5 --
        # a measurement that never ran must not be reported as a loss.
        self.assertEqual(
            regressions(AccuracyReport(), {"correct": 5, "wrong": 0, "total": 5}),
            [],
        )


class GradingTests(unittest.TestCase):
    def test_it_grades_against_the_real_resolver_and_matches_on_short_names(self):
        # The corpus stores a fully-qualified UC identifier; the schema index may
        # carry either form. A mismatch here would report every UC workspace as
        # 100% wrong.
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            layout = WorkspaceLayout(project_root=root / "workspaces" / "demo")
            layout.ensure_runtime_dirs()
            layout.profile_index_path.write_text(json.dumps({
                "profiles": [{
                    "path": "`c`.`s`.`transactions`",
                    "schema": {"LineOfBusiness": "string", "PaidAmount": "double"},
                }]
            }), encoding="utf-8")
            label = LabelledMapping(
                workspace="workspaces/demo", kpi_id="kpi_001",
                feature="LineOfBusiness",
                context="What is paid amount by line of business? sum(PaidAmount) LineOfBusiness",
                expected_dataset="`c`.`s`.`transactions`",
                expected_column="LineOfBusiness",
                label_source="user_confirmed",
            )
            report = grade([label], root)
            self.assertEqual(report.total, 1)
            self.assertEqual(report.grades[0].outcome, "correct")
            self.assertEqual(report.grades[0].rank_of_expected, 1)

    def test_markdown_renders_every_outcome_with_ascii_markers(self):
        label = LabelledMapping("w", "k", "f", "c", "d", "col", "user_confirmed")
        md = render_markdown(
            AccuracyReport(grades=[
                Grade(label=label, outcome="correct"),
                Grade(label=label, outcome="wrong"),
                Grade(label=label, outcome="abstained"),
            ]),
            {},
        )
        for marker in ("[ok]", "[x]", "[~]"):
            self.assertIn(marker, md)
        self.assertTrue(md.isascii(), "report must be ASCII (repo rule)")


if __name__ == "__main__":
    unittest.main()
