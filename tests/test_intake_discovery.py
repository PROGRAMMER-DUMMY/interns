"""Discovery scanners: measured sizes only, structured failures, never a crash."""
from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from core.intake.declaration import SourceDeclaration, save_source_declaration
from core.intake.discovery import (
    ARRIVAL_CONTINUOUS,
    ARRIVAL_ONE_SHOT,
    ARRIVAL_PERIODIC,
    ARRIVAL_UNKNOWN,
    SCANNERS,
    ScanOutcome,
    DiscoveredTable,
    classify_arrival,
    implied_lane,
    run_discovery,
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

    def declare(self, **kwargs) -> None:
        save_source_declaration(self.layout, SourceDeclaration(**kwargs))

    def discovery_payload(self) -> dict:
        path = self.layout.generated_dir / "intake" / "discovery.json"
        return json.loads(path.read_text(encoding="utf-8"))

    def write(self, relative: str, size: int, *, mtime: float | None = None) -> Path:
        path = self.workspace / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"x" * size)
        if mtime is not None:
            import os

            os.utime(path, (mtime, mtime))
        return path


class TestLocalScanner(_TempWorkspace):
    def test_flat_files_become_one_table_each_with_measured_sizes(self):
        self.write("data/orders.csv", 120)
        self.write("data/customers.csv", 80)
        self.declare(type="local_files", location="data")

        result = run_discovery(self.repo_root, "workspaces/sample_ws")
        payload = self.discovery_payload()

        self.assertEqual(result.status, "ok")
        self.assertEqual({table["name"] for table in payload["tables"]}, {"orders", "customers"})
        self.assertEqual({table["size_bytes"] for table in payload["tables"]}, {120, 80})
        self.assertEqual(payload["working_set_estimate_bytes"], 200)
        self.assertEqual(payload["open_questions"], [])

    def test_part_files_in_a_directory_collapse_into_one_table(self):
        self.write("data/events/part-0.parquet", 10)
        self.write("data/events/part-1.parquet", 15)
        self.declare(type="local_files", location="data")

        run_discovery(self.repo_root, "workspaces/sample_ws")
        payload = self.discovery_payload()

        self.assertEqual(len(payload["tables"]), 1)
        table = payload["tables"][0]
        self.assertEqual(table["name"], "events")
        self.assertEqual(table["format"], "parquet")
        self.assertEqual(table["size_bytes"], 25)

    def test_delta_directory_is_one_delta_table(self):
        self.write("data/ledger/_delta_log/00000.json", 5)
        self.write("data/ledger/part-0.parquet", 30)
        self.declare(type="local_files", location="data")

        run_discovery(self.repo_root, "workspaces/sample_ws")
        formats = {table["format"] for table in self.discovery_payload()["tables"]}
        self.assertIn("delta", formats)

    def test_row_estimates_are_never_fabricated(self):
        self.write("data/orders.csv", 120)
        self.declare(type="local_files", location="data")
        run_discovery(self.repo_root, "workspaces/sample_ws")
        self.assertIsNone(self.discovery_payload()["tables"][0]["row_estimate"])

    def test_missing_root_is_reported_not_raised(self):
        self.declare(type="local_files", location="nowhere")
        result = run_discovery(self.repo_root, "workspaces/sample_ws")
        self.assertEqual(result.status, "blocked")
        self.assertEqual(result.table_count, 0)


class TestArrivalPattern(_TempWorkspace):
    """Arrival pattern is MEASURED from file modification times, or it stays
    `unknown`. A guessed cadence would silently decide the velocity lane."""

    def test_classify_needs_timestamps_and_never_guesses(self):
        pattern, note = classify_arrival([])
        self.assertEqual(ARRIVAL_UNKNOWN, pattern)
        self.assertTrue(note)

    def test_one_timestamp_is_a_one_shot_drop(self):
        self.assertEqual(ARRIVAL_ONE_SHOT, classify_arrival([1000.0])[0])

    def test_identical_timestamps_are_a_single_drop_not_a_stream(self):
        pattern, note = classify_arrival([1000.0] * 5)
        self.assertEqual(ARRIVAL_ONE_SHOT, pattern)
        self.assertIn("one modification time", note)

    def test_tight_gaps_are_continuous_and_wide_gaps_are_periodic(self):
        tight = [1000.0 + 30 * i for i in range(6)]
        wide = [1000.0 + 86400 * i for i in range(6)]
        self.assertEqual(ARRIVAL_CONTINUOUS, classify_arrival(tight)[0])
        self.assertEqual(ARRIVAL_PERIODIC, classify_arrival(wide)[0])

    def test_local_scan_measures_the_pattern_per_table_with_evidence(self):
        for i in range(4):
            self.write(f"data/events/part-{i}.parquet", 10, mtime=1_700_000_000 + 60 * i)
        self.write("data/snapshot.csv", 10, mtime=1_700_000_000)
        self.declare(type="local_files", location="data")

        run_discovery(self.repo_root, "workspaces/sample_ws")
        tables = {t["name"]: t for t in self.discovery_payload()["tables"]}

        self.assertEqual(ARRIVAL_CONTINUOUS, tables["events"]["arrival_pattern"])
        self.assertTrue(tables["events"]["arrival_evidence"])
        self.assertEqual(ARRIVAL_ONE_SHOT, tables["snapshot"]["arrival_pattern"])

    def test_is_streaming_stays_derived_for_backward_compatibility(self):
        for i in range(4):
            self.write(f"data/events/part-{i}.parquet", 10, mtime=1_700_000_000 + 60 * i)
        self.write("data/snapshot.csv", 10, mtime=1_700_000_000)
        self.declare(type="local_files", location="data")

        run_discovery(self.repo_root, "workspaces/sample_ws")
        tables = {t["name"]: t for t in self.discovery_payload()["tables"]}
        self.assertIs(True, tables["events"]["is_streaming"])
        self.assertIs(False, tables["snapshot"]["is_streaming"])

    def test_an_unlistable_source_reports_unknown_not_a_guess(self):
        """uc_existing exposes no file arrival times: honest `unknown`."""
        from core.intake import discovery as module

        self.declare(type="uc_existing", location="cat.sch")
        outcome = ScanOutcome(
            status="ok",
            tables=[DiscoveredTable("a", "cat.sch.a", "delta", 100, None)],
        )
        original = module.SCANNERS["uc_existing"]
        module.SCANNERS["uc_existing"] = lambda declaration, **_: outcome
        self.addCleanup(lambda: module.SCANNERS.__setitem__("uc_existing", original))

        run_discovery(self.repo_root, "workspaces/sample_ws")
        table = self.discovery_payload()["tables"][0]
        self.assertEqual(ARRIVAL_UNKNOWN, table["arrival_pattern"])
        self.assertIs(False, table["is_streaming"])

    def test_implied_lane_is_none_when_arrival_and_sla_disagree(self):
        self.assertEqual("streaming", implied_lane(ARRIVAL_CONTINUOUS, "minutes"))
        self.assertEqual("micro_batch", implied_lane(ARRIVAL_PERIODIC, "hourly"))
        self.assertEqual("batch", implied_lane(ARRIVAL_ONE_SHOT, "daily"))
        self.assertIsNone(implied_lane(ARRIVAL_ONE_SHOT, "sub_minute"))
        self.assertIsNone(implied_lane(ARRIVAL_UNKNOWN, "daily"))


class TestUnmeasuredWorkingSet(_TempWorkspace):
    def test_one_unmeasured_table_makes_the_working_set_unknown(self):
        from core.intake import discovery as module

        self.declare(type="uc_existing", location="cat.sch")
        outcome = ScanOutcome(
            status="ok",
            tables=[
                DiscoveredTable("a", "cat.sch.a", "delta", 100, None),
                DiscoveredTable("b", "cat.sch.b", "delta", None, None),
            ],
        )
        original = module.SCANNERS["uc_existing"]
        module.SCANNERS["uc_existing"] = lambda declaration, **_: outcome
        self.addCleanup(lambda: module.SCANNERS.__setitem__("uc_existing", original))

        result = run_discovery(self.repo_root, "workspaces/sample_ws")
        payload = self.discovery_payload()

        self.assertIsNone(result.working_set_estimate_bytes)
        self.assertIn("volume_and_growth", payload["open_questions"])
        self.assertTrue(any("no measured byte size" in note for note in payload["notes"]))


class TestUnsupportedConnectors(_TempWorkspace):
    def test_every_declared_type_has_a_scanner(self):
        from core.intake.declaration import SOURCE_TYPES

        self.assertEqual(set(SCANNERS), set(SOURCE_TYPES))

    def test_stub_connectors_report_unsupported_and_name_what_is_needed(self):
        for connector in ("adls", "gcs", "jdbc", "sftp", "kafka"):
            with self.subTest(connector=connector):
                self.declare(type=connector, location="somewhere")
                result = run_discovery(self.repo_root, "workspaces/sample_ws")
                payload = self.discovery_payload()
                self.assertEqual(result.status, "unsupported_yet")
                self.assertTrue(payload["needs"], "stub must name what it needs")
                self.assertIn("volume_and_growth", payload["open_questions"])

    def test_undeclared_workspace_is_reported_not_raised(self):
        result = run_discovery(self.repo_root, "workspaces/sample_ws")
        self.assertEqual(result.status, "not_declared")
        self.assertTrue(self.discovery_payload()["needs"])


class TestS3Scanner(_TempWorkspace):
    def test_missing_boto3_or_credentials_never_crashes(self):
        self.declare(type="s3", location="s3://bucket/prefix", credential_ref="no_such_profile")
        result = run_discovery(self.repo_root, "workspaces/sample_ws")
        # boto3 present or not, the outcome is a structured status -- never an
        # exception and never a fabricated table list.
        self.assertIn(result.status, {"credential_or_tool_missing", "ok"})
        self.assertTrue(self.discovery_payload()["notes"])

    def test_malformed_location_is_blocked_with_guidance(self):
        self.declare(type="s3", location="https://example.invalid/bucket")
        result = run_discovery(self.repo_root, "workspaces/sample_ws")
        self.assertIn(result.status, {"blocked", "credential_or_tool_missing"})


class StageRoutingTests(_TempWorkspace):
    def test_discovery_result_carries_the_source_discovery_roster(self):
        # Routing is attached whatever the scan outcome: an undeclared source is
        # still a discovery stage the specialists own.
        summary = run_discovery(self.repo_root, "workspaces/sample_ws").summary()
        roster = routing_for("source_discovery")
        self.assertEqual(summary["stage"], "source_discovery")
        self.assertTrue(roster["agents"], "source_discovery routes no agent")
        self.assertEqual(summary["required_specialists"], roster["agents"])
        self.assertEqual(summary["suggested_skills"], roster["skills"])


if __name__ == "__main__":
    unittest.main()
