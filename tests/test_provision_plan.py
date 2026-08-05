"""Provision planner: step shapes, catalog-per-env naming, destructive blocking."""
from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from core.onboarding.workspace.delegation import routing_for
from core.provisioning.plan import (
    ADDITIVE_KINDS,
    CHECKPOINT_VOLUME,
    KIND_BLOCKED,
    build_provision_plan,
    build_steps,
    env_catalog,
)

DISCOVERY = {
    "connector": "s3",
    "scanned_at": "2026-08-05T00:00:00+00:00",
    "tables": [
        {"name": "claims", "path": "s3://bkt/pfx/claims", "format": "parquet",
         "size_bytes": 1024, "row_estimate": 10, "is_streaming": True},
        {"name": "payers", "path": "s3://bkt/pfx/payers", "format": "csv",
         "size_bytes": 512, "row_estimate": 5, "is_streaming": False},
    ],
    "working_set_estimate_bytes": 1536,
    "notes": [],
}

DECLARATION = {
    "type": "s3",
    "location": "s3://bkt/pfx",
    "format_hint": "parquet",
    "credential_ref": "uc_cred_acme",
    "declared_by": "tester",
    "declared_at": "2026-08-05T00:00:00+00:00",
}


def _workspace(tmp: str, *, declaration=None, discovery=None, name="acme") -> tuple[Path, str]:
    root = Path(tmp)
    ws = root / "workspaces" / name
    (ws / "interns" / "generated" / "intake").mkdir(parents=True, exist_ok=True)
    (ws / "workspace_settings.json").write_text(
        json.dumps({"source_declaration": declaration if declaration is not None else DECLARATION}),
        encoding="utf-8",
    )
    if discovery is not None:
        (ws / "interns" / "generated" / "intake" / "discovery.json").write_text(
            json.dumps(discovery), encoding="utf-8"
        )
    return root, f"workspaces/{name}"


class EnvNamingTests(unittest.TestCase):
    def test_env_suffix_added_once(self):
        self.assertEqual(env_catalog("rcm", "dev"), "rcm_dev")
        self.assertEqual(env_catalog("rcm", "prod"), "rcm_prod")
        self.assertEqual(env_catalog("rcm_dev", "dev"), "rcm_dev")

    def test_unsafe_catalog_name_rejected(self):
        with self.assertRaises(ValueError):
            env_catalog("rcm; DROP", "dev")


class PlanShapeTests(unittest.TestCase):
    def test_plan_written_with_ordered_additive_steps(self):
        with tempfile.TemporaryDirectory() as tmp:
            root, ws = _workspace(tmp, discovery=DISCOVERY)
            plan = build_provision_plan(root, ws, catalog="rcm", env="dev")

            payload = json.loads(
                (root / ws / "interns/generated/contracts/provision_plan.json").read_text()
            )
            self.assertEqual(payload["catalog"], "rcm_dev")
            self.assertEqual(payload["env"], "dev")
            self.assertTrue(payload["additive_only"])
            self.assertEqual(payload["discovered_table_count"], 2)
            self.assertEqual(
                payload["checkpoint_root"], f"/Volumes/rcm_dev/bronze/{CHECKPOINT_VOLUME}"
            )

            kinds = [step["kind"] for step in payload["steps"]]
            self.assertEqual(kinds[0], "create_external_location")
            self.assertEqual(kinds[1], "create_catalog")
            self.assertEqual(kinds[2:5], ["create_schema"] * 3)
            self.assertIn("create_volume", kinds)
            for step in payload["steps"]:
                self.assertIn(step["kind"], ADDITIVE_KINDS)
                self.assertTrue(step["step_id"])
                self.assertIsInstance(step["params"], dict)
                self.assertIsInstance(step["idempotent_check"], dict)
            self.assertEqual(plan.summary()["blocked_count"], 0)

    def test_no_destructive_kind_can_ever_be_emitted(self):
        steps = build_steps(
            catalog="x_dev", connector="s3", location="s3://b/p", credential_ref="c"
        )
        serialized = json.dumps(steps).lower()
        for word in ("drop", "replace", "delete", "truncate", "overwrite"):
            self.assertNotIn(word, serialized, f"planner emitted {word!r}")

    def test_checkpoint_volume_is_managed_and_in_bronze(self):
        steps = build_steps(
            catalog="x_dev", connector="s3", location="s3://b/p", credential_ref="c"
        )
        volume = next(s for s in steps if s["kind"] == "create_volume")
        self.assertEqual(volume["params"]["volume_type"], "MANAGED")
        self.assertEqual(volume["params"]["schema"], "bronze")
        self.assertEqual(volume["params"]["name"], CHECKPOINT_VOLUME)
        self.assertIn("lifecycle", volume["params"]["purpose"])

    def test_non_object_store_connector_skips_external_location(self):
        steps = build_steps(
            catalog="x_dev", connector="jdbc",
            location="jdbc:postgresql://host/db", credential_ref="scope",
        )
        self.assertNotIn("create_external_location", [s["kind"] for s in steps])
        # jdbc is not a checkpointing connector either
        self.assertNotIn("create_volume", [s["kind"] for s in steps])

    def test_credential_ref_recorded_as_name_only(self):
        with tempfile.TemporaryDirectory() as tmp:
            root, ws = _workspace(tmp, discovery=DISCOVERY)
            build_provision_plan(root, ws, catalog="rcm", env="prod")
            text = (root / ws / "interns/generated/contracts/provision_plan.json").read_text()
            self.assertIn("uc_cred_acme", text)
            for secretish in ("password", "AKIA", "token", "secret_key"):
                self.assertNotIn(secretish, text)


class DestructiveBlockingTests(unittest.TestCase):
    def test_repointing_existing_external_location_is_blocked(self):
        steps = build_steps(
            catalog="x_dev", connector="s3", location="s3://new/prefix",
            credential_ref="c",
            existing_objects={"external_location:x_dev_root": "s3://old/prefix/"},
        )
        blocked = [s for s in steps if s["kind"] == KIND_BLOCKED]
        self.assertEqual(len(blocked), 1)
        self.assertEqual(blocked[0]["params"]["requested_kind"], "create_external_location")
        self.assertIn("destructive gate", blocked[0]["reason"])
        self.assertIsNone(blocked[0]["idempotent_check"])

    def test_same_url_is_not_blocked_just_idempotent(self):
        steps = build_steps(
            catalog="x_dev", connector="s3", location="s3://same/prefix",
            credential_ref="c",
            existing_objects={"external_location:x_dev_root": "s3://same/prefix"},
        )
        self.assertFalse([s for s in steps if s["kind"] == KIND_BLOCKED])

    def test_existing_checkpoint_volume_with_location_is_blocked(self):
        steps = build_steps(
            catalog="x_dev", connector="s3", location="s3://b/p", credential_ref="c",
            existing_objects={f"volume:x_dev.bronze.{CHECKPOINT_VOLUME}": "s3://b/chk"},
        )
        blocked = [s for s in steps if s["kind"] == KIND_BLOCKED]
        self.assertEqual(len(blocked), 1)
        self.assertEqual(blocked[0]["params"]["requested_kind"], "create_volume")

    def test_grant_on_new_catalog_planned_but_blocked_on_existing_catalog(self):
        new_steps = build_steps(
            catalog="x_dev", connector="s3", location="s3://b/p", credential_ref="c",
            grant_principals=("data-readers",),
        )
        grant = next(s for s in new_steps if s["kind"] == "grant")
        self.assertEqual(grant["params"]["principal"], "data-readers")
        self.assertIn("SELECT", grant["params"]["privileges"])

        existing_steps = build_steps(
            catalog="x_dev", connector="s3", location="s3://b/p", credential_ref="c",
            grant_principals=("data-readers",),
            existing_objects={"catalog:x_dev": ""},
        )
        blocked = [s for s in existing_steps if s["kind"] == KIND_BLOCKED]
        self.assertEqual(len(blocked), 1)
        self.assertEqual(blocked[0]["params"]["requested_kind"], "grant")

    def test_existing_objects_from_discovery_are_honoured(self):
        with tempfile.TemporaryDirectory() as tmp:
            discovery = dict(DISCOVERY)
            discovery["existing_objects"] = {"external_location:rcm_dev_root": "s3://other/"}
            root, ws = _workspace(tmp, discovery=discovery)
            plan = build_provision_plan(root, ws, catalog="rcm", env="dev")
            self.assertEqual(plan.summary()["blocked_count"], 1)


class InputRefusalTests(unittest.TestCase):
    def test_missing_source_declaration_refuses(self):
        with tempfile.TemporaryDirectory() as tmp:
            root, ws = _workspace(tmp, declaration={}, discovery=DISCOVERY)
            with self.assertRaises(FileNotFoundError):
                build_provision_plan(root, ws, catalog="rcm", env="dev")

    def test_missing_discovery_still_plans_containers_with_a_note(self):
        with tempfile.TemporaryDirectory() as tmp:
            root, ws = _workspace(tmp)
            plan = build_provision_plan(root, ws, catalog="rcm", env="dev")
            self.assertTrue(any("no tables" in note for note in plan.notes))
            self.assertIn("create_catalog", [s["kind"] for s in plan.steps])

    def test_workspace_agnostic_no_hardcoded_names(self):
        with tempfile.TemporaryDirectory() as tmp:
            root, ws = _workspace(
                tmp, name="totally_other",
                declaration={**DECLARATION, "type": "gcs", "location": "gs://x/y"},
                discovery={"connector": "gcs", "tables": []},
            )
            plan = build_provision_plan(root, ws, env="dev")
            self.assertEqual(plan.catalog, "totally_other_dev")


class StageRoutingTests(unittest.TestCase):
    def test_plan_payload_and_summary_carry_the_provisioning_roster(self):
        with tempfile.TemporaryDirectory() as tmp:
            root, ws = _workspace(tmp, discovery=DISCOVERY)
            plan = build_provision_plan(root, ws, catalog="rcm", env="dev")
            payload = json.loads(
                (root / ws / "interns/generated/contracts/provision_plan.json").read_text()
            )
            roster = routing_for("provisioning")
            self.assertTrue(roster["agents"], "provisioning routes no agent")
            for label, emitted in (("payload", payload), ("summary", plan.summary())):
                with self.subTest(payload=label):
                    self.assertEqual(emitted["stage"], "provisioning")
                    self.assertEqual(emitted["required_specialists"], roster["agents"])
                    self.assertEqual(emitted["suggested_skills"], roster["skills"])


if __name__ == "__main__":
    unittest.main()
