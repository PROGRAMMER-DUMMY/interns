"""Provision apply: confirmation refusal, idempotent skips, structured failures."""
from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from core.onboarding.workspace.delegation import routing_for
from core.provisioning.apply import (
    STATUS_APPLIED,
    STATUS_DRY_RUN,
    STATUS_REFUSED_KILL_SWITCH,
    STATUS_REFUSED_NO_CONFIRMATION,
    STATUS_REFUSED_UNAVAILABLE,
    apply_provision_plan,
    covering_external_location,
)
from core.provisioning.plan import build_provision_plan

DECLARATION = {
    "type": "s3",
    "location": "s3://bkt/pfx",
    "format_hint": "parquet",
    "credential_ref": "uc_cred_x",
    "declared_by": "tester",
    "declared_at": "2026-08-05T00:00:00+00:00",
}
DISCOVERY = {
    "connector": "s3",
    "tables": [{"name": "t1", "path": "s3://bkt/pfx/t1", "format": "parquet",
                "size_bytes": 1, "row_estimate": 1, "is_streaming": True}],
    "working_set_estimate_bytes": 1,
}


class FakeApi:
    """Records calls; `existing` seeds objects that are already present."""

    def __init__(
        self,
        existing: set[str] | None = None,
        fail_on: str = "",
        locations: dict[str, str] | None = None,
    ):
        self.existing = set(existing or ())
        self.fail_on = fail_on
        self.calls: list[str] = []
        # name -> url, mirroring what Unity Catalog already holds.
        self.locations = dict(locations or {})
        # catalog name -> storage_root it was created with.
        self.storage_roots: dict[str, str] = {}

    def _create(self, key: str) -> None:
        self.calls.append(key)
        if self.fail_on and key.startswith(self.fail_on):
            raise RuntimeError("boom")
        self.existing.add(key)

    def catalog_exists(self, name):
        return f"catalog:{name}" in self.existing

    def create_catalog(self, name, storage_root=""):
        self.storage_roots[name] = storage_root
        self._create(f"catalog:{name}")

    def schema_exists(self, catalog, schema):
        return f"schema:{catalog}.{schema}" in self.existing

    def create_schema(self, catalog, schema):
        self._create(f"schema:{catalog}.{schema}")

    def external_location_exists(self, name):
        return f"external_location:{name}" in self.existing

    def list_external_locations(self):
        return dict(self.locations)

    def create_external_location(self, name, url, credential):
        self._create(f"external_location:{name}")
        self.locations[name] = url

    def volume_exists(self, catalog, schema, name):
        return f"volume:{catalog}.{schema}.{name}" in self.existing

    def create_managed_volume(self, catalog, schema, name):
        self._create(f"volume:{catalog}.{schema}.{name}")

    def create_external_volume(self, catalog, schema, name, location):
        self._create(f"volume:{catalog}.{schema}.{name}")

    def grant(self, securable_type, securable, principal, privileges):
        self._create(f"grant:{securable}:{principal}")


def _workspace(
    tmp: str, *, confirmed: bool, grants=(), existing_objects=None, storage_root=""
):
    root = Path(tmp)
    ws_rel = "workspaces/demo"
    ws = root / ws_rel
    (ws / "interns" / "generated" / "intake").mkdir(parents=True, exist_ok=True)
    (ws / "workspace_settings.json").write_text(
        json.dumps({"source_declaration": DECLARATION}), encoding="utf-8"
    )
    discovery = dict(DISCOVERY)
    if existing_objects:
        discovery["existing_objects"] = existing_objects
    (ws / "interns/generated/intake/discovery.json").write_text(
        json.dumps(discovery), encoding="utf-8"
    )
    if confirmed:
        marker = ws / "interns" / "reports" / "solution_blueprint"
        marker.mkdir(parents=True, exist_ok=True)
        (marker / "current.confirmed.json").write_text(
            json.dumps({"confirmed_by": "a human", "status": "confirmed"}), encoding="utf-8"
        )
    build_provision_plan(
        root, ws_rel, catalog="demo", env="dev", grant_principals=grants,
        storage_root=storage_root,
    )
    return root, ws_rel


def _log(root: Path, ws_rel: str) -> dict:
    return json.loads(
        (root / ws_rel / "interns/generated/evidence/provisioning/apply_log.json").read_text()
    )


class ConfirmationGateTests(unittest.TestCase):
    def test_without_confirmation_it_is_a_dry_run_and_creates_nothing(self):
        with tempfile.TemporaryDirectory() as tmp:
            root, ws = _workspace(tmp, confirmed=False)
            api = FakeApi()
            result = apply_provision_plan(root, ws, api=api)
            self.assertEqual(result.status, STATUS_REFUSED_NO_CONFIRMATION)
            self.assertTrue(result.dry_run)
            self.assertFalse(result.ok)
            self.assertEqual(api.calls, [])
            self.assertIn("apply-blueprint-answer", result.next_command)
            self.assertEqual(_log(root, ws)["counts"]["created"], 0)

    def test_explicit_no_dry_run_without_confirmation_refuses(self):
        with tempfile.TemporaryDirectory() as tmp:
            root, ws = _workspace(tmp, confirmed=False)
            api = FakeApi()
            result = apply_provision_plan(root, ws, dry_run=False, api=api)
            self.assertEqual(result.status, STATUS_REFUSED_NO_CONFIRMATION)
            self.assertFalse(result.ok)
            self.assertEqual(api.calls, [])
            self.assertTrue(all(s["status"] == "blocked" for s in result.steps))

    def test_confirmed_workspace_applies_by_default(self):
        with tempfile.TemporaryDirectory() as tmp:
            root, ws = _workspace(tmp, confirmed=True)
            api = FakeApi()
            result = apply_provision_plan(root, ws, api=api)
            self.assertEqual(result.status, STATUS_APPLIED)
            self.assertTrue(result.ok)
            self.assertFalse(result.dry_run)
            self.assertEqual(result.created, len(result.steps))
            self.assertEqual(
                api.calls,
                [
                    "external_location:demo_dev_root",
                    "catalog:demo_dev",
                    "schema:demo_dev.bronze",
                    "schema:demo_dev.silver",
                    "schema:demo_dev.gold",
                    "volume:demo_dev.bronze._checkpoints",
                ],
            )

    def test_confirmed_but_explicit_dry_run_creates_nothing(self):
        with tempfile.TemporaryDirectory() as tmp:
            root, ws = _workspace(tmp, confirmed=True)
            api = FakeApi()
            result = apply_provision_plan(root, ws, dry_run=True, api=api)
            self.assertEqual(result.status, STATUS_DRY_RUN)
            self.assertTrue(result.ok)
            self.assertEqual(api.calls, [])
            self.assertTrue(all(s["status"] == "would_create" for s in result.steps))


class IdempotencyTests(unittest.TestCase):
    def test_existing_objects_are_skipped_not_recreated(self):
        with tempfile.TemporaryDirectory() as tmp:
            root, ws = _workspace(tmp, confirmed=True)
            api = FakeApi(existing={"catalog:demo_dev", "schema:demo_dev.bronze"})
            result = apply_provision_plan(root, ws, api=api)
            self.assertEqual(result.status, STATUS_APPLIED)
            self.assertEqual(result.existing, 2)
            self.assertNotIn("catalog:demo_dev", api.calls)
            skipped = [s for s in result.steps if s["status"] == "existing"]
            self.assertTrue(all(s["detail"].startswith("[ok] existing") for s in skipped))

    def test_second_run_is_a_full_no_op(self):
        with tempfile.TemporaryDirectory() as tmp:
            root, ws = _workspace(tmp, confirmed=True)
            api = FakeApi()
            apply_provision_plan(root, ws, api=api)
            first = list(api.calls)
            second = apply_provision_plan(root, ws, api=api)
            self.assertEqual(api.calls, first)  # nothing created the second time
            self.assertEqual(second.created, 0)
            self.assertEqual(second.existing, len(second.steps))

    def test_blocked_destructive_steps_are_never_executed(self):
        with tempfile.TemporaryDirectory() as tmp:
            root, ws = _workspace(
                tmp, confirmed=True,
                existing_objects={"external_location:demo_dev_root": "s3://elsewhere/"},
            )
            api = FakeApi()
            result = apply_provision_plan(root, ws, api=api)
            self.assertEqual(result.blocked, 1)
            self.assertNotIn("external_location:demo_dev_root", api.calls)
            self.assertTrue(result.ok)  # the rest of the additive plan still lands


class ExternalLocationOverlapTests(unittest.TestCase):
    """F14: Unity Catalog keys external-location identity on URL prefix, not on
    name. A name-only check plans a create that UC rejects with
    'overlaps with an existing external location'."""

    def test_parent_path_covers_the_planned_url(self):
        found = covering_external_location(
            "s3://bkt/pfx/", {"platform_root": "s3://bkt/"}
        )
        self.assertEqual(found, "platform_root")

    def test_child_path_also_overlaps(self):
        # UC rejects in both directions, so a narrower existing location counts.
        found = covering_external_location(
            "s3://bkt/pfx/", {"narrow": "s3://bkt/pfx/deeper/"}
        )
        self.assertEqual(found, "narrow")

    def test_sibling_prefix_is_not_an_overlap(self):
        # 's3://bkt/pfx2/' must not match 's3://bkt/pfx/' on a raw startswith.
        found = covering_external_location(
            "s3://bkt/pfx/", {"sibling": "s3://bkt/pfx2/"}
        )
        self.assertEqual(found, "")

    def test_unrelated_bucket_is_not_an_overlap(self):
        found = covering_external_location(
            "s3://bkt/pfx/", {"other": "s3://different-bucket/"}
        )
        self.assertEqual(found, "")

    def test_existing_location_under_a_different_name_is_reused_not_recreated(self):
        with tempfile.TemporaryDirectory() as tmp:
            root, ws = _workspace(tmp, confirmed=True)
            # The bucket is already registered by a platform team, under a name
            # this workspace would never guess.
            api = FakeApi(locations={"healthcare_ext_loc": "s3://bkt/"})
            result = apply_provision_plan(root, ws, api=api)

            self.assertEqual(result.status, STATUS_APPLIED)
            self.assertTrue(result.ok)
            self.assertNotIn("external_location:demo_dev_root", api.calls)
            loc_steps = [
                s for s in result.steps if s["step_id"].startswith("create_external_location:")
            ]
            self.assertEqual(len(loc_steps), 1)
            self.assertEqual(loc_steps[0]["status"], "existing")
            self.assertIn("healthcare_ext_loc", loc_steps[0]["detail"])


class CatalogStorageRootTests(unittest.TestCase):
    """F15: a metastore with no storage root cannot create a catalog without an
    explicit MANAGED LOCATION. Where managed data physically lives is a
    residency decision, so it is an operator input recorded in the plan --
    never derived silently from the source location."""

    def test_storage_root_is_absent_by_default(self):
        # Metastores that DO have a root must keep working untouched.
        with tempfile.TemporaryDirectory() as tmp:
            root, ws = _workspace(tmp, confirmed=True)
            api = FakeApi()
            apply_provision_plan(root, ws, api=api)
            self.assertEqual(api.storage_roots["demo_dev"], "")

    def test_declared_storage_root_reaches_catalog_creation(self):
        with tempfile.TemporaryDirectory() as tmp:
            root, ws = _workspace(
                tmp, confirmed=True, storage_root="s3://bkt/"
            )
            api = FakeApi()
            result = apply_provision_plan(root, ws, api=api)
            self.assertEqual(result.status, STATUS_APPLIED)
            self.assertEqual(api.storage_roots["demo_dev"], "s3://bkt/")

    def test_storage_root_is_recorded_in_the_plan_for_review(self):
        with tempfile.TemporaryDirectory() as tmp:
            root, ws = _workspace(tmp, confirmed=True, storage_root="s3://bkt/")
            plan = json.loads(
                (Path(root) / ws / "interns/generated/contracts/provision_plan.json").read_text()
            )
            catalog_steps = [s for s in plan["steps"] if s["kind"] == "create_catalog"]
            self.assertEqual(len(catalog_steps), 1)
            self.assertEqual(catalog_steps[0]["params"]["storage_root"], "s3://bkt/")


class FailureTests(unittest.TestCase):
    def test_step_failure_stops_and_is_recorded(self):
        with tempfile.TemporaryDirectory() as tmp:
            root, ws = _workspace(tmp, confirmed=True)
            api = FakeApi(fail_on="catalog:")
            result = apply_provision_plan(root, ws, api=api)
            self.assertFalse(result.ok)
            self.assertEqual(result.failed, 1)
            self.assertEqual(len(result.steps), 2)  # stopped after the failure
            self.assertEqual(_log(root, ws)["status"], "failed")

    def test_databricks_unreachable_is_a_structured_failure(self):
        with tempfile.TemporaryDirectory() as tmp:
            root, ws = _workspace(tmp, confirmed=True)
            with mock.patch(
                "core.provisioning.apply._sdk_api",
                side_effect=ConnectionError("Databricks not configured"),
            ):
                result = apply_provision_plan(root, ws)
            self.assertEqual(result.status, STATUS_REFUSED_UNAVAILABLE)
            self.assertFalse(result.ok)
            self.assertIn("check-platform-readiness", result.next_command)
            self.assertIn("Nothing was created", result.detail)
            self.assertEqual(result.created, 0)

    def test_kill_switch_env_refuses_even_when_confirmed(self):
        with tempfile.TemporaryDirectory() as tmp:
            root, ws = _workspace(tmp, confirmed=True)
            api = FakeApi()
            with mock.patch.dict(
                "os.environ", {"AUTORESEARCH_ALLOW_REMOTE_EXECUTION": "0"}, clear=False
            ):
                result = apply_provision_plan(root, ws, api=api)
            self.assertEqual(result.status, STATUS_REFUSED_KILL_SWITCH)
            self.assertFalse(result.ok)
            self.assertEqual(api.calls, [])

    def test_missing_plan_refuses(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "workspaces" / "empty" / "interns").mkdir(parents=True)
            with self.assertRaises(FileNotFoundError):
                apply_provision_plan(root, "workspaces/empty", api=FakeApi())


class ApplyLogTests(unittest.TestCase):
    def test_log_shape_and_no_secret_values(self):
        with tempfile.TemporaryDirectory() as tmp:
            root, ws = _workspace(tmp, confirmed=True, grants=("readers",))
            result = apply_provision_plan(root, ws, api=FakeApi())
            log = _log(root, ws)
            self.assertEqual(log["artifact_type"], "evidence/provisioning/apply_log.json")
            self.assertTrue(log["additive_only"])
            self.assertTrue(log["confirmed_blueprint"])
            self.assertEqual(log["catalog"], "demo_dev")
            self.assertEqual(
                set(log["counts"]), {"created", "existing", "blocked", "failed"}
            )
            self.assertEqual(len(log["steps"]), len(result.steps))
            raw = json.dumps(log).lower()
            for word in ("password", "token", "secret_value", "akia"):
                self.assertNotIn(word, raw)


class StageRoutingTests(unittest.TestCase):
    def test_apply_summary_carries_the_provisioning_roster(self):
        with tempfile.TemporaryDirectory() as tmp:
            root, ws = _workspace(tmp, confirmed=True)
            summary = apply_provision_plan(root, ws, api=FakeApi()).summary()
            roster = routing_for("provisioning")
            self.assertTrue(roster["agents"], "provisioning routes no agent")
            self.assertEqual(summary["stage"], "provisioning")
            self.assertEqual(summary["required_specialists"], roster["agents"])
            self.assertEqual(summary["suggested_skills"], roster["skills"])


if __name__ == "__main__":
    unittest.main()
