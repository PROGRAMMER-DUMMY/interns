"""Fetch a declared source's DOCUMENTS into the workspace.

Discovery already sees the documents sitting beside the data -- a KPI workbook,
a data-model diagram, a data dictionary -- and the KPI/data-model paths already
know how to read them. What was missing was the step in between: a way to get a
document OUT of object storage and into the workspace, without asking the
operator for a second set of cloud credentials on their laptop.

Found by a live replay: a workspace's KPIs sat in `docs/Sample_KPI.xlsx` in the
source bucket for the whole run. Discovery listed it, nothing could read it, so
the KPI registry stayed empty, `join_complexity` stayed unmeasurable, the engine
decision blocked, and `confirm-blueprint` refused a plan it could not complete.

The transport is the same one discovery uses: a SQL warehouse the account
already has, reading through the Unity Catalog external location that already
holds the credential. `read_files(..., format => 'binaryFile')` returns the
bytes; `base64(...)` carries them through a SQL result set intact.

Deliberately bounded: documents are small by nature (a workbook, a diagram, a
PDF), and a SQL result cell is not a file transfer protocol. Anything above
``MAX_DOCUMENT_BYTES`` is reported and skipped rather than silently truncated --
half a spreadsheet parses as a whole one and is worse than no spreadsheet.
"""
from __future__ import annotations

import base64
import binascii
import hashlib
import json
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from core.intake.declaration import load_source_declaration
from core.observability.log_redaction import redact
from core.storage.workspace_layout import WorkspaceLayout

# A SQL result cell carries the whole document base64-encoded (~4/3 the raw
# size). Well under a warehouse row limit for real documents; a refusal above it
# is honest, a truncation is not.
MAX_DOCUMENT_BYTES = 8 * 1024 * 1024

# Suffixes worth fetching: things a human wrote FOR this workspace. Data files
# are discovery's job, not this one.
DOCUMENT_SUFFIXES = (
    ".xlsx", ".xls", ".csv_dict", ".pdf", ".docx", ".doc",
    ".md", ".txt", ".png", ".jpg", ".jpeg", ".json_schema",
)

_OBJECT_STORE_CONNECTORS = frozenset({"s3", "adls", "gcs"})


@dataclass(frozen=True)
class FetchedDocument:
    name: str
    source_uri: str
    local_path: str
    size_bytes: int
    sha256: str

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class DocumentFetchResult:
    status: str  # ok | nothing_to_fetch | unsupported_connector | blocked
    connector: str
    source: str
    documents: list[FetchedDocument] = field(default_factory=list)
    skipped: list[dict[str, Any]] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    ok: bool = True

    def summary(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "connector": self.connector,
            "source": self.source,
            "document_count": len(self.documents),
            "documents": [d.to_dict() for d in self.documents],
            "skipped": self.skipped,
            "notes": self.notes,
            "ok": self.ok,
        }


def documents_root(declaration_location: str, override: str = "") -> str:
    """Where a source's documents live.

    Convention: a sibling `docs/` of the declared data prefix, because that is
    how the estates seen so far are laid out (`<bucket>/datasets/`,
    `<bucket>/docs/`). An explicit override wins, so a different layout needs a
    flag rather than a code change.
    """
    if override:
        return override if override.endswith("/") else override + "/"
    base = declaration_location.rstrip("/")
    if base.endswith("/datasets"):
        base = base[: -len("/datasets")]
    return f"{base}/docs/"


def _is_document(name: str) -> bool:
    lowered = name.lower()
    return any(lowered.endswith(suffix) for suffix in DOCUMENT_SUFFIXES)


def fetch_documents(
    repo_root: str | Path,
    workspace: str | Path,
    *,
    docs_uri: str = "",
    gateway: Any = None,
    max_bytes: int = MAX_DOCUMENT_BYTES,
) -> DocumentFetchResult:
    """Copy a declared source's documents into ``<workspace>/docs/``.

    ``gateway`` is any object exposing ``list_uri(uri)`` and
    ``fetch_base64(uri)``; injected so tests never touch a warehouse.
    """
    repo_root = Path(repo_root).resolve()
    ws_path = (repo_root / workspace).resolve()
    layout = WorkspaceLayout(project_root=ws_path)

    declaration = load_source_declaration(layout)
    if declaration is None:
        return DocumentFetchResult(
            status="blocked", connector="", source="", ok=False,
            notes=["no source declared; run `declare-source` first"],
        )
    connector = str(getattr(declaration, "type", "") or "")
    location = str(getattr(declaration, "location", "") or "")
    if connector not in _OBJECT_STORE_CONNECTORS:
        return DocumentFetchResult(
            status="unsupported_connector", connector=connector, source=location, ok=True,
            notes=[
                f"connector `{connector}` has no document transport; documents are "
                "fetched only from object storage (s3/adls/gcs). Place them under "
                "the workspace's own docs/ folder instead."
            ],
        )

    root = documents_root(location, docs_uri)
    if gateway is None:
        from core.intake.discovery import _open_unity_catalog_gateway  # local import: optional dep

        gateway, why = _open_unity_catalog_gateway(ws_path)
        if gateway is None:
            return DocumentFetchResult(
                status="blocked", connector=connector, source=root, ok=False,
                notes=[
                    why or f"{root} is not reachable through Unity Catalog",
                    "Register the location as a Unity Catalog external location "
                    "(Databricks then holds the credential), or place the documents "
                    "under the workspace's own docs/ folder.",
                ],
            )

    try:
        rows = gateway.list_uri(root)
    except Exception as exc:  # noqa: BLE001 - a listing failure must not crash intake
        return DocumentFetchResult(
            status="blocked", connector=connector, source=root, ok=False,
            notes=[f"listing {root} failed: {redact(str(exc))}"],
        )

    out_dir = ws_path / "docs"
    fetched: list[FetchedDocument] = []
    skipped: list[dict[str, Any]] = []
    notes: list[str] = [f"documents read through Unity Catalog from {root}"]

    for row in rows or []:
        uri, name, size = _row_fields(row)
        if not name or name.endswith("/"):
            continue
        if not _is_document(name):
            skipped.append({"name": name, "reason": "not a document suffix"})
            continue
        if size and size > max_bytes:
            # Truncating a workbook produces a file that parses as whole and is
            # wrong; refusing names the problem instead.
            skipped.append({
                "name": name,
                "reason": f"{size} bytes exceeds the {max_bytes}-byte transport limit",
            })
            continue
        try:
            payload = gateway.fetch_base64(uri)
        except Exception as exc:  # noqa: BLE001
            skipped.append({"name": name, "reason": f"read failed: {redact(str(exc))}"})
            continue
        if not payload:
            skipped.append({"name": name, "reason": "empty response"})
            continue
        try:
            raw = base64.b64decode(payload)
        except (binascii.Error, ValueError) as exc:
            skipped.append({"name": name, "reason": f"undecodable payload: {exc}"})
            continue
        if size and len(raw) != size:
            # A short read is a silent corruption; say so rather than store it.
            skipped.append({
                "name": name,
                "reason": f"recovered {len(raw)} bytes, expected {size} -- refusing a partial file",
            })
            continue
        out_dir.mkdir(parents=True, exist_ok=True)
        target = out_dir / name
        target.write_bytes(raw)
        fetched.append(
            FetchedDocument(
                name=name,
                source_uri=uri,
                local_path=target.relative_to(repo_root).as_posix(),
                size_bytes=len(raw),
                sha256=hashlib.sha256(raw).hexdigest(),
            )
        )

    status = "ok" if fetched else "nothing_to_fetch"
    if not fetched:
        notes.append(f"no documents matched {DOCUMENT_SUFFIXES} under {root}")
    result = DocumentFetchResult(
        status=status, connector=connector, source=root,
        documents=fetched, skipped=skipped, notes=notes, ok=True,
    )
    _write_manifest(layout, repo_root, result)
    return result


def _row_fields(row: Any) -> tuple[str, str, int]:
    """(uri, name, size) from a LIST row, defensively."""
    try:
        uri = str(row[0] or "")
        name = str(row[1] or "").rstrip("/")
        size = int(row[2] or 0)
    except (IndexError, TypeError, ValueError):
        return "", "", 0
    return uri, name, size


def _write_manifest(layout: WorkspaceLayout, repo_root: Path, result: DocumentFetchResult) -> None:
    out = layout.generated_dir / "intake" / "documents.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "artifact_type": "intake/documents.json",
        "version": 1,
        "generated_by": "fetch-source-documents",
        "generated_at": datetime.now(timezone.utc).isoformat(),
        **result.summary(),
    }
    out.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")


__all__ = [
    "DOCUMENT_SUFFIXES",
    "MAX_DOCUMENT_BYTES",
    "DocumentFetchResult",
    "FetchedDocument",
    "documents_root",
    "fetch_documents",
]
