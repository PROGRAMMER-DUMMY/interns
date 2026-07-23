"""Regression tests for the production-hardening tranches on the medallion
generator: data-contract population/application, idempotent silver MERGE, the
5 DQ dimensions, the SLA contract, and the SCD2 dimension emitter.
"""
from __future__ import annotations

import unittest

import duckdb

from core.medallion.merge_emitter import emit_scd2_merge, emit_silver_merge
from core.medallion.silver_contract import TableContract
from core.medallion.design import (
    _seed_accuracy_assertions,
    _seed_type_casts,
    _build_sla_contract,
    _split_schema_incompatible_groups,
)


class ContractRoundTripTests(unittest.TestCase):
    def test_key_rename_survives_round_trip(self):
        tc = TableContract(key_rename={"Id": "encounter_id"},
                           dedup_keys=["source_system", "encounter_id"])
        back = TableContract.from_dict(tc.to_dict())
        self.assertEqual(back.key_rename, {"Id": "encounter_id"})

    def test_seed_temporal_casts_and_money_accuracy(self):
        schema = {"Id": "string", "START": "string", "BASE_COST": "double", "NAME": "string"}
        casts = _seed_type_casts(schema, exclude={"Id"})
        self.assertIn("START", casts)
        self.assertEqual(casts["START"]["to"], "TIMESTAMP")
        accuracy = _seed_accuracy_assertions(schema, exclude=set())
        ids = {a["id"] for a in accuracy}
        self.assertIn("nonneg_base_cost", ids)
        self.assertTrue(all(a["min_value"] == 0 for a in accuracy))


class SchemaIncompatibleGroupSplitTests(unittest.TestCase):
    def test_unrelated_datasets_sharing_a_filename_prefix_are_split(self):
        by_logical = {
            "exception": [
                {"path": "workspaces/x/datasets/ops/exception_operations.csv",
                 "schema": {"Id": "string", "ship_ref": "string", "Date": "string", "exc_cd": "string", "note": "string"}},
                {"path": "workspaces/x/datasets/ops/exception_reference.csv",
                 "schema": {"exc_cd": "string", "Name": "string", "severity": "string"}},
            ]
        }
        result = _split_schema_incompatible_groups(by_logical)
        self.assertEqual(len(result["exception"]), 1)
        self.assertIn("exception_reference", result)
        self.assertEqual(len(result["exception_reference"]), 1)

    def test_genuinely_multi_source_same_schema_stays_grouped(self):
        by_logical = {
            "patient": [
                {"path": "workspaces/x/datasets/hospital-a/patients.csv",
                 "schema": {"Id": "string", "Name": "string", "DOB": "string"}},
                {"path": "workspaces/x/datasets/hospital-b/patients.csv",
                 "schema": {"Id": "string", "Name": "string", "DOB": "string"}},
            ]
        }
        result = _split_schema_incompatible_groups(by_logical)
        self.assertEqual(len(result), 1)
        self.assertEqual(len(result["patient"]), 2)

    def test_single_dataset_group_is_unchanged(self):
        by_logical = {"vendor": [{"path": "workspaces/x/datasets/vendor.csv", "schema": {"Id": "string"}}]}
        result = _split_schema_incompatible_groups(by_logical)
        self.assertEqual(result, by_logical)

    def test_a_single_shared_lookup_key_alone_does_not_imply_sameness(self):
        # A small reference table sharing only its join/lookup key (not most
        # of its schema) with a fact table hits a bare 50% overlap ratio
        # (1 shared column / 2 total on the smaller side) -- that must still
        # split, since sharing a foreign key means "related", not "the same
        # entity from another source".
        by_logical = {
            "fuel": [
                {"path": "workspaces/x/datasets/fleet/fuel_logs.csv",
                 "schema": {"Id": "string", "vin": "string", "Date": "string", "litres": "double", "Amount": "double"}},
                {"path": "workspaces/x/datasets/pricing/fuel_prices.csv",
                 "schema": {"Date": "string", "pence_per_litre": "double"}},
            ]
        }
        result = _split_schema_incompatible_groups(by_logical)
        self.assertEqual(len(result["fuel"]), 1)
        self.assertIn("fuel_prices", result)


class IdempotentMergeTests(unittest.TestCase):
    def _con(self):
        con = duckdb.connect()
        con.execute("CREATE SCHEMA silver; CREATE SCHEMA gold;")
        return con

    def test_silver_merge_is_idempotent(self):
        con = self._con()
        con.execute("CREATE TABLE bronze_src AS SELECT * FROM (VALUES (1,'a'),(2,'b')) t(encounter_id, v);")
        p0 = ("CREATE OR REPLACE TABLE silver.encounter AS\n"
              "WITH unioned AS (SELECT *, 's' AS source_system FROM bronze_src)\nSELECT * FROM unioned;")
        merge = emit_silver_merge("encounter", ["source_system", "encounter_id"], p0)
        con.execute("DROP TABLE IF EXISTS silver.encounter;")
        run = lambda: [con.execute(s) for s in merge.split(";") if s.strip()]
        run()
        n1 = con.execute("SELECT COUNT(*) FROM silver.encounter").fetchone()[0]
        run()
        n2 = con.execute("SELECT COUNT(*) FROM silver.encounter").fetchone()[0]
        self.assertEqual(n1, n2)
        self.assertEqual(n1, 2)

    def test_scd2_idempotent_and_tracks_history(self):
        con = self._con()
        con.execute("CREATE TABLE src AS SELECT * FROM (VALUES (1,'free'),(2,'free')) t(user_id, plan);")
        scd2 = emit_scd2_merge("dim_user", ["user_id"], ["plan"], "SELECT user_id, plan FROM src")
        run = lambda: [con.execute(s) for s in scd2.split(";") if s.strip()]
        run()
        n1 = con.execute("SELECT COUNT(*) FROM gold.dim_user").fetchone()[0]
        run()
        n2 = con.execute("SELECT COUNT(*) FROM gold.dim_user").fetchone()[0]
        self.assertEqual(n1, n2)  # idempotent
        con.execute("DELETE FROM src; INSERT INTO src VALUES (1,'premium'),(2,'free');")
        run()
        u1 = con.execute(
            "SELECT COUNT(*) FROM gold.dim_user WHERE user_id=1").fetchone()[0]
        self.assertEqual(u1, 2)  # closed 'free' + current 'premium'
        cur = con.execute(
            "SELECT plan FROM gold.dim_user WHERE user_id=1 AND valid_to IS NULL").fetchone()[0]
        self.assertEqual(cur, "premium")


class SlaContractTests(unittest.TestCase):
    def test_sla_contract_picks_freshness_column(self):
        from types import SimpleNamespace
        manifest = SimpleNamespace(silver=[SimpleNamespace(name="encounter")])
        contract = SimpleNamespace(tables={
            "encounter": TableContract(type_casts={"START": __import__(
                "core.medallion.silver_contract", fromlist=["TypeCast"]).TypeCast("string", "TIMESTAMP")}),
        })
        sla = _build_sla_contract("demo", manifest, contract)
        entry = sla["tables"][0]
        self.assertEqual(entry["freshness_column"], "START")
        self.assertEqual(entry["max_lag_hours"], 24)
        self.assertIn("retention_policy", sla)


if __name__ == "__main__":
    unittest.main()
