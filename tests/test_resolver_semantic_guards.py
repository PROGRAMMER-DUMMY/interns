"""Semantic guards on the resolver's contextual candidate scorer.

Found live on a three-CSV demo workspace: `sum(ContractedRate)` was offered
`payors.PayorName` -- a String column holding "Acme Health", "Beta Care" -- as
a selectable panel option. It scored ~8 purely on name/context overlap
("payor" appears in both the KPI text and the dataset name) and cleared the
threshold, because in `_contextual_score` EVERY value-evidence rule is a
bonus and the only penalty in the whole function is name-based
(`column ends in "id"`, -30).

That is a wrong-number path with a human signature on it: the panel presents
the option, a person accepts it, and the provenance records a human decision.
CLAUDE.md already forbids offering semantically mismatched candidates; these
tests make the code enforce it.

The guard is a DISQUALIFIER, not another bonus. A bonus loses to a +30 name
match; only a disqualifier survives contact with a strong lexical signal.
"""
from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from core.onboarding.kpi.feature_resolver import contextual_column_candidates
from core.storage.workspace_layout import WorkspaceLayout


def _index(*entries: dict) -> dict[str, list[dict]]:
    """A schema_index keyed the way the resolver builds it: normalized column
    name -> entries. Values here mirror a real profile artifact's fields."""
    out: dict[str, list[dict]] = {}
    for entry in entries:
        out.setdefault(entry["column"].lower(), []).append(entry)
    return out


_TEXT_COLUMN = {
    "dataset": "workspaces/demo/datasets/payors.csv",
    "column": "PayorName",
    "dtype": "String",
    "cardinality_ratio": 1.0,
    "value_pattern": "text",
}
_MONEY_COLUMN = {
    "dataset": "workspaces/demo/datasets/payors.csv",
    "column": "PayorRate",
    "dtype": "Float64",
    "cardinality_ratio": 0.8,
    "value_pattern": "currency_2dp",
}

_CONTEXT = "What is the total contracted rate by payor? sum(ContractedRate) PayorID"


class NumericAggregateDisqualifierTests(unittest.TestCase):
    def test_a_text_column_is_not_offered_for_a_summed_feature(self):
        candidates = contextual_column_candidates(
            "ContractedRate",
            _CONTEXT,
            _index(_TEXT_COLUMN),
            metric="sum(ContractedRate)",
        )
        self.assertEqual(
            [c["column"] for c in candidates],
            [],
            "a String column was offered as the definition of a summed measure",
        )

    def test_a_numeric_column_is_still_offered(self):
        """The disqualifier must not simply suppress the whole rule -- a
        legitimate numeric candidate has to survive it."""
        candidates = contextual_column_candidates(
            "ContractedRate",
            _CONTEXT,
            _index(_MONEY_COLUMN),
            metric="sum(ContractedRate)",
        )
        self.assertIn("PayorRate", [c["column"] for c in candidates])

    def test_without_a_numeric_aggregate_a_text_column_stays_eligible(self):
        """`ContractedRate` cut BY a label is a different question from summing
        it. The disqualifier keys on the aggregate, never on the feature name,
        so a non-aggregated feature keeps its text candidates."""
        candidates = contextual_column_candidates(
            "ContractedRate",
            _CONTEXT,
            _index(_TEXT_COLUMN),
            metric="count(ClaimID)",
        )
        self.assertIn("PayorName", [c["column"] for c in candidates])

    def test_an_absent_metric_does_not_disqualify_anything(self):
        """Unknown must stay non-accusing: callers that pass no metric get the
        previous behaviour rather than a silently emptied candidate list."""
        candidates = contextual_column_candidates(
            "ContractedRate", _CONTEXT, _index(_TEXT_COLUMN)
        )
        self.assertIn("PayorName", [c["column"] for c in candidates])

    def test_an_unprofiled_column_is_not_disqualified(self):
        """A column with no dtype recorded has told us nothing. Treating
        missing evidence as a contradiction would drop real candidates on any
        workspace whose profiler could not read types."""
        unprofiled = {**_TEXT_COLUMN, "dtype": "", "value_pattern": ""}
        candidates = contextual_column_candidates(
            "ContractedRate",
            _CONTEXT,
            _index(unprofiled),
            metric="sum(ContractedRate)",
        )
        self.assertIn("PayorName", [c["column"] for c in candidates])


class DerivationDetectorFailureIsVisibleTests(unittest.TestCase):
    """`_derivation_pattern_options` swallowed every exception and returned
    [], so a crashed detector was indistinguishable from "no pattern applies"
    -- and the `pragma: no cover` on that branch meant no test would ever see
    it. This is the same failure class as F25 (a bare `except` turning a
    discovery error into "zero tables"), which cost a full debugging session.

    A detector crash must still not break resolution -- pattern detection is
    genuinely advisory -- but the reason has to survive somewhere a human
    reads.
    """

    def _resolver(self, tmp: str):
        from core.onboarding.kpi.feature_resolver import KPIFeatureResolver

        workspace = Path(tmp) / "workspaces" / "demo"
        (workspace / "interns").mkdir(parents=True, exist_ok=True)
        resolver = KPIFeatureResolver.__new__(KPIFeatureResolver)
        resolver.layout = WorkspaceLayout(project_root=workspace)
        resolver.derivation_pattern_error = ""
        # A real profile index, or the method short-circuits before reaching
        # the detector and the crash path is never exercised.
        resolver.layout.profiles_dir.mkdir(parents=True, exist_ok=True)
        (resolver.layout.profiles_dir / "profile_index.json").write_text(
            json.dumps({"artifact_type": "profile_index.json", "profiles": []}),
            encoding="utf-8",
        )
        return resolver

    def test_a_detector_crash_is_recorded_not_swallowed(self):
        with tempfile.TemporaryDirectory() as tmp:
            resolver = self._resolver(tmp)
            with mock.patch(
                "core.onboarding.features.derivation_patterns.detect_derivation_patterns",
                side_effect=RuntimeError("detector exploded"),
            ):
                options = resolver._derivation_pattern_options({"name": "any question"})
            self.assertEqual(options, [], "a crash must not break resolution")
            self.assertIn("detector exploded", resolver.derivation_pattern_error)
            self.assertIn("RuntimeError", resolver.derivation_pattern_error)

    def test_no_patterns_is_not_reported_as_an_error(self):
        """Absent must stay distinguishable from failed, in both directions."""
        with tempfile.TemporaryDirectory() as tmp:
            resolver = self._resolver(tmp)
            options = resolver._derivation_pattern_options({"name": "any question"})
            self.assertEqual(options, [])
            self.assertEqual(resolver.derivation_pattern_error, "")


if __name__ == "__main__":
    unittest.main()
