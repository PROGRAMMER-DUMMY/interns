"""Structured metadata storage backends.

Executable artifacts stay as files under ``workspace/interns``. Structured JSON
state is written to a pluggable metadata store so local and enterprise deployments
can query contracts, mappings, profiles, decisions, and run evidence consistently.
"""
from __future__ import annotations

import json
import os
import re
from abc import ABC, abstractmethod
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


@dataclass(frozen=True)
class MetadataWriteResult:
    backend: str
    collection: str
    document_id: str
    path: str | None = None
    mirrored: bool = False
    warning: str | None = None


class MetadataStore(ABC):
    backend_name = "metadata"

    @abstractmethod
    def upsert(
        self,
        collection: str,
        document_id: str,
        payload: dict[str, Any],
        *,
        workspace: str,
    ) -> MetadataWriteResult:
        """Write one structured document."""


class LocalMetadataStore(MetadataStore):
    backend_name = "local"

    def __init__(self, root: str | Path):
        self.root = Path(root)

    def upsert(
        self,
        collection: str,
        document_id: str,
        payload: dict[str, Any],
        *,
        workspace: str,
    ) -> MetadataWriteResult:
        path = self.root / _safe_name(collection) / f"{_safe_name(document_id)}.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps(_envelope(workspace, collection, document_id, payload), indent=2, default=str)
            + "\n",
            encoding="utf-8",
        )
        return MetadataWriteResult(
            backend=self.backend_name,
            collection=collection,
            document_id=document_id,
            path=str(path),
        )


class DeltaMetadataStore(MetadataStore):
    backend_name = "delta"

    def __init__(self, root: str | Path, *, local_fallback: LocalMetadataStore | None = None):
        self.root = Path(root)
        self.local_fallback = local_fallback

    def upsert(
        self,
        collection: str,
        document_id: str,
        payload: dict[str, Any],
        *,
        workspace: str,
    ) -> MetadataWriteResult:
        document = _envelope(workspace, collection, document_id, payload)
        table_path = self.root / _safe_name(collection)
        row = {
            "workspace": document["workspace"],
            "collection": document["collection"],
            "document_id": document["document_id"],
            "updated_at": document["updated_at"],
            "payload_json": json.dumps(payload, default=str),
        }
        try:
            table_path.mkdir(parents=True, exist_ok=True)
            _write_delta_row(table_path, row)
            return MetadataWriteResult(
                backend=self.backend_name,
                collection=collection,
                document_id=document_id,
                path=str(table_path),
                mirrored=True,
            )
        except Exception as exc:
            if self.local_fallback:
                local = self.local_fallback.upsert(
                    collection,
                    document_id,
                    payload,
                    workspace=workspace,
                )
                return MetadataWriteResult(
                    backend=f"{self.backend_name}->{local.backend}",
                    collection=collection,
                    document_id=document_id,
                    path=local.path,
                    mirrored=False,
                    warning=f"delta_write_failed:{type(exc).__name__}:{exc}",
                )
            return MetadataWriteResult(
                backend=self.backend_name,
                collection=collection,
                document_id=document_id,
                path=str(table_path),
                mirrored=False,
                warning=f"delta_write_failed:{type(exc).__name__}:{exc}",
            )


class MongoMetadataStore(MetadataStore):
    backend_name = "mongo"

    def __init__(
        self,
        uri: str,
        *,
        database: str = "autoresearch",
        local_fallback: LocalMetadataStore | None = None,
    ):
        self.uri = uri
        self.database = database
        self.local_fallback = local_fallback
        self._client = None

    def upsert(
        self,
        collection: str,
        document_id: str,
        payload: dict[str, Any],
        *,
        workspace: str,
    ) -> MetadataWriteResult:
        document = _envelope(workspace, collection, document_id, payload)
        try:
            db = self._database()
            db[_safe_name(collection)].replace_one(
                {"_id": document_id, "workspace": workspace},
                {"_id": document_id, **document},
                upsert=True,
            )
            return MetadataWriteResult(
                backend=self.backend_name,
                collection=collection,
                document_id=document_id,
                mirrored=True,
            )
        except Exception as exc:
            if self.local_fallback:
                local = self.local_fallback.upsert(
                    collection,
                    document_id,
                    payload,
                    workspace=workspace,
                )
                return MetadataWriteResult(
                    backend=f"{self.backend_name}->{local.backend}",
                    collection=collection,
                    document_id=document_id,
                    path=local.path,
                    mirrored=False,
                    warning=f"mongo_write_failed:{type(exc).__name__}:{exc}",
                )
            return MetadataWriteResult(
                backend=self.backend_name,
                collection=collection,
                document_id=document_id,
                mirrored=False,
                warning=f"mongo_write_failed:{type(exc).__name__}:{exc}",
            )

    def _database(self):
        if self._client is None:
            try:
                from pymongo import MongoClient
            except ImportError as exc:  # pragma: no cover - optional dependency
                raise RuntimeError("pymongo is required for MongoMetadataStore") from exc
            self._client = MongoClient(self.uri, serverSelectionTimeoutMS=3000)
            self._client.admin.command("ping")
        return self._client[self.database]


def build_metadata_store(layout, *, repo_root: str | Path | None = None) -> MetadataStore:
    """Build the configured metadata store.

    Selection precedence (most specific first):

    1. ``AUTORESEARCH_METADATA_BACKEND`` env var (``local``, ``delta``, ``mongo``)
       — explicit override for tests/CI/ops.
    2. ``workspace_settings.json`` keys ``delta_enabled`` (bool) or
       ``metadata_backend`` (string) — per-workspace opt-in.
    3. Default: ``local`` (file-per-document JSON, no Delta-table scaffolding).
       The Delta backend would otherwise emit hundreds of KB of Delta-log +
       parquet snapshots per workspace, which is pure overhead for any
       workspace that does NOT target Databricks/Spark downstream.

    Environment variables:
    - ``AUTORESEARCH_METADATA_BACKEND``: explicit override of all settings.
    - ``AUTORESEARCH_MONGO_URI`` / ``AUTORESEARCH_MONGO_DB``: Mongo backend config.
    """
    local_root = layout.state_dir / "metadata_store"
    local_store = LocalMetadataStore(local_root)

    env_backend = os.environ.get("AUTORESEARCH_METADATA_BACKEND", "").strip().lower()
    settings: dict[str, Any] = {}
    try:
        settings = layout.load_settings() or {}
    except Exception:
        settings = {}
    workspace_backend = str(settings.get("metadata_backend") or "").strip().lower()
    delta_enabled = bool(settings.get("delta_enabled", False))

    if env_backend:
        backend = env_backend
    elif workspace_backend:
        backend = workspace_backend
    elif delta_enabled:
        backend = "delta"
    else:
        backend = "local"

    if backend == "local":
        return local_store
    if backend == "delta":
        return DeltaMetadataStore(layout.state_dir / "delta_metadata", local_fallback=local_store)
    if backend != "mongo":
        # Fail loud on an unknown backend string rather than silently becoming
        # Delta (the old dead branch). Ref: core-audit storage.md.
        raise ValueError(
            f"unknown metadata backend {backend!r}; expected local | delta | mongo"
        )
    uri = os.environ.get("AUTORESEARCH_MONGO_URI", "").strip()
    if not uri:
        return DeltaMetadataStore(layout.state_dir / "delta_metadata", local_fallback=local_store)
    database = os.environ.get("AUTORESEARCH_MONGO_DB", "autoresearch").strip() or "autoresearch"
    return MongoMetadataStore(uri, database=database, local_fallback=local_store)


def _envelope(
    workspace: str,
    collection: str,
    document_id: str,
    payload: dict[str, Any],
) -> dict[str, Any]:
    return {
        "workspace": workspace,
        "collection": collection,
        "document_id": document_id,
        "updated_at": datetime.now(timezone.utc).isoformat(),
        "payload": payload,
    }


def _safe_name(value: str) -> str:
    cleaned = re.sub(r"[^A-Za-z0-9_.-]+", "_", value).strip("_")
    return cleaned or "document"


def _write_delta_row(table_path: Path, row: dict[str, str]) -> None:
    try:
        import pyarrow as pa
        from deltalake import DeltaTable, write_deltalake
    except ImportError as exc:  # pragma: no cover - dependency is expected in project env
        raise RuntimeError("deltalake and pyarrow are required for DeltaMetadataStore") from exc

    table = pa.Table.from_pylist(
        [row],
        schema=pa.schema([
            ("workspace", pa.string()),
            ("collection", pa.string()),
            ("document_id", pa.string()),
            ("updated_at", pa.string()),
            ("payload_json", pa.string()),
        ]),
    )
    if (table_path / "_delta_log").exists():
        # Real upsert: replace the existing (workspace, document_id) row
        # instead of blind-appending a duplicate ("append" mode previously
        # made every upsert() call an insert, so re-upserting the same
        # document_id left stale rows behind for readers to trip over).
        # `collection` is constant within one table (one Delta table per
        # collection, per the caller), so it is not part of the merge key.
        DeltaTable(str(table_path)).merge(
            table,
            predicate=(
                "target.workspace = source.workspace "
                "AND target.document_id = source.document_id"
            ),
            source_alias="source",
            target_alias="target",
        ).when_matched_update_all().when_not_matched_insert_all().execute()
    else:
        write_deltalake(str(table_path), table, mode="overwrite")
