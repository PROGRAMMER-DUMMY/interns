"""Source declaration: validated, persisted, and never carrying a secret value."""
from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from core.intake.declaration import (
    SETTINGS_KEY,
    SourceDeclaration,
    load_source_declaration,
    save_source_declaration,
)
from core.onboarding.workspace.delegation import routing_for
from core.storage.workspace_layout import WorkspaceLayout


class _TempWorkspace(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.repo_root = Path(self._tmp.name)
        self.workspace = self.repo_root / "workspaces" / "sample_ws"
        self.workspace.mkdir(parents=True)
        self.layout = WorkspaceLayout(project_root=self.workspace)
        self.addCleanup(self._tmp.cleanup)


class TestValidation(_TempWorkspace):
    def test_unknown_type_is_rejected(self):
        with self.assertRaises(ValueError) as ctx:
            SourceDeclaration(type="carrier_pigeon", location="somewhere").validate()
        self.assertIn("unknown source type", str(ctx.exception))

    def test_missing_location_is_rejected(self):
        with self.assertRaises(ValueError):
            SourceDeclaration(type="s3", location="  ").validate()

    def test_every_declared_type_validates_with_a_location(self):
        from core.intake.declaration import SOURCE_TYPES

        for source_type in SOURCE_TYPES:
            SourceDeclaration(type=source_type, location="anywhere").validate()

    def test_credential_reference_name_is_accepted(self):
        for ref in ("intake_scope/aws_key", "default-profile", "AWS_PROFILE_NAME", "uc_storage_cred"):
            SourceDeclaration(type="s3", location="s3://bucket/prefix", credential_ref=ref).validate()

    def test_secret_shaped_credential_is_refused_without_echoing_it(self):
        secrets = (
            "AKIAIOSFODNN7EXAMPLE",
            "dapi0123456789abcdef0123456789abcd",
            "sk-abcdefghijklmnopqrstuvwxyz012345",
            "eyJhbGciOiJIUzI1NiJ9.payload",
            "-----BEGIN RSA PRIVATE KEY-----",
            "postgres://user:hunter2@host:5432/db",
            "password=hunter2",
            "aGVsbG8gd29ybGQgdGhpcyBpcyBhIHNlY3JldCB2YWx1ZQ==",
        )
        for value in secrets:
            with self.subTest(value=value[:6]):
                with self.assertRaises(ValueError) as ctx:
                    SourceDeclaration(
                        type="s3", location="s3://bucket", credential_ref=value
                    ).validate()
                self.assertNotIn(value, str(ctx.exception))

    def test_reference_with_whitespace_is_refused(self):
        with self.assertRaises(ValueError):
            SourceDeclaration(
                type="jdbc", location="jdbc:postgresql://host/db", credential_ref="my profile"
            ).validate()


class TestOptionalFields(_TempWorkspace):
    """`schema_registry_url` and `one_shot` are optional; the registry URL is a
    URL, so it is validated as one -- and refused if it smuggles a credential."""

    def test_both_fields_default_to_empty_and_false(self):
        declaration = SourceDeclaration(type="kafka", location="broker:9092").validate()
        self.assertEqual("", declaration.schema_registry_url)
        self.assertIs(False, declaration.one_shot)

    def test_registry_url_round_trips(self):
        save_source_declaration(
            self.layout,
            SourceDeclaration(
                type="kafka",
                location="broker:9092",
                schema_registry_url="https://registry.internal:8081",
                one_shot=True,
            ),
        )
        loaded = load_source_declaration(self.layout)
        assert loaded is not None
        self.assertEqual("https://registry.internal:8081", loaded.schema_registry_url)
        self.assertIs(True, loaded.one_shot)

    def test_non_url_registry_is_refused(self):
        for value in ("registry.internal", "ftp://registry", "not a url"):
            with self.subTest(value=value):
                with self.assertRaises(ValueError):
                    SourceDeclaration(
                        type="kafka", location="broker:9092", schema_registry_url=value
                    ).validate()

    def test_registry_url_with_an_inline_credential_is_refused(self):
        value = "https://user:hunter2@registry.internal:8081"
        with self.assertRaises(ValueError) as ctx:
            SourceDeclaration(
                type="kafka", location="broker:9092", schema_registry_url=value
            ).validate()
        self.assertNotIn("hunter2", str(ctx.exception))

    def test_one_shot_from_settings_text_is_coerced(self):
        self.assertIs(
            True,
            SourceDeclaration.from_dict(
                {"type": "jdbc", "location": "jdbc:postgresql://host/db", "one_shot": "true"}
            ).one_shot,
        )
        self.assertIs(
            False,
            SourceDeclaration.from_dict(
                {"type": "jdbc", "location": "jdbc:postgresql://host/db"}
            ).one_shot,
        )


class TestPersistence(_TempWorkspace):
    def test_save_then_load_round_trips(self):
        declaration = SourceDeclaration(
            type="s3",
            location="s3://bucket/prefix",
            format_hint="parquet",
            credential_ref="intake_scope/aws_profile",
            declared_by="Reviewer",
        )
        summary = save_source_declaration(self.layout, declaration)
        self.assertEqual(summary["source_declaration"]["type"], "s3")

        loaded = load_source_declaration(self.layout)
        assert loaded is not None
        self.assertEqual(loaded.location, "s3://bucket/prefix")
        self.assertEqual(loaded.credential_ref, "intake_scope/aws_profile")
        self.assertTrue(loaded.declared_at)

    def test_declaration_lands_under_the_settings_key_and_keeps_other_keys(self):
        self.layout.state_dir.mkdir(parents=True, exist_ok=True)
        self.layout.workspace_settings.write_text(
            json.dumps({"output_dialect": "sql"}), encoding="utf-8"
        )
        save_source_declaration(
            self.layout, SourceDeclaration(type="uc_existing", location="cat.sch")
        )
        settings = json.loads(self.layout.workspace_settings.read_text(encoding="utf-8"))
        self.assertEqual(settings["output_dialect"], "sql")
        self.assertEqual(settings[SETTINGS_KEY]["type"], "uc_existing")

    def test_saving_an_invalid_declaration_writes_nothing(self):
        with self.assertRaises(ValueError):
            save_source_declaration(self.layout, SourceDeclaration(type="s3", location=""))
        self.assertFalse(self.layout.workspace_settings.exists())

    def test_undeclared_workspace_loads_as_none(self):
        self.assertIsNone(load_source_declaration(self.layout))


class TestStageRouting(_TempWorkspace):
    def test_declare_source_result_carries_the_source_declaration_roster(self):
        result = save_source_declaration(
            self.layout, SourceDeclaration(type="s3", location="s3://bkt/pfx")
        )
        roster = routing_for("source_declaration")
        self.assertEqual(result["stage"], "source_declaration")
        self.assertTrue(roster["agents"], "source_declaration routes no agent")
        self.assertEqual(result["required_specialists"], roster["agents"])
        self.assertEqual(result["suggested_skills"], roster["skills"])


if __name__ == "__main__":
    unittest.main()
