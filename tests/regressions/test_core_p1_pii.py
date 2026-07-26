"""Regression tests for core remediation P1 — PII/PHI fails green.

Each test locks down one bullet of REMEDIATION_PLAN.md P1 so a sensitive column
can no longer reach Gold, the results packet, or a non-covered remote target
while every gate reports green. Workspace-agnostic; no domain vocabulary.
"""
from __future__ import annotations

import hashlib
import json
import os
import shutil
import time
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace

from core.storage.workspace_layout import WorkspaceLayout


def _write(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data), encoding="utf-8")


# ── P1a: masking is single-sourced and identical across engines ──────────────
class MaskingParityTests(unittest.TestCase):
    def test_sensitivity_read_from_both_contract_shapes(self) -> None:
        from core.onboarding.kpi.sensitive_masking import load_sensitive_columns

        with TemporaryDirectory() as tmp:
            layout = WorkspaceLayout(project_root=Path(tmp) / "workspaces" / "demo")
            # Flat shape the onboarder writes.
            _write(
                layout.contracts_dir / "semantic_contract.json",
                {"columns": {"ssn": {"is_sensitive": True}, "dept": {"is_sensitive": False}}},
            )
            self.assertEqual(load_sensitive_columns(layout), {"ssn"})

            # Nested shape used by other producers.
            _write(
                layout.contracts_dir / "semantic_contract.json",
                {"datasets": [{"name": "d", "columns": {"PAN": {"pii": True}}, "pii_columns": ["iban"]}]},
            )
            self.assertEqual(load_sensitive_columns(layout), {"pan", "iban"})

    def test_feature_sensitivity_uses_metadata_not_expression_split(self) -> None:
        # ob-kpi-b.md:126 — a derived formula's rendered expression must NOT be
        # string-split to detect sensitivity; the source-column metadata is used.
        from core.onboarding.kpi.sensitive_masking import (
            feature_sensitive_columns,
            is_feature_sensitive,
        )

        sensitive = {"ssn"}
        derived = {
            "feature": "masked_metric",
            "resolution_type": "derived_formula",
            "source_columns": [{"column": "ssn"}, {"column": "amount"}],
        }
        self.assertEqual(feature_sensitive_columns(derived, sensitive), {"ssn"})
        self.assertTrue(is_feature_sensitive(derived, sensitive))

        # A non-sensitive feature whose rendered expr merely *contains* a
        # sensitive substring must not be flagged.
        benign = {"feature": "ssn_count_band", "source_columns": [{"column": "ssn_count_band"}]}
        self.assertFalse(is_feature_sensitive(benign, sensitive))

    def test_sql_mask_is_sha256_and_cross_engine_identical(self) -> None:
        import duckdb

        from core.onboarding.kpi.sensitive_masking import mask_sql_expr

        expr = mask_sql_expr('"name"', "duckdb")
        self.assertIn("sha256", expr)
        self.assertNotIn("hash(", expr)  # the old non-crypto, non-portable hash

        # DuckDB sha256(CAST(x AS VARCHAR)) == python hashlib == polars helper.
        value = "Alice"
        duck = duckdb.sql(f"SELECT {mask_sql_expr(repr(value), 'duckdb')}").fetchone()[0]
        py = hashlib.sha256(value.encode("utf-8")).hexdigest()
        self.assertEqual(duck, py)

    def test_databricks_mask_uses_sha2(self) -> None:
        from core.onboarding.kpi.sensitive_masking import mask_sql_expr

        self.assertIn("sha2(", mask_sql_expr('`name`', "databricks"))

    def test_polars_helper_matches_duckdb_digest(self) -> None:
        import polars as pl

        from core.onboarding.kpi.sensitive_masking import (
            POLARS_MASK_HELPER,
            polars_mask_helper_lines,
        )

        ns: dict = {"pl": pl, "hashlib": hashlib}
        exec("\n".join(polars_mask_helper_lines()), ns)  # noqa: S102 - generated helper under test
        df = pl.DataFrame({"name": ["Alice", None]})
        out = df.with_columns(ns[POLARS_MASK_HELPER]("name"))["name"].to_list()
        self.assertEqual(out[0], hashlib.sha256("Alice".encode()).hexdigest())
        self.assertIsNone(out[1])

    def test_all_three_generators_consume_shared_masking(self) -> None:
        # Guards against an engine being forgotten: every generator must import
        # the shared masking lookup so it cannot silently skip masking.
        from core.onboarding.kpi import (
            polars_generator,
            pyspark_generator,
            sql_generator,
        )

        for mod in (sql_generator, polars_generator, pyspark_generator):
            src = Path(mod.__file__).read_text(encoding="utf-8")
            self.assertIn("sensitive_masking", src, mod.__name__)
            self.assertIn("load_sensitive_columns", src, mod.__name__)


# ── P1b: result packet + verifier tables redact ──────────────────────────────
class DisplayRedactionTests(unittest.TestCase):
    def test_redact_table_rows_masks_pii_and_buckets_age(self) -> None:
        from core.onboarding.kpi.pii_redaction import (
            REDACTION_PLACEHOLDER,
            redact_table_rows,
        )

        cols = ["ssn", "age", "dept"]
        rows = [["123-45-6789", 95, "ER"], [None, 40, "ICU"]]
        out = redact_table_rows(cols, rows)
        self.assertEqual(out[0][0], REDACTION_PLACEHOLDER)  # ssn redacted
        self.assertEqual(out[0][1], "90+")                   # age>89 bucketed
        self.assertEqual(out[0][2], "ER")                    # non-PII passes
        self.assertIsNone(out[1][0])                          # None preserved
        self.assertEqual(out[1][1], 40)

    def test_workspace_patterns_extend_defaults(self) -> None:
        from core.onboarding.kpi.pii_redaction import (
            DEFAULT_PII_COLUMN_PATTERNS,
            workspace_redaction_patterns,
        )

        # No workspace -> defaults; never narrower than defaults.
        self.assertEqual(workspace_redaction_patterns(None), DEFAULT_PII_COLUMN_PATTERNS)

    def test_result_packet_renderer_is_wired_to_redaction(self) -> None:
        from core.onboarding.workspace import flow

        src = Path(flow.__file__).read_text(encoding="utf-8")
        self.assertIn("redact_table_rows", src)
        self.assertIn("workspace_redaction_patterns", src)

    def test_verifier_sample_table_is_wired_to_redaction(self) -> None:
        from core.onboarding.kpi import verify_kpi_output

        src = Path(verify_kpi_output.__file__).read_text(encoding="utf-8")
        self.assertIn("redact_table_rows", src)


# ── P1c: sensitivity field shape unified so the Silver invariant can fire ─────
class SensitivityShapeUnificationTests(unittest.TestCase):
    def test_validator_collects_flat_is_sensitive(self) -> None:
        from core.onboarding.workspace.validation import _collect_pii_columns_from_sc

        flat = {"columns": {"ssn": {"is_sensitive": True}, "dept": {"is_sensitive": False}}}
        self.assertIn("ssn", _collect_pii_columns_from_sc(flat))
        self.assertNotIn("dept", _collect_pii_columns_from_sc(flat))

        nested = {"datasets": [{"name": "d", "columns": {"pan": {"pii": True}}}]}
        self.assertIn("d.pan", _collect_pii_columns_from_sc(nested))

    def test_medallion_collect_reads_flat_shape(self) -> None:
        from core.medallion.pii import collect_pii_columns_from_semantic_contract

        flat = {"columns": {"SSN": {"is_sensitive": True}}}
        self.assertIn("ssn", collect_pii_columns_from_semantic_contract(flat))

    def test_medallion_design_attributes_flat_sensitive_via_schema(self) -> None:
        from core.medallion.design import (
            _pii_lookup_from_semantic,
            _sensitive_cols_for_dataset,
        )

        semantic = {"columns": {"ssn": {"is_sensitive": True}}}
        lookup = _pii_lookup_from_semantic(semantic)
        self.assertIn("ssn", lookup.get("*", []))
        # Global sensitive name attributed to a dataset that has the column.
        cols = _sensitive_cols_for_dataset(lookup, "patients", {"ssn": "VARCHAR", "dept": "VARCHAR"})
        self.assertEqual(cols, ["ssn"])
        # ...and NOT to a dataset without it.
        self.assertEqual(_sensitive_cols_for_dataset(lookup, "depts", {"dept": "VARCHAR"}), [])


# ── P1d: PCI patterns single-sourced from phi_gate (full-set equality) ────────
class PciSingleSourceTests(unittest.TestCase):
    def test_display_pci_equals_gate_pci(self) -> None:
        from core.governance.phi_gate import PCI_IDENTIFIER_PATTERNS
        from core.onboarding.kpi import pii_redaction

        gate = {p for ps in PCI_IDENTIFIER_PATTERNS.values() for p in ps}
        self.assertEqual(set(pii_redaction._PCI_COLUMN_PATTERNS), gate)

    def test_previously_drifted_columns_now_match(self) -> None:
        from core.onboarding.kpi.pii_redaction import is_pii_column

        for col in ("cid", "magstripe", "exp_month", "swift_code"):
            self.assertTrue(is_pii_column(col), col)


# ── P1e: phi_gate fails closed on a stale profile ────────────────────────────
class PhiGateFreshnessTests(unittest.TestCase):
    def _layout_with_profile(self, tmp: str, *, stale: bool, column: str = "dept"):
        layout = WorkspaceLayout(project_root=Path(tmp) / "workspaces" / "demo")
        dataset = Path(tmp) / "datasets" / "d.csv"
        dataset.parent.mkdir(parents=True, exist_ok=True)
        dataset.write_text("a,b\n1,2\n", encoding="utf-8")
        index = layout.profile_index_path
        _write(
            index,
            {"profiles": [{"path": str(dataset), "schema": {column: "VARCHAR"}, "size_bytes": dataset.stat().st_size}]},
        )
        if stale:
            # Make the dataset newer than the index (well beyond tolerance).
            future = time.time() + 60
            os.utime(dataset, (future, future))
        else:
            # Index newer than dataset.
            past = time.time() - 60
            os.utime(dataset, (past, past))
        return layout

    def test_fresh_profile_not_flagged(self) -> None:
        from core.governance.phi_gate import assess_workspace_phi

        with TemporaryDirectory() as tmp:
            layout = self._layout_with_profile(tmp, stale=False)
            self.assertFalse(assess_workspace_phi(layout).stale)

    def test_stale_profile_flagged(self) -> None:
        from core.governance.phi_gate import assess_workspace_phi

        with TemporaryDirectory() as tmp:
            layout = self._layout_with_profile(tmp, stale=True)
            self.assertTrue(assess_workspace_phi(layout).stale)

    def test_stale_profile_blocks_uncovered_remote(self) -> None:
        from core.governance.phi_gate import enforce_remote_phi_gate

        with TemporaryDirectory() as tmp:
            layout = self._layout_with_profile(tmp, stale=True)
            cfg = SimpleNamespace(phi_covered=False)
            failure = enforce_remote_phi_gate(layout, cfg, operation="remote_upload")
            self.assertIsNotNone(failure)
            self.assertIn("stale", failure.stage.lower())

    def test_stale_profile_allowed_when_covered(self) -> None:
        from core.governance.phi_gate import enforce_remote_phi_gate

        with TemporaryDirectory() as tmp:
            layout = self._layout_with_profile(tmp, stale=True)
            cfg = SimpleNamespace(phi_covered=True)  # covered target: allowed
            self.assertIsNone(enforce_remote_phi_gate(layout, cfg))


# ── P1f: execution backend PHI gate fails CLOSED on internal error ───────────
class BackendFailClosedTests(unittest.TestCase):
    def test_gate_internal_error_denies_remote(self) -> None:
        from core import governance
        # Importing the package alone does not bind the submodule attribute; the
        # patch target below needs `core.governance.phi_gate` imported explicitly.
        import core.governance.phi_gate  # noqa: F401
        from core.execution import backend

        def _boom(*a, **k):
            raise RuntimeError("gate exploded")

        original = governance.phi_gate.enforce_remote_sensitive_gate
        governance.phi_gate.enforce_remote_sensitive_gate = _boom
        # `_phi_gate_failure_for_task` returns None early when the workspace path
        # does not exist, so the workspace must be REAL for the gate to be reached.
        # Previously this named a checked-in workspace that was later deleted, so
        # the test silently exercised the early return instead of the gate. Create
        # a throwaway one under the repo root instead of depending on the tree.
        ws_dir = backend.ROOT / "workspaces" / "_tmp_phi_gate_failclosed"
        ws_dir.mkdir(parents=True, exist_ok=True)
        try:
            task = {"workspace": f"workspaces/{ws_dir.name}"}
            cfg = SimpleNamespace(databricks=SimpleNamespace(phi_covered=False, pci_covered=False))
            failure = backend._phi_gate_failure_for_task(task, cfg)
            self.assertIsNotNone(failure)  # fail CLOSED, not None (fail-open)
            self.assertEqual(failure.stage, "remote_execute_phi_gate_error")
        finally:
            governance.phi_gate.enforce_remote_sensitive_gate = original
            shutil.rmtree(ws_dir, ignore_errors=True)


if __name__ == "__main__":
    unittest.main()
