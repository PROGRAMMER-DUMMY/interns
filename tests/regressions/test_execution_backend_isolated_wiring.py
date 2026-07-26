"""Production-readiness fix, P2: core/execution/backend.py's
IsolatedDuckDBBackend (restricted subprocess environment -- strips secrets
down to PATH/SYSTEMROOT/PYTHONPATH) was real, tested code that
build_execution_backend never actually returned -- the no-Databricks branch
always returned bare DuckDBBackend() (full os.environ inherited by every
generated-script subprocess). docs/core_audit/PROD_SECURITY_GAPS.md Gap 5
step 1.

Fixed: `execution = "isolated"` is now a real, wired config value, checked
BEFORE the Databricks-active branch (isolation is a local-subprocess policy,
orthogonal to whether Databricks is configured at all). Every other existing
`execution` value's behavior is unchanged.

See ~/.claude/plans/dynamic-cooking-firefly.md P2.
"""
from __future__ import annotations

import unittest
from dataclasses import dataclass

from core.execution.backend import (
    DuckDBBackend,
    IsolatedDuckDBBackend,
    build_execution_backend,
)


@dataclass
class _FakeDatabricksConfig:
    execution: str = "duckdb"
    enabled: bool = False

    def is_active(self) -> bool:
        return self.enabled


@dataclass
class _FakeConfig:
    databricks: _FakeDatabricksConfig


class IsolatedBackendWiringTests(unittest.TestCase):
    def test_isolated_execution_selects_isolated_backend_databricks_inactive(self):
        cfg = _FakeConfig(databricks=_FakeDatabricksConfig(execution="isolated", enabled=False))
        backend = build_execution_backend(cfg)
        self.assertIsInstance(backend, IsolatedDuckDBBackend)

    def test_isolated_execution_selects_isolated_backend_even_when_databricks_active(self):
        # Isolation is a local-subprocess policy, orthogonal to Databricks
        # activation -- must win regardless.
        cfg = _FakeConfig(databricks=_FakeDatabricksConfig(execution="isolated", enabled=True))
        backend = build_execution_backend(cfg)
        self.assertIsInstance(backend, IsolatedDuckDBBackend)

    def test_default_duckdb_execution_unaffected_databricks_inactive(self):
        cfg = _FakeConfig(databricks=_FakeDatabricksConfig(execution="duckdb", enabled=False))
        backend = build_execution_backend(cfg)
        self.assertIsInstance(backend, DuckDBBackend)
        self.assertNotIsInstance(backend, IsolatedDuckDBBackend)


if __name__ == "__main__":
    unittest.main()
