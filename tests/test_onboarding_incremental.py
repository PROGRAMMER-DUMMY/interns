"""Incremental onboarding: fingerprint manifest + skip/redo decisions.

Phase 3 track A acceptance:
- re-onboarding an unchanged workspace skips every dataset and replays the
  recorded result without rewriting a single artifact (byte-identical);
- changing one dataset re-profiles only that dataset;
- an mtime-only change (e.g. ``git checkout`` restoring content) is UNCHANGED;
- a deleted dataset has its profile artifact dropped and downstream contracts
  flagged stale;
- ``--force`` restores the legacy full re-run.
"""
from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path

from core.onboarding.workspace.incremental import (
    ChangeSet,
    diff_fingerprints,
    fingerprint_file,
    fingerprint_inputs,
    load_manifest,
)
from core.onboarding.workspace.onboarding import WorkspaceOnboarder

ORDERS_CSV = "Id,Amount\nO1,10\nO2,20\n"
CUSTOMERS_CSV = "Cid,Name\nC1,A\nC2,B\n"


class FingerprintTests(unittest.TestCase):
    def test_hash_tracks_content_not_mtime(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "data.csv"
            path.write_text(ORDERS_CSV, encoding="utf-8")
            first = fingerprint_file(path)
            # Touch the file: new mtime, same bytes.
            os.utime(path, ns=(first["mtime_ns"] + 5_000_000_000,) * 2)
            second = fingerprint_file(path)
            self.assertEqual(first["sha256"], second["sha256"])
            self.assertNotEqual(first["mtime_ns"], second["mtime_ns"])
            path.write_text(ORDERS_CSV + "O3,30\n", encoding="utf-8")
            self.assertNotEqual(fingerprint_file(path)["sha256"], first["sha256"])

    def test_fingerprint_inputs_reuses_stored_hash_on_stat_match(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            path = root / "data.csv"
            path.write_text(ORDERS_CSV, encoding="utf-8")
            stat = path.stat()
            sentinel = {
                "data.csv": {
                    "size": stat.st_size,
                    "mtime_ns": stat.st_mtime_ns,
                    "sha256": "sentinel-not-rehashed",
                }
            }
            current = fingerprint_inputs(root, ["data.csv"], sentinel)
            # size+mtime matched, so the stored hash is trusted (no re-read).
            self.assertEqual(current["data.csv"]["sha256"], "sentinel-not-rehashed")

    def test_fingerprint_inputs_skips_missing_files_and_sorts(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "b.csv").write_text("x\n1\n", encoding="utf-8")
            (root / "a.csv").write_text("y\n2\n", encoding="utf-8")
            current = fingerprint_inputs(root, ["b.csv", "missing.csv", "a.csv"])
            self.assertEqual(list(current), ["a.csv", "b.csv"])


class DiffTests(unittest.TestCase):
    def test_added_changed_removed_unchanged(self) -> None:
        previous = {
            "keep.csv": {"size": 10, "mtime_ns": 1, "sha256": "aaa"},
            "edit.csv": {"size": 10, "mtime_ns": 1, "sha256": "bbb"},
            "gone.csv": {"size": 10, "mtime_ns": 1, "sha256": "ccc"},
        }
        current = {
            "keep.csv": {"size": 10, "mtime_ns": 99, "sha256": "aaa"},  # mtime only
            "edit.csv": {"size": 11, "mtime_ns": 99, "sha256": "ddd"},
            "new.csv": {"size": 5, "mtime_ns": 99, "sha256": "eee"},
        }
        changes = diff_fingerprints(previous, current)
        self.assertEqual(changes.unchanged, ["keep.csv"])
        self.assertEqual(changes.changed, ["edit.csv"])
        self.assertEqual(changes.added, ["new.csv"])
        self.assertEqual(changes.removed, ["gone.csv"])
        self.assertFalse(changes.nothing_changed)

    def test_nothing_changed(self) -> None:
        fp = {"a.csv": {"size": 1, "mtime_ns": 2, "sha256": "x"}}
        self.assertTrue(diff_fingerprints(fp, dict(fp)).nothing_changed)
        self.assertTrue(ChangeSet(unchanged=["a.csv"]).nothing_changed)
        self.assertFalse(diff_fingerprints(None, fp).nothing_changed)


class IncrementalOnboardingTests(unittest.TestCase):
    """End-to-end skip/redo behavior through WorkspaceOnboarder.run()."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)
        self.workspace = self.root / "workspaces" / "demo"
        (self.workspace / "docs").mkdir(parents=True)
        (self.workspace / "datasets").mkdir(parents=True)
        (self.workspace / "docs" / "kpis.md").write_text(
            "### KPI 1 - Revenue\nTotal Amount by month.\n", encoding="utf-8"
        )
        self.orders = self.workspace / "datasets" / "orders.csv"
        self.customers = self.workspace / "datasets" / "customers.csv"
        self.orders.write_text(ORDERS_CSV, encoding="utf-8")
        self.customers.write_text(CUSTOMERS_CSV, encoding="utf-8")

    def _run(self, **kwargs):
        return WorkspaceOnboarder(
            self.root, "workspaces/demo", sample_rows=10, **kwargs
        ).run()

    def _artifact_bytes(self) -> dict[str, bytes]:
        generated = self.workspace / "interns" / "generated"
        tracked = sorted(generated.rglob("*.json")) + sorted(generated.rglob("*.sql"))
        return {str(p.relative_to(self.root)): p.read_bytes() for p in tracked}

    def test_unchanged_rerun_skips_and_is_byte_identical(self) -> None:
        first = self._run()
        self.assertEqual(first.incremental["mode"], "full")
        self.assertEqual(first.incremental["datasets_profiled"], 2)
        state_dir = self.workspace / "interns" / "state"
        self.assertTrue((state_dir / "onboarding_manifest.json").exists())
        self.assertTrue((state_dir / "onboarding_manifest.md").exists())

        before = self._artifact_bytes()
        second = self._run()
        self.assertEqual(second.incremental["mode"], "skipped")
        self.assertEqual(second.incremental["datasets_reused"], 2)
        self.assertEqual(second.incremental["datasets_profiled"], 0)
        self.assertEqual(second.kpi_count, first.kpi_count)
        self.assertEqual(second.profile_count, first.profile_count)
        self.assertEqual(second.artifacts, first.artifacts)
        self.assertEqual(self._artifact_bytes(), before)
        self.assertTrue(
            any("incremental_skip" in w for w in second.warnings), second.warnings
        )

    def test_changed_dataset_reprofiles_only_that_dataset(self) -> None:
        self._run()
        customers_profile = next(
            p
            for p in (self.workspace / "interns" / "generated" / "profiles").glob(
                "*.profile.json"
            )
            if "customers" in p.name
        )
        customers_bytes = customers_profile.read_bytes()

        self.orders.write_text(ORDERS_CSV + "O3,30\n", encoding="utf-8")
        result = self._run()
        self.assertEqual(result.incremental["mode"], "incremental")
        self.assertEqual(result.incremental["datasets_profiled"], 1)
        self.assertEqual(result.incremental["datasets_reused"], 1)
        # Unchanged dataset's profile artifact untouched, changed one updated.
        self.assertEqual(customers_profile.read_bytes(), customers_bytes)
        index = json.loads(
            (
                self.workspace
                / "interns"
                / "generated"
                / "profiles"
                / "profile_index.json"
            ).read_text(encoding="utf-8")
        )
        orders_summary = next(
            p for p in index["profiles"] if "orders" in str(p["profile_path"])
        )
        self.assertEqual(orders_summary["row_count"], 3)

    def test_mtime_only_change_still_skips(self) -> None:
        self._run()
        # Same bytes, new mtime (the git-checkout-after-test case).
        self.orders.write_text(ORDERS_CSV, encoding="utf-8")
        result = self._run()
        self.assertEqual(result.incremental["mode"], "skipped")

    def test_deleted_dataset_drops_profile_and_flags_stale(self) -> None:
        self._run()
        profiles_dir = self.workspace / "interns" / "generated" / "profiles"
        self.assertTrue(any("customers" in p.name for p in profiles_dir.glob("*.profile.json")))

        self.customers.unlink()
        result = self._run()
        self.assertEqual(result.incremental["mode"], "incremental")
        self.assertEqual(result.incremental["datasets_removed"], 1)
        self.assertFalse(
            any("customers" in p.name for p in profiles_dir.glob("*.profile.json"))
        )
        self.assertTrue(
            any("dataset_removed" in w and "stale" in w for w in result.warnings),
            result.warnings,
        )
        domain_model = json.loads(
            (
                self.workspace
                / "interns"
                / "generated"
                / "contracts"
                / "domain_model.json"
            ).read_text(encoding="utf-8")
        )
        self.assertFalse(
            any("customers" in str(d.get("path")) for d in domain_model["datasets"])
        )

    def test_force_runs_full_rebuild(self) -> None:
        self._run()
        result = self._run(force=True)
        self.assertEqual(result.incremental["mode"], "full")
        self.assertEqual(result.incremental["datasets_profiled"], 2)
        self.assertEqual(result.incremental["datasets_reused"], 0)

    def test_missing_artifact_disables_skip_fast_path(self) -> None:
        first = self._run()
        registry = (
            self.workspace / "interns" / "generated" / "contracts" / "kpi_registry.json"
        )
        registry.unlink()
        result = self._run()
        # Fast path must notice the missing artifact and rebuild.
        self.assertNotEqual(result.incremental["mode"], "skipped")
        self.assertTrue(registry.exists())
        self.assertEqual(result.kpi_count, first.kpi_count)

    def test_manifest_is_machine_only_with_paired_md(self) -> None:
        self._run()
        state_dir = self.workspace / "interns" / "state"
        manifest = load_manifest(state_dir)
        self.assertIsNotNone(manifest)
        self.assertTrue(manifest["machine_only"])
        self.assertEqual(
            sorted(manifest["inputs"]),
            ["data_files", "decision_files", "model_files", "registry_files"],
        )
        self.assertEqual(len(manifest["inputs"]["data_files"]), 2)
        md = (state_dir / "onboarding_manifest.md").read_text(encoding="utf-8")
        self.assertIn("Onboarding Manifest", md)
        self.assertIn("orders.csv", md)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
