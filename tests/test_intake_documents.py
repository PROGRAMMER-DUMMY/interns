"""Fetching a source's documents into the workspace.

Found by a live replay: a workspace's KPIs sat in `docs/Sample_KPI.xlsx` inside
the source bucket for an entire run. Discovery listed the file, nothing could
read it, so the KPI registry stayed empty, `join_complexity` was unmeasurable,
the engine decision blocked, and `confirm-blueprint` refused a plan it could
never complete. The bytes were reachable the whole time through the same Unity
Catalog credential discovery already used.
"""
from __future__ import annotations

import base64
import json
import tempfile
import unittest
from pathlib import Path

from core.intake import documents
from core.intake.declaration import SourceDeclaration, save_source_declaration
from core.storage.workspace_layout import WorkspaceLayout


class FakeGateway:
    """Stands in for the SQL warehouse; no network, no credentials."""

    def __init__(self, listing, payloads=None, list_error=None, fetch_error=None):
        self._listing = listing
        self._payloads = payloads or {}
        self._list_error = list_error
        self._fetch_error = fetch_error
        self.fetched: list[str] = []

    def list_uri(self, uri):
        if self._list_error:
            raise RuntimeError(self._list_error)
        return self._listing

    def fetch_base64(self, uri):
        self.fetched.append(uri)
        if self._fetch_error:
            raise RuntimeError(self._fetch_error)
        return self._payloads.get(uri, "")


def _workspace(tmp: str, *, connector: str = "s3", location: str = "s3://bucket/datasets/"):
    root = Path(tmp)
    ws = root / "workspaces" / "demo"
    (ws / "interns").mkdir(parents=True)
    layout = WorkspaceLayout(project_root=ws)
    save_source_declaration(
        layout,
        SourceDeclaration(
            type=connector, location=location, format_hint="csv",
            credential_ref="cred_name", declared_by="tester",
        ),
    )
    return root, "workspaces/demo"


def _row(uri: str, name: str, size: int):
    return [uri, name, str(size), "1700000000000"]


class DocumentsRootTests(unittest.TestCase):
    def test_docs_is_a_sibling_of_the_data_prefix(self):
        self.assertEqual(
            documents.documents_root("s3://bucket/datasets/"), "s3://bucket/docs/"
        )

    def test_an_override_wins(self):
        self.assertEqual(
            documents.documents_root("s3://bucket/datasets/", "s3://bucket/kpi/"),
            "s3://bucket/kpi/",
        )


class FetchTests(unittest.TestCase):
    def test_documents_land_in_the_workspace_with_bytes_intact(self):
        raw = b"PK\x03\x04 fake workbook bytes"
        uri = "s3://bucket/docs/kpi.xlsx"
        with tempfile.TemporaryDirectory() as tmp:
            root, ws = _workspace(tmp)
            gw = FakeGateway(
                [_row(uri, "kpi.xlsx", len(raw))],
                {uri: base64.b64encode(raw).decode()},
            )
            result = documents.fetch_documents(root, ws, gateway=gw)
            self.assertEqual(result.status, "ok")
            self.assertEqual(len(result.documents), 1)
            landed = root / result.documents[0].local_path
            self.assertEqual(landed.read_bytes(), raw)

    def test_non_document_files_are_skipped(self):
        uri = "s3://bucket/docs/part-0.parquet"
        with tempfile.TemporaryDirectory() as tmp:
            root, ws = _workspace(tmp)
            gw = FakeGateway([_row(uri, "part-0.parquet", 10)])
            result = documents.fetch_documents(root, ws, gateway=gw)
            self.assertEqual(result.status, "nothing_to_fetch")
            self.assertEqual(gw.fetched, [], "a non-document must not be transferred")

    def test_a_short_read_is_refused_not_stored(self):
        """A truncated workbook parses as a whole one -- worse than none."""
        raw = b"only-part"
        uri = "s3://bucket/docs/kpi.xlsx"
        with tempfile.TemporaryDirectory() as tmp:
            root, ws = _workspace(tmp)
            gw = FakeGateway(
                [_row(uri, "kpi.xlsx", 999)],  # declared size != recovered size
                {uri: base64.b64encode(raw).decode()},
            )
            result = documents.fetch_documents(root, ws, gateway=gw)
            self.assertEqual(result.documents, [])
            self.assertIn("refusing a partial file", result.skipped[0]["reason"])

    def test_oversized_document_is_reported_not_truncated(self):
        uri = "s3://bucket/docs/huge.pdf"
        with tempfile.TemporaryDirectory() as tmp:
            root, ws = _workspace(tmp)
            gw = FakeGateway([_row(uri, "huge.pdf", documents.MAX_DOCUMENT_BYTES + 1)])
            result = documents.fetch_documents(root, ws, gateway=gw)
            self.assertEqual(result.documents, [])
            self.assertIn("exceeds", result.skipped[0]["reason"])
            self.assertEqual(gw.fetched, [])

    def test_listing_failure_is_structured_not_raised(self):
        with tempfile.TemporaryDirectory() as tmp:
            root, ws = _workspace(tmp)
            gw = FakeGateway([], list_error="warehouse unreachable")
            result = documents.fetch_documents(root, ws, gateway=gw)
            self.assertFalse(result.ok)
            self.assertEqual(result.status, "blocked")

    def test_read_failure_skips_one_document_not_the_run(self):
        good, bad = "s3://bucket/docs/a.md", "s3://bucket/docs/b.md"
        with tempfile.TemporaryDirectory() as tmp:
            root, ws = _workspace(tmp)
            gw = FakeGateway(
                [_row(bad, "b.md", 4), _row(good, "a.md", 4)],
                {good: base64.b64encode(b"data").decode()},
            )
            result = documents.fetch_documents(root, ws, gateway=gw)
            self.assertEqual([d.name for d in result.documents], ["a.md"])
            self.assertTrue(result.skipped)

    def test_non_object_store_connector_is_reported_not_attempted(self):
        with tempfile.TemporaryDirectory() as tmp:
            root, ws = _workspace(tmp, connector="jdbc", location="jdbc:postgresql://h/db")
            result = documents.fetch_documents(root, ws, gateway=FakeGateway([]))
            self.assertEqual(result.status, "unsupported_connector")
            self.assertTrue(result.ok, "an unsupported connector is not a failure")

    def test_manifest_is_written(self):
        raw = b"# notes"
        uri = "s3://bucket/docs/readme.md"
        with tempfile.TemporaryDirectory() as tmp:
            root, ws = _workspace(tmp)
            gw = FakeGateway(
                [_row(uri, "readme.md", len(raw))], {uri: base64.b64encode(raw).decode()}
            )
            documents.fetch_documents(root, ws, gateway=gw)
            manifest = root / ws / "interns" / "generated" / "intake" / "documents.json"
            payload = json.loads(manifest.read_text(encoding="utf-8"))
            self.assertEqual(payload["artifact_type"], "intake/documents.json")
            self.assertEqual(payload["document_count"], 1)


class UriSafetyTests(unittest.TestCase):
    def test_a_quote_in_a_uri_is_refused(self):
        """The path is interpolated into SQL; a quote is a statement break."""
        from core.intake.discovery import _assert_safe_uri

        # Embedded, not trailing: a trailing newline is stripped harmlessly and
        # leaves a clean URI, so only an EMBEDDED one can break the statement.
        for hostile in ("s3://b/x'.csv", "s3://b/x\nDROP", "s3://b/x\rDROP"):
            with self.assertRaises(ValueError):
                _assert_safe_uri(hostile)

    def test_a_normal_uri_passes(self):
        from core.intake.discovery import _assert_safe_uri

        self.assertEqual(
            _assert_safe_uri("s3://bucket/docs/Sample_KPI (1).xlsx"),
            "s3://bucket/docs/Sample_KPI (1).xlsx",
        )


if __name__ == "__main__":
    unittest.main()
