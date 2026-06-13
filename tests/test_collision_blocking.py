"""Colliding column names block instead of silently auto-resolving (F4).

When the same column name exists in several datasets with table-specific
meanings (the hostile workspace reuses Id/Name/Date/START/END/Amount/Status
with 6+ meanings), a direct name match must NOT auto-resolve. Resolution
consults, in order: dictionary context, relationship/lineage evidence,
accepted workspace definitions, and only then name matching -- which blocks
with per-candidate evidence when the collision survives.
"""
from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from core.onboarding.kpi.feature_resolver import (
    KPIFeatureResolver,
    _column_identity_groups,
    _relationship_join_worthy,
)


def _write_workspace(
    root: Path,
    *,
    kpis: list[dict],
    profiles: dict[str, dict[str, str]],
    dictionary_csv: str = "",
    relationships: list[dict] | None = None,
) -> Path:
    workspace = root / "workspaces" / "demo"
    contracts = workspace / "interns" / "generated" / "contracts"
    profiles_dir = workspace / "interns" / "generated" / "profiles"
    contracts.mkdir(parents=True)
    profiles_dir.mkdir(parents=True)
    (workspace / "datasets").mkdir(parents=True, exist_ok=True)
    profile_entries = []
    for name, schema in profiles.items():
        profile_entries.append(
            {
                "path": f"workspaces/demo/datasets/{name}.csv",
                "row_count": 3,
                "schema": schema,
                "profile_path": (
                    f"workspaces/demo/interns/generated/profiles/{name}.profile.json"
                ),
            }
        )
    (profiles_dir / "profile_index.json").write_text(
        json.dumps(
            {
                "artifact_type": "profile_index.json",
                "version": 1,
                "generated_by": "onboard-workspace",
                "workspace": "workspaces/demo",
                "profiles": profile_entries,
            }
        ),
        encoding="utf-8",
    )
    (contracts / "kpi_registry.json").write_text(
        json.dumps(
            {
                "artifact_type": "kpi_registry.json",
                "version": 1,
                "generated_by": "onboard-workspace",
                "workspace": "workspaces/demo",
                "kpis": kpis,
            }
        ),
        encoding="utf-8",
    )
    if dictionary_csv:
        docs = workspace / "docs"
        docs.mkdir(parents=True, exist_ok=True)
        (docs / "data_dictionary.csv").write_text(dictionary_csv, encoding="utf-8")
    if relationships is not None:
        (contracts / "relationship_contracts.json").write_text(
            json.dumps(
                {
                    "artifact_type": "relationship_contracts.json",
                    "version": 1,
                    "generated_by": "build-relationship-contracts",
                    "workspace": "workspaces/demo",
                    "relationships": relationships,
                    "summary": {
                        "relationship_count": len(relationships),
                        "executable_relationship_count": 0,
                        "candidate_relationship_count": len(relationships),
                    },
                }
            ),
            encoding="utf-8",
        )
    return workspace


def _kpi(metric: str) -> dict:
    return {
        "name": "Test KPI",
        "description": "",
        "cuts": "",
        "metric": metric,
        "refinement_required": "",
        "source": "docs/kpis.md",
        "status": "needs_mapping",
    }


def _resolve(root: Path) -> dict:
    KPIFeatureResolver(root, "workspaces/demo").run()
    return json.loads(
        (
            root
            / "workspaces"
            / "demo"
            / "interns"
            / "generated"
            / "contracts"
            / "kpi_feature_mapping.json"
        ).read_text(encoding="utf-8")
    )


_COLLIDING_PROFILES = {
    "shipments": {"Id": "String", "Amount": "Float64", "Status": "String"},
    "invoices": {"Id": "String", "Amount": "Float64", "Status": "String"},
    "movements": {"Id": "String", "Amount": "Float64", "Status": "String"},
}


class CollisionBlocksTests(unittest.TestCase):
    def test_colliding_amount_blocks_with_per_candidate_evidence(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            _write_workspace(
                root,
                kpis=[_kpi("sum(Amount)")],
                profiles=_COLLIDING_PROFILES,
                dictionary_csv=(
                    "table,column,description,data_type,source_system\n"
                    "shipments,Amount,Quoted charge for the shipment,decimal,TMS\n"
                    "invoices,Amount,Billed amount for the line,decimal,FINSYS\n"
                    "movements,Amount,Internal cost allocated to the leg,decimal,TMS\n"
                ),
            )
            mapping = _resolve(root)
            kpi = mapping["kpis"][0]
            self.assertEqual(kpi["status"], "blocked_questions_pending")
            feature = next(
                item for item in kpi["features"] if item["feature"] == "Amount"
            )
            self.assertEqual(feature["state"], "blocked_ambiguous")
            self.assertEqual(feature["resolution_type"], "ambiguous_direct_columns")
            self.assertEqual(len(feature["candidates"]), 3)
            # Per-candidate evidence: each candidate carries its own documented
            # meaning, so the user chooses on evidence, not on a bare name.
            reasons = {candidate["reason"] for candidate in feature["candidates"]}
            self.assertIn("Billed amount for the line", reasons)
            self.assertIn("Quoted charge for the shipment", reasons)
            self.assertTrue(kpi["open_questions"])

    def test_dictionary_context_disambiguates_clear_collision(self) -> None:
        """When the KPI's own text clearly matches one candidate's dictionary
        description (real terms, not the column name echoing itself), the
        dictionary tier resolves the collision."""
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            _write_workspace(
                root,
                kpis=[
                    {
                        "name": "Billed line revenue net of tax",
                        "description": "Sum the billed line amounts net of tax.",
                        "cuts": "",
                        "metric": "sum(Amount)",
                        "refinement_required": "",
                        "source": "docs/kpis.md",
                        "status": "needs_mapping",
                    }
                ],
                profiles=_COLLIDING_PROFILES,
                dictionary_csv=(
                    "table,column,description,data_type,source_system\n"
                    "shipments,Amount,Quoted charge for the shipment,decimal,TMS\n"
                    "invoices,Amount,Billed line revenue net of tax,decimal,FINSYS\n"
                    "movements,Amount,Internal cost allocated to the leg,decimal,TMS\n"
                ),
            )
            mapping = _resolve(root)
            feature = next(
                item
                for item in mapping["kpis"][0]["features"]
                if item["feature"] == "Amount"
            )
            self.assertEqual(feature["state"], "proven_alias")
            self.assertEqual(feature["resolution_type"], "contextual_dictionary_column")
            self.assertEqual(len(feature["source_columns"]), 1)
            self.assertIn("invoices", str(feature["source_columns"][0]["dataset"]))

    def test_collision_without_dictionary_still_blocks(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            _write_workspace(
                root,
                kpis=[_kpi("sum(Amount)")],
                profiles=_COLLIDING_PROFILES,
            )
            mapping = _resolve(root)
            feature = next(
                item
                for item in mapping["kpis"][0]["features"]
                if item["feature"] == "Amount"
            )
            self.assertEqual(feature["state"], "blocked_ambiguous")

    def test_single_dataset_direct_match_still_proves(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            _write_workspace(
                root,
                kpis=[_kpi("sum(NetWeight)")],
                profiles={
                    "shipments": {"Id": "String", "NetWeight": "Float64"},
                    "invoices": {"Id": "String", "Amount": "Float64"},
                },
            )
            mapping = _resolve(root)
            feature = next(
                item
                for item in mapping["kpis"][0]["features"]
                if item["feature"] == "NetWeight"
            )
            self.assertEqual(feature["state"], "proven_direct")

    def test_lineage_unified_collision_stays_proven(self) -> None:
        """fact.FacilityID -> dim.FacilityID joined by a proven relationship is
        one logical field, not a collision."""
        relationships = [
            {
                "relationship_id": "fact_facilityid_dim_facilityid",
                "left_dataset": "workspaces/demo/datasets/encounters.csv",
                "left_column": "FacilityID",
                "right_dataset": "workspaces/demo/datasets/facilities.csv",
                "right_column": "FacilityID",
                "state": "proven_data_model",
                "executable_usage_policy": {"allowed_in_sql_generation": True},
            }
        ]
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            _write_workspace(
                root,
                kpis=[_kpi("count(FacilityID)")],
                profiles={
                    "encounters": {"EncounterID": "String", "FacilityID": "String"},
                    "facilities": {"FacilityID": "String", "Name": "String"},
                },
                relationships=relationships,
            )
            mapping = _resolve(root)
            feature = next(
                item
                for item in mapping["kpis"][0]["features"]
                if item["feature"] == "FacilityID"
            )
            self.assertEqual(feature["state"], "proven_direct")
            evidence_types = {item.get("type") for item in feature["evidence"]}
            self.assertIn("relationship_unification", evidence_types)

    def test_unrelated_profile_edge_does_not_unify(self) -> None:
        """A raw profile name-overlap edge whose keys do NOT resolve must not
        unify a collision (hostile shipments.Id vs invoices.Id trap)."""
        relationships = [
            {
                "relationship_id": "shipments_id_invoices_id",
                "left_dataset": "workspaces/demo/datasets/shipments.csv",
                "left_column": "Amount",
                "right_dataset": "workspaces/demo/datasets/invoices.csv",
                "right_column": "Amount",
                "state": "profile_validated",
                "referential_integrity_checks": {"left_keys_resolve": False},
            }
        ]
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            _write_workspace(
                root,
                kpis=[_kpi("sum(Amount)")],
                profiles={
                    "shipments": {"Id": "String", "Amount": "Float64"},
                    "invoices": {"Id": "String", "Amount": "Float64"},
                },
                relationships=relationships,
            )
            mapping = _resolve(root)
            feature = next(
                item
                for item in mapping["kpis"][0]["features"]
                if item["feature"] == "Amount"
            )
            self.assertEqual(feature["state"], "blocked_ambiguous")


class ColumnIdentityGroupTests(unittest.TestCase):
    def test_join_worthy_states(self) -> None:
        self.assertTrue(_relationship_join_worthy({"state": "proven_data_model"}))
        self.assertTrue(_relationship_join_worthy({"state": "documented_data_model"}))
        self.assertTrue(_relationship_join_worthy({"state": "user_confirmed"}))
        self.assertFalse(_relationship_join_worthy({"state": "profile_validated"}))
        self.assertTrue(
            _relationship_join_worthy(
                {
                    "state": "profile_validated",
                    "referential_integrity_checks": {"left_keys_resolve": True},
                }
            )
        )
        self.assertFalse(_relationship_join_worthy({"state": "rejected"}))

    def test_transitive_unification(self) -> None:
        groups = _column_identity_groups(
            [
                {
                    "left_dataset": "a.csv",
                    "left_column": "Key",
                    "right_dataset": "b.csv",
                    "right_column": "Key",
                    "state": "proven_data_model",
                },
                {
                    "left_dataset": "c.csv",
                    "left_column": "Key",
                    "right_dataset": "b.csv",
                    "right_column": "Key",
                    "state": "documented_data_model",
                },
            ]
        )
        self.assertEqual(
            groups[("a", "key")],
            groups[("c", "key")],
        )


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
