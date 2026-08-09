"""Discovery scanners: measured sizes only, structured failures, never a crash."""
from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from core.intake import discovery as discovery_module
from core.intake.declaration import SourceDeclaration, save_source_declaration
from core.intake.discovery import (
    ARRIVAL_CONTINUOUS,
    ARRIVAL_ONE_SHOT,
    ARRIVAL_PERIODIC,
    ARRIVAL_UNKNOWN,
    SCANNERS,
    UC_NEED,
    ScanOutcome,
    DiscoveredTable,
    UnityCatalogGateway,
    classify_arrival,
    implied_lane,
    run_discovery,
    scan_object_store,
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
        # No test reaches a real Databricks account. Tests that exercise the
        # Unity Catalog path re-patch this with a fake gateway.
        self.use_unity_catalog(None)

    def use_unity_catalog(self, gateway, note: str = "") -> None:
        patcher = mock.patch.object(
            discovery_module,
            "_open_unity_catalog_gateway",
            lambda workspace: (
                gateway,
                note or ("" if gateway is not None else "Databricks is not configured"),
            ),
        )
        patcher.start()
        self.addCleanup(patcher.stop)

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


def _statement_response(rows, state="SUCCEEDED"):
    return SimpleNamespace(
        statement_id="statement-1",
        status=SimpleNamespace(state=state, error=None),
        result=SimpleNamespace(data_array=rows),
    )


class _FakeStatementExecution:
    """Stands in for WorkspaceClient.statement_execution -- no network."""

    def __init__(self, listings: dict[str, list[list[str]]]):
        self.listings = listings
        self.statements: list[tuple[str, str]] = []

    def execute_statement(self, *, warehouse_id, statement, wait_timeout):
        self.statements.append((warehouse_id, statement))
        uri = statement.split("'", 1)[1].rsplit("'", 1)[0]
        return _statement_response(self.listings.get(uri, []))


class _FakeWorkspaceClient:
    def __init__(self, *, listings=None, locations=(), warehouses=()):
        self.statement_execution = _FakeStatementExecution(listings or {})
        self.external_locations = SimpleNamespace(list=lambda: list(locations))
        self.warehouses = SimpleNamespace(list=lambda: list(warehouses))


def _gateway(*, listings=None, locations=(("estate", "s3://bucket/data"),),
             warehouses=(("wh-1", "RUNNING"),), preferred=""):
    client = _FakeWorkspaceClient(
        listings=listings,
        locations=[SimpleNamespace(name=name, url=url) for name, url in locations],
        warehouses=[SimpleNamespace(id=wid, state=state) for wid, state in warehouses],
    )
    return UnityCatalogGateway(client, preferred_warehouse_id=preferred)


# `s3://bucket/data` holding one flat file, one part-file directory, and a
# directory whose NAME carries a data suffix (the trap: it is not a file).
_LISTINGS = {
    "s3://bucket/data/": [
        ["s3://bucket/data/snapshot.csv", "snapshot.csv", "120", "1700000000000"],
        ["s3://bucket/data/orders/", "orders/", "0", "0"],
        ["s3://bucket/data/archive.parquet/", "archive.parquet/", "0", "0"],
    ],
    "s3://bucket/data/orders/": [
        ["s3://bucket/data/orders/part-0.parquet", "part-0.parquet", "10", "1700000000000"],
        ["s3://bucket/data/orders/part-1.parquet", "part-1.parquet", "15", "1700000060000"],
        # A cadence needs more than two arrivals: two files a minute apart is one
        # upload, not a stream (see BulkUploadNotCadenceTests).
        ["s3://bucket/data/orders/part-2.parquet", "part-2.parquet", "20", "1700000120000"],
        ["s3://bucket/data/orders/part-3.parquet", "part-3.parquet", "25", "1700000180000"],
    ],
    "s3://bucket/data/archive.parquet/": [],
}


class TestUnityCatalogScanner(_TempWorkspace):
    """The bucket is already a UC external location, so Databricks holds the
    credential -- discovery must not demand a second local credential set."""

    def test_list_rows_become_tables_with_measured_sizes_and_arrival(self):
        self.declare(type="s3", location="s3://bucket/data")
        self.use_unity_catalog(_gateway(listings=_LISTINGS))

        result = run_discovery(self.repo_root, "workspaces/sample_ws")
        payload = self.discovery_payload()
        tables = {table["name"]: table for table in payload["tables"]}

        self.assertEqual(result.status, "ok")
        self.assertEqual({"snapshot", "orders"}, set(tables))
        self.assertEqual(120, tables["snapshot"]["size_bytes"])
        self.assertEqual(70, tables["orders"]["size_bytes"])  # 10+15+20+25, summed across parts
        self.assertEqual("parquet", tables["orders"]["format"])
        self.assertEqual("s3://bucket/data/orders", tables["orders"]["path"])
        self.assertEqual(190, payload["working_set_estimate_bytes"])  # 120 snapshot + 70 orders
        # arrival measured from modification_time_ms, never fabricated
        self.assertEqual(ARRIVAL_CONTINUOUS, tables["orders"]["arrival_pattern"])
        self.assertEqual(ARRIVAL_ONE_SHOT, tables["snapshot"]["arrival_pattern"])

    def test_directory_rows_are_recursed_not_counted_as_files(self):
        self.declare(type="s3", location="s3://bucket/data")
        self.use_unity_catalog(_gateway(listings=_LISTINGS))

        run_discovery(self.repo_root, "workspaces/sample_ws")
        names = {table["name"] for table in self.discovery_payload()["tables"]}
        self.assertNotIn("archive", names, "a trailing-slash directory is not a file")
        self.assertNotIn("orders/", names)

    def test_the_path_used_is_recorded_in_the_notes(self):
        self.declare(type="s3", location="s3://bucket/data")
        self.use_unity_catalog(_gateway(listings=_LISTINGS))

        run_discovery(self.repo_root, "workspaces/sample_ws")
        notes = " ".join(self.discovery_payload()["notes"])
        self.assertIn("listed via Unity Catalog external location estate on warehouse wh-1", notes)

    def test_a_stopped_warehouse_is_reported_because_the_list_cold_starts_it(self):
        self.declare(type="s3", location="s3://bucket/data")
        self.use_unity_catalog(_gateway(listings=_LISTINGS, warehouses=(("wh-1", "STOPPED"),)))

        run_discovery(self.repo_root, "workspaces/sample_ws")
        notes = " ".join(self.discovery_payload()["notes"])
        self.assertIn("STOPPED", notes)
        self.assertIn("cold start", notes)

    def test_the_configured_warehouse_wins_over_the_first_available(self):
        gateway = _gateway(
            listings=_LISTINGS,
            warehouses=(("wh-first", "RUNNING"), ("wh-configured", "RUNNING")),
            preferred="wh-configured",
        )
        self.assertEqual(("wh-configured", "RUNNING"), gateway.warehouse())
        unconfigured = _gateway(
            listings=_LISTINGS, warehouses=(("wh-first", "RUNNING"), ("wh-second", "RUNNING"))
        )
        self.assertEqual(("wh-first", "RUNNING"), unconfigured.warehouse())

    def test_truncation_is_honored_and_leaves_the_working_set_unknown(self):
        self.declare(type="s3", location="s3://bucket/data")
        self.use_unity_catalog(_gateway(listings=_LISTINGS))

        result = run_discovery(self.repo_root, "workspaces/sample_ws", max_items=1)
        payload = self.discovery_payload()

        self.assertIs(True, payload["scan_policy"]["truncated"])
        self.assertIsNone(result.working_set_estimate_bytes)
        self.assertTrue(any("truncated" in note for note in payload["notes"]))

    def test_adls_and_gcs_list_through_the_same_path(self):
        cases = {
            "adls": (
                "abfss://raw@account.dfs.core.windows.net/landing",
                "abfss://raw@account.dfs.core.windows.net/landing/",
            ),
            "gcs": ("gs://bucket/landing", "gs://bucket/landing/"),
        }
        for connector, (location, listed_uri) in cases.items():
            with self.subTest(connector=connector):
                self.declare(type=connector, location=location)
                self.use_unity_catalog(
                    _gateway(
                        listings={listed_uri: [[f"{listed_uri}events.json", "events.json", "42", "1700000000000"]]},
                        locations=(("landing_zone", location),),
                    )
                )
                result = run_discovery(self.repo_root, "workspaces/sample_ws")
                payload = self.discovery_payload()
                self.assertEqual("ok", result.status)
                self.assertEqual(42, payload["tables"][0]["size_bytes"])

    def test_a_failed_list_falls_back_instead_of_raising(self):
        client = _FakeWorkspaceClient(
            locations=[SimpleNamespace(name="estate", url="s3://bucket/data")],
            warehouses=[SimpleNamespace(id="wh-1", state="RUNNING")],
        )
        client.statement_execution.execute_statement = mock.Mock(side_effect=RuntimeError("denied"))
        self.declare(type="s3", location="s3://bucket/data")
        self.use_unity_catalog(UnityCatalogGateway(client))

        result = run_discovery(self.repo_root, "workspaces/sample_ws")
        self.assertIn(result.status, {"credential_or_tool_missing", "ok"})
        self.assertTrue(any("LIST on" in note for note in self.discovery_payload()["notes"]))


class TestExternalLocationMatching(unittest.TestCase):
    def test_prefix_matching_respects_segment_boundaries(self):
        locations = [("estate", "s3://bucket/data")]
        match = discovery_module._matching_external_location
        self.assertEqual("estate", match("s3://bucket/data/orders", locations))
        self.assertEqual("estate", match("s3://bucket/data", locations))
        self.assertEqual("estate", match("s3://bucket/data/", locations))
        self.assertEqual("", match("s3://bucket/database/orders", locations))
        self.assertEqual("", match("s3://bucket-2/data", locations))
        self.assertEqual("", match("s3://bucket/data", []))


class TestScannerSelection(_TempWorkspace):
    """Unity Catalog -> boto3 -> honest refusal, and the refusal names both."""

    def test_unity_catalog_is_preferred_over_the_local_sdk(self):
        self.declare(type="s3", location="s3://bucket/data")
        self.use_unity_catalog(_gateway(listings=_LISTINGS))
        with mock.patch.object(discovery_module, "scan_s3") as local:
            result = run_discovery(self.repo_root, "workspaces/sample_ws")
        local.assert_not_called()
        self.assertEqual("ok", result.status)

    def test_boto3_is_used_when_no_external_location_covers_the_bucket(self):
        self.declare(type="s3", location="s3://other-bucket/data")
        self.use_unity_catalog(_gateway(listings=_LISTINGS))
        sentinel = ScanOutcome(
            status="ok",
            tables=[DiscoveredTable("via_boto3", "s3://other-bucket/data", "csv", 7, None)],
            notes=["local sdk note"],
        )
        with mock.patch.object(discovery_module, "scan_s3", return_value=sentinel), \
                mock.patch("importlib.util.find_spec", return_value=object()):
            result = run_discovery(self.repo_root, "workspaces/sample_ws")

        payload = self.discovery_payload()
        self.assertEqual("ok", result.status)
        self.assertEqual("via_boto3", payload["tables"][0]["name"])
        # the UC reason survives into the evidence, then the local note
        self.assertTrue(any("not under any Unity Catalog external location" in n for n in payload["notes"]))
        self.assertIn("local sdk note", payload["notes"])

    def test_neither_path_available_names_both_options(self):
        self.declare(type="s3", location="s3://other-bucket/data")
        with mock.patch("importlib.util.find_spec", return_value=None):
            result = run_discovery(self.repo_root, "workspaces/sample_ws")

        payload = self.discovery_payload()
        self.assertEqual("credential_or_tool_missing", result.status)
        self.assertIn(UC_NEED, payload["needs"])
        self.assertTrue(any("boto3" in need for need in payload["needs"]))
        self.assertIn("volume_and_growth", payload["open_questions"])

    def test_a_non_uri_location_never_reaches_the_warehouse(self):
        gateway = _gateway(listings=_LISTINGS)
        declaration = SourceDeclaration(type="s3", location="not a uri'; DROP")
        outcome = scan_object_store(
            declaration, workspace=self.workspace, repo_root=self.repo_root,
            max_items=10, max_seconds=5.0, gateway=gateway,
        )
        self.assertEqual([], gateway.client.statement_execution.statements)
        self.assertTrue(any("not an object-store URI" in note for note in outcome.notes))


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


class EntityGroupingTests(unittest.TestCase):
    """A folder of distinct entities is not a part-file layout.

    Found by a live replay: five EMR entity files per hospital
    (departments/encounters/patients/providers/transactions) were collapsed into
    ONE table named after the folder. Merging unrelated schemas into a single
    bronze table poisons every grain and join built on top of it.
    """

    def _tables(self, names, root="root"):
        entries = [(Path(root) / n, 100, None) for n in names]
        return discovery_module._group_paths(Path(root), entries)

    def test_distinct_entities_in_one_folder_become_separate_tables(self):
        tables = self._tables([
            "EMR/hospital-a/patients.csv",
            "EMR/hospital-a/encounters.csv",
            "EMR/hospital-a/providers.csv",
        ])
        self.assertEqual(len(tables), 3, [t.name for t in tables])
        self.assertEqual(
            {t.name for t in tables}, {"patients", "encounters", "providers"}
        )

    def test_a_split_entity_keeps_the_real_file_path(self):
        """F22: the split keyed each entity by its STEM, so the recorded path
        dropped the extension -- `.../hospital-a/patients` instead of
        `.../hospital-a/patients.csv`. That path is what `generate-ingestion`
        writes into `COPY INTO ... FROM`, and it does not exist:
        `[PATH_NOT_FOUND] ... SQLSTATE 42K03` on the live warehouse."""
        tables = self._tables([
            "EMR/hospital-a/patients.csv",
            "EMR/hospital-a/encounters.csv",
        ])
        by_name = {t.name: t for t in tables}
        self.assertTrue(
            by_name["patients"].path.endswith("patients.csv"), by_name["patients"].path
        )

    def test_a_part_file_directory_still_points_at_the_directory(self):
        """The other half of the contract: a real part-file layout must keep
        pointing at the FOLDER, which is what COPY INTO/Auto Loader want when
        the dataset is many files."""
        tables = self._tables([
            "orders/part-00001.csv",
            "orders/part-00002.csv",
        ])
        self.assertEqual(len(tables), 1)
        self.assertTrue(tables[0].path.endswith("orders"), tables[0].path)

    def test_shards_of_one_dataset_stay_grouped(self):
        """part-00001/part-00002 differ only by a digit run: one dataset."""
        tables = self._tables([
            "orders/part-00001.csv",
            "orders/part-00002.csv",
            "orders/part-00003.csv",
        ])
        self.assertEqual(len(tables), 1, [t.name for t in tables])
        self.assertEqual(tables[0].size_bytes, 300)

    def test_numbered_sources_of_one_dataset_stay_grouped(self):
        """hospital1_claim_data / hospital2_claim_data: same template, one set."""
        tables = self._tables([
            "claims/hospital1_claim_data.csv",
            "claims/hospital2_claim_data.csv",
        ])
        self.assertEqual(len(tables), 1, [t.name for t in tables])
        self.assertEqual(tables[0].name, "claims")

    def test_same_entity_in_two_folders_gets_unique_qualified_names(self):
        """Two tables cannot share a name: bronze naming and dbt models collide."""
        tables = self._tables([
            "EMR/hospital-a/patients.csv",
            "EMR/hospital-a/encounters.csv",
            "EMR/hospital-b/patients.csv",
            "EMR/hospital-b/encounters.csv",
        ])
        names = [t.name for t in tables]
        self.assertEqual(len(names), len(set(names)), names)
        self.assertIn("hospital-a__patients", names)
        self.assertIn("hospital-b__patients", names)

    def test_unambiguous_names_are_left_short(self):
        """Only colliding names get qualified; the rest stay readable."""
        tables = self._tables([
            "EMR/hospital-a/patients.csv",
            "EMR/hospital-a/encounters.csv",
            "cptcodes/cptcodes.csv",
        ])
        self.assertIn("cptcodes", [t.name for t in tables])

    def test_sizes_are_preserved_across_the_split(self):
        tables = self._tables([
            "EMR/hospital-a/patients.csv",
            "EMR/hospital-a/encounters.csv",
        ])
        self.assertEqual(sum(t.size_bytes for t in tables), 200)


class BulkUploadNotCadenceTests(unittest.TestCase):
    """A few files written seconds apart is one upload, not a stream.

    Found live: two files 3s apart were classified `continuous`, which routed the
    table to Auto Loader streaming instead of a batch COPY INTO -- an ingestion
    mode chosen from two data points.
    """

    def test_two_files_seconds_apart_is_a_single_drop(self):
        pattern, evidence = discovery_module.classify_arrival([1000.0, 1003.0])
        self.assertEqual(pattern, discovery_module.ARRIVAL_ONE_SHOT)
        self.assertIn("bulk drop", evidence)

    def test_tight_burst_is_a_single_drop(self):
        pattern, _ = discovery_module.classify_arrival([1000.0, 1001.0, 1002.0])
        self.assertEqual(pattern, discovery_module.ARRIVAL_ONE_SHOT)

    def test_a_real_stream_is_still_continuous(self):
        stamps = [1000.0 + 60 * i for i in range(10)]
        pattern, _ = discovery_module.classify_arrival(stamps)
        self.assertEqual(pattern, discovery_module.ARRIVAL_CONTINUOUS)

    def test_a_daily_cadence_is_periodic(self):
        stamps = [1000.0 + 86400 * i for i in range(5)]
        pattern, _ = discovery_module.classify_arrival(stamps)
        self.assertEqual(pattern, discovery_module.ARRIVAL_PERIODIC)
