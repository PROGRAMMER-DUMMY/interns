"""Bulk KPI definitions: apply-kpi-definition --from-file (.csv / .json).

Proves the bulk path is all-or-nothing (validate every row before applying
anything), reuses the single-definition apply path per row, records per-row
provenance honestly (named confirmer -> source: human; empty -> source: agent,
never fabricated), honors the --confirmed-by CLI default, and refuses to mix
--from-file with the single-definition flags.
"""
from __future__ import annotations

import contextlib
import io
import json
import tempfile
import unittest
from pathlib import Path

from core.onboarding.kpi.kpi_definition import (
    apply_kpi_definitions_from_file,
    kpi_definition_key,
    load_kpi_definition_store,
    main,
)
from core.storage.workspace_layout import WorkspaceLayout

_Q1 = "How many orders had zero discount?"
_Q2 = "Average order value by region?"
_Q3 = "How many distinct customers placed an order?"


class BulkKpiDefinitionTests(unittest.TestCase):
    def _ws(self, root: Path) -> Path:
        ws = root / "workspaces" / "demo"
        contracts = ws / "interns" / "generated" / "contracts"
        contracts.mkdir(parents=True)
        (contracts / "kpi_registry.json").write_text(
            json.dumps(
                {
                    "kpis": [
                        {"name": _Q1, "metric": "", "cuts": ""},
                        {"name": _Q2, "metric": "", "cuts": ""},
                        {"name": _Q3, "metric": "", "cuts": ""},
                    ]
                }
            ),
            encoding="utf-8",
        )
        return ws

    def _store(self, root: Path) -> dict:
        layout = WorkspaceLayout(project_root=(root / "workspaces/demo").resolve())
        return load_kpi_definition_store(layout)

    def _registry(self, root: Path) -> dict:
        return json.loads(
            (root / "workspaces/demo/interns/generated/contracts/kpi_registry.json").read_text(
                encoding="utf-8"
            )
        )

    def test_happy_csv_applies_all_rows(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self._ws(root)
            bulk = root / "definitions.csv"
            bulk.write_text(
                "kpi_id,business_question,metric,cuts,confirmed_by,note\n"
                'kpi_001,,count(*),discount = 0,alice,zero-discount orders\n'
                f'"",{_Q2},avg(order_value),region,bob,\n',
                encoding="utf-8",
            )
            report = apply_kpi_definitions_from_file(root, "workspaces/demo", bulk)
            self.assertEqual(report["errors"], [])
            self.assertEqual(len(report["applied"]), 2)
            self.assertEqual(
                [item["status"] for item in report["applied"]], ["patched", "patched"]
            )

            registry = self._registry(root)
            self.assertEqual(registry["kpis"][0]["metric"], "count(*)")
            self.assertEqual(registry["kpis"][0]["cuts"], "discount = 0")
            self.assertEqual(registry["kpis"][1]["metric"], "avg(order_value)")

            store = self._store(root)
            self.assertEqual(store[kpi_definition_key(_Q1)]["confirmed_by"], "alice")
            self.assertEqual(store[kpi_definition_key(_Q2)]["confirmed_by"], "bob")

    def test_happy_json_applies_all_rows(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self._ws(root)
            bulk = root / "definitions.json"
            bulk.write_text(
                json.dumps(
                    [
                        {
                            "kpi_id": "kpi_001",
                            "metric": "count(*)",
                            "cuts": "discount = 0",
                            "confirmed_by": "alice",
                        },
                        {
                            "business_question": _Q3,
                            "metric": "count(distinct customer_id)",
                            "confirmed_by": "alice",
                        },
                    ]
                ),
                encoding="utf-8",
            )
            report = apply_kpi_definitions_from_file(root, "workspaces/demo", bulk)
            self.assertEqual(report["errors"], [])
            self.assertEqual(len(report["applied"]), 2)
            registry = self._registry(root)
            self.assertEqual(registry["kpis"][0]["metric"], "count(*)")
            self.assertEqual(
                registry["kpis"][2]["metric"], "count(distinct customer_id)"
            )
            store = self._store(root)
            self.assertEqual(store[kpi_definition_key(_Q3)]["source"], "human")

    def test_all_or_nothing_on_one_bad_row(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self._ws(root)
            bulk = root / "definitions.csv"
            bulk.write_text(
                "kpi_id,metric,cuts,confirmed_by\n"
                "kpi_001,count(*),discount = 0,alice\n"
                "kpi_999,count(*),,alice\n"  # unknown kpi -> invalidates the batch
                "kpi_002,,,alice\n",  # blank metric AND cuts -> row error
                encoding="utf-8",
            )
            report = apply_kpi_definitions_from_file(root, "workspaces/demo", bulk)
            self.assertEqual(report["applied"], [])
            self.assertEqual([item["row"] for item in report["errors"]], [2, 3])
            self.assertIn("kpi_999", report["errors"][0]["error"])
            self.assertIn("metric or cuts", report["errors"][1]["error"])

            # NOTHING was applied: registry untouched, decision store absent.
            registry = self._registry(root)
            self.assertEqual(registry["kpis"][0]["metric"], "")
            self.assertEqual(self._store(root), {})

    def test_per_row_confirmed_by_provenance(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self._ws(root)
            bulk = root / "definitions.csv"
            bulk.write_text(
                "kpi_id,metric,confirmed_by\n"
                "kpi_001,count(*),alice\n"
                "kpi_002,avg(order_value),\n",  # no confirmer, no default
                encoding="utf-8",
            )
            report = apply_kpi_definitions_from_file(root, "workspaces/demo", bulk)
            self.assertEqual(report["errors"], [])
            self.assertEqual(
                [item["source"] for item in report["applied"]], ["human", "agent"]
            )
            store = self._store(root)
            self.assertEqual(store[kpi_definition_key(_Q1)]["source"], "human")
            self.assertEqual(store[kpi_definition_key(_Q1)]["confirmed_by"], "alice")
            # Empty confirmer is recorded honestly as agent-asserted (BUG-014).
            self.assertEqual(store[kpi_definition_key(_Q2)]["source"], "agent")
            self.assertEqual(store[kpi_definition_key(_Q2)]["confirmed_by"], "")
            registry = self._registry(root)
            self.assertEqual(registry["kpis"][0]["definition_source"], "human_confirmed")
            self.assertEqual(registry["kpis"][1]["definition_source"], "agent_asserted")

    def test_default_confirmed_by_fallback(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self._ws(root)
            bulk = root / "definitions.csv"
            bulk.write_text(
                "kpi_id,metric,confirmed_by\n"
                "kpi_001,count(*),\n"  # falls back to the CLI default
                "kpi_002,avg(order_value),bob\n",  # keeps its own confirmer
                encoding="utf-8",
            )
            report = apply_kpi_definitions_from_file(
                root, "workspaces/demo", bulk, default_confirmed_by="ops-reviewer"
            )
            self.assertEqual(report["errors"], [])
            self.assertEqual(
                [item["source"] for item in report["applied"]], ["human", "human"]
            )
            store = self._store(root)
            self.assertEqual(
                store[kpi_definition_key(_Q1)]["confirmed_by"], "ops-reviewer"
            )
            self.assertEqual(store[kpi_definition_key(_Q2)]["confirmed_by"], "bob")

    def test_mixed_flags_error(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self._ws(root)
            bulk = root / "definitions.csv"
            bulk.write_text(
                "kpi_id,metric,confirmed_by\nkpi_001,count(*),alice\n",
                encoding="utf-8",
            )
            out = io.StringIO()
            with contextlib.redirect_stdout(out):
                code = main(
                    [
                        "--workspace",
                        "workspaces/demo",
                        "--repo-root",
                        str(root),
                        "--from-file",
                        str(bulk),
                        "--kpi-id",
                        "kpi_001",
                    ]
                )
            self.assertEqual(code, 2)
            self.assertIn("mutually exclusive", out.getvalue())
            self.assertIn("--kpi-id", out.getvalue())
            self.assertEqual(self._store(root), {})  # nothing applied

    def test_unknown_csv_column_is_named_in_error(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self._ws(root)
            bulk = root / "definitions.csv"
            bulk.write_text(
                "kpi_id,metric,confirmed_by,priority\nkpi_001,count(*),alice,high\n",
                encoding="utf-8",
            )
            with self.assertRaises(ValueError) as ctx:
                apply_kpi_definitions_from_file(root, "workspaces/demo", bulk)
            self.assertIn("priority", str(ctx.exception))
            self.assertEqual(self._store(root), {})

    def test_cli_bulk_happy_path_prints_summary_table(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self._ws(root)
            bulk = root / "definitions.csv"
            bulk.write_text(
                "kpi_id,metric,cuts,confirmed_by\nkpi_001,count(*),discount = 0,alice\n",
                encoding="utf-8",
            )
            out = io.StringIO()
            with contextlib.redirect_stdout(out):
                code = main(
                    [
                        "--workspace",
                        "workspaces/demo",
                        "--repo-root",
                        str(root),
                        "--from-file",
                        str(bulk),
                    ]
                )
            text = out.getvalue()
            self.assertEqual(code, 0)
            self.assertIn("[ok] applied 1 kpi definition(s)", text)
            self.assertIn("kpi_001", text)
            self.assertIn("human", text)
            self.assertIn("patched", text)


if __name__ == "__main__":
    unittest.main()
