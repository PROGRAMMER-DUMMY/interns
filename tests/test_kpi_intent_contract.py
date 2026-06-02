"""Tests for core.onboarding.kpi.intent_contract.

Covers:
  1. A share KPI with no recorded denominator scope yields
     denominator_scope confidence=low and appears in low_confidence_facets.
  2. A clean sum-by-cuts KPI yields grain=high, metric=high and NO low facets.
  3. An age KPI without an event date yields temporal_anchor=low.
  4. The artifact JSON is written with one entry per KPI.

All tests use .venv\\Scripts\\python.exe -m unittest tests.test_kpi_intent_contract
(never uv run).  No domain vocabulary is hardcoded.
"""
from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from core.onboarding.kpi.intent_contract import (
    build_intent_contract,
    low_confidence_facets,
    write_intent_contract,
)


# ---------------------------------------------------------------------------
# Helper: build a minimal KPI dict
# ---------------------------------------------------------------------------

def _kpi(
    kpi_id: str = "kpi_001",
    name: str = "",
    metric: str = "",
    cuts: str = "",
    features: list | None = None,
) -> dict:
    return {
        "kpi_id": kpi_id,
        "name": name,
        "metric": metric,
        "cuts": cuts,
        "features": features or [],
    }


def _feature(label: str, column: str, dataset: str = "data.csv") -> dict:
    return {
        "feature": label,
        "source_columns": [{"column": column, "dataset": dataset}],
    }


# ---------------------------------------------------------------------------
# Test 1: Share KPI with no denominator scope -> low confidence + blocker
# ---------------------------------------------------------------------------

class TestShareKpiNoScope(unittest.TestCase):
    """A percentage-share KPI whose pipeline_decisions carries no denominator
    scope must yield denominator_scope confidence=low and surface in
    low_confidence_facets."""

    def setUp(self):
        self.kpi = _kpi(
            kpi_id="kpi_002",
            name="Percentage share of lives for department",
            metric="percentage of count(distinct member_id) / count(distinct member_id) for department",
            cuts="department, gender",
            features=[
                _feature("member_id", "member_id"),
                _feature("department", "DepartmentName"),
                _feature("gender", "Gender"),
            ],
        )
        self.contract = build_intent_contract(self.kpi, decisions={})

    def test_denominator_scope_confidence_is_low(self):
        facets_by_name = {f["facet"]: f for f in self.contract["facets"]}
        denom = facets_by_name["denominator_scope"]
        self.assertEqual(
            denom["confidence"], "low",
            f"Expected denominator_scope confidence=low, got {denom['confidence']!r}. "
            f"Facet: {denom}",
        )

    def test_denominator_scope_default_is_grand_total(self):
        facets_by_name = {f["facet"]: f for f in self.contract["facets"]}
        denom = facets_by_name["denominator_scope"]
        # The default (when no scope is recorded) is grand_total
        self.assertEqual(denom["value"], "grand_total")

    def test_denominator_scope_source_is_default(self):
        facets_by_name = {f["facet"]: f for f in self.contract["facets"]}
        denom = facets_by_name["denominator_scope"]
        self.assertEqual(denom["source"], "default")

    def test_denominator_scope_appears_in_low_confidence_facets(self):
        questions = low_confidence_facets(self.contract)
        low_facet_names = [q["facet"] for q in questions]
        self.assertIn(
            "denominator_scope",
            low_facet_names,
            f"denominator_scope not in low_confidence_facets. Got: {low_facet_names}",
        )

    def test_denominator_scope_question_names_interpretations(self):
        questions = low_confidence_facets(self.contract)
        denom_q = next((q for q in questions if q["facet"] == "denominator_scope"), None)
        self.assertIsNotNone(denom_q, "Expected a denominator_scope blocker question")
        interp_labels = [i["label"] for i in denom_q["interpretations"]]
        # Must offer at least the default AND at least one within-group alternative
        self.assertGreaterEqual(len(interp_labels), 2, f"Expected >=2 interpretations, got {interp_labels}")
        # One must be grand_total (the default)
        self.assertTrue(
            any("grand_total" in lbl or "within" in lbl for lbl in interp_labels),
            f"Interpretations should name grand_total or within_<group>: {interp_labels}",
        )

    def test_low_confidence_count_is_nonzero(self):
        self.assertGreater(
            self.contract["low_confidence_count"],
            0,
            "low_confidence_count must be > 0 for a share KPI with no scope",
        )


# ---------------------------------------------------------------------------
# Test 2: Share KPI WITH a recorded denominator scope -> confidence=high
# ---------------------------------------------------------------------------

class TestShareKpiWithRecordedScope(unittest.TestCase):
    """When a denominator scope is recorded in pipeline_decisions, the facet
    must be confidence=high and source=human."""

    def setUp(self):
        self.kpi = _kpi(
            kpi_id="kpi_002",
            name="Percentage share of lives for department",
            metric="percentage of count(distinct member_id) / count(distinct member_id) for department",
            cuts="department",
            features=[_feature("member_id", "member_id"), _feature("department", "DeptName")],
        )
        decisions = {"kpi_002": {"denominator_scope": "within_department"}}
        self.contract = build_intent_contract(self.kpi, decisions=decisions)

    def test_denominator_scope_confidence_is_high_when_recorded(self):
        facets_by_name = {f["facet"]: f for f in self.contract["facets"]}
        denom = facets_by_name["denominator_scope"]
        self.assertEqual(denom["confidence"], "high")
        self.assertEqual(denom["source"], "human")
        self.assertEqual(denom["value"], "within_department")

    def test_not_in_low_confidence_facets_when_recorded(self):
        questions = low_confidence_facets(self.contract)
        low_facet_names = [q["facet"] for q in questions]
        self.assertNotIn(
            "denominator_scope",
            low_facet_names,
            "denominator_scope should NOT be a blocker when scope is recorded",
        )


# ---------------------------------------------------------------------------
# Test 3: Clean sum-by-cuts KPI -> grain=high, metric=high, no low facets
# ---------------------------------------------------------------------------

class TestCleanSumBycuts(unittest.TestCase):
    """A sum(col) metric with fully-resolved cuts must yield metric=high,
    grain=high, and no low-confidence facets."""

    def setUp(self):
        self.kpi = _kpi(
            kpi_id="kpi_010",
            name="Total amount by region and channel",
            metric="sum(total_amount)",
            cuts="region, channel",
            features=[
                _feature("total_amount", "total_amount"),
                _feature("region", "RegionCode"),
                _feature("channel", "SalesChannel"),
            ],
        )
        self.contract = build_intent_contract(self.kpi, decisions={})

    def test_metric_confidence_is_high(self):
        facets_by_name = {f["facet"]: f for f in self.contract["facets"]}
        metric = facets_by_name["metric"]
        self.assertEqual(
            metric["confidence"], "high",
            f"Expected metric confidence=high, got {metric['confidence']!r}. Facet: {metric}",
        )

    def test_grain_confidence_is_high(self):
        facets_by_name = {f["facet"]: f for f in self.contract["facets"]}
        grain = facets_by_name["grain"]
        self.assertEqual(
            grain["confidence"], "high",
            f"Expected grain confidence=high, got {grain['confidence']!r}. Facet: {grain}",
        )

    def test_no_low_confidence_facets(self):
        questions = low_confidence_facets(self.contract)
        self.assertEqual(
            questions, [],
            f"Expected no low-confidence facets for a clean sum KPI. Got: {questions}",
        )

    def test_metric_value_contains_sum(self):
        facets_by_name = {f["facet"]: f for f in self.contract["facets"]}
        metric = facets_by_name["metric"]
        self.assertIn("sum", str(metric["value"]).lower())

    def test_grain_value_contains_both_cuts(self):
        facets_by_name = {f["facet"]: f for f in self.contract["facets"]}
        grain = facets_by_name["grain"]
        grain_str = str(grain["value"])
        # Both cut tokens should appear
        self.assertTrue(
            "region" in grain_str.lower() or "RegionCode" in grain_str,
            f"Expected region in grain value, got: {grain_str}",
        )
        self.assertTrue(
            "channel" in grain_str.lower() or "SalesChannel" in grain_str,
            f"Expected channel in grain value, got: {grain_str}",
        )

    def test_denominator_scope_is_none_for_non_share(self):
        facets_by_name = {f["facet"]: f for f in self.contract["facets"]}
        denom = facets_by_name["denominator_scope"]
        self.assertEqual(
            denom["confidence"], "none",
            "denominator_scope should be none for a non-share KPI",
        )

    def test_temporal_anchor_is_none_for_no_date_arithmetic(self):
        facets_by_name = {f["facet"]: f for f in self.contract["facets"]}
        temporal = facets_by_name["temporal_anchor"]
        self.assertEqual(
            temporal["confidence"], "none",
            "temporal_anchor should be none when no date arithmetic is present",
        )


# ---------------------------------------------------------------------------
# Test 4: Age KPI without event date -> temporal_anchor=low
# ---------------------------------------------------------------------------

class TestAgeKpiNoEventDate(unittest.TestCase):
    """A KPI with age arithmetic in cuts but no explicit event-date grain column
    (e.g. Month(col)) must yield temporal_anchor confidence=low."""

    def setUp(self):
        self.kpi = _kpi(
            kpi_id="kpi_020",
            name="Count of records above 65 years",
            metric="count(*)",
            cuts="age(birthdate), status",
            features=[
                _feature("birthdate", "DateOfBirth"),
                _feature("status", "RecordStatus"),
            ],
        )
        self.contract = build_intent_contract(self.kpi, decisions={})

    def test_temporal_anchor_confidence_is_low(self):
        facets_by_name = {f["facet"]: f for f in self.contract["facets"]}
        temporal = facets_by_name["temporal_anchor"]
        self.assertEqual(
            temporal["confidence"], "low",
            f"Expected temporal_anchor confidence=low (no event-date grain), "
            f"got {temporal['confidence']!r}. Facet: {temporal}",
        )

    def test_temporal_anchor_source_is_default(self):
        facets_by_name = {f["facet"]: f for f in self.contract["facets"]}
        temporal = facets_by_name["temporal_anchor"]
        self.assertEqual(temporal["source"], "default")

    def test_temporal_anchor_default_value_is_current_date(self):
        # The dangerous default is current_date when no event date is known
        facets_by_name = {f["facet"]: f for f in self.contract["facets"]}
        temporal = facets_by_name["temporal_anchor"]
        self.assertEqual(temporal["value"], "current_date")

    def test_temporal_anchor_appears_in_low_confidence_facets(self):
        questions = low_confidence_facets(self.contract)
        low_facet_names = [q["facet"] for q in questions]
        self.assertIn(
            "temporal_anchor",
            low_facet_names,
            f"temporal_anchor not in low_confidence_facets. Got: {low_facet_names}",
        )

    def test_temporal_anchor_question_mentions_event_date(self):
        questions = low_confidence_facets(self.contract)
        ta_q = next((q for q in questions if q["facet"] == "temporal_anchor"), None)
        self.assertIsNotNone(ta_q)
        self.assertIn(
            "event_date",
            ta_q["question"].lower(),
            f"temporal_anchor question should mention event_date: {ta_q['question']}",
        )


# ---------------------------------------------------------------------------
# Test 5: Age KPI WITH event date -> temporal_anchor=medium (not low)
# ---------------------------------------------------------------------------

class TestAgeKpiWithEventDate(unittest.TestCase):
    """A KPI with age arithmetic AND an explicit time-grain source column
    must yield temporal_anchor confidence=medium (anchor is determinable)."""

    def setUp(self):
        self.kpi = _kpi(
            kpi_id="kpi_021",
            name="Count of records by age and service month",
            metric="count(*)",
            cuts="age(birthdate), Month(service_date), status",
            features=[
                _feature("birthdate", "DateOfBirth"),
                _feature("service_date", "ServiceDate"),
                _feature("status", "RecordStatus"),
            ],
        )
        self.contract = build_intent_contract(self.kpi, decisions={})

    def test_temporal_anchor_confidence_is_medium(self):
        facets_by_name = {f["facet"]: f for f in self.contract["facets"]}
        temporal = facets_by_name["temporal_anchor"]
        self.assertEqual(
            temporal["confidence"], "medium",
            f"Expected temporal_anchor confidence=medium (event-date grain present), "
            f"got {temporal['confidence']!r}. Facet: {temporal}",
        )

    def test_temporal_anchor_not_in_low_confidence_facets(self):
        questions = low_confidence_facets(self.contract)
        low_facet_names = [q["facet"] for q in questions]
        self.assertNotIn(
            "temporal_anchor",
            low_facet_names,
            "temporal_anchor should not be a blocker when event-date grain is present (medium conf)",
        )


# ---------------------------------------------------------------------------
# Test 6: Artifact JSON written with one entry per KPI
# ---------------------------------------------------------------------------

class TestWriteIntentContractArtifact(unittest.TestCase):
    """write_intent_contract writes kpi_intent_contract.json with one entry per KPI."""

    def test_artifact_written_with_one_entry_per_kpi(self):
        registry = [
            {
                "kpi_id": "kpi_001",
                "name": "Total revenue by region",
                "metric": "sum(revenue)",
                "cuts": "region",
                "features": [
                    _feature("revenue", "Revenue"),
                    _feature("region", "RegionCode"),
                ],
            },
            {
                "kpi_id": "kpi_002",
                "name": "Share of members for segment",
                "metric": "percentage of count(distinct member_id) / count(distinct member_id) for segment",
                "cuts": "segment, plan_type",
                "features": [
                    _feature("member_id", "MemberID"),
                    _feature("segment", "Segment"),
                    _feature("plan_type", "PlanType"),
                ],
            },
        ]
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            workspace_path = "workspaces/test_project"
            workspace_root = root / workspace_path
            contracts_dir = workspace_root / "interns" / "generated" / "contracts"
            contracts_dir.mkdir(parents=True)
            (contracts_dir / "kpi_registry.json").write_text(
                json.dumps(registry), encoding="utf-8"
            )

            result = write_intent_contract(root, workspace_path)

            json_path = Path(result["json_path"])
            self.assertTrue(json_path.exists(), f"JSON artifact not written: {json_path}")

            md_path = Path(result["md_path"])
            self.assertTrue(md_path.exists(), f"MD artifact not written: {md_path}")

            artifact = json.loads(json_path.read_text(encoding="utf-8"))
            self.assertIn("contracts", artifact)
            self.assertEqual(len(artifact["contracts"]), 2, "Expected one contract per KPI")
            self.assertEqual(artifact["kpi_count"], 2)

            ids = [c["kpi_id"] for c in artifact["contracts"]]
            self.assertIn("kpi_001", ids)
            self.assertIn("kpi_002", ids)

    def test_artifact_each_contract_has_seven_facets(self):
        registry = [
            {
                "kpi_id": "kpi_010",
                "name": "Count by category",
                "metric": "count(*)",
                "cuts": "category",
                "features": [_feature("category", "Category")],
            },
        ]
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            workspace_path = "workspaces/test_project"
            workspace_root = root / workspace_path
            contracts_dir = workspace_root / "interns" / "generated" / "contracts"
            contracts_dir.mkdir(parents=True)
            (contracts_dir / "kpi_registry.json").write_text(
                json.dumps(registry), encoding="utf-8"
            )

            result = write_intent_contract(root, workspace_path)
            artifact = json.loads(Path(result["json_path"]).read_text(encoding="utf-8"))
            contract = artifact["contracts"][0]
            facet_names = [f["facet"] for f in contract["facets"]]
            expected_facets = {
                "metric", "grain", "filters", "denominator_scope",
                "temporal_anchor", "output_shape", "null_zero_handling",
            }
            self.assertEqual(set(facet_names), expected_facets)

    def test_artifact_json_path_under_interns_generated_contracts(self):
        registry = [{"kpi_id": "kpi_001", "name": "Test", "metric": "count(*)", "cuts": "", "features": []}]
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            workspace_path = "workspaces/proj"
            contracts_dir = root / workspace_path / "interns" / "generated" / "contracts"
            contracts_dir.mkdir(parents=True)
            (contracts_dir / "kpi_registry.json").write_text(json.dumps(registry), encoding="utf-8")

            result = write_intent_contract(root, workspace_path)

            self.assertIn("interns", result["json_path"])
            self.assertIn("generated", result["json_path"])
            self.assertIn("contracts", result["json_path"])
            self.assertIn("kpi_intent_contract.json", result["json_path"])

    def test_artifact_md_path_under_interns_reports(self):
        registry = [{"kpi_id": "kpi_001", "name": "Test", "metric": "count(*)", "cuts": "", "features": []}]
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            workspace_path = "workspaces/proj"
            contracts_dir = root / workspace_path / "interns" / "generated" / "contracts"
            contracts_dir.mkdir(parents=True)
            (contracts_dir / "kpi_registry.json").write_text(json.dumps(registry), encoding="utf-8")

            result = write_intent_contract(root, workspace_path)

            self.assertIn("interns", result["md_path"])
            self.assertIn("reports", result["md_path"])
            self.assertIn("kpi_intent_contract.md", result["md_path"])


# ---------------------------------------------------------------------------
# Test 7: Facet schema invariants
# ---------------------------------------------------------------------------

class TestFacetSchemaInvariants(unittest.TestCase):
    """Every facet in every contract must carry the required keys and valid
    confidence/source values."""

    VALID_CONFIDENCE = {"high", "medium", "low", "none"}
    VALID_SOURCE = {"human", "default", "derived"}
    REQUIRED_KEYS = {"facet", "value", "confidence", "source", "evidence", "alternatives"}
    EXPECTED_FACETS = {
        "metric", "grain", "filters", "denominator_scope",
        "temporal_anchor", "output_shape", "null_zero_handling",
    }

    def _check_contract(self, kpi: dict, decisions: dict | None = None) -> None:
        contract = build_intent_contract(kpi, decisions or {})
        facet_names = {f["facet"] for f in contract["facets"]}
        self.assertEqual(facet_names, self.EXPECTED_FACETS, f"Unexpected facet set: {facet_names}")
        for facet in contract["facets"]:
            missing = self.REQUIRED_KEYS - set(facet.keys())
            self.assertEqual(missing, set(), f"Facet {facet['facet']} missing keys: {missing}")
            self.assertIn(
                facet["confidence"], self.VALID_CONFIDENCE,
                f"Facet {facet['facet']} has invalid confidence: {facet['confidence']!r}",
            )
            self.assertIn(
                facet["source"], self.VALID_SOURCE,
                f"Facet {facet['facet']} has invalid source: {facet['source']!r}",
            )
            self.assertIsInstance(facet["evidence"], list)
            self.assertIsInstance(facet["alternatives"], list)

    def test_schema_for_empty_kpi(self):
        self._check_contract(_kpi())

    def test_schema_for_sum_kpi(self):
        self._check_contract(_kpi(
            metric="sum(amount)",
            cuts="category, month",
            features=[_feature("amount", "Amount"), _feature("category", "Cat")],
        ))

    def test_schema_for_share_kpi(self):
        self._check_contract(_kpi(
            metric="percentage of count(distinct id) / count(distinct id) for group",
            cuts="group",
            features=[_feature("id", "ID"), _feature("group", "GroupName")],
        ))

    def test_schema_for_age_kpi_no_event_date(self):
        self._check_contract(_kpi(
            metric="count(*)",
            cuts="age(dob), status",
            features=[_feature("dob", "DateOfBirth"), _feature("status", "Status")],
        ))

    def test_schema_for_top_n_kpi(self):
        self._check_contract(_kpi(
            name="Top 10 products by revenue",
            metric="sum(revenue)",
            cuts="product",
            features=[_feature("revenue", "Revenue"), _feature("product", "ProductName")],
        ))


# ---------------------------------------------------------------------------
# Test 8: null_zero_handling always present and source=default
# ---------------------------------------------------------------------------

class TestNullZeroHandlingFacet(unittest.TestCase):
    """null_zero_handling is always recorded as source=default, confidence=medium."""

    def _get_facet(self, kpi: dict) -> dict:
        contract = build_intent_contract(kpi, {})
        return next(f for f in contract["facets"] if f["facet"] == "null_zero_handling")

    def test_null_zero_source_is_default(self):
        facet = self._get_facet(_kpi(metric="sum(x)", cuts="y"))
        self.assertEqual(facet["source"], "default")

    def test_null_zero_confidence_is_medium(self):
        facet = self._get_facet(_kpi(metric="sum(x)", cuts="y"))
        self.assertEqual(facet["confidence"], "medium")

    def test_null_zero_not_in_low_confidence_facets(self):
        # medium confidence is not a blocker
        kpi = _kpi(metric="sum(x)", cuts="y", features=[_feature("x", "X"), _feature("y", "Y")])
        contract = build_intent_contract(kpi, {})
        questions = low_confidence_facets(contract)
        names = [q["facet"] for q in questions]
        self.assertNotIn("null_zero_handling", names)

    def test_null_zero_evidence_mentions_share_for_share_kpi(self):
        kpi = _kpi(
            metric="percentage of sum(amount) / sum(amount) for group",
            cuts="group",
            features=[_feature("amount", "Amount"), _feature("group", "Group")],
        )
        facet = self._get_facet(kpi)
        evidence_text = " ".join(facet["evidence"]).lower()
        self.assertIn("share", evidence_text)


# ---------------------------------------------------------------------------
# Test 9: output_shape detection
# ---------------------------------------------------------------------------

class TestOutputShapeFacet(unittest.TestCase):
    def _shape(self, **kwargs) -> dict:
        kpi = _kpi(**kwargs)
        contract = build_intent_contract(kpi, {})
        return next(f for f in contract["facets"] if f["facet"] == "output_shape")

    def test_top_n_yields_ranking_high(self):
        facet = self._shape(
            name="Top 5 categories by sales",
            metric="sum(sales)",
            cuts="category",
        )
        self.assertEqual(facet["confidence"], "high")
        self.assertIn("ranking", str(facet["value"]).lower())
        self.assertIn("5", str(facet["value"]))

    def test_share_metric_yields_share_high(self):
        facet = self._shape(
            metric="percentage of count(distinct x) / count(distinct x) for grp",
            cuts="grp",
        )
        self.assertEqual(facet["confidence"], "high")
        self.assertEqual(facet["value"], "share")

    def test_time_bucket_cuts_yields_trend_high(self):
        facet = self._shape(
            metric="sum(amount)",
            cuts="month, region",
            features=[_feature("amount", "Amount"), _feature("region", "Region")],
        )
        self.assertEqual(facet["confidence"], "high")
        self.assertEqual(facet["value"], "trend")

    def test_no_metric_no_cuts_yields_low(self):
        facet = self._shape(metric="", cuts="")
        self.assertEqual(facet["confidence"], "low")

    def test_metric_and_cuts_no_signal_yields_medium(self):
        facet = self._shape(
            metric="sum(amount)",
            cuts="region",
            features=[_feature("amount", "Amount"), _feature("region", "Region")],
        )
        self.assertEqual(facet["confidence"], "medium")
        self.assertEqual(facet["value"], "flat")


# ---------------------------------------------------------------------------
# Test 10: count(*) metric -> high confidence
# ---------------------------------------------------------------------------

class TestCountStarMetric(unittest.TestCase):
    def test_count_star_yields_metric_high(self):
        kpi = _kpi(metric="count(*)", cuts="status", features=[_feature("status", "Status")])
        contract = build_intent_contract(kpi, {})
        facets = {f["facet"]: f for f in contract["facets"]}
        self.assertEqual(facets["metric"]["confidence"], "high")
        self.assertEqual(facets["metric"]["value"], "count(*)")


# ---------------------------------------------------------------------------
# Test 11: workspace-agnostic — no Healthcare/Medicare words
# ---------------------------------------------------------------------------

class TestWorkspaceAgnostic(unittest.TestCase):
    """The module must not contain hardcoded domain vocabulary."""

    def test_no_domain_vocabulary_in_module_source(self):
        import core.onboarding.kpi.intent_contract as ic
        import inspect
        source = inspect.getsource(ic)
        forbidden = ["healthcare", "medicare", "hospital a", "rcm", "payor", "encounter"]
        for word in forbidden:
            self.assertNotIn(
                word, source.lower(),
                f"Domain word {word!r} found in intent_contract.py source — module must be workspace-agnostic",
            )


if __name__ == "__main__":
    unittest.main()
