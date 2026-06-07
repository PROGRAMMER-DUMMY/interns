"""Apply a human-confirmed KPI definition into the registry (loop-close).

Proves the decision is persisted with human provenance, mirrored into the live
contract, requires a confirmer, and is re-applied to KpiDefinition rows (so it
survives re-onboarding and overrides a derived guess).
"""
from __future__ import annotations

import json
import tempfile
import unittest
from dataclasses import dataclass
from pathlib import Path

from core.onboarding.kpi.kpi_definition import (
    apply_accepted_definitions_to_kpis,
    apply_kpi_definition,
    kpi_definition_key,
    load_kpi_definition_store,
)
from core.storage.workspace_layout import WorkspaceLayout


@dataclass(frozen=True)
class _Kpi:
    name: str
    metric: str = ""
    cuts: str = ""


class ApplyKpiDefinitionTests(unittest.TestCase):
    def _ws(self, root: Path) -> Path:
        ws = root / "workspaces" / "demo"
        contracts = ws / "interns" / "generated" / "contracts"
        contracts.mkdir(parents=True)
        (contracts / "kpi_registry.json").write_text(
            json.dumps(
                {
                    "kpis": [
                        {"name": "How many orders had zero discount?", "metric": "", "cuts": ""},
                        {"name": "Average order value by region?", "metric": "", "cuts": ""},
                    ]
                }
            ),
            encoding="utf-8",
        )
        return ws

    def test_apply_persists_decision_and_patches_contract(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self._ws(root)
            result = apply_kpi_definition(
                root,
                "workspaces/demo",
                kpi_id="kpi_001",
                metric="count(*)",
                cuts="discount = 0",
                confirmed_by="maintainer",
                note="zero-discount orders",
            )
            self.assertTrue(result.contract_patched)
            self.assertEqual(result.business_question, "How many orders had zero discount?")

            # Contract reflects the definition + provenance.
            registry = json.loads(
                (root / "workspaces/demo/interns/generated/contracts/kpi_registry.json").read_text()
            )
            kpi = registry["kpis"][0]
            self.assertEqual(kpi["metric"], "count(*)")
            self.assertEqual(kpi["cuts"], "discount = 0")
            self.assertEqual(kpi["definition_source"], "human_confirmed")
            self.assertEqual(kpi["definition_confirmed_by"], "maintainer")

            # Decision store is durable + carries human provenance.
            layout = WorkspaceLayout(project_root=(root / "workspaces/demo").resolve())
            store = load_kpi_definition_store(layout)
            key = kpi_definition_key("How many orders had zero discount?")
            self.assertIn(key, store)
            self.assertEqual(store[key]["source"], "human")
            self.assertEqual(store[key]["confirmed_by"], "maintainer")

    def test_confirmed_by_is_required(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self._ws(root)
            with self.assertRaises(ValueError):
                apply_kpi_definition(
                    root, "workspaces/demo", kpi_id="kpi_001", metric="count(*)", confirmed_by=""
                )

    def test_requires_metric_or_cuts(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self._ws(root)
            with self.assertRaises(ValueError):
                apply_kpi_definition(
                    root, "workspaces/demo", kpi_id="kpi_001", confirmed_by="x"
                )

    def test_unknown_kpi_id_raises(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self._ws(root)
            with self.assertRaises(ValueError):
                apply_kpi_definition(
                    root, "workspaces/demo", kpi_id="kpi_999", metric="count(*)", confirmed_by="x"
                )

    def test_accepted_definition_overrides_derived_cell(self) -> None:
        store = {
            kpi_definition_key("Average order value by region?"): {
                "metric": "avg(order_value)",
                "cuts": "region",
            }
        }
        kpis = [
            _Kpi(name="Average order value by region?", metric="wrong_guess", cuts=""),
            _Kpi(name="Untouched KPI?", metric="", cuts=""),
        ]
        out = apply_accepted_definitions_to_kpis(kpis, store)
        self.assertEqual(out[0].metric, "avg(order_value)")
        self.assertEqual(out[0].cuts, "region")
        self.assertEqual(out[1].metric, "")  # no decision -> untouched

    def test_business_question_path_without_kpi_id(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self._ws(root)
            result = apply_kpi_definition(
                root,
                "workspaces/demo",
                business_question="Average order value by region?",
                metric="avg(order_value)",
                cuts="region",
                confirmed_by="maintainer",
            )
            self.assertTrue(result.contract_patched)


if __name__ == "__main__":
    unittest.main()
