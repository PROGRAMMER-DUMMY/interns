"""Onboarding consumption of human-confirmed PDF KPI candidates.

Closes the PDF->registry loop: a candidate a human accepted via
apply-document-candidate (durable accepted_candidates.json) is merged into the
KPI set during onboarding as a proposal (status=needs_mapping, source=document_pdf).
"""
from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from core.onboarding.workspace.onboarding import (
    KpiDefinition,
    WorkspaceOnboarder,
    _document_kpi_header_role,
)


class DocumentKpiHeaderRoleTests(unittest.TestCase):
    def test_name_metric_cuts_headers(self):
        self.assertEqual(_document_kpi_header_role("KPI"), "name")
        self.assertEqual(_document_kpi_header_role("Indicator"), "name")
        self.assertEqual(_document_kpi_header_role("Metric"), "metric")
        self.assertEqual(_document_kpi_header_role("Formula"), "metric")
        self.assertEqual(_document_kpi_header_role("Cuts"), "cuts")
        self.assertEqual(_document_kpi_header_role("Dimension"), "cuts")

    def test_unknown_header_is_none(self):
        self.assertIsNone(_document_kpi_header_role("Owner"))
        self.assertIsNone(_document_kpi_header_role("Notes"))


class AcceptedDocumentKpiMergeTests(unittest.TestCase):
    def _onboarder_with_accepted(self, tmp: Path, decisions: list[dict]) -> WorkspaceOnboarder:
        ws = tmp / "workspaces" / "demo"
        ws.mkdir(parents=True)
        ob = WorkspaceOnboarder(tmp, "workspaces/demo")
        ob.layout.ensure_runtime_dirs()
        store = ob.layout.generated_dir / "documents" / "accepted_candidates.json"
        store.parent.mkdir(parents=True, exist_ok=True)
        store.write_text(
            json.dumps({"artifact_type": "accepted_document_candidates", "decisions": decisions}),
            encoding="utf-8",
        )
        return ob

    def _kpi_decision(self, name: str) -> dict:
        return {
            "decision": "accepted",
            "candidate_type": "kpi_registry_candidate",
            "confirmed_by": "shubham",
            "source_document": "workspaces/demo/docs/spec.pdf",
            "content": {
                "headers": ["KPI", "Metric", "Cuts"],
                "sample_rows": [{"KPI": name, "Metric": "sum(PaidAmount)", "Cuts": "PayorID"}],
            },
        }

    def test_accepted_kpi_becomes_proposal(self):
        with tempfile.TemporaryDirectory() as tmp:
            ob = self._onboarder_with_accepted(Path(tmp), [self._kpi_decision("Total Paid Amount")])
            merged, warnings = ob._accepted_document_kpis([])
            self.assertEqual(len(merged), 1)
            kpi = merged[0]
            self.assertEqual(kpi.name, "Total Paid Amount")
            self.assertEqual(kpi.metric, "sum(PaidAmount)")
            self.assertEqual(kpi.cuts, "PayorID")
            self.assertEqual(kpi.status, "needs_mapping")
            self.assertTrue(kpi.source.startswith("document_pdf:"))
            self.assertTrue(any("document_kpis_merged" in w for w in warnings))

    def test_authored_kpi_name_is_not_overridden(self):
        with tempfile.TemporaryDirectory() as tmp:
            ob = self._onboarder_with_accepted(Path(tmp), [self._kpi_decision("Total Paid Amount")])
            existing = [KpiDefinition(name="Total Paid Amount", metric="sum(x)", cuts="y")]
            merged, _ = ob._accepted_document_kpis(existing)
            self.assertEqual(merged, [])  # same name already present -> not duplicated

    def test_no_accepted_store_is_noop(self):
        with tempfile.TemporaryDirectory() as tmp:
            ws = Path(tmp) / "workspaces" / "demo"
            ws.mkdir(parents=True)
            ob = WorkspaceOnboarder(Path(tmp), "workspaces/demo")
            ob.layout.ensure_runtime_dirs()
            merged, warnings = ob._accepted_document_kpis([])
            self.assertEqual(merged, [])
            self.assertEqual(warnings, [])


if __name__ == "__main__":
    unittest.main()
