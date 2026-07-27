"""Regression: source discovery must work on remote storage, not just local disk.

Origin (2026-07-27): a user said "my data is at s3://amzn-workspace-rcm/ and I
want everything on Databricks", and the platform replied by asking what to name
a folder. `discover-external-sources` was local-filesystem only --
`Path(external_root).expanduser().resolve()` -- so it could not see the bucket,
could not find the `docs/` folder holding the KPI spreadsheet and the data-model
image, and had nothing to propose.

The classification tree never opens a file (it reads `.suffix`, `.name`,
`.stem`, `.relative_to()` only), so making it storage-agnostic was a change to
the WALK, not to the logic. These tests pin both halves:

  * a URI must never go through `Path(...)`, which collapses `s3://b/k` to
    `s3:/b/k` -- the same identifier-collapse class as the Unity Catalog bug, and
    it fails silently by producing a plausible-looking wrong root;
  * the governance allowlist (theme T8) must still refuse an un-configured root,
    and must not leak across a prefix boundary.

`memory://` is used as the remote backend so the suite needs no credentials and
no network -- it exercises the same fsspec code path as `s3://`.
"""
from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import fsspec

from core.onboarding.sources import external_discovery as ed
from core.storage.external_data import (
    ExternalDataPolicy,
    bounded_external_files,
    is_storage_uri,
    is_within_allowed_roots,
)

_ROOT = "memory://amzn-workspace-rcm/"
_KEYS = [
    "/amzn-workspace-rcm/transactions/part-0.parquet",
    "/amzn-workspace-rcm/transactions/part-1.parquet",
    "/amzn-workspace-rcm/patients/patients.csv",
    "/amzn-workspace-rcm/docs/Sample_KPI.xlsx",
    "/amzn-workspace-rcm/docs/DataModel.png",
    "/amzn-workspace-rcm/logs/app.log",
]


def _seed_memory() -> None:
    fs = fsspec.filesystem("memory")
    for key in _KEYS:
        with fs.open(key, "wb") as handle:
            handle.write(b"x")


class UriDetectionTests(unittest.TestCase):
    def test_storage_uris_are_recognised_by_scheme_not_a_fixed_list(self):
        for uri in ("s3://b/k", "gs://b/k", "abfss://c@a.dfs.core.windows.net/x",
                    "http://h/x", "memory://b/k", "made-up+scheme://b/k"):
            with self.subTest(uri=uri):
                self.assertTrue(is_storage_uri(uri))

    def test_local_paths_are_not_uris(self):
        for path in ("C:/Users/x", "/tmp/x", "relative/path", "", "."):
            with self.subTest(path=path):
                self.assertFalse(is_storage_uri(path))

    def test_a_uri_survives_the_constructor_intact(self):
        # `Path("s3://b/k")` collapses the `//`. That is the whole bug.
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "workspaces" / "w").mkdir(parents=True)
            d = ed.ExternalSourceDiscoverer(root, "workspaces/w", "s3://bucket/prefix")
            self.assertEqual(d.external_root.protocol, "s3")
            self.assertIn("bucket", str(d.external_root))
            self.assertNotIn("s3:/bucket", str(d.external_root).replace("s3://", ""))


class AllowlistTests(unittest.TestCase):
    """T8 governance: an un-configured root is refused, for URIs too."""

    POLICY = ExternalDataPolicy(configured_uri_roots=("s3://amzn-workspace-rcm/",))

    def test_a_configured_uri_root_is_allowed(self):
        self.assertTrue(is_within_allowed_roots(
            "s3://amzn-workspace-rcm/docs", Path("."), self.POLICY))

    def test_an_unconfigured_bucket_is_refused(self):
        self.assertFalse(is_within_allowed_roots(
            "s3://someone-elses-bucket/", Path("."), self.POLICY))

    def test_prefix_matching_does_not_leak_across_a_name_boundary(self):
        # `s3://bucket-evil` must NOT be allowed by a root of `s3://bucket`.
        # Prefix matching without a segment boundary is how allowlists leak.
        self.assertFalse(is_within_allowed_roots(
            "s3://amzn-workspace-rcm-evil/x", Path("."), self.POLICY))

    def test_no_policy_refuses_every_uri(self):
        self.assertFalse(is_within_allowed_roots("s3://any/", Path("."), None))

    def test_local_allowlisting_is_unchanged(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp)
            self.assertTrue(is_within_allowed_roots(repo / "inside", repo, None))
            self.assertFalse(is_within_allowed_roots(
                Path(tempfile.gettempdir()) / "elsewhere_xyz", repo / "sub", None))


class RemoteWalkTests(unittest.TestCase):
    def test_it_lists_a_remote_root_with_usable_path_objects(self):
        _seed_memory()
        files, truncated = bounded_external_files(_ROOT, max_paths=100, max_seconds=10)
        self.assertFalse(truncated)
        self.assertEqual(len(files), len(_KEYS))
        names = {f.name for f in files}
        self.assertIn("Sample_KPI.xlsx", names)
        # The attributes classification depends on must survive the round trip.
        by_name = {f.name: f for f in files}
        self.assertEqual(by_name["Sample_KPI.xlsx"].suffix, ".xlsx")
        self.assertEqual(by_name["part-0.parquet"].stem, "part-0")

    def test_max_paths_bounds_a_remote_listing(self):
        _seed_memory()
        files, truncated = bounded_external_files(_ROOT, max_paths=2, max_seconds=10)
        self.assertEqual(len(files), 2)
        self.assertTrue(truncated)

    def test_a_missing_backend_names_the_package_to_install(self):
        # fsspec raises ImportError for an uninstalled driver; a bare traceback
        # would send someone hunting. Only meaningful when s3fs is absent.
        try:
            import s3fs  # noqa: F401
            self.skipTest("s3fs installed; nothing to assert about its absence")
        except ImportError:
            pass
        with self.assertRaises(RuntimeError) as ctx:
            bounded_external_files("s3://b/k", max_paths=1, max_seconds=1)
        self.assertIn("s3fs", str(ctx.exception))

    def test_local_walk_is_unchanged(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "sub").mkdir()
            (root / "a.csv").write_text("x", encoding="utf-8")
            (root / "sub" / "b.parquet").write_text("x", encoding="utf-8")
            files, truncated = bounded_external_files(root, max_paths=100, max_seconds=10)
            self.assertFalse(truncated)
            self.assertEqual({f.name for f in files}, {"a.csv", "b.parquet"})
            self.assertTrue(all(isinstance(f, Path) for f in files))


class EndToEndDiscoveryTests(unittest.TestCase):
    """The whole point: datasets, documents and logs classified off a URI."""

    def _run(self, tmp: str):
        _seed_memory()
        root = Path(tmp)
        (root / "workspaces" / "rcm").mkdir(parents=True)
        policy = ExternalDataPolicy(configured_uri_roots=("memory://amzn-workspace-rcm/",))
        with patch.object(ed, "load_external_data_policy", return_value=policy):
            result = ed.ExternalSourceDiscoverer(root, "workspaces/rcm", _ROOT).run()
        return root, json.loads((root / result.discovery_path).read_text(encoding="utf-8"))

    def test_datasets_documents_and_logs_are_classified(self):
        with tempfile.TemporaryDirectory() as tmp:
            _, payload = self._run(tmp)
            by_path = {f["relative_path"]: f for f in payload["files"]}
            self.assertEqual(by_path["transactions/part-0.parquet"]["class_name"], "dataset")
            self.assertEqual(by_path["patients/patients.csv"]["class_name"], "dataset")
            # The KPI sheet and the data-model image are CONTEXT, not tables --
            # in Unity Catalog terms they belong in a volume, not a table.
            self.assertEqual(by_path["docs/Sample_KPI.xlsx"]["class_name"], "document")
            self.assertEqual(by_path["docs/DataModel.png"]["class_name"], "document")
            self.assertEqual(by_path["logs/app.log"]["recommendation"], "exclude_by_default")

    def test_the_remote_root_is_recorded_unmangled(self):
        with tempfile.TemporaryDirectory() as tmp:
            _, payload = self._run(tmp)
            self.assertTrue(payload["external_root"].startswith("memory://"))

    def test_an_unallowlisted_remote_root_is_refused(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "workspaces" / "rcm").mkdir(parents=True)
            empty = ExternalDataPolicy()
            with patch.object(ed, "load_external_data_policy", return_value=empty):
                with self.assertRaises(PermissionError) as ctx:
                    ed.ExternalSourceDiscoverer(root, "workspaces/rcm", _ROOT).run()
            self.assertIn("external_data_roots.local.json", str(ctx.exception))


if __name__ == "__main__":
    unittest.main()
