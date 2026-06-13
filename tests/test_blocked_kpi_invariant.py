"""Blocked-KPI honesty invariant (hostile-workspace finding F1).

A KPI must never be ``blocked_questions_pending`` with zero questions: every
blocked KPI carries at least one panel question or an explicit machine-readable
blocker feature. Also covers the no-silent-ready rules: a KPI without a metric,
or with a machine-derived (``derived_from_question``) metric, must block for
confirmation instead of silently reaching ``ready_for_sql``.
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
    return workspace


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


class BlockedKpiInvariantTests(unittest.TestCase):
    def test_blocked_kpi_with_no_extractable_identifiers_still_asks(self) -> None:
        """Metric text yielding zero identifiers must not be a silent dead end."""
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            _write_workspace(
                root,
                kpis=[
                    {
                        "name": "Opaque KPI",
                        "description": "",
                        "cuts": "",
                        "metric": "total of",
                        "refinement_required": "",
                        "source": "docs/kpis.md",
                        "status": "needs_mapping",
                    }
                ],
                profiles={"orders": {"OrderId": "String", "Amount": "Float64"}},
            )
            mapping = _resolve(root)
            kpi = mapping["kpis"][0]
            self.assertEqual(kpi["status"], "blocked_questions_pending")
            self.assertTrue(kpi["open_questions"], "blocked KPI must carry a question")
            self.assertTrue(
                any(
                    feature.get("resolution_type") == "kpi_definition_required"
                    for feature in kpi["features"]
                )
            )

    def test_every_blocked_kpi_in_mapping_has_a_question(self) -> None:
        """The F1 invariant across an entire mapping with mixed KPI shapes."""
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            _write_workspace(
                root,
                kpis=[
                    {
                        "name": "KPI A - prose only",
                        "description": "Share of orders delivered on time, monthly.",
                        "cuts": "",
                        "metric": "",
                        "refinement_required": "",
                        "source": "docs/kpis.md",
                        "status": "needs_mapping",
                    },
                    {
                        "name": "KPI B - cuts only",
                        "description": "",
                        "cuts": "Region",
                        "metric": "",
                        "refinement_required": "",
                        "source": "docs/kpis.md",
                        "status": "needs_mapping",
                    },
                    {
                        "name": "KPI C - unresolvable metric",
                        "description": "",
                        "cuts": "",
                        "metric": "sum(UnknownThing)",
                        "refinement_required": "",
                        "source": "docs/kpis.md",
                        "status": "needs_mapping",
                    },
                ],
                profiles={"orders": {"OrderId": "String", "Region": "String"}},
            )
            mapping = _resolve(root)
            for kpi in mapping["kpis"]:
                if kpi["status"] != "blocked_questions_pending":
                    continue
                self.assertTrue(
                    kpi["open_questions"],
                    f"{kpi['kpi_id']} is blocked with zero questions",
                )

    def test_cuts_only_kpi_blocks_with_definition_ask(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            _write_workspace(
                root,
                kpis=[
                    {
                        "name": "Damage rate by region",
                        "description": "",
                        "cuts": "Region",
                        "metric": "",
                        "refinement_required": "",
                        "source": "docs/kpis.md",
                        "status": "needs_mapping",
                    }
                ],
                profiles={"orders": {"OrderId": "String", "Region": "String"}},
            )
            mapping = _resolve(root)
            kpi = mapping["kpis"][0]
            self.assertEqual(kpi["status"], "blocked_questions_pending")
            types = {feature.get("resolution_type") for feature in kpi["features"]}
            self.assertIn("kpi_definition_required", types)
            self.assertTrue(kpi["open_questions"])

    def test_machine_derived_metric_requires_confirmation(self) -> None:
        """provenance=derived_from_question must block for confirmation."""
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            _write_workspace(
                root,
                kpis=[
                    {
                        "name": "Average depot dwell hours",
                        "description": "How long freight sits before moving on.",
                        "cuts": "",
                        "metric": "avg(hours)",
                        "metric_provenance": "derived_from_question",
                        "refinement_required": "",
                        "source": "docs/kpis.md",
                        "status": "needs_mapping",
                    }
                ],
                profiles={"timesheets": {"EmpId": "String", "hours": "Float64"}},
            )
            mapping = _resolve(root)
            kpi = mapping["kpis"][0]
            self.assertEqual(kpi["status"], "blocked_questions_pending")
            confirm = [
                feature
                for feature in kpi["features"]
                if feature.get("resolution_type") == "derived_metric_unconfirmed"
            ]
            self.assertEqual(len(confirm), 1)
            self.assertIn("derived_from_question", confirm[0]["question"])
            self.assertTrue(confirm[0]["candidates"], "must carry column candidates")

    def test_authored_metric_still_resolves_ready(self) -> None:
        """Control: an authored metric that fully resolves stays ready."""
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            _write_workspace(
                root,
                kpis=[
                    {
                        "name": "Average hours",
                        "description": "",
                        "cuts": "",
                        "metric": "avg(hours)",
                        "refinement_required": "",
                        "source": "docs/kpis.md",
                        "status": "needs_mapping",
                    }
                ],
                profiles={"timesheets": {"EmpId": "String", "hours": "Float64"}},
            )
            mapping = _resolve(root)
            self.assertEqual(mapping["kpis"][0]["status"], "ready_for_sql")

    def test_definition_blocker_carries_prose_and_anchor_evidence(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            _write_workspace(
                root,
                kpis=[
                    {
                        "name": "Monthly revenue per active account",
                        "description": (
                            "Take what we actually billed and give me revenue per "
                            "active account, by month."
                        ),
                        "cuts": "",
                        "metric": "",
                        "refinement_required": "",
                        "source": "docs/kpis.md",
                        "status": "needs_mapping",
                    }
                ],
                profiles={
                    "invoices": {"inv_no": "String", "Amount": "Float64", "acct": "String"},
                    "party": {"party_key": "String", "Status": "String"},
                },
                dictionary_csv=(
                    "table,column,description,data_type,source_system\n"
                    "invoices,Amount,Billed revenue amount for the line,decimal,FINSYS\n"
                ),
            )
            mapping = _resolve(root)
            kpi = mapping["kpis"][0]
            self.assertEqual(kpi["status"], "blocked_questions_pending")
            definition = next(
                feature
                for feature in kpi["features"]
                if feature.get("resolution_type") == "kpi_definition_required"
            )
            evidence_types = {item.get("type") for item in definition["evidence"]}
            self.assertIn("kpi_prose", evidence_types)
            self.assertIn("prose_term_match", evidence_types)
            anchors = [
                item
                for item in definition["evidence"]
                if item.get("type") == "prose_term_match"
            ]
            self.assertTrue(
                any("invoices" in str(anchor.get("source", "")) for anchor in anchors),
                f"expected an invoices anchor, got: {anchors}",
            )
            self.assertIn("Monthly revenue per active account", definition["question"])


def _write_mapping(workspace: Path, kpis: list[dict], *, blocked: int) -> None:
    contracts = workspace / "interns" / "generated" / "contracts"
    contracts.mkdir(parents=True, exist_ok=True)
    (contracts / "kpi_feature_mapping.json").write_text(
        json.dumps(
            {
                "artifact_type": "kpi_feature_mapping.json",
                "version": 2,
                "generated_by": "resolve-kpi-features",
                "workspace": "workspaces/demo",
                "kpis": kpis,
                "blocker_clusters": [],
                "summary": {
                    "kpi_count": len(kpis),
                    "ready_kpi_count": len(kpis) - blocked,
                    "blocked_kpi_count": blocked,
                    "unresolved_feature_count": blocked,
                },
            }
        ),
        encoding="utf-8",
    )


def _mapped_kpi(kpi_id: str, *, question: str | None) -> dict:
    feature = {
        "feature": "SomeFeature",
        "state": "blocked_missing_evidence",
        "resolution_type": "unresolved",
        "source_columns": [],
        "evidence": [],
        "question": question,
    }
    return {
        "kpi_id": kpi_id,
        "name": f"{kpi_id} name",
        "status": "blocked_questions_pending",
        "features": [feature],
        "open_questions": [question] if question else [],
    }


def _write_panel(
    workspace: Path,
    *,
    question: str,
    options: list[dict] | None = None,
    status: str = "blocked_without_question",
) -> None:
    panel_dir = workspace / "interns" / "reports" / "blocker_question_panel"
    panel_dir.mkdir(parents=True, exist_ok=True)
    (panel_dir / "current.json").write_text(
        json.dumps(
            {
                "artifact_type": "blocker_question_panel/current.json",
                "version": 1,
                "generated_by": "blocker-question-panel",
                "workspace": "workspaces/demo",
                "question_id": "q1",
                "feature": "SomeFeature",
                "status": status,
                "blocker": "blocker text",
                "question": question,
                "answer_type": "none",
                "recommended_answer": "",
                "why": "",
                "options": options or [],
            }
        ),
        encoding="utf-8",
    )
    (panel_dir / "current.md").write_text("# panel\n", encoding="utf-8")


class ValidatorDeadEndTests(unittest.TestCase):
    """F3: validate-workspace-artifacts flags the silent dead-end shapes."""

    def _validate(self, root: Path):
        from core.onboarding.workspace.validation import WorkspaceArtifactValidator

        return WorkspaceArtifactValidator(root, "workspaces/demo").run()

    def test_blocked_kpi_with_zero_questions_is_an_error(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            workspace = _write_workspace(root, kpis=[], profiles={})
            _write_mapping(
                workspace, [_mapped_kpi("kpi_001", question=None)], blocked=1
            )
            _write_panel(workspace, question="ask something")
            result = self._validate(root)
            self.assertFalse(result.ok)
            self.assertTrue(
                any("silent dead end" in error for error in result.errors),
                f"expected dead-end error, got: {result.errors}",
            )

    def test_panel_that_asks_nothing_while_blocked_is_an_error(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            workspace = _write_workspace(root, kpis=[], profiles={})
            _write_mapping(
                workspace, [_mapped_kpi("kpi_001", question="define it")], blocked=1
            )
            _write_panel(workspace, question="", options=[])
            result = self._validate(root)
            self.assertFalse(result.ok)
            self.assertTrue(
                any("asks nothing" in error for error in result.errors),
                f"expected empty-panel error, got: {result.errors}",
            )

    def test_open_questions_contradicting_blocked_mapping_is_an_error(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            workspace = _write_workspace(root, kpis=[], profiles={})
            _write_mapping(
                workspace, [_mapped_kpi("kpi_001", question="define it")], blocked=1
            )
            _write_panel(workspace, question="define it")
            reports = workspace / "interns" / "reports"
            reports.mkdir(parents=True, exist_ok=True)
            (reports / "open_questions.md").write_text(
                "# Open Questions\n\nAll KPI features resolved — no open questions.\n",
                encoding="utf-8",
            )
            result = self._validate(root)
            self.assertFalse(result.ok)
            self.assertTrue(
                any("contradicts the" in error for error in result.errors),
                f"expected contradiction error, got: {result.errors}",
            )

    def test_blocked_kpi_with_question_and_panel_passes_dead_end_checks(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            workspace = _write_workspace(root, kpis=[], profiles={})
            _write_mapping(
                workspace, [_mapped_kpi("kpi_001", question="define it")], blocked=1
            )
            _write_panel(workspace, question="define it")
            reports = workspace / "interns" / "reports"
            reports.mkdir(parents=True, exist_ok=True)
            (reports / "open_questions.md").write_text(
                "# Open Questions\n\n- define it\n", encoding="utf-8"
            )
            result = self._validate(root)
            dead_end = [
                error
                for error in result.errors
                if "silent dead end" in error
                or "asks nothing" in error
                or "contradicts the" in error
            ]
            self.assertEqual(dead_end, [], f"unexpected dead-end errors: {dead_end}")


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
