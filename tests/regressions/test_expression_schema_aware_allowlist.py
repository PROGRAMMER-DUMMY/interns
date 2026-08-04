"""Regression: a stopword that is also a REAL column name must survive.

Task 1 widened BUSINESS_TEXT_STOPWORDS to stop formula/statistical
vocabulary ("std", "benchmark", "high") from surfacing as KPI features
needing resolution. That list is workspace-agnostic by design -- and that
is exactly why it over-reaches: "high"/"low" are the canonical OHLC
columns of any market-data workspace, and "score"/"weight"/"flag"/
"actual"/"expected" are ordinary column names somewhere. Extraction was
dropping them silently, with no blocker raised, so the KPI resolved
against a column the formula never named.

The same reasoning the plan applied to "LOS" applies here, one step
further: only the workspace's OWN schema can tell a generic word apart
from a legitimate business column. So the stopword list stays exactly as
Task 1 left it (a formula word with no matching column is still filtered)
and real schema evidence overrides it per workspace.
"""
from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from core.onboarding.features.expression import extract_expression
from tests.test_blocked_kpi_invariant import _resolve, _write_workspace


class SchemaAwareAllowlistTests(unittest.TestCase):
    def test_ohlc_columns_survive_when_the_schema_has_them(self):
        extracted = extract_expression(
            "max(High) - min(Low) per ticker",
            known_columns={"High", "Low", "ticker"},
        )
        self.assertIn("High", extracted.identifiers)
        self.assertIn("Low", extracted.identifiers)
        self.assertIn("ticker", extracted.identifiers)

    def test_the_same_words_are_still_filtered_without_schema_evidence(self):
        # The market-data workspace's columns must not leak into a workspace
        # that has no such columns -- there, "High"/"Low" really are banding
        # vocabulary, which is what Task 1 fixed.
        extracted = extract_expression("max(High) - min(Low) per ticker")
        self.assertEqual(extracted.identifiers, ["ticker"])

    def test_ordinary_column_names_survive(self):
        for text, column in [
            ("sum(Weight) per department", "Weight"),
            ("avg(Score) by provider", "Score"),
            ("count(Flag) where Actual > Expected", "Flag"),
        ]:
            with self.subTest(text=text):
                self.assertNotIn(column, extract_expression(text).identifiers)
                self.assertIn(
                    column,
                    extract_expression(text, known_columns=[column]).identifiers,
                )

    def test_matching_is_case_insensitive(self):
        # The schema spells it `WEIGHT`, the KPI author wrote `Weight`.
        extracted = extract_expression(
            "sum(Weight) per department", known_columns=["WEIGHT", "Department"]
        )
        self.assertIn("Weight", extracted.identifiers)

    def test_a_known_column_beats_a_workspace_filter_term_too(self):
        # A filter VALUE that happens to share a name with a column: schema
        # evidence is the stronger signal, same as for stopwords.
        extracted = extract_expression(
            "sum(Amount) where Region = X",
            workspace_filter_terms=["Region"],
            known_columns=["Region", "Amount"],
        )
        self.assertIn("Region", extracted.identifiers)

    def test_none_or_empty_known_columns_changes_nothing(self):
        baseline = extract_expression("avg(Score) by provider").identifiers
        self.assertEqual(
            extract_expression("avg(Score) by provider", known_columns=None).identifiers,
            baseline,
        )
        self.assertEqual(
            extract_expression("avg(Score) by provider", known_columns=[]).identifiers,
            baseline,
        )


class ResolverThreadsTheSchemaThroughTests(unittest.TestCase):
    """The low-level fix is worthless if the resolver never passes its schema."""

    def test_resolver_wrapper_forwards_known_columns(self):
        from core.onboarding.kpi.feature_resolver import extract_expression as wrapper

        self.assertIn(
            "High", wrapper("max(High) per ticker", known_columns=["High"]).identifiers
        )
        self.assertNotIn("High", wrapper("max(High) per ticker").identifiers)

    def test_a_kpi_over_an_ohlc_column_resolves_end_to_end(self):
        # The real failure this closes: a market-data workspace whose columns
        # ARE named High/Low lost both features before the resolver ever
        # scored them -- no feature, no blocker, no trace.
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            _write_workspace(
                root,
                kpis=[
                    {
                        "name": "Daily trading range",
                        "description": "Intraday high-low spread per ticker",
                        "cuts": "per Ticker",
                        "metric": "max(High) - min(Low)",
                        "refinement_required": "",
                        "source": "docs/kpis.md",
                        "status": "needs_mapping",
                    }
                ],
                profiles={
                    "quotes": {
                        "Ticker": "String",
                        "High": "Float64",
                        "Low": "Float64",
                    }
                },
            )
            features = _resolve(root)["kpis"][0]["features"]
            named = {feature.get("feature") for feature in features}
            self.assertIn("High", named)
            self.assertIn("Low", named)

    def test_a_workspace_without_those_columns_still_filters_them(self):
        # Same KPI text, a schema that has no High/Low: Task 1's fix must
        # still hold -- banding vocabulary raises no phantom feature.
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            _write_workspace(
                root,
                kpis=[
                    {
                        "name": "Risk tier spread",
                        "description": "",
                        "cuts": "per Ticker",
                        "metric": "max(High) - min(Low)",
                        "refinement_required": "",
                        "source": "docs/kpis.md",
                        "status": "needs_mapping",
                    }
                ],
                profiles={"quotes": {"Ticker": "String", "Price": "Float64"}},
            )
            named = {
                feature.get("feature") for feature in _resolve(root)["kpis"][0]["features"]
            }
            self.assertNotIn("High", named)
            self.assertNotIn("Low", named)


if __name__ == "__main__":
    unittest.main()
