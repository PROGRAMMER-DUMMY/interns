"""``no_supporting_evidence`` blocker labeling (hostile-workspace refinement 1c).

A blocked KPI whose prose anchors to NOTHING in the workspace — no term maps to
any profiled column, dataset name, data-dictionary description, or accepted
workspace definition, and no feature carries workspace-anchored evidence or
candidates — may presuppose data the workspace does not contain. The resolver
must label that blocker ``no_supporting_evidence`` and the blocker panel must
ask the user to confirm the absence or point at the source. A KPI with at
least one evidence anchor must NOT get the label. Detection is evidence-shape
only; nothing here is keyed to any workspace's domain wording.
"""
from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from core.onboarding.kpi.feature_resolver import KPIFeatureResolver


def _write_workspace(
    root: Path,
    *,
    kpis: list[dict],
    profiles: dict[str, dict[str, str]] | None = None,
    dictionary_csv: str = "",
    definitions: list[dict] | None = None,
) -> Path:
    workspace = root / "workspaces" / "demo"
    contracts = workspace / "interns" / "generated" / "contracts"
    profiles_dir = workspace / "interns" / "generated" / "profiles"
    contracts.mkdir(parents=True)
    profiles_dir.mkdir(parents=True)
    (workspace / "datasets").mkdir(parents=True, exist_ok=True)
    profile_entries = []
    for name, schema in (profiles or {}).items():
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
    if definitions is not None:
        (contracts / "workspace_feature_definitions.json").write_text(
            json.dumps(
                {
                    "artifact_type": "workspace_feature_definitions.json",
                    "version": 1,
                    "generated_by": "apply-kpi-panel-answer",
                    "workspace": "workspaces/demo",
                    "definitions": definitions,
                }
            ),
            encoding="utf-8",
        )
    return workspace


def _kpi(
    name: str,
    *,
    description: str = "",
    metric: str = "",
    cuts: str = "",
    refinement_required: str = "",
) -> dict:
    return {
        "name": name,
        "description": description,
        "cuts": cuts,
        "metric": metric,
        "refinement_required": refinement_required,
        "source": "docs/kpis.md",
        "status": "needs_mapping",
    }


def _resolve(root: Path) -> dict:
    KPIFeatureResolver(root, "workspaces/demo").run()
    mapping_path = (
        root
        / "workspaces"
        / "demo"
        / "interns"
        / "generated"
        / "contracts"
        / "kpi_feature_mapping.json"
    )
    return json.loads(mapping_path.read_text(encoding="utf-8"))


def _panel(root: Path, name: str) -> dict:
    path = (
        root
        / "workspaces"
        / "demo"
        / "interns"
        / "reports"
        / "blocker_question_panel"
        / name
    )
    return json.loads(path.read_text(encoding="utf-8"))


def _no_evidence_features(kpi: dict) -> list[dict]:
    return [
        feature
        for feature in kpi.get("features", [])
        if feature.get("resolution_type") == "no_supporting_evidence"
    ]


class NoSupportingEvidenceLabelTests(unittest.TestCase):
    def test_prose_kpi_matching_nothing_gets_label(self) -> None:
        """Prose-only KPI whose terms anchor to no workspace evidence."""
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            _write_workspace(
                root,
                kpis=[
                    _kpi(
                        "Share of widgets reclassified after handover",
                        description=(
                            "Fraction of widgets that were reclassified to a "
                            "deluxe tier after handover, weekly."
                        ),
                    )
                ],
                profiles={"orders": {"OrderId": "String", "Region": "String"}},
            )
            mapping = _resolve(root)
            kpi = mapping["kpis"][0]
            self.assertEqual(kpi["status"], "blocked_questions_pending")
            self.assertEqual(kpi.get("blocker_label"), "no_supporting_evidence")
            features = _no_evidence_features(kpi)
            self.assertEqual(len(features), 1)
            feature = features[0]
            self.assertEqual(feature["state"], "blocked_missing_evidence")
            self.assertEqual(feature.get("blocker_label"), "no_supporting_evidence")
            self.assertIn("may presuppose data", feature["question"])
            self.assertIn(feature["question"], kpi["open_questions"])
            evidence_types = {item.get("type") for item in feature["evidence"]}
            self.assertIn("no_supporting_evidence_scan", evidence_types)
            self.assertIn("kpi_prose", evidence_types)

    def test_panel_question_survives_deferral_and_offers_confirm_or_source(self) -> None:
        """Empty-metric+cuts KPIs are deferred from feature questions, but the
        no_supporting_evidence ask is about the KPI itself and must surface."""
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            _write_workspace(
                root,
                kpis=[
                    _kpi(
                        "Share of widgets reclassified after handover",
                        description=(
                            "Fraction of widgets that were reclassified to a "
                            "deluxe tier after handover, weekly."
                        ),
                    )
                ],
                profiles={"orders": {"OrderId": "String", "Region": "String"}},
            )
            _resolve(root)
            current = _panel(root, "current.json")
            self.assertEqual(current.get("answer_type"), "no_supporting_evidence")
            self.assertEqual(current.get("blocker_label"), "no_supporting_evidence")
            self.assertIn("may presuppose data", current.get("question", ""))
            option_ids = [option.get("option_id") for option in current.get("options", [])]
            self.assertEqual(option_ids, ["option_a", "option_b", "custom"])
            labels = " ".join(
                str(option.get("label")) for option in current.get("options", [])
            ).lower()
            self.assertIn("does not exist", labels)
            self.assertIn("point to the source", labels)
            index = _panel(root, "index.json")
            self.assertTrue(
                any(
                    question.get("answer_type") == "no_supporting_evidence"
                    for question in index.get("questions", [])
                ),
                "panel set must include the no_supporting_evidence question",
            )

    def test_metric_kpi_with_unmatched_terms_gets_label(self) -> None:
        """Main resolution path: an authored metric whose tokens and prose both
        anchor to nothing."""
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            _write_workspace(
                root,
                kpis=[
                    _kpi(
                        "Total flux uplift",
                        description="Accrued flux uplift across the fleet, weekly.",
                        metric="sum(flux_uplift)",
                    )
                ],
                profiles={"orders": {"OrderId": "String", "Region": "String"}},
            )
            mapping = _resolve(root)
            kpi = mapping["kpis"][0]
            self.assertEqual(kpi["status"], "blocked_questions_pending")
            self.assertEqual(kpi.get("blocker_label"), "no_supporting_evidence")
            self.assertEqual(len(_no_evidence_features(kpi)), 1)

    def test_kpi_with_prose_anchor_is_not_labeled(self) -> None:
        """At least one prose term anchoring to workspace evidence blocks the
        label even when no feature resolved."""
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            _write_workspace(
                root,
                kpis=[
                    _kpi(
                        "Monthly revenue per active account",
                        description=(
                            "Take what we actually billed and give me revenue "
                            "per active account, by month."
                        ),
                    )
                ],
                profiles={
                    "invoices": {
                        "inv_no": "String",
                        "Amount": "Float64",
                        "acct": "String",
                    }
                },
                dictionary_csv=(
                    "table,column,description,data_type,source_system\n"
                    "invoices,Amount,Billed revenue amount for the line,decimal,FINSYS\n"
                ),
            )
            mapping = _resolve(root)
            kpi = mapping["kpis"][0]
            self.assertEqual(kpi["status"], "blocked_questions_pending")
            self.assertNotIn("blocker_label", kpi)
            self.assertEqual(_no_evidence_features(kpi), [])

    def test_kpi_with_resolved_feature_is_not_labeled(self) -> None:
        """A proven column is supporting evidence even if another token blocks.

        The blocked token is a cross-dataset name collision (blocked_ambiguous):
        a free-floating made-up cut would instead be fuzzy-matched to the proven
        metric column and folded away by the physical-column dedupe, leaving the
        KPI ready rather than blocked.
        """
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            _write_workspace(
                root,
                kpis=[
                    _kpi(
                        "Average hours by tier",
                        metric="avg(hours)",
                        cuts="tier",
                    )
                ],
                profiles={
                    "timesheets": {
                        "EmpId": "String",
                        "hours": "Float64",
                        "tier": "String",
                    },
                    "orgs": {"OrgId": "String", "tier": "String"},
                },
            )
            mapping = _resolve(root)
            kpi = mapping["kpis"][0]
            self.assertEqual(kpi["status"], "blocked_questions_pending")
            self.assertNotIn("blocker_label", kpi)
            self.assertEqual(_no_evidence_features(kpi), [])

    def test_placeholder_seed_kpi_is_not_labeled(self) -> None:
        """A seed KPI is undefined, not presupposing missing data."""
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            _write_workspace(
                root,
                kpis=[
                    _kpi(
                        "Seed KPI",
                        refinement_required="confirm metric and grain",
                    )
                ],
                profiles={"orders": {"OrderId": "String", "Region": "String"}},
            )
            mapping = _resolve(root)
            kpi = mapping["kpis"][0]
            self.assertEqual(kpi["status"], "blocked_questions_pending")
            self.assertTrue(kpi.get("placeholder_seed"))
            self.assertNotIn("blocker_label", kpi)
            self.assertEqual(_no_evidence_features(kpi), [])
            self.assertTrue(
                any(
                    feature.get("resolution_type") == "kpi_definition_required"
                    for feature in kpi["features"]
                )
            )

    def test_saved_definition_keeps_resolving_the_blocker(self) -> None:
        """An accepted panel answer (saved workspace definition) must resolve
        the no_supporting_evidence blocker on every re-run."""
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            _write_workspace(
                root,
                kpis=[
                    _kpi(
                        "Share of widgets reclassified after handover",
                        description=(
                            "Fraction of widgets that were reclassified to a "
                            "deluxe tier after handover, weekly."
                        ),
                    )
                ],
                profiles={"orders": {"OrderId": "String", "Region": "String"}},
                definitions=[
                    {
                        "feature": "KPI supporting evidence",
                        "state": "user_confirmed",
                        "resolution_type": "custom_business_definition",
                        "definition": (
                            "Confirmed: the workspace does not contain this data."
                        ),
                        "evidence_note": "Confirmed absent by reviewer.",
                        "applies_to_kpis": ["kpi_001"],
                    }
                ],
            )
            mapping = _resolve(root)
            kpi = mapping["kpis"][0]
            self.assertNotIn("blocker_label", kpi)
            # apply_definition_to_feature rewrites resolution_type to the
            # definition's, so look the feature up by its stable name.
            features = [
                feature
                for feature in kpi["features"]
                if feature.get("feature") == "KPI supporting evidence"
            ]
            self.assertEqual(len(features), 1)
            self.assertEqual(features[0]["state"], "user_confirmed")

    def test_confirming_a_real_feature_retires_the_label(self) -> None:
        """A later workspace definition that confirms a real feature breaks the
        zero-anchors premise: the synthetic blocker and the KPI label must go,
        and the KPI must become ready (BUG: enterprise definition-apply test)."""
        from core.onboarding.kpi.feature_resolver import apply_workspace_definition

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            workspace = _write_workspace(
                root,
                kpis=[
                    _kpi(
                        "Where are flux uplifts trending?",
                        description="Needs the flux uplift figure, weekly.",
                        metric="sum(flux_uplift)",
                    )
                ],
                profiles={"orders": {"OrderId": "String", "Region": "String"}},
            )
            mapping = _resolve(root)
            kpi = mapping["kpis"][0]
            self.assertEqual(kpi.get("blocker_label"), "no_supporting_evidence")
            summary = apply_workspace_definition(
                root,
                "workspaces/demo",
                feature="flux_uplift",
                state="user_confirmed",
                resolution_type="user_confirmed_formula",
                definition="Flux uplift is derived upstream; excluded here.",
                evidence_note="User confirmed the definition.",
            )
            self.assertEqual(summary["ready_kpi_count"], 1)
            mapping = json.loads(
                (
                    workspace
                    / "interns"
                    / "generated"
                    / "contracts"
                    / "kpi_feature_mapping.json"
                ).read_text(encoding="utf-8")
            )
            kpi = mapping["kpis"][0]
            self.assertEqual(kpi["status"], "ready_for_sql")
            self.assertNotIn("blocker_label", kpi)
            self.assertEqual(_no_evidence_features(kpi), [])

    def test_validator_accepts_labeled_artifacts(self) -> None:
        """The labeled mapping + panel raise none of the dead-end errors and no
        errors about the mapping or panel shape."""
        from core.onboarding.workspace.validation import WorkspaceArtifactValidator

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            _write_workspace(
                root,
                kpis=[
                    _kpi(
                        "Share of widgets reclassified after handover",
                        description=(
                            "Fraction of widgets that were reclassified to a "
                            "deluxe tier after handover, weekly."
                        ),
                    )
                ],
                profiles={"orders": {"OrderId": "String", "Region": "String"}},
            )
            _resolve(root)
            result = WorkspaceArtifactValidator(root, "workspaces/demo").run()
            flagged = [
                error
                for error in result.errors
                if "silent dead end" in error
                or "asks nothing" in error
                or "contradicts the" in error
                or "kpi_feature_mapping" in error
                or "blocker_question_panel" in error
            ]
            self.assertEqual(flagged, [], f"unexpected validator errors: {flagged}")


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
