"""Non-blocking semantic gloss self-grill in verify_kpi_output, plus generation
gloss discovery. Proves the verifier flags a measure column whose dictionary
gloss does not match the question's measure noun -- the wrong-column class the
structural intent check cannot see. Generic fixtures (orders/tickets)."""
from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from core.onboarding.kpi.verify_kpi_output import KPIOutputVerifier, VerifyRecord


def _ws_with_dictionary(root: Path, rows: str) -> Path:
    ws = root / "workspaces" / "demo"
    ws.mkdir(parents=True)
    (ws / "data_dictionary.csv").write_text(
        "Table,Field,Description\n" + rows, encoding="utf-8"
    )
    return ws


class SemanticGlossCheckTests(unittest.TestCase):
    def _verifier(self, root: Path) -> KPIOutputVerifier:
        return KPIOutputVerifier(root, "workspaces/demo")

    def test_mismatched_gloss_emits_warning_but_stays_ok(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            _ws_with_dictionary(
                root,
                "tickets,base_price,The base price before any adjustments\n"
                "tickets,settled_total,The total settled value including all line items\n",
            )
            v = self._verifier(root)
            rec = VerifyRecord(kpi_id="kpi_001", sql_path="x")
            kpi = {"name": "average settled value by region", "metric": "avg(base_price)"}
            v._check_semantic_gloss(rec, kpi)
            self.assertTrue(any("semantic_gloss_mismatch" in w for w in rec.warnings))
            self.assertTrue(rec.ok)  # warning, not error

    def test_matching_gloss_emits_no_warning(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            _ws_with_dictionary(
                root,
                "tickets,settled_total,The total settled value including all line items\n",
            )
            v = self._verifier(root)
            rec = VerifyRecord(kpi_id="kpi_001", sql_path="x")
            kpi = {"name": "average settled value by region", "metric": "avg(settled_total)"}
            v._check_semantic_gloss(rec, kpi)
            self.assertFalse(any("semantic_gloss_mismatch" in w for w in rec.warnings))

    def test_no_dictionary_is_noop(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "workspaces" / "demo").mkdir(parents=True)
            v = self._verifier(root)
            rec = VerifyRecord(kpi_id="kpi_001", sql_path="x")
            v._check_semantic_gloss(rec, {"name": "average x", "metric": "avg(y)"})
            self.assertEqual(rec.warnings, [])


class GrainExplosionTests(unittest.TestCase):
    def _verifier(self, root):
        (root / "workspaces" / "demo").mkdir(parents=True)
        return KPIOutputVerifier(root, "workspaces/demo")

    def test_raw_age_grouping_warns(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            v = self._verifier(root)
            rec = VerifyRecord(kpi_id="kpi_002", sql_path="x")
            v._check_grain_explosion(rec, {"cuts": "Department, Gender, Age (DOB)"})
            self.assertTrue(any("grain_explosion_risk" in w for w in rec.warnings))
            self.assertTrue(rec.ok)  # non-blocking

    def test_age_in_filter_does_not_warn(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            v = self._verifier(root)
            rec = VerifyRecord(kpi_id="k", sql_path="x")
            v._check_grain_explosion(rec, {"cuts": "Department, age >= 50"})
            self.assertFalse(any("grain_explosion_risk" in w for w in rec.warnings))


class GenerationGlossDiscoveryTests(unittest.TestCase):
    def test_load_session_glosses_discovers_dictionary(self) -> None:
        from core.onboarding.kpi.generation_workflow import _load_session_glosses

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            _ws_with_dictionary(
                root, "orders,net_value,The net value after discounts\n"
            )
            glosses = _load_session_glosses({"workspace": "workspaces/demo"}, root)
            self.assertTrue(any(g["column"] == "net_value" for g in glosses))


if __name__ == "__main__":
    unittest.main()
