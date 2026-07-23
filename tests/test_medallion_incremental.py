"""Tests for fingerprint-based incremental medallion refresh.

Covers the pure skip/rebuild planning (core/medallion/incremental.py) and the
end-to-end build behaviour: second run skips unchanged tables, source or SQL
changes cascade downstream, and --force rebuilds everything.
"""
from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest import mock

from core.medallion.incremental import (
    RefreshManifest,
    combine_fingerprints,
    fingerprint_file,
    plan_incremental,
    record_built,
)
from core.medallion.manifest import (
    BronzeTable,
    GoldTable,
    Manifest,
    SilverTable,
    manifest_to_yaml,
)


def _make_manifest() -> Manifest:
    return Manifest(
        workspace="demo",
        inputs_hash="sha256:test",
        target="duckdb",
        bronze=[
            BronzeTable(name="events", source_file="data/events.csv", source_system="test"),
        ],
        silver=[
            SilverTable(name="events", derived_from=["bronze.events"], primary_key=["id"]),
        ],
        gold=[
            GoldTable(name="events_fact", kind="fact", derived_from=["silver.events"], grain="id"),
        ],
    )


def _write_pipeline_files(root: Path, medallion: Path) -> None:
    (root / "data").mkdir(parents=True, exist_ok=True)
    (root / "data" / "events.csv").write_text("id,value\n1,10\n2,20\n", encoding="utf-8")
    bronze = medallion / "bronze"
    silver = medallion / "silver"
    gold = medallion / "gold"
    for d in (bronze, silver, gold):
        d.mkdir(parents=True, exist_ok=True)
    (bronze / "events.duckdb.sql").write_text(
        "CREATE OR REPLACE TABLE bronze.events AS\n"
        "SELECT * FROM read_csv_auto('data/events.csv', HEADER=TRUE);\n",
        encoding="utf-8",
    )
    (silver / "events.duckdb.sql").write_text(
        "CREATE OR REPLACE TABLE silver.events AS\n"
        "SELECT id, value FROM bronze.events;\n",
        encoding="utf-8",
    )
    (gold / "events_fact.duckdb.sql").write_text(
        "CREATE OR REPLACE TABLE gold.events_fact AS\n"
        "SELECT id, value FROM silver.events;\n",
        encoding="utf-8",
    )


def _paths(root: Path, medallion: Path) -> dict[str, Path]:
    state = root / "state"
    return {
        "medallion": medallion,
        "bronze": medallion / "bronze",
        "silver": medallion / "silver",
        "gold": medallion / "gold",
        "state": state,
    }


class FingerprintHelperTests(unittest.TestCase):
    def test_fingerprint_file_missing_and_content_change(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "f.csv"
            self.assertEqual(fingerprint_file(path), "missing")
            path.write_text("a", encoding="utf-8")
            first = fingerprint_file(path)
            self.assertTrue(first.startswith("sha256:"))
            path.write_text("b", encoding="utf-8")
            self.assertNotEqual(first, fingerprint_file(path))

    def test_combine_fingerprints_is_order_sensitive_and_stable(self):
        a = combine_fingerprints(["x", "y"])
        self.assertEqual(a, combine_fingerprints(["x", "y"]))
        self.assertNotEqual(a, combine_fingerprints(["y", "x"]))


class RefreshManifestTests(unittest.TestCase):
    def test_save_writes_json_and_md_and_roundtrips(self):
        with tempfile.TemporaryDirectory() as tmp:
            state = Path(tmp)
            manifest = RefreshManifest(workspace="demo")
            root = Path(tmp)
            medallion = root / "medallion"
            _write_pipeline_files(root, medallion)
            plan = plan_incremental(_make_manifest(), _paths(root, medallion), root, manifest)
            for decision in plan.decisions.values():
                record_built(manifest, decision, run_id="run-1")
            json_path, md_path = manifest.save(state)
            self.assertTrue(json_path.exists())
            self.assertTrue(md_path.exists())
            self.assertIn("bronze.events", md_path.read_text(encoding="utf-8"))

            loaded = RefreshManifest.load(state, workspace="demo")
            entry = loaded.entry("bronze", "events")
            self.assertIsNotNone(entry)
            self.assertEqual(entry.run_id, "run-1")
            self.assertEqual(
                entry.fingerprint, manifest.entry("bronze", "events").fingerprint
            )

    def test_load_missing_or_corrupt_returns_empty(self):
        with tempfile.TemporaryDirectory() as tmp:
            state = Path(tmp)
            self.assertEqual(RefreshManifest.load(state).layers["bronze"], {})
            (state / "refresh_manifest.json").write_text("{not json", encoding="utf-8")
            self.assertEqual(RefreshManifest.load(state).layers["bronze"], {})


class IncrementalPlanTests(unittest.TestCase):
    def _planned_workspace(self, tmp: str):
        root = Path(tmp)
        medallion = root / "medallion"
        _write_pipeline_files(root, medallion)
        return root, medallion, _make_manifest(), _paths(root, medallion)

    def _record_all(self, refresh, plan):
        for decision in plan.decisions.values():
            record_built(refresh, decision, run_id="run-1")

    def test_first_plan_builds_everything(self):
        with tempfile.TemporaryDirectory() as tmp:
            root, medallion, manifest, paths = self._planned_workspace(tmp)
            plan = plan_incremental(manifest, paths, root, RefreshManifest())
            for key in ("bronze.events", "silver.events", "gold.events_fact"):
                self.assertEqual(plan.decisions[key].action, "build")
                self.assertEqual(plan.decisions[key].reason, "no_prior_build")
            self.assertEqual(plan.kpi_action, "build")

    def test_unchanged_inputs_skip_everything_including_kpi(self):
        with tempfile.TemporaryDirectory() as tmp:
            root, medallion, manifest, paths = self._planned_workspace(tmp)
            refresh = RefreshManifest(workspace="demo")
            self._record_all(refresh, plan_incremental(manifest, paths, root, refresh))

            second = plan_incremental(manifest, paths, root, refresh)
            for key in ("bronze.events", "silver.events", "gold.events_fact"):
                self.assertEqual(second.decisions[key].action, "skip")
                self.assertEqual(second.decisions[key].reason, "fingerprint_unchanged")
            self.assertEqual(second.kpi_action, "skip")
            self.assertEqual(second.kpi_reason, "gold_unchanged")

    def test_source_change_cascades_to_all_layers(self):
        with tempfile.TemporaryDirectory() as tmp:
            root, medallion, manifest, paths = self._planned_workspace(tmp)
            refresh = RefreshManifest(workspace="demo")
            self._record_all(refresh, plan_incremental(manifest, paths, root, refresh))

            (root / "data" / "events.csv").write_text("id,value\n1,99\n", encoding="utf-8")
            plan = plan_incremental(manifest, paths, root, refresh)
            for key in ("bronze.events", "silver.events", "gold.events_fact"):
                self.assertEqual(plan.decisions[key].action, "build", key)
                self.assertEqual(plan.decisions[key].reason, "fingerprint_changed", key)
            self.assertEqual(plan.kpi_action, "build")

    def test_silver_sql_change_rebuilds_silver_and_gold_only(self):
        with tempfile.TemporaryDirectory() as tmp:
            root, medallion, manifest, paths = self._planned_workspace(tmp)
            refresh = RefreshManifest(workspace="demo")
            self._record_all(refresh, plan_incremental(manifest, paths, root, refresh))

            (medallion / "silver" / "events.duckdb.sql").write_text(
                "CREATE OR REPLACE TABLE silver.events AS\n"
                "SELECT id, value * 2 AS value FROM bronze.events;\n",
                encoding="utf-8",
            )
            plan = plan_incremental(manifest, paths, root, refresh)
            self.assertEqual(plan.decisions["bronze.events"].action, "skip")
            self.assertEqual(plan.decisions["silver.events"].action, "build")
            self.assertEqual(plan.decisions["gold.events_fact"].action, "build")

    def test_force_rebuilds_everything(self):
        with tempfile.TemporaryDirectory() as tmp:
            root, medallion, manifest, paths = self._planned_workspace(tmp)
            refresh = RefreshManifest(workspace="demo")
            self._record_all(refresh, plan_incremental(manifest, paths, root, refresh))

            plan = plan_incremental(manifest, paths, root, refresh, force=True)
            for decision in plan.decisions.values():
                self.assertEqual(decision.action, "build")
                self.assertEqual(decision.reason, "forced")
            self.assertEqual(plan.kpi_action, "build")
            self.assertEqual(plan.kpi_reason, "forced")

    def test_unresolved_upstream_is_stable_across_plans(self):
        with tempfile.TemporaryDirectory() as tmp:
            root, medallion, manifest, paths = self._planned_workspace(tmp)
            manifest.gold[0].derived_from = ["silver.events", "external.mystery"]
            refresh = RefreshManifest(workspace="demo")
            self._record_all(refresh, plan_incremental(manifest, paths, root, refresh))
            plan = plan_incremental(manifest, paths, root, refresh)
            self.assertEqual(plan.decisions["gold.events_fact"].action, "skip")


class IncrementalBuildEndToEndTests(unittest.TestCase):
    def _workspace(self, root: Path) -> Path:
        workspace = root / "workspaces" / "demo"
        medallion = workspace / "interns" / "generated" / "medallion"
        _write_pipeline_files(root, medallion)
        (medallion / "manifest.yaml").write_text(
            manifest_to_yaml(_make_manifest()), encoding="utf-8"
        )
        return workspace

    def _build(self, workspace: Path, root: Path, **kwargs):
        from core.medallion.build import build_medallion

        with mock.patch("core.medallion.mlflow_emit.start_run", return_value=None), \
                mock.patch("core.medallion.mlflow_emit.finalize_run", return_value=None):
            return build_medallion(workspace, root, cfg=None, **kwargs)

    def test_second_run_skips_and_force_rebuilds(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            workspace = self._workspace(root)

            first = self._build(workspace, root)
            for key in ("bronze.events", "silver.events", "gold.events_fact"):
                self.assertEqual(first.per_table_status[key].status, "ok", key)

            second = self._build(workspace, root)
            for key in ("bronze.events", "silver.events", "gold.events_fact"):
                self.assertEqual(second.per_table_status[key].status, "skipped", key)
            self.assertEqual(second.per_table_status["kpi.regeneration"].status, "skipped")

            state_dir = workspace / "interns" / "state" / "medallion"
            self.assertTrue((state_dir / "refresh_manifest.json").exists())
            self.assertTrue((state_dir / "refresh_manifest.md").exists())

            forced = self._build(workspace, root, force=True)
            for key in ("bronze.events", "silver.events", "gold.events_fact"):
                self.assertEqual(forced.per_table_status[key].status, "ok", key)

    def test_source_change_triggers_rebuild_on_next_run(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            workspace = self._workspace(root)
            self._build(workspace, root)

            (root / "data" / "events.csv").write_text(
                "id,value\n1,10\n2,20\n3,30\n", encoding="utf-8"
            )
            changed = self._build(workspace, root)
            for key in ("bronze.events", "silver.events", "gold.events_fact"):
                self.assertEqual(changed.per_table_status[key].status, "ok", key)
            self.assertEqual(changed.per_table_status["gold.events_fact"].row_count_after, 3)

    def test_force_replaces_stale_rows_left_by_a_pk_value_correction(self):
        # Found live: emit_silver_merge deletes existing rows by matching PK
        # VALUES against the freshly-computed rows. If a silver SQL edit
        # changes what value a PK/dedup-key column now produces for the same
        # logical row (e.g. a masking rule is corrected), the old row's PK
        # no longer matches any new row's PK, so the P1 MERGE's DELETE never
        # touches it -- it silently accumulates as an orphaned duplicate next
        # to the corrected row. A plain (non-forced) rebuild goes through the
        # same MERGE and does NOT clean this up. --force now runs the raw
        # CREATE OR REPLACE TABLE SQL instead of the MERGE rewrite, which is
        # immune to this by construction (a full replace, not a value-match).
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            workspace = self._workspace(root)
            silver_sql = (
                workspace / "interns" / "generated" / "medallion" / "silver" / "events.duckdb.sql"
            )
            # Both versions keep "id" as VARCHAR (same type both builds) --
            # only the runtime STRING VALUE differs, matching the real bug
            # (a masking rule producing a hash vs. plaintext, both VARCHAR).
            # A type change would fail the MERGE outright; a same-type VALUE
            # change is the actually-silent, actually-dangerous case.
            silver_sql.write_text(
                "CREATE OR REPLACE TABLE silver.events AS\n"
                "SELECT 'h-' || CAST(id AS VARCHAR) AS id, value FROM bronze.events;\n",
                encoding="utf-8",
            )
            self._build(workspace, root)

            # Simulate an upstream PK-value correction: "id" is now
            # transformed differently (prefix removed), same logical rows,
            # different PK value, same VARCHAR type.
            silver_sql.write_text(
                "CREATE OR REPLACE TABLE silver.events AS\n"
                "SELECT 'e-' || CAST(id AS VARCHAR) AS id, value FROM bronze.events;\n",
                encoding="utf-8",
            )

            # A normal (non-forced) rebuild: fingerprint changed so it rebuilds,
            # but through the MERGE path -- the old un-prefixed rows persist.
            self._build(workspace, root)
            db_path = workspace / "interns" / "state" / "medallion" / "workspace.duckdb"
            import duckdb

            con = duckdb.connect(str(db_path))
            unforced_count = con.execute("SELECT COUNT(*) FROM silver.events").fetchone()[0]
            con.close()
            self.assertEqual(unforced_count, 4)  # BUG (MERGE-on-PK): 2 old + 2 corrected

            forced = self._build(workspace, root, force=True)
            self.assertEqual(forced.per_table_status["silver.events"].status, "ok")
            con = duckdb.connect(str(db_path))
            forced_count = con.execute("SELECT COUNT(*) FROM silver.events").fetchone()[0]
            ids = {row[0] for row in con.execute("SELECT id FROM silver.events").fetchall()}
            con.close()
            self.assertEqual(forced_count, 2)  # clean: stale rows replaced, not accumulated
            self.assertEqual(ids, {"e-1", "e-2"})

    def test_skip_is_overridden_when_table_is_missing(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            workspace = self._workspace(root)
            self._build(workspace, root)

            db_path = workspace / "interns" / "state" / "medallion" / "workspace.duckdb"
            import duckdb

            con = duckdb.connect(str(db_path))
            con.execute("DROP TABLE bronze.events")
            con.close()

            rebuilt = self._build(workspace, root)
            self.assertEqual(rebuilt.per_table_status["bronze.events"].status, "ok")
            # Downstream fingerprints are unchanged and their tables still exist.
            self.assertEqual(rebuilt.per_table_status["silver.events"].status, "skipped")
            self.assertEqual(rebuilt.per_table_status["gold.events_fact"].status, "skipped")


if __name__ == "__main__":
    unittest.main()
