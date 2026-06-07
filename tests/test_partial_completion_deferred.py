"""Issue #2: a MIX of defined + undefined KPIs must produce partial results.

An undefined KPI (no metric AND no grain/cuts) is DEFERRED, not a blocker:
  - its unresolved feature tokens must NOT create feature-blocker questions
    (otherwise a defined KPI is held hostage by an undefined sibling), and
  - it must NOT count toward the source-to-target blocked count, so the DEFINED
    KPIs clear the gate and reach generation.

These tests pin the three generic mechanisms that make that true. Run with
.venv\\Scripts\\python.exe -m unittest tests.test_partial_completion_deferred
(never uv run). Generic fixtures only (orders/customers/regions)."""
from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from core.onboarding.kpi.blocker_question_panel import (
    _deferred_kpi_ids_from_registry,
    _feature_items,
)
from core.onboarding.relationships.source_to_target_planner import _kpi_is_undefined
from core.storage.workspace_layout import WorkspaceLayout


class DeferredKpiIdDerivationTests(unittest.TestCase):
    def _write_registry(self, root: Path, kpis: list[dict]) -> Path:
        ws = root / "workspaces" / "proj"
        layout = WorkspaceLayout(project_root=ws)
        layout.contracts_dir.mkdir(parents=True, exist_ok=True)
        (layout.contracts_dir / "kpi_registry.json").write_text(
            json.dumps({"kpis": kpis}), encoding="utf-8"
        )
        return ws

    def test_only_undefined_kpis_are_deferred(self):
        with tempfile.TemporaryDirectory() as tmp:
            ws = self._write_registry(
                Path(tmp),
                [
                    {"kpi_id": "kpi_001", "metric": "sum(amount)", "cuts": "region"},
                    {"kpi_id": "kpi_002", "metric": "", "cuts": ""},  # undefined
                    {"kpi_id": "kpi_003", "metric": "", "cuts": "region"},  # grain -> defined
                ],
            )
            self.assertEqual(_deferred_kpi_ids_from_registry(ws), {"kpi_002"})

    def test_enumerated_id_when_kpi_id_absent(self):
        with tempfile.TemporaryDirectory() as tmp:
            ws = self._write_registry(
                Path(tmp),
                [
                    {"metric": "count(*)", "cuts": ""},  # idx 1 -> defined
                    {"metric": "", "cuts": ""},  # idx 2 -> undefined
                ],
            )
            self.assertEqual(_deferred_kpi_ids_from_registry(ws), {"kpi_002"})

    def test_missing_registry_returns_empty(self):
        with tempfile.TemporaryDirectory() as tmp:
            self.assertEqual(
                _deferred_kpi_ids_from_registry(Path(tmp) / "nope"), set()
            )


class FeatureItemFilteringTests(unittest.TestCase):
    def _mapping(self):
        return {
            "kpis": [
                {
                    "kpi_id": "kpi_001",
                    "features": [
                        {"feature": "Amount", "state": "unresolved"},
                        {"feature": "Region", "state": "proven_direct"},
                    ],
                },
                {
                    "kpi_id": "kpi_002",  # deferred (undefined sibling)
                    "features": [
                        {"feature": "MysteryToken", "state": "unresolved"},
                    ],
                },
            ]
        }

    def test_deferred_kpi_features_are_filtered_out(self):
        items = _feature_items(self._mapping(), {"kpi_002"})
        names = set(items.keys())
        # the defined KPI's unresolved feature is kept...
        self.assertTrue(any("amount" in n for n in names), names)
        # ...the deferred KPI's unresolved feature is dropped
        self.assertFalse(any("mystery" in n for n in names), names)

    def test_no_deferral_keeps_all_unresolved(self):
        items = _feature_items(self._mapping(), set())
        names = set(items.keys())
        self.assertTrue(any("amount" in n for n in names), names)
        self.assertTrue(any("mystery" in n for n in names), names)


class KpiIsUndefinedTests(unittest.TestCase):
    def test_empty_metric_and_cuts_is_undefined(self):
        self.assertTrue(_kpi_is_undefined({"metric": "", "cuts": ""}))

    def test_metric_present_is_defined(self):
        self.assertFalse(_kpi_is_undefined({"metric": "sum(x)", "cuts": ""}))

    def test_cuts_present_is_defined(self):
        self.assertFalse(_kpi_is_undefined({"metric": "", "cuts": "region"}))


if __name__ == "__main__":
    unittest.main()
