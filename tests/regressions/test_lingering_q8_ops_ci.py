"""Regressions for Q8 (lingering-issues plan): CI<->green-gate parity + the
PySpark parity-check temp-dir leak. See ~/.claude/plans/dynamic-cooking-firefly.md Q8.

P1.3 (CI job runs green_gate.py --json) was already true on the current
`.github/workflows/ci.yml` -- verified, no code change needed there.

P1.4: `_pyspark_parity` (core/onboarding/kpi/verify_kpi_output.py) spawns the
generated `<kpi>_pyspark.py` script as a subprocess with a 1200s timeout. A
timeout kills the child hard (no graceful `spark.stop()`), and the generated
script previously never set `spark.local.dir`, so Spark's shuffle/blockmgr
scratch files landed in the bare OS temp root with no owner to clean them up.
Fix: the PARENT now owns a dedicated scratch dir, passes it to the child via
`SPARK_LOCAL_DIR_OVERRIDE`, and always removes it in a `finally` -- so cleanup
happens even when the child is killed, not just on a graceful exit.
"""
from __future__ import annotations

import json
import os
import subprocess
import tempfile
import unittest
from pathlib import Path

from core.onboarding.kpi.pyspark_generator import PySparkKPIGenerator
from core.onboarding.kpi.verify_kpi_output import KPIOutputVerifier, VerifyRecord


class CiGreenGateParityTests(unittest.TestCase):
    def test_ci_tests_job_invokes_green_gate(self):
        repo_root = Path(__file__).resolve().parents[2]
        ci_yml = (repo_root / ".github" / "workflows" / "ci.yml").read_text(encoding="utf-8")
        self.assertIn("core.dev.green_gate --json", ci_yml)


class PysparkScratchDirTests(unittest.TestCase):
    def _generated_script_text(self, tmp_path: Path) -> str:
        ws = tmp_path / "workspaces" / "demo"
        contracts = ws / "interns" / "generated" / "contracts"
        profiles = ws / "interns" / "generated" / "profiles"
        contracts.mkdir(parents=True)
        profiles.mkdir(parents=True)
        (ws / "datasets").mkdir(parents=True)
        (ws / "datasets" / "sales.csv").write_text(
            "region,amount\nNA,10\nEU,20\n", encoding="utf-8"
        )
        ds = "workspaces/demo/datasets/sales.csv"
        (profiles / "profile_index.json").write_text(json.dumps({"profiles": [{
            "path": ds, "format": "csv", "row_count": 2,
            "schema": {"region": {}, "amount": {}},
        }]}), encoding="utf-8")
        (contracts / "kpi_feature_mapping.json").write_text(json.dumps({"kpis": [{
            "kpi_id": "kpi_001", "name": "total amount by region",
            "metric": "sum(amount)", "cuts": "region",
            "features": [
                {"feature": "amount", "state": "proven_direct",
                 "source_columns": [{"dataset": ds, "column": "amount"}]},
                {"feature": "region", "state": "proven_direct",
                 "source_columns": [{"dataset": ds, "column": "region"}]},
            ],
        }]}), encoding="utf-8")
        PySparkKPIGenerator(tmp_path, "workspaces/demo").generate("kpi_001")
        script_path = ws / "interns" / "generated" / "solutions" / "kpi_001_pyspark.py"
        return script_path.read_text(encoding="utf-8")

    def test_generated_script_wires_spark_local_dir_from_env_override(self):
        with tempfile.TemporaryDirectory() as tmp:
            text = self._generated_script_text(Path(tmp))
        self.assertIn("SPARK_LOCAL_DIR_OVERRIDE", text)
        self.assertIn('.config("spark.local.dir", _spark_local_dir)', text)

    def test_pyspark_parity_cleans_up_scratch_dir_on_success(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "workspaces" / "demo").mkdir(parents=True)
            verifier = KPIOutputVerifier(root, "workspaces/demo")
            verifier.layout.solutions_dir.mkdir(parents=True, exist_ok=True)
            pyspark_path = verifier.layout.solutions_dir / "kpi_001_pyspark.py"
            pyspark_path.write_text("# stub", encoding="utf-8")

            seen_dirs: list[str] = []
            real_run = subprocess.run

            def fake_run(cmd, **kwargs):
                scratch = kwargs["env"]["SPARK_LOCAL_DIR_OVERRIDE"]
                seen_dirs.append(scratch)
                self.assertTrue(os.path.isdir(scratch))
                return subprocess.CompletedProcess(cmd, 1, stdout="", stderr="boom")

            import core.onboarding.kpi.verify_kpi_output as vko

            orig = vko.subprocess.run
            vko.subprocess.run = fake_run
            try:
                record = VerifyRecord(kpi_id="kpi_001", sql_path="x", result_view="kpi_001_results")
                verifier._pyspark_parity(record, "kpi_001", "amount", None, None)
            finally:
                vko.subprocess.run = orig

            self.assertEqual(len(seen_dirs), 1)
            self.assertFalse(os.path.isdir(seen_dirs[0]), "scratch dir must be removed after the run")

    def test_pyspark_parity_cleans_up_scratch_dir_on_timeout(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "workspaces" / "demo").mkdir(parents=True)
            verifier = KPIOutputVerifier(root, "workspaces/demo")
            verifier.layout.solutions_dir.mkdir(parents=True, exist_ok=True)
            pyspark_path = verifier.layout.solutions_dir / "kpi_001_pyspark.py"
            pyspark_path.write_text("# stub", encoding="utf-8")

            seen_dirs: list[str] = []

            def fake_run(cmd, **kwargs):
                seen_dirs.append(kwargs["env"]["SPARK_LOCAL_DIR_OVERRIDE"])
                raise subprocess.TimeoutExpired(cmd, 1200)

            import core.onboarding.kpi.verify_kpi_output as vko

            orig = vko.subprocess.run
            vko.subprocess.run = fake_run
            try:
                record = VerifyRecord(kpi_id="kpi_001", sql_path="x", result_view="kpi_001_results")
                verifier._pyspark_parity(record, "kpi_001", "amount", None, None)
            finally:
                vko.subprocess.run = orig

            self.assertEqual(len(seen_dirs), 1)
            self.assertFalse(
                os.path.isdir(seen_dirs[0]),
                "scratch dir must be removed even when the child is killed on timeout",
            )


if __name__ == "__main__":
    unittest.main()
