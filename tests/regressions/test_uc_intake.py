"""Regression: creating Unity Catalog objects needs an approved plan and a gate.

Phase C. The platform used to assume tables already existed in Unity Catalog and
left "my data is in S3" as a dead end. Intake closes it by creating GOVERNANCE
objects only -- it moves no bytes.

The tests below pin the refusals, because the refusals are the feature: intake
creates catalogs and tables, which is exactly the class of action that must not
happen because an agent inferred it was wanted.

A fake `UnityCatalogApi` stands in for Databricks so the whole path is exercised
with no account, no credentials and no network.
"""
from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from core.onboarding import blueprint as bp
from core.onboarding.databricks import uc_intake as uci
from core.storage.workspace_layout import WorkspaceLayout

_AUTHORIZED = {"AUTORESEARCH_ALLOW_REMOTE_EXECUTION": "1"}
_ROLE = "arn:aws:iam::123456789012:role/uc-access"


class FakeUcApi:
    """Records calls; nothing exists until created."""

    def __init__(self, preexisting: set[str] | None = None) -> None:
        self.existing: set[str] = set(preexisting or ())
        self.calls: list[str] = []

    # -- existence ------------------------------------------------------
    def storage_credential_exists(self, name): return f"cred:{name}" in self.existing
    def external_location_exists(self, name):  return f"loc:{name}" in self.existing
    def catalog_exists(self, name):            return f"cat:{name}" in self.existing
    def schema_exists(self, c, s):             return f"schema:{c}.{s}" in self.existing
    def volume_exists(self, c, s, n):          return f"vol:{c}.{s}.{n}" in self.existing
    def table_exists(self, fqn):               return f"tbl:{fqn}" in self.existing

    # -- creation -------------------------------------------------------
    def create_storage_credential(self, name, role_arn):
        self.calls.append(f"create_cred:{name}:{role_arn}"); self.existing.add(f"cred:{name}")

    def create_external_location(self, name, url, credential):
        self.calls.append(f"create_loc:{name}:{url}"); self.existing.add(f"loc:{name}")

    def create_catalog(self, name):
        self.calls.append(f"create_cat:{name}"); self.existing.add(f"cat:{name}")

    def create_schema(self, c, s):
        self.calls.append(f"create_schema:{c}.{s}"); self.existing.add(f"schema:{c}.{s}")

    def create_external_volume(self, c, s, n, location):
        self.calls.append(f"create_vol:{c}.{s}.{n}:{location}"); self.existing.add(f"vol:{c}.{s}.{n}")

    def create_external_table(self, fqn, location):
        self.calls.append(f"create_tbl:{fqn}:{location}"); self.existing.add(f"tbl:{fqn}")


def _blueprint_payload(status: str = "approved") -> dict:
    return {
        "status": status,
        "source_root": "s3://bkt/",
        "catalog": "cat",
        "schemas": {"bronze": "bronze", "silver": "silver", "gold": "gold"},
        "approval": {"confirmed_by": "Shubham", "source": "human"} if status == "approved" else {},
        "groups": [
            {"group": "transactions", "disposition": bp.DISPOSITION_EXTERNAL_TABLE,
             "target": "cat.bronze.transactions"},
            {"group": "patients", "disposition": bp.DISPOSITION_MANAGED_TABLE,
             "target": "cat.bronze.patients"},
            {"group": "docs/KPI.xlsx", "disposition": bp.DISPOSITION_VOLUME,
             "target": "/Volumes/cat/bronze/docs"},
            {"group": "logs", "disposition": bp.DISPOSITION_EXCLUDED, "target": ""},
        ],
    }


def _workspace(tmp: str, status: str = "approved") -> Path:
    root = Path(tmp)
    layout = WorkspaceLayout(project_root=root / "workspaces" / "w")
    layout.ensure_runtime_dirs()
    out = layout.reports_dir / "solution_blueprint"
    out.mkdir(parents=True, exist_ok=True)
    (out / "current.json").write_text(json.dumps(_blueprint_payload(status)), encoding="utf-8")
    return root


class RefusalTests(unittest.TestCase):
    """Each refusal must fire BEFORE anything is created."""

    def test_no_blueprint_refuses(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            WorkspaceLayout(project_root=root / "workspaces" / "w").ensure_runtime_dirs()
            with self.assertRaises(FileNotFoundError):
                uci.run_intake(root, "workspaces/w")

    def test_an_unapproved_blueprint_refuses(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = _workspace(tmp, status="draft")
            with self.assertRaises(PermissionError) as ctx:
                uci.run_intake(root, "workspaces/w", role_arn=_ROLE, apply=True)
            self.assertIn("not approved", str(ctx.exception))

    def test_apply_without_the_remote_gate_refuses_and_creates_nothing(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = _workspace(tmp)
            api = FakeUcApi()
            with patch.dict(os.environ, {}, clear=False):
                os.environ.pop("AUTORESEARCH_ALLOW_REMOTE_EXECUTION", None)
                with self.assertRaises(PermissionError):
                    uci.run_intake(root, "workspaces/w", role_arn=_ROLE, apply=True, api=api)
            self.assertEqual(api.calls, [])

    def test_apply_without_a_role_arn_refuses(self):
        # There is no safe default for the IAM role UC assumes into your bucket.
        with tempfile.TemporaryDirectory() as tmp:
            root = _workspace(tmp)
            api = FakeUcApi()
            with patch.dict(os.environ, _AUTHORIZED):
                with self.assertRaises(ValueError) as ctx:
                    uci.run_intake(root, "workspaces/w", role_arn="", apply=True, api=api)
            self.assertIn("role-arn", str(ctx.exception))
            self.assertEqual(api.calls, [])


class DryRunTests(unittest.TestCase):
    def test_dry_run_is_the_default_and_creates_nothing(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = _workspace(tmp)
            api = FakeUcApi()
            with patch.dict(os.environ, _AUTHORIZED):
                result = uci.run_intake(root, "workspaces/w", role_arn=_ROLE, api=api)
            self.assertEqual(result.status, "dry_run")
            self.assertEqual(api.calls, [])
            md = (root / result.current_markdown_path).read_text(encoding="utf-8")
            self.assertIn("nothing was created", md.lower())

    def test_the_dry_run_plan_lists_every_object_it_would_create(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = _workspace(tmp)
            result = uci.run_intake(root, "workspaces/w", api=FakeUcApi())
            kinds = {op["kind"] for op in result.operations}
            self.assertEqual(
                kinds,
                {"storage_credential", "external_location", "catalog", "schema", "volume", "table"},
            )


class ApplyTests(unittest.TestCase):
    def _apply(self, root: Path, api: FakeUcApi):
        with patch.dict(os.environ, _AUTHORIZED):
            return uci.run_intake(root, "workspaces/w", role_arn=_ROLE, apply=True, api=api)

    def test_it_creates_the_full_chain_in_order(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = _workspace(tmp)
            api = FakeUcApi()
            result = self._apply(root, api)
            self.assertEqual(result.status, "applied")
            self.assertTrue(result.ok)
            # Credential before location before catalog before schemas.
            order = [c.split(":")[0] for c in api.calls]
            self.assertEqual(order[0], "create_cred")
            self.assertEqual(order[1], "create_loc")
            self.assertEqual(order[2], "create_cat")
            self.assertIn("create_schema", order)

    def test_an_external_table_is_registered_in_place(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = _workspace(tmp)
            api = FakeUcApi()
            self._apply(root, api)
            created = [c for c in api.calls if c.startswith("create_tbl:")]
            self.assertEqual(len(created), 1)
            self.assertIn("cat.bronze.transactions", created[0])
            self.assertIn("s3://bkt/transactions/", created[0])

    def test_a_managed_table_is_reported_not_executed(self):
        # Governance step moves no bytes; COPY INTO is its own decision.
        with tempfile.TemporaryDirectory() as tmp:
            root = _workspace(tmp)
            api = FakeUcApi()
            result = self._apply(root, api)
            self.assertFalse(any("cat.bronze.patients" in c for c in api.calls))
            patients = next(op for op in result.operations if op["name"] == "cat.bronze.patients")
            self.assertEqual(patients["status"], "requires_copy")
            md = (root / result.current_markdown_path).read_text(encoding="utf-8")
            self.assertIn("moves no bytes", md)

    def test_an_excluded_group_produces_no_operation(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = _workspace(tmp)
            result = uci.run_intake(root, "workspaces/w", api=FakeUcApi())
            self.assertFalse(any("logs" in op["name"] for op in result.operations))

    def test_rerunning_skips_what_already_exists(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = _workspace(tmp)
            api = FakeUcApi()
            self._apply(root, api)
            first = len(api.calls)
            result = self._apply(root, api)      # same api -> everything exists now
            self.assertEqual(len(api.calls), first, "idempotent re-run must create nothing")
            self.assertEqual(result.status, "applied")
            self.assertGreater(result.skipped, 0)
            self.assertEqual(result.created, 0)

    def test_a_failure_stops_the_chain_rather_than_cascading(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = _workspace(tmp)
            api = FakeUcApi()

            def boom(name, role_arn):
                raise RuntimeError("access denied")

            api.create_storage_credential = boom
            result = self._apply(root, api)
            self.assertEqual(result.status, "failed")
            self.assertFalse(result.ok)
            self.assertEqual(result.failed, 1)
            # Nothing after the failure was attempted.
            self.assertEqual(api.calls, [])


if __name__ == "__main__":
    unittest.main()
