"""Tests for bounded internal-doc retrieval (core.context.doc_retrieval)."""
from __future__ import annotations

import unittest

from core.context.doc_retrieval import build_doc_manifest, retrieve_docs
from core.paths import PROJECT_ROOT

REPO = PROJECT_ROOT
SCHEMA_DOC = "docs/agents/data processing/schema_types_identification_guide.md"
TOKEN_DOC = "docs/reference/databricks_token_scopes.md"


class BuildDocManifestTests(unittest.TestCase):
    def test_manifest_includes_data_processing_and_reference_docs(self) -> None:
        manifest = build_doc_manifest(REPO)
        paths = {entry["path"] for entry in manifest}
        self.assertIn(SCHEMA_DOC, paths)
        self.assertIn(TOKEN_DOC, paths)
        # index.md files are excluded from the manifest.
        self.assertFalse(any(p.endswith("index.md") for p in paths))

    def test_manifest_entries_have_required_fields(self) -> None:
        manifest = build_doc_manifest(REPO)
        self.assertTrue(manifest)
        for entry in manifest:
            self.assertEqual(
                set(entry), {"path", "title", "summary", "keywords"}
            )
            self.assertIsInstance(entry["keywords"], list)

    def test_schema_doc_keywords_enriched_from_headings_and_index(self) -> None:
        manifest = {e["path"]: e for e in build_doc_manifest(REPO)}
        kws = set(manifest[SCHEMA_DOC]["keywords"])
        # From section headings.
        self.assertIn("snowflake", kws)
        self.assertIn("star", kws)
        # From the index.md "what it is" description / decision framework.
        self.assertIn("schema", kws)

    def test_missing_docs_tree_returns_empty(self) -> None:
        self.assertEqual(build_doc_manifest(REPO / "does_not_exist_xyz"), [])


class RetrieveDocsTests(unittest.TestCase):
    def test_snowflake_star_schema_query_returns_schema_guide_as_top_hit(self) -> None:
        hits = retrieve_docs("snowflake star schema join", REPO, top_k=3, max_chars=1800)
        self.assertTrue(hits)
        self.assertEqual(hits[0]["path"], SCHEMA_DOC)
        self.assertGreater(hits[0]["score"], 0)
        self.assertTrue(hits[0]["excerpt"].strip())
        self.assertLessEqual(len(hits[0]["excerpt"]), 1800)

    def test_databricks_token_scope_query_retrieves_token_doc(self) -> None:
        hits = retrieve_docs("databricks token scope", REPO, top_k=5)
        paths = [h["path"] for h in hits]
        self.assertIn(TOKEN_DOC, paths)

    def test_empty_query_returns_empty_list(self) -> None:
        self.assertEqual(retrieve_docs("", REPO), [])
        self.assertEqual(retrieve_docs("   ", REPO), [])

    def test_no_match_query_returns_empty_list(self) -> None:
        # Tokens with no overlap against any derived doc keywords.
        self.assertEqual(retrieve_docs("zzqqxx wibblezorp", REPO), [])

    def test_excerpt_never_exceeds_max_chars(self) -> None:
        for query in ("snowflake star schema", "databricks token scope", "medallion bronze silver"):
            for cap in (200, 800, 1800):
                for hit in retrieve_docs(query, REPO, top_k=3, max_chars=cap):
                    self.assertLessEqual(len(hit["excerpt"]), cap)

    def test_top_k_bounds_result_count(self) -> None:
        hits = retrieve_docs("databricks", REPO, top_k=2)
        self.assertLessEqual(len(hits), 2)

    def test_deterministic_ordering(self) -> None:
        a = retrieve_docs("databricks token scope", REPO, top_k=5)
        b = retrieve_docs("databricks token scope", REPO, top_k=5)
        self.assertEqual([h["path"] for h in a], [h["path"] for h in b])

    def test_missing_docs_tree_returns_empty(self) -> None:
        self.assertEqual(
            retrieve_docs("snowflake", REPO / "does_not_exist_xyz"), []
        )


if __name__ == "__main__":
    unittest.main()
