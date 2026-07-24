"""Regressions for Q3 (lingering-issues plan): relationship/governance gate
closure. See ~/.claude/plans/dynamic-cooking-firefly.md Q3.

- A free-text "X joins Y on COL" doc sentence is documented-but-unproven, not
  auto-promoted to executable without real key-overlap/uniqueness evidence.
- A second, previously-unaudited instance of the same class of bug found while
  verifying the first fix: a data-model doc merely NAMING both entities +
  generic relationship language (no explicit join description at all) was
  also auto-promoting a profile candidate straight to proven_data_model
  whenever uniqueness/RI were undeterminable.
- A plan selecting two "fact" sources that only connect through a shared
  dimension (no direct edge) is flagged as a fan-trap risk.
"""
from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from core.onboarding.relationships.contracts import (
    DOCUMENTED_RELATIONSHIP_STATE,
    RelationshipContractBuilder,
    _parse_relationships_from_docs,
    _promote_profile_relationships_with_doc_context,
    _relationship,
    _relationships_from_diagram_sidecars,
)
from core.onboarding.relationships.source_to_target_planner import _fan_trap_risks, _join_plan
from core.storage.workspace_layout import WorkspaceLayout


class FreeTextJoinNotAutoPromotedTests(unittest.TestCase):
    def test_free_text_join_with_no_data_backing_is_documented_not_proven(self):
        doc = {"path": "model.md", "text": "orders joins customers on customer_id.\n"}
        profiles = {
            "workspaces/demo/orders.csv": {"schema": {"customer_id": "Int64"}},
            "workspaces/demo/customers.csv": {"schema": {"customer_id": "Int64"}},
        }
        relationships = _parse_relationships_from_docs([doc], profiles)
        self.assertTrue(relationships, "expected the free-text join to parse at all")
        for rel in relationships:
            self.assertEqual(
                rel["state"], DOCUMENTED_RELATIONSHIP_STATE,
                "a free-text doc sentence alone must never emit proven_data_model directly",
            )
            self.assertFalse(rel["executable_usage_policy"]["allowed_in_sql_generation"])

    def test_end_to_end_builder_leaves_unbacked_free_text_join_non_executable(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            workspace = root / "workspaces" / "demo"
            layout = WorkspaceLayout(project_root=workspace)
            layout.ensure_runtime_dirs()
            (workspace / "relationship_model.md").write_text(
                "orders joins customers on customer_id.\n", encoding="utf-8",
            )
            (layout.profiles_dir / "profile_index.json").write_text(
                json.dumps({
                    "profiles": [
                        {"path": "workspaces/demo/orders.csv", "schema": {"customer_id": "Int64"},
                         "profile_path": "x/orders.profile.json"},
                        {"path": "workspaces/demo/customers.csv", "schema": {"customer_id": "Int64"},
                         "profile_path": "x/customers.profile.json"},
                    ]
                }),
                encoding="utf-8",
            )
            (layout.contracts_dir / "domain_model.json").write_text(
                json.dumps({"data_models": ["workspaces/demo/relationship_model.md"]}),
                encoding="utf-8",
            )
            # No orders.csv/customers.csv content on disk -- nothing to prove
            # uniqueness/RI against, so promotion must not happen.
            result = RelationshipContractBuilder(root, "workspaces/demo").build()
            self.assertEqual(result.executable_relationship_count, 0)


class DocContextPromotionRequiresRealEvidenceTests(unittest.TestCase):
    """A doc merely naming both tables + generic relationship-language words
    is not data evidence -- promotion to proven_data_model still requires
    confirmed uniqueness + RI, same bar as every other promotion path."""

    def _profile_validated_candidate(self, dimension_key_unique, ri_ratio) -> dict:
        return _relationship(
            left_dataset="workspaces/demo/orders.csv", left_column="customer_id",
            right_dataset="workspaces/demo/customers.csv", right_column="customer_id",
            state="profile_validated", confidence=0.62, evidence_sources=[],
            dimension_key_unique=dimension_key_unique, referential_integrity_ratio=ri_ratio,
        )

    def _doc(self) -> dict:
        return {
            "path": "model.md",
            "text": (
                "The data model has relationships between orders and customers via "
                "customer_id (foreign key)."
            ),
        }

    def test_undetermined_evidence_is_not_promoted(self):
        candidate = self._profile_validated_candidate(None, None)
        [result] = _promote_profile_relationships_with_doc_context([candidate], [self._doc()])
        self.assertNotEqual(result["state"], "proven_data_model")
        self.assertFalse(result["executable_usage_policy"]["allowed_in_sql_generation"])

    def test_confirmed_evidence_is_promoted(self):
        candidate = self._profile_validated_candidate(True, 1.0)
        [result] = _promote_profile_relationships_with_doc_context([candidate], [self._doc()])
        self.assertEqual(result["state"], "proven_data_model")
        self.assertTrue(result["executable_usage_policy"]["allowed_in_sql_generation"])

    def test_known_non_unique_stays_blocked_even_with_doc_mention(self):
        candidate = self._profile_validated_candidate(False, 1.0)
        [result] = _promote_profile_relationships_with_doc_context([candidate], [self._doc()])
        self.assertNotEqual(result["state"], "proven_data_model")
        self.assertFalse(result["executable_usage_policy"]["allowed_in_sql_generation"])


class DiagramSidecarRequiresRealEvidenceTests(unittest.TestCase):
    """Third instance of the same class of bug, found while verifying the
    first two: a diagram-declared edge whose uniqueness/RI could not be
    determined at all (no readable CSV backing it) still became
    proven_data_model on diagram say-so alone -- contradicting the module's
    own stated invariant ("diagram intent never overrides observed data
    quality")."""

    def _write_sidecar(self, layout: WorkspaceLayout) -> None:
        sidecar_dir = layout.generated_dir / "data_model_images"
        sidecar_dir.mkdir(parents=True, exist_ok=True)
        sidecar = {
            "source_image": "workspaces/demo/docs/DataModel.png",
            "relationships": [
                {
                    "relationship_id": "fact__customer_id__dim_customer__customer_id",
                    "from_table": "Fact", "from_column": "customer_id",
                    "to_table": "Dim_Customer", "to_column": "customer_id",
                    "confidence": 0.85,
                },
            ],
            "profile_matching": {
                "relationship_matches": [
                    {
                        "relationship_id": "fact__customer_id__dim_customer__customer_id",
                        "state": "profile_matched",
                        "confidence": 0.85,
                        "from_match": {
                            "table_name": "Fact", "column_name": "customer_id",
                            "dataset": "workspaces/demo/orders.csv", "profile_column": "customer_id",
                        },
                        "to_match": {
                            "table_name": "Dim_Customer", "column_name": "customer_id",
                            "dataset": "workspaces/demo/customers.csv", "profile_column": "customer_id",
                        },
                    },
                ],
            },
        }
        (sidecar_dir / "diagram.model.json").write_text(json.dumps(sidecar), encoding="utf-8")

    def test_undeterminable_diagram_edge_is_documented_not_proven(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            workspace = root / "workspaces" / "demo"
            layout = WorkspaceLayout(project_root=workspace)
            layout.ensure_runtime_dirs()
            self._write_sidecar(layout)
            profiles = {
                "workspaces/demo/orders.csv": {"schema": {"customer_id": "Int64"}},
                "workspaces/demo/customers.csv": {"schema": {"customer_id": "Int64"}},
            }
            # No orders.csv/customers.csv content on disk -- uniqueness/RI are
            # both undeterminable.
            [rel] = _relationships_from_diagram_sidecars(layout, profiles, root)
            self.assertEqual(rel["state"], DOCUMENTED_RELATIONSHIP_STATE)
            self.assertFalse(rel["executable_usage_policy"]["allowed_in_sql_generation"])


class FanTrapDetectionTests(unittest.TestCase):
    def _edge(self, left: str, right: str) -> dict[str, str]:
        rel = _relationship(
            left_dataset=left, left_column="id", right_dataset=right, right_column="id",
            state="proven_data_model", confidence=0.9, evidence_sources=[],
            dimension_key_unique=True, referential_integrity_ratio=1.0,
        )
        return {
            "left_dataset": left, "left_column": "id",
            "right_dataset": right, "right_column": "id",
            "relationship_id": rel["relationship_id"], "state": rel["state"],
        }

    def test_two_facts_sharing_a_dimension_with_no_direct_edge_is_flagged(self):
        executable = [self._edge("fact_a", "dim"), self._edge("fact_b", "dim")]
        risks = _fan_trap_risks(executable)
        self.assertEqual(len(risks), 1)
        self.assertEqual(risks[0]["shared_dimension"], "dim")
        self.assertEqual(sorted(risks[0]["fact_sources"]), ["fact_a", "fact_b"])

    def test_single_fact_to_dimension_is_not_a_fan_trap(self):
        executable = [self._edge("fact_a", "dim")]
        self.assertEqual(_fan_trap_risks(executable), [])

    def test_two_facts_with_a_direct_edge_between_them_are_not_flagged(self):
        # fact_a and fact_b are themselves directly related -- not the
        # independent-fan-out shape a fan trap describes.
        executable = [
            self._edge("fact_a", "dim"),
            self._edge("fact_b", "dim"),
            self._edge("fact_a", "fact_b"),
        ]
        self.assertEqual(_fan_trap_risks(executable), [])

    def test_join_plan_surfaces_fan_trap_risk_field(self):
        relationships = [
            _relationship(
                left_dataset="fact_a", left_column="id", right_dataset="dim", right_column="id",
                state="proven_data_model", confidence=0.9, evidence_sources=[],
                dimension_key_unique=True, referential_integrity_ratio=1.0,
            ),
            _relationship(
                left_dataset="fact_b", left_column="id", right_dataset="dim", right_column="id",
                state="proven_data_model", confidence=0.9, evidence_sources=[],
                dimension_key_unique=True, referential_integrity_ratio=1.0,
            ),
        ]
        profiles = {s: {"schema": {}} for s in ("fact_a", "fact_b", "dim")}
        plan = _join_plan(["fact_a", "fact_b", "dim"], profiles, relationships)
        self.assertEqual(len(plan["fan_trap_risk"]), 1)


if __name__ == "__main__":
    unittest.main()
