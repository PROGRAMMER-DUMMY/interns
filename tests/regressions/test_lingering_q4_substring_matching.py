"""Regressions for Q4 (lingering-issues plan): substring-to-token matching
remainder (T9). See ~/.claude/plans/dynamic-cooking-firefly.md Q4.

- derivation_patterns._name_has: word-boundary, not raw substring, so 2-char
  start/stop hints ("in"/"to"/"out") don't misfire on unrelated column names.
- intent_coverage.prose_filter_findings: an age-threshold integer must appear
  in a real comparison/DATEDIFF context, not merely as a bare substring
  anywhere in the generated SQL (a LIMIT clause, a year, ...).
- external_discovery: a .jsonl file under a logs/ directory classifies as a
  log, not an ingestible dataset candidate.
"""
from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from core.onboarding.features.derivation_patterns import _START_HINTS, _STOP_HINTS, _name_has
from core.onboarding.kpi.intent_coverage import _age_threshold_present, prose_filter_findings
from core.onboarding.sources.external_discovery import ExternalSourceDiscoverer


class NameHasWordBoundaryTests(unittest.TestCase):
    def test_columns_merely_containing_the_hint_text_do_not_match(self):
        # "insured_amount" contains "in"; "total_amount" contains "to";
        # "routing_number" contains both "in" and "out"; "origin_code"
        # contains "in" -- none of these are genuine start/stop columns.
        for name in ("insured_amount", "total_amount", "routing_number", "origin_code"):
            col = {"column": name}
            self.assertFalse(_name_has(col, _START_HINTS), name)
            self.assertFalse(_name_has(col, _STOP_HINTS), name)

    def test_genuine_delimited_hint_segment_still_matches(self):
        self.assertTrue(_name_has({"column": "admit_date"}, _START_HINTS))
        self.assertTrue(_name_has({"column": "discharge_date"}, _STOP_HINTS))
        self.assertTrue(_name_has({"column": "start_time"}, _START_HINTS))
        self.assertTrue(_name_has({"column": "end_time"}, _STOP_HINTS))


class AgeThresholdContextTests(unittest.TestCase):
    def test_bare_digit_in_unrelated_sql_is_not_accepted(self):
        # A LIMIT clause and a year both contain the digit "5" as a bare
        # substring; neither is a real age-threshold comparison.
        self.assertFalse(_age_threshold_present("5", "select * from t limit 5"))
        self.assertFalse(_age_threshold_present("5", "select * from t where yr = 2025"))

    def test_real_comparison_context_is_accepted(self):
        self.assertTrue(_age_threshold_present("5", "where age > 5"))
        self.assertTrue(_age_threshold_present("5", "where age >= 5"))
        self.assertTrue(_age_threshold_present("5", "where age < 6"))
        self.assertTrue(_age_threshold_present("5", "where datediff('year', dob, now()) >= 5"))

    def test_prose_filter_findings_end_to_end(self):
        kpi = {"kpi_id": "kpi_001", "name": "Patients above 5 years old"}
        # SQL with only an unrelated "5" (a LIMIT clause) must still report the
        # filter as not realized.
        findings = prose_filter_findings(kpi, "select * from patients limit 5")
        self.assertTrue(any(f.code == "filter_not_realized" for f in findings))
        # SQL with a real comparison against the threshold clears the finding.
        findings = prose_filter_findings(kpi, "select * from patients where age >= 5")
        self.assertFalse(findings)


class JsonlLogClassificationTests(unittest.TestCase):
    def test_jsonl_under_logs_dir_classifies_as_log_not_dataset(self):
        with tempfile.TemporaryDirectory() as td:
            repo_root = Path(td)
            external_root = repo_root / "ext"
            (external_root / "logs").mkdir(parents=True)
            (external_root / "logs" / "app.jsonl").write_text("{}\n", encoding="utf-8")
            (external_root / "data.jsonl").write_text('{"a": 1}\n', encoding="utf-8")
            discoverer = ExternalSourceDiscoverer(repo_root, "workspaces/demo", external_root)
            classes = discoverer._classify([
                external_root / "logs" / "app.jsonl",
                external_root / "data.jsonl",
            ])
            by_path = {c.relative_path: c for c in classes}
            self.assertEqual(by_path["logs/app.jsonl"].class_name, "log_or_state")
            self.assertEqual(by_path["data.jsonl"].class_name, "dataset")


if __name__ == "__main__":
    unittest.main()
