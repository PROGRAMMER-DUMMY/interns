"""Dictionary-vs-profile reconciliation (hostile-workspace refinement 1b).

A workspace data dictionary is documentation evidence, not ground truth. The
reconciliation pass cross-checks every dictionary claim against profile
evidence and emits structured ``dictionary_conflicts``; the feature resolver
demotes proven mappings that stand on contradicted columns to answerable
blockers, and the blocker panel asks the user to rule which evidence wins.

Covered lie classes (all evidence-shape, no domain vocabulary):
- declared enum vs observed values        (enum_mismatch)
- claimed single unit vs mixed sibling    (unit_mismatch)
- documented column that does not exist   (phantom_column / misplaced_column)
- claim worded around another dataset     (misattributed_claim)
plus a truthful-dictionary no-conflict case and the resolver/panel/validator
wiring.
"""
from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from core.onboarding.documents.dictionary_reconciliation import (
    apply_conflicts_to_mapping,
    claimed_qualifiers,
    declared_enum_values,
    load_data_dictionary_rows,
    reconcile_dictionary_claims,
)
from core.onboarding.kpi.feature_resolver import KPIFeatureResolver


def _profile(
    name: str,
    columns: dict[str, list[str]],
    *,
    dtypes: dict[str, str] | None = None,
) -> dict:
    dtypes = dtypes or {}
    return {
        "path": f"workspaces/demo/datasets/{name}.csv",
        "row_count": 50,
        "schema": {column: dtypes.get(column, "String") for column in columns},
        "profile_path": (
            f"workspaces/demo/interns/generated/profiles/{name}.profile.json"
        ),
        "columns": [
            {"name": column, "dtype": dtypes.get(column, "String"), "sample_values": values}
            for column, values in columns.items()
        ],
    }


def _row(table: str, field: str, description: str) -> dict:
    return {
        "table": table,
        "field": field,
        "description": description,
        "path": "workspaces/demo/docs/data_dictionary.csv",
    }


class EnumMismatchTests(unittest.TestCase):
    def test_declared_enum_with_zero_overlap_is_error(self) -> None:
        """'ACTIVE or CLOSED' documented; A/C/S observed -> error."""
        conflicts = reconcile_dictionary_claims(
            [_row("party", "Status", "Account status: ACTIVE or CLOSED")],
            [_profile("party", {"party_key": ["P-1", "P-2"], "Status": ["A", "C", "S"]})],
        )
        self.assertEqual(len(conflicts), 1)
        conflict = conflicts[0]
        self.assertEqual(conflict["type"], "enum_mismatch")
        self.assertEqual(conflict["severity"], "error")
        self.assertEqual(conflict["column"], "Status")
        self.assertEqual(conflict["declared_values"], ["ACTIVE", "CLOSED"])
        self.assertEqual(conflict["observed_values"], ["A", "C", "S"])
        self.assertTrue(conflict["conflict_id"].startswith("dict_conflict_"))

    def test_observed_values_outside_declared_set_is_warning(self) -> None:
        conflicts = reconcile_dictionary_claims(
            [_row("orders", "state", "Order state: OPEN / CLOSED")],
            [_profile("orders", {"state": ["OPEN", "CLOSED", "VOID"]})],
        )
        self.assertEqual(len(conflicts), 1)
        self.assertEqual(conflicts[0]["type"], "enum_mismatch")
        self.assertEqual(conflicts[0]["severity"], "warning")
        self.assertEqual(conflicts[0]["undeclared_values"], ["VOID"])

    def test_matching_enum_yields_no_conflict(self) -> None:
        conflicts = reconcile_dictionary_claims(
            [_row("orders", "state", "Order state: OPEN / CLOSED / VOID")],
            [_profile("orders", {"state": ["OPEN", "CLOSED", "VOID"]})],
        )
        self.assertEqual(conflicts, [])

    def test_declared_enum_parsing_shapes(self) -> None:
        self.assertEqual(
            declared_enum_values("Status: BOOKED / IN_TRANSIT / DELIVERED"),
            ["BOOKED", "IN_TRANSIT", "DELIVERED"],
        )
        self.assertEqual(declared_enum_values("ACTIVE or CLOSED"), ["ACTIVE", "CLOSED"])
        self.assertEqual(declared_enum_values("a single TOKEN only"), [])


class UnitMismatchTests(unittest.TestCase):
    def test_claimed_unit_word_vs_mixed_sibling_units_is_error(self) -> None:
        """'in kilograms' documented; sibling uom column observes KG and LB."""
        conflicts = reconcile_dictionary_claims(
            [_row("shipments", "wgt", "Chargeable weight in kilograms")],
            [
                _profile(
                    "shipments",
                    {"wgt": ["12.5", "30.1"], "wgt_uom": ["KG", "LB"]},
                    dtypes={"wgt": "Float64"},
                )
            ],
        )
        self.assertEqual(len(conflicts), 1)
        conflict = conflicts[0]
        self.assertEqual(conflict["type"], "unit_mismatch")
        self.assertEqual(conflict["severity"], "error")
        self.assertEqual(conflict["column"], "wgt")
        self.assertEqual(conflict["qualifier_column"], "wgt_uom")
        self.assertEqual(conflict["claimed_qualifier_canonical"], "kg")

    def test_claimed_code_token_vs_mixed_sibling_codes_is_error(self) -> None:
        """'in GBP' documented; sibling currency column observes EUR and GBP."""
        conflicts = reconcile_dictionary_claims(
            [_row("invoices", "Amount", "Billed amount in GBP net of tax")],
            [
                _profile(
                    "invoices",
                    {"Amount": ["10.0"], "ccy": ["EUR", "GBP"]},
                    dtypes={"Amount": "Float64"},
                )
            ],
        )
        self.assertEqual(len(conflicts), 1)
        self.assertEqual(conflicts[0]["type"], "unit_mismatch")
        self.assertEqual(conflicts[0]["qualifier_column"], "ccy")

    def test_prose_temporal_word_is_not_a_unit_claim(self) -> None:
        """'time of day' must not collide with a DAY/NIGHT category sibling."""
        conflicts = reconcile_dictionary_claims(
            [_row("shifts", "begin_at", "Shift start time of day")],
            [_profile("shifts", {"begin_at": ["06:00"], "period_cd": ["DAY", "NIGHT"]})],
        )
        self.assertEqual(conflicts, [])

    def test_bare_acronyms_outside_measurement_context_are_not_claims(self) -> None:
        """Name-suffix style tokens (PhD, MD, JD) never become unit claims."""
        self.assertEqual(claimed_qualifiers("Name suffix, such as PhD, MD, JD, etc."), [])
        claims = claimed_qualifiers("Chargeable weight in kilograms")
        self.assertEqual(
            [(claim["canonical"], claim["origin"]) for claim in claims],
            [("kg", "unit_word")],
        )

    def test_non_qualifier_shaped_alternatives_do_not_fire(self) -> None:
        """Claimed code mixed with differently-shaped category codes: no claim
        that the sibling is a unit column."""
        conflicts = reconcile_dictionary_claims(
            [_row("payments", "Amount", "Cash received in USD")],
            [_profile("payments", {"Amount": ["10.0"], "method": ["CARD", "USD", "CHEQUE"]})],
        )
        self.assertEqual(conflicts, [])


class PhantomColumnTests(unittest.TestCase):
    def test_documented_column_missing_everywhere_is_error(self) -> None:
        conflicts = reconcile_dictionary_claims(
            [_row("shipments", "del_date", "Actual delivery date")],
            [_profile("shipments", {"Id": ["S1"], "END": ["2025-01-01"]})],
        )
        self.assertEqual(len(conflicts), 1)
        self.assertEqual(conflicts[0]["type"], "phantom_column")
        self.assertEqual(conflicts[0]["severity"], "error")
        self.assertEqual(conflicts[0]["dictionary_field"], "del_date")

    def test_column_documented_in_wrong_table_is_misplaced_warning(self) -> None:
        conflicts = reconcile_dictionary_claims(
            [_row("shipments", "inv_no", "Invoice number")],
            [
                _profile("shipments", {"Id": ["S1"]}),
                _profile("invoices", {"inv_no": ["I1"]}),
            ],
        )
        self.assertEqual(len(conflicts), 1)
        self.assertEqual(conflicts[0]["type"], "misplaced_column")
        self.assertEqual(conflicts[0]["severity"], "warning")

    def test_unprofiled_table_asserts_nothing(self) -> None:
        """No profile evidence for the documented table -> no conflict."""
        conflicts = reconcile_dictionary_claims(
            [_row("ledger", "balance", "Account balance")],
            [_profile("shipments", {"Id": ["S1"]})],
        )
        self.assertEqual(conflicts, [])


class MisattributedClaimTests(unittest.TestCase):
    def test_claim_worded_around_other_dataset_with_same_column_is_warning(self) -> None:
        """'Final invoiced revenue' on shipments.Amount while invoices.Amount
        exists -> the documented meaning may belong to the other column."""
        conflicts = reconcile_dictionary_claims(
            [_row("shipments", "Amount", "Final invoiced revenue for the shipment")],
            [
                _profile("shipments", {"Amount": ["10.0"]}, dtypes={"Amount": "Float64"}),
                _profile("invoices", {"Amount": ["12.0"]}, dtypes={"Amount": "Float64"}),
            ],
        )
        self.assertEqual(len(conflicts), 1)
        conflict = conflicts[0]
        self.assertEqual(conflict["type"], "misattributed_claim")
        self.assertEqual(conflict["severity"], "warning")
        self.assertEqual(conflict["suspected_dataset"], "invoices.csv")
        self.assertEqual(conflict["suspected_column"], "Amount")

    def test_explicit_join_reference_is_not_misattribution(self) -> None:
        """'joins to invoices.inv_no' deliberately names the other table."""
        conflicts = reconcile_dictionary_claims(
            [_row("payments", "inv_no", "Paid invoice number; joins to invoices.inv_no")],
            [
                _profile("payments", {"inv_no": ["I1"]}),
                _profile("invoices", {"inv_no": ["I1"]}),
            ],
        )
        self.assertEqual(conflicts, [])

    def test_own_table_wording_is_not_misattribution(self) -> None:
        conflicts = reconcile_dictionary_claims(
            [_row("invoices", "Amount", "Invoiced amount for the line")],
            [
                _profile("invoices", {"Amount": ["12.0"]}, dtypes={"Amount": "Float64"}),
                _profile("shipments", {"Amount": ["10.0"]}, dtypes={"Amount": "Float64"}),
            ],
        )
        self.assertEqual(conflicts, [])


class TruthfulDictionaryTests(unittest.TestCase):
    def test_truthful_dictionary_yields_no_conflicts(self) -> None:
        rows = [
            _row("shipments", "Id", "Shipment number (primary key)"),
            _row("shipments", "Status", "Lifecycle status: BOOKED / DELIVERED"),
            _row("shipments", "wgt", "Chargeable weight in kilograms"),
            _row("invoices", "Amount", "Billed amount for the line"),
        ]
        profiles = [
            _profile(
                "shipments",
                {
                    "Id": ["S1", "S2"],
                    "Status": ["BOOKED", "DELIVERED"],
                    "wgt": ["12.5", "30.1"],
                    "wgt_uom": ["KG"],
                },
                dtypes={"wgt": "Float64"},
            ),
            _profile("invoices", {"Amount": ["12.0"]}, dtypes={"Amount": "Float64"}),
        ]
        self.assertEqual(reconcile_dictionary_claims(rows, profiles), [])


class MappingApplicationTests(unittest.TestCase):
    def _mapping(self, state: str = "proven_direct") -> dict:
        return {
            "kpis": [
                {
                    "kpi_id": "kpi_001",
                    "status": "ready_for_sql",
                    "features": [
                        {
                            "feature": "wgt",
                            "state": state,
                            "resolution_type": "direct_column",
                            "source_columns": [
                                {
                                    "dataset": "workspaces/demo/datasets/shipments.csv",
                                    "column": "wgt",
                                }
                            ],
                            "conflicts": [],
                            "evidence": [],
                            "question": None,
                        }
                    ],
                    "open_questions": [],
                }
            ]
        }

    def _error_conflicts(self) -> list[dict]:
        return reconcile_dictionary_claims(
            [_row("shipments", "wgt", "Chargeable weight in kilograms")],
            [
                _profile(
                    "shipments",
                    {"wgt": ["12.5"], "wgt_uom": ["KG", "LB"]},
                    dtypes={"wgt": "Float64"},
                )
            ],
        )

    def test_error_conflict_demotes_proven_feature_and_blocks_kpi(self) -> None:
        mapping = self._mapping()
        demoted = apply_conflicts_to_mapping(mapping, self._error_conflicts())
        self.assertEqual(demoted, 1)
        kpi = mapping["kpis"][0]
        feature = kpi["features"][0]
        self.assertEqual(kpi["status"], "blocked_questions_pending")
        self.assertEqual(feature["state"], "blocked_ambiguous")
        self.assertEqual(feature["resolution_type"], "dictionary_conflict")
        self.assertTrue(feature["question"])
        self.assertIn(feature["question"], kpi["open_questions"])
        self.assertEqual(len(feature["conflicts"]), 1)
        self.assertTrue(
            any(
                item.get("type") == "dictionary_profile_conflict"
                for item in feature["evidence"]
            )
        )

    def test_user_confirmed_feature_is_never_demoted(self) -> None:
        mapping = self._mapping(state="user_confirmed")
        demoted = apply_conflicts_to_mapping(mapping, self._error_conflicts())
        self.assertEqual(demoted, 0)
        kpi = mapping["kpis"][0]
        self.assertEqual(kpi["status"], "ready_for_sql")
        self.assertEqual(kpi["features"][0]["state"], "user_confirmed")
        # Conflicts still attach for visibility.
        self.assertEqual(len(kpi["features"][0]["conflicts"]), 1)

    def test_warning_conflict_attaches_without_demotion(self) -> None:
        warnings = reconcile_dictionary_claims(
            [_row("shipments", "Amount", "Final invoiced revenue for the shipment")],
            [
                _profile("shipments", {"Amount": ["10.0"]}, dtypes={"Amount": "Float64"}),
                _profile("invoices", {"Amount": ["12.0"]}, dtypes={"Amount": "Float64"}),
            ],
        )
        mapping = {
            "kpis": [
                {
                    "kpi_id": "kpi_001",
                    "status": "ready_for_sql",
                    "features": [
                        {
                            "feature": "Amount",
                            "state": "proven_direct",
                            "resolution_type": "direct_column",
                            "source_columns": [
                                {
                                    "dataset": "workspaces/demo/datasets/shipments.csv",
                                    "column": "Amount",
                                }
                            ],
                            "conflicts": [],
                            "evidence": [],
                            "question": None,
                        }
                    ],
                    "open_questions": [],
                }
            ]
        }
        demoted = apply_conflicts_to_mapping(mapping, warnings)
        self.assertEqual(demoted, 0)
        kpi = mapping["kpis"][0]
        self.assertEqual(kpi["status"], "ready_for_sql")
        self.assertEqual(len(kpi["features"][0]["conflicts"]), 1)


def _write_workspace(
    root: Path,
    *,
    kpis: list[dict],
    profiles: list[dict],
    dictionary_csv: str = "",
) -> Path:
    workspace = root / "workspaces" / "demo"
    contracts = workspace / "interns" / "generated" / "contracts"
    profiles_dir = workspace / "interns" / "generated" / "profiles"
    contracts.mkdir(parents=True)
    profiles_dir.mkdir(parents=True)
    (workspace / "datasets").mkdir(parents=True, exist_ok=True)
    (profiles_dir / "profile_index.json").write_text(
        json.dumps(
            {
                "artifact_type": "profile_index.json",
                "version": 1,
                "generated_by": "onboard-workspace",
                "workspace": "workspaces/demo",
                "profiles": profiles,
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
    return workspace


def _kpi(name: str, *, metric: str = "", cuts: str = "", description: str = "") -> dict:
    return {
        "name": name,
        "description": description,
        "cuts": cuts,
        "metric": metric,
        "refinement_required": "",
        "source": "docs/kpis.md",
        "status": "needs_mapping",
    }


_LYING_DICTIONARY = (
    "table,column,description,data_type,source_system\n"
    "shipments,wgt,Chargeable weight in kilograms,decimal,TMS\n"
)

_TRUTHFUL_DICTIONARY = (
    "table,column,description,data_type,source_system\n"
    "shipments,wgt,Chargeable weight; unit per wgt_uom,decimal,TMS\n"
)


def _shipments_profile() -> dict:
    return _profile(
        "shipments",
        {
            "Id": ["S1", "S2"],
            "wgt": ["12.5", "30.1"],
            "wgt_uom": ["KG", "LB"],
        },
        dtypes={"wgt": "Float64"},
    )


class ResolverWiringTests(unittest.TestCase):
    def _resolve(self, root: Path) -> dict:
        KPIFeatureResolver(root, "workspaces/demo").run()
        return json.loads(
            (
                root
                / "workspaces/demo/interns/generated/contracts/kpi_feature_mapping.json"
            ).read_text(encoding="utf-8")
        )

    def test_lying_dictionary_demotes_proven_mapping_and_writes_contract(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            _write_workspace(
                root,
                kpis=[_kpi("Total chargeable weight", metric="sum(wgt)")],
                profiles=[_shipments_profile()],
                dictionary_csv=_LYING_DICTIONARY,
            )
            mapping = self._resolve(root)
            kpi = mapping["kpis"][0]
            self.assertEqual(kpi["status"], "blocked_questions_pending")
            features = [
                feature
                for feature in kpi["features"]
                if feature.get("resolution_type") == "dictionary_conflict"
            ]
            self.assertEqual(len(features), 1)
            self.assertEqual(features[0]["state"], "blocked_ambiguous")
            self.assertEqual(mapping["summary"]["blocked_kpi_count"], 1)
            self.assertGreaterEqual(mapping["dictionary_conflicts"]["error_count"], 1)
            contract = json.loads(
                (
                    root
                    / "workspaces/demo/interns/generated/contracts/dictionary_conflicts.json"
                ).read_text(encoding="utf-8")
            )
            self.assertEqual(contract["artifact_type"], "dictionary_conflicts.json")
            self.assertEqual(contract["generated_by"], "resolve-kpi-features")
            types = {conflict["type"] for conflict in contract["conflicts"]}
            self.assertIn("unit_mismatch", types)

    def test_panel_surfaces_dictionary_conflict_question(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            _write_workspace(
                root,
                kpis=[_kpi("Total chargeable weight", metric="sum(wgt)")],
                profiles=[_shipments_profile()],
                dictionary_csv=_LYING_DICTIONARY,
            )
            self._resolve(root)
            current = json.loads(
                (
                    root
                    / "workspaces/demo/interns/reports/blocker_question_panel/current.json"
                ).read_text(encoding="utf-8")
            )
            self.assertEqual(current["answer_type"], "dictionary_conflict")
            self.assertEqual(current["blocker_label"], "dictionary_conflict")
            # The validator rejects recommending a non-custom placeholder option.
            self.assertEqual(current["recommended_option_id"], "custom")
            option_ids = [option["option_id"] for option in current["options"]]
            self.assertEqual(option_ids, ["option_a", "option_b", "custom"])
            labels = " ".join(
                str(option.get("label")) for option in current["options"]
            ).lower()
            self.assertIn("trust the observed data", labels)
            self.assertIn("dictionary is right", labels)
            self.assertTrue(current.get("dictionary_conflicts"))

    def test_truthful_dictionary_keeps_kpi_ready(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            _write_workspace(
                root,
                kpis=[_kpi("Total chargeable weight", metric="sum(wgt)")],
                profiles=[_shipments_profile()],
                dictionary_csv=_TRUTHFUL_DICTIONARY,
            )
            mapping = self._resolve(root)
            self.assertEqual(mapping["kpis"][0]["status"], "ready_for_sql")
            self.assertEqual(mapping["dictionary_conflicts"]["conflict_count"], 0)
            contract_path = (
                root
                / "workspaces/demo/interns/generated/contracts/dictionary_conflicts.json"
            )
            self.assertTrue(contract_path.exists())

    def test_workspace_without_dictionary_writes_no_contract(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            _write_workspace(
                root,
                kpis=[_kpi("Total chargeable weight", metric="sum(wgt)")],
                profiles=[_shipments_profile()],
            )
            mapping = self._resolve(root)
            self.assertEqual(mapping["kpis"][0]["status"], "ready_for_sql")
            self.assertNotIn("dictionary_conflicts", mapping)
            self.assertFalse(
                (
                    root
                    / "workspaces/demo/interns/generated/contracts/dictionary_conflicts.json"
                ).exists()
            )

    def test_loader_reads_dictionary_shaped_csv(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            workspace = _write_workspace(
                root,
                kpis=[_kpi("Total chargeable weight", metric="sum(wgt)")],
                profiles=[_shipments_profile()],
                dictionary_csv=_LYING_DICTIONARY,
            )
            rows = load_data_dictionary_rows(workspace, root)
            self.assertEqual(len(rows), 1)
            self.assertEqual(rows[0]["table"], "shipments")
            self.assertEqual(rows[0]["field"], "wgt")
            self.assertIn("kilograms", rows[0]["description"])
            self.assertTrue(rows[0]["path"].endswith("docs/data_dictionary.csv"))


class ValidatorWiringTests(unittest.TestCase):
    def test_generated_artifacts_pass_validation(self) -> None:
        from core.onboarding.workspace.validation import WorkspaceArtifactValidator

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            _write_workspace(
                root,
                kpis=[_kpi("Total chargeable weight", metric="sum(wgt)")],
                profiles=[_shipments_profile()],
                dictionary_csv=_LYING_DICTIONARY,
            )
            KPIFeatureResolver(root, "workspaces/demo").run()
            result = WorkspaceArtifactValidator(root, "workspaces/demo").run()
            flagged = [
                error
                for error in result.errors
                if "dictionary_conflicts" in error
                or "kpi_feature_mapping" in error
                or "blocker_question_panel" in error
            ]
            self.assertEqual(flagged, [], f"unexpected validator errors: {flagged}")

    def test_hand_promoted_kpi_over_error_conflict_fails_validation(self) -> None:
        """A ready KPI consuming an error-conflicted column without a human
        decision is the fabrication shape the validator must reject."""
        from core.onboarding.workspace.validation import WorkspaceArtifactValidator

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            _write_workspace(
                root,
                kpis=[_kpi("Total chargeable weight", metric="sum(wgt)")],
                profiles=[_shipments_profile()],
                dictionary_csv=_LYING_DICTIONARY,
            )
            KPIFeatureResolver(root, "workspaces/demo").run()
            mapping_path = (
                root
                / "workspaces/demo/interns/generated/contracts/kpi_feature_mapping.json"
            )
            mapping = json.loads(mapping_path.read_text(encoding="utf-8"))
            # Simulate the hand-edit the gate exists to catch.
            kpi = mapping["kpis"][0]
            kpi["status"] = "ready_for_sql"
            for feature in kpi["features"]:
                if feature.get("resolution_type") == "dictionary_conflict":
                    feature["state"] = "proven_direct"
            mapping["summary"]["ready_kpi_count"] = 1
            mapping["summary"]["blocked_kpi_count"] = 0
            mapping_path.write_text(
                json.dumps(mapping, indent=2), encoding="utf-8"
            )
            result = WorkspaceArtifactValidator(root, "workspaces/demo").run()
            tainted = [
                error
                for error in result.errors
                if "error-severity dictionary conflict" in error
            ]
            self.assertEqual(len(tainted), 1, f"errors: {result.errors}")

    def test_missing_contract_with_dictionary_and_profiles_warns(self) -> None:
        from core.onboarding.workspace.validation import WorkspaceArtifactValidator

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            _write_workspace(
                root,
                kpis=[_kpi("Total chargeable weight", metric="sum(wgt)")],
                profiles=[_shipments_profile()],
                dictionary_csv=_LYING_DICTIONARY,
            )
            KPIFeatureResolver(root, "workspaces/demo").run()
            contract_path = (
                root
                / "workspaces/demo/interns/generated/contracts/dictionary_conflicts.json"
            )
            contract_path.unlink()
            result = WorkspaceArtifactValidator(root, "workspaces/demo").run()
            self.assertTrue(
                any(
                    "dictionary_conflicts.json" in warning
                    and "rerun resolve-kpi-features" in warning
                    for warning in result.warnings
                ),
                f"warnings: {result.warnings}",
            )


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
