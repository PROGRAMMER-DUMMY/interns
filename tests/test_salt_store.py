"""core.medallion.salt_store: the persistence half of the workspace PII salt.

`secrets.toml` is SHARED across every workspace on the box. The lookup path is
already exercised indirectly elsewhere; what is pinned here is the write path,
because a bad write does not fail the current workspace -- it silently destroys
OTHER workspaces' salts, and a lost salt makes every previously hashed PII value
unjoinable forever. Nothing here touches the real home directory or Databricks.
"""
from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest import mock

from core.medallion import salt_store


class MaterializeSaltCorruptStoreTest(unittest.TestCase):
    def test_refuses_rather_than_clobbering_a_corrupt_store(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            cfg = home / ".config" / "autoresearch" / "secrets.toml"
            cfg.parent.mkdir(parents=True)
            corrupt = '[workspaces.other]\nmedallion_salt = "keep-me\n'
            cfg.write_text(corrupt, encoding="utf-8")

            with mock.patch.object(
                salt_store, "get_workspace_salt",
                side_effect=salt_store.SaltMissing("none"),
            ), mock.patch.object(Path, "home", classmethod(lambda cls: home)):
                with self.assertRaises(RuntimeError) as ctx:
                    salt_store.materialize_salt_if_missing("demo")

            self.assertIn("unreadable", str(ctx.exception))
            # The other workspace's salt is still on disk, byte for byte.
            self.assertEqual(cfg.read_text(encoding="utf-8"), corrupt)

    def test_writes_alongside_an_existing_readable_store(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            cfg = home / ".config" / "autoresearch" / "secrets.toml"
            cfg.parent.mkdir(parents=True)
            cfg.write_text(
                '[workspaces.other]\nmedallion_salt = "keep-me"\n', encoding="utf-8"
            )

            with mock.patch.object(
                salt_store, "get_workspace_salt",
                side_effect=salt_store.SaltMissing("none"),
            ), mock.patch.object(Path, "home", classmethod(lambda cls: home)):
                new_salt = salt_store.materialize_salt_if_missing("demo")

            import toml

            data = toml.loads(cfg.read_text(encoding="utf-8"))
            self.assertEqual(data["workspaces"]["other"]["medallion_salt"], "keep-me")
            self.assertEqual(data["workspaces"]["demo"]["medallion_salt"], new_salt)


if __name__ == "__main__":
    unittest.main()
