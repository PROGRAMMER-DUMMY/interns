from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path

from core.storage.metadata_store import (
    DeltaMetadataStore,
    LocalMetadataStore,
    MongoMetadataStore,
    build_metadata_store,
)
from core.storage.workspace_layout import WorkspaceLayout


class MetadataStoreTests(unittest.TestCase):
    def test_local_metadata_store_writes_structured_document(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = LocalMetadataStore(Path(tmp) / "metadata")
            result = store.upsert(
                "contracts",
                "semantic_contract",
                {"kpi_count": 2},
                workspace="workspaces/demo",
            )
            self.assertEqual(result.backend, "local")
            path = Path(result.path)
            self.assertTrue(path.exists())
            payload = json.loads(path.read_text(encoding="utf-8"))
            self.assertEqual(payload["workspace"], "workspaces/demo")
            self.assertEqual(payload["payload"]["kpi_count"], 2)

    def test_delta_metadata_store_writes_structured_table(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = DeltaMetadataStore(Path(tmp) / "delta")
            result = store.upsert(
                "contracts",
                "semantic_contract",
                {"kpi_count": 2},
                workspace="workspaces/demo",
            )
            self.assertEqual(result.backend, "delta")
            table_path = Path(result.path)
            self.assertTrue((table_path / "_delta_log").exists())

            from deltalake import DeltaTable

            rows = DeltaTable(str(table_path)).to_pyarrow_table().to_pylist()
            self.assertEqual(rows[0]["workspace"], "workspaces/demo")
            self.assertEqual(rows[0]["document_id"], "semantic_contract")
            self.assertIn('"kpi_count": 2', rows[0]["payload_json"])

    def test_mongo_metadata_store_falls_back_to_local_when_unavailable(self):
        with tempfile.TemporaryDirectory() as tmp:
            fallback = LocalMetadataStore(Path(tmp) / "metadata")
            store = MongoMetadataStore("mongodb://127.0.0.1:1", local_fallback=fallback)
            result = store.upsert(
                "contracts",
                "semantic_contract",
                {"kpi_count": 2},
                workspace="workspaces/demo",
            )
            self.assertIn("mongo->local", result.backend)
            self.assertIsNotNone(result.warning)
            self.assertTrue(Path(result.path).exists())

    def test_build_metadata_store_uses_local_without_mongo_uri(self):
        with tempfile.TemporaryDirectory() as tmp:
            old_backend = os.environ.get("AUTORESEARCH_METADATA_BACKEND")
            old_uri = os.environ.get("AUTORESEARCH_MONGO_URI")
            try:
                os.environ["AUTORESEARCH_METADATA_BACKEND"] = "mongo"
                os.environ.pop("AUTORESEARCH_MONGO_URI", None)
                layout = WorkspaceLayout(project_root=Path(tmp) / "workspaces" / "demo")
                layout.ensure_runtime_dirs()
                self.assertIsInstance(build_metadata_store(layout), DeltaMetadataStore)
            finally:
                if old_backend is None:
                    os.environ.pop("AUTORESEARCH_METADATA_BACKEND", None)
                else:
                    os.environ["AUTORESEARCH_METADATA_BACKEND"] = old_backend
                if old_uri is None:
                    os.environ.pop("AUTORESEARCH_MONGO_URI", None)
                else:
                    os.environ["AUTORESEARCH_MONGO_URI"] = old_uri

    def test_build_metadata_store_defaults_to_delta(self):
        with tempfile.TemporaryDirectory() as tmp:
            old_backend = os.environ.get("AUTORESEARCH_METADATA_BACKEND")
            try:
                os.environ.pop("AUTORESEARCH_METADATA_BACKEND", None)
                layout = WorkspaceLayout(project_root=Path(tmp) / "workspaces" / "demo")
                layout.ensure_runtime_dirs()
                self.assertIsInstance(build_metadata_store(layout), DeltaMetadataStore)
            finally:
                if old_backend is None:
                    os.environ.pop("AUTORESEARCH_METADATA_BACKEND", None)
                else:
                    os.environ["AUTORESEARCH_METADATA_BACKEND"] = old_backend


if __name__ == "__main__":
    unittest.main()
