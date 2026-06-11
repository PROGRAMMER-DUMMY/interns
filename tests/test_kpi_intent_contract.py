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
    _load_pipeline_decisions,
    build_intent_contract,
    intent_facet_panel_questions,
    low_confidence_facets,
    record_intent_answer,
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

    def test_artifact_each_contract_has_all_facets(self):
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
                "grain_bucketing", "temporal_anchor", "output_shape",
                "null_zero_handling",
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
        "grain_bucketing", "temporal_anchor", "output_shape",
        "null_zero_handling",
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


# ---------------------------------------------------------------------------
# Test 12: grain_bucketing facet
# ---------------------------------------------------------------------------

class TestGrainBucketingFacet(unittest.TestCase):
    """A share/percentage metric cut by a raw continuous dimension (exact age /
    days-since) must yield grain_bucketing confidence=low and surface as a
    blocker, unless a bucketing decision is recorded. Non-share or
    categorical-only KPIs leave the facet not-applicable (none). Generic
    fixtures only (customers/orders/region)."""

    def _facet(self, kpi: dict, decisions: dict | None = None) -> dict:
        contract = build_intent_contract(kpi, decisions or {})
        return next(f for f in contract["facets"] if f["facet"] == "grain_bucketing")

    def test_share_with_raw_age_cut_no_decision_is_low(self):
        kpi = _kpi(
            name="share of customers by age",
            metric="share of count(*) for region",
            cuts="region, age(date_of_birth)",
            features=[_feature("region", "Region"),
                      _feature("date_of_birth", "DateOfBirth")],
        )
        facet = self._facet(kpi)
        self.assertEqual(facet["confidence"], "low")
        self.assertEqual(facet["value"], "band_continuous_cuts")
        self.assertIn("exact_value_grain", facet["alternatives"])

    def test_low_grain_bucketing_surfaces_in_low_confidence_facets(self):
        kpi = _kpi(
            metric="percentage of count(*) for grp",
            cuts="grp, days since signup_date",
            features=[_feature("grp", "Group"),
                      _feature("signup_date", "SignupDate")],
        )
        contract = build_intent_contract(kpi, {})
        names = [q["facet"] for q in low_confidence_facets(contract)]
        self.assertIn("grain_bucketing", names)

    def test_recorded_decision_makes_grain_bucketing_high(self):
        kpi = _kpi(
            kpi_id="kpi_007",
            metric="share of count(*) for region",
            cuts="region, age(date_of_birth)",
            features=[_feature("region", "Region"),
                      _feature("date_of_birth", "DateOfBirth")],
        )
        decisions = {"grain_bucketing_decisions": {"kpi_007": "band_continuous_cuts"}}
        facet = self._facet(kpi, decisions)
        self.assertEqual(facet["confidence"], "high")
        self.assertEqual(facet["value"], "band_continuous_cuts")
        names = [q["facet"] for q in low_confidence_facets(
            build_intent_contract(kpi, decisions))]
        self.assertNotIn("grain_bucketing", names)

    def test_non_share_age_kpi_is_not_applicable(self):
        kpi = _kpi(
            metric="count(distinct customer_id)",
            cuts="region, age(date_of_birth)",
            features=[_feature("customer_id", "CustomerID"),
                      _feature("region", "Region"),
                      _feature("date_of_birth", "DateOfBirth")],
        )
        facet = self._facet(kpi)
        self.assertEqual(facet["confidence"], "none")
        self.assertIsNone(facet["value"])

    def test_share_categorical_only_is_not_applicable(self):
        kpi = _kpi(
            metric="share of count(*) for region",
            cuts="region, channel",
            features=[_feature("region", "Region"), _feature("channel", "Channel")],
        )
        facet = self._facet(kpi)
        self.assertEqual(facet["confidence"], "none")

    def test_age_threshold_filter_is_not_applicable(self):
        # `age > 50` is a filter, not a grouping dimension.
        kpi = _kpi(
            metric="share of count(*) for region",
            cuts="region, age > 50",
            features=[_feature("region", "Region")],
        )
        facet = self._facet(kpi)
        self.assertEqual(facet["confidence"], "none")


class TestGrainBucketingPanelE2E(unittest.TestCase):
    """End-to-end (in-process) seam for the grain_bucketing facet: a share KPI cut
    by raw age must (1) surface as a blocker-panel question via
    intent_facet_panel_questions, (2) persist to pipeline_decisions.json when
    answered via record_intent_answer, and (3) converge (stop re-asking) on the
    next panel build. Mirrors how prepare-kpi-blocker-panel / apply-kpi-panel-answer
    drive these functions, without a subprocess CLI run. Generic fixtures only."""

    def _registry(self):
        return [
            _kpi(
                kpi_id="kpi_001",
                name="share of customers by age",
                metric="share of count(*) for region",
                cuts="region, age(date_of_birth)",
                features=[
                    _feature("region", "Region"),
                    _feature("date_of_birth", "DateOfBirth"),
                ],
            ),
        ]

    def test_panel_emits_then_persists_then_converges(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            workspace_path = "workspaces/proj"
            contracts_dir = root / workspace_path / "interns" / "generated" / "contracts"
            contracts_dir.mkdir(parents=True)
            (contracts_dir / "kpi_registry.json").write_text(
                json.dumps(self._registry()), encoding="utf-8"
            )

            # (1) the facet surfaces as a routed blocker-panel question
            questions = intent_facet_panel_questions(root, workspace_path)
            gb = [q for q in questions if q.get("facet") == "grain_bucketing"]
            self.assertTrue(
                gb, f"grain_bucketing not surfaced by panel. Facets: "
                f"{[q.get('facet') for q in questions]}",
            )

            # (2) answering it via the apply path persists to pipeline_decisions
            record_intent_answer(
                root,
                workspace_path,
                kpi_id="kpi_001",
                facet="grain_bucketing",
                value="band_continuous_cuts",
                confirmed_by="tester",
            )
            decisions = _load_pipeline_decisions((root / workspace_path).resolve())
            recorded = decisions.get("grain_bucketing_decisions") or {}
            self.assertIn(
                "kpi_001", recorded,
                f"grain_bucketing answer not mirrored to pipeline_decisions: {decisions}",
            )

            # (3) the panel converges -- the answered facet is no longer re-asked
            again = intent_facet_panel_questions(root, workspace_path)
            self.assertFalse(
                [q for q in again if q.get("facet") == "grain_bucketing"
                 and q.get("kpi_id") == "kpi_001"],
                "grain_bucketing should not be re-asked after it was answered",
            )


class TestRegistryWithoutKpiIdBackfill(unittest.TestCase):
    """The real onboarding kpi_registry.json rows carry NO explicit kpi_id (keys are
    name/metric/cuts/...). intent_facet_panel_questions must backfill the positional
    ``kpi_{idx:03d}`` so questions key to a real KPI -- otherwise question_id becomes
    ``intent__<facet>``, applies_to_kpis is empty, and record_intent_answer mirrors the
    decision to pipeline_decisions[""] which the generator never reads (follow_ups #1).

    Regression guard: prior fixtures always set kpi_id explicitly, masking this bug."""

    def _registry_no_ids(self):
        # Mirrors the on-disk shape: positional order defines the id; no kpi_id key.
        return [
            {
                "name": "trend of amount paid",
                "metric": "sum(amount_paid)",
                "cuts": "month(service_date), region",
            },
            {
                "name": "share of customers by age",
                "metric": "share of count(distinct customer_id) / count(distinct customer_id) for region",
                "cuts": "region, age(date_of_birth)",
            },
        ]

    def test_questions_backfill_positional_kpi_id(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            workspace_path = "workspaces/proj"
            contracts_dir = root / workspace_path / "interns" / "generated" / "contracts"
            contracts_dir.mkdir(parents=True)
            (contracts_dir / "kpi_registry.json").write_text(
                json.dumps({"kpis": self._registry_no_ids()}), encoding="utf-8"
            )

            questions = intent_facet_panel_questions(root, workspace_path)
            self.assertTrue(questions, "no intent questions surfaced")

            # No question may carry an empty kpi_id, and ids/feature keys must be well-formed.
            for q in questions:
                self.assertTrue(
                    str(q.get("kpi_id") or "").strip(),
                    f"question {q.get('facet')} has empty kpi_id: {q.get('question_id')}",
                )
                self.assertNotIn("intent__", str(q.get("question_id")))
                self.assertNotIn("::::", str(q.get("feature")))

            # The share-by-raw-age KPI is the 2nd row -> positional id kpi_002.
            gb = [q for q in questions if q.get("facet") == "grain_bucketing"]
            self.assertTrue(gb, "grain_bucketing not surfaced for the share-by-age KPI")
            self.assertEqual(gb[0].get("kpi_id"), "kpi_002")
            self.assertEqual(gb[0].get("question_id"), "intent_kpi_002_grain_bucketing")
            self.assertEqual(gb[0].get("applies_to_kpis"), ["kpi_002"])

    def test_answer_mirrors_to_real_kpi_id_not_empty(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            workspace_path = "workspaces/proj"
            contracts_dir = root / workspace_path / "interns" / "generated" / "contracts"
            contracts_dir.mkdir(parents=True)
            (contracts_dir / "kpi_registry.json").write_text(
                json.dumps({"kpis": self._registry_no_ids()}), encoding="utf-8"
            )

            questions = intent_facet_panel_questions(root, workspace_path)
            gb = [q for q in questions if q.get("facet") == "grain_bucketing"][0]
            record_intent_answer(
                root,
                workspace_path,
                kpi_id=str(gb.get("kpi_id")),
                facet="grain_bucketing",
                value="band_continuous_cuts",
                confirmed_by="tester",
            )
            decisions = _load_pipeline_decisions((root / workspace_path).resolve())
            recorded = decisions.get("grain_bucketing_decisions") or {}
            self.assertIn("kpi_002", recorded, f"answer mis-keyed: {recorded}")
            self.assertNotIn("", recorded, "decision mirrored to empty kpi_id")


class TestHardBlockingFacetBecomesCurrent(unittest.TestCase):
    """grain_bucketing also HARD-blocks the execution harness, so when there are no
    feature-mapping blockers it must become the answerable `current` panel -- otherwise
    apply-kpi-panel-answer returns an empty panel and the operator loops (the quota
    burn). Advisory intent facets (denominator_scope, temporal_anchor) stay set-only."""

    def test_grain_facet_is_current_when_no_feature_blockers(self):
        from core.onboarding.kpi.blocker_question_panel import BlockerQuestionPanelBuilder

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            ws = "workspaces/proj"
            contracts = root / ws / "interns" / "generated" / "contracts"
            contracts.mkdir(parents=True)
            # Registry (no kpi_id, mirroring real onboarding): a plain KPI then a
            # share-by-raw-age KPI whose grain_bucketing is the hard blocker.
            (contracts / "kpi_registry.json").write_text(
                json.dumps({"kpis": [
                    {"name": "trend", "metric": "sum(amount_paid)", "cuts": "month(service_date)"},
                    {"name": "share of customers by age",
                     "metric": "share of count(distinct customer_id) / count(distinct customer_id) for region",
                     "cuts": "region, age(date_of_birth)"},
                ]}),
                encoding="utf-8",
            )
            # Feature mapping with no unresolved clusters -> zero feature questions.
            mapping_path = contracts / "kpi_feature_mapping.json"
            mapping_path.write_text(json.dumps({"kpis": []}), encoding="utf-8")

            BlockerQuestionPanelBuilder(root, ws, mapping_path=mapping_path).run()
            current = json.loads(
                (root / ws / "interns" / "reports" / "blocker_question_panel"
                 / "current.json").read_text(encoding="utf-8")
            )
            self.assertEqual(current.get("facet"), "grain_bucketing")
            self.assertEqual(current.get("kpi_id"), "kpi_002")
            self.assertTrue(current.get("options"), "current grain panel must carry options")


if __name__ == "__main__":
    unittest.main()
