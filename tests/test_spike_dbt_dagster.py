"""Phase 2 spike guard: the dbt models keep reconciling to gold + governance.

Runs the offline validation harness (spikes/dbt_dagster/validate_spike.py) and
asserts it passes. Skipped when the workspace bronze layer is absent, like the
other dashboard real-data tests. This keeps the spike honest under green-gate
without pulling dbt/dagster into the repo's dependencies.
"""
from __future__ import annotations

import importlib.util
import unittest
from pathlib import Path

_REPO = Path(__file__).resolve().parents[1]
_BRONZE = (_REPO / "workspaces" / "Healthcare-RCM-Data-Platform"
           / "interns" / "state" / "medallion" / "bronze")
_HARNESS = _REPO / "spikes" / "dbt_dagster" / "validate_spike.py"


@unittest.skipUnless(_BRONZE.exists() and _HARNESS.exists(),
                     "Healthcare-RCM bronze layer or spike harness not present")
class TestDbtSpikeReconciles(unittest.TestCase):
    def test_validate_spike_passes(self):
        spec = importlib.util.spec_from_file_location("validate_spike", _HARNESS)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        self.assertEqual(module.main(), 0,
                         "dbt spike models no longer reconcile to gold")


if __name__ == "__main__":
    unittest.main()
