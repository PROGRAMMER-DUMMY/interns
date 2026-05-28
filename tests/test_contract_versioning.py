"""Tests for the contract schema versioning registry."""
from __future__ import annotations

import logging
import unittest

from core.contracts import versioning
from core.contracts.versioning import (
    known_artifacts,
    migrate,
    register_contract,
    register_migration,
)

# Importing the writer modules registers each contract as a side effect. The
# registry is module-level, so importing once per process is sufficient.
import core.onboarding.lexicon.builder  # noqa: F401
import core.onboarding.workspace.onboarding  # noqa: F401
import core.onboarding.kpi.feature_resolver  # noqa: F401
import core.onboarding.relationships.contracts  # noqa: F401
import core.onboarding.relationships.source_to_target_planner  # noqa: F401
import core.onboarding.pipeline_plan  # noqa: F401
import core.onboarding.kpi.blocker_question_panel  # noqa: F401


class ContractVersioningTests(unittest.TestCase):
    def setUp(self) -> None:
        # Snapshot the registries so each test can register fake artifacts
        # without leaking into sibling tests.
        self._registry_snapshot = dict(versioning._REGISTRY)
        self._migrations_snapshot = dict(versioning._MIGRATIONS)

    def tearDown(self) -> None:
        versioning._REGISTRY.clear()
        versioning._REGISTRY.update(self._registry_snapshot)
        versioning._MIGRATIONS.clear()
        versioning._MIGRATIONS.update(self._migrations_snapshot)

    def test_migrate_returns_payload_unchanged_when_current(self) -> None:
        register_contract("fake_current.json", current_version=3)
        payload = {"artifact_type": "fake_current.json", "version": 3, "value": 42}
        result = migrate(payload)
        self.assertIs(result, payload)
        self.assertEqual(result["value"], 42)

    def test_migrate_raises_when_below_minimum_readable(self) -> None:
        register_contract(
            "fake_min.json",
            current_version=5,
            minimum_readable_version=3,
        )
        payload = {"artifact_type": "fake_min.json", "version": 2}
        with self.assertRaises(ValueError):
            migrate(payload)

    def test_migrate_applies_chain_of_two_migrations(self) -> None:
        register_contract("fake_chain.json", current_version=3)

        def to_v2(payload: dict) -> dict:
            out = dict(payload)
            out["touched_by_v2"] = True
            return out

        def to_v3(payload: dict) -> dict:
            out = dict(payload)
            out["touched_by_v3"] = True
            return out

        register_migration("fake_chain.json", 1, 2, to_v2)
        register_migration("fake_chain.json", 2, 3, to_v3)

        payload = {"artifact_type": "fake_chain.json", "version": 1, "seed": "start"}
        result = migrate(payload)
        self.assertEqual(result["version"], 3)
        self.assertTrue(result["touched_by_v2"])
        self.assertTrue(result["touched_by_v3"])
        self.assertEqual(result["seed"], "start")

    def test_migrate_unregistered_artifact_returns_payload_unchanged(self) -> None:
        payload = {"artifact_type": "not_registered.json", "version": 99, "x": 1}
        result = migrate(payload)
        self.assertIs(result, payload)

    def test_migrate_warns_when_version_above_current(self) -> None:
        register_contract("fake_future.json", current_version=2)
        payload = {"artifact_type": "fake_future.json", "version": 5}
        with self.assertLogs(versioning.LOGGER, level=logging.WARNING) as captured:
            result = migrate(payload)
        self.assertIs(result, payload)
        self.assertTrue(
            any("fake_future.json" in message for message in captured.output)
        )

    def test_known_artifacts_includes_registered_contracts(self) -> None:
        registered = set(known_artifacts())
        expected = {
            "workspace_lexicon.json",
            "kpi_registry.json",
            "kpi_feature_mapping.json",
            "relationship_contracts.json",
            "source_to_target_plan.json",
            "pipeline_plan.json",
            "blocker_question_panel/current.json",
        }
        missing = expected - registered
        self.assertFalse(missing, f"missing registered artifacts: {missing}")


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
