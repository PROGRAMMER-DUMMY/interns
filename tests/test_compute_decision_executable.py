"""The recorded compute decision is ACTED ON at build time: a workspace whose
volume recommends distributed (Spark) but runs on local duckdb gets a clear
mismatch warning -- closing the recommend-vs-run gap for compute."""
from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from core.medallion.build import _compute_decision_mismatch


def _decisions(dir_: Path, bytes_: int, recommendation: str) -> Path:
    (dir_ / "architecture_decisions.json").write_text(
        json.dumps({
            "measured": {"total_source_bytes": bytes_},
            "decisions": {"compute_engine": {"recommendation": recommendation}},
        }), encoding="utf-8")
    return dir_


class ComputeDecisionExecutableTests(unittest.TestCase):
    def test_single_node_recommendation_on_duckdb_is_fine(self):
        with tempfile.TemporaryDirectory() as tmp:
            d = _decisions(Path(tmp), 16_000_000, "Single-node (Polars / DuckDB)")
            self.assertIsNone(_compute_decision_mismatch(d, "duckdb"))

    def test_spark_recommendation_on_duckdb_warns(self):
        with tempfile.TemporaryDirectory() as tmp:
            d = _decisions(Path(tmp), 5 * 1024 ** 4, "Distributed (Spark)")
            warning = _compute_decision_mismatch(d, "duckdb")
            self.assertIsNotNone(warning)
            self.assertIn("--target delta", warning)

    def test_spark_recommendation_on_delta_is_fine(self):
        with tempfile.TemporaryDirectory() as tmp:
            d = _decisions(Path(tmp), 5 * 1024 ** 4, "Distributed (Spark)")
            self.assertIsNone(_compute_decision_mismatch(d, "delta"))

    def test_missing_decisions_file_is_silent(self):
        with tempfile.TemporaryDirectory() as tmp:
            self.assertIsNone(_compute_decision_mismatch(Path(tmp), "duckdb"))


if __name__ == "__main__":
    unittest.main()
